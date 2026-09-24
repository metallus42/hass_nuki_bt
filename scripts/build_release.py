"""Build an installable integration archive without development artifacts."""

import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build_release(source: Path, target: Path) -> None:
    """Archive only the integration's Python, JSON and YAML resources."""
    with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".json", ".yaml"}:
                if path.is_symlink():
                    raise ValueError(f"Release resource must be a regular file: {path}")
                archive.write(path, path.relative_to(source))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    build_release(Path(__file__).resolve().parents[1] / "custom_components/hass_nuki_bt", args.archive)
