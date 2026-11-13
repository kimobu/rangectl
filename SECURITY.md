# Security policy

## Supported version

Security fixes are applied to the current `main` branch. This project does not
currently maintain older release branches.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature when it is available. If
the repository does not show a **Report a vulnerability** button, open a public
issue asking the maintainer for a private contact channel. Do not include an
unpublished exploit, credential, private range definition, or operator data in
that issue.

Include the affected command or component, the conditions required to trigger
the problem, and a minimal reproduction that does not contain real lab secrets.

## Lab safety

`rangectl` manages intentionally vulnerable training environments. Deploy
ranges only on networks designed for isolated security training. Do not point
scenario callbacks, payload infrastructure, or simulated activity at systems
outside the authorized lab.
