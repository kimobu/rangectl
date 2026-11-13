# Built-in playbooks

These Ansible playbooks compose reusable roles. They read host and range
variables from the generated inventory and, where required, data from a content
pack.

Keep course-specific scenario setup, instructor-only delivery, actor payloads,
and branded lab orchestration in the content pack's `playbooks/` directory.

AD playbooks expect `domain_name` and an AD data tree at
`{{ rangectl_range_base }}/ad/{{ domain_name }}/data`. Set
`rangectl_ad_data_path` to use a different data directory.
