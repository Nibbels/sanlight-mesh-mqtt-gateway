import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sanlight_mesh.constants import SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE
from sanlight_mesh.gateway_store import GatewayStore
from sanlight_mesh.protocol import decode_daylight_status_pdu


def daylight_status(*, malformed=False):
    if malformed:
        pdu = bytes.fromhex("c48b0a0000")
    else:
        values = ((0, 0), (360, 20), (1080, 0))
        parameters = (
            (12).to_bytes(4, "little")
            + bytes((len(values),))
            + b"".join(
                minute.to_bytes(2, "little") + bytes((brightness,))
                for minute, brightness in values
            )
            + b"Flower 12/12\x00"
        )
        pdu = bytes.fromhex("c48b0a") + parameters
    return decode_daylight_status_pdu(
        pdu,
        request_opcode=SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE,
    )


class GatewayStoreTest(unittest.TestCase):
    def test_dedup_result_and_node_state_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            now = datetime.now(timezone.utc)
            store = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            store.remember_result("a", {"ok": True}, now=now)
            store.update_node("0003", 48, now=now)
            reloaded = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            self.assertEqual(reloaded.get_result("a"), {"ok": True})
            self.assertEqual(reloaded.get_node("0003").max_brightness, 48)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_inflight_marker_is_persistent_and_cleared_by_final_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            store.mark_inflight("write-1", {"action": "set-max", "target": "0003"})
            reloaded = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            self.assertEqual(
                reloaded.get_inflight("write-1")["command"]["target"], "0003"
            )
            reloaded.remember_result("write-1", {"ok": True})
            self.assertIsNone(reloaded.get_inflight("write-1"))

    def test_prunes_expired_and_limits_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            now = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
            store = GatewayStore(path, dedup_ttl_seconds=60, dedup_max_entries=2)
            store.remember_result("old", {"ok": True}, now=now - timedelta(seconds=61))
            store.remember_result("new1", {"ok": True}, now=now - timedelta(seconds=1))
            store.remember_result("new2", {"ok": True}, now=now)
            store.prune(now)
            self.assertIsNone(store.get_result("old"))
            self.assertIsNotNone(store.get_result("new1"))
            self.assertIsNotNone(store.get_result("new2"))

    def test_mesh_health_tracks_complete_no_response_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc)
            second = first + timedelta(minutes=1)
            recovered = second + timedelta(minutes=1)
            store = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)

            store.record_mesh_outcome(
                command_id="read-1",
                action="read-daylight",
                target="all",
                response_observed=False,
                complete_no_response=True,
                now=first,
            )
            store.record_mesh_outcome(
                command_id="read-2",
                action="refresh",
                target="all",
                response_observed=False,
                complete_no_response=True,
                now=second,
            )

            reloaded = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            health = reloaded.get_mesh_health()
            self.assertEqual(health["consecutiveCompleteNoResponseCommands"], 2)
            self.assertEqual(health["lastCompleteNoResponseCommand"]["id"], "read-2")

            reloaded.record_mesh_outcome(
                command_id="read-3",
                action="refresh",
                target="0002",
                response_observed=True,
                complete_no_response=False,
                now=recovered,
            )
            health = GatewayStore(
                path,
                dedup_ttl_seconds=3600,
                dedup_max_entries=10,
            ).get_mesh_health()
            self.assertEqual(health["consecutiveCompleteNoResponseCommands"], 0)
            self.assertEqual(health["lastSuccessfulResponseAt"], recovered.isoformat().replace("+00:00", "Z"))
            self.assertNotIn("lastCompleteNoResponseAt", health)

    def test_daylight_verified_state_survives_raw_only_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
            second = first + timedelta(minutes=1)
            store = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            store.update_node("0003", 68, now=first)
            store.update_daylight("0003", daylight_status(), now=first)
            store.update_daylight(
                "0003",
                daylight_status(malformed=True),
                now=second,
            )

            reloaded = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            state = reloaded.get_node("0003")
            self.assertIsNotNone(state.daylight_status)
            self.assertTrue(state.daylight_status.parsed)
            self.assertEqual(state.daylight_status.configuration.name, "Flower 12/12")
            self.assertFalse(state.daylight_last_read_ok)
            self.assertEqual(state.daylight_last_read_at, second)
            self.assertIsNotNone(state.daylight_last_observation)
            self.assertFalse(state.daylight_last_observation.parsed)
            self.assertIn("too short", state.daylight_last_error)

    def test_no_response_preserves_verified_daylight_and_clears_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            first = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
            second = first + timedelta(minutes=1)
            store = GatewayStore(path, dedup_ttl_seconds=3600, dedup_max_entries=10)
            store.update_node("0003", 68, now=first)
            store.update_daylight("0003", daylight_status(), now=first)
            store.record_daylight_failure("0003", "timeout", now=second)

            state = GatewayStore(
                path,
                dedup_ttl_seconds=3600,
                dedup_max_entries=10,
            ).get_node("0003")
            self.assertIsNotNone(state.daylight_status)
            self.assertIsNone(state.daylight_last_observation)
            self.assertFalse(state.daylight_last_read_ok)
            self.assertEqual(state.daylight_last_error, "timeout")
