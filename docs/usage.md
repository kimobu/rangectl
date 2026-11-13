# Usage

This page describes the command groups and local files that operators use after
installation. For a first deployment, follow [Getting Started](./getting-started.md).

Run the checked-in wrapper from the repository root:

```bash
./rangectl validate example
```

The wrapper runs the project through `uv`; no virtual environment activation is
required.

## Content packs

Private ranges can live outside this repository. `rangectl` searches for
content packs in this order:

- paths listed in `RANGECTL_CONTENT_DIRS`
- `content/` under the `rangectl` checkout
- `~/lab-content`
- `~/rangectl-lab-content/packs`

Each named pack can use this layout:

```text
lab-content/
  training-range/
    range.yml
    playbooks/
    roles/
    docs/
```

With `~/lab-content/training-range/range.yml` present, the range can be used by
name:

```bash
./rangectl validate training-range
./rangectl render training-range
./rangectl configure training-range --phase services
```

Relative playbook and documentation paths resolve from the external range
directory. Generated state remains in this checkout under `state/<range>/`.

## Local environment

Copy the environment template, then add provider credentials and
operator-specific settings:

```bash
cp rangectl.env .rangectl.env
$EDITOR .rangectl.env
```

Git ignores `.rangectl.env`. `rangectl` loads it from the repository root and
merges it with the current process environment.

List the settings required by a range:

```bash
./rangectl env example
```

## Local runtime files

Installers and archives are not stored in Git. The
[Local Artifacts](../artifacts/README.md) guide lists the official download URLs
and exact staging paths for Sysmon and Windows Management Framework 5.1.

Some older Windows images need the NuGet PackageManagement provider before
PowerShell modules can be installed. `rangectl` first looks for this optional
offline file:

```text
playbooks/files/Microsoft.PackageManagement.NuGetProvider-2.8.5.208.dll
```

Git ignores this directory because the DLL is a local runtime dependency. If
the file is absent, `rangectl` tries to install the provider from the PowerShell
Gallery. In a disconnected lab, place the DLL at the path above before running
the base phase.

## Validate

```bash
./rangectl validate example
```

Offline validation checks:

- required range fields
- space CIDRs, gateways, and VLANs
- overlapping spaces
- duplicate IPs
- host owner/type matches
- Tart host references

Online validation adds provider prerequisite checks:

```bash
./rangectl validate example --online
```

Store provider credentials and local host details in `.rangectl.env` or export
them in the current process environment.

Proxmox HTTPS and SSH connections verify server identity by default. Set
`PROXMOX_VE_CA_FILE` for a private Proxmox CA and
`RANGECTL_SSH_KNOWN_HOSTS` for a dedicated SSH known-hosts file. The
`PROXMOX_VE_INSECURE=true` and `RANGECTL_SSH_INSECURE=true` overrides disable
identity verification and are intended only for isolated, disposable labs.

## Doctor

```bash
./rangectl doctor example
./rangectl doctor example --online
```

`doctor` verifies the range definition, content-pack discovery, local state,
playbook paths, required tools, template catalog coverage, and environment
values. `--online` also probes configured providers such as Proxmox, Tart, and
AWS.

## Render

```bash
./rangectl render example
```

`render` writes provider files and inventory under `state/<range>/`:

```text
state/example/inventory/hosts.yml
state/example/range-state.json
state/example/render/tofu/
state/example/render/ansible/
state/example/runs/
```

Built-in example ranges write relative docs output from the repository root.
External content packs write relative docs output under the pack range
directory, so generated private docs do not land in this public checkout.

Mutating commands write JSON records under `state/<range>/runs/`. Each record
contains the command, return code, timestamps, and duration. `status` prints the
latest record when one exists.

## Template catalogs

Provider template mappings live in YAML catalogs under `templates/*.yml`.
`rangectl` loads them in this order:

- public examples under `rangectl/templates/`
- content-pack catalogs such as `~/lab-content/templates/proxmox.yml`
- range-local catalogs such as `<pack>/<range>/templates/proxmox.yml`

Later catalogs override earlier ones. A Proxmox range can use a logical template
name such as `win2019`; the catalog maps that name to the provider template key
and VMID used for cloning.

Template build definitions describe how to create a provider template; catalogs
describe how a range refers to an existing template. See
[Template Builds](./template-builds.md) and
`templates/builds/proxmox-windows.example.yml` for the Windows-on-Proxmox
contract.

Useful template commands:

```bash
./rangectl templates list
./rangectl templates check example
./rangectl templates inspect-url \
  'https://go.microsoft.com/fwlink/p/?LinkID=2195174&clcid=0x409&culture=en-us&country=US'
```

`templates list` shows the resolved catalog. `templates check` rejects unknown
logical template names. `inspect-url` follows redirects and infers source
metadata without downloading the installer.

## Configure

```bash
./rangectl phases example
./rangectl configure example --dry-run
./rangectl configure example --phase services
./rangectl configure example --phase services --limit app01
```

`configure` runs the Ansible phases declared by the range. Phases marked
`manual: true` are skipped by `up`. Run them explicitly with `--phase` when the
range instructions tell you to.

External content packs can keep playbooks and roles beside `range.yml`. For a
relative playbook path, `rangectl` checks the pack range directory before the
public checkout. The Ansible roles path also includes `<pack-range>/roles` when
that directory exists.

`phases` prints phase names, manual status, resolved playbook paths, and the
exact command to run each phase.

## Up

```bash
./rangectl up example
```

`up` runs the normal deployment sequence:

- render generated files
- apply all infrastructure providers enabled by the range
- run configured Ansible phases, skipping phases marked `manual: true`

`up` approves non-destructive infrastructure applies. It refuses a plan that
destroys or replaces resources unless you pass `--allow-replace`. Review
`doctor`, `plan`, and `infra plan` output before the first deployment or after a
topology change.

## Plan

```bash
./rangectl plan example
```

`plan` prints an orchestration-level plan. It does not call Terraform/OpenTofu,
Ansible, Proxmox, AWS, Azure, or Tart.

## Infra

```bash
./rangectl infra plan example
./rangectl infra apply example --auto-approve
./rangectl infra destroy example --auto-approve
./rangectl destroy example --auto-approve
```

`infra` renders the range, selects its infrastructure providers, and invokes
each provider's lifecycle tool. `apply` and `destroy` require
`--auto-approve`.

Set `RANGECTL_TERRAFORM_PARALLELISM` in `.rangectl.env` to limit concurrent
Terraform/OpenTofu operations. Lowering the value can prevent simultaneous VM
clone tasks from timing out against Proxmox.

Set `RANGECTL_SKIP_PROVIDERS` or pass `--skip-provider` when you intentionally
want a partial run. For example, `RANGECTL_SKIP_PROVIDERS=aws` lets a range with
optional cloud content deploy its local Proxmox and Tart infrastructure without
touching AWS.

The top-level `destroy` and `down` commands invoke the same destroy workflow and
have the same approval requirement as `infra destroy`. If stale partial state
blocks provider refresh, review the state and then pass `--no-refresh` to
`destroy`, `down`, or `infra destroy`.

## Status and inventory

```bash
./rangectl status example
./rangectl inventory example
./rangectl inventory example --path
./rangectl inventory example --render
```

`status` summarizes local rendered state and the latest run record; it is not a
guest health check. `inventory` prints the generated Ansible inventory.
`--path` prints only its path, and `--render` regenerates it first.

## Power

```bash
./rangectl power status example
./rangectl power on example --hosts workstation01 --execute
./rangectl power off example --hosts workstation01 --execute
```

`power status` runs immediately. State-changing power actions require
`--execute`. Provider selection comes from the range definition; `--hosts`
limits the action to a comma-separated host list.

## Snapshots

```bash
./rangectl snapshot list example
./rangectl snapshot create example clean --execute
./rangectl snapshot revert example clean --hosts workstation01 --execute
./rangectl snapshot delete example clean --execute
```

Snapshot changes require `--execute`. Use `--hosts` when only selected provider
resources should be changed.

## Activity

A range can compile per-host activity configuration, install an agent, queue
triggers, and collect JSONL logs. See [Activity Simulation](./activity.md).

## Docs

```bash
./rangectl doc create example
```

`doc create` regenerates the documentation output declared by the range
definition. Change the range definition or generator when that output is wrong;
do not use generated files as the source of truth.
