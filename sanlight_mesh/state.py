"""Secure local BlueZ token state handling."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


class StateError(RuntimeError):
    """Raised for unsafe, corrupt or mismatching local state."""


def _has_posix_permission_bits() -> bool:
    """Return whether chmod-style owner/group/other bits are meaningful."""
    return os.name == "posix"


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _has_posix_permission_bits():
        return
    try:
        mode = path.stat().st_mode & 0o777
        if mode != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise StateError(f"Cannot secure state directory {path}: {exc}") from exc


def write_state(path: Path, state: Mapping[str, Any]) -> None:
    """Atomically write JSON state with directory 0700 and file 0600."""
    _ensure_private_directory(path.parent)
    old_umask = os.umask(0o077)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            if _has_posix_permission_bits():
                os.fchmod(fd, 0o600)
            payload = (json.dumps(dict(state), indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            if _has_posix_permission_bits():
                os.chmod(path, 0o600)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            except (AttributeError, OSError):
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise StateError(f"Cannot write state file {path}: {exc}") from exc
    finally:
        os.umask(old_umask)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        if _has_posix_permission_bits():
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                raise StateError(
                    f"State file {path} is too broadly accessible (mode {mode:04o}); "
                    "run chmod 600 on it"
                )
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"State file {path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise StateError(f"Cannot read state file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"State file {path} must contain a JSON object")
    return value


def token_from_state(state: Mapping[str, Any], label: str) -> int | None:
    raw = state.get("token")
    if raw in (None, ""):
        return None
    try:
        token = int(str(raw), 16)
    except ValueError as exc:
        raise StateError(f"{label} state contains no valid token") from exc
    if not 0 <= token <= 0xFFFFFFFFFFFFFFFF:
        raise StateError(f"{label} token is outside the uint64 range")
    return token


def validate_state_identity(
    state: Mapping[str, Any], expected: Mapping[str, Any], label: str
) -> None:
    for key, value in expected.items():
        if state.get(key) != value:
            raise StateError(
                f"{label} state identity mismatch for {key}; "
                "the local state belongs to a different CDB or identity"
            )
