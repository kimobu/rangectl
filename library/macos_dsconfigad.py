#!/usr/bin/python

from __future__ import annotations

from ansible.module_utils.basic import AnsibleModule


def run_command(module: AnsibleModule, args: list[str], check_rc: bool = True) -> tuple[int, str, str]:
    rc, out, err = module.run_command(args)
    if check_rc and rc != 0:
        module.fail_json(msg=f"command failed: {' '.join(args)}", rc=rc, stdout=out, stderr=err)
    return rc, out, err


def current_binding(module: AnsibleModule, dsconfigad_path: str) -> dict[str, str] | None:
    rc, out, _ = module.run_command([dsconfigad_path, "-show"])
    if rc != 0:
        return None

    data: dict[str, str] = {}
    for line in out.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    if not data.get("Active Directory Domain"):
        return None
    return data


def ensure_present(module: AnsibleModule, dsconfigad_path: str) -> None:
    params = module.params
    bound = current_binding(module, dsconfigad_path)
    desired_domain = params["domain"].lower()
    desired_computer = params["computer_id"]
    desired_preferred = params["preferred_dc"] or ""

    if bound is not None:
        current_domain = bound.get("Active Directory Domain", "").lower()
        current_computer = bound.get("Computer Account", "").rstrip("$")
        current_preferred = bound.get("Preferred Domain controller", bound.get("Preferred Domain server", ""))
        if (
            current_domain == desired_domain
            and current_computer == desired_computer
            and (not desired_preferred or current_preferred == desired_preferred)
        ):
            module.exit_json(changed=False, binding=bound)

    add_cmd = [
        dsconfigad_path,
        "-add",
        params["domain"],
        "-computer",
        desired_computer,
        "-username",
        params["username"],
        "-password",
        params["password"],
    ]
    if params["ou"]:
        add_cmd.extend(["-ou", params["ou"]])
    if params["mobile"] is not None:
        add_cmd.extend(["-mobile", "enable" if params["mobile"] else "disable"])
    if params["mobileconfirm"] is not None:
        add_cmd.extend(["-mobileconfirm", "enable" if params["mobileconfirm"] else "disable"])
    if params["localhome"] is not None:
        add_cmd.extend(["-localhome", "enable" if params["localhome"] else "disable"])
    if params["use_unc_path"] is not None:
        add_cmd.extend(["-useuncpath", "enable" if params["use_unc_path"] else "disable"])
    if params["preferred_dc"]:
        add_cmd.extend(["-preferred", params["preferred_dc"]])

    if module.check_mode:
        module.exit_json(changed=True, planned_command=add_cmd)

    if bound is not None:
        run_command(
            module,
            [
                dsconfigad_path,
                "-remove",
                "-force",
                "-u",
                params["username"],
                "-p",
                params["password"],
            ],
        )

    run_command(module, add_cmd)
    module.exit_json(changed=True, binding=current_binding(module, dsconfigad_path))


def ensure_absent(module: AnsibleModule, dsconfigad_path: str) -> None:
    bound = current_binding(module, dsconfigad_path)
    if bound is None:
        module.exit_json(changed=False)

    if module.check_mode:
        module.exit_json(changed=True)

    run_command(
        module,
        [
            dsconfigad_path,
            "-remove",
            "-force",
            "-u",
            module.params["username"],
            "-p",
            module.params["password"],
        ],
    )
    module.exit_json(changed=True)


def main() -> None:
    module = AnsibleModule(
        argument_spec={
            "state": {"type": "str", "choices": ["present", "absent"], "default": "present"},
            "domain": {"type": "str", "required": True},
            "username": {"type": "str", "required": True, "no_log": False},
            "password": {"type": "str", "required": True, "no_log": True},
            "computer_id": {"type": "str", "required": True},
            "ou": {"type": "str", "default": ""},
            "preferred_dc": {"type": "str", "default": ""},
            "mobile": {"type": "bool", "default": True},
            "mobileconfirm": {"type": "bool", "default": False},
            "localhome": {"type": "bool", "default": True},
            "use_unc_path": {"type": "bool", "default": False},
        },
        supports_check_mode=True,
    )

    dsconfigad_path = module.get_bin_path("dsconfigad", required=True)
    if module.params["state"] == "present":
        ensure_present(module, dsconfigad_path)
    ensure_absent(module, dsconfigad_path)


if __name__ == "__main__":
    main()
