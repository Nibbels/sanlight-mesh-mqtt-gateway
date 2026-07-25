"""Serialized MQTT-to-BlueZ edge gateway service."""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .cdb import load_mesh_material, validate_material_pair
from .gateway_config import (
    GatewayConfig,
    GatewayConfigError,
    load_gateway_config,
    redacted_config_summary,
)
from .gateway_executor import CliCommandExecutor, ExecutionResult, GatewayExecutionError
from .gateway_protocol import (
    GatewayCommand,
    GatewayProtocolError,
    PROTOCOL_VERSION,
    decode_command,
    isoformat_utc,
    make_result,
    utc_now,
)
from .gateway_queue import GatewayCommandQueue
from .gateway_store import GatewayStore
from .mqtt_transport import IncomingMessage, MqttTransportError, PahoMqttTransport
from .traffic_safety import BRIGHTNESS_WRITE_MIN_INTERVAL_SECONDS
from .state import StateError, read_state, validate_state_identity


SERVICE_VERSION = "0.4.0"


class GatewayServiceError(RuntimeError):
    pass


def _clock_text(seconds: int) -> str:
    normalized = seconds % 86_400
    return f"{normalized // 3600:02d}:{(normalized % 3600) // 60:02d}:{normalized % 60:02d}"


def _gateway_local_clock() -> tuple[datetime, int, str]:
    observed = datetime.now().astimezone()
    seconds = observed.hour * 3600 + observed.minute * 60 + observed.second
    return observed, seconds, _clock_text(seconds)


class SanlightMqttGateway:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        control = load_mesh_material(config.cdb_path, config.control_app_id)
        sender = load_mesh_material(config.cdb_path, config.sender_app_id)
        validate_material_pair(control, sender, config.control_app_id, config.sender_app_id)
        expected_states = (
            (
                "control provisioner",
                config.state_dir / "control-provisioner.json",
                {
                    "role": "provisioner",
                    "meshUUID": str(control.mesh_uuid),
                    "provisionerUUID": str(control.provisioner.uuid),
                    "unicast": control.provisioner.unicast,
                    "appId": config.control_app_id,
                },
            ),
            (
                "canonical sender",
                config.state_dir / "canonical-sender.json",
                {
                    "role": "canonical-sender",
                    "meshUUID": str(control.mesh_uuid),
                    "senderProvisionerUUID": str(sender.provisioner.uuid),
                    "senderAppId": config.sender_app_id,
                    "unicast": sender.provisioner.unicast,
                },
            ),
        )
        for label, path, expected in expected_states:
            try:
                state = read_state(path)
                if state is not None:
                    validate_state_identity(state, expected, label.title())
            except StateError as exc:
                raise GatewayServiceError(str(exc)) from exc
            if state is None:
                raise GatewayServiceError(
                    f"{label} state is missing at {path}; complete SETUP.md first"
                )

        self.mesh_uuid = str(control.mesh_uuid)
        self.sender_unicast = f"{sender.provisioner.unicast:04X}"
        self.nodes = {f"{address:04X}": name for address, name in control.sanlight_nodes.items()}
        if not self.nodes:
            raise GatewayServiceError("CDB contains no SANlight vendor-model lamp nodes")
        self.store = GatewayStore(
            config.state_dir / "mqtt-gateway-state.json",
            dedup_ttl_seconds=config.dedup_ttl_seconds,
            dedup_max_entries=config.dedup_max_entries,
        )
        self.command_queue = GatewayCommandQueue(config.queue_max_size)
        self.executor = CliCommandExecutor(config, self.nodes)
        self.transport: PahoMqttTransport | None = None
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.refresh_thread: threading.Thread | None = None
        self.last_write_monotonic = 0.0
        self.connected = False
        self.sequence_state: dict[str, int] = {}

    def bind_transport(self, transport: PahoMqttTransport) -> None:
        self.transport = transport

    def start_workers(self) -> None:
        if self.worker is not None:
            return
        self.worker = threading.Thread(
            target=self._worker_loop,
            name="sanlight-mqtt-worker",
            daemon=True,
        )
        self.worker.start()
        if self.config.refresh_interval_seconds > 0:
            self.refresh_thread = threading.Thread(
                target=self._refresh_loop,
                name="sanlight-mqtt-refresh",
                daemon=True,
            )
            self.refresh_thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.transport is not None:
            try:
                self.transport.disconnect()
            except Exception as exc:
                print(f"MQTT disconnect warning: {exc}", file=sys.stderr)

    def _require_transport(self) -> PahoMqttTransport:
        if self.transport is None:
            raise GatewayServiceError("MQTT transport has not been bound")
        return self.transport

    def result_topic(self, command_id: str) -> str:
        return f"{self.config.topic_root}/result/{command_id}"

    def node_state_topic(self, address: str) -> str:
        return f"{self.config.topic_root}/nodes/{address}/state"

    def publish_result(self, command_id: str, result: Mapping[str, Any]) -> None:
        self._require_transport().publish_json(
            self.result_topic(command_id), result, retain=False
        )

    def publish_node_state(self, address: str, *, cached: bool = False) -> None:
        state = self.store.get_node(address)
        if state is None:
            return
        payload: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "address": address,
            "name": self.nodes[address],
            "maxBrightness": state.max_brightness,
            "off": state.max_brightness == 0,
            "verified": True,
            "verifiedAt": isoformat_utc(state.verified_at),
            "liveVerified": state.live_status is not None,
        }
        if cached:
            payload["cached"] = True
        if state.live_status is not None and state.live_verified_at is not None:
            payload.update(
                {
                    "lampClockSeconds": (state.live_status.lamp_time_ms // 1000) % 86_400,
                    "lampClock": _clock_text(state.live_status.lamp_time_ms // 1000),
                    "liveBrightnessRaw": state.live_status.brightness_raw,
                    "liveBrightnessPercentEstimate": (
                        state.live_status.brightness_percent_estimate
                    ),
                    "liveVerifiedAt": isoformat_utc(state.live_verified_at),
                }
            )
        if state.daylight_last_read_at is not None:
            daylight: dict[str, Any] = {
                "verified": state.daylight_status is not None,
                "lastReadAt": isoformat_utc(state.daylight_last_read_at),
                "lastReadOk": state.daylight_last_read_ok is True,
            }
            if (
                state.daylight_status is not None
                and state.daylight_verified_at is not None
            ):
                daylight.update(state.daylight_status.to_document())
                daylight["verifiedAt"] = isoformat_utc(state.daylight_verified_at)
            if state.daylight_last_observation is not None:
                observation = state.daylight_last_observation
                if state.daylight_status is None:
                    # No validated configuration exists yet. Keep the latest raw
                    # response directly visible for protocol investigation.
                    daylight.update(observation.to_document())
                elif (
                    state.daylight_last_read_ok is not True
                    or observation.raw_pdu != state.daylight_status.raw_pdu
                ):
                    daylight["lastObservation"] = observation.to_document()
            if state.daylight_last_error is not None:
                daylight["lastError"] = state.daylight_last_error
            payload["daylightConfiguration"] = daylight
        self._require_transport().publish_json(
            self.node_state_topic(address), payload, retain=True
        )

    def publish_cached_states(self) -> None:
        for address in self.nodes:
            self.publish_node_state(address, cached=True)

    def publish_gateway_info(self) -> None:
        observed, local_clock_seconds, local_clock = _gateway_local_clock()
        payload: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "serviceVersion": SERVICE_VERSION,
            "gatewayId": self.config.gateway_id,
            "meshUuid": self.mesh_uuid,
            "senderAddress": self.sender_unicast,
            "nodes": [
                {"address": address, "name": name}
                for address, name in sorted(self.nodes.items())
            ],
            "commandTopic": self.config.command_topic,
            "writePolicy": {
                "setMaxRange": {"minimum": 20, "maximum": 100},
                "blackoutRequiresConfirmation": True,
                "minimumWriteIntervalSeconds": BRIGHTNESS_WRITE_MIN_INTERVAL_SECONDS,
                "coalesceWindowSeconds": self.config.coalesce_window_seconds,
                "recommendedAutomationIntervalSeconds": 60,
                "clockSecondsRange": {"minimum": 0, "maximum": 86399},
                "clockVerificationToleranceSeconds": 5,
            },
            "localClockSeconds": local_clock_seconds,
            "localClock": local_clock,
            "timestamp": isoformat_utc(observed),
        }
        mesh_health = self.store.get_mesh_health()
        if mesh_health:
            payload["meshHealth"] = mesh_health
        payload.update(self.sequence_state)
        remaining = self.sequence_state.get("sequenceRemaining")
        if isinstance(remaining, int):
            capacity = 0xFFFFFF
            ratio = remaining / capacity
            status = "critical" if ratio <= 0.10 else "warning" if ratio <= 0.25 else "ok"
            payload["sequenceStatus"] = status
            payload["sequenceRemainingPercent"] = round(ratio * 100, 2)
            if status != "ok":
                payload["sequenceWarning"] = (
                    "Sender Sequence Number space is running low. Stop high-frequency "
                    "automation and plan a standards-compliant IV Update or controlled "
                    "Mesh rebuild before exhaustion."
                )
        self._require_transport().publish_json(
            f"{self.config.topic_root}/gateway/info", payload, retain=True
        )

    def on_connected(self) -> None:
        self.connected = True
        transport = self._require_transport()
        transport.publish_text(
            f"{self.config.topic_root}/availability", "online", retain=True
        )
        self.publish_gateway_info()
        for address, name in sorted(self.nodes.items()):
            transport.publish_json(
                f"{self.config.topic_root}/nodes/{address}/meta",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "address": address,
                    "name": name,
                    "writable": {"maxBrightness": {"minimum": 20, "maximum": 100}},
                    "readable": {"daylightConfiguration": True},
                    "supportsExplicitBlackout": True,
                },
                retain=True,
            )
        self.publish_cached_states()
        if self.config.refresh_on_start:
            self.enqueue_internal_refresh("startup")
        print(
            f"MQTT connected; subscribed to {self.config.command_topic}. "
            f"Published {len(self.nodes)} node definitions."
        )

    def on_disconnected(self, reason: str) -> None:
        self.connected = False
        print(f"MQTT disconnected: {reason}", file=sys.stderr)

    @staticmethod
    def _best_effort_id(payload: bytes) -> str | None:
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception:
            return None
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            candidate = value["id"]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", candidate):
                return candidate
        return None

    def on_message(self, message: IncomingMessage) -> None:
        if message.topic != self.config.command_topic:
            return
        fallback_id = self._best_effort_id(message.payload) or f"invalid-{uuid.uuid4().hex}"
        try:
            if message.retain:
                raise GatewayProtocolError(
                    "retained commands are rejected to prevent replay after reconnect"
                )
            command = decode_command(message.payload)
            if command.target not in ("all", "latest", "gateway") and command.target not in self.nodes:
                raise GatewayProtocolError(
                    f"target {command.target} is not a SANlight node in this CDB"
                )
            cached = self.store.get_result(command.command_id)
            if cached is not None:
                duplicate = json.loads(json.dumps(cached))
                duplicate.setdefault("details", {})
                if isinstance(duplicate["details"], dict):
                    duplicate["details"]["duplicateDelivery"] = True
                self.publish_result(command.command_id, duplicate)
                return
            inflight = self.store.get_inflight(command.command_id)
            if inflight is not None:
                result = make_result(
                    command,
                    ok=False,
                    status="indeterminate-after-restart",
                    message=(
                        "This command id was executing when the gateway stopped before "
                        "a final result was stored. It was not executed again. Refresh "
                        "the affected node state and use a new command id only after "
                        "deciding whether another write is required."
                    ),
                    details={"previousExecution": inflight},
                )
                self.publish_result(command.command_id, result)
                return
            if self.command_queue.contains(command.command_id):
                print(f"Ignoring duplicate delivery while {command.command_id} is pending.")
                return
            self.command_queue.put(command)
            print(
                f"Queued MQTT command {command.command_id}: {command.action} "
                f"target={command.target}; queue={self.command_queue.size()}."
            )
        except queue.Full:
            result = make_result(
                None,
                command_id=fallback_id,
                ok=False,
                status="queue-full",
                message="Gateway command queue is full; retry with a new command id.",
            )
            self.publish_result(fallback_id, result)
        except GatewayProtocolError as exc:
            result = make_result(
                None,
                command_id=fallback_id,
                ok=False,
                status="rejected",
                message=str(exc),
            )
            self.publish_result(fallback_id, result)

    def enqueue_internal_refresh(self, reason: str) -> None:
        now = utc_now()
        payload = {
            "id": f"internal-{reason}-{int(now.timestamp())}-{uuid.uuid4().hex[:8]}",
            "action": "refresh",
            "target": "all",
            "createdAt": isoformat_utc(now),
            "ttlSeconds": min(300, max(30, self.config.command_timeout_seconds * 2)),
        }
        try:
            self.command_queue.put(decode_command(json.dumps(payload).encode("utf-8"), now=now))
        except (queue.Full, GatewayProtocolError):
            print(f"Skipped internal {reason} refresh because the queue is unavailable.")

    def _refresh_loop(self) -> None:
        interval = self.config.refresh_interval_seconds
        while not self.stop_event.wait(interval):
            if self.connected:
                self.enqueue_internal_refresh("periodic")

    def _publish_and_remember(self, command: GatewayCommand, result: Mapping[str, Any]) -> None:
        # Persist first: if MQTT disconnects after Mesh execution, a QoS 1
        # redelivery with the same id must not execute the command again.
        self.store.remember_result(command.command_id, result)
        try:
            self.publish_result(command.command_id, result)
        except Exception as exc:
            print(
                f"Result {command.command_id} stored locally but MQTT publish failed: {exc}",
                file=sys.stderr,
            )

    def _wait_for_write_slot(self, command: GatewayCommand) -> bool:
        remaining = BRIGHTNESS_WRITE_MIN_INTERVAL_SECONDS - (
            time.monotonic() - self.last_write_monotonic
        )
        if remaining <= 0:
            return True
        if utc_now().timestamp() + remaining >= command.expires_at.timestamp():
            return False
        print(
            f"Waiting {remaining:.1f}s for the Mesh-write safety interval "
            f"before command {command.command_id}."
        )
        return not self.stop_event.wait(remaining)

    def _handle_superseded(self, command: GatewayCommand, replacement_id: str) -> None:
        result = make_result(
            command,
            ok=False,
            status="superseded",
            message=(
                f"A newer queued set-max command for node {command.target} replaced "
                "this command before Bluetooth transmission."
            ),
            details={"replacementId": replacement_id, "meshMessagesSent": 0},
        )
        self._publish_and_remember(command, result)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                command = self.command_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            original = command
            try:
                command, superseded = self.command_queue.coalesce_set_max(
                    command,
                    wait_seconds=self.config.coalesce_window_seconds,
                    sleep=lambda seconds: self.stop_event.wait(seconds),
                )
                for item in superseded:
                    self._handle_superseded(item.command, item.replacement_id)

                if command.expired():
                    result = make_result(
                        command,
                        ok=False,
                        status="expired",
                        message="Command expired before execution; no Mesh message was sent.",
                        details={"meshMessagesSent": 0},
                    )
                    self._publish_and_remember(command, result)
                    continue

                if command.action == "set-max" and command.value is not None:
                    cached = self.store.get_node(command.target)
                    if (
                        cached is not None
                        and cached.max_brightness == command.value
                        and self.config.state_fresh_seconds > 0
                        and self.store.node_is_fresh(
                            command.target,
                            fresh_seconds=self.config.state_fresh_seconds,
                        )
                    ):
                        result = make_result(
                            command,
                            ok=True,
                            status="no-op",
                            message=(
                                f"Fresh verified state already reports {command.value}%; "
                                "no Mesh message was sent."
                            ),
                            details={
                                "reported": {command.target: command.value},
                                "meshMessagesSent": 0,
                            },
                        )
                        self._publish_and_remember(command, result)
                        continue

                if command.is_write and not self._wait_for_write_slot(command):
                    result = make_result(
                        command,
                        ok=False,
                        status="expired",
                        message=(
                            "Command expired while waiting for the Mesh-write "
                            "safety interval; no Mesh write was sent."
                        ),
                        details={"meshMessagesSent": 0},
                    )
                    self._publish_and_remember(command, result)
                    continue

                self.store.mark_inflight(
                    command.command_id,
                    {
                        "action": command.action,
                        "target": command.target,
                        **({"requested": command.value} if command.value is not None else {}),
                        **(
                            {"requestedSecondsSinceMidnight": command.seconds_since_midnight}
                            if command.seconds_since_midnight is not None
                            else {}
                        ),
                    },
                )
                execution = self.executor.execute(command)
                if command.is_write:
                    self.last_write_monotonic = time.monotonic()

                if command.action != "refresh-gateway-info":
                    self.store.record_mesh_outcome(
                        command_id=command.command_id,
                        action=command.action,
                        target=command.target,
                        response_observed=bool(
                            execution.reported
                            or execution.live_reported
                            or execution.daylight_reported
                        ),
                        complete_no_response=(
                            execution.details.get("failureClass")
                            == "mesh-no-response"
                        ),
                    )

                addresses_to_publish: set[str] = set()
                for address, value in execution.reported.items():
                    if address in self.nodes:
                        self.store.update_node(address, value)
                        addresses_to_publish.add(address)
                for address, live_status in execution.live_reported.items():
                    if address in self.nodes:
                        self.store.update_live(address, live_status)
                        addresses_to_publish.add(address)
                daylight_now = utc_now()
                for address, daylight_status in execution.daylight_reported.items():
                    if address in self.nodes:
                        self.store.update_daylight(
                            address,
                            daylight_status,
                            now=daylight_now,
                        )
                        addresses_to_publish.add(address)

                if command.action == "read-daylight":
                    expected_daylight = (
                        tuple(self.nodes)
                        if command.target == "all"
                        else (command.target,)
                    )
                    execution_errors = execution.details.get("errors", {})
                    for address in expected_daylight:
                        if address not in self.nodes or address in execution.daylight_reported:
                            continue
                        error = "no daylight status was returned"
                        if isinstance(execution_errors, Mapping):
                            candidate = execution_errors.get(address)
                            if isinstance(candidate, str) and candidate:
                                error = candidate
                        self.store.record_daylight_failure(
                            address,
                            error,
                            now=daylight_now,
                        )
                        addresses_to_publish.add(address)

                if command.action == "refresh":
                    expected_live = (
                        tuple(self.nodes)
                        if command.target == "all"
                        else (command.target,)
                    )
                elif command.action in {"sync-clock", "set-clock"}:
                    expected_live = (
                        tuple(self.nodes)
                        if command.target == "all"
                        else (command.target,)
                    )
                elif command.action == "restore-blackout" and execution.ok:
                    # The restore CLI verifies MaxBrightness but does not read live
                    # output. Invalidate every cached live value until the next
                    # refresh, even if parsing the restored percentage summary is
                    # incomplete.
                    expected_live = tuple(self.nodes)
                elif command.is_write:
                    expected_live = tuple(execution.reported)
                else:
                    expected_live = ()
                for address in expected_live:
                    if address in self.nodes and address not in execution.live_reported:
                        self.store.clear_live(address)
                        addresses_to_publish.add(address)

                for address in sorted(addresses_to_publish):
                    try:
                        self.publish_node_state(address)
                    except Exception as exc:
                        print(
                            f"Verified state for {address} stored locally but MQTT "
                            f"state publish failed: {exc}",
                            file=sys.stderr,
                        )

                live_reported = {
                    address: {
                        "lampClockSeconds": (status.lamp_time_ms // 1000) % 86_400,
                        "lampClock": _clock_text(status.lamp_time_ms // 1000),
                        "liveBrightnessRaw": status.brightness_raw,
                        "liveBrightnessPercentEstimate": (
                            status.brightness_percent_estimate
                        ),
                    }
                    for address, status in execution.live_reported.items()
                }
                daylight_reported = {
                    address: status.to_document()
                    for address, status in execution.daylight_reported.items()
                }
                result = make_result(
                    command,
                    ok=execution.ok,
                    status=execution.status,
                    message=execution.message,
                    details={
                        "reported": dict(execution.reported),
                        **({"liveReported": live_reported} if live_reported else {}),
                        **(
                            {"daylightReported": daylight_reported}
                            if daylight_reported
                            else {}
                        ),
                        **dict(execution.details),
                    },
                )
                self._publish_and_remember(command, result)
                print(
                    f"Command {command.command_id} completed: "
                    f"status={execution.status} ok={execution.ok}."
                )
                if command.action != "refresh-gateway-info":
                    sequence_state = self.executor.sender_sequence_state()
                    if sequence_state is not None:
                        self.sequence_state = sequence_state
                try:
                    self.publish_gateway_info()
                except Exception as exc:
                    print(
                        f"Command completed but gateway-info publish failed: {exc}",
                        file=sys.stderr,
                    )
            except Exception as exc:
                result = make_result(
                    command,
                    ok=False,
                    status="gateway-error",
                    message=f"Gateway execution error: {exc}",
                )
                try:
                    self._publish_and_remember(command, result)
                except Exception as publish_exc:
                    print(f"Cannot publish gateway error: {publish_exc}", file=sys.stderr)
            finally:
                self.command_queue.done(original)
                if command.command_id != original.command_id:
                    self.command_queue.done(command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SANlight MQTT edge gateway")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration/CDB and print a redacted summary",
    )
    return parser


def run_gateway(config: GatewayConfig) -> int:
    gateway = SanlightMqttGateway(config)
    transport = PahoMqttTransport(
        config,
        on_message=gateway.on_message,
        on_connected=gateway.on_connected,
        on_disconnected=gateway.on_disconnected,
    )
    gateway.bind_transport(transport)
    gateway.start_workers()

    def stop_handler(signum: int, frame: object) -> None:
        print(f"Received signal {signum}; stopping MQTT gateway.")
        gateway.stop()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    transport.run_forever()
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = build_parser().parse_args(argv)
    try:
        config = load_gateway_config(args.config)
        if args.check:
            gateway = SanlightMqttGateway(config)
            summary = redacted_config_summary(config)
            summary.update(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serviceVersion": SERVICE_VERSION,
                    "meshUuid": gateway.mesh_uuid,
                    "senderAddress": gateway.sender_unicast,
                    "nodes": gateway.nodes,
                }
            )
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            print("Configuration check complete. No key, password or token was printed.")
            return 0
        return run_gateway(config)
    except (
        GatewayConfigError,
        GatewayServiceError,
        GatewayExecutionError,
        MqttTransportError,
        ValueError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
