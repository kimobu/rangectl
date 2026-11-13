# Template Builds

`rangectl` ranges use logical template names such as `win2019` or
`ubuntu-2404`. Template catalogs map those names to provider-specific template
IDs. Template builds create the provider templates before a range clones them.

The implemented build target is Windows on Proxmox with Packer. `rangectl`
generates the unattended-install and Cloudbase-Init media, runs Packer, and
converts the resulting VM into a Proxmox template. The build definition names
the Windows, VirtIO, and Cloudbase-Init source assets.

## Values you must provide

The build definition or `.rangectl.env` must provide:

- Proxmox target: endpoint credentials in `.rangectl.env`, target node,
  storage, pool, bridge, and default ISO storage.
- Source assets: Windows ISO, VirtIO ISO, and Cloudbase-Init MSI locations. For
  Microsoft evaluation media, provide either a download URL or a Proxmox ISO
  volume path.
- Legal and licensing choices: evaluation ISO versus licensed media, product key
  handling, and acceptance of vendor license terms.
- Template identity: logical catalog name, output VMID, Proxmox VM name, OS
  family/version, and architecture.
- Hardware policy: CPU, memory, disk size, BIOS versus UEFI, machine type, TPM,
  Secure Boot, SCSI controller, and NIC model.
- Locale policy: timezone, locale, keyboard layout, and administrator username.
- Bootstrap secrets: initial administrator password and WinRM password. Store
  these in environment variables, not inline YAML.
- Readiness requirements: WinRM availability, QEMU guest agent installation,
  Cloudbase-Init configuration, sysprep generalization, guest shutdown, and the
  Proxmox template flag.

Template builds verify Proxmox TLS and SSH host keys by default. Set
`PROXMOX_VE_CA_FILE` for a private CA and `RANGECTL_SSH_KNOWN_HOSTS` for an
isolated known-hosts file. An isolated lab without managed trust stores can set
`PROXMOX_VE_INSECURE=true` and `RANGECTL_SSH_INSECURE=true`. Both settings
disable server identity verification.

## Values `rangectl` can discover

`rangectl` discovers or derives:

- Proxmox API reachability and authentication mode from `.rangectl.env`.
- Existing VMID collisions, node names, storage IDs, bridges, and pools.
- Whether required ISO files are already present on Proxmox.
- Whether Packer and ISO build tooling are installed locally. `genisoimage` is
  preferred for the Windows autounattend ISO because it preserves the exact
  `Autounattend.xml` filename Windows setup scans for. `xorriso` is acceptable
  when called with relaxed filename options.
- Source URL metadata, including the final redirect URL, filename, architecture,
  and hints about locale, Windows version, and edition.
- Generated Autounattend, first-boot scripts, Cloudbase-Init config, and Packer
  variable files.
- Download size, build status, logs, final template VMID, and catalog update
  suggestions.

## Expected Windows flow

The Windows build is unattended:

1. Packer boots the installer.
2. The generated Autounattend file partitions the disk and selects the requested
   Windows image.
3. First-boot scripts install or verify VirtIO drivers, QEMU guest agent, and
   Cloudbase-Init.
4. Packer connects over WinRM and runs its provisioners.
5. Sysprep generalizes and shuts down the guest.
6. The build converts the stopped VM into a Proxmox template.

Use the console only to diagnose a failed build. A build that requires manual
driver loading or installer input does not meet the unattended-build contract.
Correct its answer file, driver path, image index, boot command, or source media
definition.

## Commands

The implemented command surface is:

```bash
./rangectl templates list
./rangectl templates check example
./rangectl templates inspect-url \
  'https://go.microsoft.com/fwlink/p/?LinkID=2195174&clcid=0x409&culture=en-us&country=US'
./rangectl templates doctor proxmox
./rangectl templates build proxmox win2019 \
  --definition templates/builds/proxmox-windows.example.yml \
  --runner proxmox-node \
  --replace-existing
```

- `templates list` shows the resolved catalog.
- `templates check` rejects unknown logical template names in a range.
- `templates inspect-url` follows redirects and reports metadata without
  downloading the ISO.
- `templates doctor` checks local build tools and Proxmox prerequisites.
- `templates build` renders, validates, and runs the Packer build definition.

The Proxmox Windows build generates its own Autounattend and Cloudbase-Init
media, but the generated files must preserve the same contract as the known-good
GOAD-style Proxmox templates:

- Proxmox cloud-init is enabled on the template VM.
- Cloudbase-Init can read Proxmox config-drive metadata on cloned VMs.
- OOBE is suppressed during both installation and sysprep clone boot.
- WinRM comes up during the Packer build without manual console work.
- Sysprep generalizes the VM and leaves Cloudbase-Init enabled for the clone.

Windows profiles use fixed hardware baselines by OS family. Windows 11 builds
follow the Proxmox Windows 11 guest requirements and the validated
GOAD-compatible disk layout:

- OVMF/UEFI with EFI storage, pre-enrolled Microsoft keys, and TPM 2.0.
- `q35` machine type and Proxmox `os = win11`.
- VirtIO network.
- SATA OS disk, matching the previously working template path.
- Discard enabled, and writeback cache restored after the Packer build.
- QEMU guest agent installed inside the template.

See [Windows 11 guest best practices](https://pve.proxmox.com/wiki/Windows_11_guest_best_practices)
in the Proxmox documentation.

Windows 10 and Windows Server 2016/2019 use the validated GOAD-compatible
UEFI/q35/SATA baseline. Windows Server 2012 R2 uses a legacy-compatible path:
SeaBIOS, i440fx, SATA disk, older VirtIO media, no runtime VirtIO reinstall, and
Cloudbase-Init 0.9.11. Newer Cloudbase-Init builds need prerequisite updates on
a clean 2012 R2 eval image and fail with MSI `1603` without them.

For most Proxmox Windows builds, prefer `runner: proxmox-node`. `rangectl`
stages the generated Packer workdir, local Packer binary, and Proxmox plugin
onto the Proxmox node, then runs Packer there so WinRM traffic stays on the same
host and VLAN as the build VM. If the build VLAN has no IP on the Proxmox node,
set `runner_vlan_interface` and `runner_vlan_ip` in the private build
definition. Windows Server 2012 R2 can use `runner: local` when the Proxmox node
source address is not accepted by the guest WinRM listener.

Clone-test every rebuilt Windows template before a range uses it. The clone must
boot without OOBE prompts, consume Cloudbase-Init metadata, apply the requested
hostname and network values, and answer WinRM. Packer completion alone does not
test clone customization.

## Downloaded assets

Downloaded assets are written atomically and cached with URL, size, and SHA-256
metadata. Remote assets have a 16 GiB default limit. Set `sha256`,
`max_size_bytes`, or `max_size_gib` in an asset definition to require a checksum
or change that limit. Plain HTTP is supported for lab-local artifact servers.
