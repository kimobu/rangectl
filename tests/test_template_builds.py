from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from rangectl_lib.template_builds import infer_template_source_hints, render_packer_windows_proxmox_build


class TemplateBuildTests(unittest.TestCase):
    def test_microsoft_fwlink_query_and_filename_hints(self) -> None:
        hints = infer_template_source_hints(
            "https://go.microsoft.com/fwlink/p/?LinkID=2195174&clcid=0x409&culture=en-us&country=US",
            resolved_url="https://software-static.download.prss.microsoft.com/pr/download/Windows_Server_2016_Datacenter_EVAL_en-us_14393_refresh.ISO",
            filename="Windows_Server_2016_Datacenter_EVAL_en-us_14393_refresh.ISO",
            headers={"content-length": "6972221440"},
        )

        self.assertEqual(hints["culture"], "en-us")
        self.assertEqual(hints["country"], "US")
        self.assertEqual(hints["locale"], "en-US")
        self.assertEqual(hints["keyboard"], "en-US")
        self.assertEqual(hints["size_bytes"], 6972221440)
        self.assertEqual(hints["os"]["family"], "windows")
        self.assertEqual(hints["os"]["product"], "windows-server")
        self.assertEqual(hints["os"]["version"], "server-2016")
        self.assertEqual(hints["os"]["edition_hint"], "Datacenter")
        self.assertEqual(hints["os"]["license_hint"], "evaluation")
        self.assertNotIn("arch", hints["os"])

    def test_archive_winblue_server_2012_r2_hint(self) -> None:
        hints = infer_template_source_hints(
            "https://archive.org/download/wserver2012r2eval/9600.17050.WINBLUE_REFRESH.140317-1640_X64FRE_SERVER_EVAL_EN-US-IR3_SSS_X64FREE_EN-US_DV9.ISO",
            resolved_url="https://dn.example/9600.17050.WINBLUE_REFRESH.140317-1640_X64FRE_SERVER_EVAL_EN-US-IR3_SSS_X64FREE_EN-US_DV9.ISO",
            filename="9600.17050.WINBLUE_REFRESH.140317-1640_X64FRE_SERVER_EVAL_EN-US-IR3_SSS_X64FREE_EN-US_DV9.ISO",
            headers={"content-length": "4542291968"},
        )

        self.assertEqual(hints["locale"], "en-US")
        self.assertEqual(hints["os"]["version"], "server-2012-r2")
        self.assertEqual(hints["os"]["license_hint"], "evaluation")

    def test_rendered_windows_template_preserves_cloudbase_and_cloudinit_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            msi = temp_path / "CloudbaseInitSetup_Stable_x64.msi"
            msi.write_bytes(b"placeholder")
            definition = _build_definition(msi)
            out_dir = temp_path / "win10"
            render_packer_windows_proxmox_build(definition, "win10-ltsc", _build_env(), out_dir)

            packer_hcl = (out_dir / "windows-proxmox.pkr.hcl").read_text(encoding="utf-8")
            cloudbase_conf = (out_dir / "sysprep" / "cloudbase-init.conf").read_text(encoding="utf-8")
            sysprep_unattend = (out_dir / "sysprep" / "cloudbase-init-unattend.xml").read_text(encoding="utf-8")
            autounattend = (out_dir / "Autounattend.xml").read_text(encoding="utf-8")
            cloudbase_p2 = (out_dir / "cloudbase-init-p2.ps1").read_text(encoding="utf-8")
            winrm_bootstrap = (out_dir / "sysprep" / "rangectl-winrm-bootstrap.ps1")
            packer_winrm = (out_dir / "sysprep" / "rangectl-packer-winrm.ps1")
            variables = (out_dir / "variables.auto.pkrvars.hcl").read_text(encoding="utf-8")

            self.assertTrue(winrm_bootstrap.exists())
            self.assertTrue(packer_winrm.exists())
            self.assertIn("cloud_init              = true", packer_hcl)
            self.assertIn("cloud_init_storage_pool = var.proxmox_iso_storage", packer_hcl)
            self.assertIn('"<spacebar><wait>"', packer_hcl)
            self.assertIn("type     = var.virtio_iso_type", packer_hcl)
            self.assertIn("index    = var.virtio_iso_index", packer_hcl)
            self.assertIn('virtio_iso_type = "sata"', variables)
            self.assertIn('virtio_iso_index = "4"', variables)
            self.assertIn("cache_mode             = var.vm_disk_cache", packer_hcl)
            self.assertIn("config_drive_raw_hdd=true", cloudbase_conf)
            self.assertNotIn("config_drive_raw_hhd", cloudbase_conf)
            self.assertIn("metadata_services=cloudbaseinit.metadata.services.configdrive.ConfigDriveService", cloudbase_conf)
            self.assertIn("NetworkConfigPlugin,cloudbaseinit.plugins.common.localscripts.LocalScriptsPlugin", cloudbase_conf)
            self.assertIn("<SkipMachineOOBE>true</SkipMachineOOBE>", sysprep_unattend)
            self.assertIn("<LogonCount>1</LogonCount>", sysprep_unattend)
            self.assertIn("<AdministratorPassword>", sysprep_unattend)
            self.assertIn("sc config cloudbase-init start= auto", sysprep_unattend)
            self.assertIn("<Type>EFI</Type>", autounattend)
            self.assertIn("<LogonCount>1</LogonCount>", autounattend)
            self.assertIn("<AdministratorPassword>", autounattend)
            self.assertIn("D:\\vioscsi\\w10\\amd64", autounattend)
            self.assertIn("N:\\vioscsi\\w10\\amd64", autounattend)
            self.assertIn("rangectl-winrm-bootstrap.ps1", autounattend)
            self.assertIn('script            = "${path.root}/sysprep/rangectl-packer-winrm.ps1"', packer_hcl)
            self.assertNotIn("Microsoft-Windows-Deployment", autounattend)
            self.assertNotIn("rangectl-specialize-winrm", autounattend)
            self.assertIn("99-rangectl-enable-winrm.ps1", cloudbase_p2)
            self.assertIn("RangectlEnableWinRM", cloudbase_p2)
            self.assertIn("<StartBoundary>2020-01-01T00:00:00</StartBoundary>", cloudbase_p2)
            self.assertIn("<StartWhenAvailable>true</StartWhenAvailable>", cloudbase_p2)
            self.assertIn("rangectl-enable-winrm-task.xml", cloudbase_p2)
            self.assertNotIn("delayed-auto", cloudbase_p2)
            self.assertIn('vm_machine = "q35"', variables)
            self.assertIn('bios = "ovmf"', variables)
            self.assertIn('vm_disk_type = "sata"', variables)
            self.assertIn('boot_iso_type = "sata"', variables)
            self.assertIn('boot_iso_index = "1"', variables)
            self.assertIn("proxmox_skip_tls_verify = false", variables)

    def test_rendered_windows_11_template_requires_uefi_and_tpm(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            msi = temp_path / "CloudbaseInitSetup_Stable_x64.msi"
            msi.write_bytes(b"placeholder")
            definition = _build_definition(msi)
            out_dir = temp_path / "win11"
            render_packer_windows_proxmox_build(definition, "win11-ltsc", _build_env(), out_dir)

            packer_hcl = (out_dir / "windows-proxmox.pkr.hcl").read_text(encoding="utf-8")
            variables = (out_dir / "variables.auto.pkrvars.hcl").read_text(encoding="utf-8")
            autounattend = (out_dir / "Autounattend.xml").read_text(encoding="utf-8")

            self.assertIn('bios = "ovmf"', variables)
            self.assertIn('vm_machine = "pc-q35-11.0"', variables)
            self.assertIn("enable_efi = true", variables)
            self.assertIn("enable_tpm = true", variables)
            self.assertIn('tpm_version = "v2.0"', variables)
            self.assertIn('os = "win11"', variables)
            self.assertIn('vm_disk_type = "scsi"', variables)
            self.assertIn("vm_disk_io_thread = true", variables)
            self.assertIn('vm_disk_cache = "writeback"', variables)
            self.assertIn('boot_iso_type = "sata"', variables)
            self.assertIn('boot_iso_index = "1"', variables)
            self.assertIn('virtio_iso_type = "sata"', variables)
            self.assertIn('virtio_iso_index = "4"', variables)
            self.assertIn('dynamic "efi_config"', packer_hcl)
            self.assertIn('dynamic "tpm_config"', packer_hcl)
            self.assertIn("<DiskConfiguration>", autounattend)
            self.assertIn("<Type>EFI</Type>", autounattend)
            self.assertIn("rangectl-winrm-bootstrap.ps1", autounattend)
            self.assertIn("DriverPaths", autounattend)
            self.assertIn(r"D:\vioscsi\w11\amd64", autounattend)
            self.assertIn(r"N:\vioscsi\w11\amd64", autounattend)
            self.assertIn(r"D:\NetKVM\w11\amd64", autounattend)
            self.assertIn(r"D:\Balloon\w11\amd64", autounattend)
            self.assertNotIn(r"D:\viostor\w11\amd64", autounattend)
            self.assertNotIn(r"D:\qemupciserial\w11\amd64", autounattend)

    def test_rendered_windows_server_template_suppresses_server_oobe(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            msi = temp_path / "CloudbaseInitSetup_Stable_x64.msi"
            msi.write_bytes(b"placeholder")
            definition = _build_definition(msi)
            out_dir = temp_path / "win2019"
            render_packer_windows_proxmox_build(definition, "win2019", _build_env(), out_dir)

            autounattend = (out_dir / "Autounattend.xml").read_text(encoding="utf-8")
            sysprep_unattend = (out_dir / "sysprep" / "cloudbase-init-unattend.xml").read_text(encoding="utf-8")

            self.assertIn("<AdministratorPassword>", autounattend)
            self.assertIn("<AdministratorPassword>", sysprep_unattend)
            self.assertIn("Microsoft-Windows-OutOfBoxExperience", autounattend)
            self.assertIn("<DoNotOpenInitialConfigurationTasksAtLogon>true</DoNotOpenInitialConfigurationTasksAtLogon>", autounattend)
            self.assertIn("Microsoft-Windows-Security-SPP-UX", autounattend)
            self.assertIn("<SkipAutoActivation>true</SkipAutoActivation>", autounattend)
            self.assertIn("<SkipMachineOOBE>true</SkipMachineOOBE>", sysprep_unattend)
            self.assertIn("<SkipUserOOBE>true</SkipUserOOBE>", sysprep_unattend)

    def test_rendered_windows_2012_r2_template_uses_legacy_compatibility(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            msi = temp_path / "CloudbaseInitSetup_Stable_x64.msi"
            msi.write_bytes(b"placeholder")
            definition = _build_definition(msi)
            out_dir = temp_path / "win2012r2"
            render_packer_windows_proxmox_build(definition, "win2012r2", _build_env(), out_dir)

            variables = (out_dir / "variables.auto.pkrvars.hcl").read_text(encoding="utf-8")
            packer_hcl = (out_dir / "windows-proxmox.pkr.hcl").read_text(encoding="utf-8")
            winrm_bootstrap = (out_dir / "sysprep" / "rangectl-winrm-bootstrap.ps1").read_text(encoding="utf-8")
            packer_winrm = (out_dir / "sysprep" / "rangectl-packer-winrm.ps1").read_text(encoding="utf-8")
            qemu_guest_agent = (out_dir / "qemu-guest-agent.ps1").read_text(encoding="utf-8")
            cloudbase_combined = (out_dir / "cloudbase-init-combined.ps1").read_text(encoding="utf-8")

            self.assertIn('bios = "seabios"', variables)
            self.assertIn('vm_machine = "pc"', variables)
            self.assertIn('vm_disk_type = "sata"', variables)
            self.assertIn('$installRuntimeVirtioDrivers = "false"', winrm_bootstrap)
            self.assertIn("runtime VirtIO driver install disabled", winrm_bootstrap)
            self.assertIn("pnputil.exe /add-driver", winrm_bootstrap)
            self.assertIn("pnputil.exe -i -a", winrm_bootstrap)
            self.assertIn("starting Packer WinRM verification", packer_winrm)
            self.assertNotIn("$installRuntimeVirtioDrivers", packer_winrm)
            self.assertNotIn("netsh interface ip set address", packer_winrm)
            self.assertIn("QEMU guest agent installation disabled", qemu_guest_agent)
            self.assertNotIn('script            = "${path.root}/qemu-guest-agent.ps1"', packer_hcl)
            self.assertIn('script            = "${path.root}/cloudbase-init-combined.ps1"', packer_hcl)
            self.assertNotIn('script            = "${path.root}/cloudbase-init-p2.ps1"', packer_hcl)
            self.assertIn("Starting sysprep shutdown.", cloudbase_combined)

    def test_windows_11_rejects_bios_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            msi = temp_path / "CloudbaseInitSetup_Stable_x64.msi"
            msi.write_bytes(b"placeholder")
            definition = _build_definition(msi)
            definition["builds"]["win11-ltsc"]["hardware"]["bios"] = "seabios"

            with self.assertRaisesRegex(ValueError, "Windows 11 template profile is invalid"):
                render_packer_windows_proxmox_build(definition, "win11-ltsc", _build_env(), temp_path / "win11")

def _build_env() -> dict[str, str]:
    return {
        "PROXMOX_VE_ENDPOINT": "https://proxmox.invalid:8006",
        "PROXMOX_VE_USERNAME": "root@pam",
        "PROXMOX_VE_PASSWORD": "dummy",
        "PROXMOX_NODE": "pve2",
        "PROXMOX_POOL": "test",
        "PROXMOX_STORAGE": "local-lvm",
        "PROXMOX_NETWORK_BRIDGE": "vmbr0",
        "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD": "vagrant",
    }


if __name__ == "__main__":
    unittest.main()


def _build_definition(cloudbase_msi: Path) -> dict[str, object]:
    return {
        "version": 1,
        "provider": "proxmox",
        "builder": "packer",
        "defaults": {
            "node_env": "PROXMOX_NODE",
            "pool_env": "PROXMOX_POOL",
            "vm_storage_env": "PROXMOX_STORAGE",
            "iso_storage": "pve-truenas",
            "network_bridge_env": "PROXMOX_NETWORK_BRIDGE",
        },
        "assets": {
            "virtio_iso": {"source": "pve-truenas:iso/virtio-win.iso"},
            "cloudbase_init_msi": {"source": str(cloudbase_msi)},
        },
        "builds": {
            "win2012r2": {
                "output": {"vmid": 902, "name": "template-windows-server2012r2-cloudinit"},
                "os": {
                    "image_name": "Windows Server 2012 R2 SERVERDATACENTER",
                    "virtio_driver_os": "2k12R2",
                    "proxmox_os": "win10",
                    "iso": {"source": "pve-truenas:iso/windows_server_2012r2_x64.iso"},
                },
                "hardware": {
                    "bios": "seabios",
                    "machine": "pc",
                    "disk_type": "sata",
                    "build_vlan": "60",
                    "install_qemu_guest_agent": False,
                },
                "unattended": {
                    "administrator_username": "vagrant",
                    "administrator_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
                "provisioning": {
                    "winrm_username": "vagrant",
                    "winrm_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
            },
            "win2019": {
                "output": {"vmid": 900, "name": "template-windows-server2019-cloudinit"},
                "os": {
                    "image_name": "Windows Server 2019 SERVERDATACENTER",
                    "virtio_driver_os": "2k19",
                    "proxmox_os": "win10",
                    "iso": {"source": "pve-truenas:iso/windows_server2019_x64FREE_en-us.iso"},
                },
                "hardware": {"build_vlan": "60"},
                "unattended": {
                    "administrator_username": "vagrant",
                    "administrator_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
                "provisioning": {
                    "winrm_username": "vagrant",
                    "winrm_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
            },
            "win10-ltsc": {
                "output": {"vmid": 904, "name": "template-windows10-ltsc-cloudinit"},
                "os": {
                    "image_name": "Windows 10 Enterprise LTSC 2021",
                    "virtio_driver_os": "w10",
                    "proxmox_os": "win10",
                    "iso": {"source": "pve-truenas:iso/windows_10_ltsc_2021_eval_x64.iso"},
                },
                "hardware": {"build_vlan": "60"},
                "unattended": {
                    "administrator_username": "vagrant",
                    "administrator_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
                "provisioning": {
                    "winrm_username": "vagrant",
                    "winrm_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
            },
            "win11-ltsc": {
                "output": {"vmid": 906, "name": "template-windows11-ltsc-cloudinit"},
                "os": {
                    "image_name": "Windows 11 Enterprise LTSC 2024 Evaluation",
                    "virtio_driver_os": "w11",
                    "proxmox_os": "win11",
                    "iso": {"source": "pve-truenas:iso/windows_11_ltsc_eval_x64.iso"},
                },
                "hardware": {"build_vlan": "60"},
                "unattended": {
                    "administrator_username": "vagrant",
                    "administrator_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
                "provisioning": {
                    "winrm_username": "vagrant",
                    "winrm_password_env": "RANGECTL_TEMPLATE_WINDOWS_ADMIN_PASSWORD",
                },
            },
        },
    }
