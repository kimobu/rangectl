from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, time as datetime_time
from pathlib import Path
from typing import Any

from .content import ContentProvider, build_content_provider
from .models import ActionSpec, AgentConfig
from .scenario_state import (
    ScenarioStateStore,
    build_scenario_state_store,
    migrate_legacy_activity_log,
    migrate_legacy_scenario_state,
)


@dataclass(slots=True)
class ActionContext:
    config: AgentConfig
    content: ContentProvider
    state_dir: Path
    scenario_state: ScenarioStateStore
    dry_run: bool = False

    def log(self, action: ActionSpec, result: str, detail: dict[str, Any] | None = None) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "range": self.config.range_name,
            "host": self.config.host,
            "persona": self.config.persona.id,
            "user": self.config.persona.username,
            "action": action.type,
            "trackable_id": action.trackable_id,
            "result": result,
            "detail": detail or {},
        }
        content = json.dumps(record, sort_keys=True) + "\n"
        if self.scenario_state.remote:
            try:
                self.scenario_state.append_text("events.jsonl", content)
            except (OSError, RuntimeError):
                # Action logging must not interrupt user activity or cause a
                # completed scenario step to run again.
                pass
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "activity.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(content)
        print(content, end="")


ActionHandler = Any
_REGISTRY: dict[str, ActionHandler] = {}


def register_action(name: str, handler: ActionHandler) -> None:
    _REGISTRY[name] = handler


def run_agent(config: AgentConfig, *, once: bool = False, dry_run: bool = False) -> int:
    _load_builtin_actions()
    state_dir = Path(os.environ.get("RANGECTL_ACTIVITY_STATE_DIR", config.state_dir)).expanduser()
    context = ActionContext(
        config=config,
        content=build_content_provider(config.ai, state_dir),
        state_dir=state_dir,
        scenario_state=build_scenario_state_store(config, state_dir),
        dry_run=dry_run,
    )
    if context.scenario_state.remote and not dry_run:
        legacy_state_dirs = [state_dir]
        legacy_paths = os.environ.get("RANGECTL_ACTIVITY_LEGACY_STATE_DIRS", "")
        legacy_state_dirs.extend(Path(value) for value in legacy_paths.split(os.pathsep) if value)
        for legacy_state_dir in dict.fromkeys(legacy_state_dirs):
            migrate_legacy_scenario_state(legacy_state_dir, context.scenario_state)
            migrate_legacy_activity_log(legacy_state_dir, context.scenario_state)
    stop = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while not stop:
        inside_work_hours = not config.enforce_work_hours or _inside_work_hours(config)
        actions: list[ActionSpec] = []
        if inside_work_hours:
            actions.extend(_load_trigger_actions(context))
            actions.extend(_select_actions(config.actions, config.max_actions_per_cycle))
        elif once:
            context.log(ActionSpec(type="agent.schedule"), "skipped", {"reason": "outside work hours"})
        for action in actions:
            if stop:
                break
            if action.delay_before > 0 and not dry_run:
                _sleep_with_jitter(action.delay_before, _delay_jitter(config, action, "before"))
            _run_action(context, action)
            if action.delay_after > 0 and not dry_run:
                _sleep_with_jitter(action.delay_after, _delay_jitter(config, action, "after"))
        if once or not config.loop:
            return 0
        if config.cycle_delay > 0 and not dry_run:
            _sleep_with_jitter(config.cycle_delay, config.cycle_jitter)
    return 0


def _select_actions(actions: list[ActionSpec], limit: int) -> list[ActionSpec]:
    always = [
        action
        for action in actions
        if bool(action.args.get("always", False)) and random.randint(1, 100) <= action.probability
    ]
    candidates = [
        action
        for action in actions
        if not bool(action.args.get("always", False)) and random.randint(1, 100) <= action.probability
    ]
    random.shuffle(candidates)
    selected = always + candidates[: max(limit - len(always), 0)]
    return selected[: max(limit, len(always), 1)]


def _run_action(context: ActionContext, action: ActionSpec) -> None:
    handler = _REGISTRY.get(action.type)
    if handler is None:
        context.log(action, "skipped", {"reason": f"unknown action {action.type}"})
        return
    try:
        detail = handler(context, action)
        context.log(action, "ok", detail if isinstance(detail, dict) else {})
    except Exception as exc:
        context.log(action, "error", {"error": str(exc)})


def _delay_jitter(config: AgentConfig, action: ActionSpec, position: str) -> int:
    if position == "before":
        value = action.delay_before_jitter
        arg_name = "delay_before_jitter"
    else:
        value = action.delay_after_jitter
        arg_name = "delay_after_jitter"
    if value > 0:
        return value
    try:
        return int(action.args.get(arg_name, config.action_delay_jitter))
    except (TypeError, ValueError):
        return config.action_delay_jitter


def _sleep_with_jitter(base_seconds: int, jitter_seconds: int) -> None:
    jitter_seconds = max(int(jitter_seconds), 0)
    if jitter_seconds <= 0:
        time.sleep(base_seconds)
        return
    sleep_seconds = max(float(base_seconds) + random.uniform(-jitter_seconds, jitter_seconds), 0.0)
    time.sleep(sleep_seconds)


def _load_trigger_actions(context: ActionContext) -> list[ActionSpec]:
    trigger_dir = context.state_dir / "triggers"
    if not trigger_dir.exists():
        return []
    actions: list[ActionSpec] = []
    processed_dir = trigger_dir / "processed"
    if not context.dry_run:
        processed_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(trigger_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw_actions = data.get("actions") if isinstance(data, dict) else None
            if raw_actions is None and isinstance(data, dict):
                raw_actions = [data]
            if not isinstance(raw_actions, list):
                raise ValueError("trigger file must contain an action object or actions list")
            actions.extend(ActionSpec.from_dict(item) for item in raw_actions)
            if not context.dry_run:
                path.rename(processed_dir / path.name)
        except Exception as exc:
            context.log(ActionSpec(type="agent.trigger"), "error", {"path": str(path), "error": str(exc)})
            if not context.dry_run:
                path.rename(processed_dir / f"error-{path.name}")
    return actions


def _inside_work_hours(config: AgentConfig, now: datetime | None = None) -> bool:
    start = _parse_hhmm(config.persona.work_hours.start)
    end = _parse_hhmm(config.persona.work_hours.end)
    timestamp = now or datetime.now()
    weekday = timestamp.weekday()
    if config.persona.work_hours.weekdays and weekday not in _weekday_indexes(config.persona.work_hours.weekdays):
        return False
    now_time = timestamp.time()
    if start <= end:
        return start <= now_time <= end
    return now_time >= start or now_time <= end


def _parse_hhmm(value: str) -> datetime_time:
    hour_text, _, minute_text = value.partition(":")
    return datetime_time(hour=int(hour_text), minute=int(minute_text or "0"))


def _weekday_indexes(weekdays: list[str]) -> set[int]:
    lookup = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    return {lookup[day] for day in weekdays if day in lookup}


def _load_builtin_actions() -> None:
    if _REGISTRY:
        return
    from .actions import applications, browser, email, fileops, scenario, shell, ui
    from .actions import office

    applications.register(register_action)
    browser.register(register_action)
    email.register(register_action)
    fileops.register(register_action)
    office.register(register_action)
    scenario.register(register_action)
    shell.register(register_action)
    ui.register(register_action)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rangectl-activity-agent")
    parser.add_argument("config", nargs="?", type=Path)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    config = load_agent_config(args.config)
    return run_agent(config, once=args.once, dry_run=args.dry_run)


def load_agent_config(path: Path | None) -> AgentConfig:
    remote_url = os.environ.get("RANGECTL_ACTIVITY_CONFIG_URL", "").strip()
    if not remote_url:
        if path is None:
            raise SystemExit("a config path or RANGECTL_ACTIVITY_CONFIG_URL is required")
        return AgentConfig.from_file(path)

    parsed = urllib.parse.urlsplit(remote_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("RANGECTL_ACTIVITY_CONFIG_URL must use http:// or https://")
    token = os.environ.get("RANGECTL_ACTIVITY_CONFIG_TOKEN", "").strip()
    if not token:
        token = os.environ.get("RANGECTL_SCENARIO_STATE_TOKEN", "").strip()
    if not token:
        raise SystemExit("a remote activity config token is required")
    request = urllib.request.Request(
        remote_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read(4 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"remote activity config failed with HTTP {exc.code}") from exc
    except OSError as exc:
        raise SystemExit(f"remote activity config failed: {exc}") from exc
    if len(content) > 4 * 1024 * 1024:
        raise SystemExit("remote activity config exceeds 4 MiB")
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("remote activity config is not valid UTF-8 JSON") from exc
    if not isinstance(data, dict):
        raise SystemExit("remote activity config must contain a JSON object")
    return AgentConfig.from_dict(data)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
