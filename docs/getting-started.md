# Getting Started

This guide installs `rangectl` and deploys a first lab range.

The commands use the public `example` definition. Replace `example` with your
range key when you are ready to use a provider-backed range.

## 1. Clone the Repositories

Clone `rangectl` first:

```bash
git clone https://github.com/kimobu/rangectl.git
cd rangectl
```

If your range lives in a private content pack, clone that too:

```bash
git clone git@github.com:your-org/lab-content.git ~/lab-content
```

`rangectl` searches `~/lab-content` automatically. If the content pack lives
elsewhere, add its parent directory to `RANGECTL_CONTENT_DIRS` in
`.rangectl.env`.

## 2. Install local tools

Install `uv`; the checked-in `./rangectl` wrapper uses it to create the Python
environment and install the required Python packages.

Install the tools required by the providers you use:

- Proxmox: `terraform` or `tofu`, plus `ansible-playbook`.
- AWS or Azure: the corresponding cloud CLI.
- Windows template builds: `packer` and one of `xorriso`, `genisoimage`, or
  `mkisofs`.

`./rangectl doctor RANGE` reports missing tools for a specific range.

## 3. Create `.rangectl.env`

Copy the checked-in example file:

```bash
cp rangectl.env .rangectl.env
$EDITOR .rangectl.env
```

Git ignores `.rangectl.env`. Store local credentials, paths, and
operator-specific settings there; do not add them to a range definition.

To see what a range needs, run:

```bash
./rangectl env example
```

For example, a Proxmox range may require:

```bash
PROXMOX_VE_ENDPOINT=https://proxmox.example.test:8006
PROXMOX_VE_API_TOKEN=user@pam!tokenid=secret
PROXMOX_NODE=pve1
PROXMOX_STORAGE=local-lvm
PROXMOX_NETWORK_BRIDGE=vmbr0
RANGECTL_TERRAFORM_PARALLELISM=2
```

Authenticate with either `PROXMOX_VE_API_TOKEN` or the
`PROXMOX_VE_USERNAME`/`PROXMOX_VE_PASSWORD` pair.

Proxmox TLS and SSH/Ansible host-key verification are enabled by default. Set
`PROXMOX_VE_CA_FILE` to the PEM bundle for a private CA. For SSH template
staging, accept the host key in the normal user file or point
`RANGECTL_SSH_KNOWN_HOSTS` to a dedicated known-hosts file.

An isolated, disposable lab without either trust store can disable the checks:

```bash
PROXMOX_VE_INSECURE=true
RANGECTL_SSH_INSECURE=true
```

Both overrides disable server identity verification. Do not use them outside an
isolated lab network.

`PROXMOX_STORAGE` is where cloned VMs and cloud-init disks are created. Large
ranges need enough free space on that storage for every full clone.

`RANGECTL_TERRAFORM_PARALLELISM` limits concurrent Terraform/OpenTofu provider
operations. A value such as `2` can prevent concurrent Windows clone tasks from
overloading a Proxmox API.

`RANGECTL_SKIP_PROVIDERS` omits named providers. For example, set it to `aws`
when a range includes AWS resources but the current run must deploy only its
local infrastructure.

## 4. List ranges

Check that `rangectl` can find your ranges:

```bash
./rangectl list
```

The output should include `example` and any discovered content-pack ranges.

## 5. Validate the range

Run the offline checks first:

```bash
./rangectl validate example
```

Then run the full local preflight:

```bash
./rangectl doctor example
```

For a real provider-backed range, use `--online` after `.rangectl.env` is ready:

```bash
./rangectl doctor example --online
```

Resolve every `error` before continuing. Review each warning against the range's
operator guide before applying infrastructure.

## 6. Check templates

For Proxmox ranges, check template names before you deploy:

```bash
./rangectl templates list
./rangectl templates check example
```

If a logical template name is missing, add it to a content-pack catalog or
override the public mapping. Public catalog examples live under `templates/`.

## 7. Review the plan

Print the high-level plan:

```bash
./rangectl plan example
```

For provider-backed ranges, also review the infrastructure plan:

```bash
./rangectl infra plan example
```

`plan` prints the orchestration steps without calling providers or Ansible.
`infra plan` asks the configured lifecycle tools, such as Terraform/OpenTofu,
for their proposed infrastructure changes.

## 8. Review configuration phases

List the Ansible phases:

```bash
./rangectl phases example
```

Phases marked `manual` do not run during `up`. The range's operator guide should
state when to run each manual phase:

```bash
./rangectl configure example --phase phase-name
```

## 9. Deploy the range

After resolving errors and reviewing both plans, create or update the range:

```bash
./rangectl up example
```

`up` renders generated files, applies infrastructure, and runs non-manual
configuration phases.

If the provider plan would destroy or replace existing resources, `up` stops.
Only continue with `--allow-replace` when you have reviewed the plan and intend
to replace those resources:

```bash
./rangectl up example --allow-replace
```

## 10. Check status

After the run, check local state:

```bash
./rangectl status example
```

`status` shows the rendered state, host count, collected activity logs, and
latest run record when each item exists. It reports local state; it does not
probe every guest.

## 11. Run one configuration phase

Repeat one part of the configuration without applying infrastructure:

```bash
./rangectl configure example --phase services
```

Use `--dry-run` to print the Ansible command without running it:

```bash
./rangectl configure example --phase services --dry-run
```

## 12. Tear down the range

Destroy range infrastructure with an explicit approval flag:

```bash
./rangectl destroy example --auto-approve
```

`down` is the same operation:

```bash
./rangectl down example --auto-approve
```

To destroy only provider resources that support host targeting:

```bash
./rangectl destroy example --hosts host1,host2 --auto-approve
```

If a failed or interrupted provider operation leaves state that cannot refresh,
retry teardown with `--no-refresh`. Review the provider state before bypassing
refresh so that the destroy plan still targets the intended resources.
