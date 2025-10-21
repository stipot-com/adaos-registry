import os
import sys
from importlib import import_module
from pathlib import Path


def _ensure_slot_paths() -> None:
    current = Path(__file__).resolve()
    src_root: Path | None = None
    slot_root: Path | None = None

    for parent in current.parents:
        if parent.name == "src":
            src_root = parent
            slot_root = parent.parent
            break

    paths: list[Path] = []
    if src_root and slot_root:
        vendor_root = slot_root / "vendor"
        if vendor_root.is_dir():
            paths.append(vendor_root)
        paths.append(src_root)
    else:
        dev_dir = os.getenv("ADAOS_DEV_SKILL_DIR")
        if dev_dir:
            candidate = Path(dev_dir)
            vendor_root = candidate / "vendor"
            if vendor_root.is_dir():
                paths.append(vendor_root)
            paths.append(candidate)

    for path in reversed(paths):
        path_str = str(path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)


_ensure_slot_paths()


def _skill_module():
    package = os.getenv("ADAOS_SKILL_PACKAGE")
    if package:
        return f"{package}.handlers.main"

    current = Path(__file__).resolve()
    try:
        parts = current.parts
        skills_index = parts.index("skills")
        skill_name = parts[skills_index + 1]
    except (ValueError, IndexError) as exc:  # pragma: no cover
        raise RuntimeError("unable to infer skill package name") from exc
    return f"skills.{skill_name}.handlers.main"


handlers = import_module(_skill_module())
get_weather = getattr(handlers, "get_weather", None)

if __name__ == "__main__":
    if get_weather is None:
        raise SystemExit("handler is missing get_weather tool")
    result = get_weather(city="Test City")
    if not isinstance(result, dict):
        raise SystemExit("result must be a dict")
    if "ok" not in result:
        raise SystemExit("result missing 'ok' field")
    raise SystemExit(0)
