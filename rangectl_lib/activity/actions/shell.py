from __future__ import annotations

import platform
import random
import shlex
from typing import Any, Callable

from rangectl_lib.process import run_bounded


def register(add: Callable[[str, Any], None]) -> None:
    add("shell.run", run)


def run(context: Any, action: Any) -> dict[str, Any]:
    commands = action.args.get("commands") or action.args.get("command")
    if isinstance(commands, str):
        commands = [commands]
    if not commands:
        return {"status": "skipped", "reason": "no commands configured"}
    command = str(random.choice(commands))
    if context.dry_run:
        return {"command": command}
    use_shell = action.args.get("shell") is True
    argv = command if use_shell else shlex.split(command)
    completed = run_bounded(argv, shell=use_shell, timeout=int(action.args.get("timeout", 30)))
    return {
        "command": command,
        "shell": use_shell,
        "platform": platform.system(),
        "returncode": completed["returncode"],
        "stdout": completed["stdout"],
        "stderr": completed["stderr"],
    }
