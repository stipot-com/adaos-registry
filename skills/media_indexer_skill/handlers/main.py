"""AdaOS-скилл для индексации и семантического поиска по локальным медиафайлам.

Модуль повторяет ML-pipeline настольного приложения: сканирует папку,
извлекает технические метаданные, распознает сущности из имен файлов,
обогащает данные через внешние сервисы и строит FAISS-индекс для поиска
по видео, аудио и изображениям.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import sys
import time
from typing import Any, Dict, List

import yaml

from adaos.sdk.core.decorators import subscribe, tool
from adaos.sdk.data import ctx_subnet
from adaos.sdk.data.context import clear_current_skill, set_current_skill
from adaos.services.agent_context import get_ctx

_SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from lib.scanner import DirectoryScanner
from lib.extractor import TechnicalMetadataExtractor
from lib.ner_predictor import NERPredictor
from lib.enrichment import EnrichmentService
from lib.vector_db import VectorDatabase

logger = logging.getLogger(__name__)

SCORE_THRESHOLD = 25.0
DEFAULT_DEMO_DIRECTORY = r"D:\diploma_final\demo_media"

_state: Dict[str, Any] = {
    "scanner": None,
    "extractor": None,
    "ner": None,
    "enricher": None,
    "vector_db": None,
    "indexed_directory": None,
    "selected_directory": DEFAULT_DEMO_DIRECTORY,
    "selected_query": "test",
}


def _event_payload(evt: Any) -> Dict[str, Any]:
    payload = getattr(evt, "payload", None) if hasattr(evt, "payload") else evt
    return payload if isinstance(payload, dict) else {}


def _target_context(payload: Dict[str, Any]) -> tuple[bool, str | None]:
    meta = payload.get("_meta") if isinstance(payload.get("_meta"), dict) else {}
    target_node_id = str(
        payload.get("target_node_id")
        or payload.get("node_id")
        or meta.get("target_node_id")
        or meta.get("node_target_id")
        or ""
    ).strip()
    try:
        local_node_id = str(getattr(get_ctx().config, "node_id", "") or "").strip()
    except Exception:
        local_node_id = ""
    if target_node_id and local_node_id and target_node_id != local_node_id:
        return False, None
    raw_ws = payload.get("webspace_id") or payload.get("workspace_id") or meta.get("webspace_id") or meta.get("workspace_id")
    return True, str(raw_ws).strip() if raw_ws else None


def _load_skill_data_projections() -> None:
    try:
        ctx = get_ctx()
        try:
            existing = ctx.projections.resolve("subnet", "media_indexer.snapshot")
        except Exception:
            existing = []
        if existing:
            return
        skills_root = ctx.paths.skills_workspace_dir()
        skills_root = skills_root() if callable(skills_root) else skills_root
        manifest_path = pathlib.Path(skills_root) / "media_indexer_skill" / "skill.yaml"
        if not manifest_path.exists():
            return
        spec = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        entries = spec.get("data_projections") or []
        if isinstance(entries, list) and entries:
            ctx.projections.load_entries(entries)
    except Exception:
        logger.debug("failed to load media_indexer_skill data_projections", exc_info=True)


def _current_form(directory: str | None = None, query: str | None = None, k: int | None = None) -> Dict[str, Any]:
    return {
        "directory": directory or _state.get("selected_directory") or _state.get("indexed_directory") or DEFAULT_DEMO_DIRECTORY,
        "query": query or _state.get("selected_query") or "test",
        "k": int(k or 5),
    }


def _resolve_directory(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("directory") or "").strip()
    if not raw or raw.startswith("$"):
        raw = str(_state.get("selected_directory") or _state.get("indexed_directory") or DEFAULT_DEMO_DIRECTORY).strip()
    return raw or DEFAULT_DEMO_DIRECTORY


def _resolve_query(payload: Dict[str, Any]) -> str:
    raw = str(payload.get("query") or "").strip()
    if not raw or raw.startswith("$"):
        raw = str(_state.get("selected_query") or "test").strip()
    return raw or "test"


def _status_payload(
    *,
    value: str,
    subtitle: str,
    description: str,
    error: str = "",
    indexed_count: int | None = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "value": value,
        "label": "Media Indexer",
        "subtitle": subtitle,
        "description": description,
        "error": error,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if indexed_count is not None:
        payload["indexed_count"] = int(indexed_count)
    return payload


def _snapshot_payload(
    *,
    status: Dict[str, Any],
    form: Dict[str, Any] | None = None,
    results: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "status": status,
        "form": form or _current_form(),
        "results": results or [],
    }


def _project_snapshot(snapshot: Dict[str, Any], *, webspace_id: str | None = None) -> None:
    pushed = False
    try:
        _load_skill_data_projections()
        pushed = set_current_skill("media_indexer_skill")
        ctx_subnet.set("media_indexer.snapshot", snapshot, webspace_id=webspace_id)
    except Exception:
        logger.warning("failed to project media_indexer.snapshot", exc_info=True)
    finally:
        if pushed:
            clear_current_skill()


def _ensure_initialized() -> None:
    """Lazy-инициализация всех ML-модулей при первом вызове."""
    if _state["vector_db"] is None:
        logger.info("Инициализация ML-компонентов media_indexer_skill...")
        _state["scanner"] = None
        _state["extractor"] = TechnicalMetadataExtractor()
        _state["ner"] = NERPredictor()
        _state["enricher"] = EnrichmentService()
        _state["vector_db"] = VectorDatabase()
        logger.info("ML-компоненты инициализированы")


def has_cyrillic(text: str) -> bool:
    """True, если в тексте есть кириллические символы."""
    return bool(text) and bool(re.search(r"[\u0400-\u04FF]", text))


def _flatten_inventory(inventory: Dict[str, List[Any]]) -> List[tuple[Any, str]]:
    all_files: List[tuple[Any, str]] = []
    for m_type, m_list in inventory.items():
        all_files.extend((media, m_type) for media in m_list)
    return all_files


def _build_display_title(stem: str, title: str, artist: str) -> str:
    if artist and title:
        return f"{artist} - {title}"
    if title:
        return title
    return stem


@tool("scan_and_index")
def scan_and_index(directory: str) -> Dict[str, Any]:
    """
    Сканирует указанную папку, прогоняет медиафайлы через ML-pipeline
    (NER + enrichment + FAISS-индексирование) и подготавливает индекс
    для последующего поиска.
    """
    _ensure_initialized()

    path = pathlib.Path(directory).expanduser()
    if not path.exists() or not path.is_dir():
        return {
            "status": "error",
            "indexed_count": 0,
            "errors": [f"Directory not found or not a directory: {directory}"],
        }

    try:
        scanner = DirectoryScanner(str(path), compute_hashes=False)
        _state["scanner"] = scanner
        inventory = scanner.scan()
    except Exception as exc:
        logger.exception("Ошибка при сканировании директории %s", path)
        return {"status": "error", "indexed_count": 0, "errors": [str(exc)]}

    all_files = _flatten_inventory(inventory)
    if not all_files:
        _state["indexed_directory"] = str(path)
        return {"status": "ok", "indexed_count": 0, "errors": []}

    extractor = _state["extractor"]
    ner = _state["ner"]
    enricher = _state["enricher"]
    vector_db = _state["vector_db"]

    errors: List[str] = []
    indexed = 0

    for media, ftype in all_files:
        try:
            logger.info("Анализ файла: %s", media.name)

            extractor.extract(media.full_path, ftype)

            ner_result = ner.extract_entities(media.name)
            title = ner_result.get("title") or ""
            year = ner_result.get("year") or "---"
            quality = ner_result.get("quality") or "---"
            artist = ner_result.get("artist") or ""

            enriched = enricher.enrich(media.full_path, ftype)
            if ftype == "video" and title:
                imdb_data = enricher.enrich_video(title)
                enriched.update(imdb_data)

            stem = pathlib.Path(media.name).stem
            display_title = _build_display_title(stem, title, artist)

            payload = {
                "real_file_name": media.name,
                "display_title": display_title,
                "full_path": media.full_path,
                "type": ftype,
                "ftype": ftype,
                "title": display_title,
                "ner_title": title,
                "year": year,
                "quality": quality,
                "artist": artist,
                "enriched": enriched,
            }

            if ftype == "image":
                vector_db.add_image(media.full_path, payload)
                parts = ["фотография фото изображение photo image", stem]
                search_text = " ".join(parts)
                vector_db.add_text(search_text, payload)

            elif ftype == "audio":
                parts = ["музыка аудио песня трек"]
                if artist:
                    parts.append(f"исполнитель {artist}")
                if title:
                    parts.append(f"название {title}")
                if quality and quality != "---":
                    parts.append(quality)

                if enriched.get("shazam_title"):
                    parts.append(f"shazam {enriched['shazam_title']}")
                if enriched.get("shazam_subtitle"):
                    parts.append(f"shazam-исполнитель {enriched['shazam_subtitle']}")
                if enriched.get("shazam_genre"):
                    parts.append(f"жанр {enriched['shazam_genre']}")

                if has_cyrillic(stem):
                    parts.append("русская русский на русском")

                parts.append(stem)
                search_text = " ".join(filter(bool, parts))
                vector_db.add_text(search_text, payload)

            elif ftype == "video":
                parts = ["видео фильм кино"]
                if title:
                    parts.append(f"название {title}")
                if year != "---":
                    parts.append(f"год {year}")
                if quality != "---":
                    parts.append(quality)
                if "imdb" in enriched:
                    plot = enriched["imdb"].get("plot", "")
                    if plot:
                        parts.append(plot)

                if has_cyrillic(stem):
                    parts.append("русское кино русский фильм")

                parts.append(stem)
                search_text = " ".join(filter(bool, parts))
                vector_db.add_text(search_text, payload)

            else:
                logger.info("Пропущен неподдерживаемый тип медиа: %s (%s)", media.name, ftype)
                continue

            indexed += 1
            logger.info("Файл успешно проиндексирован: %s", media.name)

        except Exception as exc:
            logger.exception("Ошибка при обработке %s", getattr(media, "name", "unknown"))
            errors.append(f"{getattr(media, 'name', 'unknown')}: {exc}")

    _state["indexed_directory"] = str(path)

    return {"status": "ok", "indexed_count": indexed, "errors": errors}


@tool("search_media")
def search_media(query: str, k: int = 5) -> Dict[str, Any]:
    """
    Семантический поиск по проиндексированным медиафайлам.
    Возвращает top-k результатов, отфильтрованных по порогу уверенности.
    """
    if _state["vector_db"] is None:
        return {
            "status": "error",
            "results": [],
            "message": "Index is empty. Call scan_and_index first.",
        }

    if not query or not query.strip():
        return {"status": "ok", "results": []}

    try:
        limit = int(k or 5)
    except (TypeError, ValueError):
        limit = 5

    raw_results = _state["vector_db"].search(query.strip(), k=limit)
    valid_results = [result for result in raw_results if result.get("score", 0) >= SCORE_THRESHOLD]

    formatted = [
        {
            "score": float(result.get("score", 0.0)),
            "path": result.get("payload", {}).get("full_path", ""),
            "payload": result.get("payload", {}),
        }
        for result in valid_results
    ]

    return {"status": "ok", "results": formatted}


@subscribe("media_indexer.action")
async def on_media_indexer_action(evt: Any) -> None:
    payload = _event_payload(evt)
    allowed, webspace_id = _target_context(payload)
    if not allowed:
        return

    action_id = str(payload.get("id") or payload.get("action") or "").strip().lower()
    directory = _resolve_directory(payload)
    query = _resolve_query(payload)
    try:
        k = int(payload.get("k") or 5)
    except (TypeError, ValueError):
        k = 5

    _state["selected_directory"] = directory
    _state["selected_query"] = query
    form = _current_form(directory=directory, query=query, k=k)

    if action_id in {"set_directory", "directory"}:
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="ready",
                    subtitle="directory selected",
                    description=f"Selected {directory}",
                ),
                form=form,
            ),
            webspace_id=webspace_id,
        )
        return

    if action_id in {"set_query", "query"}:
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="ready",
                    subtitle="query selected",
                    description=f"Query: {query}",
                ),
                form=form,
            ),
            webspace_id=webspace_id,
        )
        return

    if action_id == "scan":
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="scanning",
                    subtitle="indexing media files",
                    description=f"Scanning {directory}",
                ),
                form=form,
            ),
            webspace_id=webspace_id,
        )
        result = await asyncio.to_thread(scan_and_index, directory)
        errors = list(result.get("errors") or [])
        ok = str(result.get("status") or "").lower() == "ok"
        indexed_count = int(result.get("indexed_count") or 0)
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="indexed" if ok else "error",
                    subtitle=f"{indexed_count} files indexed" if ok else "scan failed",
                    description="Index is ready for semantic search." if ok else "; ".join(errors[:3]),
                    error="" if ok else "; ".join(errors[:3]),
                    indexed_count=indexed_count,
                ),
                form=form,
            ),
            webspace_id=webspace_id,
        )
        return

    if action_id == "search":
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="searching",
                    subtitle="semantic search",
                    description=f"Searching for: {query}",
                ),
                form=form,
            ),
            webspace_id=webspace_id,
        )
        result = await asyncio.to_thread(search_media, query, k=k)
        results = list(result.get("results") or [])
        error = str(result.get("message") or "") if str(result.get("status") or "").lower() != "ok" else ""
        _project_snapshot(
            _snapshot_payload(
                status=_status_payload(
                    value="done" if not error else "error",
                    subtitle=f"{len(results)} results",
                    description=f"Query: {query}" if not error else error,
                    error=error,
                ),
                form=form,
                results=results,
            ),
            webspace_id=webspace_id,
        )
