# Ansible roles

This directory contains reusable guest-configuration roles for services,
identity infrastructure, endpoints, telemetry, activity simulation, and lab
scenarios.

Roles should read host and range settings from the generated inventory through
`rangectl_*` variables. Course-specific values and scenario mappings belong in
the range definition or content pack, not in role tasks.

Keep each role rerunnable. Put configurable values in `defaults/main.yml`, keep
task behavior in `tasks/`, and use handlers for service restarts when
appropriate. A content pack can provide additional roles in its own `roles/`
directory; `rangectl` adds that directory to the Ansible roles path.
