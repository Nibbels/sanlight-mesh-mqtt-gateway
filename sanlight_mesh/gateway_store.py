"""Persistent non-secret cache for MQTT deduplication and verified node state."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from .gateway_protocol import isoformat_utc, parse_timestamp, utc_now
from .protocol import DaylightStatus, LiveStatus, decode_daylight_status_pdu
from .state import read_state, write_state


STORE_VERSION = 1


@dataclass(frozen=True)
class CachedNodeState:
    max_brightness: int
    verified_at: datetime
    live_status: LiveStatus | None = None
    live_verified_at: datetime | None = None
    daylight_status: DaylightStatus | None = None
    daylight_verified_at: datetime | None = None
    daylight_last_observation: DaylightStatus | None = None
    daylight_last_read_at: datetime | None = None
    daylight_last_read_ok: bool | None = None
    daylight_last_error: str | None = None


class GatewayStore:
    def __init__(
        self,
        path: Path,
        *,
        dedup_ttl_seconds: int,
        dedup_max_entries: int,
    ) -> None:
        self.path = path
        self.dedup_ttl_seconds = dedup_ttl_seconds
        self.dedup_max_entries = dedup_max_entries
        self.document: dict[str, Any] = {
            "version": STORE_VERSION,
            "processed": {},
            "inflight": {},
            "nodes": {},
            "meshHealth": {},
        }
        loaded = read_state(path)
        if loaded is not None:
            self._load_document(loaded)
        self.prune()

    def _load_document(self, value: Mapping[str, Any]) -> None:
        if value.get("version") != STORE_VERSION:
            return
        processed = value.get("processed", {})
        inflight = value.get("inflight", {})
        nodes = value.get("nodes", {})
        mesh_health = value.get("meshHealth", {})
        if (
            isinstance(processed, dict)
            and isinstance(inflight, dict)
            and isinstance(nodes, dict)
        ):
            self.document = {
                "version": STORE_VERSION,
                "processed": dict(processed),
                "inflight": dict(inflight),
                "nodes": dict(nodes),
                "meshHealth": (
                    dict(mesh_health) if isinstance(mesh_health, dict) else {}
                ),
            }

    @property
    def processed(self) -> dict[str, Any]:
        return self.document["processed"]

    @property
    def inflight(self) -> dict[str, Any]:
        return self.document["inflight"]

    @property
    def nodes(self) -> dict[str, Any]:
        return self.document["nodes"]

    @property
    def mesh_health(self) -> dict[str, Any]:
        return self.document["meshHealth"]

    def get_mesh_health(self) -> dict[str, Any]:
        return dict(self.mesh_health)

    def record_mesh_outcome(
        self,
        *,
        command_id: str,
        action: str,
        target: str,
        response_observed: bool,
        complete_no_response: bool,
        now: datetime | None = None,
    ) -> None:
        if not response_observed and not complete_no_response:
            return

        current = now or utc_now()
        health = dict(self.mesh_health)
        if response_observed:
            health.update(
                {
                    "lastSuccessfulResponseAt": isoformat_utc(current),
                    "consecutiveCompleteNoResponseCommands": 0,
                }
            )
            health.pop("lastCompleteNoResponseAt", None)
            health.pop("lastCompleteNoResponseCommand", None)
        elif complete_no_response:
            previous = health.get("consecutiveCompleteNoResponseCommands", 0)
            if isinstance(previous, bool) or not isinstance(previous, int) or previous < 0:
                previous = 0
            health.update(
                {
                    "consecutiveCompleteNoResponseCommands": previous + 1,
                    "lastCompleteNoResponseAt": isoformat_utc(current),
                    "lastCompleteNoResponseCommand": {
                        "id": command_id,
                        "action": action,
                        "target": target,
                    },
                }
            )
        self.document["meshHealth"] = health
        self.save()

    def save(self) -> None:
        write_state(self.path, self.document)

    def prune(self, now: datetime | None = None) -> None:
        current = now or utc_now()
        cutoff = current - timedelta(seconds=self.dedup_ttl_seconds)
        valid: list[tuple[str, dict[str, Any], datetime]] = []
        for command_id, record in list(self.processed.items()):
            if not isinstance(record, dict):
                continue
            try:
                timestamp = parse_timestamp(record.get("completedAt"), "completedAt")
            except Exception:
                continue
            if timestamp >= cutoff and isinstance(record.get("result"), dict):
                valid.append((command_id, record, timestamp))
        valid.sort(key=lambda item: item[2], reverse=True)
        self.document["processed"] = {
            command_id: record
            for command_id, record, _ in valid[: self.dedup_max_entries]
        }

    def get_result(self, command_id: str) -> dict[str, Any] | None:
        record = self.processed.get(command_id)
        if not isinstance(record, dict):
            return None
        result = record.get("result")
        return dict(result) if isinstance(result, dict) else None

    def remember_result(
        self,
        command_id: str,
        result: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        self.processed[command_id] = {
            "completedAt": isoformat_utc(current),
            "result": dict(result),
        }
        self.inflight.pop(command_id, None)
        self.prune(current)
        self.save()


    def mark_inflight(
        self,
        command_id: str,
        command: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        self.inflight[command_id] = {
            "startedAt": isoformat_utc(current),
            "command": dict(command),
        }
        self.save()

    def get_inflight(self, command_id: str) -> dict[str, Any] | None:
        value = self.inflight.get(command_id)
        return dict(value) if isinstance(value, dict) else None

    def update_node(
        self,
        address: str,
        max_brightness: int,
        *,
        now: datetime | None = None,
    ) -> None:
        if not 0 <= max_brightness <= 100:
            raise ValueError("cached MaxBrightness must be between 0 and 100")
        current = now or utc_now()
        existing = self.nodes.get(address)
        record = dict(existing) if isinstance(existing, dict) else {}
        record.update(
            {
                "maxBrightness": max_brightness,
                "verifiedAt": isoformat_utc(current),
            }
        )
        self.nodes[address] = record
        self.save()

    def update_live(
        self,
        address: str,
        status: LiveStatus,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        existing = self.nodes.get(address)
        record = dict(existing) if isinstance(existing, dict) else {}
        record.update(
            {
                "lampTimeMs": status.lamp_time_ms,
                "liveBrightnessRaw": status.brightness_raw,
                "liveVerifiedAt": isoformat_utc(current),
            }
        )
        self.nodes[address] = record
        self.save()

    def clear_live(self, address: str) -> None:
        existing = self.nodes.get(address)
        if not isinstance(existing, dict):
            return
        record = dict(existing)
        changed = False
        for key in ("lampTimeMs", "liveBrightnessRaw", "liveVerifiedAt"):
            if key in record:
                record.pop(key)
                changed = True
        if changed:
            self.nodes[address] = record
            self.save()

    @staticmethod
    def _decode_stored_daylight(
        record: Mapping[str, Any],
        *,
        request_key: str,
        raw_key: str,
    ) -> DaylightStatus | None:
        request_opcode = record.get(request_key)
        raw_pdu_hex = record.get(raw_key)
        if (
            isinstance(request_opcode, bool)
            or not isinstance(request_opcode, int)
            or not isinstance(raw_pdu_hex, str)
        ):
            return None
        try:
            raw_pdu = bytes.fromhex(raw_pdu_hex)
            return decode_daylight_status_pdu(
                raw_pdu,
                request_opcode=request_opcode,
            )
        except ValueError:
            return None

    def update_daylight(
        self,
        address: str,
        status: DaylightStatus,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        existing = self.nodes.get(address)
        record = dict(existing) if isinstance(existing, dict) else {}
        record.update(
            {
                "daylightLastReadAt": isoformat_utc(current),
                "daylightLastReadOk": status.parsed,
                "daylightObservationRequestOpcode": status.request_opcode,
                "daylightObservationRawPduHex": status.raw_pdu.hex(),
            }
        )
        if status.parsed:
            record.update(
                {
                    "daylightVerifiedAt": isoformat_utc(current),
                    "daylightVerifiedRequestOpcode": status.request_opcode,
                    "daylightVerifiedRawPduHex": status.raw_pdu.hex(),
                }
            )
            record.pop("daylightLastError", None)
        else:
            record["daylightLastError"] = (
                status.parse_error or "daylight response could not be parsed"
            )[:1000]
        self.nodes[address] = record
        self.save()

    def record_daylight_failure(
        self,
        address: str,
        error: str,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        existing = self.nodes.get(address)
        record = dict(existing) if isinstance(existing, dict) else {}
        record.update(
            {
                "daylightLastReadAt": isoformat_utc(current),
                "daylightLastReadOk": False,
                "daylightLastError": str(error)[:1000],
            }
        )
        record.pop("daylightObservationRequestOpcode", None)
        record.pop("daylightObservationRawPduHex", None)
        self.nodes[address] = record
        self.save()

    def get_node(self, address: str) -> CachedNodeState | None:
        record = self.nodes.get(address)
        if not isinstance(record, dict):
            return None
        value = record.get("maxBrightness")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            return None
        try:
            verified_at = parse_timestamp(record.get("verifiedAt"), "verifiedAt")
        except Exception:
            return None

        live_status: LiveStatus | None = None
        live_verified_at: datetime | None = None
        lamp_time_ms = record.get("lampTimeMs")
        brightness_raw = record.get("liveBrightnessRaw")
        if (
            not isinstance(lamp_time_ms, bool)
            and isinstance(lamp_time_ms, int)
            and 0 <= lamp_time_ms <= 0xFFFFFFFF
            and not isinstance(brightness_raw, bool)
            and isinstance(brightness_raw, int)
            and 0 <= brightness_raw <= 0xFFFF
        ):
            try:
                live_verified_at = parse_timestamp(
                    record.get("liveVerifiedAt"), "liveVerifiedAt"
                )
            except Exception:
                live_verified_at = None
            if live_verified_at is not None:
                live_status = LiveStatus(lamp_time_ms, brightness_raw)

        daylight_status = self._decode_stored_daylight(
            record,
            request_key="daylightVerifiedRequestOpcode",
            raw_key="daylightVerifiedRawPduHex",
        )
        daylight_verified_at: datetime | None = None
        if daylight_status is not None and daylight_status.parsed:
            try:
                daylight_verified_at = parse_timestamp(
                    record.get("daylightVerifiedAt"), "daylightVerifiedAt"
                )
            except Exception:
                daylight_verified_at = None
        if daylight_verified_at is None:
            daylight_status = None

        daylight_last_observation = self._decode_stored_daylight(
            record,
            request_key="daylightObservationRequestOpcode",
            raw_key="daylightObservationRawPduHex",
        )
        daylight_last_read_at: datetime | None = None
        try:
            daylight_last_read_at = parse_timestamp(
                record.get("daylightLastReadAt"), "daylightLastReadAt"
            )
        except Exception:
            daylight_last_read_at = None
        daylight_last_read_ok = record.get("daylightLastReadOk")
        if not isinstance(daylight_last_read_ok, bool):
            daylight_last_read_ok = None
        daylight_last_error = record.get("daylightLastError")
        if not isinstance(daylight_last_error, str):
            daylight_last_error = None

        return CachedNodeState(
            value,
            verified_at,
            live_status=live_status,
            live_verified_at=live_verified_at,
            daylight_status=daylight_status,
            daylight_verified_at=daylight_verified_at,
            daylight_last_observation=daylight_last_observation,
            daylight_last_read_at=daylight_last_read_at,
            daylight_last_read_ok=daylight_last_read_ok,
            daylight_last_error=daylight_last_error,
        )

    def node_is_fresh(
        self,
        address: str,
        *,
        fresh_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        state = self.get_node(address)
        if state is None:
            return False
        current = now or utc_now()
        return current - state.verified_at <= timedelta(seconds=fresh_seconds)
