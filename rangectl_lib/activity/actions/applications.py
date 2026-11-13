from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable


MACOS_APPS = [
    "Calendar",
    "Notes",
    "Reminders",
    "Contacts",
    "Calculator",
    "Preview",
    "TextEdit",
    "Dictionary",
    "System Settings",
    "Maps",
]

MACOS_ALWAYS_PROTECTED_APPS = [
    "Finder",
    "System Settings",
    "System Preferences",
    "SecurityAgent",
    "Python",
    "Python Launcher",
    "osascript",
    "Terminal",
]

MACOS_ALWAYS_CLOSE_APPS = [
    "Install Command Line Developer Tools",
]

WINDOWS_APPS = [
    "notepad.exe",
    "calc.exe",
    "mspaint.exe",
]

MACOS_APP_BUNDLE_IDS = {
    "Brave Browser": "com.brave.Browser",
    "Discord": "com.hnc.Discord",
    "Docker": "com.docker.docker",
    "Google Chrome": "com.google.Chrome",
    "Microsoft Edge": "com.microsoft.edgemac",
    "Microsoft Excel": "com.microsoft.Excel",
    "Microsoft Outlook": "com.microsoft.Outlook",
    "Microsoft Word": "com.microsoft.Word",
    "Slack": "com.tinyspeck.slackmacgap",
    "Telegram": "org.telegram.desktop",
    "Visual Studio Code": "com.microsoft.VSCode",
    "Zoom": "us.zoom.xos",
}

MACOS_APP_PATHS = {
    "Brave Browser": "/Applications/Brave Browser.app",
    "Discord": "/Applications/Discord.app",
    "Docker": "/Applications/Docker.app",
    "Google Chrome": "/Applications/Google Chrome.app",
    "iTerm": "/Applications/iTerm.app",
    "iTerm2": "/Applications/iTerm.app",
    "Microsoft Edge": "/Applications/Microsoft Edge.app",
    "Microsoft Excel": "/Applications/Microsoft Excel.app",
    "Microsoft Outlook": "/Applications/Microsoft Outlook.app",
    "Microsoft Word": "/Applications/Microsoft Word.app",
    "Slack": "/Applications/Slack.app",
    "Telegram": "/Applications/Telegram.app",
    "Visual Studio Code": "/Applications/Visual Studio Code.app",
    "Zoom": "/Applications/zoom.us.app",
}


def register(add: Callable[[str, Any], None]) -> None:
    add("application.open_close", open_close)
    add("application.limit_open", limit_open)
    add("application.quit_excess", limit_open)


def open_close(context: Any, action: Any) -> dict[str, Any]:
    apps = action.args.get("apps")
    if isinstance(apps, str):
        apps = [apps]
    if not apps:
        apps = _default_apps()
    if not apps:
        return {"status": "skipped", "reason": f"unsupported platform {sys.platform}"}
    app = str(random.choice(apps))
    close_probability = int(action.args.get("close_probability", 70))
    dwell_seconds = _dwell_seconds(action)
    should_close = random.randint(1, 100) <= close_probability
    if context.dry_run:
        return {"app": app, "dwell_seconds": dwell_seconds, "close": should_close}
    result = _open_app(app)
    if not result.get("opened"):
        return {"app": app, **result}
    if dwell_seconds > 0:
        time.sleep(dwell_seconds)
    if should_close:
        result["closed"] = _close_app(app)
    else:
        result["closed"] = False
    return {"app": app, "dwell_seconds": dwell_seconds, **result}


def limit_open(context: Any, action: Any) -> dict[str, Any]:
    max_apps = int(action.args.get("max_apps", 5))
    if max_apps <= 0:
        return {"status": "skipped", "reason": "max_apps must be positive"}
    if sys.platform != "darwin":
        return {"status": "skipped", "reason": f"unsupported platform {sys.platform}"}

    preferred_apps = _string_list(action.args.get("preferred_apps") or action.args.get("keep_apps"))
    protected_apps = _string_list(action.args.get("protected_apps"))
    avoid_apps = _string_list(action.args.get("avoid_apps"))
    protected = _normalized_set(MACOS_ALWAYS_PROTECTED_APPS + protected_apps)
    avoid = _normalized_set(MACOS_ALWAYS_CLOSE_APPS + avoid_apps)
    running = _running_macos_apps()
    if not running.get("ok"):
        return running

    ordered_running = [app for app in running["apps"] if _normalize_app(app) not in protected]
    always_quit = [app for app in ordered_running if _normalize_app(app) in avoid]
    eligible_running = [app for app in ordered_running if _normalize_app(app) not in avoid]
    preferred = _normalized_set(preferred_apps)
    keep: list[str] = []
    for app in eligible_running:
        if _normalize_app(app) in preferred and len(keep) < max_apps:
            keep.append(app)
    for app in eligible_running:
        if app not in keep and len(keep) < max_apps:
            keep.append(app)

    keep_normalized = _normalized_set(keep)
    quit_apps = always_quit + [
        app
        for app in eligible_running
        if _normalize_app(app) not in keep_normalized
    ]
    graceful = bool(action.args.get("graceful", True))
    force = bool(action.args.get("force", False))
    if context.dry_run:
        return {
            "max_apps": max_apps,
            "running_apps": running["apps"],
            "keep_apps": keep,
            "quit_apps": quit_apps,
            "dry_run": True,
        }

    closed: list[str] = []
    failed: list[str] = []
    for app in quit_apps:
        ok = _quit_macos_app(app) if graceful else False
        if not ok and force:
            ok = _pkill_app(app)
        if ok:
            closed.append(app)
        else:
            failed.append(app)
    return {
        "max_apps": max_apps,
        "running_count": len(running["apps"]),
        "keep_apps": keep,
        "closed_apps": closed,
        "failed_apps": failed,
    }


def _default_apps() -> list[str]:
    if sys.platform == "darwin":
        return MACOS_APPS
    if sys.platform.startswith("win"):
        return WINDOWS_APPS
    return []


def _dwell_seconds(action: Any) -> int:
    minimum = int(action.args.get("min_dwell_seconds", 8))
    maximum = int(action.args.get("max_dwell_seconds", 45))
    if maximum < minimum:
        maximum = minimum
    return random.randint(minimum, maximum)


def _open_app(app: str) -> dict[str, Any]:
    if sys.platform == "darwin":
        result = _run_open(["open", "-a", app], "macos")
        if result.get("opened"):
            return result
        fallback = _open_macos_app_fallback(app)
        if fallback.get("opened"):
            return fallback
        return result
    if sys.platform.startswith("win"):
        try:
            process = subprocess.Popen([app])
        except OSError as exc:
            return {"opened": False, "platform": "windows", "error": str(exc)}
        return {"opened": True, "platform": "windows", "pid": process.pid}
    return {"opened": False, "platform": sys.platform, "error": "unsupported platform"}


def _open_macos_app_fallback(app: str) -> dict[str, Any]:
    bundle_id = MACOS_APP_BUNDLE_IDS.get(app)
    if bundle_id:
        result = _run_open(["open", "-b", bundle_id], "macos")
        if result.get("opened"):
            result["method"] = "bundle_id"
            return result
    app_path = MACOS_APP_PATHS.get(app)
    if app_path and Path(app_path).exists():
        result = _run_open(["open", "-a", app_path], "macos")
        if result.get("opened"):
            result["method"] = "path"
            return result
        return result
    return {"opened": False, "platform": "macos", "error": f"Unable to find application named '{app}'"}


def _close_app(app: str) -> bool:
    if sys.platform == "darwin":
        if _quit_macos_app(app):
            return True
        return _pkill_app(app)
    if sys.platform.startswith("win"):
        return _run_close(["taskkill", "/IM", app, "/T", "/F"])
    return False


def _quit_macos_app(app: str) -> bool:
    script = f"tell application {_applescript_quote(app)} to quit"
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _pkill_app(app: str) -> bool:
    return _run_close(["pkill", "-x", app])


def _run_open(command: list[str], platform: str) -> dict[str, Any]:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"opened": False, "platform": platform, "error": str(exc)}
    detail: dict[str, Any] = {"opened": result.returncode == 0, "platform": platform}
    if result.returncode != 0:
        detail["error"] = (result.stderr or result.stdout).strip()
    return detail


def _run_close(command: list[str]) -> bool:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _running_macos_apps() -> dict[str, Any]:
    script = """
tell application "System Events"
    set appNames to name of application processes whose background only is false
end tell
set AppleScript's text item delimiters to linefeed
return appNames as text
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    apps = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"ok": True, "apps": apps}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _normalized_set(values: list[str]) -> set[str]:
    return {_normalize_app(value) for value in values}


def _normalize_app(value: str) -> str:
    return value.strip().casefold()


def _applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
