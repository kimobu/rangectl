from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rangectl_lib.activity.actions.scenario import _scenario_log
from rangectl_lib.proxmox_api import ProxmoxClient
from rangectl_lib.process import run_bounded
from rangectl_lib.security import safe_child_path, secure_write_text
from rangectl_lib.template_builds import fetch_asset


class _Response:
    def __init__(self, body: bytes, *, content_length: int | None = None) -> None:
        self.body = body
        self.offset = 0
        self.url = "https://cdn.example.test/asset.bin"
        self.headers = {} if content_length is None else {"Content-Length": str(content_length)}

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        chunk = self.body[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class SecurityTests(unittest.TestCase):
    def test_proxmox_tls_verification_is_default(self) -> None:
        base = {"PROXMOX_VE_ENDPOINT": "https://pve.test:8006", "PROXMOX_VE_API_TOKEN": "token"}
        self.assertTrue(ProxmoxClient.from_env(base).verify_tls)
        self.assertFalse(ProxmoxClient.from_env({**base, "PROXMOX_VE_INSECURE": "true"}).verify_tls)

    def test_pfsense_ssh_host_key_verification_is_default(self) -> None:
        root = Path(__file__).resolve().parents[1]
        defaults = (root / "roles/pfsense/defaults/main.yml").read_text(encoding="utf-8")
        tasks = (root / "roles/pfsense/tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("pfsense_ssh_strict_host_key_checking: true", defaults)
        self.assertNotIn("StrictHostKeyChecking=no", tasks)
        self.assertNotIn("UserKnownHostsFile=/dev/null", tasks)

    def test_safe_child_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                safe_child_path(Path(temp), "../outside")

    def test_secure_write_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "secret.json"
            secure_write_text(path, "secret\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_asset_download_is_bounded_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            with patch("urllib.request.urlopen", return_value=_Response(b"oversized")):
                with self.assertRaisesRegex(ValueError, "configured limit"):
                    fetch_asset("https://example.test/asset.bin", cache, max_bytes=4)
            self.assertFalse((cache / "asset.bin").exists())
            self.assertEqual(list(cache.glob("*.part")), [])

    def test_asset_cache_records_and_checks_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            with patch("urllib.request.urlopen", return_value=_Response(b"content")) as opener:
                target = fetch_asset("https://example.test/asset.bin", cache, max_bytes=100)
            self.assertEqual(target.read_bytes(), b"content")
            metadata = json.loads((cache / "asset.bin.rangectl.json").read_text())
            self.assertEqual(metadata["size"], 7)
            with patch("urllib.request.urlopen") as second_opener:
                self.assertEqual(fetch_asset("https://example.test/asset.bin", cache), target)
                second_opener.assert_not_called()
            opener.assert_called_once()

    def test_scenario_log_filename_cannot_escape_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            context = SimpleNamespace(
                state_dir=temp,
                config=SimpleNamespace(range_name="unit", host="host", persona=SimpleNamespace(id="user", username="user")),
            )
            _scenario_log(context, {"id": "../../escape"}, "phase", {})
            self.assertFalse((Path(temp).parent / "escape.jsonl").exists())
            self.assertTrue((Path(temp) / "scenarios" / "------escape.jsonl").exists())

    def test_command_capture_keeps_only_bounded_tail(self) -> None:
        result = run_bounded([sys.executable, "-c", "print('x' * 10000)"], timeout=5, tail_bytes=128)
        self.assertEqual(result["returncode"], 0)
        self.assertLessEqual(len(result["stdout"].encode()), 128)


if __name__ == "__main__":
    unittest.main()
