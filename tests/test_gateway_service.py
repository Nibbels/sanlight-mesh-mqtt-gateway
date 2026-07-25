import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sanlight_mesh.constants import SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE
from sanlight_mesh.gateway_config import GatewayConfig, MqttConfig
from sanlight_mesh.gateway_executor import ExecutionResult
from sanlight_mesh.gateway_protocol import isoformat_utc
from sanlight_mesh.gateway_service import SanlightMqttGateway
from sanlight_mesh.mqtt_transport import IncomingMessage
from sanlight_mesh.protocol import decode_daylight_status_pdu
from sanlight_mesh.state import write_state
from sanlight_mesh.cdb import load_mesh_material


FIXTURE = Path(__file__).parent / "fixtures" / "sample_cdb.json"
ROOT = Path(__file__).resolve().parents[1]


def daylight_status(
    configuration_id=7,
    *,
    name="Flower 12/12",
    values=((0, 0), (360, 20), (1080, 0)),
):
    parameters = (
        configuration_id.to_bytes(4, "little")
        + bytes((len(values),))
        + b"".join(
            minute.to_bytes(2, "little") + bytes((brightness,))
            for minute, brightness in values
        )
        + name.encode("utf-8")
        + b"\x00"
    )
    return decode_daylight_status_pdu(
        bytes.fromhex("c48b0a") + parameters,
        request_opcode=SANLIGHT_GET_DAYLIGHT_CONFIGURATION_OPCODE,
    )


class FakeTransport:
    def __init__(self):
        self.json_messages = []
        self.text_messages = []
        self.disconnected = False

    def publish_json(self, topic, payload, retain=False):
        self.json_messages.append((topic, dict(payload), retain))

    def publish_text(self, topic, payload, retain=False):
        self.text_messages.append((topic, payload, retain))

    def disconnect(self):
        self.disconnected = True


class FakeExecutor:
    def __init__(self):
        self.commands = []

    def execute(self, command):
        self.commands.append(command)
        if command.action == "read-daylight":
            addresses = ("0002", "0003") if command.target == "all" else (command.target,)
            return ExecutionResult(
                ok=True,
                status="verified",
                message="daylight verified",
                reported={},
                daylight_reported={
                    address: daylight_status(int(address, 16)) for address in addresses
                },
                details={},
            )
        value = command.value if command.value is not None else 68
        return ExecutionResult(
            ok=True,
            status="verified",
            message="verified",
            reported={"0003": value} if command.target != "all" else {"0002": 68, "0003": 68},
            details={"exitCode": 0},
        )

    def sender_sequence_state(self):
        return {"sequenceNumber": 100, "sequenceRemaining": 16777115}


class MixedDaylightExecutor(FakeExecutor):
    def execute(self, command):
        if command.action != "read-daylight" or command.target != "all":
            return super().execute(command)
        self.commands.append(command)
        return ExecutionResult(
            ok=True,
            status="verified",
            message="daylight verified",
            reported={},
            daylight_reported={
                "0002": daylight_status(
                    396_724_180,
                    name="100% 6:18",
                    values=(
                        (0, 0),
                        (360, 0),
                        (361, 20),
                        (390, 100),
                        (1410, 100),
                        (1438, 20),
                        (1440, 0),
                    ),
                ),
                "0003": daylight_status(
                    947_599_001,
                    name="100% 12:12",
                    values=(
                        (0, 0),
                        (359, 0),
                        (360, 20),
                        (390, 100),
                        (1050, 100),
                        (1080, 20),
                        (1081, 0),
                        (1440, 0),
                    ),
                ),
            },
            details={},
        )


class NoResponseExecutor(FakeExecutor):
    def execute(self, command):
        self.commands.append(command)
        return ExecutionResult(
            ok=False,
            status="mesh-no-response",
            message="no response",
            reported={},
            live_reported={},
            daylight_reported={},
            details={
                "failureClass": "mesh-no-response",
                "errors": {"0002": "timeout", "0003": "timeout"},
            },
        )


class GatewayServiceTest(unittest.TestCase):
    def make_gateway(self, tmp):
        state = Path(tmp) / "state"
        control = load_mesh_material(FIXTURE, 1)
        sender = load_mesh_material(FIXTURE, 2)
        write_state(
            state / "control-provisioner.json",
            {
                "token": "1",
                "role": "provisioner",
                "meshUUID": str(control.mesh_uuid),
                "provisionerUUID": str(control.provisioner.uuid),
                "unicast": control.provisioner.unicast,
                "appId": 1,
            },
        )
        write_state(
            state / "canonical-sender.json",
            {
                "token": "2",
                "role": "canonical-sender",
                "meshUUID": str(control.mesh_uuid),
                "senderProvisionerUUID": str(sender.provisioner.uuid),
                "senderAppId": 2,
                "unicast": sender.provisioner.unicast,
            },
        )
        config = GatewayConfig(
            config_path=Path(tmp) / "gateway.toml",
            project_root=ROOT,
            gateway_id="test-pi",
            cdb_path=FIXTURE,
            control_app_id=1,
            sender_app_id=2,
            state_dir=state,
            command_timeout_seconds=45,
            queue_max_size=10,
            dedup_ttl_seconds=3600,
            dedup_max_entries=32,
            coalesce_window_seconds=0,
            state_fresh_seconds=0,
            refresh_on_start=False,
            refresh_interval_seconds=0,
            topic_prefix="sanlightmesh/v1",
            mqtt=MqttConfig("broker", 1883, "client", None, None, 60, 1, False, None),
        )
        gateway = SanlightMqttGateway(config)
        gateway.executor = FakeExecutor()
        transport = FakeTransport()
        gateway.bind_transport(transport)
        gateway.start_workers()
        return gateway, transport

    @staticmethod
    def payload(command_id="cmd-1"):
        now = datetime.now(timezone.utc)
        return json.dumps(
            {
                "id": command_id,
                "action": "set-max",
                "target": "0003",
                "value": 48,
                "createdAt": isoformat_utc(now),
                "ttlSeconds": 30,
            }
        ).encode()

    @staticmethod
    def wait_for_result(transport, command_id, timeout=2):
        deadline = time.monotonic() + timeout
        suffix = f"/result/{command_id}"
        while time.monotonic() < deadline:
            for topic, payload, retained in transport.json_messages:
                if topic.endswith(suffix):
                    return payload
            time.sleep(0.01)
        raise AssertionError(f"result {command_id} not published")

    def test_retained_command_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            try:
                gateway.on_message(
                    IncomingMessage(gateway.config.command_topic, self.payload(), 1, True)
                )
                result = self.wait_for_result(transport, "cmd-1")
                self.assertEqual(result["status"], "rejected")
                self.assertIn("retained commands", result["message"])
                self.assertEqual(gateway.executor.commands, [])
            finally:
                gateway.stop()

    def test_retained_invalid_payload_is_rejected_before_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            try:
                gateway.on_message(
                    IncomingMessage(gateway.config.command_topic, b"", 1, True)
                )
                results = [
                    payload
                    for topic, payload, retained in transport.json_messages
                    if "/result/invalid-" in topic
                ]
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["status"], "rejected")
                self.assertIn("retained commands", results[0]["message"])
                self.assertEqual(gateway.executor.commands, [])
            finally:
                gateway.stop()

    def test_verified_command_updates_retained_state_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            try:
                message = IncomingMessage(gateway.config.command_topic, self.payload(), 1, False)
                gateway.on_message(message)
                result = self.wait_for_result(transport, "cmd-1")
                self.assertTrue(result["ok"])
                self.assertEqual(len(gateway.executor.commands), 1)
                state_messages = [
                    payload
                    for topic, payload, retained in transport.json_messages
                    if topic.endswith("/nodes/0003/state") and retained
                ]
                self.assertEqual(state_messages[-1]["maxBrightness"], 48)
                info_messages = [
                    payload
                    for topic, payload, retained in transport.json_messages
                    if topic.endswith("/gateway/info") and retained
                ]
                self.assertEqual(info_messages[-1]["sequenceStatus"], "ok")

                # QoS 1 redelivery with the same id republishes the stored result.
                gateway.on_message(message)
                time.sleep(0.05)
                self.assertEqual(len(gateway.executor.commands), 1)
                duplicate_results = [
                    payload
                    for topic, payload, retained in transport.json_messages
                    if topic.endswith("/result/cmd-1")
                ]
                self.assertTrue(duplicate_results[-1]["details"]["duplicateDelivery"])
            finally:
                gateway.stop()

    def test_complete_no_response_is_persisted_and_published_in_gateway_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            gateway.executor = NoResponseExecutor()
            try:
                now = datetime.now(timezone.utc)
                payload = json.dumps(
                    {
                        "id": "mesh-timeout-1",
                        "action": "read-daylight",
                        "target": "all",
                        "createdAt": isoformat_utc(now),
                        "ttlSeconds": 30,
                    }
                ).encode()
                gateway.on_message(
                    IncomingMessage(gateway.config.command_topic, payload, 1, False)
                )

                result = self.wait_for_result(transport, "mesh-timeout-1")
                self.assertEqual(result["status"], "mesh-no-response")
                health = gateway.store.get_mesh_health()
                self.assertEqual(health["consecutiveCompleteNoResponseCommands"], 1)
                self.assertEqual(
                    health["lastCompleteNoResponseCommand"]["id"],
                    "mesh-timeout-1",
                )
                info_messages = [
                    message
                    for topic, message, retained in transport.json_messages
                    if topic.endswith("/gateway/info") and retained
                ]
                self.assertEqual(
                    info_messages[-1]["meshHealth"][
                        "consecutiveCompleteNoResponseCommands"
                    ],
                    1,
                )
            finally:
                gateway.stop()

    def test_all_lamp_daylight_read_accepts_different_valid_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            gateway.executor = MixedDaylightExecutor()
            try:
                gateway.store.update_node("0002", 30)
                gateway.store.update_node("0003", 30)
                now = datetime.now(timezone.utc)
                payload = json.dumps(
                    {
                        "id": "daylight-mixed",
                        "action": "read-daylight",
                        "target": "all",
                        "createdAt": isoformat_utc(now),
                        "ttlSeconds": 30,
                    }
                ).encode()
                gateway.on_message(
                    IncomingMessage(gateway.config.command_topic, payload, 1, False)
                )

                result = self.wait_for_result(transport, "daylight-mixed")
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], "verified")
                reported = result["details"]["daylightReported"]
                self.assertEqual(
                    reported["0002"]["configuration"]["name"], "100% 6:18"
                )
                self.assertEqual(
                    reported["0003"]["configuration"]["name"], "100% 12:12"
                )
                self.assertNotEqual(
                    reported["0002"]["configuration"],
                    reported["0003"]["configuration"],
                )

                retained = {
                    topic.rsplit("/", 2)[-2]: message["daylightConfiguration"]
                    for topic, message, is_retained in transport.json_messages
                    if is_retained
                    and topic.endswith("/state")
                    and "daylightConfiguration" in message
                }
                self.assertEqual(
                    retained["0002"]["configuration"]["name"], "100% 6:18"
                )
                self.assertEqual(
                    retained["0003"]["configuration"]["name"], "100% 12:12"
                )
                self.assertTrue(retained["0002"]["verified"])
                self.assertTrue(retained["0003"]["verified"])
            finally:
                gateway.stop()

    def test_read_daylight_publishes_raw_and_parsed_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            gateway, transport = self.make_gateway(tmp)
            try:
                # Node state v1 is anchored by a verified MaxBrightness value.
                gateway.store.update_node("0003", 68)
                now = datetime.now(timezone.utc)
                payload = json.dumps(
                    {
                        "id": "daylight-1",
                        "action": "read-daylight",
                        "target": "0003",
                        "createdAt": isoformat_utc(now),
                        "ttlSeconds": 30,
                    }
                ).encode()
                gateway.on_message(
                    IncomingMessage(gateway.config.command_topic, payload, 1, False)
                )
                result = self.wait_for_result(transport, "daylight-1")
                self.assertTrue(result["ok"])
                reported = result["details"]["daylightReported"]["0003"]
                self.assertTrue(reported["parsed"])
                self.assertEqual(reported["configuration"]["name"], "Flower 12/12")
                self.assertTrue(reported["rawPduHex"].startswith("c48b0a"))

                state_messages = [
                    message
                    for topic, message, retained in transport.json_messages
                    if topic.endswith("/nodes/0003/state") and retained
                ]
                daylight = state_messages[-1]["daylightConfiguration"]
                self.assertTrue(daylight["verified"])
                self.assertTrue(daylight["lastReadOk"])
                self.assertEqual(daylight["configuration"]["id"], 3)
                self.assertTrue(daylight["rawPduHex"].startswith("c48b0a"))
            finally:
                gateway.stop()
