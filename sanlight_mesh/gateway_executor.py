"""Typed execution backend for the MQTT gateway.

The first gateway release intentionally reuses the already hardware-validated CLI
transaction engine in a child process. ``bluetooth-meshd`` remains persistent; the
child only attaches the local D-Bus application for one serialized transaction.
No shell is involved and MQTT input never becomes an executable path or option.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from .gateway_config import GatewayConfig
from .gateway_protocol import GatewayCommand
from .protocol import DaylightStatus, LiveStatus, decode_daylight_status_pdu


_GET_MAX_RE = re.compile(
    r"GET-MAX COMPLETE\. Node 0x([0-9A-F]{4}) reports MaxBrightness (\d+)%"
)
_GET_LIVE_RE = re.compile(
    r"GET-LIVE COMPLETE\. Node 0x([0-9A-F]{4}) reports "
    r"lampTimeMs=(\d+) lampClock=([0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}) "
    r"liveBrightnessRaw=(\d+) "
    r"liveBrightnessPercentEstimate=([0-9]+(?:\.[0-9]+)?)%\."
)
_GET_DAYLIGHT_RE = re.compile(r"^GET-DAYLIGHT COMPLETE\. (\{.*\})$", re.MULTILINE)
_SEQUENCE_RE = re.compile(
    r"sequenceNumber=(\d+) \(0x[0-9A-Fa-f]+\).*?sequenceRemaining=(\d+)",
    re.DOTALL,
)
_STATUS_VALUE_RE = re.compile(r"from 0x([0-9A-F]{4}): (\d+)%")
_NODE_VALUE_RE = re.compile(
    r"Node 0x([0-9A-F]{4}) (?:already )?reports(?: MaxBrightness)? (\d+)%"
)
_FINAL_VALUE_RE = re.compile(r"0x([0-9A-F]{4})=(\d+)%")


CLOCK_VERIFICATION_TOLERANCE_SECONDS = 5
SECONDS_PER_DAY = 86_400

_MESH_NO_RESPONSE_MARKERS = (
    "GET-LIVE COMPLETE. No SANlight 0x0D status was observed",
    "GET-MAX UNCONFIRMED. BlueZ accepted the read-only query",
    "GET-DAYLIGHT COMPLETE. No SANlight 0x0F or 0x04 daylight status was observed",
)


def _is_mesh_no_response_message(message: str) -> bool:
    return any(marker in message for marker in _MESH_NO_RESPONSE_MARKERS)


def _all_errors_are_mesh_no_response(errors: Mapping[str, object]) -> bool:
    messages: list[str] = []
    for value in errors.values():
        if isinstance(value, Mapping):
            messages.extend(
                nested for nested in value.values() if isinstance(nested, str)
            )
        elif isinstance(value, str):
            messages.append(value)
    return bool(messages) and all(_is_mesh_no_response_message(item) for item in messages)


def _mesh_no_response_details(errors: Mapping[str, object]) -> dict[str, object]:
    return {
        "errors": dict(errors),
        "failureClass": "mesh-no-response",
        "transmissionAccepted": True,
        "recoveryHint": (
            "Capture diagnostics before recovery, then run "
            "'sudo sanlight-gateway recover-mesh'."
        ),
    }


def _local_seconds_since_midnight() -> int:
    now = datetime.now().astimezone()
    return now.hour * 3600 + now.minute * 60 + now.second


def _clock_text(seconds: int) -> str:
    normalized = seconds % SECONDS_PER_DAY
    return f"{normalized // 3600:02d}:{(normalized % 3600) // 60:02d}:{normalized % 60:02d}"


def _circular_clock_difference(reported: int, expected: int) -> int:
    return ((reported - expected + 43_200) % SECONDS_PER_DAY) - 43_200


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def summary(self) -> str:
        lines = [line.strip() for line in (self.stdout + "\n" + self.stderr).splitlines()]
        useful = [line for line in lines if line]
        return useful[-1][:500] if useful else f"process exited {self.exit_code}"


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    status: str
    message: str
    reported: Mapping[str, int]
    details: Mapping[str, object]
    live_reported: Mapping[str, LiveStatus] = field(default_factory=dict)
    daylight_reported: Mapping[str, DaylightStatus] = field(default_factory=dict)


class GatewayExecutionError(RuntimeError):
    pass


class CliCommandExecutor:
    def __init__(self, config: GatewayConfig, node_addresses: Iterable[str]) -> None:
        self.config = config
        self.node_addresses = tuple(sorted(node_addresses))
        self.entrypoint = config.project_root / "sanlight_canonical_sender_poc.py"
        if not self.entrypoint.is_file():
            raise GatewayExecutionError(f"CLI entrypoint not found: {self.entrypoint}")

    def _base_argv(self) -> list[str]:
        return [
            sys.executable,
            str(self.entrypoint),
            "--cdb",
            str(self.config.cdb_path),
            "--control-app-id",
            str(self.config.control_app_id),
            "--sender-app-id",
            str(self.config.sender_app_id),
            "--provisioner-state",
            str(self.config.state_dir / "control-provisioner.json"),
            "--sender-state",
            str(self.config.state_dir / "canonical-sender.json"),
        ]

    def _run(self, arguments: list[str], timeout: int | None = None) -> ProcessResult:
        argv = self._base_argv() + arguments
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            completed = subprocess.run(
                argv,
                cwd=self.config.project_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.config.command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayExecutionError(
                f"command exceeded {timeout or self.config.command_timeout_seconds} seconds"
            ) from exc
        return ProcessResult(completed.returncode, completed.stdout, completed.stderr)

    @staticmethod
    def _parse_get_max(result: ProcessResult) -> tuple[str, int] | None:
        matches = _GET_MAX_RE.findall(result.stdout)
        if not matches:
            return None
        address, value = matches[-1]
        parsed = int(value)
        if not 0 <= parsed <= 100:
            return None
        return address, parsed

    @staticmethod
    def _parse_get_live(result: ProcessResult) -> tuple[str, LiveStatus] | None:
        matches = _GET_LIVE_RE.findall(result.stdout)
        if not matches:
            return None
        address, lamp_time_ms, lamp_clock, brightness_raw, percent_estimate = matches[-1]
        try:
            status = LiveStatus(int(lamp_time_ms), int(brightness_raw))
        except ValueError:
            return None
        if status.lamp_clock != lamp_clock:
            return None
        if abs(status.brightness_percent_estimate - float(percent_estimate)) > 0.05:
            return None
        return address, status

    @staticmethod
    def _parse_get_daylight(
        result: ProcessResult,
    ) -> tuple[str, DaylightStatus] | None:
        matches = _GET_DAYLIGHT_RE.findall(result.stdout)
        if not matches:
            return None
        try:
            document = json.loads(matches[-1])
        except json.JSONDecodeError:
            return None
        if not isinstance(document, dict):
            return None
        address = document.get("address")
        request_opcode = document.get("requestOpcode")
        raw_pdu_hex = document.get("rawPduHex")
        if (
            not isinstance(address, str)
            or re.fullmatch(r"[0-9A-F]{4}", address) is None
            or isinstance(request_opcode, bool)
            or not isinstance(request_opcode, int)
            or not isinstance(raw_pdu_hex, str)
            or len(raw_pdu_hex) % 2 != 0
            or re.fullmatch(r"[0-9a-f]+", raw_pdu_hex) is None
        ):
            return None
        try:
            status = decode_daylight_status_pdu(
                bytes.fromhex(raw_pdu_hex),
                request_opcode=request_opcode,
            )
        except ValueError:
            return None
        if document.get("verified") is not status.parsed:
            return None
        return address, status

    @staticmethod
    def _parse_reported_values(result: ProcessResult) -> dict[str, int]:
        reported: dict[str, int] = {}
        for pattern in (_STATUS_VALUE_RE, _NODE_VALUE_RE, _FINAL_VALUE_RE):
            for address, value in pattern.findall(result.stdout):
                parsed = int(value)
                if 0 <= parsed <= 100:
                    reported[address] = parsed
        return reported

    def refresh(self, target: str) -> ExecutionResult:
        targets = self.node_addresses if target == "all" else (target,)
        reported: dict[str, int] = {}
        live_reported: dict[str, LiveStatus] = {}
        errors: dict[str, dict[str, str]] = {}
        for address in targets:
            max_result = self._run(["get-max", address])
            parsed_max = self._parse_get_max(max_result)
            if (
                max_result.exit_code == 0
                and parsed_max is not None
                and parsed_max[0] == address
            ):
                reported[address] = parsed_max[1]
            else:
                errors.setdefault(address, {})["maxBrightness"] = max_result.summary

            live_result = self._run(["get-live", address])
            parsed_live = self._parse_get_live(live_result)
            if (
                live_result.exit_code == 0
                and parsed_live is not None
                and parsed_live[0] == address
            ):
                live_reported[address] = parsed_live[1]
            else:
                errors.setdefault(address, {})["liveBrightness"] = live_result.summary

        ok = not errors
        any_reported = bool(reported or live_reported)
        complete_no_response = (
            not any_reported
            and bool(errors)
            and _all_errors_are_mesh_no_response(errors)
        )
        if ok:
            status_text = "verified"
            message = (
                "MaxBrightness, live lamp output, and lamp clocks refreshed and verified."
                if target == "all"
                else "MaxBrightness, live lamp output, and lamp clock refreshed and verified."
            )
            details: Mapping[str, object] = {}
        elif complete_no_response:
            status_text = "mesh-no-response"
            message = (
                "BlueZ accepted the read-only Mesh requests, but no selected lamp "
                "returned a status response. The local Bluetooth Mesh transport may "
                "be stale. Capture diagnostics before recovery."
            )
            details = _mesh_no_response_details(errors)
        else:
            status_text = "partial" if any_reported else "failed"
            message = "One or more read-only lamp status requests failed."
            details = {"errors": errors} if errors else {}

        return ExecutionResult(
            ok=ok,
            status=status_text,
            message=message,
            reported=reported,
            live_reported=live_reported,
            details=details,
        )

    def read_daylight(self, target: str) -> ExecutionResult:
        targets = self.node_addresses if target == "all" else (target,)
        daylight_reported: dict[str, DaylightStatus] = {}
        errors: dict[str, str] = {}
        verified_count = 0

        for address in targets:
            process = self._run(
                ["get-daylight", address],
                timeout=max(self.config.command_timeout_seconds, 30),
            )
            parsed = self._parse_get_daylight(process)
            if parsed is None or parsed[0] != address:
                errors[address] = process.summary
                continue
            status = parsed[1]
            daylight_reported[address] = status
            if status.parsed:
                verified_count += 1
            else:
                errors[address] = status.parse_error or "daylight response was raw-only"

        total = len(targets)
        ok = verified_count == total
        if ok:
            message = (
                "Stored daylight configurations read and verified for all selected lamps."
                if target == "all"
                else "Stored daylight configuration read and verified."
            )
            status_text = "verified"
            details: Mapping[str, object] = {}
        elif daylight_reported:
            message = (
                f"Daylight configuration verified for {verified_count} of {total} "
                "selected lamps; raw responses were retained where available."
            )
            status_text = "partial"
            details: Mapping[str, object] = {"errors": errors} if errors else {}
        elif errors and _all_errors_are_mesh_no_response(errors):
            message = (
                "BlueZ accepted the read-only daylight requests, but no selected lamp "
                "returned a status response. The local Bluetooth Mesh transport may "
                "be stale. Capture diagnostics before recovery."
            )
            status_text = "mesh-no-response"
            details = _mesh_no_response_details(errors)
        else:
            message = "No daylight configuration response could be read."
            status_text = "failed"
            details = {"errors": errors} if errors else {}

        return ExecutionResult(
            ok=ok,
            status=status_text,
            message=message,
            reported={},
            live_reported={},
            daylight_reported=daylight_reported,
            details=details,
        )

    def _clock_write(self, command: GatewayCommand) -> ExecutionResult:
        targets = self.node_addresses if command.target == "all" else (command.target,)
        batch_started = time.monotonic()
        base_seconds = command.seconds_since_midnight
        node_details: dict[str, dict[str, object]] = {}
        live_reported: dict[str, LiveStatus] = {}
        verified_count = 0

        for address in targets:
            if command.action == "sync-clock":
                written_seconds = _local_seconds_since_midnight()
            else:
                assert base_seconds is not None
                elapsed = int(time.monotonic() - batch_started)
                written_seconds = (base_seconds + elapsed) % SECONDS_PER_DAY

            write_started = time.monotonic()
            write_result = self._run(["set-uptime", address, str(written_seconds)])
            read_result = self._run(["get-live", address])
            parsed_live = self._parse_get_live(read_result)
            details: dict[str, object] = {
                "writtenSecondsSinceMidnight": written_seconds,
                "writtenClock": _clock_text(written_seconds),
                "writeExitCode": write_result.exit_code,
            }

            if (
                read_result.exit_code == 0
                and parsed_live is not None
                and parsed_live[0] == address
            ):
                status = parsed_live[1]
                live_reported[address] = status
                reported_seconds = (status.lamp_time_ms // 1000) % SECONDS_PER_DAY
                if command.action == "sync-clock":
                    expected_seconds = _local_seconds_since_midnight()
                else:
                    elapsed_after_write = int(time.monotonic() - write_started)
                    expected_seconds = (written_seconds + elapsed_after_write) % SECONDS_PER_DAY
                difference = _circular_clock_difference(
                    reported_seconds, expected_seconds
                )
                details.update(
                    {
                        "reportedSecondsSinceMidnight": reported_seconds,
                        "reportedClock": _clock_text(reported_seconds),
                        "expectedSecondsSinceMidnight": expected_seconds,
                        "verificationDifferenceSeconds": difference,
                    }
                )
                if (
                    write_result.exit_code == 0
                    and abs(difference) <= CLOCK_VERIFICATION_TOLERANCE_SECONDS
                ):
                    details["status"] = "verified"
                    verified_count += 1
                else:
                    details["status"] = "unconfirmed"
                    if write_result.exit_code != 0:
                        details["writeError"] = write_result.summary
            else:
                details["status"] = "unconfirmed"
                details["readbackError"] = read_result.summary
                if write_result.exit_code != 0:
                    details["writeError"] = write_result.summary

            node_details[address] = details

        total = len(targets)
        ok = verified_count == total
        status = "verified" if ok else "partial" if verified_count else "unconfirmed"
        if ok:
            if command.action == "sync-clock":
                message = (
                    "Lamp clocks synchronized with the gateway local clock and verified."
                    if command.target == "all"
                    else "Lamp clock synchronized with the gateway local clock and verified."
                )
            else:
                message = (
                    "Requested lamp clocks applied and verified."
                    if command.target == "all"
                    else "Requested lamp clock applied and verified."
                )
        elif verified_count:
            message = f"Clock write verified for {verified_count} of {total} selected lamps."
        else:
            message = "Clock write could not be verified on any selected lamp."
        return ExecutionResult(
            ok=ok,
            status=status,
            message=message,
            reported={},
            live_reported=live_reported,
            details={
                "nodes": node_details,
                "verificationToleranceSeconds": CLOCK_VERIFICATION_TOLERANCE_SECONDS,
            },
        )

    def execute(self, command: GatewayCommand) -> ExecutionResult:
        if command.action == "refresh-gateway-info":
            return ExecutionResult(
                ok=True,
                status="verified",
                message="Gateway information refreshed without a Mesh operation.",
                reported={},
                live_reported={},
                details={"meshMessagesSent": 0},
            )

        if command.action in {"sync-clock", "set-clock"}:
            return self._clock_write(command)

        if command.action == "read-daylight":
            return self.read_daylight(command.target)

        if command.action == "refresh":
            return self.refresh(command.target)

        if command.action == "set-max":
            assert command.value is not None
            result = self._run(["set-max", command.target, str(command.value)])
            if result.exit_code == 0:
                live_result = self._run(["get-live", command.target])
                parsed_live = self._parse_get_live(live_result)
                live_reported: dict[str, LiveStatus] = {}
                details: dict[str, object] = {"exitCode": result.exit_code}
                if (
                    live_result.exit_code == 0
                    and parsed_live is not None
                    and parsed_live[0] == command.target
                ):
                    live_reported[command.target] = parsed_live[1]
                else:
                    details["liveError"] = live_result.summary
                return ExecutionResult(
                    ok=True,
                    status="verified",
                    message=(
                        f"Node {command.target} reports MaxBrightness "
                        f"{command.value}% as requested."
                    ),
                    reported={command.target: command.value},
                    live_reported=live_reported,
                    details=details,
                )
            return ExecutionResult(
                ok=False,
                status="unconfirmed" if result.exit_code in (3, 4) else "failed",
                message=result.summary,
                reported={},
                live_reported={},
                details={"exitCode": result.exit_code},
            )

        if command.action == "blackout":
            result = self._run(
                ["blackout", command.target, "--confirm-blackout"],
                timeout=max(self.config.command_timeout_seconds, 120),
            )
            if result.exit_code != 0:
                return ExecutionResult(
                    ok=False,
                    status="unconfirmed" if result.exit_code in (3, 4) else "failed",
                    message=result.summary,
                    reported={},
                    live_reported={},
                    details={"exitCode": result.exit_code},
                )
            expected = self.node_addresses if command.target == "all" else (command.target,)
            # CLI exit 0 is reached only after its own per-node GetMaxBrightness
            # verification. Avoid a redundant second MaxBrightness refresh.
            reported = {address: 0 for address in expected}
            return ExecutionResult(
                ok=True,
                status="verified",
                message="All selected nodes report 0% (off).",
                reported=reported,
                live_reported={},
                details={"exitCode": result.exit_code},
            )

        if command.action == "restore-blackout":
            result = self._run(
                ["restore-blackout", "latest", "--confirm-restore"],
                timeout=max(self.config.command_timeout_seconds, 180),
            )
            if result.exit_code != 0:
                return ExecutionResult(
                    ok=False,
                    status="unconfirmed" if result.exit_code in (3, 4) else "failed",
                    message=result.summary,
                    reported={},
                    live_reported={},
                    details={"exitCode": result.exit_code},
                )
            reported = self._parse_reported_values(result)
            return ExecutionResult(
                ok=True,
                status="verified",
                message=(
                    "Latest blackout snapshot restored and verified by the CLI "
                    "transaction engine."
                ),
                reported=reported,
                live_reported={},
                details={"exitCode": result.exit_code},
            )

        raise GatewayExecutionError(f"unsupported action: {command.action}")

    def sender_sequence_state(self) -> dict[str, int] | None:
        result = self._run(["show-sender-state"])
        if result.exit_code != 0:
            return None
        match = _SEQUENCE_RE.search(result.stdout)
        if not match:
            return None
        return {
            "sequenceNumber": int(match.group(1)),
            "sequenceRemaining": int(match.group(2)),
        }
