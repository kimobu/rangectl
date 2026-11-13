from __future__ import annotations

from pathlib import Path
from typing import Any

from rangectl_lib.model import range_name, range_state_dir

from .models import AgentConfig, RangeActivity


def compile_activity(range_data: dict[str, Any], out_dir: Path | None = None) -> list[Path]:
    activity = RangeActivity.from_range(range_data)
    if not activity.enabled:
        return []
    target_dir = out_dir or (range_state_dir(range_data) / "activity")
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for persona in activity.personas:
        profile = activity.profiles.get(persona.role) or activity.profiles.get("office")
        if profile is None:
            continue
        config = AgentConfig(
            range_name=range_name(range_data),
            host=persona.host,
            persona=persona,
            actions=profile.actions,
            ai=activity.ai,
            state_dir=str(target_dir / persona.host),
            loop=bool(activity.defaults.get("loop", True)),
            max_actions_per_cycle=int(activity.defaults.get("max_actions_per_cycle", 3)),
            cycle_delay=int(activity.defaults.get("cycle_delay", 300)),
            cycle_jitter=int(activity.defaults.get("cycle_jitter", 0)),
            action_delay_jitter=int(activity.defaults.get("action_delay_jitter", 0)),
            enforce_work_hours=bool(activity.defaults.get("enforce_work_hours", False)),
        )
        json_path = target_dir / f"{persona.host}.activity.json"
        yaml_path = target_dir / f"{persona.host}.activity.yml"
        config.write(json_path)
        config.write(yaml_path)
        written.extend([json_path, yaml_path])
    return written
