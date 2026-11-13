# rangectl Architecture

`rangectl` is the orchestration layer for a lab range. It reads and validates a
range definition, renders provider and Ansible files, invokes lifecycle tools,
and records local run metadata. Terraform/OpenTofu, Ansible, Tart, and cloud
CLIs retain their existing responsibilities.

## Ownership

| Resource type | Lifecycle owner | Configuration owner |
| --- | --- | --- |
| Proxmox VM | OpenTofu/Terraform | Ansible |
| Proxmox LXC | OpenTofu/Terraform | Ansible |
| pfSense VM | OpenTofu/Terraform | Ansible or a pack-provided role |
| AWS compute/network/DNS | OpenTofu/Terraform | Ansible where applicable |
| Azure compute/network/DNS | OpenTofu/Terraform | Ansible where applicable |
| Tart macOS VM | Tart provider flow | Ansible/bootstrap scripts |
| External host | External | Ansible only |

One lifecycle owner creates and destroys each resource. For example,
Terraform/OpenTofu creates a Proxmox VM; Ansible configures the guest after it
exists.

## Data flow

```text
ranges/<name>.yml or <content-pack>/<name>/range.yml
    |
    v
validate
    |
    v
state/<name>/render/
    |-- tofu/proxmox
    |-- tofu/aws
    |-- tofu/azure
    |-- tart/
    |-- ansible/
    v
configure / apply phases
    |
    v
state/<name>/range-state.json
    |
    v
generated range docs
```

Built-in examples live under `ranges/`. Private and course-specific ranges
belong in content packs outside this repository. Files below `state/<name>/`
are generated local outputs, not range source.

## Command contract

Each command performs a bounded operation and can run without an interactive
shell:

```bash
rangectl validate example
rangectl doctor example
rangectl plan example
rangectl render example
rangectl configure example
rangectl infra plan example
rangectl doc create example
```

`up` renders files, applies infrastructure, and runs configured non-manual
Ansible phases. The lower-level commands let an operator inspect generated
files, review provider changes, or repeat one phase.

## Provider status

Proxmox rendering uses the `bpg/proxmox` Terraform/OpenTofu provider and local
providers for generated helper assets. Tart macOS guests use a separate
lifecycle because `rangectl` manages Tart over SSH. A range can also declare AWS
or Azure resources.

Provider credentials and local host details belong in `.rangectl.env`, not in a
public range definition. The checked-in `rangectl.env` is a commented template;
Git ignores `.rangectl.env`.

Provider and SSH transports verify server identity by default. Set
`PROXMOX_VE_CA_FILE` for a private Proxmox CA and
`RANGECTL_SSH_KNOWN_HOSTS` for a dedicated SSH known-hosts file. The
`PROXMOX_VE_INSECURE` and `RANGECTL_SSH_INSECURE` overrides disable these
checks. Restrict the overrides to isolated, disposable labs and never put them
in public range definitions.

The pfSense role follows the same rule. It enables strict SSH host-key checking
by default and accepts an optional `pfsense_ssh_known_hosts_file`. A content
pack may disable `pfsense_ssh_strict_host_key_checking` only for a disposable
lab where the operator has accepted the impersonation risk.
