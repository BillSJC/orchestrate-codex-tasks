#!/usr/bin/env python3
"""Shared deterministic helpers for the orchestration scripts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
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


def configure_utf8_stdio() -> None:
    """Keep CLI output independent of the host's legacy default code page."""
    for stream, errors in (
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


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


def normalize_failure_policy(
    value: Any,
    field: str = "failurePolicy",
) -> dict[str, int]:
    """Validate the bounded local correction budget for recoverable control errors."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be an object")
    unknown = sorted(set(value) - {"localCorrectionBudget"})
    if unknown:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} contains unsupported fields",
            {"fields": unknown},
        )
    budget = value.get("localCorrectionBudget", 1)
    if (
        not isinstance(budget, int)
        or isinstance(budget, bool)
        or not 0 <= budget <= 2
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field}.localCorrectionBudget must be an integer from 0 to 2",
        )
    return {"localCorrectionBudget": budget}


def normalize_step_contracts(
    value: Any,
    field: str = "stepContracts",
) -> list[dict[str, Any]]:
    """Validate per-step result contracts for a bounded REPLAN batch."""
    if value is None:
        return []
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} must be a list containing 1 to 32 contracts",
        )
    normalized: list[dict[str, Any]] = []
    allowed = {
        "step",
        "acceptedExitCodes",
        "expectedFailureSignature",
        "timeoutSeconds",
        "partialWriteCheck",
    }
    for index, contract in enumerate(value):
        item_field = f"{field}[{index}]"
        if not isinstance(contract, dict):
            raise OrchestrationError("INVALID_FIELD", f"{item_field} must be an object")
        unknown = sorted(set(contract) - allowed)
        if unknown:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{item_field} contains unsupported fields",
                {"fields": unknown},
            )
        exit_codes = contract.get("acceptedExitCodes")
        if not isinstance(exit_codes, list) or not exit_codes or len(exit_codes) > 16:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{item_field}.acceptedExitCodes must contain 1 to 16 exit codes",
            )
        if any(
            not isinstance(code, int)
            or isinstance(code, bool)
            or code < -2147483648
            or code > 4294967295
            for code in exit_codes
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                (
                    f"{item_field}.acceptedExitCodes must contain signed or unsigned "
                    "32-bit process exit codes"
                ),
            )
        if len(exit_codes) != len(set(exit_codes)):
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{item_field}.acceptedExitCodes must not contain duplicates",
            )
        timeout_seconds = contract.get("timeoutSeconds")
        if (
            not isinstance(timeout_seconds, int)
            or isinstance(timeout_seconds, bool)
            or not 1 <= timeout_seconds <= 86400
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{item_field}.timeoutSeconds must be an integer from 1 to 86400",
            )
        expected_signature = contract.get("expectedFailureSignature")
        if expected_signature is not None:
            expected_signature = require_text(
                expected_signature,
                f"{item_field}.expectedFailureSignature",
                maximum=2048,
            )
        normalized.append(
            {
                "step": require_text(contract.get("step"), f"{item_field}.step"),
                "acceptedExitCodes": sorted(exit_codes),
                "expectedFailureSignature": expected_signature,
                "timeoutSeconds": timeout_seconds,
                "partialWriteCheck": require_text(
                    contract.get("partialWriteCheck"),
                    f"{item_field}.partialWriteCheck",
                ),
            }
        )
    return normalized


def normalize_execution_plan(value: Any, field: str = "executionPlan") -> dict[str, Any]:
    """Validate a legacy bounded batch retained for recovery compatibility."""
    if not isinstance(value, dict):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be an object")
    allowed = {
        "steps",
        "stopOnFirstNonzero",
        "stopOnTimeout",
        "maxWallTimeMinutes",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} contains unsupported fields",
            {"fields": unknown},
        )
    steps = require_string_list(
        value.get("steps"),
        f"{field}.steps",
        minimum=1,
        maximum=32,
    )
    stop_on_first_nonzero = value.get("stopOnFirstNonzero")
    stop_on_timeout = value.get("stopOnTimeout")
    if stop_on_first_nonzero is not True or stop_on_timeout is not True:
        raise OrchestrationError(
            "INVALID_FIELD",
            (
                f"{field}.stopOnFirstNonzero and {field}.stopOnTimeout "
                "must both be true"
            ),
        )
    max_wall_time = value.get("maxWallTimeMinutes")
    if (
        not isinstance(max_wall_time, int)
        or isinstance(max_wall_time, bool)
        or not 1 <= max_wall_time <= 1440
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field}.maxWallTimeMinutes must be an integer between 1 and 1440",
        )
    return {
        "steps": steps,
        "stopOnFirstNonzero": True,
        "stopOnTimeout": True,
        "maxWallTimeMinutes": max_wall_time,
    }


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
