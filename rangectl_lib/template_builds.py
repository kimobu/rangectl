from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from email.message import Message
from typing import Any

import yaml

from rangectl_lib.security import safe_child_path, secure_write_text


USER_AGENT = "rangectl-template-inspector/0.1"
LOCALE_RE = re.compile(r"(?<![a-z0-9])([a-z]{2})[-_]([a-z]{2})(?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class UrlInspection:
    source_url: str
    resolved_url: str
    status: int | None
    headers: dict[str, str]
    hints: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "resolved_url": self.resolved_url,
            "status": self.status,
            "headers": self.headers,
            "hints": self.hints,
        }


@dataclass(frozen=True)
class WindowsOsProfile:
    name: str
    machine: str
    bios: str
    disk_type: str
    disk_io_thread: bool
    enable_efi: bool
    enable_tpm: bool
    efi_pre_enrolled_keys: bool
    tpm_version: str


def inspect_template_source_url(url: str, timeout: int = 30) -> UrlInspection:
    response = _open_without_body(url, timeout=timeout)
    headers = {key.lower(): value for key, value in response.headers.items()}
    resolved_url = response.url
    filename = _filename_from_headers(headers) or _filename_from_url(resolved_url)
    hints = infer_template_source_hints(url, resolved_url, filename, headers)
    return UrlInspection(
        source_url=url,
        resolved_url=resolved_url,
        status=response.status,
        headers=_interesting_headers(headers),
        hints=hints,
    )


def load_template_build_definition(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return data


def render_packer_windows_proxmox_build(
    definition: dict[str, Any],
    build_name: str,
    env: dict[str, str],
    out_dir: Path,
) -> dict[str, Any]:
    build = (definition.get("builds") or {}).get(build_name)
    if not isinstance(build, dict):
        raise ValueError(f"template build {build_name!r} not found in definition")
    if definition.get("provider") != "proxmox":
        raise ValueError("only provider: proxmox is supported by this renderer")
    if definition.get("builder") != "packer":
        raise ValueError("only builder: packer is supported by this renderer")

    out_dir.mkdir(parents=True, exist_ok=True)
    sysprep_dir = out_dir / "sysprep"
    sysprep_dir.mkdir(exist_ok=True)

    defaults = definition.get("defaults") or {}
    assets = definition.get("assets") or {}
    build_assets = build.get("assets") or {}
    output = build.get("output") or {}
    os_data = build.get("os") or {}
    hardware = build.get("hardware") or {}
    provisioning = build.get("provisioning") or {}
    unattended = build.get("unattended") or {}

    vmid = int(output["vmid"])
    vm_name = str(output["name"])
    template_description = str(
        output.get("description")
        or f"Windows cloud-init template built by rangectl at {datetime.now(timezone.utc).isoformat()}"
    )
    iso = os_data.get("iso") or {}
    iso_source = _required(iso, "source", "os.iso.source")
    iso_file = _proxmox_iso_file(iso_source)
    virtio_asset = build_assets.get("virtio_iso") or assets.get("virtio_iso") or {}
    cloudbase_asset = build_assets.get("cloudbase_init_msi") or assets.get("cloudbase_init_msi") or {}
    updates_asset = build_assets.get("windows_updates") or assets.get("windows_updates") or {}
    virtio_iso = _proxmox_iso_file(_required(virtio_asset, "source", "assets.virtio_iso.source"))
    cloudbase_source = _required(cloudbase_asset, "source", "assets.cloudbase_init_msi.source")

    proxmox_endpoint = _required_env(env, "PROXMOX_VE_ENDPOINT")
    proxmox_url = proxmox_endpoint.rstrip("/") + "/api2/json"
    proxmox_username = _required_env(env, "PROXMOX_VE_USERNAME")
    proxmox_password = _required_env(env, "PROXMOX_VE_PASSWORD")
    proxmox_node = _env_or_default(env, defaults.get("node_env"), "pve1")
    proxmox_pool = _env_or_default(env, defaults.get("pool_env"), "rangectl")
    proxmox_vm_storage = _env_or_default(env, defaults.get("vm_storage_env"), "local-zfs-nvme")
    proxmox_bridge = _env_or_default(env, defaults.get("network_bridge_env"), "vmbr0")
    proxmox_iso_storage = str(defaults.get("iso_storage") or "local")
    proxmox_vlan_tag = str(hardware.get("build_vlan") or defaults.get("build_vlan") or "")
    autounattend_iso_name = str(hardware.get("autounattend_iso_name") or f"rangectl-{build_name}-autounattend.iso")
    autounattend_iso = str(hardware.get("autounattend_iso") or f"{proxmox_iso_storage}:iso/{autounattend_iso_name}")

    winrm_username = str(provisioning.get("winrm_username") or unattended.get("administrator_username") or "vagrant")
    winrm_password_env = provisioning.get("winrm_password_env") or unattended.get("administrator_password_env")
    winrm_password = _env_or_default(env, winrm_password_env, str(provisioning.get("winrm_password") or "vagrant"))
    locale = str(unattended.get("locale") or os_data.get("locale") or "en-US")
    keyboard = str(unattended.get("keyboard") or locale)
    install_timezone = str(unattended.get("timezone") or "UTC")
    image_name = str(os_data.get("image_name") or "Windows Server 2016 SERVERDATACENTER")
    virtio_driver_os = str(os_data.get("virtio_driver_os") or "2k16")
    build_ip = str(hardware.get("build_ip") or "")
    build_netmask = str(hardware.get("build_netmask") or "255.255.255.0")
    build_gateway = str(hardware.get("build_gateway") or "")
    build_dns = str(hardware.get("build_dns") or build_gateway)
    profile = _windows_os_profile(build_name, os_data, hardware, image_name)
    disk_type = profile.disk_type
    is_windows_11 = _is_windows_11_os(os_data, image_name)
    boot_iso_type = str(hardware.get("boot_iso_type") or "sata")
    boot_iso_index = str(hardware.get("boot_iso_index") or ("2" if boot_iso_type == "ide" else "1"))
    virtio_iso_type = str(hardware.get("virtio_iso_type") or "sata")
    virtio_iso_index = str(hardware.get("virtio_iso_index") or ("0" if virtio_iso_type == "ide" else "4"))
    install_boot_device = str(hardware.get("install_boot_device") or f"{boot_iso_type}{boot_iso_index}")
    install_boot_order = str(hardware.get("install_boot_order") or f"{install_boot_device};{disk_type}0;net0")
    final_boot_order = str(hardware.get("final_boot_order") or f"{disk_type}0;net0")
    final_boot_order_delay_seconds = int(hardware.get("final_boot_order_delay_seconds") or (240 if is_windows_11 else 0))
    disk_cache = hardware.get("disk_cache")
    if disk_cache is None and is_windows_11:
        disk_cache = "writeback"
    install_qemu_guest_agent = bool(hardware.get("install_qemu_guest_agent", True))
    install_runtime_virtio_drivers = bool(
        hardware.get("install_runtime_virtio_drivers", virtio_driver_os.lower() != "2k12r2")
    )
    is_windows_server = "server" in image_name.lower()

    cache_dir = Path(env.get("RANGECTL_TEMPLATE_CACHE_DIR", str(Path("state") / "templates" / "cache"))).expanduser()
    cloudbase_msi = fetch_asset(
        cloudbase_source,
        cache_dir,
        sha256=str(cloudbase_asset.get("sha256") or "") or None,
        max_bytes=_asset_max_bytes(cloudbase_asset),
    )
    shutil.copy2(cloudbase_msi, sysprep_dir / "CloudbaseInitSetup_Stable_x64.msi")
    updates_dir = out_dir / "updates"
    _stage_windows_updates(updates_asset.get("source"), updates_dir)
    (sysprep_dir / "cloudbase-init.conf").write_text(CLOUDBASE_INIT_CONF, encoding="utf-8")
    (sysprep_dir / "cloudbase-init-unattend.conf").write_text(CLOUDBASE_INIT_UNATTEND_CONF, encoding="utf-8")
    (sysprep_dir / "cloudbase-init-unattend.xml").write_text(
        render_cloudbase_init_unattend_xml(
            username=winrm_username,
            password=winrm_password,
            locale=locale,
            keyboard=keyboard,
            timezone=install_timezone,
        ),
        encoding="utf-8",
    )
    firstlogon_script = (
        RANGECTL_FIRSTLOGON_PS1
        .replace("__RANGECTL_BUILD_IP__", build_ip)
        .replace("__RANGECTL_BUILD_NETMASK__", build_netmask)
        .replace("__RANGECTL_BUILD_GATEWAY__", build_gateway)
        .replace("__RANGECTL_BUILD_DNS__", build_dns)
    )
    (sysprep_dir / "rangectl-firstlogon.ps1").write_text(firstlogon_script, encoding="utf-8")
    bootstrap_script = RANGECTL_FIRSTLOGON_BOOTSTRAP_PS1
    replacements = {
        "__VIRTIO_DRIVER_OS__": virtio_driver_os,
        "__BUILD_IP__": build_ip,
        "__BUILD_NETMASK__": build_netmask,
        "__BUILD_GATEWAY__": build_gateway,
        "__BUILD_DNS__": build_dns,
        "__INSTALL_RUNTIME_VIRTIO_DRIVERS__": "true" if install_runtime_virtio_drivers else "false",
    }
    for key, value in replacements.items():
        bootstrap_script = bootstrap_script.replace(key, value)
    winrm_bootstrap_script = (
        bootstrap_script
        .replace("rangectl-firstlogon.log", "rangectl-winrm-bootstrap.log")
        .replace("starting first-logon bootstrap", "starting WinRM bootstrap")
        .replace("first-logon bootstrap complete", "WinRM bootstrap complete")
    )
    (sysprep_dir / "rangectl-winrm-bootstrap.ps1").write_text(winrm_bootstrap_script, encoding="utf-8")
    (sysprep_dir / "rangectl-packer-winrm.ps1").write_text(PACKER_WINRM_BOOTSTRAP_PS1, encoding="utf-8")
    (out_dir / "Autounattend.xml").write_text(
        render_windows_autounattend(
            username=winrm_username,
            password=winrm_password,
            locale=locale,
            keyboard=keyboard,
            timezone=install_timezone,
            image_name=image_name,
            virtio_driver_os=virtio_driver_os,
            firmware=profile.bios,
            install_qemu_guest_agent=install_qemu_guest_agent,
            is_windows_server=is_windows_server,
            winrm_bootstrap_script=winrm_bootstrap_script,
        ),
        encoding="utf-8",
    )

    variables = {
        "proxmox_url": proxmox_url,
        "proxmox_username": proxmox_username,
        "proxmox_password": proxmox_password,
        "proxmox_skip_tls_verify": env.get("PROXMOX_VE_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"},
        "proxmox_node": proxmox_node,
        "proxmox_pool": proxmox_pool,
        "proxmox_vm_storage": proxmox_vm_storage,
        "proxmox_iso_storage": proxmox_iso_storage,
        "proxmox_bridge": proxmox_bridge,
        "proxmox_vlan_tag": proxmox_vlan_tag,
        "winrm_username": winrm_username,
        "winrm_password": winrm_password,
        "vm_id": vmid,
        "vm_name": vm_name,
        "template_description": template_description,
        "iso_file": iso_file,
        "autounattend_iso": autounattend_iso,
        "virtio_iso": virtio_iso,
        "boot_iso_type": boot_iso_type,
        "boot_iso_index": boot_iso_index,
        "virtio_iso_type": virtio_iso_type,
        "virtio_iso_index": virtio_iso_index,
        "vm_cpu_cores": str(hardware.get("cpu_cores", 2)),
        "vm_cpu_type": str(hardware.get("cpu_type", "host")),
        "vm_machine": profile.machine,
        "vm_memory": str(hardware.get("memory_mb", 4096)),
        "vm_disk_size": _disk_size(hardware.get("disk_gb", 40)),
        "vm_disk_type": disk_type,
        "vm_disk_io_thread": profile.disk_io_thread,
        "vm_disk_cache": str(disk_cache or "none"),
        "vm_network_model": str(hardware.get("network_model", "virtio")),
        "vm_sockets": str(hardware.get("sockets", 1)),
        "vm_disk_format": str(hardware.get("disk_format", "raw")),
        "bios": profile.bios,
        "os": str(os_data.get("proxmox_os", "win10")),
        "enable_efi": profile.enable_efi,
        "efi_storage_pool": str(hardware.get("efi_storage_pool") or proxmox_vm_storage),
        "efi_pre_enrolled_keys": profile.efi_pre_enrolled_keys,
        "enable_tpm": profile.enable_tpm,
        "tpm_version": str(hardware.get("tpm_version") or profile.tpm_version),
        "tpm_storage_pool": str(hardware.get("tpm_storage_pool") or proxmox_vm_storage),
    }

    if build_ip:
        variables["winrm_host"] = build_ip
        winrm_host_variable = 'variable "winrm_host" {}\n'
        winrm_host_line = "  winrm_host               = var.winrm_host\n"
    else:
        winrm_host_variable = ""
        winrm_host_line = ""

    packer_hcl = (
        PACKER_WINDOWS_PROXMOX_HCL
        .replace("__WINRM_HOST_VARIABLE__", winrm_host_variable)
        .replace("__WINRM_HOST_LINE__", winrm_host_line)
        .replace("__QEMU_GUEST_AGENT_PROVISIONER__", QEMU_GUEST_AGENT_PROVISIONER if install_qemu_guest_agent else "")
        .replace(
            "__CLOUDBASE_PROVISIONERS__",
            CLOUDBASE_PROVISIONERS if install_qemu_guest_agent else CLOUDBASE_COMBINED_PROVISIONER,
        )
    )
    (out_dir / "windows-proxmox.pkr.hcl").write_text(packer_hcl, encoding="utf-8")
    (out_dir / "variables.auto.pkrvars.hcl").write_text(_hcl_vars(variables), encoding="utf-8")
    qemu_guest_agent_script = QEMU_GUEST_AGENT_PS1
    if not install_qemu_guest_agent:
        qemu_guest_agent_script = 'Write-Host "QEMU guest agent installation disabled for this build"; exit 0\n'
    (out_dir / "qemu-guest-agent.ps1").write_text(qemu_guest_agent_script, encoding="utf-8")
    (out_dir / "cloudbase-init.ps1").write_text(CLOUDBASE_INIT_PS1, encoding="utf-8")
    (out_dir / "cloudbase-init-p2.ps1").write_text(CLOUDBASE_INIT_P2_PS1, encoding="utf-8")
    (out_dir / "cloudbase-init-combined.ps1").write_text(
        _combine_cloudbase_init_scripts(CLOUDBASE_INIT_PS1, CLOUDBASE_INIT_P2_PS1),
        encoding="utf-8",
    )
    (out_dir / "build-metadata.yml").write_text(
        yaml.safe_dump(
            {
                "build": build_name,
                "vmid": vmid,
                "vm_name": vm_name,
                "iso_file": iso_file,
                "autounattend_iso": autounattend_iso,
                "autounattend_iso_name": autounattend_iso_name,
                "proxmox_iso_storage": proxmox_iso_storage,
                "install_boot_device": install_boot_device,
                "install_boot_order": install_boot_order,
                "final_boot_order_delay_seconds": final_boot_order_delay_seconds,
                "final_boot_order": final_boot_order,
                "final_disk_cache": str(disk_cache or ""),
                "workdir": str(out_dir),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    return {
        "build": build_name,
        "vmid": vmid,
        "vm_name": vm_name,
        "autounattend_iso_name": autounattend_iso_name,
        "proxmox_iso_storage": proxmox_iso_storage,
        "install_boot_order": install_boot_order,
        "force_install_boot_order": is_windows_11,
        "final_boot_order_delay_seconds": final_boot_order_delay_seconds,
        "final_boot_order": final_boot_order,
        "final_disk_cache": str(disk_cache or ""),
        "final_disk_key": f"{disk_type}0",
        "workdir": out_dir,
        "packer_file": out_dir / "windows-proxmox.pkr.hcl",
    }


def run_packer_build(workdir: Path, packer_bin: str = "packer") -> int:
    init = subprocess.run([packer_bin, "init", "."], cwd=workdir)
    if init.returncode != 0:
        return init.returncode
    build = subprocess.run([packer_bin, "build", "-force", "."], cwd=workdir)
    return build.returncode


def run_packer_validate(workdir: Path, packer_bin: str = "packer") -> int:
    init = subprocess.run([packer_bin, "init", "."], cwd=workdir)
    if init.returncode != 0:
        return init.returncode
    validate = subprocess.run([packer_bin, "validate", "."], cwd=workdir)
    return validate.returncode


def _combine_cloudbase_init_scripts(install_script: str, finalize_script: str) -> str:
    install_script = install_script.rstrip()
    if install_script.endswith("exit 0"):
        install_script = install_script[: -len("exit 0")].rstrip()
    return install_script + "\n\n" + finalize_script.lstrip()


def template_build_tool_status(env: dict[str, str]) -> dict[str, Any]:
    return {
        "packer": shutil.which(env.get("RANGECTL_PACKER_BIN", "packer")),
        "xorriso": shutil.which(env.get("RANGECTL_XORRISO_BIN", "xorriso")),
        "genisoimage": shutil.which("genisoimage"),
        "mkisofs": shutil.which("mkisofs"),
    }


DEFAULT_ASSET_MAX_BYTES = 16 * 1024**3


def _asset_max_bytes(asset: dict[str, Any]) -> int:
    if asset.get("max_size_bytes") is not None:
        value = int(asset["max_size_bytes"])
    elif asset.get("max_size_gib") is not None:
        value = int(float(asset["max_size_gib"]) * 1024**3)
    else:
        value = DEFAULT_ASSET_MAX_BYTES
    if value < 1:
        raise ValueError("asset max size must be positive")
    return value


def fetch_asset(
    source: str,
    cache_dir: Path,
    *,
    sha256: str | None = None,
    max_bytes: int = DEFAULT_ASSET_MAX_BYTES,
) -> Path:
    if source.startswith("file://"):
        return Path(urllib.parse.urlparse(source).path)
    local = Path(source).expanduser()
    if local.exists():
        return local
    if not source.startswith(("http://", "https://")):
        raise ValueError(f"unsupported asset source {source!r}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_asset_filename(_filename_from_url(source) or "asset.bin")
    target = safe_child_path(cache_dir, filename)
    if _valid_cached_asset(target, source, sha256):
        return target
    request = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
    fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".part", dir=cache_dir)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    written = 0
    resolved_url = source
    try:
        with os.fdopen(fd, "wb") as handle, urllib.request.urlopen(request, timeout=120) as response:
            resolved_url = response.url
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise ValueError(f"asset exceeds configured limit of {max_bytes} bytes")
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"asset exceeds configured limit of {max_bytes} bytes")
                digest.update(chunk)
                handle.write(chunk)
        actual_sha256 = digest.hexdigest()
        if sha256 and not hmac.compare_digest(actual_sha256, sha256.lower()):
            raise ValueError("asset SHA-256 mismatch")
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        secure_write_text(
            _asset_metadata_path(target),
            json.dumps({"source": source, "resolved_url": resolved_url, "size": written, "sha256": actual_sha256}, sort_keys=True) + "\n",
        )
        return target
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _safe_asset_filename(filename: str) -> str:
    decoded = urllib.parse.unquote(filename)
    if Path(decoded).name != decoded or decoded in {"", ".", ".."}:
        raise ValueError(f"unsafe asset filename: {filename!r}")
    return decoded


def _asset_metadata_path(target: Path) -> Path:
    return target.with_name(f"{target.name}.rangectl.json")


def _valid_cached_asset(target: Path, source: str, expected_sha256: str | None) -> bool:
    metadata_path = _asset_metadata_path(target)
    if not target.is_file() or not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("source") != source or target.stat().st_size != int(metadata["size"]):
            return False
        digest = _sha256_file(target)
        recorded = str(metadata.get("sha256") or "")
        required = expected_sha256.lower() if expected_sha256 else recorded
        return bool(required) and hmac.compare_digest(digest, required) and hmac.compare_digest(recorded, digest)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _stage_windows_updates(source: Any, updates_dir: Path) -> None:
    if updates_dir.exists():
        shutil.rmtree(updates_dir)
    updates_dir.mkdir(parents=True, exist_ok=True)
    if not source:
        return
    source_path = Path(str(source)).expanduser()
    if not source_path.exists():
        raise ValueError(f"windows_updates source does not exist: {source_path}")
    if source_path.is_dir():
        for child in sorted(source_path.iterdir()):
            if child.name.startswith("."):
                continue
            if child.is_file():
                shutil.copy2(child, updates_dir / child.name)
        return
    shutil.copy2(source_path, updates_dir / source_path.name)


def infer_template_source_hints(
    source_url: str,
    resolved_url: str | None = None,
    filename: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    headers = headers or {}
    resolved_url = resolved_url or source_url
    parsed = urllib.parse.urlparse(source_url)
    query = urllib.parse.parse_qs(parsed.query)
    culture = _first_query_value(query, "culture")
    country = _first_query_value(query, "country")
    locale = _normalize_locale(culture) if culture else None
    filename = filename or _filename_from_headers(headers) or _filename_from_url(resolved_url)

    if not locale:
        locale = _locale_from_text(" ".join(part for part in [filename, resolved_url] if part))

    hints: dict[str, Any] = {
        "filename": filename,
        "culture": culture,
        "country": country,
        "locale": locale,
    }
    if locale:
        hints["keyboard"] = locale

    size = headers.get("content-length")
    if size and size.isdigit():
        hints["size_bytes"] = int(size)

    windows = _windows_hints(" ".join(part for part in [filename, resolved_url, source_url] if part))
    if windows:
        hints["os"] = windows

    return {key: value for key, value in hints.items() if value not in (None, "", {})}


def _required(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not value:
        raise ValueError(f"{label} is required")
    return str(value)


def _required_env(env: dict[str, str], key: str) -> str:
    value = env.get(key)
    if not value:
        raise ValueError(f"{key} is required in environment or .rangectl.env")
    return value


def _env_or_default(env: dict[str, str], env_key: Any, default: str) -> str:
    if env_key and env.get(str(env_key)):
        return env[str(env_key)]
    return default


def _proxmox_iso_file(source: str) -> str:
    if re.match(r"^[A-Za-z0-9_.-]+:iso/.+", source):
        return source
    if source.startswith("proxmox:"):
        return source[len("proxmox:") :]
    raise ValueError(f"{source!r} is not a Proxmox ISO volume such as pve-truenas:iso/file.iso")


def _disk_size(value: Any) -> str:
    if isinstance(value, int):
        return f"{value}G"
    text = str(value)
    if text.isdigit():
        return f"{text}G"
    return text


def _hcl_vars(values: dict[str, Any]) -> str:
    lines = []
    for key, value in values.items():
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _open_without_body(url: str, timeout: int) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code not in {403, 405, 501}:
            raise
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
    return urllib.request.urlopen(request, timeout=timeout)


def _interesting_headers(headers: dict[str, str]) -> dict[str, str]:
    keep = {
        "content-disposition",
        "content-length",
        "content-type",
        "etag",
        "last-modified",
    }
    return {key: value for key, value in headers.items() if key in keep}


def _filename_from_headers(headers: dict[str, str]) -> str | None:
    value = headers.get("content-disposition")
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    if filename:
        return filename
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1))
    return None


def _filename_from_url(url: str) -> str | None:
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name or None


def _first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0] or None


def _normalize_locale(value: str) -> str:
    match = LOCALE_RE.search(value)
    if not match:
        return value
    return f"{match.group(1).lower()}-{match.group(2).upper()}"


def _locale_from_text(text: str) -> str | None:
    match = LOCALE_RE.search(text)
    if not match:
        return None
    return f"{match.group(1).lower()}-{match.group(2).upper()}"


def _windows_hints(text: str) -> dict[str, Any]:
    lower = text.lower()
    if not any(marker in lower for marker in ("windows", "win", "server_eval", "client_ltsc")):
        return {}

    hints: dict[str, Any] = {"family": "windows"}
    if "server" in lower:
        hints["product"] = "windows-server"
        for year in ("2025", "2022", "2019", "2016", "2012"):
            if year in lower:
                suffix = "-r2" if year == "2012" and ("r2" in lower or "winblue" in lower) else ""
                hints["version"] = f"server-{year}{suffix}"
                break
        if "17763" in lower and "version" not in hints:
            hints["version"] = "server-2019"
    elif "windows_11" in lower or "windows-11" in lower or "win11" in lower:
        hints["product"] = "windows-client"
        hints["version"] = "windows-11"
    elif "windows_10" in lower or "windows-10" in lower or "win10" in lower:
        hints["product"] = "windows-client"
        hints["version"] = "windows-10"
    elif "client_ltsc" in lower:
        hints["product"] = "windows-client"
        if "26100" in lower:
            hints["version"] = "windows-11"
        elif "19044" in lower:
            hints["version"] = "windows-10"

    if "ltsc" in lower:
        hints["edition_hint"] = "Enterprise LTSC"
    elif "datacenter" in lower:
        hints["edition_hint"] = "Datacenter"
    elif "standard" in lower:
        hints["edition_hint"] = "Standard"
    elif re.search(r"(?<![a-z])pro(?![a-z])", lower):
        hints["edition_hint"] = "Pro"
    elif "enterprise" in lower:
        hints["edition_hint"] = "Enterprise"

    if "eval" in lower:
        hints["license_hint"] = "evaluation"

    if any(marker in lower for marker in ("x64", "amd64", "x86_64")):
        hints["arch"] = "x86_64"
    elif re.search(r"(?<!x)86(?!_64)", lower):
        hints["arch"] = "x86"
    elif "arm64" in lower or "aarch64" in lower:
        hints["arch"] = "arm64"

    return hints


def _windows_os_profile(build_name: str, os_data: dict[str, Any], hardware: dict[str, Any], image_name: str) -> WindowsOsProfile:
    os_text = " ".join(
        str(item)
        for item in [
            build_name,
            os_data.get("proxmox_os", ""),
            os_data.get("virtio_driver_os", ""),
            image_name,
        ]
    ).lower()

    if "2012" in os_text:
        defaults = WindowsOsProfile(
            name="windows-server-2012-r2",
            machine="pc",
            bios="seabios",
            disk_type="sata",
            disk_io_thread=False,
            enable_efi=False,
            enable_tpm=False,
            efi_pre_enrolled_keys=False,
            tpm_version="v2.0",
        )
    elif "win11" in os_text or "windows 11" in os_text:
        defaults = WindowsOsProfile(
            name="windows-11",
            machine="pc-q35-11.0",
            bios="ovmf",
            disk_type="scsi",
            disk_io_thread=True,
            enable_efi=True,
            enable_tpm=True,
            efi_pre_enrolled_keys=True,
            tpm_version="v2.0",
        )
    else:
        defaults = WindowsOsProfile(
            name="windows-goad-compatible",
            machine="q35",
            bios="ovmf",
            disk_type="sata",
            disk_io_thread=False,
            enable_efi=True,
            enable_tpm=False,
            efi_pre_enrolled_keys=True,
            tpm_version="v2.0",
        )

    profile = WindowsOsProfile(
        name=defaults.name,
        machine=str(hardware.get("machine") or defaults.machine),
        bios=str(hardware.get("bios") or defaults.bios),
        disk_type=str(hardware.get("disk_type") or defaults.disk_type),
        disk_io_thread=bool(hardware.get("disk_io_thread", defaults.disk_io_thread)),
        enable_efi=bool(hardware.get("enable_efi", defaults.enable_efi)),
        enable_tpm=bool(hardware.get("enable_tpm", defaults.enable_tpm)),
        efi_pre_enrolled_keys=bool(hardware.get("efi_pre_enrolled_keys", defaults.efi_pre_enrolled_keys)),
        tpm_version=str(hardware.get("tpm_version") or defaults.tpm_version),
    )
    _validate_windows_os_profile(profile)
    return profile


def _is_windows_11_os(os_data: dict[str, Any], image_name: str) -> bool:
    os_text = " ".join(
        str(item)
        for item in [
            os_data.get("proxmox_os", ""),
            os_data.get("virtio_driver_os", ""),
            image_name,
        ]
    ).lower()
    return "win11" in os_text or "windows 11" in os_text


def _validate_windows_os_profile(profile: WindowsOsProfile) -> None:
    if profile.name == "windows-11":
        problems = []
        if profile.bios != "ovmf":
            problems.append("bios must be ovmf")
        if profile.machine != "q35" and not profile.machine.startswith("pc-q35-"):
            problems.append("machine must be q35 or pc-q35-*")
        if not profile.enable_efi:
            problems.append("EFI must be enabled")
        if not profile.enable_tpm:
            problems.append("TPM must be enabled")
        if profile.tpm_version != "v2.0":
            problems.append("TPM version must be v2.0")
        if problems:
            raise ValueError("Windows 11 template profile is invalid: " + "; ".join(problems))
    if profile.bios == "ovmf" and not profile.enable_efi:
        raise ValueError(f"{profile.name} uses OVMF but EFI config is disabled")


def render_windows_autounattend(
    *,
    username: str,
    password: str,
    locale: str,
    keyboard: str,
    timezone: str,
    image_name: str,
    virtio_driver_os: str,
    firmware: str,
    install_qemu_guest_agent: bool,
    is_windows_server: bool,
    winrm_bootstrap_script: str = "",
) -> str:
    partitioning = _windows_partitioning(firmware)
    return WINDOWS_AUTOUNATTEND.format(
        username=_xml_escape(username),
        password=_xml_escape(password),
        locale=_xml_escape(locale),
        keyboard=_xml_escape(keyboard),
        timezone=_xml_escape(timezone),
        image_name=_xml_escape(image_name),
        virtio_driver_os=_xml_escape(virtio_driver_os),
        windows_pe_driver_paths=_windows_pe_driver_paths(virtio_driver_os),
        windows_partitioning=partitioning["xml"],
        install_partition_id=partitioning["install_partition_id"],
        server_specialize_components=_server_specialize_components() if is_windows_server else "",
    )


def render_cloudbase_init_unattend_xml(
    *,
    username: str,
    password: str,
    locale: str,
    keyboard: str,
    timezone: str,
) -> str:
    return CLOUDBASE_INIT_UNATTEND_XML.format(
        username=_xml_escape(username),
        password=_xml_escape(password),
        locale=_xml_escape(locale),
        keyboard=_xml_escape(keyboard),
        timezone=_xml_escape(timezone),
    )


def _windows_pe_driver_paths(virtio_driver_os: str) -> str:
    if virtio_driver_os.lower() == "w11":
        driver_roots = ["vioscsi", "NetKVM", "Balloon"]
    else:
        driver_roots = [
            "viostor",
            "NetKVM",
            "Balloon",
            "pvpanic",
            "qemupciserial",
            "qxldod",
            "vioinput",
            "viorng",
            "vioscsi",
            "vioserial",
        ]
    drives = "DEFGHIJKLMN"
    lines = []
    index = 1
    for drive in drives:
        for root in driver_roots:
            path = _xml_escape(f"{drive}:\\{root}\\{virtio_driver_os}\\amd64")
            lines.append(
                f'        <PathAndCredentials wcm:action="add" wcm:keyValue="{index}"><Path>{path}</Path></PathAndCredentials>'
            )
            index += 1
    return "\n".join(lines)


def _windows_partitioning(firmware: str) -> dict[str, Any]:
    if firmware == "ovmf":
        return {
            "install_partition_id": "3",
            "xml": r'''          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>100</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>128</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System</Label><Format>FAT32</Format></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Letter>C</Letter><Label>Windows</Label><Format>NTFS</Format></ModifyPartition>
          </ModifyPartitions>''',
        }
    return {
        "install_partition_id": "2",
        "xml": r'''          <CreatePartitions>
            <CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Size>350</Size></CreatePartition>
            <CreatePartition wcm:action="add"><Order>2</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>
          </CreatePartitions>
          <ModifyPartitions>
            <ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Label>System Reserved</Label><Format>NTFS</Format><Active>true</Active></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID><Letter>C</Letter><Label>Windows</Label><Format>NTFS</Format></ModifyPartition>
          </ModifyPartitions>''',
    }


def _xml_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _server_specialize_components() -> str:
    return r'''    <component name="Microsoft-Windows-ServerManager-SvrMgrNc" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DoNotOpenServerManagerAtLogon>true</DoNotOpenServerManagerAtLogon>
    </component>
    <component name="Microsoft-Windows-IE-ESC" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <IEHardenAdmin>false</IEHardenAdmin>
      <IEHardenUser>false</IEHardenUser>
    </component>
    <component name="Microsoft-Windows-OutOfBoxExperience" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DoNotOpenInitialConfigurationTasksAtLogon>true</DoNotOpenInitialConfigurationTasksAtLogon>
    </component>
    <component name="Microsoft-Windows-Security-SPP-UX" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SkipAutoActivation>true</SkipAutoActivation>
    </component>
'''


def _build_network_command(
    build_ip: str,
    netmask: str,
    gateway: str,
    dns: str,
    *,
    include_exit: bool = True,
) -> str:
    prefix_length = _netmask_prefix_length(netmask)
    commands = [
        "$rangectlAdapter = (Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | Select-Object -First 1 -ExpandProperty Name)",
        "if (-not $rangectlAdapter) {$rangectlAdapter = (Get-NetAdapter | Select-Object -First 1 -ExpandProperty Name)}",
        "Disable-NetAdapterBinding -Name $rangectlAdapter -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue",
        "netsh interface teredo set state disabled",
        "netsh interface isatap set state disabled",
    ]
    if build_ip and gateway:
        commands.append(
            "Get-NetIPAddress -InterfaceAlias $rangectlAdapter -AddressFamily IPv4 -ErrorAction SilentlyContinue "
            "| Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue"
        )
        commands.append(
            f"New-NetIPAddress -InterfaceAlias $rangectlAdapter -IPAddress {build_ip} "
            f"-PrefixLength {prefix_length} -DefaultGateway {gateway} -ErrorAction SilentlyContinue"
        )
    if dns:
        commands.append(
            f"Set-DnsClientServerAddress -InterfaceAlias $rangectlAdapter -ServerAddresses {dns} "
            "-ErrorAction SilentlyContinue"
        )
    if include_exit:
        commands.append("exit 0")
    return "; ".join(commands)


def _netmask_prefix_length(netmask: str) -> int:
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except ValueError:
        return 24


def _mounted_script_command(script_name: str, *, best_effort: bool = False) -> str:
    drives = "D E F G H I J K L M N O P Q R S T U V W X Y Z"
    script_name = script_name.replace("/", "\\")
    missing_log = "C:\\setup\\rangectl-specialize-winrm-notfound.log"
    script_exit = "0" if best_effort else "!errorlevel!"
    missing_exit = "0" if best_effort else "2"
    return (
        "cmd.exe /d /v:on /c "
        "mkdir C:\\setup 2>NUL "
        f"& for %d in ({drives}) do @if exist %d:\\sysprep\\{script_name} "
        f"(powershell.exe -NoProfile -ExecutionPolicy Bypass -File %d:\\sysprep\\{script_name} & exit /b {script_exit}) "
        f"& for %d in ({drives}) do @if exist %d:\\{script_name} "
        f"(powershell.exe -NoProfile -ExecutionPolicy Bypass -File %d:\\{script_name} & exit /b {script_exit}) "
        f"& echo {script_name} not found on mounted media > {missing_log} "
        f"& exit /b {missing_exit}"
    )


PACKER_WINDOWS_PROXMOX_HCL = r'''
packer {
  required_plugins {
    proxmox = {
      version = ">= 1.2.3"
      source  = "github.com/hashicorp/proxmox"
    }
  }
}

variable "proxmox_url" {}
variable "proxmox_username" {}
variable "proxmox_password" {
  sensitive = true
}
variable "proxmox_skip_tls_verify" {
  type = bool
}
variable "proxmox_node" {}
variable "proxmox_pool" {}
variable "proxmox_vm_storage" {}
variable "proxmox_iso_storage" {}
variable "proxmox_bridge" {}
variable "proxmox_vlan_tag" {}
variable "winrm_username" {}
variable "winrm_password" {
  sensitive = true
}
__WINRM_HOST_VARIABLE__
variable "vm_id" {
  type = number
}
variable "vm_name" {}
variable "template_description" {}
variable "iso_file" {}
variable "autounattend_iso" {}
variable "virtio_iso" {}
variable "boot_iso_type" {}
variable "boot_iso_index" {}
variable "virtio_iso_type" {}
variable "virtio_iso_index" {}
variable "vm_cpu_cores" {}
variable "vm_cpu_type" {}
variable "vm_machine" {}
variable "vm_memory" {}
variable "vm_disk_size" {}
variable "vm_disk_type" {}
variable "vm_disk_io_thread" {
  type = bool
}
variable "vm_disk_cache" {}
variable "vm_network_model" {}
variable "vm_sockets" {}
variable "vm_disk_format" {}
variable "bios" {}
variable "os" {}
variable "enable_efi" {
  type = bool
}
variable "efi_storage_pool" {}
variable "efi_pre_enrolled_keys" {
  type = bool
}
variable "enable_tpm" {
  type = bool
}
variable "tpm_version" {}
variable "tpm_storage_pool" {}

source "proxmox-iso" "windows" {
  boot_iso {
    type     = var.boot_iso_type
    index    = var.boot_iso_index
    iso_file = var.iso_file
    unmount  = true
  }

  additional_iso_files {
    type     = "sata"
    index    = "3"
    iso_file = var.autounattend_iso
    unmount  = true
  }

  additional_iso_files {
    type     = var.virtio_iso_type
    index    = var.virtio_iso_index
    iso_file = var.virtio_iso
    unmount  = true
  }

  additional_iso_files {
    type             = "sata"
    index            = "5"
    cd_files         = [
      "sysprep/CloudbaseInitSetup_Stable_x64.msi",
      "sysprep/cloudbase-init.conf",
      "sysprep/cloudbase-init-unattend.conf",
      "sysprep/cloudbase-init-unattend.xml",
      "sysprep/rangectl-firstlogon.ps1",
      "sysprep/rangectl-winrm-bootstrap.ps1",
      "sysprep/rangectl-packer-winrm.ps1",
      "updates",
    ]
    cd_label         = "RANGECTL"
    iso_storage_pool = var.proxmox_iso_storage
    unmount          = true
  }

  cloud_init              = true
  cloud_init_storage_pool = var.proxmox_iso_storage
  communicator             = "winrm"
  winrm_username           = var.winrm_username
  winrm_password           = var.winrm_password
__WINRM_HOST_LINE__
  winrm_timeout            = "120m"
  winrm_use_ssl            = false
  winrm_insecure           = true
  winrm_no_proxy           = true

  cores                    = var.vm_cpu_cores
  cpu_type                 = var.vm_cpu_type
  scsi_controller          = "virtio-scsi-single"
  disks {
    disk_size              = var.vm_disk_size
    format                 = var.vm_disk_format
    storage_pool           = var.proxmox_vm_storage
    type                   = var.vm_disk_type
    cache_mode             = var.vm_disk_cache
    discard                = true
    io_thread              = var.vm_disk_io_thread
  }

  dynamic "efi_config" {
    for_each = var.enable_efi ? [1] : []
    content {
      efi_storage_pool  = var.efi_storage_pool
      pre_enrolled_keys = var.efi_pre_enrolled_keys
    }
  }

  dynamic "tpm_config" {
    for_each = var.enable_tpm ? [1] : []
    content {
      tpm_version      = var.tpm_version
      tpm_storage_pool = var.tpm_storage_pool
    }
  }

  insecure_skip_tls_verify = var.proxmox_skip_tls_verify
  machine                  = var.vm_machine
  memory                   = var.vm_memory
  network_adapters {
    bridge                 = var.proxmox_bridge
    model                  = var.vm_network_model
    vlan_tag               = var.proxmox_vlan_tag
  }
  node                     = var.proxmox_node
  os                       = var.os
  password                 = var.proxmox_password
  pool                     = var.proxmox_pool
  proxmox_url              = var.proxmox_url
  sockets                  = var.vm_sockets
  template_description     = var.template_description
  template_name            = var.vm_name
  username                 = var.proxmox_username
  vm_id                    = var.vm_id
  vm_name                  = var.vm_name
  bios                     = var.bios
  boot_wait                = "1s"
  boot_command             = [
    "<spacebar><wait>",
    "<spacebar><wait>",
    "<spacebar><wait>",
    "<enter><wait>",
    "<enter>",
  ]
  task_timeout             = "60m"
}

build {
  sources = ["source.proxmox-iso.windows"]

  provisioner "powershell" {
    pause_before      = "10s"
    script            = "${path.root}/sysprep/rangectl-packer-winrm.ps1"
    valid_exit_codes  = [0]
  }

__QEMU_GUEST_AGENT_PROVISIONER__

__CLOUDBASE_PROVISIONERS__
}
'''


QEMU_GUEST_AGENT_PROVISIONER = r'''  provisioner "powershell" {
    pause_before      = "10s"
    script            = "${path.root}/qemu-guest-agent.ps1"
    valid_exit_codes  = [0, 3010]
  }
'''


CLOUDBASE_PROVISIONERS = r'''  provisioner "powershell" {
    pause_before      = "30s"
    script            = "${path.root}/cloudbase-init.ps1"
    valid_exit_codes  = [0, 259]
  }

  provisioner "powershell" {
    pause_before      = "30s"
    pause_after       = "90s"
    script            = "${path.root}/cloudbase-init-p2.ps1"
    valid_exit_codes  = [0, 259]
  }
'''


CLOUDBASE_COMBINED_PROVISIONER = r'''  provisioner "powershell" {
    pause_before      = "60s"
    pause_after       = "90s"
    script            = "${path.root}/cloudbase-init-combined.ps1"
    valid_exit_codes  = [0, 259]
  }
'''


RANGECTL_FIRSTLOGON_BOOTSTRAP_PS1 = r'''
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Path "C:\setup" -Force | Out-Null
$logPath = "C:\setup\rangectl-firstlogon.log"

function Write-RangectlLog {
  param([string]$Message)
  $line = "$(Get-Date -Format s) $Message"
  Write-Host $line
  Add-Content -Path $logPath -Value $line
}

Write-RangectlLog "starting first-logon bootstrap"
$driverOs = "__VIRTIO_DRIVER_OS__"
$installRuntimeVirtioDrivers = "__INSTALL_RUNTIME_VIRTIO_DRIVERS__"
if ($installRuntimeVirtioDrivers -eq "true") {
  foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
    foreach ($root in "NetKVM","vioserial","Balloon") {
      $driverDir = "${drive}:\${root}\${driverOs}\amd64"
      if (-not (Test-Path $driverDir)) {
        continue
      }
      Write-RangectlLog "installing VirtIO driver from $driverDir"
      Get-ChildItem -Path $driverDir -Filter "*.inf" -ErrorAction SilentlyContinue | ForEach-Object {
        $output = pnputil.exe /add-driver $_.FullName /install 2>&1
        if ($LASTEXITCODE -ne 0) {
          $output = pnputil.exe -i -a $_.FullName 2>&1
        }
        Write-RangectlLog ($output -join "`n")
      }
    }
  }
} else {
  Write-RangectlLog "runtime VirtIO driver install disabled for $driverOs"
}

$buildIp = "__BUILD_IP__"
$netmask = "__BUILD_NETMASK__"
$gateway = "__BUILD_GATEWAY__"
$dns = "__BUILD_DNS__"
if ($buildIp -and $gateway) {
  try {
    $adapter = (Get-NetAdapter -ErrorAction Stop | Where-Object {$_.Status -eq "Up"} | Sort-Object InterfaceIndex | Select-Object -First 1 -ExpandProperty Name)
    if (-not $adapter) {
      $adapter = (Get-NetAdapter -ErrorAction Stop | Sort-Object InterfaceIndex | Select-Object -First 1 -ExpandProperty Name)
    }
    if ($adapter) {
      $currentIp = Get-NetIPAddress -InterfaceAlias $adapter -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $buildIp } |
        Select-Object -First 1
      if ($currentIp) {
        Write-RangectlLog "build network $buildIp already configured on adapter $adapter"
      } else {
        Write-RangectlLog "setting build network $buildIp on adapter $adapter"
        netsh interface ip set address name="$adapter" static $buildIp $netmask $gateway | Out-Null
        $addressExit = $LASTEXITCODE
        if ($addressExit -ne 0) {
          Write-RangectlLog "netsh set address exited $addressExit"
        }
        if ($dns) {
          netsh interface ip set dns name="$adapter" static $dns | Out-Null
          $dnsExit = $LASTEXITCODE
          if ($dnsExit -ne 0) {
            Write-RangectlLog "netsh set dns exited $dnsExit"
          }
        }
      }
    } else {
      Write-RangectlLog "no network adapter found for build IP"
    }
  } catch {
    Write-RangectlLog "build network configuration warning: $($_.Exception.Message)"
  }
  $global:LASTEXITCODE = 0
}

Write-RangectlLog "configuring WinRM"
powercfg.exe /hibernate off | Out-Null
powercfg.exe /change standby-timeout-ac 0 | Out-Null
powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
powercfg.exe /change monitor-timeout-ac 0 | Out-Null
$wlms = Get-Service -Name WLMS -ErrorAction SilentlyContinue
if ($wlms) {
  Stop-Service -Name WLMS -Force -ErrorAction SilentlyContinue
  sc.exe config WLMS start= disabled | Out-Null
  Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WLMS" -Name Start -Value 4 -ErrorAction SilentlyContinue
}
Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f | Out-Null
$global:LASTEXITCODE = 0
Set-Service -Name WinRM -StartupType Automatic
Start-Service -Name WinRM
try {
  Set-WSManQuickConfig -Force
} catch {
  Write-RangectlLog "Set-WSManQuickConfig warning: $($_.Exception.Message)"
  winrm quickconfig -quiet 2>$null | Out-Null
  $global:LASTEXITCODE = 0
}
Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $true
Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $true
try {
  Enable-PSRemoting -Force
} catch {
  Write-RangectlLog "Enable-PSRemoting warning: $($_.Exception.Message)"
}
Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -Enabled True -Profile Any -ErrorAction SilentlyContinue
if (-not (Get-NetFirewallRule -Name Rangectl-WinRM-HTTP -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name Rangectl-WinRM-HTTP -DisplayName "rangectl WinRM HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any | Out-Null
}
$global:LASTEXITCODE = 0
Write-RangectlLog "first-logon bootstrap complete"
exit 0
'''


PACKER_WINRM_BOOTSTRAP_PS1 = r'''
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Path "C:\setup" -Force | Out-Null
$logPath = "C:\setup\rangectl-packer-winrm.log"

function Write-RangectlLog {
  param([string]$Message)
  $line = "$(Get-Date -Format s) $Message"
  Write-Host $line
  Add-Content -Path $logPath -Value $line
}

Write-RangectlLog "starting Packer WinRM verification"
powercfg.exe /hibernate off | Out-Null
powercfg.exe /change standby-timeout-ac 0 | Out-Null
powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
powercfg.exe /change monitor-timeout-ac 0 | Out-Null
$wlms = Get-Service -Name WLMS -ErrorAction SilentlyContinue
if ($wlms) {
  Stop-Service -Name WLMS -Force -ErrorAction SilentlyContinue
  sc.exe config WLMS start= disabled | Out-Null
  Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WLMS" -Name Start -Value 4 -ErrorAction SilentlyContinue
}
Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f | Out-Null
$global:LASTEXITCODE = 0
Set-Service -Name WinRM -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name WinRM -ErrorAction SilentlyContinue
winrm quickconfig -quiet 2>$null | Out-Null
$global:LASTEXITCODE = 0
winrm set winrm/config/service/auth '@{Basic="true"}' | Out-Null
$global:LASTEXITCODE = 0
winrm set winrm/config/service '@{AllowUnencrypted="true"}' | Out-Null
$global:LASTEXITCODE = 0
Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -Enabled True -Profile Any -ErrorAction SilentlyContinue
if (-not (Get-NetFirewallRule -Name Rangectl-WinRM-HTTP -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name Rangectl-WinRM-HTTP -DisplayName "rangectl WinRM HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any | Out-Null
}
$global:LASTEXITCODE = 0
Write-RangectlLog "Packer WinRM verification complete"
exit 0
'''


QEMU_GUEST_AGENT_PS1 = r'''
$ErrorActionPreference = "Stop"
$msi = $null
foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
  foreach ($path in @("${drive}:\guest-agent\qemu-ga-x86_64.msi", "${drive}:\qemu-ga-x86_64.msi")) {
    if (Test-Path $path) {
      $msi = $path
      break
    }
  }
  if ($msi) { break }
}

if (-not $msi) {
  Write-Host "QEMU guest agent MSI not found; skipping"
  exit 0
}

Write-Host "Installing QEMU guest agent from $msi"
$process = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
if ($process.ExitCode -notin @(0, 3010)) {
  Write-Host "ERROR: QEMU guest agent installer exited $($process.ExitCode)"
  exit $process.ExitCode
}

Set-Service -Name QEMU-GA -StartupType Automatic -ErrorAction SilentlyContinue
Start-Service -Name QEMU-GA -ErrorAction SilentlyContinue
exit $process.ExitCode
'''


WINDOWS_11_AUTOUNATTEND = r'''<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <DriverPaths>
{windows_pe_driver_paths}
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>{locale}</UILanguage></SetupUILanguage>
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add">
              <Key>/IMAGE/NAME</Key>
              <Value>{image_name}</Value>
            </MetaData>
          </InstallFrom>
          <InstallTo>
            <DiskID>0</DiskID>
            <PartitionID>3</PartitionID>
          </InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <ProductKey><WillShowUI>OnError</WillShowUI></ProductKey>
        <AcceptEula>true</AcceptEula>
        <FullName>{username}</FullName>
        <Organization>rangectl</Organization>
      </UserData>
      <UseConfigurationSet>false</UseConfigurationSet>
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>cmd.exe /c echo SELECT DISK=0 &gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>cmd.exe /c echo CLEAN &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Path>cmd.exe /c echo CONVERT GPT &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>4</Order>
          <Path>cmd.exe /c echo CREATE PARTITION EFI SIZE=300 &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>5</Order>
          <Path>cmd.exe /c echo FORMAT QUICK FS=FAT32 LABEL=System &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>6</Order>
          <Path>cmd.exe /c echo CREATE PARTITION MSR SIZE=16 &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>7</Order>
          <Path>cmd.exe /c echo CREATE PARTITION PRIMARY &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>8</Order>
          <Path>cmd.exe /c echo FORMAT QUICK FS=NTFS LABEL=Windows &gt;&gt; X:\diskpart.txt</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>9</Order>
          <Path>cmd.exe /c diskpart.exe /s X:\diskpart.txt &gt; X:\diskpart.log 2&gt;&amp;1</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$xml = [xml]::new(); $xml.Load('C:\Windows\Panther\unattend.xml'); $sb = [scriptblock]::Create($xml.unattend.Extensions.ExtractScript); Invoke-Command -ScriptBlock $sb -ArgumentList $xml;"</Path>
        </RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Path>powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath 'C:\Windows\Setup\Scripts\Specialize.ps1' -Raw | Invoke-Expression;"</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <TimeZone>{timezone}</TimeZone>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <AutoLogon>
        <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
        <Username>{username}</Username>
      </AutoLogon>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{username}</Name>
            <Group>Administrators</Group>
            <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <OOBE>
        <ProtectYourPC>3</ProtectYourPC>
        <HideEULAPage>true</HideEULAPage>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
      </OOBE>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Enable rangectl build network and WinRM</Description>
          <CommandLine>powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Content -LiteralPath 'C:\Windows\Setup\Scripts\RangectlWinRM.ps1' -Raw | Invoke-Expression;"</CommandLine>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
  <Extensions xmlns="https://rangectl.local/windows/unattend">
    <ExtractScript>
param([xml] $Document)
foreach ($file in $Document.unattend.Extensions.File) {{
  $path = [System.Environment]::ExpandEnvironmentVariables($file.GetAttribute('path'))
  New-Item -ItemType Directory -Path ($path | Split-Path -Parent) -Force | Out-Null
  $bytes = [System.Text.Encoding]::UTF8.GetPreamble() + [System.Text.Encoding]::UTF8.GetBytes($file.InnerText.Trim())
  [System.IO.File]::WriteAllBytes($path, $bytes)
}}
    </ExtractScript>
    <File path="C:\Windows\Setup\Scripts\Specialize.ps1">
reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f | Out-Null
reg.exe add "HKLM\SYSTEM\CurrentControlSet\Control\BitLocker" /v PreventDeviceEncryption /t REG_DWORD /d 1 /f | Out-Null
reg.exe add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard" /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard" /v RequirePlatformSecurityFeatures /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity" /v Enabled /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SYSTEM\CurrentControlSet\Control\Lsa" /v LsaCfgFlags /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard" /v EnableVirtualizationBasedSecurity /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard" /v LsaCfgFlags /t REG_DWORD /d 0 /f | Out-Null
reg.exe add "HKLM\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard" /v RequirePlatformSecurityFeatures /t REG_DWORD /d 0 /f | Out-Null
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Force
    </File>
    <File path="C:\Windows\Setup\Scripts\RangectlWinRM.ps1">
{winrm_bootstrap_script}
    </File>
  </Extensions>
</unattend>
'''


WINDOWS_AUTOUNATTEND = r'''<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-PnpCustomizationsWinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <DriverPaths>
{windows_pe_driver_paths}
      </DriverPaths>
    </component>
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SetupUILanguage><UILanguage>{locale}</UILanguage></SetupUILanguage>
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Allow template install without TPM checks</Description>
          <Path>cmd.exe /c reg add HKLM\SYSTEM\Setup\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f &amp;&amp; reg add HKLM\SYSTEM\Setup\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f &amp;&amp; reg add HKLM\SYSTEM\Setup\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>
      </RunSynchronous>
      <DiskConfiguration>
        <Disk wcm:action="add">
          <DiskID>0</DiskID>
          <WillWipeDisk>true</WillWipeDisk>
{windows_partitioning}
        </Disk>
        <WillShowUI>OnError</WillShowUI>
      </DiskConfiguration>
      <ImageInstall>
        <OSImage>
          <InstallFrom>
            <MetaData wcm:action="add"><Key>/IMAGE/NAME</Key><Value>{image_name}</Value></MetaData>
          </InstallFrom>
          <InstallTo><DiskID>0</DiskID><PartitionID>{install_partition_id}</PartitionID></InstallTo>
        </OSImage>
      </ImageInstall>
      <UserData>
        <AcceptEula>true</AcceptEula>
        <FullName>rangectl</FullName>
        <Organization>rangectl</Organization>
        <ProductKey><WillShowUI>OnError</WillShowUI></ProductKey>
      </UserData>
    </component>
  </settings>
  <settings pass="offlineServicing">
    <component name="Microsoft-Windows-PnpCustomizationsNonWinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <DriverPaths>
{windows_pe_driver_paths}
      </DriverPaths>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <ComputerName>template</ComputerName>
      <TimeZone>{timezone}</TimeZone>
    </component>
{server_specialize_components}
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <AutoLogon>
        <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
        <Username>{username}</Username>
      </AutoLogon>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{username}</Name>
            <Group>Administrators</Group>
            <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
      <FirstLogonCommands>
        <SynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Description>Set PowerShell execution policy</Description>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Force"</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>2</Order>
          <Description>Suppress network discovery prompt</Description>
          <CommandLine>cmd.exe /c reg add "HKLM\System\CurrentControlSet\Control\Network\NewNetworkWindowOff" /f</CommandLine>
        </SynchronousCommand>
        <SynchronousCommand wcm:action="add">
          <Order>3</Order>
          <Description>Enable rangectl build network and WinRM</Description>
          <CommandLine>powershell -NoProfile -ExecutionPolicy Bypass -Command "$v=Get-Volume -FileSystemLabel 'RANGECTL' -ErrorAction Stop; &amp; (Join-Path ($v.DriveLetter + ':\') 'rangectl-winrm-bootstrap.ps1')"</CommandLine>
        </SynchronousCommand>
      </FirstLogonCommands>
    </component>
  </settings>
</unattend>
'''


CLOUDBASE_INIT_PS1 = r'''
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path "C:\setup" -Force | Out-Null

function Install-RangectlWindowsUpdates {
  $updatesSource = $null
  foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
    $candidate = "${drive}:\updates"
    if ((Test-Path $candidate) -and ((Get-ChildItem -Path $candidate -Filter "*.msu" -ErrorAction SilentlyContinue | Select-Object -First 1) -ne $null)) {
      $updatesSource = $candidate
      break
    }
  }
  if (-not $updatesSource) {
    Write-Host "No rangectl Windows update media found; skipping offline updates"
    return
  }

  $updatesDest = "C:\setup\updates"
  New-Item -ItemType Directory -Path $updatesDest -Force | Out-Null
  Copy-Item -Path (Join-Path $updatesSource "*") -Destination $updatesDest -Recurse -Force
  Write-Host "Installing offline Windows updates from $updatesDest"

  $preferredOrder = @(
    "Windows8.1-KB2919442-x64.msu",
    "Windows8.1-KB2919355-x64.msu",
    "Windows8.1-KB2932046-x64.msu",
    "Windows8.1-KB2959977-x64.msu",
    "Windows8.1-KB2937592-x64.msu",
    "Windows8.1-KB2938439-x64.msu",
    "Windows8.1-KB2934018-x64.msu",
    "Windows8.1-KB2999226-x64.msu",
    "Windows8.1-KB2975061-x64.msu",
    "Win8.1AndW2K12R2-KB3191564-x64.msu"
  )

  $clearCompression = Join-Path $updatesDest "clearcompressionflag.exe"
  if (Test-Path $clearCompression) {
    Write-Host "Running clearcompressionflag.exe"
    $clear = Start-Process -FilePath $clearCompression -Wait -PassThru
    Write-Host "clearcompressionflag.exe exited $($clear.ExitCode)"
  }

  $installed = @{}
  foreach ($name in $preferredOrder) {
    $path = Join-Path $updatesDest $name
    if (-not (Test-Path $path)) {
      continue
    }
    Write-Host "Installing $name"
    $proc = Start-Process -FilePath "wusa.exe" -ArgumentList "`"$path`" /quiet /norestart" -Wait -PassThru
    if ($proc.ExitCode -notin @(0, 3010, 2359302)) {
      Write-Host "ERROR: $name failed with exit code $($proc.ExitCode)"
      exit $proc.ExitCode
    }
    $installed[$name] = $true
  }

  foreach ($msu in Get-ChildItem -Path $updatesDest -Filter "*.msu" | Sort-Object Name) {
    if ($installed.ContainsKey($msu.Name)) {
      continue
    }
    Write-Host "Installing $($msu.Name)"
    $proc = Start-Process -FilePath "wusa.exe" -ArgumentList "`"$($msu.FullName)`" /quiet /norestart" -Wait -PassThru
    if ($proc.ExitCode -notin @(0, 3010, 2359302)) {
      Write-Host "ERROR: $($msu.Name) failed with exit code $($proc.ExitCode)"
      exit $proc.ExitCode
    }
  }
}

$msiSource = $null
foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
  foreach ($path in @("${drive}:\sysprep\CloudbaseInitSetup_Stable_x64.msi", "${drive}:\CloudbaseInitSetup_Stable_x64.msi")) {
    if (Test-Path $path) {
      $msiSource = $path
      break
    }
  }
  if ($msiSource) { break }
}
if (-not $msiSource) {
  Write-Host "ERROR: Cloudbase-Init MSI not found on generated rangectl ISO"
  exit 1
}

$msiDest = "C:\setup\CloudbaseInitSetup_Stable_x64.msi"
Copy-Item -Path $msiSource -Destination $msiDest -Force
$process = Start-Process -FilePath "msiexec.exe" -ArgumentList "/i `"$msiDest`" /qn /norestart /l*v C:\setup\cloudbase-init-install.log" -Wait -PassThru
if ($process.ExitCode -notin @(0, 3010)) {
  Write-Host "ERROR: Cloudbase-Init installer exited $($process.ExitCode)"
  exit $process.ExitCode
}
exit 0
'''


RANGECTL_FIRSTLOGON_PS1 = r'''
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path "C:\setup" -Force | Out-Null
$logPath = "C:\setup\rangectl-firstlogon.log"
$localScript = "C:\setup\rangectl-firstlogon.ps1"
if ($PSCommandPath -and ($PSCommandPath -ne $localScript)) {
  Copy-Item -Path $PSCommandPath -Destination $localScript -Force
}

function Write-RangectlLog {
  param([string]$Message)
  $line = "$(Get-Date -Format s) $Message"
  Write-Host $line
  Add-Content -Path $logPath -Value $line
}

function Set-RangectlDword {
  param(
    [string]$Path,
    [string]$Name,
    [int]$Value
  )
  if (-not (Test-Path $Path)) {
    New-Item -Path $Path -Force | Out-Null
  }
  New-ItemProperty -Path $Path -Name $Name -Value $Value -PropertyType DWord -Force | Out-Null
}

function Disable-RangectlWindowsUpdates {
  Write-RangectlLog "Disabling automatic Windows updates"
  $windowsUpdatePolicy = 'HKLM:\Software\Policies\Microsoft\Windows\WindowsUpdate'
  $automaticUpdatePolicy = Join-Path $windowsUpdatePolicy 'AU'
  Set-RangectlDword -Path $automaticUpdatePolicy -Name NoAutoUpdate -Value 1
  Set-RangectlDword -Path $automaticUpdatePolicy -Name AUOptions -Value 1
  Set-RangectlDword -Path $automaticUpdatePolicy -Name ScheduledInstallDay -Value 0
  Set-RangectlDword -Path $automaticUpdatePolicy -Name ScheduledInstallTime -Value 3
  Set-RangectlDword -Path $windowsUpdatePolicy -Name DisableOSUpgrade -Value 1
  Set-RangectlDword -Path $windowsUpdatePolicy -Name DisableWindowsUpdateAccess -Value 1
  Set-RangectlDword -Path $windowsUpdatePolicy -Name DoNotConnectToWindowsUpdateInternetLocations -Value 1
  Set-RangectlDword -Path 'HKLM:\Software\Policies\Microsoft\Windows\DeliveryOptimization' -Name DODownloadMode -Value 0
  Set-RangectlDword -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update' -Name AUOptions -Value 1

  foreach ($serviceName in @('wuauserv','UsoSvc','WaaSMedicSvc','BITS','DoSvc','InstallService')) {
    $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    if ($service -and $service.Status -ne 'Stopped') {
      Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
    }
    & sc.exe config $serviceName start= disabled | Out-Null
    $serviceKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$serviceName"
    if (Test-Path $serviceKey) {
      Set-ItemProperty -Path $serviceKey -Name Start -Value 4 -ErrorAction SilentlyContinue
    }
  }

  foreach ($taskRoot in @(
    '\Microsoft\Windows\WindowsUpdate\',
    '\Microsoft\Windows\UpdateOrchestrator\',
    '\Microsoft\Windows\WaaSMedic\',
    '\Microsoft\Windows\InstallService\',
    '\Microsoft\Windows\USO_UxBroker\'
  )) {
    Get-ScheduledTask -TaskPath $taskRoot -ErrorAction SilentlyContinue |
      Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null
  }
}

function Disable-RangectlBitLocker {
  Write-RangectlLog "Disabling automatic BitLocker device encryption"
  Set-RangectlDword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\BitLocker' -Name PreventDeviceEncryption -Value 1
  if (Get-Command manage-bde.exe -ErrorAction SilentlyContinue) {
    $status = manage-bde.exe -status C: 2>$null | Out-String
    if ($LASTEXITCODE -eq 0 -and $status -match 'Conversion Status:\s+(?!Fully Decrypted)') {
      Write-RangectlLog "Turning BitLocker off on C:"
      manage-bde.exe -off C: | Out-Null
    }
  }
}

function Disable-RangectlVirtualizationSecurity {
  Write-RangectlLog "Disabling automatic VBS and Credential Guard"
  Set-RangectlDword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard' -Name EnableVirtualizationBasedSecurity -Value 0
  Set-RangectlDword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard' -Name RequirePlatformSecurityFeatures -Value 0
  Set-RangectlDword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' -Name Enabled -Value 0
  Set-RangectlDword -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Lsa' -Name LsaCfgFlags -Value 0
  Set-RangectlDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name EnableVirtualizationBasedSecurity -Value 0
  Set-RangectlDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name LsaCfgFlags -Value 0
  Set-RangectlDword -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceGuard' -Name RequirePlatformSecurityFeatures -Value 0
  try {
    bcdedit.exe /set '{current}' hypervisorlaunchtype off | Out-Null
    bcdedit.exe /set '{current}' vsmlaunchtype off | Out-Null
    bcdedit.exe /set '{current}' loadoptions DISABLE-LSA-ISO | Out-Null
  } catch {
    Write-RangectlLog "VBS bcdedit warning: $($_.Exception.Message)"
  }
}

function Install-RangectlWindowsUpdates {
  $marker = "C:\setup\updates-complete.marker"
  if (Test-Path $marker) {
    Write-RangectlLog "Offline Windows updates already completed; skipping"
    return $false
  }

  $updatesSource = $null
  foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
    $candidate = "${drive}:\updates"
    if ((Test-Path $candidate) -and ((Get-ChildItem -Path $candidate -Filter "*.msu" -ErrorAction SilentlyContinue | Select-Object -First 1) -ne $null)) {
      $updatesSource = $candidate
      break
    }
  }
  if (-not $updatesSource) {
    Write-RangectlLog "No rangectl Windows update media found; skipping offline updates"
    New-Item -ItemType File -Path $marker -Force | Out-Null
    return $false
  }

  $updatesDest = "C:\setup\updates"
  New-Item -ItemType Directory -Path $updatesDest -Force | Out-Null
  Copy-Item -Path (Join-Path $updatesSource "*") -Destination $updatesDest -Recurse -Force
  Write-RangectlLog "Installing offline Windows updates from $updatesDest"

  $preferredOrder = @(
    "Windows8.1-KB2919442-x64.msu",
    "Windows8.1-KB2919355-x64.msu",
    "Windows8.1-KB2932046-x64.msu",
    "Windows8.1-KB2959977-x64.msu",
    "Windows8.1-KB2937592-x64.msu",
    "Windows8.1-KB2938439-x64.msu",
    "Windows8.1-KB2934018-x64.msu",
    "Windows8.1-KB2999226-x64.msu",
    "Windows8.1-KB2975061-x64.msu",
    "Win8.1AndW2K12R2-KB3191564-x64.msu"
  )

  $clearCompression = Join-Path $updatesDest "clearcompressionflag.exe"
  if (Test-Path $clearCompression) {
    Write-RangectlLog "Running clearcompressionflag.exe"
    $clear = Start-Process -FilePath $clearCompression -Wait -PassThru
    Write-RangectlLog "clearcompressionflag.exe exited $($clear.ExitCode)"
  }

  $installed = @{}
  $restartRequired = $false
  foreach ($name in $preferredOrder) {
    $path = Join-Path $updatesDest $name
    if (-not (Test-Path $path)) {
      continue
    }
    Write-RangectlLog "Installing $name"
    $proc = Start-Process -FilePath "wusa.exe" -ArgumentList "`"$path`" /quiet /norestart" -Wait -PassThru
    if ($proc.ExitCode -notin @(0, 3010, 2359302)) {
      Write-RangectlLog "ERROR: $name failed with exit code $($proc.ExitCode)"
      exit $proc.ExitCode
    }
    if ($proc.ExitCode -eq 3010) {
      $restartRequired = $true
    }
    Write-RangectlLog "$name completed with exit code $($proc.ExitCode)"
    $installed[$name] = $true
  }

  foreach ($msu in Get-ChildItem -Path $updatesDest -Filter "*.msu" | Sort-Object Name) {
    if ($installed.ContainsKey($msu.Name)) {
      continue
    }
    Write-RangectlLog "Installing $($msu.Name)"
    $proc = Start-Process -FilePath "wusa.exe" -ArgumentList "`"$($msu.FullName)`" /quiet /norestart" -Wait -PassThru
    if ($proc.ExitCode -notin @(0, 3010, 2359302)) {
      Write-RangectlLog "ERROR: $($msu.Name) failed with exit code $($proc.ExitCode)"
      exit $proc.ExitCode
    }
    if ($proc.ExitCode -eq 3010) {
      $restartRequired = $true
    }
    Write-RangectlLog "$($msu.Name) completed with exit code $($proc.ExitCode)"
  }

  New-Item -ItemType File -Path $marker -Force | Out-Null
  return $restartRequired
}

function Set-RangectlBuildNetwork {
  $buildIp = "__RANGECTL_BUILD_IP__"
  $netmask = "__RANGECTL_BUILD_NETMASK__"
  $gateway = "__RANGECTL_BUILD_GATEWAY__"
  $dns = "__RANGECTL_BUILD_DNS__"
  if (-not $buildIp -or -not $gateway) {
    return
  }
  $adapter = (Get-NetAdapter | Where-Object {$_.Status -eq "Up"} | Select-Object -First 1 -ExpandProperty Name)
  if (-not $adapter) {
    $adapter = (Get-NetAdapter | Select-Object -First 1 -ExpandProperty Name)
  }
  if (-not $adapter) {
    Write-RangectlLog "No network adapter found for static build IP"
    return
  }
  Write-RangectlLog "Setting static build IP $buildIp on adapter $adapter"
  Disable-NetAdapterBinding -Name $adapter -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue
  & netsh.exe interface ip set address name="$adapter" static $buildIp $netmask $gateway | Out-Null
  if ($dns) {
    & netsh.exe interface ip set dns name="$adapter" static $dns | Out-Null
  }
}

function Enable-RangectlWinRM {
  Write-RangectlLog "Configuring WinRM for Packer"
  powercfg.exe /hibernate off | Out-Null
  powercfg.exe /change standby-timeout-ac 0 | Out-Null
  powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
  powercfg.exe /change monitor-timeout-ac 0 | Out-Null
  $wlms = Get-Service -Name WLMS -ErrorAction SilentlyContinue
  if ($wlms) {
    Stop-Service -Name WLMS -Force -ErrorAction SilentlyContinue
    & sc.exe config WLMS start= disabled | Out-Null
    Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WLMS" -Name Start -Value 4 -ErrorAction SilentlyContinue
  }
  Set-RangectlBuildNetwork
  Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
  & reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f | Out-Null
  Set-Service -Name WinRM -StartupType Automatic
  Start-Service -Name WinRM
  Set-WSManQuickConfig -Force
  Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value true
  Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value true
  Enable-PSRemoting -Force
  Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -Enabled True -Profile Any -ErrorAction SilentlyContinue
  if (-not (Get-NetFirewallRule -Name Rangectl-WinRM-HTTP -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name Rangectl-WinRM-HTTP -DisplayName "rangectl WinRM HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any | Out-Null
  }
  Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value true
  Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value true
}

Disable-RangectlWindowsUpdates
Disable-RangectlBitLocker
Disable-RangectlVirtualizationSecurity
$restartRequired = Install-RangectlWindowsUpdates
if ($restartRequired) {
  Write-RangectlLog "Offline updates require a reboot; scheduling bootstrap resume"
  Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce" -Name "RangectlFirstLogon" -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$localScript`""
  Restart-Computer -Force
  exit 0
}
Enable-RangectlWinRM
Write-RangectlLog "rangectl first-logon bootstrap complete"
'''


CLOUDBASE_INIT_P2_PS1 = r'''
$ErrorActionPreference = "Stop"
$cloudbasePath = "C:\Program Files\Cloudbase Solutions\Cloudbase-Init"
$confPath = Join-Path $cloudbasePath "conf"
if (-not (Test-Path $confPath)) {
  Write-Host "ERROR: Cloudbase-Init conf path not found: $confPath"
  exit 1
}

$sysprepRoot = $null
foreach ($drive in "D","E","F","G","H","I","J","K","L","M","N","O","P","Q","R","S","T","U","V","W","X","Y","Z") {
  foreach ($candidate in @("${drive}:\sysprep", "${drive}:\")) {
    if ((Test-Path (Join-Path $candidate "cloudbase-init.conf")) -and (Test-Path (Join-Path $candidate "cloudbase-init-unattend.xml"))) {
      $sysprepRoot = $candidate
      break
    }
  }
  if ($sysprepRoot) { break }
}
if (-not $sysprepRoot) {
  Write-Host "ERROR: generated rangectl sysprep directory not found"
  exit 1
}

foreach ($name in "cloudbase-init.conf","cloudbase-init-unattend.conf","cloudbase-init-unattend.xml") {
  Copy-Item -Path (Join-Path $sysprepRoot $name) -Destination (Join-Path $confPath $name) -Force
}

$localScriptsPath = Join-Path $cloudbasePath "LocalScripts"
New-Item -ItemType Directory -Path $localScriptsPath -Force | Out-Null
New-Item -ItemType Directory -Path "C:\setup" -Force | Out-Null
$winrmScript = @'
$ErrorActionPreference = "Continue"
$logPath = "C:\setup\rangectl-cloudbase-winrm.log"
function Write-RangectlLog {
  param([string]$Message)
  $line = "$(Get-Date -Format s) $Message"
  Add-Content -Path $logPath -Value $line
}
Write-RangectlLog "configuring clone WinRM"
powercfg.exe /hibernate off | Out-Null
powercfg.exe /change standby-timeout-ac 0 | Out-Null
powercfg.exe /change hibernate-timeout-ac 0 | Out-Null
powercfg.exe /change monitor-timeout-ac 0 | Out-Null
$wlms = Get-Service -Name WLMS -ErrorAction SilentlyContinue
if ($wlms) {
  Stop-Service -Name WLMS -Force -ErrorAction SilentlyContinue
  & sc.exe config WLMS start= disabled | Out-Null
  Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\WLMS" -Name Start -Value 4 -ErrorAction SilentlyContinue
}
reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f | Out-Null
Set-Service -Name WinRM -StartupType Automatic
Start-Service -Name WinRM
Set-WSManQuickConfig -Force
Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $true
Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $true
Enable-PSRemoting -Force
Set-NetFirewallRule -DisplayGroup "Windows Remote Management" -Enabled True -Profile Any -ErrorAction SilentlyContinue
if (-not (Get-NetFirewallRule -Name Rangectl-WinRM-HTTP -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -Name Rangectl-WinRM-HTTP -DisplayName "rangectl WinRM HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any | Out-Null
}
Write-RangectlLog "clone WinRM configured"
exit 0
'@
$winrmScriptPath = "C:\setup\rangectl-enable-winrm.ps1"
$localWinrmScriptPath = Join-Path $localScriptsPath "99-rangectl-enable-winrm.ps1"
Set-Content -Path $winrmScriptPath -Value $winrmScript -Encoding ASCII -Force
Set-Content -Path $localWinrmScriptPath -Value $winrmScript -Encoding ASCII -Force
$taskXml = @'
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Keep rangectl clone WinRM enabled after Cloudbase-Init reboots.</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>2020-01-01T00:00:00</StartBoundary>
    </BootTrigger>
  </Triggers>
  <Principals>
    <Principal id="System">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>5</Priority>
  </Settings>
  <Actions Context="System">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File C:\setup\rangectl-enable-winrm.ps1</Arguments>
    </Exec>
  </Actions>
</Task>
'@
$taskXmlPath = "C:\setup\rangectl-enable-winrm-task.xml"
Set-Content -Path $taskXmlPath -Value $taskXml -Encoding Unicode -Force
& schtasks.exe /Create /TN "RangectlEnableWinRM" /XML $taskXmlPath /F | Out-Null

& sc.exe config cloudbase-init obj= "LocalSystem" | Out-Null
Set-Service -Name cloudbase-init -StartupType Automatic
& sc.exe config cloudbase-init start= auto | Out-Null
Stop-Service -Name cloudbase-init -Force -ErrorAction SilentlyContinue
& net user cloudbase-init /delete 2>$null | Out-Null

$sysprepExe = "C:\Windows\System32\Sysprep\sysprep.exe"
if (-not (Test-Path $sysprepExe)) {
  Write-Host "ERROR: sysprep.exe not found"
  exit 1
}
Set-Location $confPath
Write-Host "Starting sysprep shutdown."
$process = Start-Process -FilePath $sysprepExe -ArgumentList "/generalize /oobe /mode:vm /unattend:cloudbase-init-unattend.xml /quit /quiet" -NoNewWindow -Wait -PassThru
Write-Host "Sysprep exit code: $($process.ExitCode)"
if ($process.ExitCode -ne 0) {
  exit $process.ExitCode
}
shutdown.exe /s /t 15 /f /c "rangectl template sysprep completed"
exit 0
'''


CLOUDBASE_INIT_CONF = r'''[DEFAULT]
username=Administrator
groups=Administrators
inject_user_password=true
config_drive_label=cidata
config_drive_raw_hdd=true
config_drive_cdrom=true
config_drive_vfat=true
bsdtar_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\bin\bsdtar.exe
mtools_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\bin\
verbose=true
debug=true
logdir=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\log\
logfile=cloudbase-init.log
default_log_levels=comtypes=INFO,suds=INFO,iso8601=WARN,requests=WARN
logging_serial_port_settings=
mtu_use_dhcp_config=true
ntp_use_dhcp_config=true
local_scripts_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\LocalScripts\
metadata_services=cloudbaseinit.metadata.services.configdrive.ConfigDriveService
plugins=cloudbaseinit.plugins.common.networkconfig.NetworkConfigPlugin,cloudbaseinit.plugins.common.localscripts.LocalScriptsPlugin,cloudbaseinit.plugins.windows.setuserpassword.SetUserPasswordPlugin,cloudbaseinit.plugins.common.userdata.UserDataPlugin,cloudbaseinit.plugins.common.sethostname.SetHostNamePlugin,cloudbaseinit.plugins.common.mtu.MTUPlugin,cloudbaseinit.plugins.windows.extendvolumes.ExtendVolumesPlugin
check_latest_version=false
network_adapter=Ethernet
'''


CLOUDBASE_INIT_UNATTEND_CONF = r'''[DEFAULT]
username=Administrator
groups=Administrators
inject_user_password=true
config_drive_label=cidata
config_drive_raw_hdd=true
config_drive_cdrom=true
config_drive_vfat=true
bsdtar_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\bin\bsdtar.exe
mtools_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\bin\
verbose=true
debug=true
logdir=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\log\
logfile=cloudbase-init-unattend.log
default_log_levels=comtypes=INFO,suds=INFO,iso8601=WARN,requests=WARN
logging_serial_port_settings=
mtu_use_dhcp_config=true
ntp_use_dhcp_config=true
local_scripts_path=C:\Program Files\Cloudbase Solutions\Cloudbase-Init\LocalScripts\
metadata_services=cloudbaseinit.metadata.services.configdrive.ConfigDriveService
plugins=cloudbaseinit.plugins.common.mtu.MTUPlugin,cloudbaseinit.plugins.common.sethostname.SetHostNamePlugin,cloudbaseinit.plugins.windows.extendvolumes.ExtendVolumesPlugin,cloudbaseinit.plugins.common.networkconfig.NetworkConfigPlugin,cloudbaseinit.plugins.common.userdata.UserDataPlugin
allow_reboot=false
stop_service_on_exit=false
check_latest_version=false
'''


CLOUDBASE_INIT_UNATTEND_XML = r'''<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="generalize">
    <component name="Microsoft-Windows-Security-SPP" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <SkipRearm>0</SkipRearm>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS">
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{locale}</SystemLocale>
      <UILanguage>{locale}</UILanguage>
      <UserLocale>{locale}</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <TimeZone>{timezone}</TimeZone>
      <AutoLogon>
        <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
        <Enabled>true</Enabled>
        <LogonCount>1</LogonCount>
        <Username>{username}</Username>
      </AutoLogon>
      <UserAccounts>
        <AdministratorPassword>
          <Value>{password}</Value>
          <PlainText>true</PlainText>
        </AdministratorPassword>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{username}</Name>
            <Group>Administrators</Group>
            <Password><Value>{password}</Value><PlainText>true</PlainText></Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <NetworkLocation>Work</NetworkLocation>
        <ProtectYourPC>3</ProtectYourPC>
        <SkipMachineOOBE>true</SkipMachineOOBE>
        <SkipUserOOBE>true</SkipUserOOBE>
      </OOBE>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add">
          <Order>1</Order>
          <Path>sc config cloudbase-init start= auto</Path>
          <Description>Re-enable auto start of cloudbase-init</Description>
          <WillReboot>Never</WillReboot>
        </RunSynchronousCommand>
      </RunSynchronous>
    </component>
  </settings>
</unattend>
'''
