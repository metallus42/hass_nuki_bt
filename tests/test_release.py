"""Reject installable archives that are valid ZIPs but broken integrations."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_validator", ROOT / "scripts/validate_release.py")
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class ReleaseTests(unittest.TestCase):
    """Exercise missing modules and tag mismatches using real package content."""

    def validate_modified_package(self, omit=None, extra=None, expected_version=None):
        """Build an isolated archive with a selected packaging defect."""
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "release.zip"
            package = ROOT / "custom_components/hass_nuki_bt"
            with ZipFile(archive, "w") as zipped:
                for path in package.rglob("*"):
                    name = str(path.relative_to(package))
                    if path.is_file() and path.suffix in {".py", ".json", ".yaml"} and name != omit:
                        zipped.write(path, name)
                if extra:
                    zipped.writestr(*extra)
            validator.validate_release(archive, expected_version)

    def test_complete_package_is_valid(self):
        """The actual release resources satisfy the install contract."""
        version = json.loads((ROOT / "custom_components/hass_nuki_bt/manifest.json").read_text())["version"]
        self.validate_modified_package(expected_version=version)

    def test_missing_runtime_modules_are_rejected(self):
        """A syntax-valid ZIP cannot omit its initializer or action/log helpers."""
        for module in ("__init__.py", "logs.py", "protocol.py", "switch.py"):
            with self.subTest(module=module), self.assertRaisesRegex(ValueError, "Missing integration"):
                self.validate_modified_package(omit=module)

    def test_missing_relative_import_is_rejected(self):
        """New helper dependencies must be shipped along with their caller."""
        with self.assertRaisesRegex(ValueError, "Missing relative import"):
            self.validate_modified_package(extra=("extra.py", "from .missing_helper import action\n"))

    def test_wrong_release_version_is_rejected(self):
        """The installed manifest must report the published tag."""
        with self.assertRaisesRegex(ValueError, "version"):
            self.validate_modified_package(expected_version="0.0.0-incorrect")

    def test_archive_cannot_escape_target_directory(self):
        """Validation must happen before extracting a downloaded archive."""
        with self.assertRaisesRegex(ValueError, "Unsafe archive path"):
            self.validate_modified_package(extra=("../outside.py", ""))
