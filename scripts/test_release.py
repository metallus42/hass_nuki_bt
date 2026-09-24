"""Import and regression-test the extracted release in the HA runtime."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile

from validate_release import validate_release


def test_release(archive: Path) -> None:
    """Run tests against the ZIP contents, never the source checkout."""
    validate_release(archive)
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="nuki-release-test-") as directory:
        stage = Path(directory)
        package = stage / "custom_components/hass_nuki_bt"
        package.mkdir(parents=True)
        with ZipFile(archive) as zipped:
            zipped.extractall(package)
        for name in ("tests", "scripts"):
            shutil.copytree(source / name, stage / name, ignore=shutil.ignore_patterns("__pycache__"))
        env = os.environ | {"PYTHONPATH": str(stage), "PYTHONDONTWRITEBYTECODE": "1"}
        modules = [f"custom_components.hass_nuki_bt.{p.stem}" for p in package.glob("*.py") if p.stem != "__init__"]
        subprocess.run(
            [sys.executable, "-c", "import importlib, sys; [importlib.import_module(name) for name in sys.argv[1:]]", *modules],
            cwd=stage, env=env, check=True, timeout=60,
        )
        subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=stage, env=env, check=True, timeout=120)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    test_release(parser.parse_args().archive.resolve())
