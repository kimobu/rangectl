# Windows template rebuild plan

Use this checklist when changing the Windows-on-Proxmox template pipeline.
Template work is complete only after a generated template boots as a clone and
accepts the expected remote-management and cloud-init configuration.

## Build contract

Every generated Windows template must preserve these properties:

- Packer enables Proxmox cloud-init and names its storage pool.
- Cloudbase-Init enables raw config-drive disks and the ConfigDrive metadata
  service.
- Windows installs without manual console input.
- Initial setup and post-sysprep OOBE are suppressed.
- An elevated bootstrap context enables WinRM before Packer needs it.
- Cloudbase-Init remains installed and enabled after sysprep.
- Sysprep generalizes and shuts down the guest before Proxmox converts it to a
  template.
- A clone consumes config-drive metadata and applies its hostname, network, and
  credentials.

The renderer must reject an operating-system profile that has not defined and
validated its firmware, machine type, storage controller, network device, and
Cloudbase-Init behavior.

## Operating-system profiles

Do not share one hardware profile across incompatible Windows releases.

- Windows 10 LTSC, Server 2016, and Server 2019 use the established BIOS or
  UEFI profile recorded in the build definition.
- Windows Server 2012 R2 uses its legacy VirtIO and offline-update path.
- Windows 11 LTSC requires OVMF/UEFI, an EFI disk, TPM 2.0, the `q35` machine
  type, a VirtIO NIC, and Proxmox `os = win11`.

Windows 11 generation must fail when the selected profile uses SeaBIOS, omits
the EFI disk, or omits TPM 2.0.

## Static validation

Unit tests must confirm that generated output:

- contains `cloud_init = true` and `cloud_init_storage_pool`;
- contains `config_drive_raw_hdd=true`, with no
  `config_drive_raw_hhd` typo;
- gives Windows 11 OVMF, EFI, TPM 2.0, `q35`, and `os = win11`;
- does not add Windows 11-only TPM requirements to older profiles;
- suppresses OOBE in install and sysprep answer files; and
- leaves Cloudbase-Init ready to run when a clone starts.

Run `packer validate` in every generated build directory before starting a
template build.

## Clone validation

Validate one client profile at a time:

1. Render the template work directory.
2. Run unit tests and `packer validate`.
3. Build the template.
4. Clone one disposable VM from it.
5. Confirm that the clone boots without region, keyboard, or account prompts.
6. Confirm that Cloudbase-Init applies hostname and network metadata.
7. Confirm that WinRM responds.
8. Reboot and repeat the boot, metadata, and WinRM checks.

After both a legacy client and Windows 11 pass, repeat the process for each
server profile affected by the change. Do not redeploy a complete range until
the disposable clone checks pass.

## Workstation validation

A Windows workstation is healthy only when:

- the desktop loads after reboot;
- WinRM responds on TCP 5985;
- domain membership and the secure channel are valid;
- required software is installed;
- telemetry services run when the range enables them; and
- lock-screen policy permits any configured user simulation or remote access.

Treat QEMU Guest Agent as an additional signal, not the sole health check. It
can stop responding during large software or telemetry installations while the
guest and WinRM remain healthy.

On affected Windows 11 guests, a post-domain-join OVMF boot hang may require
disabling VBS, Credential Guard, HVCI, LSA isolation, and the corresponding BCD
launch settings before reboot. Keep BitLocker and automatic device encryption
disabled on disposable lab templates unless the range explicitly tests them.

If a domain secure channel needs repair, reset the machine password against the
range's domain controller and restart Netlogon:

```powershell
Reset-ComputerMachinePassword -Server <domain-controller> -Credential <domain-admin>
Restart-Service Netlogon
```

## Change procedure

1. Add or update invariant tests.
2. Change the smallest renderer or profile responsible for the behavior.
3. Render and inspect the generated Packer, answer-file, Cloudbase-Init, and
   bootstrap outputs.
4. Run unit tests and `packer validate`.
5. Build and validate one disposable clone.
6. Update the public example build definition when the schema or required
   inputs change.
7. Record any operator action that cannot be automated safely.

Do not compensate for a broken template with repeated Ansible retries or
one-off clone repairs. Fix the template pipeline, rebuild, and rerun the clone
validation.
