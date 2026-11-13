# Templates

This directory contains public examples of provider template catalogs and
template build definitions.

Range files should reference logical template names. Provider renderers map
those logical names to provider-specific template IDs through catalogs such as
`proxmox.example.yml`.

Template build definitions describe how to create base images before a catalog
references them. The checked-in definitions are examples. Store installation
media paths, checksums, product choices, and installation-specific VMIDs in a
private content pack.

Useful commands:

```bash
./rangectl templates list
./rangectl templates check example
./rangectl templates doctor proxmox
```

- `templates list` shows the resolved catalog.
- `templates check` rejects unknown logical names in a range.
- `templates doctor proxmox` checks build prerequisites before a Proxmox
  template build.
