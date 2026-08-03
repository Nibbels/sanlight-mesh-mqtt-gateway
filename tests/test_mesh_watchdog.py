import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sanlight_mesh.mesh_watchdog import (
    WatchdogConfig,
    assess_probe,
    eligible_incident,
    load_watchdog_config,
    parse_uart_tx,
)


class MeshWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 3, 18, 40, tzinfo=timezone.utc)
        self.config = WatchdogConfig()
        self.health = {
            "lastSuccessfulResponseAt": "2026-08-03T18:10:00Z",
            "consecutiveCompleteNoResponseCommands": 1,
            "lastCompleteNoResponseAt": "2026-08-03T18:40:00Z",
            "lastCompleteNoResponseCommand": {
                "id": "read-daylight-all-test",
                "action": "read-daylight",
                "target": "all",
            },
        }

    def test_parse_uart_tx_prefers_pl011(self):
        text = (
            "serinfo:1.0 driver revision:\n"
            "0: uart:16550A tx:99 rx:100 oe:0\n"
            "1: uart:PL011 rev2 tx:1125170 rx:688184352 oe:55 RTS|CTS\n"
        )
        self.assertEqual(parse_uart_tx(text), 1125170)

    def test_confirmed_stall_requires_acceptance_no_response_and_zero_tx(self):
        output = (
            "GetUptimeAndBrightness accepted for Mesh transmission.\n"
            "GET-LIVE COMPLETE. No SANlight 0x0D status was observed after 2 attempts.\n"
        )
        assessment = assess_probe(output, 1000, 1000)
        self.assertTrue(assessment.confirmed_stall)
        self.assertEqual(assessment.outcome, "confirmed-tx-stall")

    def test_transmitted_probe_without_response_does_not_recover(self):
        output = (
            "GetUptimeAndBrightness accepted for Mesh transmission.\n"
            "GET-LIVE COMPLETE. No SANlight 0x0D status was observed after 2 attempts.\n"
        )
        assessment = assess_probe(output, 1000, 1140)
        self.assertFalse(assessment.confirmed_stall)
        self.assertEqual(assessment.outcome, "hci-tx-observed-no-lamp-response")

    def test_successful_probe_never_recovers(self):
        output = (
            "GetUptimeAndBrightness accepted for Mesh transmission.\n"
            "GET-LIVE COMPLETE. Node 0x0002 reports lampTimeMs=1 "
            "lampClock=00:00:00.001 liveBrightnessRaw=0 "
            "liveBrightnessPercentEstimate=0.0%.\n"
        )
        assessment = assess_probe(output, 1000, 1140)
        self.assertFalse(assessment.confirmed_stall)
        self.assertEqual(assessment.outcome, "response-observed")

    def test_missing_uart_counter_fails_closed(self):
        output = (
            "GetUptimeAndBrightness accepted for Mesh transmission.\n"
            "GET-LIVE COMPLETE. No SANlight 0x0D status was observed after 2 attempts.\n"
        )
        assessment = assess_probe(output, None, None)
        self.assertFalse(assessment.confirmed_stall)
        self.assertEqual(assessment.outcome, "uart-counter-unavailable")

    def test_recent_all_node_read_failure_is_eligible(self):
        eligible, _ = eligible_incident(
            self.health, {}, self.config, now=self.now
        )
        self.assertTrue(eligible)

    def test_single_node_failure_is_not_eligible(self):
        self.health["lastCompleteNoResponseCommand"]["target"] = "0002"
        eligible, reason = eligible_incident(
            self.health, {}, self.config, now=self.now
        )
        self.assertFalse(eligible)
        self.assertIn("all configured lamps", reason)

    def test_write_action_is_not_eligible(self):
        self.health["lastCompleteNoResponseCommand"]["action"] = "set-max"
        eligible, reason = eligible_incident(
            self.health, {}, self.config, now=self.now
        )
        self.assertFalse(eligible)
        self.assertIn("read-only", reason)

    def test_probe_cooldown_blocks_repeated_active_probes(self):
        state = {"lastProbeAt": "2026-08-03T18:20:00Z"}
        eligible, reason = eligible_incident(
            self.health, state, self.config, now=self.now
        )
        self.assertFalse(eligible)
        self.assertIn("probe cooldown", reason)

    def test_recovery_attempt_window_limits_restart_loop(self):
        state = {
            "recoveryHistory": [
                "2026-08-03T17:00:00Z",
                "2026-08-03T18:00:00Z",
            ]
        }
        eligible, reason = eligible_incident(
            self.health, state, self.config, now=self.now
        )
        self.assertFalse(eligible)
        self.assertIn("attempt limit", reason)

    def test_config_defaults_enable_watchdog(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.toml"
            path.write_text("[gateway]\nid='test'\n", encoding="utf-8")
            settings = load_watchdog_config(path)
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.max_recoveries_in_window, 2)

    def test_config_rejects_unknown_watchdog_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.toml"
            path.write_text(
                "[gateway]\nid='test'\n[watchdog]\nunknown=true\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown watchdog setting"):
                load_watchdog_config(path)

    def test_productization_files_install_watchdog_timer(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "systemd/sanlight-mesh-watchdog.service.example").read_text(
            encoding="utf-8"
        )
        timer = (root / "systemd/sanlight-mesh-watchdog.timer").read_text(
            encoding="utf-8"
        )
        installer = (root / "scripts/install-mqtt-gateway.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("sanlight_mesh.mesh_watchdog", service)
        self.assertIn("OnUnitInactiveSec=60s", timer)
        self.assertIn("systemctl enable sanlight-mesh-watchdog.timer", installer)
        self.assertIn("systemctl restart sanlight-mesh-watchdog.timer", installer)


if __name__ == "__main__":
    unittest.main()
