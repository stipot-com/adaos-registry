"""AdaOS-скилл для индексации и семантического поиска по локальным медиафайлам.

Модуль повторяет ML-pipeline настольного приложения: сканирует папку,
извлекает технические метаданные, распознает сущности из имен файлов,
обогащает данные через внешние сервисы и строит FAISS-индекс для поиска
по видео, аудио и изображениям.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys
from typing import Any, Dict, List

from adaos.sdk.core.decorators import tool

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

_state: Dict[str, Any] = {
    "scanner": None,
    "extractor": None,
    "ner": None,
    "enricher": None,
    "vector_db": None,
    "indexed_directory": None,
}


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