from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISALLOWED_SUFFIXES = {
    ".7z",
    ".bin",
    ".bz2",
    ".der",
    ".dmg",
    ".dll",
    ".exe",
    ".gz",
    ".img",
    ".iso",
    ".msi",
    ".msu",
    ".p12",
    ".pfx",
    ".pkg",
    ".rar",
    ".tar",
    ".tgz",
    ".xz",
    ".zip",
}


class RepositoryHygieneTests(unittest.TestCase):
    def _tracked_files(self) -> list[str]:
        completed = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            self.skipTest("Git index is not available")
        return [path for path in completed.stdout.decode("utf-8").split("\0") if path]

    def test_git_does_not_track_binary_artifacts(self) -> None:
        offenders = sorted(
            path
            for path in self._tracked_files()
            if any(path.lower().endswith(suffix) for suffix in DISALLOWED_SUFFIXES)
        )
        self.assertEqual([], offenders, f"stage binary dependencies locally instead: {offenders}")

    def test_public_tree_has_license(self) -> None:
        license_path = ROOT / "LICENSE"
        self.assertTrue(license_path.is_file())
        self.assertIn(
            "GNU GENERAL PUBLIC LICENSE",
            license_path.read_text(encoding="utf-8"),
        )

    def test_public_tree_has_no_private_range_markers(self) -> None:
        markers = (
            "C" + "TIH",
            "c" + "tih",
            "AI" + "MS",
            "dunder" + "mifflin",
            "001" + "470" + "352",
        )
        offenders: list[str] = []
        for relative in self._tracked_files():
            path = ROOT / relative
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(marker in content for marker in markers):
                offenders.append(relative)
        self.assertEqual([], offenders, f"move private range details to a content pack: {offenders}")

    def test_public_tree_has_no_private_key_blocks(self) -> None:
        markers = (
            "-----BEGIN " + "PRIVATE KEY-----",
            "-----BEGIN " + "RSA PRIVATE KEY-----",
        )
        offenders: list[str] = []
        for relative in self._tracked_files():
            path = ROOT / relative
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(marker in content for marker in markers):
                offenders.append(relative)
        self.assertEqual([], offenders, f"remove private keys from Git: {offenders}")


if __name__ == "__main__":
    unittest.main()
