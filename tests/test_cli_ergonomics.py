from __future__ import annotations

import subprocess
import unittest
import importlib.util
from pathlib import Path

from rangectl_lib.model import validate_range


ROOT = Path(__file__).resolve().parents[1]
MAIN_SPEC = importlib.util.spec_from_file_location("rangectl_main", ROOT / "cmd" / "rangectl" / "main.py")
assert MAIN_SPEC is not None
rangectl_main = importlib.util.module_from_spec(MAIN_SPEC)
assert MAIN_SPEC.loader is not None
MAIN_SPEC.loader.exec_module(rangectl_main)


class CliErgonomicsTests(unittest.TestCase):
    def run_rangectl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(ROOT / "rangectl"), *args],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def test_deploy_command_is_removed_from_help(self) -> None:
        completed = self.run_rangectl("--help")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("doctor", completed.stdout)
        self.assertIn("phases", completed.stdout)
        self.assertIn("destroy", completed.stdout)
        self.assertNotIn("deploy", completed.stdout)

    def test_phases_command_lists_manual_and_playbook_paths(self) -> None:
        completed = self.run_rangectl("phases", "example")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("Range: example", completed.stdout)
        self.assertIn("Configure phases: none", completed.stdout)

    def test_phase_validation_requires_structured_entries(self) -> None:
        result = validate_range({
            "apiVersion": "rangectl.dev/v1alpha1",
            "kind": "Range",
            "metadata": {"name": "unit"},
            "providers": {},
            "spaces": {},
            "hosts": [],
            "phases": [
                {"name": "services", "manual": "no", "playbooks": "playbooks/services.yml"},
                {"name": "services", "playbooks": [123]},
            ],
        })

        self.assertIn("phase services manual must be true or false", result.errors)
        self.assertIn("phase services playbooks must be a list", result.errors)
        self.assertIn("duplicate phase name: services", result.errors)
        self.assertIn("phase services playbooks[0] must be a non-empty string", result.errors)

    def test_terraform_parallelism_env_is_validated(self) -> None:
        self.assertEqual(
            rangectl_main._terraform_parallelism_args({"RANGECTL_TERRAFORM_PARALLELISM": "2"}),
            ["-parallelism=2"],
        )
        self.assertEqual(
            rangectl_main._terraform_parallelism_args({"PROXMOX_PARALLELISM": "3"}),
            ["-parallelism=3"],
        )

        with self.assertRaises(ValueError):
            rangectl_main._terraform_parallelism_args({"RANGECTL_TERRAFORM_PARALLELISM": "0"})


if __name__ == "__main__":
    unittest.main()
