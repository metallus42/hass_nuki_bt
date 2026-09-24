"""Validate the installable ZIP, including materialized English translations."""

import argparse
import ast
import json
from pathlib import Path, PurePosixPath
import stat
from zipfile import ZipFile


REQUIRED_FILES = {
    "__init__.py", "binary_sensor.py", "button.py", "config_flow.py", "const.py",
    "coordinator.py", "entity.py", "event.py", "lock.py", "logs.py", "pairing.py",
    "protocol.py", "sensor.py", "switch.py", "services.yaml", "manifest.json",
    "strings.json", "translations/en.json", "translations/de.json",
}


def validate_release(path: Path, expected_version: str | None = None) -> None:
    """Reject archives that cannot supply valid Home Assistant resources."""
    with ZipFile(path) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise ValueError(f"Corrupt archive member: {bad_file}")
        files = {member.filename for member in archive.infolist() if not member.is_dir()}
        missing = REQUIRED_FILES - files
        if missing:
            raise ValueError(f"Missing integration files: {', '.join(sorted(missing))}")
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("Archive contains duplicate paths")
        documents = {}
        for member in archive.infolist():
            path_parts = PurePosixPath(member.filename)
            if path_parts.is_absolute() or ".." in path_parts.parts or "\\" in member.filename:
                raise ValueError(f"Unsafe archive path: {member.filename}")
            if member.is_dir():
                continue
            if stat.S_ISLNK(member.external_attr >> 16):
                raise ValueError(f"Archive contains a symlink: {member.filename}")
            data = archive.read(member)
            if member.filename.endswith(".json"):
                try:
                    document = json.loads(data)
                except ValueError as error:
                    raise ValueError(f"Invalid JSON: {member.filename}") from error
                if not isinstance(document, dict):
                    raise TypeError(f"Expected a JSON object: {member.filename}")
                documents[member.filename] = document
            elif member.filename.endswith(".py"):
                compile(data, member.filename, "exec")
                for node in ast.walk(ast.parse(data)):
                    if isinstance(node, ast.ImportFrom) and node.level and node.module:
                        parent = path_parts.parent
                        for _ in range(node.level - 1):
                            parent = parent.parent
                        module = parent / node.module.replace(".", "/")
                        if f"{module}.py" not in files and f"{module}/__init__.py" not in files:
                            raise ValueError(f"Missing relative import {module} in {member.filename}")
        if documents["translations/en.json"] != documents["strings.json"]:
            raise ValueError("English translations must match strings.json")
        manifest = documents["manifest.json"]
        if manifest.get("domain") != "hass_nuki_bt":
            raise ValueError("Unexpected integration domain")
        if expected_version is not None and manifest.get("version") != expected_version:
            raise ValueError("Manifest version does not match release tag")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    validate_release(args.archive, args.expected_version)
