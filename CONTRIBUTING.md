# Contributing

Keep changes focused on the public orchestration engine. Private ranges,
credentials, generated state, course artifacts, and operator-specific values
belong in content packs or ignored local files.

## Local checks

Run these checks from the repository root:

```bash
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q cmd rangectl_lib
./rangectl validate example
git diff --check
```

For changes to an Ansible role or playbook, also run a syntax check using an
inventory that exercises the affected hosts.

## Pull requests

Explain the user-visible behavior, the lifecycle owner involved, and the checks
you ran. Keep generated files, credentials, binaries, and local `state/`
outputs out of the commit.
