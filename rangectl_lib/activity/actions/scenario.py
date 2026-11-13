from __future__ import annotations

import hashlib
import json
import os
import random
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ..scenario_state import (
    LocalScenarioStateStore,
    ScenarioStateStore,
    load_json_document,
    migrate_legacy_scenario_state,
)


def register(add: Callable[[str, Any], None]) -> None:
    add("scenario.dwell", dwell)


def dwell(context: Any, action: Any) -> dict[str, Any]:
    scenarios = action.args.get("scenarios") or action.args.get("scenario")
    if isinstance(scenarios, dict):
        scenarios = [scenarios]
    if not isinstance(scenarios, list) or not scenarios:
        return {"status": "skipped", "reason": "no scenarios configured"}

    store = _scenario_store(context)
    migrate_legacy_scenario_state(Path(context.state_dir), store)
    now = time.time()
    results = []
    for raw in scenarios:
        if not isinstance(raw, dict):
            continue
        results.append(_run_scenario(context, raw, now))
    return {"scenarios": results}


def _run_scenario(context: Any, scenario: dict[str, Any], now: float) -> dict[str, Any]:
    scenario_id = str(scenario.get("id") or scenario.get("name") or "scenario")
    store = _scenario_store(context)
    state_name = _state_document_name(context, scenario_id)
    state = _read_state(store, state_name)
    loaded_legacy = False
    legacy_name = _legacy_state_document_name(scenario_id)
    if not state and store.remote and legacy_name != state_name:
        state = _read_state(store, legacy_name)
        loaded_legacy = bool(state)
    original_state = json.dumps(state, sort_keys=True)
    created = False
    if not state:
        state = _initialize_state(context, scenario, scenario_id, now)
        created = True
    else:
        _migrate_state(context, scenario, scenario_id, state, now)
    if state.get("status") == "complete":
        if loaded_legacy and not context.dry_run:
            store.write_text(state_name, json.dumps(state, indent=2, sort_keys=True) + "\n")
        _write_manifest(context, scenario, state)
        return {
            "id": scenario_id,
            "created": created,
            "status": "complete",
            "executed_phases": [],
            "periodic": {"ran": False, "reason": "scenario_complete"},
            "complete_phases": sum(1 for phase in state.get("phases", {}).values() if phase.get("complete")),
            "total_phases": len(state.get("phases", {})),
        }

    executed: list[str] = []
    for phase in scenario.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("name") or f"phase-{len(executed) + 1}")
        phase_state = state.setdefault("phases", {}).get(phase_name)
        if not isinstance(phase_state, dict):
            continue
        if phase_state.get("complete"):
            continue
        if now < float(phase_state.get("due_at", now)):
            continue
        _execute_phase(context, scenario, phase, phase_name)
        phase_state["complete"] = True
        phase_state["completed_at"] = _iso_now()
        state["last_event_at"] = phase_state["completed_at"]
        executed.append(phase_name)

    periodic_result = _run_periodic(context, scenario, state, now)
    if periodic_result.get("ran"):
        state["last_event_at"] = str(periodic_result.get("last_run_at") or _iso_now())
    _update_completion(context, scenario, state, now)
    changed = created or loaded_legacy or json.dumps(state, sort_keys=True) != original_state
    if changed:
        state["updated_at"] = _iso_now()
    if changed and not context.dry_run:
        store.write_text(state_name, json.dumps(state, indent=2, sort_keys=True) + "\n")
        _write_manifest(context, scenario, state)

    return {
        "id": scenario_id,
        "created": created,
        "status": state.get("status", "running"),
        "executed_phases": executed,
        "periodic": periodic_result,
        "complete_phases": sum(1 for phase in state.get("phases", {}).values() if phase.get("complete")),
        "total_phases": len(state.get("phases", {})),
    }


def _initialize_state(context: Any, scenario: dict[str, Any], scenario_id: str, now: float) -> dict[str, Any]:
    seed = _stable_seed(context.config.host, context.config.persona.id, scenario_id)
    rng = random.Random(seed)
    time_scale = float(scenario.get("time_scale", 1.0))
    state: dict[str, Any] = {
        "id": scenario_id,
        "report": str(scenario.get("report", scenario_id)),
        "host": context.config.host,
        "persona": context.config.persona.id,
        "user": context.config.persona.username,
        "created_at": _iso_now(),
        "first_event_at": _iso_now(),
        "last_event_at": _iso_now(),
        "status": "running",
        "time_scale": time_scale,
        "phases": {},
        "periodic": {},
        "historical_context": {},
    }

    for phase in scenario.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        phase_name = str(phase.get("name") or f"phase-{len(state['phases']) + 1}")
        delay = float(phase.get("after_seconds", phase.get("delay_seconds", 0))) * time_scale
        jitter = float(phase.get("jitter_seconds", 0)) * time_scale
        if jitter > 0:
            delay += rng.uniform(-jitter, jitter)
        due_at = now + max(delay, 0)
        state["phases"][phase_name] = {
            "due_at": due_at,
            "due_at_iso": _iso_from_epoch(due_at),
            "complete": False,
        }

    backdated_items = [item for item in scenario.get("backdated_artifacts") or [] if isinstance(item, dict)]
    if backdated_items:
        max_backdate = max(int(item.get("backdate_seconds", 0)) for item in backdated_items)
        if max_backdate > 0:
            state["historical_context"]["oldest_backdated_artifact_mtime"] = _iso_from_epoch(now - max_backdate)
            state["historical_context"]["backdated_artifact_count"] = len(backdated_items)
    for item in scenario.get("backdated_artifacts") or []:
        if isinstance(item, dict):
            _execute_step(context, scenario, item, "backdated")
    return state


def _migrate_state(context: Any, scenario: dict[str, Any], scenario_id: str, state: dict[str, Any], now: float) -> None:
    created_at = str(state.get("created_at") or state.get("first_event_at") or _iso_from_epoch(now))
    first_event_at = str(state.get("first_event_at") or state.get("last_event_at") or created_at)
    state.setdefault("id", scenario_id)
    state.setdefault("report", str(scenario.get("report", scenario_id)))
    state.setdefault("host", context.config.host)
    state.setdefault("persona", context.config.persona.id)
    state.setdefault("user", context.config.persona.username)
    state["created_at"] = created_at
    state["first_event_at"] = first_event_at
    state["last_event_at"] = str(state.get("last_event_at") or first_event_at)
    state.setdefault("status", "running")
    state.setdefault("time_scale", float(scenario.get("time_scale", 1.0)))
    if not isinstance(state.get("phases"), dict):
        state["phases"] = {}
    if not isinstance(state.get("periodic"), dict):
        state["periodic"] = {}
    if not isinstance(state.get("historical_context"), dict):
        state["historical_context"] = {}
    for phase in state.get("phases", {}).values():
        if not isinstance(phase, dict):
            continue
        if phase.get("due_at") is not None and not phase.get("due_at_iso"):
            phase["due_at_iso"] = _iso_from_epoch(float(phase["due_at"]))


def _run_periodic(context: Any, scenario: dict[str, Any], state: dict[str, Any], now: float) -> dict[str, Any]:
    periodic = scenario.get("periodic")
    if not isinstance(periodic, dict) or not periodic.get("enabled", True):
        state.setdefault("periodic", {})["complete"] = True
        return {"ran": False, "reason": "disabled"}
    start_after = float(periodic.get("start_after_seconds", 0)) * float(state.get("time_scale", 1.0))
    created = _parse_iso_epoch(str(state.get("created_at", ""))) or now
    periodic_state = state.setdefault("periodic", {})
    if periodic_state.get("complete"):
        return {"ran": False, "reason": "periodic_complete"}
    end_after = periodic.get("end_after_seconds", periodic.get("run_for_seconds"))
    if end_after is not None:
        end_at = created + (float(end_after) * float(state.get("time_scale", 1.0)))
        periodic_state["end_at"] = end_at
        periodic_state["end_at_iso"] = _iso_from_epoch(end_at)
        if now >= end_at:
            periodic_state["complete"] = True
            periodic_state["completed_at"] = _iso_now()
            return {"ran": False, "reason": "periodic_window_elapsed", "completed": True}
    max_runs = periodic.get("max_runs")
    if max_runs is not None and int(periodic_state.get("count", 0)) >= int(max_runs):
        periodic_state["complete"] = True
        periodic_state["completed_at"] = _iso_now()
        return {"ran": False, "reason": "max_runs_reached", "completed": True}
    if now < created + start_after:
        return {"ran": False, "reason": "not_due"}
    interval = float(periodic.get("interval_seconds", 1800)) * float(state.get("time_scale", 1.0))
    jitter = float(periodic.get("jitter_seconds", 0)) * float(state.get("time_scale", 1.0))
    next_due = float(periodic_state.get("next_due", created + start_after))
    if now < next_due:
        return {"ran": False, "next_due": _iso_from_epoch(next_due)}

    for step in periodic.get("steps") or []:
        if isinstance(step, dict):
            _execute_step(context, scenario, step, "periodic")
    count = int(periodic_state.get("count", 0)) + 1
    periodic_state["count"] = count
    periodic_state["last_run_at"] = _iso_now()
    if max_runs is not None and count >= int(max_runs):
        periodic_state["complete"] = True
        periodic_state["completed_at"] = periodic_state["last_run_at"]
        return {"ran": True, "count": count, "completed": True, "last_run_at": periodic_state["last_run_at"]}
    next_interval = max(interval + random.uniform(-jitter, jitter), 60)
    next_due = now + next_interval
    if end_after is not None:
        end_at = created + (float(end_after) * float(state.get("time_scale", 1.0)))
        if next_due >= end_at:
            periodic_state["complete"] = True
            periodic_state["completed_at"] = periodic_state["last_run_at"]
            return {"ran": True, "count": count, "completed": True, "last_run_at": periodic_state["last_run_at"]}
    periodic_state["next_due"] = next_due
    periodic_state["next_due_iso"] = _iso_from_epoch(next_due)
    return {"ran": True, "count": count, "next_due": periodic_state["next_due_iso"], "last_run_at": periodic_state["last_run_at"]}


def _update_completion(context: Any, scenario: dict[str, Any], state: dict[str, Any], now: float) -> None:
    phases_complete = all(phase.get("complete") for phase in state.get("phases", {}).values())
    periodic = scenario.get("periodic")
    periodic_complete = not isinstance(periodic, dict) or not periodic.get("enabled", True) or bool(state.get("periodic", {}).get("complete"))
    force_end_after = scenario.get("end_after_seconds")
    if force_end_after is not None:
        created = _parse_iso_epoch(str(state.get("created_at", ""))) or now
        force_end_at = created + (float(force_end_after) * float(state.get("time_scale", 1.0)))
        state["expected_end_at"] = _iso_from_epoch(force_end_at)
        if now >= force_end_at:
            phases_complete = True
            periodic_complete = True
    if phases_complete and periodic_complete and state.get("status") != "complete":
        state["status"] = "complete"
        state["completed_at"] = _iso_now()
        state["last_event_at"] = state.get("last_event_at") or state["completed_at"]
        _scenario_log(
            context,
            scenario,
            "complete",
            {
                "event_type": "scenario_complete",
                "first_event_at": state.get("first_event_at"),
                "last_event_at": state.get("last_event_at"),
                "completed_at": state["completed_at"],
            },
        )


def _execute_phase(context: Any, scenario: dict[str, Any], phase: dict[str, Any], phase_name: str) -> None:
    for step in phase.get("steps") or []:
        if isinstance(step, dict):
            _execute_step(context, scenario, step, phase_name)


def _execute_step(context: Any, scenario: dict[str, Any], step: dict[str, Any], phase_name: str) -> None:
    step_type = str(step.get("type", "artifact"))
    if context.dry_run:
        return
    if step_type == "artifact":
        _write_artifact(context, scenario, step, phase_name)
    elif step_type == "callback":
        _callback(context, scenario, step, phase_name)
    elif step_type == "command":
        _command(context, scenario, step, phase_name)
    elif step_type == "open_url":
        _open_url(step)
    elif step_type in {"calendar_invite", "mail_calendar_invite"}:
        _calendar_invite(context, scenario, step, phase_name)
    elif step_type == "log":
        _scenario_log(context, scenario, phase_name, dict(step.get("detail") or {}))


def _write_artifact(context: Any, scenario: dict[str, Any], step: dict[str, Any], phase_name: str) -> None:
    target = _expand_path(str(step.get("path") or "~/Library/Application Support/rangectl-scenario/artifact.json"))
    content = _render(str(step.get("content") or _default_artifact_content(context, scenario, phase_name)), context, scenario, phase_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if step.get("format") == "json":
        parsed = json.loads(content) if isinstance(content, str) else content
        target.write_text(json.dumps(parsed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        target.write_text(content, encoding="utf-8")
    if step.get("mode") is not None:
        target.chmod(int(str(step["mode"]), 8))
    backdate = int(step.get("backdate_seconds", 0))
    if backdate > 0:
        ts = time.time() - backdate
        os.utime(target, (ts, ts))
    _scenario_log(context, scenario, phase_name, {"event_type": "artifact", "path": str(target)})


def _callback(context: Any, scenario: dict[str, Any], step: dict[str, Any], phase_name: str) -> None:
    url = _render(str(step.get("url") or ""), context, scenario, phase_name)
    if not url:
        return
    method = str(step.get("method", "GET")).upper()
    user_agent = _render(str(step.get("user_agent") or f"Mozilla/5.0 ({context.config.persona.role})"), context, scenario, phase_name)
    body = _render(str(step.get("body") or _default_callback_body(context, scenario, phase_name)), context, scenario, phase_name)
    output = step.get("output")
    command = ["/usr/bin/curl", "-A", user_agent, "-fsS", "--max-time", str(int(step.get("timeout", 12)))]
    if method == "POST":
        command.extend(["-X", "POST", "-H", "Content-Type: application/json", "--data", body])
    if output:
        target = _expand_path(str(output))
        target.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["-o", str(target)])
    else:
        command.extend(["-o", "/dev/null"])
    command.append(url)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=int(step.get("timeout", 12)) + 3)
    _scenario_log(
        context,
        scenario,
        phase_name,
        {
            "event_type": "callback",
            "url": url,
            "method": method,
            "user_agent": user_agent,
            "returncode": completed.returncode,
            "output": str(output or ""),
        },
    )


def _command(context: Any, scenario: dict[str, Any], step: dict[str, Any], phase_name: str) -> None:
    commands = step.get("commands") or step.get("command")
    if isinstance(commands, str):
        commands = [commands]
    if not commands:
        return
    command = _render(str(random.choice(commands)), context, scenario, phase_name)
    use_shell = step.get("shell") is True
    completed = subprocess.run(
        command if use_shell else shlex.split(command),
        shell=use_shell,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=int(step.get("timeout", 30)),
    )
    _scenario_log(
        context,
        scenario,
        phase_name,
        {
            "event_type": "command",
            "command": command,
            "shell": use_shell,
            "returncode": completed.returncode,
        },
    )


def _open_url(step: dict[str, Any]) -> None:
    url = str(step.get("url") or "")
    if not url:
        return
    subprocess.run(["open", url], check=False, capture_output=True, text=True, timeout=10)


def _calendar_invite(context: Any, scenario: dict[str, Any], step: dict[str, Any], phase_name: str) -> None:
    url = _render(str(step.get("url") or ""), context, scenario, phase_name)
    if not url:
        return
    title = _render(str(step.get("title") or "Web3 SDK troubleshooting session"), context, scenario, phase_name)
    organizer = _render(str(step.get("organizer") or "Product News Support"), context, scenario, phase_name)
    ics_path = _expand_path(str(step.get("ics_path") or "~/Downloads/rangectl_meeting.ics"))
    ics_path.parent.mkdir(parents=True, exist_ok=True)
    if not ics_path.exists() or bool(step.get("overwrite_ics", True)):
        ics_path.write_text(
            _ics_content(
                title,
                organizer,
                context.config.persona.display_name,
                context.config.persona.email.address,
                url,
                step,
            ),
            encoding="utf-8",
        )

    opened: list[str] = []
    for app_name, delay in (
        ("Mail", float(step.get("mail_dwell_seconds", 4))),
        ("Calendar", float(step.get("calendar_dwell_seconds", 6))),
    ):
        completed = subprocess.run(["open", "-gj", "-a", app_name], check=False, capture_output=True, text=True, timeout=10)
        if completed.returncode == 0:
            opened.append(app_name)
        time.sleep(max(delay + random.uniform(-1.5, 1.5), 0.2))

    subprocess.run(["open", "-a", "Calendar", str(ics_path)], check=False, capture_output=True, text=True, timeout=10)
    time.sleep(max(float(step.get("event_dwell_seconds", 8)) + random.uniform(-2.0, 2.0), 0.5))
    if bool(step.get("open_url", True)):
        browser = str(step.get("browser") or "")
        if browser:
            subprocess.run(["open", "-a", browser, url], check=False, capture_output=True, text=True, timeout=10)
        else:
            subprocess.run(["open", url], check=False, capture_output=True, text=True, timeout=10)
    _scenario_log(
        context,
        scenario,
        phase_name,
        {
            "event_type": "calendar_invite",
            "url": url,
            "ics_path": str(ics_path),
            "opened_apps": opened,
        },
    )


def _ics_content(title: str, organizer: str, attendee_name: str, attendee_email: str, url: str, step: dict[str, Any]) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prodid = str(step.get("prodid") or "-//rangectl//Calendar//EN")
    uid_prefix = str(step.get("uid_prefix") or "rangectl")
    uid_domain = str(step.get("uid_domain") or "example.test")
    organizer_email = str(step.get("organizer_email") or "meetings@example.test")
    dtstart = str(step.get("dtstart") or "20260812T180000Z")
    dtend = str(step.get("dtend") or "20260812T183000Z")
    description = str(
        step.get("description")
        or f"Join the support session and follow the meeting troubleshooting instructions if prompted: {url}"
    )
    return "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            f"PRODID:{prodid}",
            "METHOD:REQUEST",
            "BEGIN:VEVENT",
            f"UID:{uid_prefix}-{hashlib.sha256(url.encode()).hexdigest()[:12]}@{uid_domain}",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{dtstart}",
            f"DTEND:{dtend}",
            f"ORGANIZER;CN={organizer}:mailto:{organizer_email}",
            f"ATTENDEE;CN={attendee_name or attendee_email};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}",
            f"SUMMARY:{title}",
            f"LOCATION:{url}",
            f"DESCRIPTION:{description}",
            f"URL:{url}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )


def _scenario_log(context: Any, scenario: dict[str, Any], phase_name: str, detail: dict[str, Any]) -> None:
    scenario_id = str(scenario.get("id") or scenario.get("name") or "scenario")
    document_stem = _scenario_document_stem(context, scenario_id)
    record = {
        "ts": _iso_now(),
        "range": context.config.range_name,
        "host": context.config.host,
        "persona": context.config.persona.id,
        "user": context.config.persona.username,
        "scenario": scenario_id,
        "phase": phase_name,
        **detail,
    }
    _scenario_store(context).append_text(f"{document_stem}.jsonl", json.dumps(record, sort_keys=True) + "\n")


def _write_manifest(context: Any, scenario: dict[str, Any], state: dict[str, Any]) -> None:
    store = _scenario_store(context)
    manifest = load_json_document(store, "run-manifest.json")
    scenarios = manifest.setdefault("scenarios", {})
    phase_schedule = {
        name: {
            "due_at": phase.get("due_at_iso"),
            "completed_at": phase.get("completed_at"),
            "complete": bool(phase.get("complete")),
        }
        for name, phase in (state.get("phases") or {}).items()
        if isinstance(phase, dict)
    }
    first_event_at = state.get("first_event_at") or state.get("created_at") or state.get("last_event_at")
    last_event_at = state.get("last_event_at") or first_event_at
    completed_at = state.get("completed_at")
    scenario_key = str(state.get("id", scenario.get("id", "scenario")))
    manifest_entry = {
        "id": state.get("id", scenario.get("id", "scenario")),
        "report": state.get("report", scenario.get("report", "")),
        "range": context.config.range_name,
        "host": context.config.host,
        "persona": context.config.persona.id,
        "user": context.config.persona.username,
        "status": state.get("status", "running"),
        "first_event_at": first_event_at,
        "last_event_at": last_event_at,
        "completed_at": completed_at,
        "historical_context": state.get("historical_context", {}),
        "phase_schedule": phase_schedule,
        "periodic": {
            "count": int((state.get("periodic") or {}).get("count", 0)),
            "complete": bool((state.get("periodic") or {}).get("complete", False)),
            "completed_at": (state.get("periodic") or {}).get("completed_at"),
            "next_due": (state.get("periodic") or {}).get("next_due_iso"),
            "end_at": (state.get("periodic") or {}).get("end_at_iso"),
        },
        "elastic_hunt_window": {
            "start": first_event_at,
            "end": completed_at or last_event_at,
            "note": "Use this August run window for November class hunts. Backdated artifact mtimes are context, not the Elastic event window.",
        },
    }
    if scenarios.get(scenario_key) == manifest_entry:
        return
    scenarios[scenario_key] = manifest_entry
    manifest["updated_at"] = _iso_now()
    if not context.dry_run:
        store.write_text("run-manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _state_path(context: Any, scenario_id: str) -> Path:
    safe_id = _safe_scenario_id(scenario_id)
    return Path(context.state_dir) / "scenarios" / f"{safe_id}.state.json"


def _state_document_name(context: Any, scenario_id: str) -> str:
    return f"{_scenario_document_stem(context, scenario_id)}.state.json"


def _legacy_state_document_name(scenario_id: str) -> str:
    return f"{_safe_scenario_id(scenario_id)}.state.json"


def _scenario_document_stem(context: Any, scenario_id: str) -> str:
    store = _scenario_store(context)
    if not store.remote:
        return _safe_scenario_id(scenario_id)
    material = "\0".join(
        [
            str(context.config.range_name),
            str(context.config.host),
            scenario_id,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _safe_scenario_id(scenario_id: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in scenario_id)
    return safe_id or "scenario"


def _read_state(store: ScenarioStateStore, name: str) -> dict[str, Any]:
    return load_json_document(store, name)


def _scenario_store(context: Any) -> ScenarioStateStore:
    store = getattr(context, "scenario_state", None)
    if store is not None:
        return store
    return LocalScenarioStateStore(Path(context.state_dir) / "scenarios")


def _expand_path(value: str) -> Path:
    return Path(value).expanduser()


def _render(value: str, context: Any, scenario: dict[str, Any], phase_name: str) -> str:
    scenario_id = str(scenario.get("id") or scenario.get("name") or "scenario")
    replacements = {
        "scenario_id": scenario_id,
        "phase": phase_name,
        "host": context.config.host,
        "persona": context.config.persona.id,
        "username": context.config.persona.username,
        "role": context.config.persona.role,
        "timestamp": _iso_now(),
    }
    rendered = value
    for key, replacement in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", replacement)
    return rendered


def _default_artifact_content(context: Any, scenario: dict[str, Any], phase_name: str) -> str:
    return json.dumps(
        {
            "scenario": scenario.get("id") or scenario.get("name") or "scenario",
            "phase": phase_name,
            "host": context.config.host,
            "user": context.config.persona.username,
            "timestamp": _iso_now(),
        },
        sort_keys=True,
    )


def _default_callback_body(context: Any, scenario: dict[str, Any], phase_name: str) -> str:
    return _default_artifact_content(context, scenario, phase_name)


def _stable_seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_iso_epoch(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
