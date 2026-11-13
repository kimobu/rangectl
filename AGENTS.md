# AGENTS.md

Guidance for AI agents and contributors writing code in `rangectl`.

This file is not an operator runbook and should not catalog scenario content, host assignments, lab IPs, or cohort instructions. Put scenario and range facts in the source-of-truth range definitions, generated docs, or lab admin guides instead.

## First Principles

- Read the relevant source before editing. Do not infer behavior from old comments or stale generated files.
- Keep `rangectl` as an orchestration layer. It should parse, validate, render, sequence, and delegate to tools such as OpenTofu/Terraform, Ansible, Tart, and provider CLIs.
- Do not add an interactive framework shell. Commands should remain discrete, scriptable, and safe to rerun.
- Keep changes scoped. Avoid unrelated refactors while fixing a range, role, provider, or scenario bug.
- Preserve user work. The worktree may be dirty; do not revert files you did not intentionally change.

## Ownership Boundaries

Every managed resource should have one lifecycle owner:

- `tofu:proxmox`, `tofu:aws`, and `tofu:azure` own infrastructure declared through generated Terraform/OpenTofu.
- `tart` owns Tart macOS guest lifecycle.
- `external` marks things `rangectl` should reference but not create or destroy.
- Ansible owns guest configuration, services, software, telemetry, and scenario setup after infrastructure exists.
- `rangectl` owns range parsing, validation, generation, command sequencing, docs generation, and local state metadata.

When adding a feature, decide which owner should control it before writing code. Do not solve ownership confusion by adding imperative side effects in a random phase.

## Source Of Truth

- Range topology, personas, hosts, IPs, VLANs, roles, and scenario mappings belong in `ranges/*.yml`.
- Generated inventories, provider manifests, and docs under `state/` or `docs/generated/` are outputs. Do not edit them as the source of truth.
- Operator instructions belong in hand-written docs such as `docs/*.md` or scenario-specific admin guides.
- Role defaults belong in `roles/<role>/defaults/main.yml`; task behavior belongs in the role tasks/templates.
- If generated output is wrong, fix the generator or range definition, then rerender.

## Safety And State

Never commit or publish local operational state or secrets:

- `state/`
- `.terraform/`
- Terraform/OpenTofu state files
- generated SSH keys
- `.rangectl.env` or other local credential files
- lab logs, packet captures, Elastic exports, screenshots, or temporary helper scripts

Use `infra plan` for routine infrastructure checks. Destructive operations should remain explicit and require the existing approval flags. Do not add code that silently destroys or replaces lab resources.

For security-training scenarios, keep the lab-contained safety model: simulated infrastructure and seeded fake data are acceptable; uncontrolled malware, uncontrolled propagation, and real external callbacks are not.

## Development Workflow

Run commands from the repository root.

The project uses `uv` for Python dependency management. Prefer the checked-in wrapper for CLI behavior and `uv run` for Python tooling:

```bash
uv run python -m compileall cmd rangectl_lib
./rangectl validate <range>
./rangectl render <range>
./rangectl infra plan <range>
./rangectl configure <range> --phase <phase> --dry-run
```

For Ansible work, use the generated inventory for the range you are changing:

```bash
ANSIBLE_HOST_KEY_CHECKING=False ansible-playbook \
  -i state/<range>/inventory/hosts.yml \
  playbooks/<range>/<phase>.yml --syntax-check
```

Prefer targeted checks over broad reruns while diagnosing. Use `rg` for searches and direct SSH or Ansible ad hoc commands when they give faster, clearer signal.

## Python Code Guidance

- Keep the CLI thin: parse arguments, load config, validate, dispatch, and report.
- Put reusable behavior in `rangectl_lib/`, not in large command functions.
- Prefer small provider-specific helpers over broad abstractions that hide important provider differences.
- Keep validation errors concrete and actionable. Include the range key, host name, phase, or file path when possible.
- Avoid ad hoc string parsing when structured data or existing helpers are available.
- Preserve stable command output where scripts may depend on it.

## Ansible Guidance

- Keep phases rerunnable and independently callable.
- Keep roles idempotent where practical. When a task cannot be fully idempotent, make the condition and `changed_when`/`failed_when` behavior explicit.
- Prefer role defaults and inventory variables over hard-coded values in tasks.
- Register and handle expected failures instead of letting one transient UI or package-manager issue collapse an entire phase unnecessarily.
- For macOS automation, assume UI behavior varies by OS version. Add narrow helpers and visible operator notes rather than burying fragile assumptions in generic tasks.
- For package managers, verify the executable is what you think it is. For example, Homebrew detection should confirm `brew --version`, not just trust a path on `PATH`.
- After editing roles or playbooks, run at least a syntax check for the affected playbook.

## Provider Guidance

- OpenTofu/Terraform generation should be deterministic from range input.
- Do not mix provider lifecycle behavior into Ansible roles.
- Tart guests should be managed through the Tart provider path, not through ad hoc `tart run` calls in configuration roles.
- Provider-specific state capture should go under `state/<range>/` and should not be committed.

## Scenario And Activity Code

`AGENTS.md` should only describe how to work on scenario code, not what a specific scenario currently does.

When changing scenario or activity behavior:

- Use source reports and scenario PDFs as the authority.
- Keep behavior lab-contained and deterministic enough to troubleshoot.
- Prefer realistic, source-faithful host and network behavior over dummy marker files.
- Keep timing, dwell, and replay state explicit in range data or scenario code.
- Add or update the appropriate operator-facing docs when manual setup, scenario start steps, or expected telemetry changes.
- Do not put host-by-host scenario matrices, IP maps, or cohort instructions in this file.

## Documentation Rules

Update docs when you change:

- CLI commands or flags
- range schema or provider behavior
- Ansible phase ordering or manual approval requirements
- scenario operator steps
- generated documentation behavior

Choose the right location:

- `AGENTS.md`: coding guidance for AI agents/contributors.
- `README.md`: high-level project overview and common commands.
- `docs/*.md`: operator/developer references.
- `docs/generated/*.md`: generated range documentation; update the generator/source inputs, not only the output.
- scenario admin guides: manual lab-start and teaching workflows.

## Troubleshooting Discipline

- Reproduce the failure before patching when feasible.
- Inspect generated inventory and rendered provider files before assuming a role is wrong.
- Check the real target state after a helper claims success.
- For hangs, inspect child processes before killing or rerunning.
- Prefer small fixes with clear verification over large speculative rewrites.
- Leave a concise note in the final response about what was changed and what was not verified.

## Windows Workstation Lessons

These notes are generic rangectl troubleshooting guidance. Keep range-specific
host lists, IPs, and scenario runbooks in the content pack.

- Treat QEMU Guest Agent as advisory on busy Windows guests. If QGA drops during
  software or telemetry installation, check WinRM, Proxmox status, and the VM
  console before deciding the guest is broken.
- For heavy Windows phases, increase WinRM timeouts instead of assuming a
  stalled play:

  ```bash
  ./rangectl configure <range> --phase <phase> --limit <host> \
    --extra-vars 'ansible_winrm_operation_timeout_sec=90 ansible_winrm_read_timeout_sec=120'
  ```

- Snapshot after each high-risk phase only after a reboot and a direct health
  check. A successful Ansible task is not enough for Windows template or domain
  validation.
- On Windows 11 Proxmox guests, a post-domain-join OVMF boot hang can be caused
  by VBS, Credential Guard, or related virtualization security settings. Disable
  those intentionally in workstation configuration before blaming Packer or
  Cloudbase-Init.
- Keep BitLocker and automatic device encryption disabled for lab templates.
  Even if BitLocker is not the immediate boot failure, it makes template clones
  harder to diagnose.
- If a domain secure-channel repair must survive reboot, prefer
  `Reset-ComputerMachinePassword` with domain admin credentials over relying on
  `Test-ComputerSecureChannel -Repair` alone.
- Windows software installers can be quiet for a long time. Before killing a
  role, check whether the staged installer is still downloading or growing.
- Validate what a limited run actually validates. A play run with `--limit` may
  skip domain-controller or cross-host checks and still report success.

## Style

- Default to ASCII when editing files unless the file already uses non-ASCII or the content requires it.
- Add comments sparingly, only where they clarify non-obvious behavior.
- Keep YAML readable and explicit.
- Avoid unrelated formatting churn.
- Use `apply_patch` for hand edits.
