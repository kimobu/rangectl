# rangectl

`rangectl` deploys and operates cyber training ranges from YAML definitions. It
validates each range, renders provider and Ansible files, sequences deployments,
and records local run metadata. Terraform/OpenTofu, Ansible, Tart, and provider
CLIs continue to own the work they are designed to perform.

Start with [Getting Started](docs/getting-started.md) to deploy a range. Use the
[command reference](docs/usage.md) when you already have a working checkout.

## Deployment workflow

Run these commands from the repository root, replacing `RANGE` with a range key:

```bash
./rangectl env RANGE
./rangectl doctor RANGE
./rangectl doctor RANGE --online
./rangectl templates check RANGE
./rangectl plan RANGE
./rangectl infra plan RANGE
./rangectl phases RANGE
./rangectl up RANGE
./rangectl status RANGE
```

The checks serve different purposes:

- `env` lists the settings required by the range.
- `doctor` checks the definition, local tools, playbooks, template catalog, and
  environment. `--online` also checks provider access.
- `plan` shows the orchestration steps without calling providers or Ansible.
- `infra plan` asks each infrastructure provider for its proposed changes.
- `phases` lists the Ansible phases, including phases that require a separate
  manual run.
- `up` renders files, applies infrastructure, and runs non-manual configuration
  phases.
- `status` reports the locally rendered state and latest run record.

`up` refuses a provider plan that destroys or replaces resources unless you pass
`--allow-replace`. Destruction also requires an explicit approval flag:

```bash
./rangectl destroy RANGE --auto-approve
```

## Range definitions

A range definition records its networks, hosts, lifecycle owners, templates,
Ansible variables, configuration phases, and generated documentation. Public
examples live in `ranges/`. Private and course-specific definitions belong in a
content pack.

Each managed resource has one lifecycle owner:

- `tofu:proxmox`, `tofu:aws`, and `tofu:azure` use generated
  Terraform/OpenTofu.
- `tart` manages Tart macOS guests on Apple Silicon hosts.
- `external` references infrastructure that `rangectl` must not create or
  destroy.

Ansible configures guests after their lifecycle owner creates them. See
[Architecture](docs/architecture.md) for the ownership and data-flow contracts.

## Content packs

Content packs keep private ranges, artifacts, playbooks, roles, and operator
guides outside the public engine repository. A named pack can use this layout:

```text
lab-content/
  training-range/
    range.yml
    playbooks/
    roles/
    docs/
```

`rangectl` searches these locations in order:

1. paths in `RANGECTL_CONTENT_DIRS`;
2. `content/` in this checkout;
3. `~/lab-content`;
4. `~/rangectl-lab-content/packs`.

With the example layout at `~/lab-content/training-range`, use the pack by its
range key:

```bash
./rangectl doctor training-range
./rangectl up training-range
```

## Templates and configuration

Template catalogs map logical names such as `win2019`, `ubuntu-2404`, and
`macos-sonoma` to provider-specific template identifiers:

```bash
./rangectl templates list
./rangectl templates check RANGE
```

The repository also provides a Packer workflow for building Windows templates
on Proxmox. See [Template Builds](docs/template-builds.md).

Ranges split Ansible work into named phases. Run one phase without repeating the
full deployment:

```bash
./rangectl configure RANGE --phase services --dry-run
./rangectl configure RANGE --phase services
```

Phases marked `manual: true` do not run during `up`. The range's operator guide
should state when to run them.

## Day-two operations

Use the generated inventory, provider-neutral power commands, and snapshots for
routine operations:

```bash
./rangectl inventory RANGE --path
./rangectl power status RANGE
./rangectl power off RANGE --hosts workstation01 --execute
./rangectl snapshot create RANGE clean --execute
```

Activity simulation can browse, process mail, open applications, create files,
queue scenario triggers, and collect JSONL logs from configured endpoints:

```bash
./rangectl activity validate RANGE
./rangectl activity compile RANGE
./rangectl configure RANGE --phase activity-simulation
./rangectl activity collect RANGE
```

See [Activity Simulation](docs/activity.md) for persona, profile, email, and
agent-state configuration.

## Local files

Copy `rangectl.env` to `.rangectl.env` for credentials, provider endpoints, and
operator-specific paths. Git ignores `.rangectl.env`.

Rendered inventories, provider files, run records, and other local runtime state
are written under `state/<range>/`. Do not commit this directory.

```text
rangectl/
  cmd/rangectl/       # CLI entry point
  rangectl_lib/       # reusable orchestration code
  ranges/             # public example range definitions
  schemas/            # range JSON Schema
  templates/          # example catalogs and build definitions
  playbooks/          # reusable Ansible playbooks
  roles/              # reusable Ansible roles
  docs/               # operator and developer documentation
  state/              # generated local runtime state
```

## Documentation

- [Getting Started](docs/getting-started.md): install prerequisites and deploy a
  first range.
- [Usage](docs/usage.md): command and local-state reference.
- [Architecture](docs/architecture.md): lifecycle ownership and generated data
  flow.
- [Template Builds](docs/template-builds.md): build and clone-test Windows
  templates on Proxmox.
- [Activity Simulation](docs/activity.md): configure background endpoint
  activity.
- [Local Artifacts](artifacts/README.md): download and stage untracked
  installers and course files.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local checks and repository
boundaries. Report security problems using [SECURITY.md](SECURITY.md); do not
put credentials, private range definitions, or unpublished exploit details in
a public issue.

## License and provenance

`rangectl` is distributed under the
[GNU General Public License version 3](LICENSE). See
[NOTICE.md](NOTICE.md) for included third-party components and preserved
license notices.
