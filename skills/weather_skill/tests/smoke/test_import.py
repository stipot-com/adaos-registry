import importlib
import os
import sys
from pathlib import Path


def _ensure_slot_paths() -> None:
    """Insert vendored and source directories for the active slot or DEV skill."""

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


def _skill_package() -> str:
    package = os.getenv("ADAOS_SKILL_PACKAGE")
    if package:
        return package

    current = Path(__file__).resolve()
    try:
        parts = current.parts
        skills_index = parts.index("skills")
        skill_name = parts[skills_index + 1]
    except (ValueError, IndexError) as exc:  # pragma: no cover - guard
        raise RuntimeError("unable to infer skill package name") from exc
    return f"skills.{skill_name}"


if __name__ == "__main__":
    _ensure_slot_paths()
    importlib.import_module(f"{_skill_package()}.handlers.main")
