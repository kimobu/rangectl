from __future__ import annotations

import random
import subprocess
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable


_MACOS_BROWSER_BUNDLE_IDS = {
    "Google Chrome": "com.google.Chrome",
    "Microsoft Edge": "com.microsoft.edgemac",
    "Brave Browser": "com.brave.Browser",
}

_MACOS_BROWSER_PATHS = {
    "Google Chrome": "/Applications/Google Chrome.app",
    "Microsoft Edge": "/Applications/Microsoft Edge.app",
    "Brave Browser": "/Applications/Brave Browser.app",
}


def register(add: Callable[[str, Any], None]) -> None:
    add("browser.visit", visit)
    add("browser.download", download)
    add("browser.form_submit", form_submit)
    add("browser.limit_tabs", limit_tabs)
    add("browser.close_excess_tabs", limit_tabs)
    add("browser.close_tabs", close_tabs)


def visit(context: Any, action: Any) -> dict[str, Any]:
    urls = action.args.get("urls") or action.args.get("url")
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return {"status": "skipped", "reason": "no urls configured"}
    url = str(random.choice(urls))
    headless = bool(action.args.get("headless", False))
    if context.dry_run:
        return {"url": url, "headless": headless}
    if headless:
        request = urllib.request.Request(url, headers={"User-Agent": _user_agent(context)})
        try:
            with urllib.request.urlopen(request, timeout=int(action.args.get("timeout", 20))) as response:
                return {"url": url, "status_code": response.status, "bytes": len(response.read(65536))}
        except urllib.error.URLError as exc:
            return {"url": url, "error": str(exc)}
    return {"url": url, **_open_visible_url(context, action, url)}


def download(context: Any, action: Any) -> dict[str, Any]:
    urls = action.args.get("urls") or action.args.get("url")
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return {"status": "skipped", "reason": "no urls configured"}
    url = str(random.choice(urls))
    directory = Path(str(action.args.get("directory", "~/Downloads"))).expanduser()
    filename = str(action.args.get("filename") or url.rstrip("/").rsplit("/", 1)[-1] or "download.bin")
    target = directory / filename
    if context.dry_run:
        return {"url": url, "target": str(target)}
    directory.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": _user_agent(context)})
    with urllib.request.urlopen(request, timeout=int(action.args.get("timeout", 30))) as response:
        target.write_bytes(response.read())
    return {"url": url, "target": str(target), "bytes": target.stat().st_size}


def form_submit(context: Any, action: Any) -> dict[str, Any]:
    url = str(action.args.get("url", ""))
    if not url:
        return {"status": "skipped", "reason": "no form url configured"}
    fields = dict(action.args.get("fields") or {})
    rendered_fields = {
        str(key): _render_field(str(value), context)
        for key, value in fields.items()
    }
    method = str(action.args.get("method", "POST")).upper()
    open_first = bool(action.args.get("open_first", True))
    if context.dry_run:
        return {"url": url, "method": method, "fields": sorted(rendered_fields)}
    if open_first:
        _open_visible_url(context, action, url)
    encoded = urllib.parse.urlencode(rendered_fields).encode("utf-8")
    request_url = url
    data = encoded
    if method == "GET":
        separator = "&" if "?" in url else "?"
        request_url = f"{url}{separator}{encoded.decode('utf-8')}"
        data = None
    request = urllib.request.Request(request_url, data=data, headers={"User-Agent": _user_agent(context)}, method=method)
    with urllib.request.urlopen(request, timeout=int(action.args.get("timeout", 30))) as response:
        response.read(65536)
        return {"url": url, "method": method, "status_code": response.status, "fields": sorted(rendered_fields)}


def limit_tabs(context: Any, action: Any) -> dict[str, Any]:
    max_tabs = int(action.args.get("max_tabs", 10))
    if max_tabs <= 0:
        return {"status": "skipped", "reason": "max_tabs must be positive"}
    if sys.platform != "darwin":
        return {"status": "skipped", "reason": f"unsupported platform {sys.platform}"}
    browsers = action.args.get("browsers") or action.args.get("browser")
    if isinstance(browsers, str):
        browsers = [browsers]
    if not browsers:
        browsers = ["Safari", "Google Chrome", "Microsoft Edge", "Brave Browser"]
    if context.dry_run:
        return {"browsers": [str(browser) for browser in browsers], "max_tabs": max_tabs, "dry_run": True}
    results = [
        _limit_macos_browser_tabs(
            _canonical_macos_browser(str(browser)),
            max_tabs,
            fallback_close_count=int(action.args.get("fallback_close_count", 0)),
        )
        for browser in browsers
    ]
    return {"max_tabs": max_tabs, "browsers": results}


def close_tabs(context: Any, action: Any) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"status": "skipped", "reason": f"unsupported platform {sys.platform}"}
    browser_name = _canonical_macos_browser(str(action.args.get("browser", "Safari")))
    count = int(action.args.get("count", 1))
    if count <= 0:
        return {"status": "skipped", "reason": "count must be positive"}
    if context.dry_run:
        return {"browser": browser_name, "closed_tabs_requested": count, "dry_run": True}
    return _close_browser_tabs_with_gui(browser_name, count)


def _user_agent(context: Any) -> str:
    return f"rangectl-activity/{context.config.range_name} ({context.config.persona.role})"


def _open_visible_url(context: Any, action: Any, url: str) -> dict[str, Any]:
    reuse_tab = bool(action.args.get("reuse_tab", True))
    browser_name = str(action.args.get("browser", "")).strip()
    browser = browser_name.lower()
    if sys.platform == "darwin" and browser not in {"", "safari"}:
        return _open_macos_browser(browser_name, url, reuse_tab, int(action.args.get("max_tabs", 10)))
    if sys.platform == "darwin" and browser in {"", "safari"} and reuse_tab:
        safari_result = _open_safari_reused_tab(url, int(action.args.get("max_tabs", 4)))
        if safari_result.get("opened"):
            return safari_result
        fallback = _open_safari_with_periodic_reset(context, action, url)
        fallback["safari_reuse_error"] = safari_result.get("error", "")
        return fallback
    opened = webbrowser.open(url, new=0 if reuse_tab else 2)
    return {"opened": opened, "browser": "system", "reuse_tab": reuse_tab}


def _open_macos_browser(browser_name: str, url: str, reuse_tab: bool, max_tabs: int) -> dict[str, Any]:
    browser_name = _canonical_macos_browser(browser_name)
    try:
        result = subprocess.run(
            ["open", "-a", browser_name, url],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"opened": False, "browser": browser_name, "reuse_tab": reuse_tab, "error": str(exc)}
    if result.returncode != 0:
        fallback_result = _open_macos_browser_fallback(browser_name, url)
        if fallback_result.returncode == 0:
            result = fallback_result
    detail: dict[str, Any] = {
        "opened": result.returncode == 0,
        "browser": browser_name,
        "reuse_tab": reuse_tab,
    }
    if result.returncode != 0:
        detail["error"] = (result.stderr or result.stdout).strip()
    elif max_tabs > 0:
        detail["tab_limit"] = _limit_macos_browser_tabs(browser_name, max_tabs)
    return detail


def _open_macos_browser_fallback(browser_name: str, url: str) -> subprocess.CompletedProcess[str]:
    bundle_id = _MACOS_BROWSER_BUNDLE_IDS.get(browser_name)
    if bundle_id:
        args = ["open", "-b", bundle_id]
        if url:
            args.append(url)
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return result
        except (OSError, subprocess.TimeoutExpired):
            pass
    app_path = _MACOS_BROWSER_PATHS.get(browser_name)
    if app_path and Path(app_path).exists():
        args = ["open", "-a", app_path]
        if url:
            args.append(url)
        try:
            return subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(args, 1, "", str(exc))
    return subprocess.CompletedProcess(["open", "-a", browser_name], 1, "", f"Unable to find application named '{browser_name}'")


def _open_safari_with_periodic_reset(context: Any, action: Any, url: str) -> dict[str, Any]:
    reset = _reset_safari_if_needed(context, action)
    try:
        result = subprocess.run(
            ["open", "-a", "Safari", url],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"opened": False, "browser": "safari", "reuse_tab": False, "safari_reset": reset, "error": str(exc)}
    detail: dict[str, Any] = {
        "opened": result.returncode == 0,
        "browser": "safari",
        "reuse_tab": False,
        "safari_reset": reset,
    }
    if result.returncode != 0:
        detail["error"] = (result.stderr or result.stdout).strip()
    return detail


def _reset_safari_if_needed(context: Any, action: Any) -> bool:
    reset_after = int(action.args.get("safari_reset_after_visits", action.args.get("max_tabs", 4)))
    if reset_after <= 0:
        return False
    counter_path = Path(context.state_dir) / "browser-visits.count"
    try:
        count = int(counter_path.read_text(encoding="utf-8").strip() or "0") + 1
    except (OSError, ValueError):
        count = 1
    try:
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        counter_path.write_text(f"{count}\n", encoding="utf-8")
    except OSError:
        pass
    if count <= 1 or (count - 1) % reset_after != 0:
        return False
    try:
        subprocess.run(["pkill", "-x", "Safari"], check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _open_safari_reused_tab(url: str, max_tabs: int) -> dict[str, Any]:
    cull_script = ""
    if max_tabs > 0:
        cull_script = """
        repeat with browserWindow in windows
            set tabCount to count of tabs of browserWindow
            if tabCount > maxTabs then
                repeat with tabIndex from tabCount to (maxTabs + 1) by -1
                    try
                        close tab tabIndex of browserWindow
                    end try
                end repeat
            end if
        end repeat
"""
    script = f"""
on run argv
    set targetUrl to item 1 of argv
    set maxTabs to (item 2 of argv) as integer
    tell application "Safari"
        if (count of windows) = 0 then
            make new document with properties {{URL:targetUrl}}
        else
            set targetWindow to front window
            set URL of current tab of targetWindow to targetUrl
        end if
{cull_script}
    end tell
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, url, str(max_tabs)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"opened": False, "browser": "safari", "error": str(exc)}
    detail: dict[str, Any] = {
        "opened": result.returncode == 0,
        "browser": "safari",
        "reuse_tab": True,
        "max_tabs": max_tabs,
    }
    if result.returncode != 0:
        detail["error"] = (result.stderr or result.stdout).strip()
    return detail


def _limit_macos_browser_tabs(
    browser_name: str,
    max_tabs: int,
    *,
    fallback_close_count: int = 0,
) -> dict[str, Any]:
    browser_name = _canonical_macos_browser(browser_name)
    if browser_name == "Safari":
        result = _limit_safari_tabs(max_tabs)
        return _apply_tab_limit_fallback(browser_name, result, fallback_close_count)
    if browser_name in {"Google Chrome", "Microsoft Edge", "Brave Browser"}:
        result = _limit_chromium_tabs(browser_name, max_tabs)
        return _apply_tab_limit_fallback(browser_name, result, fallback_close_count)
    return {"browser": browser_name, "status": "skipped", "reason": "unsupported browser"}


def _limit_safari_tabs(max_tabs: int) -> dict[str, Any]:
    script = """
on run argv
    set maxTabs to (item 1 of argv) as integer
    tell application "System Events" to set appRunning to exists process "Safari"
    if appRunning is false then return "0,0"
    set closedCount to 0
    tell application "Safari"
        set totalTabs to 0
        repeat with browserWindow in windows
            set totalTabs to totalTabs + (count of tabs of browserWindow)
        end repeat
        repeat with windowIndex from (count of windows) to 1 by -1
            if totalTabs <= maxTabs then exit repeat
            set browserWindow to window windowIndex
            repeat while totalTabs > maxTabs and (count of tabs of browserWindow) > 1
                try
                    close tab (count of tabs of browserWindow) of browserWindow
                    set closedCount to closedCount + 1
                    set totalTabs to totalTabs - 1
                end try
            end repeat
            if totalTabs > maxTabs and (count of windows) > 1 and (count of tabs of browserWindow) = 1 then
                try
                    close browserWindow
                    set closedCount to closedCount + 1
                    set totalTabs to totalTabs - 1
                end try
            end if
        end repeat
        return (closedCount as text) & "," & (totalTabs as text)
    end tell
end run
"""
    return _run_tab_limit_script("Safari", script, max_tabs)


def _limit_chromium_tabs(browser_name: str, max_tabs: int) -> dict[str, Any]:
    app_target = f'id "{_MACOS_BROWSER_BUNDLE_IDS[browser_name]}"'
    script = f"""
on run argv
    set maxTabs to (item 1 of argv) as integer
    tell application "System Events" to set appRunning to exists process "{browser_name}"
    if appRunning is false then return "0,0"
    set closedCount to 0
    tell application {app_target}
        set totalTabs to 0
        repeat with browserWindow in windows
            set totalTabs to totalTabs + (count of tabs of browserWindow)
        end repeat
        repeat with windowIndex from (count of windows) to 1 by -1
            if totalTabs <= maxTabs then exit repeat
            set browserWindow to window windowIndex
            repeat while totalTabs > maxTabs and (count of tabs of browserWindow) > 1
                try
                    close tab (count of tabs of browserWindow) of browserWindow
                    set closedCount to closedCount + 1
                    set totalTabs to totalTabs - 1
                end try
            end repeat
            if totalTabs > maxTabs and (count of windows) > 1 and (count of tabs of browserWindow) = 1 then
                try
                    close browserWindow
                    set closedCount to closedCount + 1
                    set totalTabs to totalTabs - 1
                end try
            end if
        end repeat
        return (closedCount as text) & "," & (totalTabs as text)
    end tell
end run
"""
    return _run_tab_limit_script(browser_name, script, max_tabs)


def _run_tab_limit_script(browser_name: str, script: str, max_tabs: int) -> dict[str, Any]:
    args = ["osascript", "-e", script, str(max_tabs)]
    last_error = ""
    for attempt in range(3):
        try:
            result = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"browser": browser_name, "max_tabs": max_tabs, "closed_tabs": 0, "error": str(exc)}
        if result.returncode == 0:
            closed, total = _parse_tab_limit_output(result.stdout)
            return {
                "browser": browser_name,
                "max_tabs": max_tabs,
                "closed_tabs": closed,
                "remaining_tabs": total,
            }
        last_error = (result.stderr or result.stdout).strip()
        if attempt < 2:
            time.sleep(1)
    return {"browser": browser_name, "max_tabs": max_tabs, "closed_tabs": 0, "error": last_error}


def _parse_tab_limit_output(output: str) -> tuple[int, int]:
    text = output.strip()
    closed_text, _, total_text = text.partition(",")
    try:
        closed = int(closed_text.strip() or "0")
    except ValueError:
        closed = 0
    try:
        total = int(total_text.strip() or "0")
    except ValueError:
        total = 0
    return closed, total


def _canonical_macos_browser(browser_name: str) -> str:
    normalized = browser_name.strip().casefold()
    aliases = {
        "": "Safari",
        "safari": "Safari",
        "chrome": "Google Chrome",
        "google chrome": "Google Chrome",
        "edge": "Microsoft Edge",
        "microsoft edge": "Microsoft Edge",
        "brave": "Brave Browser",
        "brave browser": "Brave Browser",
    }
    return aliases.get(normalized, browser_name.strip())


def _apply_tab_limit_fallback(
    browser_name: str,
    result: dict[str, Any],
    fallback_close_count: int,
) -> dict[str, Any]:
    if fallback_close_count <= 0 or "error" not in result:
        return result
    fallback = _close_browser_tabs_with_gui(browser_name, fallback_close_count)
    result["fallback"] = fallback
    return result


def _close_browser_tabs_with_gui(browser_name: str, count: int) -> dict[str, Any]:
    try:
        open_result = subprocess.run(
            ["open", "-a", browser_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"browser": browser_name, "closed_tabs_requested": count, "error": str(exc)}
    if open_result.returncode != 0:
        fallback_result = _open_macos_browser_fallback(browser_name, "")
        if fallback_result.returncode != 0:
            return {
                "browser": browser_name,
                "closed_tabs_requested": count,
                "error": (open_result.stderr or open_result.stdout or fallback_result.stderr or fallback_result.stdout).strip(),
            }
    time.sleep(0.5)
    if _pyautogui_close_tabs(count):
        return {"browser": browser_name, "closed_tabs_requested": count, "method": "pyautogui"}
    if _system_events_close_tabs(count):
        return {"browser": browser_name, "closed_tabs_requested": count, "method": "system_events"}
    return {"browser": browser_name, "closed_tabs_requested": count, "error": "failed to send close-tab events"}


def _pyautogui_close_tabs(count: int) -> bool:
    try:
        import pyautogui  # type: ignore[import-not-found]
    except Exception:
        return False
    try:
        pyautogui.PAUSE = 0.05
        for _ in range(count):
            pyautogui.hotkey("command", "w")
            time.sleep(0.05)
    except Exception:
        return False
    return True


def _system_events_close_tabs(count: int) -> bool:
    script = """
on run argv
    set closeCount to (item 1 of argv) as integer
    tell application "System Events"
        repeat closeCount times
            keystroke "w" using command down
            delay 0.05
        end repeat
    end tell
end run
"""
    try:
        result = subprocess.run(
            ["osascript", "-e", script, str(count)],
            check=False,
            capture_output=True,
            text=True,
            timeout=max(5, count),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _render_field(value: str, context: Any) -> str:
    persona = context.config.persona
    replacements = {
        "{username}": persona.username,
        "{email}": persona.email.address,
        "{display_name}": persona.display_name or persona.username,
        "{password}": str(persona.email.settings.get("password", "Password123!")),
    }
    for token, replacement in replacements.items():
        value = value.replace(token, replacement)
    return value
