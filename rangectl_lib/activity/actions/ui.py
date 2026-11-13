from __future__ import annotations

import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_DISMISS_LABELS = [
    "OK",
    "Ok",
    "Close",
    "Dismiss",
    "Not Now",
    "Later",
    "Open",
    "Allow",
    "Continue",
    "Install",
    "Install...",
    "Done",
]


def register(add: Callable[[str, Any], None]) -> None:
    add("ui.click", click)
    add("ui.click_button", click_button)
    add("ui.hotkey", hotkey)
    add("ui.press", press)
    add("ui.sequence", sequence)
    add("ui.type_text", type_text)
    add("ui.dismiss_popups", dismiss_popups)
    add("ui.install_profile", install_profile)
    add("ui.clear_quarantine", clear_quarantine)
    add("macos.ui.click", click)
    add("macos.ui.click_button", click_button)
    add("macos.ui.hotkey", hotkey)
    add("macos.ui.press", press)
    add("macos.ui.sequence", sequence)
    add("macos.ui.type_text", type_text)
    add("macos.ui.dismiss_popups", dismiss_popups)
    add("macos.ui.install_profile", install_profile)
    add("macos.ui.clear_quarantine", clear_quarantine)


def click(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    x = action.args.get("x")
    y = action.args.get("y")
    if x is None or y is None:
        return {"status": "skipped", "reason": "x and y are required"}
    button = str(action.args.get("button") or "left")
    clicks = int(action.args.get("clicks", 1))
    interval = float(action.args.get("interval_seconds", 0.0))
    duration = float(action.args.get("duration_seconds", 0.0))
    if context.dry_run:
        return {"x": x, "y": y, "button": button, "clicks": clicks}
    gui = _pyautogui()
    gui.click(int(x), int(y), clicks=clicks, interval=interval, button=button, duration=duration)
    return {"backend": "pyautogui", "x": int(x), "y": int(y), "button": button, "clicks": clicks}


def click_button(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    if action.args.get("x") is not None and action.args.get("y") is not None:
        return click(context, action)
    labels = _labels(action.args.get("labels") or action.args.get("label") or action.args.get("button"))
    if not labels:
        return {"status": "skipped", "reason": "no button labels configured"}
    process_name = str(action.args.get("process") or action.args.get("app") or "")
    if action.args.get("app") and bool(action.args.get("activate", True)):
        _open_app(str(action.args["app"]))
    attempts = int(action.args.get("attempts", 5))
    interval = float(action.args.get("interval_seconds", 0.8))
    if context.dry_run:
        return {"process": process_name, "labels": labels, "attempts": attempts}
    clicked = _click_labels(labels, process_name=process_name, attempts=attempts, interval=interval)
    return {"process": process_name, "labels": labels, "clicked": clicked}


def hotkey(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    keys = [str(item) for item in _as_list(action.args.get("keys") or action.args.get("key")) if str(item)]
    if not keys:
        return {"status": "skipped", "reason": "no keys configured"}
    if context.dry_run:
        return {"keys": keys}
    gui = _pyautogui()
    gui.hotkey(*keys)
    return {"backend": "pyautogui", "keys": keys}


def press(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    keys = [str(item) for item in _as_list(action.args.get("keys") or action.args.get("key")) if str(item)]
    if not keys:
        return {"status": "skipped", "reason": "no keys configured"}
    presses = int(action.args.get("presses", 1))
    interval = float(action.args.get("interval_seconds", 0.05))
    if context.dry_run:
        return {"keys": keys, "presses": presses}
    gui = _pyautogui()
    for key in keys:
        gui.press(key, presses=presses, interval=interval)
    return {"backend": "pyautogui", "keys": keys, "presses": presses}


def type_text(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    text = str(action.args.get("text") or "")
    interval = float(action.args.get("interval_seconds", 0.01))
    if context.dry_run:
        return {"chars": len(text)}
    gui = _pyautogui()
    gui.write(text, interval=interval)
    return {"backend": "pyautogui", "chars": len(text)}


def sequence(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    steps = _as_list(action.args.get("steps"))
    if not steps:
        return {"status": "skipped", "reason": "no sequence steps configured"}
    if context.dry_run:
        return {"steps": len(steps)}
    gui = _pyautogui()
    executed = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        op = str(step.get("type") or step.get("op") or "click")
        if op == "sleep":
            time.sleep(float(step.get("seconds", step.get("duration_seconds", 1))))
        elif op == "click":
            gui.click(
                int(step["x"]),
                int(step["y"]),
                clicks=int(step.get("clicks", 1)),
                interval=float(step.get("interval_seconds", 0.0)),
                button=str(step.get("button", "left")),
                duration=float(step.get("duration_seconds", 0.0)),
            )
        elif op == "move":
            gui.moveTo(int(step["x"]), int(step["y"]), duration=float(step.get("duration_seconds", 0.0)))
        elif op == "press":
            gui.press(str(step["key"]), presses=int(step.get("presses", 1)), interval=float(step.get("interval_seconds", 0.05)))
        elif op == "hotkey":
            gui.hotkey(*[str(item) for item in _as_list(step.get("keys") or step.get("key"))])
        elif op == "type_text":
            gui.write(str(step.get("text") or ""), interval=float(step.get("interval_seconds", 0.01)))
        elif op == "screenshot":
            gui.screenshot(str(Path(str(step.get("path") or "~/Desktop/rangectl-ui.png")).expanduser()))
        else:
            continue
        executed += 1
        if step.get("after_seconds") is not None:
            time.sleep(float(step["after_seconds"]))
    return {"backend": "pyautogui", "executed": executed, "steps": len(steps)}


def dismiss_popups(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    labels = _labels(action.args.get("labels") or DEFAULT_DISMISS_LABELS)
    duration = float(action.args.get("duration_seconds", 8))
    interval = float(action.args.get("interval_seconds", 0.5))
    process_name = str(action.args.get("process") or "")
    if context.dry_run:
        return {"process": process_name, "labels": labels, "duration_seconds": duration}
    deadline = time.time() + max(duration, 0)
    clicked_total = 0
    while time.time() <= deadline:
        clicked = _click_labels(labels, process_name=process_name, attempts=1, interval=0)
        clicked_total += clicked
        if clicked == 0:
            time.sleep(interval)
    return {"process": process_name, "labels": labels, "clicked": clicked_total}


def install_profile(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS UI automation only"}
    profile = Path(str(action.args.get("profile") or "")).expanduser()
    if not profile.exists():
        return {"status": "skipped", "reason": f"profile not found: {profile}"}
    password = str(action.args.get("password") or "")
    duration = float(action.args.get("duration_seconds", 45))
    labels = _labels(action.args.get("labels") or ["Continue", "Install", "Install...", "Allow", "OK", "Done"])
    if context.dry_run:
        return {"profile": str(profile), "labels": labels, "duration_seconds": duration}

    subprocess.run(["open", str(profile)], check=False, capture_output=True, text=True, timeout=10)
    time.sleep(2)
    subprocess.run(
        ["open", "x-apple.systempreferences:com.apple.Profiles-Settings.extension"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    clicked_total = 0
    password_attempts = 0
    deadline = time.time() + max(duration, 0)
    while time.time() <= deadline:
        clicked_total += _click_labels(labels, process_name="System Settings", attempts=1, interval=0)
        if password and _fill_password(password, process_name="System Settings"):
            password_attempts += 1
        clicked_total += _click_labels(labels, process_name="System Settings", attempts=1, interval=0)
        if _profile_installed(str(action.args.get("identifier") or "")):
            return {
                "profile": str(profile),
                "installed": True,
                "clicked": clicked_total,
                "password_attempts": password_attempts,
            }
        time.sleep(0.8)
    return {
        "profile": str(profile),
        "installed": _profile_installed(str(action.args.get("identifier") or "")),
        "clicked": clicked_total,
        "password_attempts": password_attempts,
    }


def clear_quarantine(context: Any, action: Any) -> dict[str, Any]:
    if platform.system() != "Darwin":
        return {"status": "skipped", "reason": "macOS quarantine is Darwin-specific"}
    paths = [Path(str(item)).expanduser() for item in _as_list(action.args.get("paths") or action.args.get("path"))]
    if not paths:
        paths = [
            Path("/Applications"),
            Path("~/Applications").expanduser(),
            Path("~/Desktop").expanduser(),
            Path("~/Downloads").expanduser(),
            Path("~/Documents").expanduser(),
        ]
    existing = [path for path in paths if path.exists()]
    if context.dry_run:
        return {"paths": [str(path) for path in existing]}
    for path in existing:
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=int(action.args.get("timeout", 60)),
        )
    return {"paths": [str(path) for path in existing], "cleared": len(existing)}


def _open_app(app: str) -> None:
    subprocess.run(["open", "-a", app], check=False, capture_output=True, text=True, timeout=10)


def _pyautogui() -> Any:
    try:
        import pyautogui
    except Exception as exc:  # pragma: no cover - depends on target host packages.
        raise RuntimeError("pyautogui is not installed for this Python") from exc
    pyautogui.FAILSAFE = False
    pyautogui.PAUSE = 0.15
    return pyautogui


def _click_labels(labels: list[str], *, process_name: str = "", attempts: int = 1, interval: float = 0.5) -> int:
    script = _click_script(labels, process_name)
    clicked = 0
    for _ in range(max(attempts, 1)):
        result = _run_osascript(script, timeout=10)
        clicked += _last_int(result.stdout)
        if clicked:
            break
        if interval > 0:
            time.sleep(interval)
    return clicked


def _fill_password(password: str, *, process_name: str = "") -> bool:
    if not password:
        return False
    script = f"""
tell application "System Events"
  set typedPassword to false
  set targetProcesses to {_process_selector(process_name)}
  repeat with procRef in targetProcesses
    try
      if "{_escape_applescript(process_name)}" is not "" then set frontmost of procRef to true
      repeat with winRef in windows of procRef
        try
          set fieldMatches to secure text fields of entire contents of winRef
          if (count of fieldMatches) > 0 then
            set focused of item 1 of fieldMatches to true
            keystroke "{_escape_applescript(password)}"
            key code 36
            set typedPassword to true
            exit repeat
          end if
        end try
      end repeat
    end try
    if typedPassword then exit repeat
  end repeat
end tell
if typedPassword then
  return "1"
else
  return "0"
end if
"""
    result = _run_osascript(script, timeout=10)
    return _last_int(result.stdout) > 0


def _profile_installed(identifier: str) -> bool:
    if not identifier:
        return False
    result = subprocess.run(
        ["/usr/bin/profiles", "show", "-type", "configuration"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return identifier in (result.stdout or "")


def _click_script(labels: list[str], process_name: str) -> str:
    return f"""
on matchesLabel(candidate, targetLabels)
  try
    set candidateText to candidate as text
  on error
    set candidateText to ""
  end try
  repeat with targetLabel in targetLabels
    if candidateText is (targetLabel as text) then return true
  end repeat
  return false
end matchesLabel

tell application "System Events"
  set clickedCount to 0
  set targetLabels to {_applescript_list(labels)}
  set targetProcesses to {_process_selector(process_name)}
  repeat with procRef in targetProcesses
    try
      if "{_escape_applescript(process_name)}" is not "" then set frontmost of procRef to true
      repeat with winRef in windows of procRef
        try
          set uiItems to entire contents of winRef
          repeat with itemRef in uiItems
            try
              set itemRole to role description of itemRef as text
            on error
              set itemRole to ""
            end try
            if itemRole contains "button" then
              set itemName to ""
              set itemDescription to ""
              try
                set itemName to name of itemRef as text
              end try
              try
                set itemDescription to description of itemRef as text
              end try
              if my matchesLabel(itemName, targetLabels) or my matchesLabel(itemDescription, targetLabels) then
                click itemRef
                set clickedCount to clickedCount + 1
                return clickedCount as text
              end if
            end if
          end repeat
        end try
      end repeat
    end try
  end repeat
end tell
return clickedCount as text
"""


def _process_selector(process_name: str) -> str:
    escaped = _escape_applescript(process_name)
    if process_name:
        return f'{{process "{escaped}"}}'
    return "(processes whose background only is false)"


def _run_osascript(script: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=["/usr/bin/osascript", "-e", script],
            returncode=124,
            stdout=exc.stdout or "",
            stderr=str(exc),
        )


def _labels(value: Any) -> list[str]:
    return [str(item) for item in _as_list(value) if str(item)]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _applescript_list(values: list[str]) -> str:
    return "{" + ", ".join(f'"{_escape_applescript(value)}"' for value in values) + "}"


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _last_int(value: str) -> int:
    for token in reversed((value or "").replace(",", " ").split()):
        try:
            return int(token)
        except ValueError:
            continue
    return 0
