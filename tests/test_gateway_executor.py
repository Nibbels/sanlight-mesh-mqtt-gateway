import json
import tempfile
import unittest
from pathlib import Path

from sanlight_mesh.constants import SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE
from sanlight_mesh.gateway_config import GatewayConfig, MqttConfig
from sanlight_mesh.gateway_executor import CliCommandExecutor, ProcessResult
from sanlight_mesh.protocol import decode_daylight_status_pdu


def daylight_pdu(address_seed=2):
    values = ((0, 0), (360, 20), (1080, 0))
    parameters = (
        address_seed.to_bytes(4, "little")
        + bytes((len(values),))
        + b"".join(
            minute.to_bytes(2, "little") + bytes((brightness,))
            for minute, brightness in values
        )
        + b"Flower\x00"
    )
    return bytes.fromhex("c48b0a") + parameters


def daylight_stdout(address, pdu, *, verified):
    status = decode_daylight_status_pdu(
        pdu,
        request_opcode=SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE,
    )
    document = {
        "address": address,
        "verified": verified,
        **status.to_document(),
    }
    return "GET-DAYLIGHT COMPLETE. " + json.dumps(
        document, separators=(",", ":")
    )


class GatewayExecutorTest(unittest.TestCase):
    def config(self, root: Path) -> GatewayConfig:
        entry = root / "sanlight_canonical_sender_poc.py"
        entry.write_text("", encoding="utf-8")
        return GatewayConfig(
            config_path=root / "config.toml",
            project_root=root,
            gateway_id="test",
            cdb_path=root / "private.json",
            control_app_id=1,
            sender_app_id=2,
            state_dir=root / ".state",
            command_timeout_seconds=45,
            queue_max_size=10,
            dedup_ttl_seconds=60,
            dedup_max_entries=16,
            coalesce_window_seconds=2,
            state_fresh_seconds=300,
            refresh_on_start=False,
            refresh_interval_seconds=0,
            topic_prefix="sanlightmesh/v1",
            mqtt=MqttConfig("broker", 1883, "client", None, None, 60, 1, False, None),
        )

    def test_get_max_parser_accepts_zero_and_normal_values(self):
        parse = CliCommandExecutor._parse_get_max
        self.assertEqual(parse(ProcessResult(0, "GET-MAX COMPLETE. Node 0x0003 reports MaxBrightness 0% (off).", "")), ("0003", 0))
        self.assertEqual(parse(ProcessResult(0, "GET-MAX COMPLETE. Node 0x0003 reports MaxBrightness 68%.", "")), ("0003", 68))


    def test_parses_verified_values_from_restore_output(self):
        result = ProcessResult(
            0,
            "Received matching SANlight GetMaxBrightness status from 0x0003: 0% (off).\n"
            "RESTORE-BLACKOUT VERIFIED: 0x0003=68%, 0x0002=48%.",
            "",
        )
        self.assertEqual(
            CliCommandExecutor._parse_reported_values(result),
            {"0003": 68, "0002": 48},
        )

    def test_base_argv_contains_fixed_paths_not_mqtt_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = CliCommandExecutor(self.config(root), ["0002"])
            argv = executor._base_argv()
            self.assertEqual(argv[1], str(root / "sanlight_canonical_sender_poc.py"))
            self.assertNotIn("shell", " ".join(argv).lower())

    def test_get_daylight_parser_redecodes_raw_pdu(self):
        pdu = daylight_pdu()
        parsed = CliCommandExecutor._parse_get_daylight(
            ProcessResult(0, daylight_stdout("0002", pdu, verified=True), "")
        )
        self.assertIsNotNone(parsed)
        address, status = parsed
        self.assertEqual(address, "0002")
        self.assertTrue(status.parsed)
        self.assertEqual(status.configuration.name, "Flower")

    def test_get_daylight_parser_rejects_untrusted_verified_flag(self):
        pdu = daylight_pdu()
        self.assertIsNone(
            CliCommandExecutor._parse_get_daylight(
                ProcessResult(0, daylight_stdout("0002", pdu, verified=False), "")
            )
        )

    def test_refresh_classifies_complete_mesh_no_response(self):
        class StubExecutor(CliCommandExecutor):
            def _run(self, arguments, timeout=None):
                if arguments[0] == "get-max":
                    return ProcessResult(
                        4,
                        "GET-MAX UNCONFIRMED. BlueZ accepted the read-only query, but "
                        "no valid matching 0x09 status was received after 2 attempts.",
                        "",
                    )
                return ProcessResult(
                    0,
                    "GET-LIVE COMPLETE. No SANlight 0x0D status was observed after "
                    "2 attempts.",
                    "",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = StubExecutor(self.config(root), ["0002", "0003"])
            result = executor.refresh("all")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "mesh-no-response")
        self.assertEqual(result.details["failureClass"], "mesh-no-response")
        self.assertTrue(result.details["transmissionAccepted"])
        self.assertIn("may be stale", result.message)

    def test_read_daylight_classifies_complete_mesh_no_response(self):
        class StubExecutor(CliCommandExecutor):
            def _run(self, arguments, timeout=None):
                return ProcessResult(
                    3,
                    "GET-DAYLIGHT COMPLETE. No SANlight 0x0F or 0x04 daylight status "
                    "was observed after the combined and configuration-only queries.",
                    "",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = StubExecutor(self.config(root), ["0002", "0003"])
            result = executor.read_daylight("all")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "mesh-no-response")
        self.assertEqual(result.details["failureClass"], "mesh-no-response")
        self.assertIn("daylight requests", result.message)

    def test_read_daylight_retains_raw_only_responses(self):
        class StubExecutor(CliCommandExecutor):
            def _run(self, arguments, timeout=None):
                address = arguments[1]
                if address == "0002":
                    pdu = daylight_pdu(2)
                    return ProcessResult(
                        0,
                        daylight_stdout(address, pdu, verified=True),
                        "",
                    )
                raw = bytes.fromhex("c48b0a0000")
                return ProcessResult(
                    3,
                    daylight_stdout(address, raw, verified=False),
                    "",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor = StubExecutor(self.config(root), ["0002", "0003"])
            result = executor.read_daylight("all")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "partial")
        self.assertEqual(set(result.daylight_reported), {"0002", "0003"})
        self.assertTrue(result.daylight_reported["0002"].parsed)
        self.assertFalse(result.daylight_reported["0003"].parsed)
        self.assertIn("0003", result.details["errors"])
