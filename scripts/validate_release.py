"""Validate the installable ZIP, including materialized English translations."""

import argparse
import json
from pathlib import Path
import stat
from zipfile import ZipFile


def validate_release(path: Path) -> None:
    """Reject archives that cannot supply valid Home Assistant resources."""
    with ZipFile(path) as archive:
        bad_file = archive.testzip()
        if bad_file:
            raise ValueError(f"Corrupt archive member: {bad_file}")
        documents = {}
        for member in archive.infolist():
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
        for required in ("manifest.json", "strings.json", "translations/en.json"):
            if required not in documents:
                raise ValueError(f"Missing integration resource: {required}")
        if documents["translations/en.json"] != documents["strings.json"]:
            raise ValueError("English translations must match strings.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    validate_release(parser.parse_args().archive)
