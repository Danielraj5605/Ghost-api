"""Check that a built wheel ships every dashboard, report and template file.

Usage: python scripts/check_package.py dist/ghost_api-*.whl

The dashboard (ghost_api/web/static) and the report viewer (ghost_api/export/viewer) are
built from web/ and committed. CI rebuilds them and fails if they differ from the source;
this script then makes sure the wheel contains the same files.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGED = ("ghost_api/web/static", "ghost_api/export/viewer", "ghost_api/generator/templates")


def expected_files() -> set[str]:
    files: set[str] = set()
    for folder in PACKAGED:
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".py":
                files.add(path.relative_to(ROOT).as_posix())
    return files


def main(wheel: str) -> int:
    with zipfile.ZipFile(wheel) as archive:
        shipped = set(archive.namelist())
    missing = sorted(expected_files() - shipped)
    if missing:
        print("The wheel is missing packaged files:")
        for name in missing:
            print(f"  {name}")
        return 1
    print(f"OK: {len(expected_files())} dashboard, report and template files are in the wheel.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
