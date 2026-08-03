"""Conservative watchdog for the known BlueZ Mesh TX-stall failure mode.

The watchdog never retries application write commands. It reacts only to a
persisted, complete all-node read failure, stops the MQTT worker, and performs an
independent read-only probe while observing the PL011 UART TX counter. Recovery
is allowed only when BlueZ accepted the probe, no lamp status arrived, and the
UART transmitted zero bytes. That signature matches the captured BlueZ stall
while avoiding service restarts for ordinary RF loss or powered-off lamps.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

WATCHDOG_STATE_VERSION = 1
GATEWAY_SERVICE = "sanlight-mqtt-gateway.service"
MESH_SERVICE = "sanlight-meshd-generic.service"
READ_ONLY_ACTIONS = frozenset({"refresh", "read-daylight"})

_ACCEPTED_MARKER = "accepted for Mesh transmission"
_RESPONSE_MARKER = "GET-LIVE COMPLETE. Node 0x"
_NO_RESPONSE_MARKER = "GET-LIVE COMPLETE. No SANlight 0x0D status was observed"
_TX_RE = re.compile(r"(?:^|\s)tx:(\d+)(?:\s|$)")


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool = True
    incident_max_age_seconds: int = 3600
    recent_success_seconds: int = 86400
    probe_timeout_seconds: int = 50
    probe_cooldown_seconds: int = 3600
    recovery_cooldown_seconds: int = 1800
    recovery_window_seconds: int = 21600
    max_recoveries_in_window: int = 2
    verification_timeout_seconds: int = 60


@dataclass(frozen=True)
class ProbeAssessment:
    outcome: str
    confirmed_stall: bool
    transmission_accepted: bool
    response_observed: bool
    no_response_observed: bool
    uart_tx_before: int | None
    uart_tx_after: int | None
    uart_tx_delta: int | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _strict_bool(table: Mapping[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"watchdog.{key} must be true or false")
    return value


def _strict_int(
    table: Mapping[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"watchdog.{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(
            f"watchdog.{key} must be between {minimum} and {maximum} inclusive"
        )
    return value


def load_watchdog_config(path: Path) -> WatchdogConfig:
    try:
        root = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot read watchdog configuration from {path}: {exc}") from exc
    raw = root.get("watchdog", {})
    if not isinstance(raw, dict):
        raise ValueError("[watchdog] must be a TOML table")
    allowed = {
        "enabled",
        "incident_max_age_seconds",
        "recent_success_seconds",
        "probe_timeout_seconds",
        "probe_cooldown_seconds",
        "recovery_cooldown_seconds",
        "recovery_window_seconds",
        "max_recoveries_in_window",
        "verification_timeout_seconds",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown watchdog setting(s): {', '.join(unknown)}")
    return WatchdogConfig(
        enabled=_strict_bool(raw, "enabled", True),
        incident_max_age_seconds=_strict_int(
            raw, "incident_max_age_seconds", 3600, 60, 86400
        ),
        recent_success_seconds=_strict_int(
            raw, "recent_success_seconds", 86400, 300, 604800
        ),
        probe_timeout_seconds=_strict_int(
            raw, "probe_timeout_seconds", 50, 20, 180
        ),
        probe_cooldown_seconds=_strict_int(
            raw, "probe_cooldown_seconds", 3600, 60, 86400
        ),
        recovery_cooldown_seconds=_strict_int(
            raw, "recovery_cooldown_seconds", 1800, 60, 86400
        ),
        recovery_window_seconds=_strict_int(
            raw, "recovery_window_seconds", 21600, 3600, 86400
        ),
        max_recoveries_in_window=_strict_int(
            raw, "max_recoveries_in_window", 2, 1, 20
        ),
        verification_timeout_seconds=_strict_int(
            raw, "verification_timeout_seconds", 60, 20, 180
        ),
    )


def parse_uart_tx(text: str) -> int | None:
    """Return the PL011 TX total from a tty driver status file."""
    preferred: list[int] = []
    fallback: list[int] = []
    for line in text.splitlines():
        match = _TX_RE.search(line)
        if match is None:
            continue
        value = int(match.group(1))
        fallback.append(value)
        if "uart:PL011" in line:
            preferred.append(value)
    values = preferred or fallback
    return sum(values) if values else None


def read_uart_tx(paths: Sequence[Path] | None = None) -> int | None:
    candidates = paths or (
        Path("/proc/tty/driver/ttyAMA"),
        Path("/proc/tty/driver/serial"),
    )
    for path in candidates:
        try:
            parsed = parse_uart_tx(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if parsed is not None:
            return parsed
    return None


def assess_probe(output: str, tx_before: int | None, tx_after: int | None) -> ProbeAssessment:
    accepted = _ACCEPTED_MARKER in output
    response = _RESPONSE_MARKER in output and "reports" in output
    no_response = _NO_RESPONSE_MARKER in output
    delta = None
    if tx_before is not None and tx_after is not None and tx_after >= tx_before:
        delta = tx_after - tx_before

    confirmed = accepted and no_response and not response and delta == 0
    if response:
        outcome = "response-observed"
    elif confirmed:
        outcome = "confirmed-tx-stall"
    elif accepted and no_response and delta is not None and delta > 0:
        outcome = "hci-tx-observed-no-lamp-response"
    elif tx_before is None or tx_after is None:
        outcome = "uart-counter-unavailable"
    else:
        outcome = "inconclusive"
    return ProbeAssessment(
        outcome=outcome,
        confirmed_stall=confirmed,
        transmission_accepted=accepted,
        response_observed=response,
        no_response_observed=no_response,
        uart_tx_before=tx_before,
        uart_tx_after=tx_after,
        uart_tx_delta=delta,
    )


def eligible_incident(
    mesh_health: Mapping[str, Any],
    watchdog_state: Mapping[str, Any],
    config: WatchdogConfig,
    *,
    now: datetime,
) -> tuple[bool, str]:
    count = mesh_health.get("consecutiveCompleteNoResponseCommands", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return False, "no complete Mesh no-response is recorded"

    command = mesh_health.get("lastCompleteNoResponseCommand")
    if not isinstance(command, Mapping):
        return False, "last no-response command metadata is missing"
    if command.get("target") != "all":
        return False, "last no-response did not cover all configured lamps"
    if command.get("action") not in READ_ONLY_ACTIONS:
        return False, "last no-response was not a supported read-only action"

    failed_at = _parse_timestamp(mesh_health.get("lastCompleteNoResponseAt"))
    successful_at = _parse_timestamp(mesh_health.get("lastSuccessfulResponseAt"))
    if failed_at is None:
        return False, "last no-response timestamp is invalid"
    if successful_at is None:
        return False, "no earlier verified Mesh response is recorded"
    if failed_at > now + timedelta(seconds=30):
        return False, "last no-response timestamp is in the future"
    if now - failed_at > timedelta(seconds=config.incident_max_age_seconds):
        return False, "last no-response incident is too old"
    if failed_at - successful_at > timedelta(seconds=config.recent_success_seconds):
        return False, "the last verified response is too old for automatic recovery"

    last_probe = _parse_timestamp(watchdog_state.get("lastProbeAt"))
    if last_probe is not None and now - last_probe < timedelta(
        seconds=config.probe_cooldown_seconds
    ):
        return False, "watchdog probe cooldown is active"

    last_recovery = _parse_timestamp(watchdog_state.get("lastRecoveryAt"))
    if last_recovery is not None and now - last_recovery < timedelta(
        seconds=config.recovery_cooldown_seconds
    ):
        return False, "automatic recovery cooldown is active"

    history = recovery_history(watchdog_state, now, config.recovery_window_seconds)
    if len(history) >= config.max_recoveries_in_window:
        return False, "automatic recovery attempt limit is reached"
    return True, "eligible complete all-node read failure"


def recovery_history(
    state: Mapping[str, Any], now: datetime, window_seconds: int
) -> list[datetime]:
    values = state.get("recoveryHistory", [])
    if not isinstance(values, list):
        return []
    cutoff = now - timedelta(seconds=window_seconds)
    result: list[datetime] = []
    for value in values:
        parsed = _parse_timestamp(value)
        if parsed is not None and cutoff <= parsed <= now + timedelta(seconds=30):
            result.append(parsed)
    return sorted(result)


def _run(
    argv: Sequence[str], *, timeout: int, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
        timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1"},
    )


def _service_active(name: str) -> bool:
    return _run(
        ("/usr/bin/systemctl", "is-active", "--quiet", name), timeout=10
    ).returncode == 0


def _wait_service_state(name: str, active: bool, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _service_active(name) is active:
            return True
        time.sleep(0.5)
    return _service_active(name) is active


def _wait_sender_exit(timeout_seconds: int = 20) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = _run(
            ("/usr/bin/pgrep", "-f", "[s]anlight_canonical_sender_poc.py"),
            timeout=5,
        )
        if result.returncode != 0:
            return True
        time.sleep(0.5)
    return False


def _probe_argv(config: Any, node: str) -> tuple[str, ...]:
    return (
        sys.executable,
        str(config.project_root / "sanlight_canonical_sender_poc.py"),
        "--cdb",
        str(config.cdb_path),
        "--control-app-id",
        str(config.control_app_id),
        "--sender-app-id",
        str(config.sender_app_id),
        "--provisioner-state",
        str(config.state_dir / "control-provisioner.json"),
        "--sender-state",
        str(config.state_dir / "canonical-sender.json"),
        "get-live",
        node,
    )


def _management_argv(config: Any) -> tuple[str, ...]:
    installed = Path("/usr/local/sbin/sanlight-gateway")
    if installed.is_file():
        return (str(installed), "--config", str(config.config_path), "recover-mesh")
    helper = config.project_root / "scripts" / "sanlight-gateway"
    return (
        "/usr/bin/bash",
        str(helper),
        "--config",
        str(config.config_path),
        "recover-mesh",
    )


def _read_document(path: Path) -> dict[str, Any]:
    from .state import read_state

    document = read_state(path)
    return dict(document) if isinstance(document, dict) else {}


def _write_document(path: Path, document: Mapping[str, Any]) -> None:
    from .state import write_state

    write_state(path, document)


def _last_success(path: Path) -> datetime | None:
    health = _read_document(path).get("meshHealth", {})
    if not isinstance(health, Mapping):
        return None
    return _parse_timestamp(health.get("lastSuccessfulResponseAt"))


def _wait_for_new_success(path: Path, after: datetime, timeout_seconds: int) -> datetime | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        observed = _last_success(path)
        if observed is not None and observed >= after:
            return observed
        time.sleep(1)
    return None


def run_watchdog(config_path: Path) -> int:
    from .cdb import load_mesh_material
    from .gateway_config import load_gateway_config

    settings = load_watchdog_config(config_path)
    if not settings.enabled:
        return 0

    gateway = load_gateway_config(config_path)
    state_path = gateway.state_dir / "mqtt-gateway-state.json"
    watchdog_path = gateway.state_dir / "mesh-watchdog-state.json"
    lock_path = gateway.state_dir / "mesh-watchdog.lock"
    gateway.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        now = _utc_now()
        gateway_document = _read_document(state_path)
        mesh_health = gateway_document.get("meshHealth", {})
        if not isinstance(mesh_health, Mapping):
            return 0
        watchdog_state = _read_document(watchdog_path)
        eligible, reason = eligible_incident(
            mesh_health, watchdog_state, settings, now=now
        )
        if not eligible:
            return 0
        if not _service_active(GATEWAY_SERVICE):
            print("Mesh watchdog skipped: MQTT gateway service is not active.")
            return 0
        if not _service_active(MESH_SERVICE):
            print("Mesh watchdog skipped: BlueZ Mesh service is not active.")
            return 0

        control = load_mesh_material(gateway.cdb_path, gateway.control_app_id)
        nodes = sorted(control.sanlight_nodes)
        if not nodes:
            print("Mesh watchdog skipped: no SANlight nodes are present in the CDB.")
            return 0
        node = f"{nodes[0]:04X}"
        failed_at = str(mesh_health.get("lastCompleteNoResponseAt", ""))
        print(
            f"Mesh watchdog investigating {reason}; running an isolated read-only "
            f"probe against node {node}."
        )

        stopped_gateway = False
        assessment: ProbeAssessment | None = None
        try:
            stop = _run(
                ("/usr/bin/systemctl", "stop", GATEWAY_SERVICE), timeout=30
            )
            if stop.returncode != 0 or not _wait_service_state(
                GATEWAY_SERVICE, False, 10
            ):
                print("Mesh watchdog could not stop the MQTT gateway safely.")
                return 1
            stopped_gateway = True
            if not _wait_sender_exit():
                print("Mesh watchdog found a remaining Mesh sender process; probe aborted.")
                return 1

            tx_before = read_uart_tx()
            if tx_before is None:
                assessment = assess_probe("", None, None)
            else:
                try:
                    probe = _run(
                        _probe_argv(gateway, node),
                        timeout=settings.probe_timeout_seconds,
                    )
                    output = probe.stdout or ""
                except subprocess.TimeoutExpired:
                    output = ""
                tx_after = read_uart_tx()
                assessment = assess_probe(output, tx_before, tx_after)

            probe_at = _utc_now()
            history = recovery_history(
                watchdog_state, probe_at, settings.recovery_window_seconds
            )
            watchdog_state.update(
                {
                    "version": WATCHDOG_STATE_VERSION,
                    "lastProbeAt": _isoformat(probe_at),
                    "lastProbeFailureAt": failed_at,
                    "lastProbeNode": node,
                    "lastProbeOutcome": assessment.outcome,
                    "lastProbeTransmissionAccepted": assessment.transmission_accepted,
                    "lastProbeResponseObserved": assessment.response_observed,
                    "lastProbeUartTxBefore": assessment.uart_tx_before,
                    "lastProbeUartTxAfter": assessment.uart_tx_after,
                    "lastProbeUartTxDelta": assessment.uart_tx_delta,
                    "recoveryHistory": [_isoformat(item) for item in history],
                }
            )
            _write_document(watchdog_path, watchdog_state)

            if not assessment.confirmed_stall:
                print(
                    "Mesh watchdog did not confirm the BlueZ TX-stall signature: "
                    f"{assessment.outcome}; no Mesh-daemon restart was performed."
                )
                return 0

            recovery_started = _utc_now()
            history.append(recovery_started)
            watchdog_state.update(
                {
                    "lastRecoveryAt": _isoformat(recovery_started),
                    "lastRecoveryReason": "accepted read-only probe with zero UART TX",
                    "lastRecoveryOk": False,
                    "recoveryHistory": [_isoformat(item) for item in history],
                }
            )
            _write_document(watchdog_path, watchdog_state)
            print(
                "Mesh watchdog confirmed the captured BlueZ TX-stall signature; "
                "restarting the local Mesh transport."
            )
            recovery = _run(_management_argv(gateway), timeout=120)
            if recovery.returncode != 0:
                print("Mesh watchdog recovery command failed.")
                return 1
            stopped_gateway = False
            verified_at = _wait_for_new_success(
                state_path,
                recovery_started,
                settings.verification_timeout_seconds,
            )
            watchdog_state["lastRecoveryOk"] = verified_at is not None
            watchdog_state["lastRecoveryVerifiedAt"] = (
                _isoformat(verified_at) if verified_at is not None else None
            )
            _write_document(watchdog_path, watchdog_state)
            if verified_at is None:
                print(
                    "Mesh watchdog restarted the transport, but no new verified lamp "
                    "response arrived before the verification timeout."
                )
                return 1
            print(
                "Mesh watchdog recovery verified by a new read-only lamp response at "
                f"{_isoformat(verified_at)}."
            )
            return 0
        finally:
            if stopped_gateway and not _service_active(GATEWAY_SERVICE):
                _run(("/usr/bin/systemctl", "start", GATEWAY_SERVICE), timeout=30)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Conservative SANlight BlueZ Mesh TX-stall watchdog"
    )
    parser.add_argument("--config", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_watchdog(args.config.expanduser().resolve())
    except Exception as exc:
        print(f"Mesh watchdog error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
