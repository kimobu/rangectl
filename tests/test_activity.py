from __future__ import annotations

import os
import tempfile
import threading
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

from rangectl_lib.activity.agent import ActionContext, _inside_work_hours, main, run_agent
from rangectl_lib.activity.actions import office
from rangectl_lib.activity.compiler import compile_activity
from rangectl_lib.activity.models import ActionSpec, AgentConfig, RangeActivity
from rangectl_lib.activity.scenario_state import HttpScenarioStateStore
from rangectl_lib.activity.state_server import create_server


class ActivityTests(unittest.TestCase):
    def test_range_activity_parses_explicit_persona(self) -> None:
        data = {
            "activity": {
                "enabled": True,
                "personas": [
                    {
                        "id": "alice",
                        "username": "alice.example",
                        "host": "alice-pc",
                        "role": "sales",
                        "email": {"address": "alice@example.local", "backend": "outlook"},
                    }
                ],
            }
        }
        activity = RangeActivity.from_range(data)
        self.assertTrue(activity.enabled)
        self.assertEqual(activity.personas[0].email.backend, "outlook")
        self.assertIn("sales", activity.profiles)

    def test_compile_writes_agent_config(self) -> None:
        data = {
            "metadata": {"name": "unit"},
            "activity": {
                "enabled": True,
                "defaults": {
                    "enforce_work_hours": True,
                    "work_hours": {
                        "start": "09:00",
                        "end": "17:00",
                        "weekdays": ["monday", "tuesday", "wednesday", "thursday", "friday"],
                    },
                },
                "personas": [
                    {"id": "alice", "username": "alice.example", "host": "alice-pc", "role": "office"}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            written = compile_activity(data, out_dir=Path(temp))
            self.assertEqual(len(written), 2)
            json_config = next(path for path in written if path.suffix == ".json")
            config = AgentConfig.from_file(json_config)
            self.assertEqual(config.host, "alice-pc")
            self.assertGreaterEqual(len(config.actions), 1)
            self.assertTrue(config.enforce_work_hours)
            self.assertEqual(config.persona.work_hours.weekdays, ["monday", "tuesday", "wednesday", "thursday", "friday"])
            self.assertEqual(json_config.stat().st_mode & 0o777, 0o600)

    def test_work_hours_enforce_weekdays(self) -> None:
        data = {
            "metadata": {"name": "unit"},
            "activity": {
                "enabled": True,
                "defaults": {
                    "enforce_work_hours": True,
                    "work_hours": {"start": "09:00", "end": "17:00", "weekdays": ["monday"]},
                },
                "personas": [
                    {"id": "alice", "username": "alice.example", "host": "alice-pc", "role": "office"}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            written = compile_activity(data, out_dir=Path(temp))
            json_config = next(path for path in written if path.suffix == ".json")
            config = AgentConfig.from_file(json_config)
            self.assertTrue(_inside_work_hours(config, datetime(2026, 6, 29, 10, 0)))
            self.assertFalse(_inside_work_hours(config, datetime(2026, 6, 30, 10, 0)))
            self.assertFalse(_inside_work_hours(config, datetime(2026, 6, 29, 18, 0)))

    def test_agent_dry_run_logs_actions(self) -> None:
        data = {
            "metadata": {"name": "unit"},
            "activity": {
                "enabled": True,
                "defaults": {"max_actions_per_cycle": 1},
                "personas": [
                    {
                        "id": "alice",
                        "username": "alice.example",
                        "host": "alice-pc",
                        "role": "office",
                        "email": {"address": "alice@example.local", "backend": "external"},
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            written = compile_activity(data, out_dir=Path(temp))
            json_config = next(path for path in written if path.suffix == ".json")
            config = AgentConfig.from_file(json_config)
            config.state_dir = str(Path(temp) / "state")
            rc = run_agent(config, once=True, dry_run=True)
            self.assertEqual(rc, 0)
            log_path = Path(config.state_dir) / "activity.jsonl"
            self.assertTrue(log_path.exists())
            self.assertIn('"persona": "alice"', log_path.read_text(encoding="utf-8"))

    def test_office_dry_run_does_not_generate_or_write_content(self) -> None:
        config = AgentConfig.from_dict(
            {
                "range_name": "unit",
                "host": "alice-pc",
                "state_dir": ".",
                "persona": {"id": "alice", "username": "alice", "host": "alice-pc"},
                "actions": [],
            }
        )
        content = mock.Mock()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            context = ActionContext(
                config=config,
                content=content,
                state_dir=root,
                scenario_state=object(),
                dry_run=True,
            )
            cases = (
                (office.word, "report.docx", "docx"),
                (office.excel, "report.xlsx", "xlsx"),
                (office.powerpoint, "report.pptx", "pptx"),
            )
            for handler, filename, mode in cases:
                with self.subTest(filename=filename):
                    result = handler(
                        context,
                        ActionSpec(
                            type="office.test",
                            args={"directory": str(root), "filename": filename, "use_app": False},
                        ),
                    )
                    self.assertEqual(result, {"target": str(root / filename), "mode": mode})
                    self.assertFalse((root / filename).exists())
            content.generate.assert_not_called()

    def test_remote_config_and_activity_logs_never_touch_endpoint_state(self) -> None:
        data = {
            "metadata": {"name": "unit"},
            "activity": {
                "enabled": True,
                "defaults": {"max_actions_per_cycle": 1},
                "personas": [
                    {"id": "alice", "username": "alice", "host": "alice-mac", "role": "office"}
                ],
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            written = compile_activity(data, out_dir=root / "compiled")
            config_path = next(path for path in written if path.suffix == ".json")
            config = AgentConfig.from_file(config_path)
            endpoint_state = root / "endpoint-state"
            config.state_dir = str(endpoint_state)

            server = create_server("127.0.0.1", 0, root / "server", "r" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                store = HttpScenarioStateStore(base_url, "r" * 32, "unit", "alice-mac")
                store.write_text("profile.json", __import__("json").dumps(config.to_dict()))
                environment = {
                    "RANGECTL_ACTIVITY_CONFIG_URL": f"{base_url}/v1/state/unit/alice-mac/profile.json",
                    "RANGECTL_ACTIVITY_CONFIG_TOKEN": "r" * 32,
                    "RANGECTL_SCENARIO_STATE_URL": base_url,
                    "RANGECTL_SCENARIO_STATE_TOKEN": "r" * 32,
                }
                with mock.patch.dict(os.environ, environment, clear=False):
                    self.assertEqual(main(["--once", "--dry-run"]), 0)

                self.assertFalse((endpoint_state / "activity.jsonl").exists())
                self.assertFalse((endpoint_state / "scenarios").exists())
                events = root / "server" / "unit" / "alice-mac" / "events.jsonl"
                self.assertTrue(events.is_file())
                self.assertIn('"persona": "alice"', events.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_remote_log_failure_does_not_interrupt_actions_or_fall_back_to_disk(self) -> None:
        class FailingRemoteStore:
            remote = True

            def append_text(self, _name: str, _content: str) -> None:
                raise RuntimeError("unavailable")

        data = {
            "range_name": "unit",
            "host": "alice-mac",
            "state_dir": ".",
            "persona": {"id": "alice", "username": "alice", "host": "alice-mac"},
            "actions": [],
        }
        with tempfile.TemporaryDirectory() as temp:
            config = AgentConfig.from_dict(data)
            context = ActionContext(
                config=config,
                content=object(),
                state_dir=Path(temp),
                scenario_state=FailingRemoteStore(),
            )
            context.log(ActionSpec(type="browser.visit"), "ok", {"url": "https://example.test"})
            self.assertFalse((Path(temp) / "activity.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
