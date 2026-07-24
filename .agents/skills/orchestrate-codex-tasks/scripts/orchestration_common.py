#!/usr/bin/env python3
"""Shared deterministic helpers for the orchestration scripts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

PROTOCOL_VERSION = 2
LEDGER_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 256 * 1024

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
WORKER_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
UNRESOLVED_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")

SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)
BANNED_KEY_PARTS = (
    "password",
    "token",
    "secret",
    "privatekey",
    "credential",
    "cookie",
    "authorization",
)


class OrchestrationError(Exception):
    """Machine-readable validation or state error."""

    def __init__(self, code: str, message: str, details: Any | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_json_file(path: str | Path, *, require_object: bool = True) -> Any:
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise OrchestrationError("INPUT_UNAVAILABLE", f"Cannot read JSON input: {file_path}") from exc
    if size > MAX_JSON_BYTES:
        raise OrchestrationError(
            "INPUT_TOO_LARGE",
            f"JSON input exceeds {MAX_JSON_BYTES} bytes",
            {"path": str(file_path), "size": size},
        )
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestrationError("INVALID_JSON", f"Invalid JSON input: {file_path}") from exc
    if require_object and not isinstance(value, dict):
        raise OrchestrationError("INVALID_JSON", "JSON input must be an object")
    validate_safe_data(value)
    return value


def write_json_file(path: str | Path, value: Any, *, overwrite: bool = False) -> Path:
    output = Path(path)
    if output.exists() and not overwrite:
        raise OrchestrationError("OUTPUT_EXISTS", f"Refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    output.write_text(encoded, encoding="utf-8")
    try:
        output.chmod(0o600)
    except OSError:
        pass
    return output


def validate_safe_data(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise OrchestrationError("INVALID_FIELD", f"Non-string key at {path}")
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if any(part in normalized for part in BANNED_KEY_PARTS):
                raise OrchestrationError(
                    "SENSITIVE_FIELD",
                    f"Sensitive field is not allowed at {path}.{key}",
                )
            validate_safe_data(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            validate_safe_data(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 64 * 1024:
            raise OrchestrationError("VALUE_TOO_LARGE", f"String is too large at {path}")
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                raise OrchestrationError("SENSITIVE_VALUE", f"Secret-like value detected at {path}")
        return
    if value is not None and not isinstance(value, (bool, int, float)):
        raise OrchestrationError("INVALID_FIELD", f"Unsupported JSON value at {path}")


def validate_identifier(value: str, kind: str) -> str:
    patterns = {
        "runId": RUN_ID_RE,
        "workerId": WORKER_ID_RE,
        "taskId": TASK_ID_RE,
    }
    pattern = patterns[kind]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise OrchestrationError("INVALID_IDENTIFIER", f"Invalid {kind}: {value!r}")
    return value


def require_text(value: Any, field: str, *, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestrationError("INVALID_FIELD", f"{field} must be non-empty text")
    if len(value) > maximum:
        raise OrchestrationError("VALUE_TOO_LARGE", f"{field} exceeds {maximum} characters")
    return value.strip()


def require_string_list(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = 64,
) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be a list of non-empty strings")
    if not minimum <= len(value) <= maximum:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} must contain between {minimum} and {maximum} items",
        )
    return [item.strip() for item in value]


def normalize_boundary(boundary: str) -> str:
    raw = boundary.strip().replace("\\", "/")
    if not raw:
        raise OrchestrationError("INVALID_BOUNDARY", "Write boundary cannot be empty")
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        raise OrchestrationError("INVALID_BOUNDARY", f"Unsafe write boundary: {boundary}")
    normalized = str(path)
    if normalized in (".", ""):
        raise OrchestrationError("INVALID_BOUNDARY", f"Unsafe write boundary: {boundary}")
    return normalized


def boundary_prefix(boundary: str) -> tuple[str, ...]:
    normalized = normalize_boundary(boundary)
    parts: list[str] = []
    for part in PurePosixPath(normalized).parts:
        if any(character in part for character in "*?["):
            break
        parts.append(part)
    return tuple(parts)


def boundaries_overlap(left: str, right: str) -> bool:
    left_prefix = boundary_prefix(left)
    right_prefix = boundary_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    shorter = min(len(left_prefix), len(right_prefix))
    return left_prefix[:shorter] == right_prefix[:shorter]


def find_boundary_conflicts(
    worker_boundaries: dict[str, Iterable[str]],
    allowed_pairs: Iterable[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    allowed = {tuple(sorted(pair)) for pair in allowed_pairs}
    worker_ids = sorted(worker_boundaries)
    conflicts: list[dict[str, str]] = []
    for index, left_worker in enumerate(worker_ids):
        for right_worker in worker_ids[index + 1 :]:
            if tuple(sorted((left_worker, right_worker))) in allowed:
                continue
            for left_boundary in worker_boundaries[left_worker]:
                for right_boundary in worker_boundaries[right_worker]:
                    if boundaries_overlap(left_boundary, right_boundary):
                        conflicts.append(
                            {
                                "leftWorkerId": left_worker,
                                "leftBoundary": left_boundary,
                                "rightWorkerId": right_worker,
                                "rightBoundary": right_boundary,
                            }
                        )
    return conflicts


def result(ok: bool, code: str, **payload: Any) -> dict[str, Any]:
    return {"ok": ok, "code": code, **payload}


def print_result(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
