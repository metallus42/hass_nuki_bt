"""Catch manifests that import successfully but violate HA's installer policy."""

import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

spec = importlib.util.spec_from_file_location(
    "install_requirements", Path(__file__).parents[1] / "scripts/install_requirements.py"
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class DependencyConstraintsTests(unittest.TestCase):
    """Exercise pip's real constraint resolver without network or installation."""

    def resolve(self, artifact):
        """Resolve only already installed dependencies in the isolated runtime."""
        return subprocess.run(
            [*installer.installation_command(artifact, dry_run=True), "--no-index", "--no-deps"],
            capture_output=True, text=True, timeout=30, check=False,
        )

    def test_current_manifest_resolves_under_ha_constraints(self):
        """The release dependencies satisfy the same constraints HA applies."""
        manifest = Path(__file__).parents[1] / "custom_components/hass_nuki_bt/manifest.json"
        result = self.resolve(manifest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_archive_with_incompatible_timeout_is_rejected(self):
        """Reproduce the 0.0.27 conflict before an archive can be published."""
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad_release.zip"
            with ZipFile(archive, "w") as zipped:
                zipped.writestr("manifest.json", json.dumps({"requirements": ["async-timeout==5.0.1"]}))
            result = self.resolve(archive)
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        self.assertIn("5.0.1", output)
        self.assertIn("4.0.3", output)


if __name__ == "__main__":
    unittest.main()
