from __future__ import annotations

import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path

from rangectl_lib.activity.actions.scenario import dwell
from rangectl_lib.activity.models import AgentConfig
from rangectl_lib.activity.scenario_state import (
    HttpScenarioStateStore,
    migrate_legacy_activity_log,
    migrate_legacy_scenario_state,
)
from rangectl_lib.activity.state_server import create_server, derive_scoped_token


class ScenarioStateTests(unittest.TestCase):
    def test_state_server_rejects_short_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "at least 32 characters"):
                create_server("127.0.0.1", 0, Path(temp) / "server", "short")

    def test_scoped_token_cannot_access_another_host(self) -> None:
        master = "s" * 32
        with tempfile.TemporaryDirectory() as temp:
            server = create_server("127.0.0.1", 0, Path(temp) / "server", master)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                alice_token = derive_scoped_token(master, "unit", "alice-mac")
                alice = HttpScenarioStateStore(base_url, alice_token, "unit", "alice-mac")
                alice.write_text("profile.json", '{"host":"alice-mac"}\n')
                other_host = HttpScenarioStateStore(base_url, alice_token, "unit", "bob-mac")
                with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                    other_host.read_text("profile.json")

                admin = HttpScenarioStateStore(base_url, master, "unit", "alice-mac")
                self.assertEqual(admin.read_text("profile.json"), '{"host":"alice-mac"}\n')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_store_keeps_documents_on_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            server = create_server("127.0.0.1", 0, Path(temp) / "server", "x" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    token="x" * 32,
                    range_name="unit",
                    host="alice-mac",
                )
                store.write_text("scenario.state.json", '{"status":"running"}\n')
                store.append_text("scenario.jsonl", '{"phase":"one"}\n')
                store.append_text("scenario.jsonl", '{"phase":"two"}\n')

                root = Path(temp) / "server" / "unit" / "alice-mac"
                self.assertEqual((root / "scenario.state.json").read_text(), '{"status":"running"}\n')
                self.assertEqual(
                    (root / "scenario.jsonl").read_text(),
                    '{"phase":"one"}\n{"phase":"two"}\n',
                )
                self.assertEqual(store.read_text("scenario.state.json"), '{"status":"running"}\n')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_large_remote_document_is_compressed_in_transit_and_restored(self) -> None:
        content = ('{"action":"browser.visit","value":"' + ("x" * 1024) + '"}\n') * 1024
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = create_server("127.0.0.1", 0, root / "server", "c" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    derive_scoped_token("c" * 32, "unit", "alice-mac"),
                    "unit",
                    "alice-mac",
                )
                store.write_text("large.jsonl", content)
                self.assertEqual(
                    (root / "server" / "unit" / "alice-mac" / "large.jsonl").read_text(),
                    content,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_legacy_endpoint_activity_log_migrates_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            legacy = base / "endpoint"
            legacy.mkdir(parents=True)
            (legacy / "activity.jsonl").write_text('{"action":"browser.visit"}\n')

            server = create_server("127.0.0.1", 0, base / "server", "l" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    token=derive_scoped_token("l" * 32, "unit", "alice-mac"),
                    range_name="unit",
                    host="alice-mac",
                )
                self.assertTrue(migrate_legacy_activity_log(legacy, store))
                self.assertFalse((legacy / "activity.jsonl").exists())
                migrated = list(
                    (base / "server" / "unit" / "alice-mac").glob("legacy-events-*.jsonl")
                )
                self.assertEqual(len(migrated), 1)
                self.assertEqual(migrated[0].read_text(), '{"action":"browser.visit"}\n')
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_legacy_endpoint_scenario_state_migrates_and_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            legacy = base / "endpoint" / "scenarios"
            legacy.mkdir(parents=True)
            (legacy / "scenario.state.json").write_text('{"status":"running"}\n')
            (legacy / "scenario.jsonl").write_text('{"phase":"one"}\n')

            server = create_server("127.0.0.1", 0, base / "server", "y" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    base_url=f"http://127.0.0.1:{server.server_address[1]}",
                    token="y" * 32,
                    range_name="unit",
                    host="alice-mac",
                )
                self.assertTrue(migrate_legacy_scenario_state(base / "endpoint", store))
                self.assertFalse(legacy.exists())
                self.assertEqual(
                    (base / "server" / "unit" / "alice-mac" / "scenario.state.json").read_text(),
                    '{"status":"running"}\n',
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


    def test_remote_scenario_keeps_semantics_server_side_and_does_not_replay(self) -> None:
        scenario_id = "campaign-training-label"
        scenario = {
            "id": scenario_id,
            "report": "Campaign Training Label",
            "time_scale": 0,
            "phases": [
                {
                    "name": "initial-access-phase",
                    "after_seconds": 0,
                    "steps": [{"type": "log", "detail": {"event_type": "phase_seen"}}],
                }
            ],
            "periodic": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = create_server("127.0.0.1", 0, root / "server", "z" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    "z" * 32,
                    "unit",
                    "alice-mac",
                )
                config = AgentConfig.from_dict(
                    {
                        "range_name": "unit",
                        "host": "alice-mac",
                        "state_dir": str(root / "endpoint"),
                        "persona": {"id": "alice", "username": "alice", "host": "alice-mac"},
                        "actions": [],
                    }
                )
                context = SimpleNamespace(
                    config=config,
                    state_dir=root / "endpoint",
                    scenario_state=store,
                    dry_run=False,
                )
                action = SimpleNamespace(args={"scenarios": [scenario]})
                first = dwell(context, action)
                host_root = root / "server" / "unit" / "alice-mac"
                names = sorted(path.name for path in host_root.iterdir())
                self.assertFalse(any(scenario_id in name for name in names))
                opaque_log = next(path for path in host_root.glob("[0-9a-f]*.jsonl"))
                first_lines = opaque_log.read_text(encoding="utf-8").splitlines()
                self.assertEqual(first["scenarios"][0]["status"], "complete")
                self.assertTrue(any(scenario_id in path.read_text(encoding="utf-8") for path in host_root.glob("*.json")))

                second = dwell(context, action)
                self.assertEqual(second["scenarios"][0]["status"], "complete")
                self.assertEqual(first_lines, opaque_log.read_text(encoding="utf-8").splitlines())
                self.assertFalse((root / "endpoint" / "scenarios").exists())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_completed_legacy_remote_state_migrates_without_replay(self) -> None:
        scenario_id = "legacy-campaign"
        scenario = {
            "id": scenario_id,
            "phases": [{"name": "one", "after_seconds": 0, "steps": [{"type": "log"}]}],
            "periodic": {"enabled": False},
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server = create_server("127.0.0.1", 0, root / "server", "m" * 32)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                store = HttpScenarioStateStore(
                    f"http://127.0.0.1:{server.server_address[1]}",
                    "m" * 32,
                    "unit",
                    "alice-mac",
                )
                store.write_text(
                    "legacy-campaign.state.json",
                    json.dumps(
                        {
                            "id": scenario_id,
                            "status": "complete",
                            "phases": {"one": {"complete": True}},
                            "periodic": {"complete": True},
                        }
                    ),
                )
                config = AgentConfig.from_dict(
                    {
                        "range_name": "unit",
                        "host": "alice-mac",
                        "persona": {"id": "alice", "username": "alice", "host": "alice-mac"},
                        "actions": [],
                    }
                )
                context = SimpleNamespace(
                    config=config,
                    state_dir=root / "endpoint",
                    scenario_state=store,
                    dry_run=False,
                )
                result = dwell(context, SimpleNamespace(args={"scenarios": [scenario]}))
                self.assertEqual(result["scenarios"][0]["status"], "complete")
                host_root = root / "server" / "unit" / "alice-mac"
                opaque_states = [
                    path
                    for path in host_root.glob("*.state.json")
                    if path.name != "legacy-campaign.state.json"
                ]
                self.assertEqual(len(opaque_states), 1)
                self.assertFalse(list(host_root.glob("[0-9a-f]*.jsonl")))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
