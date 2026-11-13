from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rangectl_lib.model import load_range, range_base_dir
from rangectl_lib.render import _proxmox_vm_config, render_ad_compat_data, render_docs
from rangectl_lib.templates import proxmox_template_catalog


RANGE_YAML = """\
apiVersion: rangectl.dev/v1alpha1
kind: Range
metadata:
  name: unitpack
providers: {}
spaces: {}
hosts: []
docs:
  output: docs/generated/unitpack.md
"""


class ContentPackTests(unittest.TestCase):
    def test_load_range_discovers_named_content_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "unitpack"
            pack.mkdir()
            (pack / "range.yml").write_text(RANGE_YAML, encoding="utf-8")

            with patch.dict(os.environ, {"RANGECTL_CONTENT_DIRS": temp}):
                path, data = load_range("unitpack")

            self.assertEqual(path, pack / "range.yml")
            self.assertEqual(range_base_dir(data), pack)

    def test_relative_docs_output_stays_inside_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "unitpack"
            pack.mkdir()
            range_path = pack / "range.yml"
            range_path.write_text(RANGE_YAML, encoding="utf-8")

            path, data = load_range(str(range_path))
            doc_path = render_docs(data, Path("docs") / "generated")

            self.assertEqual(doc_path, pack / "docs" / "generated" / "unitpack.md")
            self.assertTrue(doc_path.exists())

    def test_range_local_template_catalog_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pack = Path(temp) / "unitpack"
            templates = pack / "templates"
            templates.mkdir(parents=True)
            range_path = pack / "range.yml"
            range_path.write_text(RANGE_YAML, encoding="utf-8")
            (templates / "proxmox.yml").write_text(
                """\
proxmox:
  aliases:
    unit-linux: UnitLinux_x64
  vm_templates:
    UnitLinux_x64:
      vmid: 1234
  lxc_templates:
    unit-lxc:
      file_id: local:vztmpl/unit-lxc.tar.zst
""",
                encoding="utf-8",
            )

            _, data = load_range(str(range_path))
            catalog = proxmox_template_catalog(data)

            self.assertEqual(catalog["aliases"]["unit-linux"], "UnitLinux_x64")
            self.assertEqual(catalog["vm_templates"]["UnitLinux_x64"], 1234)
            self.assertEqual(catalog["lxc_templates"]["unit-lxc"], "local:vztmpl/unit-lxc.tar.zst")

    def test_active_directory_compat_config_renders_from_range(self) -> None:
        range_data = {
            "apiVersion": "rangectl.dev/v1alpha1",
            "kind": "Range",
            "metadata": {"name": "unitad"},
            "providers": {},
            "spaces": {},
            "hosts": [],
            "ansible": {"vars": {"domain_name": "unitad"}},
            "active_directory": {
                "lab": {
                    "hosts": {"dc01": {"hostname": "dc01", "domain": "example.local"}},
                    "domains": {"example.local": {"dc": "dc01", "domain_password": "Password1!"}},
                }
            },
        }

        with tempfile.TemporaryDirectory() as temp:
            written = render_ad_compat_data(range_data, Path(temp))
            self.assertEqual(len(written), 1)
            self.assertEqual(written[0].name, "config.json")
            self.assertEqual(written[0], Path(temp) / "ad" / "unitad" / "data" / "config.json")
            self.assertIn('"example.local"', written[0].read_text(encoding="utf-8"))

    def test_windows_11_proxmox_vm_uses_uefi_hardware(self) -> None:
        host = {
            "name": "pam-pc",
            "hostname": "pam-pc",
            "os": "windows",
            "template": "win11",
            "space": "workstations",
            "ip": "192.168.20.103",
            "resources": {"cpu": 2, "memory_mb": 4096},
        }
        spaces = {
            "workstations": {
                "cidr": "192.168.20.0/24",
                "gateway": "192.168.20.1",
                "dns": "192.168.10.10",
                "vlan": 50,
            }
        }
        templates = {"win11": "Windows11_LTSC_x64"}

        config = _proxmox_vm_config(host, spaces, templates)

        self.assertEqual(config["os_type"], "win11")
        self.assertEqual(config["bios"], "ovmf")
        self.assertEqual(config["machine"], "pc-q35-11.0")
        self.assertEqual(config["scsi_hardware"], "virtio-scsi-single")


if __name__ == "__main__":
    unittest.main()
