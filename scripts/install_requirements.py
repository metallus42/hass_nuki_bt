"""Install release manifest requirements using Home Assistant's constraints."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

from packaging.requirements import Requirement


def installation_command(artifact: Path, *, dry_run: bool = False) -> list[str]:
    """Use the shipped dependency list and the running HA version's policy."""
    import homeassistant

    constraints = Path(homeassistant.__file__).with_name("package_constraints.txt")
    if not constraints.is_file():
        raise FileNotFoundError(f"Home Assistant package constraints are missing: {constraints}")
    if artifact.suffix == ".zip":
        with ZipFile(artifact) as archive:
            manifest = json.loads(archive.read("manifest.json"))
    else:
        manifest = json.loads(artifact.read_text())
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise ValueError("The integration manifest must declare its requirements")
    for requirement in requirements:
        Requirement(requirement)
    command = [
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--constraint", str(constraints),
    ]
    if dry_run:
        command.append("--dry-run")
    return command + requirements


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="Release ZIP or integration manifest.json")
    parser.add_argument("--dry-run", action="store_true", help="Resolve dependencies without installing")
    args = parser.parse_args()
    subprocess.run(installation_command(args.artifact, dry_run=args.dry_run), check=True)
