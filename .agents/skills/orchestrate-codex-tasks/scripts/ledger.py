#!/usr/bin/env python3
"""Durable SQLite run ledger for the Codex task orchestrator."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from orchestration_common import (  # noqa: E402
    LEDGER_SCHEMA_VERSION,
    PROTOCOL_VERSION,
    OrchestrationError,
    boundaries_overlap,
    canonical_json,
    configure_utf8_stdio,
    load_json_file,
    normalize_boundary,
    normalize_execution_plan,
    normalize_failure_policy,
    normalize_step_contracts,
    print_result,
    require_string_list,
    require_text,
    result,
    stable_hash,
    utc_now,
    validate_identifier,
    validate_safe_data,
    write_json_file,
)
from dispatch import normalize_manifest  # noqa: E402

SCHEMA_FILE = SCRIPT_DIR / "sql" / "001_initial.sql"
ACTIVE_STATES = ("PROVISIONING", "RUNNING", "REVIEW", "BLOCKED")
TERMINAL_STATES = ("ACCEPTED", "RETIRED")
WORKER_STATES = set(ACTIVE_STATES + TERMINAL_STATES)
HEALTH_STATES = {"HEALTHY", "AT_RISK", "STALLED"}
INCIDENT_CLASSES = {
    "NONE",
    "EXPECTED_RESULT",
    "RECOVERABLE_CONTROL",
    "CONTROL_DEGRADED",
    "WORK_BLOCKER",
}
BLOCKER_DISPOSITIONS = {"BLOCK", "RECOVERABLE"}
INTEGRATION_STATES = {
    "NONE",
    "PENDING_REVIEW",
    "READY",
    "HANDOFF_RUNNING",
    "HANDED_OFF",
    "VALIDATING",
    "ACCEPTED",
    "FAILED",
    "DISCARDED",
}
OPERATION_KINDS = {"CREATE_THREAD", "SEND_MESSAGE", "SET_TITLE", "HANDOFF"}
COMMANDS = {
    "DECISION",
    "CHECKPOINT",
    "REPLAN",
    "REVISION",
    "SCOPE_UPDATE",
    "LANGUAGE_UPDATE",
    "STOP",
}
MICRO_CONTROL_COMMANDS = {"DECISION", "REVISION"}
MAX_DECISION_ROUND_TRIPS = 3
ALLOWED_TRANSITIONS = {
    "PROVISIONING": {"PROVISIONING", "RUNNING", "BLOCKED", "RETIRED"},
    "RUNNING": {"RUNNING", "BLOCKED", "REVIEW", "RETIRED"},
    "BLOCKED": {"BLOCKED", "RUNNING", "REVIEW", "RETIRED"},
    "REVIEW": {"REVIEW", "RUNNING", "BLOCKED", "ACCEPTED", "RETIRED"},
    "ACCEPTED": {"ACCEPTED"},
    "RETIRED": {"RETIRED"},
}
RECORD_EVENT_TYPES = {
    "TASK_PLANNED",
    "TASK_REPLANNED",
    "TASK_STATUS_CHANGED",
    "WORKER_REGISTERED",
    "WORKER_RESOLVED",
    "WORKER_MESSAGE_APPLIED",
    "WORKER_STATE_CHANGED",
    "WORKER_HEALTH_CHANGED",
    "CURSOR_UPDATED",
    "RUN_UPDATED",
    "DECISION_RECORDED",
    "INTEGRATION_UPDATED",
    "CYCLE_STARTED",
    "CYCLE_COMPLETED",
    "RUN_COMPLETED",
    "RECOVERY_RECONSTRUCTED",
}
EVENT_PAYLOAD_FIELDS = {
    "TASK_PLANNED": {"task"},
    "TASK_REPLANNED": {
        "task",
        "previousSpecHash",
        "reason",
        "scopeChangeAuthorized",
        "userDecisionId",
    },
    "TASK_STATUS_CHANGED": {"taskId", "status", "reason"},
    "WORKER_REGISTERED": {
        "taskId",
        "threadId",
        "clientThreadId",
        "hostId",
        "state",
        "health",
        "title",
        "currentMilestone",
        "closedAcceptanceItems",
        "remainingAcceptanceItems",
        "lastDetails",
        "nextActions",
        "needs",
        "evidence",
        "lastSeq",
        "lastControllerSeq",
        "lastControllerSeqReserved",
        "cursor",
        "resultSummary",
        "lastUsefulProgressAt",
        "estimatedRemaining",
        "promptVersion",
        "promptHash",
    },
    "WORKER_RESOLVED": {"threadId", "clientThreadId", "hostId", "state"},
    "WORKER_MESSAGE_APPLIED": {
        "seq",
        "messageType",
        "summary",
        "milestone",
        "completed",
        "remaining",
        "estimate",
        "details",
        "next",
        "needs",
        "evidence",
        "appliedControllerSeq",
        "usefulProgress",
        "resumed",
        "cursor",
        "incidentClass",
        "localCorrectionAttempts",
        "blockerDisposition",
    },
    "WORKER_STATE_CHANGED": {
        "state",
        "terminalReason",
        "archiveReady",
        "replacementWorkerId",
    },
    "WORKER_HEALTH_CHANGED": {
        "health",
        "decisionRoundTrips",
        "scopeDeltaCount",
        "timeoutCount",
        "nextHealthReviewAt",
    },
    "CURSOR_UPDATED": {"cursor"},
    "RUN_UPDATED": {
        "runLanguage",
        "status",
        "maxActiveWorkers",
        "localPreferred",
        "controllerTitle",
        "persistenceMode",
    },
    "DECISION_RECORDED": {
        "decisionId",
        "source",
        "summary",
        "scope",
        "workerIds",
    },
    "INTEGRATION_UPDATED": {
        "state",
        "targetBranch",
        "externalOperationId",
        "evidence",
    },
    "CYCLE_STARTED": {"cycleNumber", "goalSummary"},
    "CYCLE_COMPLETED": {"cycleNumber", "resultSummary", "validation"},
    "RUN_COMPLETED": {"summary"},
    "RECOVERY_RECONSTRUCTED": {"summary"},
}
WORKER_SCOPED_EVENTS = {
    "WORKER_REGISTERED",
    "WORKER_RESOLVED",
    "WORKER_MESSAGE_APPLIED",
    "WORKER_STATE_CHANGED",
    "WORKER_HEALTH_CHANGED",
    "CURSOR_UPDATED",
    "INTEGRATION_UPDATED",
}
TASK_SPEC_FIELDS = {
    "taskId",
    "workerId",
    "priority",
    "titleAction",
    "objective",
    "context",
    "inScope",
    "outOfScope",
    "dependencies",
    "workerHost",
    "environment",
    "projectId",
    "writesFiles",
    "sharedLocalWriteAuthorized",
    "writeBoundary",
    "integrationPlan",
    "deliverables",
    "acceptance",
    "milestones",
    "healthCheckpoint",
    "coordinationProfile",
    "resourceClaims",
    "failurePolicy",
}


def optional_text(value: Any, field: str, *, maximum: int = 8192) -> str | None:
    if value is None:
        return None
    return require_text(value, field, maximum=maximum)


def require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be a boolean")
    return value


def require_nonnegative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} must be a non-negative integer",
        )
    return value


def optional_sha256(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} must be a lowercase SHA-256 digest",
        )
    return value


def reject_unknown_fields(
    value: dict[str, Any],
    *,
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} contains unsupported fields",
            {"fields": unknown},
        )


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def connect_database(
    path: Path,
    *,
    readonly: bool = False,
    create: bool = False,
) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    if readonly or not create:
        mode = "ro" if readonly else "rw"
        uri = f"file:{quote(str(resolved), safe='/')}?mode={mode}"
        connection = sqlite3.connect(uri, uri=True, timeout=5, isolation_level=None)
    else:
        connection = sqlite3.connect(resolved, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if readonly:
        connection.execute("PRAGMA query_only = ON")
    else:
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def check_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version != LEDGER_SCHEMA_VERSION:
        raise OrchestrationError(
            "SCHEMA_MISMATCH",
            f"Expected ledger schema {LEDGER_SCHEMA_VERSION}, found {version}",
        )


def get_run(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM run_state WHERE singleton = 1").fetchone()
    if row is None:
        raise OrchestrationError("LEDGER_UNINITIALIZED", "Ledger has no run_state row")
    return row


def assert_owner(connection: sqlite3.Connection, controller_thread_id: str) -> sqlite3.Row:
    run = get_run(connection)
    if run["controller_thread_id"] != controller_thread_id:
        raise OrchestrationError(
            "OWNER_CONFLICT",
            "Only the recorded Controller may write this ledger",
            {
                "expectedControllerThreadId": run["controller_thread_id"],
                "providedControllerThreadId": controller_thread_id,
                "controllerEpoch": run["controller_epoch"],
            },
        )
    return run


def git_command(project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def find_git_root(project_root: Path) -> Path | None:
    process = git_command(project_root, "rev-parse", "--show-toplevel")
    if process.returncode != 0:
        return None
    return Path(process.stdout.strip()).resolve()


def ensure_git_ignored(state_root: Path, git_root: Path, *, prepare: bool) -> dict[str, Any]:
    try:
        relative = state_root.resolve().relative_to(git_root)
    except ValueError:
        return {"gitRoot": str(git_root), "ignoreRule": None, "outsideGitRoot": True}

    tracked = git_command(git_root, "ls-files", "--", relative.as_posix())
    if tracked.returncode != 0:
        raise OrchestrationError("GIT_CHECK_FAILED", tracked.stderr.strip() or "git ls-files failed")
    if tracked.stdout.strip():
        raise OrchestrationError(
            "LEDGER_PATH_TRACKED",
            "The selected runtime ledger path is already tracked by Git",
            {"files": tracked.stdout.splitlines()},
        )

    sentinel = relative / ".ledger-sentinel"
    ignored = git_command(git_root, "check-ignore", "-q", "--no-index", sentinel.as_posix())
    rule = f"/{relative.as_posix().rstrip('/')}/"
    if ignored.returncode == 0:
        return {"gitRoot": str(git_root), "ignoreRule": rule, "prepared": False}
    if not prepare:
        raise OrchestrationError(
            "LOCAL_EXCLUDE_REQUIRED",
            "Runtime ledger path is not ignored by Git",
            {"requiredRule": rule, "gitRoot": str(git_root)},
        )

    exclude_result = git_command(git_root, "rev-parse", "--git-path", "info/exclude")
    if exclude_result.returncode != 0:
        raise OrchestrationError("GIT_CHECK_FAILED", "Cannot resolve .git/info/exclude")
    exclude_path = Path(exclude_result.stdout.strip())
    if not exclude_path.is_absolute():
        exclude_path = git_root / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    current = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    existing_rules = {line.strip() for line in current.splitlines()}
    if rule not in existing_rules:
        with exclude_path.open("a", encoding="utf-8") as handle:
            if current and not current.endswith("\n"):
                handle.write("\n")
            handle.write(f"{rule}\n")

    ignored = git_command(git_root, "check-ignore", "-q", "--no-index", sentinel.as_posix())
    if ignored.returncode != 0:
        raise OrchestrationError(
            "LOCAL_EXCLUDE_FAILED",
            "Could not establish a local Git ignore rule for the ledger",
            {"requiredRule": rule},
        )
    return {
        "gitRoot": str(git_root),
        "ignoreRule": rule,
        "prepared": rule not in existing_rules,
        "excludePath": str(exclude_path),
    }


def default_state_root(project_root: Path) -> Path:
    return project_root / ".codex" / "runtime" / "orchestrate-codex-tasks"


def create_runtime_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def decode_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise OrchestrationError("CORRUPT_JSON_FIELD", "Ledger contains invalid JSON") from exc


def append_event(
    connection: sqlite3.Connection,
    *,
    idempotency_key: str,
    event_type: str,
    payload: dict[str, Any],
    created_at: str,
    worker_id: str | None = None,
    operation_id: str | None = None,
) -> tuple[int, bool]:
    validate_safe_data(payload)
    payload_hash = stable_hash(payload)
    existing = connection.execute(
        """
        SELECT revision, event_type, payload_hash, worker_id, operation_id
        FROM events
        WHERE idempotency_key = ?
        """,
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        if (
            existing["event_type"] != event_type
            or existing["payload_hash"] != payload_hash
            or existing["worker_id"] != worker_id
            or existing["operation_id"] != operation_id
        ):
            raise OrchestrationError(
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key was reused with different content",
                {"idempotencyKey": idempotency_key},
            )
        return int(existing["revision"]), True

    run = get_run(connection)
    revision = int(run["revision"]) + 1
    connection.execute(
        """
        INSERT INTO events (
            revision, idempotency_key, event_type, worker_id, operation_id,
            payload_hash, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            revision,
            idempotency_key,
            event_type,
            worker_id,
            operation_id,
            payload_hash,
            canonical_json(payload),
            created_at,
        ),
    )
    connection.execute(
        "UPDATE run_state SET revision = ?, updated_at = ? WHERE singleton = 1",
        (revision, created_at),
    )
    return revision, False


def recompute_derived(connection: sqlite3.Connection, now: str) -> None:
    active_rows = connection.execute(
        """
        SELECT worker_id
        FROM workers
        WHERE state IN ('PROVISIONING', 'RUNNING', 'REVIEW', 'BLOCKED')
        ORDER BY worker_id
        """
    ).fetchall()
    active_ids = [row["worker_id"] for row in active_rows]
    queued_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM planned_tasks WHERE status = 'QUEUED'"
        ).fetchone()[0]
    )
    groups = [active_ids[index : index + 8] for index in range(0, len(active_ids), 8)]
    run = get_run(connection)
    one_to_one_since = run["one_to_one_since"]
    if len(active_ids) == 1 and queued_count == 0:
        one_to_one_since = one_to_one_since or now
    else:
        one_to_one_since = None
    connection.execute(
        """
        UPDATE run_state
        SET active_count = ?, queued_count = ?, monitor_groups_json = ?,
            one_to_one_since = ?, updated_at = ?
        WHERE singleton = 1
        """,
        (len(active_ids), queued_count, canonical_json(groups), one_to_one_since, now),
    )


def apply_state_transition(
    connection: sqlite3.Connection,
    worker_id: str,
    new_state: str,
    *,
    now: str,
    terminal_reason: str | None = None,
    archive_ready: bool = False,
    replacement_worker_id: str | None = None,
) -> None:
    if new_state not in WORKER_STATES:
        raise OrchestrationError("INVALID_STATE", f"Invalid Worker state: {new_state}")
    row = connection.execute(
        "SELECT state, health FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    if row is None:
        raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
    old_state = row["state"]
    if new_state not in ALLOWED_TRANSITIONS[old_state]:
        raise OrchestrationError(
            "ILLEGAL_TRANSITION",
            f"Worker transition {old_state} -> {new_state} is not allowed",
        )
    if replacement_worker_id is not None:
        replacement_worker_id = validate_identifier(
            replacement_worker_id,
            "workerId",
        )
        if new_state != "RETIRED":
            raise OrchestrationError(
                "INVALID_FIELD",
                "replacementWorkerId is valid only for RETIRED Workers",
            )
        if replacement_worker_id == worker_id:
            raise OrchestrationError(
                "INVALID_FIELD",
                "A Worker cannot replace itself",
            )
        replacement = connection.execute(
            "SELECT 1 FROM planned_tasks WHERE worker_id = ?",
            (replacement_worker_id,),
        ).fetchone()
        if replacement is None:
            raise OrchestrationError(
                "TASK_NOT_FOUND",
                "replacementWorkerId must refer to a planned Worker",
            )
    if new_state in TERMINAL_STATES:
        terminal_reason = require_text(terminal_reason, "terminalReason")
        if not archive_ready:
            raise OrchestrationError(
                "ARCHIVE_GATE_FAILED",
                "Terminal Worker states require archiveReady=true",
            )
    elif archive_ready:
        raise OrchestrationError(
            "ARCHIVE_GATE_FAILED",
            "archiveReady cannot be true for a non-terminal Worker",
        )
    connection.execute(
        """
        UPDATE workers
        SET state = ?,
            health = CASE
                WHEN ? <> 'BLOCKED' AND health = 'STALLED' THEN 'AT_RISK'
                ELSE health
            END,
            terminal_reason = ?, archive_ready = ?,
            replacement_worker_id = ?, updated_at = ?
        WHERE worker_id = ?
        """,
        (
            new_state,
            new_state,
            terminal_reason,
            int(archive_ready),
            replacement_worker_id,
            now,
            worker_id,
        ),
    )
    if new_state == "ACCEPTED":
        connection.execute(
            "UPDATE planned_tasks SET status = 'ACCEPTED', updated_at = ? WHERE worker_id = ?",
            (now, worker_id),
        )
    elif new_state == "RETIRED":
        connection.execute(
            "UPDATE planned_tasks SET status = 'RETIRED', updated_at = ? WHERE worker_id = ?",
            (now, worker_id),
        )
    else:
        connection.execute(
            """
            UPDATE planned_tasks
            SET status = CASE WHEN status = 'QUEUED' THEN 'DISPATCHED' ELSE status END,
                updated_at = ?
            WHERE worker_id = ?
            """,
            (now, worker_id),
        )


def task_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task = payload.get("task", payload)
    if not isinstance(task, dict):
        raise OrchestrationError("INVALID_FIELD", "task must be an object")
    reject_unknown_fields(
        task,
        allowed=TASK_SPEC_FIELDS,
        field="task specification",
    )
    task_id = validate_identifier(task.get("taskId"), "taskId")
    worker_id = validate_identifier(task.get("workerId"), "workerId")
    objective = require_text(task.get("objective"), "objective")
    dependencies = require_string_list(task.get("dependencies", []), "dependencies")
    if len(dependencies) != len(set(dependencies)):
        raise OrchestrationError(
            "INVALID_FIELD",
            "dependencies must not contain duplicates",
        )
    for dependency in dependencies:
        validate_identifier(dependency, "workerId")
    if worker_id in dependencies:
        raise OrchestrationError(
            "DEPENDENCY_CYCLE",
            f"{worker_id} cannot depend on itself",
        )
    environment_value = task.get("environment")
    normalized_environment: str | dict[str, Any]
    if isinstance(environment_value, dict):
        environment = environment_value.get("type")
        allowed_environment_fields = {
            "local": {"type"},
            "worktree": {"type", "startingState"},
            "projectless": {"type", "directoryName"},
        }.get(environment)
        if allowed_environment_fields is None:
            raise OrchestrationError("INVALID_FIELD", f"Invalid environment: {environment}")
        reject_unknown_fields(
            environment_value,
            allowed=allowed_environment_fields,
            field="task environment",
        )
        if environment == "worktree" and environment_value.get("startingState") is not None:
            starting_state = environment_value["startingState"]
            if not isinstance(starting_state, dict):
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "startingState must be an object",
                )
            state_type = starting_state.get("type")
            if state_type == "working-tree":
                reject_unknown_fields(
                    starting_state,
                    allowed={"type"},
                    field="startingState",
                )
            elif state_type == "branch":
                reject_unknown_fields(
                    starting_state,
                    allowed={"type", "branchName"},
                    field="startingState",
                )
                require_text(
                    starting_state.get("branchName"),
                    "startingState.branchName",
                    maximum=256,
                )
            else:
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "startingState.type must be working-tree or branch",
                )
        normalized_environment = dict(environment_value)
        if (
            environment == "worktree"
            and isinstance(normalized_environment.get("startingState"), dict)
            and normalized_environment["startingState"].get("type") == "branch"
        ):
            normalized_environment["startingState"] = {
                "type": "branch",
                "branchName": require_text(
                    normalized_environment["startingState"].get("branchName"),
                    "startingState.branchName",
                    maximum=256,
                ),
            }
        if environment == "projectless" and environment_value.get("directoryName") is not None:
            normalized_environment["directoryName"] = require_text(
                environment_value["directoryName"],
                "environment.directoryName",
                maximum=128,
            )
    else:
        environment = environment_value
        normalized_environment = environment
    if environment not in {"local", "worktree", "projectless"}:
        raise OrchestrationError("INVALID_FIELD", f"Invalid environment: {environment}")
    write_boundary = [
        normalize_boundary(item)
        for item in require_string_list(
            task.get("writeBoundary", []),
            "writeBoundary",
        )
    ]
    if len(write_boundary) != len(set(write_boundary)):
        raise OrchestrationError(
            "INVALID_FIELD",
            "writeBoundary must not contain duplicates",
        )
    priority = task.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise OrchestrationError("INVALID_FIELD", "priority must be an integer")
    coordination_profile = task.get("coordinationProfile")
    if coordination_profile is not None and coordination_profile not in {
        "lean",
        "standard",
        "strict",
    }:
        raise OrchestrationError(
            "INVALID_FIELD",
            "coordinationProfile must be lean, standard, or strict",
        )
    resource_claims = task.get("resourceClaims")
    if resource_claims is not None:
        if not isinstance(resource_claims, dict) or not 1 <= len(resource_claims) <= 32:
            raise OrchestrationError(
                "INVALID_FIELD",
                "resourceClaims must contain between 1 and 32 resources",
            )
        for resource, quantity in resource_claims.items():
            if (
                not isinstance(resource, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,31}", resource) is None
                or not isinstance(quantity, int)
                or isinstance(quantity, bool)
                or quantity <= 0
            ):
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "resourceClaims must map valid names to positive integers",
                )
    has_failure_policy = "failurePolicy" in task
    failure_policy = (
        normalize_failure_policy(task.get("failurePolicy"), "failurePolicy")
        if has_failure_policy
        else None
    )
    validate_safe_data(task)
    normalized_task = dict(task)
    normalized_task.update(
        {
            "taskId": task_id,
            "workerId": worker_id,
            "priority": priority,
            "objective": objective,
            "dependencies": dependencies,
            "environment": normalized_environment,
            "writeBoundary": write_boundary,
        }
    )
    if has_failure_policy:
        normalized_task["failurePolicy"] = failure_policy
    return {
        "taskId": task_id,
        "workerId": worker_id,
        "objective": objective,
        "dependencies": dependencies,
        "environment": environment,
        "writeBoundary": write_boundary,
        "priority": priority,
        "spec": normalized_task,
        "specHash": stable_hash(normalized_task),
    }


def mutate_record_event(
    connection: sqlite3.Connection,
    event_type: str,
    worker_id: str | None,
    payload: dict[str, Any],
    now: str,
) -> None:
    if event_type == "MANIFEST_ACTIVATED":
        manifest = payload.get("manifest")
        if not isinstance(manifest, dict):
            raise OrchestrationError("INVALID_FIELD", "manifest must be an object")
        normalized_manifest = normalize_manifest(manifest)
        if normalized_manifest != manifest:
            raise OrchestrationError(
                "MANIFEST_NOT_CANONICAL",
                "Activate the canonical output from dispatch.py compile-manifest",
            )
        declared_hash = manifest.get("manifestHash")
        hashable_manifest = dict(manifest)
        hashable_manifest.pop("manifestHash", None)
        manifest_hash = stable_hash(hashable_manifest)
        if declared_hash != manifest_hash:
            raise OrchestrationError(
                "MANIFEST_HASH_MISMATCH",
                "Compiled manifestHash does not match its content",
                {"declared": declared_hash, "computed": manifest_hash},
            )
        run = get_run(connection)
        if manifest.get("protocolVersion") != PROTOCOL_VERSION:
            raise OrchestrationError("PROTOCOL_MISMATCH", "Manifest protocol is unsupported")
        if manifest.get("runId") != run["run_id"]:
            raise OrchestrationError("RUN_MISMATCH", "Manifest runId differs from ledger")
        if manifest.get("runLanguage") != run["run_language"]:
            raise OrchestrationError(
                "LANGUAGE_MISMATCH",
                "Manifest runLanguage differs from ledger",
            )
        if manifest.get("controllerThreadId") != run["controller_thread_id"]:
            raise OrchestrationError(
                "OWNER_CONFLICT",
                "Manifest Controller differs from ledger owner",
            )
        connection.execute(
            """
            INSERT INTO manifests(
                manifest_hash, protocol_version, manifest_json, created_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(manifest_hash) DO NOTHING
            """,
            (
                manifest_hash,
                PROTOCOL_VERSION,
                canonical_json(manifest),
                now,
            ),
        )
        connection.execute(
            """
            UPDATE run_state
            SET current_manifest_hash = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (manifest_hash, now),
        )
        return

    if event_type == "TASK_PLANNED":
        task = task_from_payload(payload)
        existing = connection.execute(
            "SELECT spec_hash, worker_id FROM planned_tasks WHERE task_id = ?",
            (task["taskId"],),
        ).fetchone()
        if existing is not None:
            if existing["spec_hash"] != task["specHash"] or existing["worker_id"] != task["workerId"]:
                raise OrchestrationError(
                    "TASK_CONFLICT",
                    f"Task {task['taskId']} already exists with a different specification",
                )
            return
        existing_worker_task = connection.execute(
            "SELECT task_id FROM planned_tasks WHERE worker_id = ?",
            (task["workerId"],),
        ).fetchone()
        if existing_worker_task is not None:
            raise OrchestrationError(
                "TASK_CONFLICT",
                (
                    f"Worker {task['workerId']} is already bound to "
                    f"{existing_worker_task['task_id']}"
                ),
            )
        connection.execute(
            """
            INSERT INTO planned_tasks (
                task_id, worker_id, priority, status, objective, dependencies_json,
                environment, write_boundary_json, spec_json, spec_hash, created_at, updated_at
            ) VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task["taskId"],
                task["workerId"],
                task["priority"],
                task["objective"],
                canonical_json(task["dependencies"]),
                task["environment"],
                canonical_json(task["writeBoundary"]),
                canonical_json(task["spec"]),
                task["specHash"],
                now,
                now,
            ),
        )
        return

    if event_type == "TASK_REPLANNED":
        task = task_from_payload(payload)
        existing = connection.execute(
            """
            SELECT p.*, w.state AS worker_state
            FROM planned_tasks AS p
            LEFT JOIN workers AS w ON w.worker_id = p.worker_id
            WHERE p.task_id = ?
            """,
            (task["taskId"],),
        ).fetchone()
        if existing is None:
            raise OrchestrationError("TASK_NOT_FOUND", f"Unknown task: {task['taskId']}")
        if existing["worker_id"] != task["workerId"]:
            raise OrchestrationError(
                "TASK_CONFLICT",
                "TASK_REPLANNED cannot change workerId",
            )
        if existing["status"] in {"ACCEPTED", "RETIRED", "CANCELLED"}:
            raise OrchestrationError(
                "TASK_TERMINAL",
                "A terminal task cannot be replanned",
            )
        previous_hash = require_text(
            payload.get("previousSpecHash"),
            "previousSpecHash",
            maximum=64,
        )
        if previous_hash != existing["spec_hash"]:
            raise OrchestrationError(
                "STALE_TASK_SPEC",
                "previousSpecHash does not match the current task",
                {
                    "provided": previous_hash,
                    "current": existing["spec_hash"],
                },
            )
        require_text(payload.get("reason"), "reason")
        scope_change = payload.get("scopeChangeAuthorized", False)
        if not isinstance(scope_change, bool):
            raise OrchestrationError(
                "INVALID_FIELD",
                "scopeChangeAuthorized must be a boolean",
            )
        previous_spec = decode_json(existing["spec_json"], {})
        structural_changes: list[str] = []
        for field in (
            "environment",
            "projectId",
            "writesFiles",
            "sharedLocalWriteAuthorized",
        ):
            if previous_spec.get(field) != task["spec"].get(field):
                structural_changes.append(field)
        if set(previous_spec.get("writeBoundary", [])) != set(
            task["spec"].get("writeBoundary", [])
        ):
            structural_changes.append("writeBoundary")
        for field in ("inScope", "outOfScope", "deliverables", "acceptance"):
            previous_value = previous_spec.get(field, [])
            next_value = task["spec"].get(field, [])
            if (
                isinstance(previous_value, list)
                and isinstance(next_value, list)
                and len(previous_value) != len(next_value)
            ):
                structural_changes.append(f"{field}.length")
        if structural_changes and not scope_change:
            raise OrchestrationError(
                "USER_DECISION_REQUIRED",
                "Structural task scope changes require a recorded USER decision",
                {"fields": structural_changes},
            )
        user_decision_id = payload.get("userDecisionId")
        if scope_change:
            user_decision_id = require_text(
                user_decision_id,
                "userDecisionId",
                maximum=128,
            )
            decision = connection.execute(
                "SELECT source FROM decisions WHERE decision_id = ?",
                (user_decision_id,),
            ).fetchone()
            if decision is None or decision["source"] != "USER":
                raise OrchestrationError(
                    "USER_DECISION_REQUIRED",
                    "Scope-changing replan requires a recorded USER decision",
                )
        elif user_decision_id is not None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "userDecisionId is valid only when scopeChangeAuthorized=true",
            )
        connection.execute(
            """
            UPDATE planned_tasks
            SET priority = ?, objective = ?, dependencies_json = ?,
                environment = ?, write_boundary_json = ?, spec_json = ?,
                spec_hash = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                task["priority"],
                task["objective"],
                canonical_json(task["dependencies"]),
                task["environment"],
                canonical_json(task["writeBoundary"]),
                canonical_json(task["spec"]),
                task["specHash"],
                now,
                task["taskId"],
            ),
        )
        connection.execute(
            """
            UPDATE workers
            SET objective = ?, updated_at = ?
            WHERE worker_id = ?
            """,
            (task["objective"], now, task["workerId"]),
        )
        return

    if event_type == "TASK_STATUS_CHANGED":
        task_id = validate_identifier(payload.get("taskId"), "taskId")
        status = payload.get("status")
        if status not in {"QUEUED", "CANCELLED"}:
            raise OrchestrationError("INVALID_STATE", f"Invalid task status: {status}")
        row = connection.execute(
            "SELECT status, worker_id FROM planned_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise OrchestrationError("TASK_NOT_FOUND", f"Unknown task: {task_id}")
        allowed = {
            "QUEUED": {"QUEUED", "CANCELLED"},
            "DISPATCHING": {"QUEUED"},
            "CANCELLED": {"QUEUED", "CANCELLED"},
        }
        if status not in allowed.get(row["status"], set()):
            raise OrchestrationError(
                "ILLEGAL_TRANSITION",
                f"Task transition {row['status']} -> {status} is not allowed",
            )
        worker_row = connection.execute(
            "SELECT state FROM workers WHERE worker_id = ?",
            (row["worker_id"],),
        ).fetchone()
        if status == "CANCELLED" and worker_row is not None:
            raise OrchestrationError(
                "WORKER_ALREADY_CREATED",
                "A task with a Worker must be retired through the Worker terminal gate",
                {"workerState": worker_row["state"]},
            )
        if row["status"] == "DISPATCHING" and status == "QUEUED":
            create_operation = connection.execute(
                """
                SELECT status
                FROM operations
                WHERE kind = 'CREATE_THREAD' AND worker_id = ?
                ORDER BY created_at DESC, operation_id DESC
                LIMIT 1
                """,
                (row["worker_id"],),
            ).fetchone()
            if create_operation is None or create_operation["status"] != "FAILED":
                raise OrchestrationError(
                    "CREATE_NOT_FAILED",
                    "DISPATCHING can return to QUEUED only after confirmed create failure",
                    {
                        "operationStatus": (
                            create_operation["status"]
                            if create_operation is not None
                            else "MISSING"
                        )
                    },
                )
        require_text(payload.get("reason"), "reason")
        cursor = connection.execute(
            "UPDATE planned_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
            (status, now, task_id),
        )
        if cursor.rowcount != 1:
            raise OrchestrationError("TASK_NOT_FOUND", f"Unknown task: {task_id}")
        return

    if event_type == "WORKER_REGISTERED":
        worker_id = validate_identifier(worker_id, "workerId")
        task_id = validate_identifier(payload.get("taskId"), "taskId")
        task = connection.execute(
            "SELECT objective, status FROM planned_tasks WHERE task_id = ? AND worker_id = ?",
            (task_id, worker_id),
        ).fetchone()
        if task is None:
            raise OrchestrationError("TASK_NOT_FOUND", "Worker registration requires a planned task")
        if task["status"] in {"CANCELLED", "ACCEPTED", "RETIRED"}:
            raise OrchestrationError(
                "TASK_TERMINAL",
                "A Worker cannot be registered for a terminal task",
                {"status": task["status"]},
            )
        state = payload.get("state", "PROVISIONING")
        health = payload.get("health", "HEALTHY")
        if state not in ACTIVE_STATES or health not in HEALTH_STATES:
            raise OrchestrationError("INVALID_STATE", "Invalid Worker state or health")
        if health == "STALLED" and state != "BLOCKED":
            raise OrchestrationError(
                "HEALTH_STATE_CONFLICT",
                "STALLED health requires BLOCKED lifecycle state",
            )
        existing = connection.execute(
            "SELECT task_id FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if existing is not None:
            if existing["task_id"] != task_id:
                raise OrchestrationError("WORKER_CONFLICT", "Worker ID belongs to another task")
            raise OrchestrationError(
                "WORKER_CONFLICT",
                "Worker is already registered; use resolution or state events",
            )
        thread_id = optional_text(payload.get("threadId"), "threadId", maximum=256)
        client_thread_id = optional_text(
            payload.get("clientThreadId"),
            "clientThreadId",
            maximum=256,
        )
        if thread_id is None and client_thread_id is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "WORKER_REGISTERED requires threadId or clientThreadId",
            )
        if state != "PROVISIONING" and thread_id is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{state} registration requires a resolved threadId",
            )
        host_id = optional_text(payload.get("hostId"), "hostId", maximum=256)
        title = optional_text(payload.get("title"), "title", maximum=256)
        current_milestone = optional_text(
            payload.get("currentMilestone"),
            "currentMilestone",
        )
        closed_acceptance = require_string_list(
            payload.get("closedAcceptanceItems", []),
            "closedAcceptanceItems",
        )
        remaining_acceptance = require_string_list(
            payload.get("remainingAcceptanceItems", []),
            "remainingAcceptanceItems",
        )
        last_details = require_string_list(
            payload.get("lastDetails", []),
            "lastDetails",
        )
        next_actions = require_string_list(
            payload.get("nextActions", []),
            "nextActions",
        )
        needs = require_string_list(payload.get("needs", []), "needs")
        evidence = require_string_list(
            payload.get("evidence", []),
            "evidence",
        )
        last_seq = require_nonnegative_int(payload.get("lastSeq", 0), "lastSeq")
        last_controller_seq = require_nonnegative_int(
            payload.get("lastControllerSeq", 0),
            "lastControllerSeq",
        )
        last_controller_seq_reserved = require_nonnegative_int(
            payload.get("lastControllerSeqReserved", last_controller_seq),
            "lastControllerSeqReserved",
        )
        if last_controller_seq_reserved < last_controller_seq:
            raise OrchestrationError(
                "INVALID_SEQUENCE",
                "lastControllerSeqReserved cannot be below lastControllerSeq",
            )
        cursor = optional_text(payload.get("cursor"), "cursor", maximum=16384)
        result_summary = optional_text(payload.get("resultSummary"), "resultSummary")
        last_useful_progress_at = optional_text(
            payload.get("lastUsefulProgressAt"),
            "lastUsefulProgressAt",
            maximum=64,
        )
        estimated_remaining = optional_text(
            payload.get("estimatedRemaining"),
            "estimatedRemaining",
        )
        prompt_version = require_nonnegative_int(
            payload.get("promptVersion", PROTOCOL_VERSION),
            "promptVersion",
        )
        if prompt_version == 0:
            raise OrchestrationError(
                "INVALID_FIELD",
                "promptVersion must be positive",
            )
        prompt_hash = optional_sha256(payload.get("promptHash"), "promptHash")
        connection.execute(
            """
            INSERT INTO workers (
                worker_id, task_id, thread_id, client_thread_id, host_id, state, health,
                title, objective, current_milestone, closed_acceptance_json,
                remaining_acceptance_json, last_details_json, next_actions_json,
                needs_json, evidence_json, last_seq, last_controller_seq,
                last_controller_seq_reserved, cursor, result_summary,
                last_useful_progress_at, estimated_remaining, prompt_version,
                prompt_hash, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                worker_id,
                task_id,
                thread_id,
                client_thread_id,
                host_id,
                state,
                health,
                title,
                task["objective"],
                current_milestone,
                canonical_json(closed_acceptance),
                canonical_json(remaining_acceptance),
                canonical_json(last_details),
                canonical_json(next_actions),
                canonical_json(needs),
                canonical_json(evidence),
                last_seq,
                last_controller_seq,
                last_controller_seq_reserved,
                cursor,
                result_summary,
                last_useful_progress_at,
                estimated_remaining,
                prompt_version,
                prompt_hash,
                now,
                now,
            ),
        )
        connection.execute(
            "UPDATE planned_tasks SET status = 'DISPATCHED', updated_at = ? WHERE task_id = ?",
            (now, task_id),
        )
        connection.execute(
            """
            INSERT INTO integrations(worker_id, state, evidence_json, updated_at)
            VALUES (?, 'NONE', '[]', ?)
            """,
            (worker_id, now),
        )
        return

    if worker_id is not None:
        worker_id = validate_identifier(worker_id, "workerId")

    if event_type == "WORKER_RESOLVED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        row = connection.execute(
            "SELECT state, thread_id FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if row is None:
            raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
        new_state = payload.get("state", "RUNNING")
        if new_state not in {"PROVISIONING", "RUNNING", "BLOCKED"}:
            raise OrchestrationError(
                "INVALID_STATE",
                "WORKER_RESOLVED can only preserve provisioning or enter RUNNING/BLOCKED",
            )
        if new_state not in ALLOWED_TRANSITIONS[row["state"]]:
            raise OrchestrationError("ILLEGAL_TRANSITION", "Invalid resolution state transition")
        thread_id = optional_text(payload.get("threadId"), "threadId", maximum=256)
        client_thread_id = optional_text(
            payload.get("clientThreadId"),
            "clientThreadId",
            maximum=256,
        )
        host_id = optional_text(payload.get("hostId"), "hostId", maximum=256)
        if thread_id is None and client_thread_id is None and host_id is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "WORKER_RESOLVED requires at least one resolved address field",
            )
        if new_state == "RUNNING" and thread_id is None and row["thread_id"] is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "RUNNING resolution requires a real threadId",
            )
        connection.execute(
            """
            UPDATE workers
            SET thread_id = COALESCE(?, thread_id),
                client_thread_id = COALESCE(?, client_thread_id),
                host_id = COALESCE(?, host_id),
                state = ?,
                health = CASE WHEN ? = 'BLOCKED' THEN health ELSE 'HEALTHY' END,
                updated_at = ?
            WHERE worker_id = ?
            """,
            (
                thread_id,
                client_thread_id,
                host_id,
                new_state,
                new_state,
                now,
                worker_id,
            ),
        )
        return

    if event_type == "WORKER_MESSAGE_APPLIED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        row = connection.execute(
            """
            SELECT w.state, w.last_seq, w.last_controller_seq,
                   w.last_controller_seq_reserved, w.progress_without_completion,
                   w.closed_acceptance_json, p.spec_json
            FROM workers AS w
            JOIN planned_tasks AS p ON p.task_id = w.task_id
            WHERE w.worker_id = ?
            """,
            (worker_id,),
        ).fetchone()
        if row is None:
            raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
        if row["state"] in TERMINAL_STATES:
            raise OrchestrationError(
                "WORKER_TERMINAL",
                "Terminal Worker messages cannot mutate ledger state",
            )
        sequence = payload.get("seq")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence <= 0
        ):
            raise OrchestrationError("INVALID_SEQUENCE", "Worker seq must be a positive integer")
        if sequence <= row["last_seq"]:
            raise OrchestrationError(
                "STALE_WORKER_SEQ",
                "Worker message seq is not newer than the ledger",
                {"lastSeq": row["last_seq"], "receivedSeq": sequence},
            )
        message_type = payload.get("messageType")
        if message_type not in {"ACCEPTED", "PROGRESS", "BLOCKED", "DONE"}:
            raise OrchestrationError("INVALID_FIELD", f"Invalid Worker message type: {message_type}")
        incident_class = payload.get(
            "incidentClass",
            "WORK_BLOCKER" if message_type == "BLOCKED" else "NONE",
        )
        if incident_class not in INCIDENT_CLASSES:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"Invalid incidentClass: {incident_class}",
            )
        correction_attempts = payload.get("localCorrectionAttempts", 0)
        if (
            not isinstance(correction_attempts, int)
            or isinstance(correction_attempts, bool)
            or not 0 <= correction_attempts <= 2
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                "localCorrectionAttempts must be an integer from 0 to 2",
            )
        task_spec = decode_json(row["spec_json"], {})
        failure_policy = normalize_failure_policy(
            task_spec.get("failurePolicy"),
            "task.failurePolicy",
        )
        correction_budget = failure_policy["localCorrectionBudget"]
        if (
            incident_class == "RECOVERABLE_CONTROL"
            and correction_attempts > correction_budget
        ):
            raise OrchestrationError(
                "INCIDENT_BUDGET_EXCEEDED",
                "Recoverable control incident exceeds the local correction budget",
                {
                    "localCorrectionAttempts": correction_attempts,
                    "localCorrectionBudget": correction_budget,
                },
            )
        if (
            incident_class in {"NONE", "EXPECTED_RESULT", "CONTROL_DEGRADED"}
            and correction_attempts != 0
        ):
            raise OrchestrationError(
                "INCIDENT_CLASS_CONFLICT",
                f"incidentClass={incident_class} cannot consume local correction attempts",
            )
        blocker_disposition = payload.get("blockerDisposition")
        if message_type == "BLOCKED":
            blocker_disposition = blocker_disposition or "BLOCK"
            if blocker_disposition not in BLOCKER_DISPOSITIONS:
                raise OrchestrationError(
                    "INVALID_FIELD",
                    f"Invalid blockerDisposition: {blocker_disposition}",
                )
            if blocker_disposition == "BLOCK" and incident_class != "WORK_BLOCKER":
                raise OrchestrationError(
                    "INCIDENT_CLASS_CONFLICT",
                    "Only incidentClass=WORK_BLOCKER may enter lifecycle BLOCKED",
                )
            if (
                blocker_disposition == "RECOVERABLE"
                and incident_class
                not in {
                    "EXPECTED_RESULT",
                    "RECOVERABLE_CONTROL",
                    "CONTROL_DEGRADED",
                }
            ):
                raise OrchestrationError(
                    "INCIDENT_CLASS_CONFLICT",
                    "RECOVERABLE disposition requires a non-work-blocker incident class",
                )
        elif blocker_disposition is not None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "blockerDisposition is valid only for a BLOCKED message",
            )
        elif incident_class == "WORK_BLOCKER":
            raise OrchestrationError(
                "INCIDENT_CLASS_CONFLICT",
                "incidentClass=WORK_BLOCKER requires messageType=BLOCKED",
            )
        summary = require_text(payload.get("summary"), "summary")
        milestone = require_text(payload.get("milestone"), "milestone")
        estimate = require_text(payload.get("estimate"), "estimate")
        completed = require_string_list(payload.get("completed", []), "completed")
        remaining = require_string_list(payload.get("remaining", []), "remaining")
        details = require_string_list(payload.get("details", []), "details")
        next_actions = require_string_list(payload.get("next", []), "next")
        needs = require_string_list(payload.get("needs", []), "needs")
        evidence = require_string_list(payload.get("evidence", []), "evidence")
        explicit_useful = payload.get("usefulProgress")
        if explicit_useful is not None:
            require_bool(explicit_useful, "usefulProgress")
        resumed = payload.get("resumed")
        if resumed is not None:
            require_bool(resumed, "resumed")
        useful = explicit_useful is True or any(
            item.strip().lower() != "none" for item in completed
        )
        new_state = row["state"]
        if message_type == "DONE":
            new_state = "REVIEW"
        elif message_type == "BLOCKED":
            if blocker_disposition == "BLOCK":
                new_state = "BLOCKED"
            elif row["state"] in {"PROVISIONING", "RUNNING", "BLOCKED"}:
                new_state = "RUNNING"
        elif row["state"] == "PROVISIONING":
            new_state = "RUNNING"
        elif row["state"] == "BLOCKED" and resumed is True:
            new_state = "RUNNING"
        if new_state not in ALLOWED_TRANSITIONS[row["state"]]:
            raise OrchestrationError(
                "ILLEGAL_TRANSITION",
                f"Message would cause {row['state']} -> {new_state}",
            )
        applied_controller_seq = payload.get("appliedControllerSeq", row["last_controller_seq"])
        if (
            not isinstance(applied_controller_seq, int)
            or isinstance(applied_controller_seq, bool)
            or applied_controller_seq < 0
        ):
            raise OrchestrationError("INVALID_SEQUENCE", "appliedControllerSeq must be non-negative")
        if applied_controller_seq > row["last_controller_seq_reserved"]:
            raise OrchestrationError(
                "UNSENT_CONTROLLER_SEQUENCE",
                "Worker acknowledged a controllerSeq that was never reserved",
                {
                    "reserved": row["last_controller_seq_reserved"],
                    "received": applied_controller_seq,
                },
            )
        applied_controller_seq = max(applied_controller_seq, row["last_controller_seq"])
        reserved = row["last_controller_seq_reserved"]
        progress_without_completion = row["progress_without_completion"]
        if message_type == "PROGRESS":
            progress_without_completion = 0 if useful else progress_without_completion + 1
        elif useful:
            progress_without_completion = 0
        closed_acceptance = decode_json(row["closed_acceptance_json"], [])
        for item in completed:
            if item.strip().lower() != "none" and item not in closed_acceptance:
                closed_acceptance.append(item)
        connection.execute(
            """
            UPDATE workers
            SET state = ?,
                health = CASE
                    WHEN ? <> 'BLOCKED' AND health = 'STALLED' THEN 'AT_RISK'
                    ELSE health
                END,
                last_seq = ?, last_controller_seq = ?,
                last_controller_seq_reserved = ?, current_milestone = ?,
                closed_acceptance_json = ?, remaining_acceptance_json = ?,
                last_details_json = ?, next_actions_json = ?,
                needs_json = ?, evidence_json = ?,
                estimated_remaining = ?, cursor = COALESCE(?, cursor),
                result_summary = COALESCE(?, result_summary),
                last_useful_progress_at = CASE
                    WHEN ? = 1 THEN ? ELSE last_useful_progress_at
                END,
                progress_without_completion = ?,
                updated_at = ?
            WHERE worker_id = ?
            """,
            (
                new_state,
                new_state,
                sequence,
                applied_controller_seq,
                reserved,
                milestone,
                canonical_json(closed_acceptance),
                canonical_json(remaining),
                canonical_json(details),
                canonical_json(next_actions),
                canonical_json(needs),
                canonical_json(evidence),
                estimate,
                optional_text(payload.get("cursor"), "cursor", maximum=16384),
                summary if message_type == "DONE" else None,
                int(useful),
                now,
                progress_without_completion,
                now,
                worker_id,
            ),
        )
        return

    if event_type == "WORKER_STATE_CHANGED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        archive_ready = payload.get("archiveReady", False)
        require_bool(archive_ready, "archiveReady")
        apply_state_transition(
            connection,
            worker_id,
            payload.get("state"),
            now=now,
            terminal_reason=payload.get("terminalReason"),
            archive_ready=archive_ready,
            replacement_worker_id=payload.get("replacementWorkerId"),
        )
        return

    if event_type == "WORKER_HEALTH_CHANGED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        health = payload.get("health")
        if health not in HEALTH_STATES:
            raise OrchestrationError("INVALID_STATE", f"Invalid Worker health: {health}")
        row = connection.execute(
            "SELECT state FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone()
        if row is None:
            raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
        if health == "STALLED" and row["state"] != "BLOCKED":
            raise OrchestrationError(
                "HEALTH_STATE_CONFLICT",
                "STALLED health requires BLOCKED lifecycle state",
            )
        counters = {
            "decision_round_trips": payload.get("decisionRoundTrips"),
            "scope_delta_count": payload.get("scopeDeltaCount"),
            "timeout_count": payload.get("timeoutCount"),
        }
        for field, value in counters.items():
            if value is not None and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise OrchestrationError("INVALID_FIELD", f"{field} must be non-negative")
        connection.execute(
            """
            UPDATE workers
            SET health = ?,
                decision_round_trips = COALESCE(?, decision_round_trips),
                scope_delta_count = COALESCE(?, scope_delta_count),
                timeout_count = COALESCE(?, timeout_count),
                next_health_review_at = ?,
                updated_at = ?
            WHERE worker_id = ?
            """,
            (
                health,
                counters["decision_round_trips"],
                counters["scope_delta_count"],
                counters["timeout_count"],
                optional_text(
                    payload.get("nextHealthReviewAt"),
                    "nextHealthReviewAt",
                    maximum=64,
                ),
                now,
                worker_id,
            ),
        )
        return

    if event_type == "CURSOR_UPDATED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        cursor = require_text(payload.get("cursor"), "cursor", maximum=16384)
        updated = connection.execute(
            "UPDATE workers SET cursor = ?, updated_at = ? WHERE worker_id = ?",
            (cursor, now, worker_id),
        )
        if updated.rowcount != 1:
            raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
        return

    if event_type == "RUN_UPDATED":
        current_run = get_run(connection)
        if current_run["status"] == "COMPLETE":
            raise OrchestrationError(
                "RUN_COMPLETE",
                "A completed run cannot be updated or reopened",
            )
        assignments: list[str] = []
        values: list[Any] = []
        mapping = {
            "runLanguage": ("run_language", {"en", "zh-CN"}),
            "status": ("status", {"ACTIVE", "WAITING", "DEGRADED"}),
            "maxActiveWorkers": ("max_active_workers", None),
            "localPreferred": ("local_preferred", None),
            "controllerTitle": ("controller_title", None),
            "persistenceMode": ("persistence_mode", {"LOCAL", "DEGRADED"}),
        }
        for source, (column, allowed) in mapping.items():
            if source not in payload:
                continue
            value = payload[source]
            if allowed is not None and value not in allowed:
                raise OrchestrationError("INVALID_FIELD", f"Invalid {source}: {value}")
            if source == "maxActiveWorkers" and (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise OrchestrationError("INVALID_FIELD", "maxActiveWorkers must be positive")
            if source == "localPreferred":
                value = int(require_bool(value, "localPreferred"))
            if source == "controllerTitle":
                value = require_text(value, "controllerTitle", maximum=256)
            assignments.append(f"{column} = ?")
            values.append(value)
        if not assignments:
            raise OrchestrationError("INVALID_FIELD", "RUN_UPDATED contains no supported fields")
        assignments.append("updated_at = ?")
        values.extend([now, 1])
        connection.execute(
            f"UPDATE run_state SET {', '.join(assignments)} WHERE singleton = ?",
            values,
        )
        return

    if event_type == "DECISION_RECORDED":
        decision_id = require_text(payload.get("decisionId"), "decisionId", maximum=128)
        source = payload.get("source")
        if source not in {"USER", "CONTROLLER"}:
            raise OrchestrationError("INVALID_FIELD", f"Invalid decision source: {source}")
        scope = require_string_list(payload.get("scope", []), "scope")
        decision_workers = require_string_list(
            payload.get("workerIds", []),
            "workerIds",
        )
        for decision_worker in decision_workers:
            validate_identifier(decision_worker, "workerId")
        if connection.execute(
            "SELECT 1 FROM decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone() is not None:
            raise OrchestrationError(
                "DECISION_CONFLICT",
                f"Decision ID already exists: {decision_id}",
            )
        connection.execute(
            """
            INSERT INTO decisions(
                decision_id, source, summary, scope_json, worker_ids_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                source,
                require_text(payload.get("summary"), "summary"),
                canonical_json(scope),
                canonical_json(decision_workers),
                now,
            ),
        )
        return

    if event_type == "INTEGRATION_UPDATED":
        if worker_id is None:
            raise OrchestrationError("INVALID_FIELD", "workerId is required")
        state = payload.get("state")
        if state not in INTEGRATION_STATES:
            raise OrchestrationError("INVALID_STATE", f"Invalid integration state: {state}")
        if connection.execute(
            "SELECT 1 FROM workers WHERE worker_id = ?", (worker_id,)
        ).fetchone() is None:
            raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")
        evidence = payload.get("evidence", [])
        if not isinstance(evidence, list):
            raise OrchestrationError(
                "INVALID_FIELD",
                "integration evidence must be a list",
            )
        target_branch = optional_text(
            payload.get("targetBranch"),
            "targetBranch",
            maximum=256,
        )
        external_operation_id = optional_text(
            payload.get("externalOperationId"),
            "externalOperationId",
            maximum=256,
        )
        connection.execute(
            """
            INSERT INTO integrations(
                worker_id, target_branch, state, external_operation_id, evidence_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                target_branch = excluded.target_branch,
                state = excluded.state,
                external_operation_id = excluded.external_operation_id,
                evidence_json = excluded.evidence_json,
                updated_at = excluded.updated_at
            """,
            (
                worker_id,
                target_branch,
                state,
                external_operation_id,
                canonical_json(evidence),
                now,
            ),
        )
        return

    if event_type == "CYCLE_STARTED":
        cycle = payload.get("cycleNumber")
        if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle <= 0:
            raise OrchestrationError("INVALID_FIELD", "cycleNumber must be positive")
        run = get_run(connection)
        if run["status"] == "COMPLETE":
            raise OrchestrationError("RUN_COMPLETE", "Cannot start a cycle after run completion")
        active_cycle = connection.execute(
            "SELECT cycle_number FROM cycles WHERE status = 'ACTIVE'"
        ).fetchone()
        if active_cycle is not None:
            raise OrchestrationError(
                "CYCLE_ALREADY_ACTIVE",
                f"Cycle {active_cycle['cycle_number']} is still active",
            )
        expected_cycle = int(run["current_cycle"]) + 1
        if cycle != expected_cycle:
            raise OrchestrationError(
                "CYCLE_MISMATCH",
                f"The next cycle must be {expected_cycle}",
                {"provided": cycle, "expected": expected_cycle},
            )
        connection.execute(
            """
            INSERT INTO cycles(
                cycle_number, status, goal_summary, validation_json, started_at
            ) VALUES (?, 'ACTIVE', ?, '[]', ?)
            """,
            (cycle, require_text(payload.get("goalSummary"), "goalSummary"), now),
        )
        connection.execute(
            """
            UPDATE run_state
            SET current_cycle = ?, status = 'ACTIVE', updated_at = ?
            WHERE singleton = 1
            """,
            (cycle, now),
        )
        return

    if event_type == "CYCLE_COMPLETED":
        cycle = payload.get("cycleNumber")
        if not isinstance(cycle, int) or isinstance(cycle, bool) or cycle <= 0:
            raise OrchestrationError("INVALID_FIELD", "cycleNumber must be positive")
        if int(get_run(connection)["current_cycle"]) != cycle:
            raise OrchestrationError(
                "CYCLE_MISMATCH",
                "Only the current cycle can be completed",
            )
        active_workers = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM workers
                WHERE state NOT IN ('ACCEPTED', 'RETIRED')
                """
            ).fetchone()[0]
        )
        unfinished_tasks = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM planned_tasks
                WHERE status NOT IN ('CANCELLED', 'ACCEPTED', 'RETIRED')
                """
            ).fetchone()[0]
        )
        pending_operations = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM operations
                WHERE status IN ('INTENT', 'UNKNOWN')
                """
            ).fetchone()[0]
        )
        if active_workers or unfinished_tasks or pending_operations:
            raise OrchestrationError(
                "CYCLE_NOT_COMPLETE",
                "Cycle cannot complete while work or operations remain",
                {
                    "activeWorkerCount": active_workers,
                    "unfinishedTaskCount": unfinished_tasks,
                    "pendingOperationCount": pending_operations,
                },
            )
        validation = payload.get("validation", [])
        if not isinstance(validation, list):
            raise OrchestrationError(
                "INVALID_FIELD",
                "cycle validation must be a list",
            )
        updated = connection.execute(
            """
            UPDATE cycles
            SET status = 'COMPLETE', result_summary = ?, validation_json = ?,
                completed_at = ?
            WHERE cycle_number = ? AND status = 'ACTIVE'
            """,
            (
                require_text(payload.get("resultSummary"), "resultSummary"),
                canonical_json(validation),
                now,
                cycle,
            ),
        )
        if updated.rowcount != 1:
            raise OrchestrationError("CYCLE_NOT_ACTIVE", f"Cycle {cycle} is not active")
        return

    if event_type == "RUN_COMPLETED":
        require_text(payload.get("summary"), "summary")
        if get_run(connection)["status"] == "COMPLETE":
            raise OrchestrationError(
                "RUN_COMPLETE",
                "The run is already complete",
            )
        active = connection.execute(
            "SELECT COUNT(*) FROM workers WHERE state NOT IN ('ACCEPTED', 'RETIRED')"
        ).fetchone()[0]
        unfinished_tasks = connection.execute(
            """
            SELECT COUNT(*) FROM planned_tasks
            WHERE status NOT IN ('CANCELLED', 'ACCEPTED', 'RETIRED')
            """
        ).fetchone()[0]
        active_cycles = connection.execute(
            "SELECT COUNT(*) FROM cycles WHERE status = 'ACTIVE'"
        ).fetchone()[0]
        pending_operations = connection.execute(
            "SELECT COUNT(*) FROM operations WHERE status IN ('INTENT','UNKNOWN')"
        ).fetchone()[0]
        if active or unfinished_tasks or active_cycles or pending_operations:
            raise OrchestrationError(
                "RUN_NOT_COMPLETE",
                "Run cannot complete while work, cycles, or operations remain",
                {
                    "activeCount": active,
                    "unfinishedTaskCount": unfinished_tasks,
                    "activeCycleCount": active_cycles,
                    "pendingOperationCount": pending_operations,
                },
            )
        connection.execute(
            "UPDATE run_state SET status = 'COMPLETE', updated_at = ? WHERE singleton = 1",
            (now,),
        )
        return

    if event_type == "RECOVERY_RECONSTRUCTED":
        require_text(payload.get("summary"), "summary")
        connection.execute(
            """
            UPDATE run_state
            SET reconstructed = 1, persistence_mode = 'LOCAL', updated_at = ?
            WHERE singleton = 1
            """,
            (now,),
        )
        return

    raise OrchestrationError("INVALID_EVENT_TYPE", f"Unsupported event type: {event_type}")


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    run_id = validate_identifier(args.run_id, "runId")
    controller_thread_id = require_text(args.controller_thread_id, "controllerThreadId", maximum=256)
    controller_host_id = args.controller_host_id
    if controller_host_id is not None:
        controller_host_id = require_text(
            controller_host_id,
            "controllerHostId",
            maximum=256,
        )
    if not isinstance(args.max_active_workers, int) or args.max_active_workers <= 0:
        raise OrchestrationError(
            "INVALID_FIELD",
            "maxActiveWorkers must be a positive integer",
        )
    goal_summary = require_text(args.goal_summary, "goalSummary")
    project_root = Path(args.project_root).expanduser().resolve() if args.project_root else None
    if project_root is not None and not project_root.is_dir():
        raise OrchestrationError(
            "PROJECT_ROOT_INVALID",
            f"Project root is not a directory: {project_root}",
        )
    if args.state_root:
        state_root = Path(args.state_root).expanduser().resolve()
    elif project_root:
        state_root = default_state_root(project_root)
    else:
        raise OrchestrationError(
            "STATE_ROOT_REQUIRED",
            "project-root or an explicit stable state-root is required",
        )

    git_details: dict[str, Any] | None = None
    if project_root:
        try:
            state_root.resolve().relative_to(project_root)
        except ValueError as exc:
            raise OrchestrationError(
                "LEDGER_PATH_OUTSIDE_PROJECT",
                "A project run ledger must resolve inside project-root",
                {
                    "projectRoot": str(project_root),
                    "stateRoot": str(state_root.resolve()),
                },
            ) from exc
        git_root = find_git_root(project_root)
        if git_root is not None:
            git_details = ensure_git_ignored(
                state_root,
                git_root,
                prepare=args.prepare_local_exclude,
            )

    run_directory = state_root / "runs" / run_id
    create_runtime_directory(run_directory)
    database_path = run_directory / "ledger.sqlite3"
    now = utc_now()

    if database_path.exists():
        with connect_database(database_path, readonly=True) as connection:
            check_schema(connection)
            run = get_run(connection)
            expected = {
                "runId": run_id,
                "controllerThreadId": controller_thread_id,
                "runLanguage": args.run_language,
            }
            actual = {
                "runId": run["run_id"],
                "controllerThreadId": run["controller_thread_id"],
                "runLanguage": run["run_language"],
            }
            if expected != actual:
                raise OrchestrationError(
                    "INIT_CONFLICT",
                    "Existing ledger does not match the requested run",
                    {"expected": expected, "actual": actual},
                )
            return result(
                True,
                "LEDGER_EXISTS",
                created=False,
                databasePath=str(database_path),
                runId=run_id,
                revision=run["revision"],
                git=git_details,
            )

    if not SCHEMA_FILE.exists():
        raise OrchestrationError("SCHEMA_UNAVAILABLE", f"Missing schema: {SCHEMA_FILE}")
    connection = connect_database(database_path, create=True)
    try:
        connection.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
        check_schema(connection)
        payload = {
            "runId": run_id,
            "runLanguage": args.run_language,
            "controllerThreadId": controller_thread_id,
            "controllerHostId": controller_host_id,
            "maxActiveWorkers": args.max_active_workers,
            "goalSummary": goal_summary,
            "reconstructed": bool(args.reconstructed),
        }
        validate_safe_data(payload)
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO run_state(
                    singleton, run_id, protocol_version, run_language,
                    controller_thread_id, controller_host_id, controller_epoch,
                    status, max_active_workers, local_preferred, current_cycle,
                    persistence_mode, reconstructed, revision, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, 1, 'ACTIVE', ?, 1, 1, 'LOCAL', ?, 0, ?, ?)
                """,
                (
                    run_id,
                    PROTOCOL_VERSION,
                    args.run_language,
                    controller_thread_id,
                    controller_host_id,
                    args.max_active_workers,
                    int(args.reconstructed),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO cycles(
                    cycle_number, status, goal_summary, validation_json, started_at
                ) VALUES (1, 'ACTIVE', ?, '[]', ?)
                """,
                (goal_summary, now),
            )
            revision, _ = append_event(
                connection,
                idempotency_key=f"{run_id}:init",
                event_type="RUN_INITIALIZED",
                payload=payload,
                created_at=now,
            )
            recompute_derived(connection, now)
    except Exception:
        connection.close()
        if database_path.exists():
            database_path.unlink()
        raise
    else:
        connection.close()
    try:
        database_path.chmod(0o600)
    except OSError:
        pass
    return result(
        True,
        "LEDGER_CREATED",
        created=True,
        databasePath=str(database_path),
        runDirectory=str(run_directory),
        runId=run_id,
        revision=revision,
        protocolVersion=PROTOCOL_VERSION,
        schemaVersion=LEDGER_SCHEMA_VERSION,
        git=git_details,
    )


def command_record(args: argparse.Namespace) -> dict[str, Any]:
    event = load_json_file(args.event_file)
    reject_unknown_fields(
        event,
        allowed={"idempotencyKey", "type", "workerId", "payload", "occurredAt"},
        field="event",
    )
    idempotency_key = require_text(event.get("idempotencyKey"), "idempotencyKey", maximum=256)
    event_type = event.get("type")
    if event_type not in RECORD_EVENT_TYPES:
        raise OrchestrationError("INVALID_EVENT_TYPE", f"Unsupported event type: {event_type}")
    worker_id = event.get("workerId")
    if worker_id is not None:
        validate_identifier(worker_id, "workerId")
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise OrchestrationError("INVALID_FIELD", "payload must be an object")
    reject_unknown_fields(
        payload,
        allowed=EVENT_PAYLOAD_FIELDS[event_type],
        field=f"{event_type} payload",
    )
    if event_type in WORKER_SCOPED_EVENTS and worker_id is None:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{event_type} requires top-level workerId",
        )
    if event_type in {"TASK_PLANNED", "TASK_REPLANNED"}:
        task_value = payload.get("task")
        if not isinstance(task_value, dict):
            raise OrchestrationError("INVALID_FIELD", "task must be an object")
        task_worker_id = validate_identifier(task_value.get("workerId"), "workerId")
        if worker_id is not None and worker_id != task_worker_id:
            raise OrchestrationError(
                "INVALID_FIELD",
                "event workerId must match task.workerId",
            )
        worker_id = task_worker_id
    occurred_at = event.get("occurredAt")
    now = (
        require_text(occurred_at, "occurredAt", maximum=64)
        if occurred_at is not None
        else utc_now()
    )
    database_path = Path(args.db)
    with connect_database(database_path) as connection:
        check_schema(connection)
        with transaction(connection):
            assert_owner(connection, args.controller_thread_id)
            existing = connection.execute(
                """
                SELECT event_type, payload_hash, revision, worker_id
                FROM events
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["event_type"] != event_type
                    or existing["payload_hash"] != stable_hash(payload)
                    or existing["worker_id"] != worker_id
                ):
                    raise OrchestrationError(
                        "IDEMPOTENCY_CONFLICT",
                        "Idempotency key was reused with different content",
                    )
                return result(
                    True,
                    "EVENT_ALREADY_APPLIED",
                    duplicate=True,
                    revision=existing["revision"],
                    idempotencyKey=idempotency_key,
                )
            if event_type == "CURSOR_UPDATED":
                requested_cursor = require_text(
                    payload.get("cursor"),
                    "cursor",
                    maximum=16384,
                )
                cursor_row = connection.execute(
                    "SELECT cursor FROM workers WHERE worker_id = ?",
                    (worker_id,),
                ).fetchone()
                if cursor_row is None:
                    raise OrchestrationError(
                        "WORKER_NOT_FOUND",
                        f"Unknown Worker: {worker_id}",
                    )
                if cursor_row["cursor"] == requested_cursor:
                    return result(
                        True,
                        "CURSOR_UNCHANGED",
                        duplicate=True,
                        noChange=True,
                        revision=get_run(connection)["revision"],
                        idempotencyKey=idempotency_key,
                    )
            mutate_record_event(connection, event_type, worker_id, payload, now)
            recompute_derived(connection, now)
            revision, _ = append_event(
                connection,
                idempotency_key=idempotency_key,
                event_type=event_type,
                payload=payload,
                created_at=now,
                worker_id=worker_id,
            )
    return result(
        True,
        "EVENT_APPLIED",
        duplicate=False,
        revision=revision,
        idempotencyKey=idempotency_key,
        eventType=event_type,
    )


def command_activate_manifest(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_json_file(args.manifest_file)
    normalized = normalize_manifest(manifest)
    if normalized != manifest:
        raise OrchestrationError(
            "MANIFEST_NOT_CANONICAL",
            "Run dispatch.py compile-manifest before activation",
        )
    manifest_hash = manifest["manifestHash"]
    database_path = Path(args.db)
    now = utc_now()
    with connect_database(database_path) as connection:
        check_schema(connection)
        with transaction(connection):
            assert_owner(connection, args.controller_thread_id)
            run = get_run(connection)
            activation_key = f"{manifest['runId']}:manifest:{manifest_hash}:activated"
            existing = connection.execute(
                "SELECT revision FROM events WHERE idempotency_key = ?",
                (activation_key,),
            ).fetchone()
            if existing is not None:
                active = get_run(connection)["current_manifest_hash"] == manifest_hash
                return result(
                    True,
                    "MANIFEST_ALREADY_ACTIVE" if active else "MANIFEST_ALREADY_RECORDED",
                    duplicate=True,
                    active=active,
                    manifestHash=manifest_hash,
                    revision=existing["revision"],
                )
            if run["status"] != "ACTIVE":
                raise OrchestrationError(
                    "RUN_NOT_ACTIVE",
                    "Manifest activation requires an ACTIVE run",
                    {"status": run["status"]},
                )
            manifest_task_ids = {
                worker["taskId"] for worker in manifest["workers"]
            }
            omitted_rows = connection.execute(
                """
                SELECT task_id, worker_id, status
                FROM planned_tasks
                WHERE status NOT IN ('CANCELLED', 'ACCEPTED', 'RETIRED')
                ORDER BY worker_id
                """
            ).fetchall()
            omitted = [
                {
                    "taskId": row["task_id"],
                    "workerId": row["worker_id"],
                    "status": row["status"],
                }
                for row in omitted_rows
                if row["task_id"] not in manifest_task_ids
            ]
            if omitted:
                raise OrchestrationError(
                    "MANIFEST_OMITS_ACTIVE_TASK",
                    "A new manifest cannot silently drop non-terminal tasks",
                    {"tasks": omitted},
                )
            mutate_record_event(
                connection,
                "MANIFEST_ACTIVATED",
                None,
                {"manifest": manifest},
                now,
            )
            activation_revision, _ = append_event(
                connection,
                idempotency_key=activation_key,
                event_type="MANIFEST_ACTIVATED",
                payload={"manifest": manifest},
                created_at=now,
            )
            task_revisions: dict[str, int] = {}
            for worker in manifest["workers"]:
                task_payload = {"task": worker}
                task = task_from_payload(task_payload)
                existing_task = connection.execute(
                    "SELECT spec_hash FROM planned_tasks WHERE task_id = ?",
                    (task["taskId"],),
                ).fetchone()
                existing_worker_task = connection.execute(
                    "SELECT task_id FROM planned_tasks WHERE worker_id = ?",
                    (task["workerId"],),
                ).fetchone()
                if (
                    existing_worker_task is not None
                    and existing_worker_task["task_id"] != task["taskId"]
                ):
                    raise OrchestrationError(
                        "TASK_CONFLICT",
                        (
                            f"Worker {task['workerId']} is already bound to "
                            f"{existing_worker_task['task_id']}"
                        ),
                    )
                if existing_task is not None:
                    if existing_task["spec_hash"] != task["specHash"]:
                        raise OrchestrationError(
                            "TASK_CONFLICT",
                            (
                                f"Task {task['taskId']} changed; apply TASK_REPLANNED "
                                "before activating this manifest"
                            ),
                        )
                    continue
                mutate_record_event(
                    connection,
                    "TASK_PLANNED",
                    worker["workerId"],
                    task_payload,
                    now,
                )
                revision, _ = append_event(
                    connection,
                    idempotency_key=(
                        f"{manifest['runId']}:task:{worker['taskId']}:"
                        f"planned:v{PROTOCOL_VERSION}"
                    ),
                    event_type="TASK_PLANNED",
                    payload=task_payload,
                    created_at=now,
                    worker_id=worker["workerId"],
                )
                task_revisions[worker["workerId"]] = revision
            recompute_derived(connection, now)
    return result(
        True,
        "MANIFEST_ACTIVATED",
        duplicate=False,
        manifestHash=manifest_hash,
        activationRevision=activation_revision,
        taskRevisions=task_revisions,
        plannedWorkerCount=len(task_revisions),
    )


def generate_operation_id(
    connection: sqlite3.Connection,
    kind: str,
    worker_id: str | None,
    controller_seq: int | None,
) -> tuple[str, int]:
    run_id = get_run(connection)["run_id"]
    if kind == "SEND_MESSAGE":
        assert worker_id is not None and controller_seq is not None
        return f"{run_id}:message:{worker_id}:{controller_seq:06d}", controller_seq
    attempt = int(
        connection.execute(
            "SELECT COUNT(*) FROM operations WHERE kind = ? AND worker_id IS ?",
            (kind, worker_id),
        ).fetchone()[0]
    ) + 1
    label = {
        "CREATE_THREAD": "create",
        "SET_TITLE": "title",
        "HANDOFF": "handoff",
    }[kind]
    target = worker_id or "controller"
    return f"{run_id}:{label}:{target}:{attempt:02d}", attempt


def normalize_operation_request(
    kind: str,
    request: dict[str, Any],
    worker_id: str | None,
) -> dict[str, Any]:
    normalized = dict(request)
    if kind == "CREATE_THREAD":
        if "prompt" in normalized:
            raise OrchestrationError(
                "SENSITIVE_FIELD",
                "Do not store the full Worker Prompt in the ledger intent",
            )
        reject_unknown_fields(
            normalized,
            allowed={"environment", "promptHash", "title", "targetHash"},
            field="CREATE_THREAD request",
        )
        prompt_hash = normalized.get("promptHash")
        if (
            not isinstance(prompt_hash, str)
            or re.fullmatch(r"[0-9a-f]{64}", prompt_hash) is None
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                "CREATE_THREAD intent requires a SHA-256 promptHash",
            )
        if normalized.get("environment") is not None:
            environment = normalized["environment"]
            if environment not in {"local", "worktree", "projectless"}:
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "CREATE_THREAD environment is invalid",
                )
        if normalized.get("title") is not None:
            normalized["title"] = require_text(
                normalized["title"],
                "title",
                maximum=256,
            )
        if normalized.get("targetHash") is not None:
            normalized["targetHash"] = optional_sha256(
                normalized["targetHash"],
                "targetHash",
            )
    elif kind == "SEND_MESSAGE":
        if "prompt" in normalized:
            raise OrchestrationError(
                "SENSITIVE_FIELD",
                "Store semantic command fields, not the rendered message Prompt",
            )
        reject_unknown_fields(
            normalized,
            allowed={
                "command",
                "reason",
                "decision",
                "instructions",
                "acceptanceDelta",
                "executionPlan",
                "stepContracts",
            },
            field="SEND_MESSAGE request",
        )
        command = normalized.get("command")
        if command not in COMMANDS:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"Invalid Controller command: {command}",
            )
        for field in ("reason", "decision"):
            if normalized.get(field) is not None:
                normalized[field] = require_text(normalized[field], field)
        for field in ("instructions", "acceptanceDelta"):
            if normalized.get(field) is not None:
                normalized[field] = require_string_list(
                    normalized[field],
                    field,
                    minimum=1,
                )
        if normalized.get("executionPlan") is not None:
            if command != "REPLAN":
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "executionPlan is allowed only for REPLAN",
                )
            normalized["executionPlan"] = normalize_execution_plan(
                normalized["executionPlan"]
            )
        if normalized.get("stepContracts") is not None:
            if command != "REPLAN":
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "stepContracts is allowed only for REPLAN",
                )
            normalized["stepContracts"] = normalize_step_contracts(
                normalized["stepContracts"]
            )
        if (
            normalized.get("executionPlan") is not None
            and normalized.get("stepContracts")
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                "Use stepContracts or legacy executionPlan, not both",
            )
    elif kind == "SET_TITLE":
        reject_unknown_fields(
            normalized,
            allowed={"title", "target"},
            field="SET_TITLE request",
        )
        normalized["title"] = require_text(
            normalized.get("title"),
            "title",
            maximum=256,
        )
        target = normalized.get("target", "worker" if worker_id else "controller")
        if target not in {"controller", "worker"}:
            raise OrchestrationError(
                "INVALID_FIELD",
                "SET_TITLE target must be controller or worker",
            )
        if target == "worker" and worker_id is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "Worker title intent requires workerId",
            )
        if target == "controller" and worker_id is not None:
            raise OrchestrationError(
                "INVALID_FIELD",
                "Controller title intent must omit workerId",
            )
        normalized["target"] = target
    elif kind == "HANDOFF":
        reject_unknown_fields(
            normalized,
            allowed={"targetBranch", "destinationHostId"},
            field="HANDOFF request",
        )
        for field in ("targetBranch", "destinationHostId"):
            if normalized.get(field) is not None:
                normalized[field] = require_text(
                    normalized[field],
                    field,
                    maximum=256,
                )
    validate_safe_data(normalized)
    return normalized


def command_intent(args: argparse.Namespace) -> dict[str, Any]:
    kind = args.kind
    if kind not in OPERATION_KINDS:
        raise OrchestrationError("INVALID_OPERATION", f"Unsupported operation kind: {kind}")
    request_id = require_text(args.request_id, "requestId", maximum=256)
    worker_id = args.worker_id
    if worker_id is not None:
        validate_identifier(worker_id, "workerId")
    request = normalize_operation_request(
        kind,
        load_json_file(args.request_file),
        worker_id,
    )
    request_hash = stable_hash(request)
    now = utc_now()
    database_path = Path(args.db)
    with connect_database(database_path) as connection:
        check_schema(connection)
        with transaction(connection):
            run = assert_owner(connection, args.controller_thread_id)
            existing = connection.execute(
                "SELECT * FROM operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["kind"] != kind
                    or existing["worker_id"] != worker_id
                    or existing["request_hash"] != request_hash
                ):
                    raise OrchestrationError(
                        "IDEMPOTENCY_CONFLICT",
                        "requestId was reused with different operation content",
                    )
                return result(
                    True,
                    "INTENT_ALREADY_RECORDED",
                    duplicate=True,
                    operationId=existing["operation_id"],
                    requestId=request_id,
                    status=existing["status"],
                    controllerSeq=existing["controller_seq"],
                )

            run_status = run["status"]
            if kind in {"CREATE_THREAD", "HANDOFF"} and run_status != "ACTIVE":
                raise OrchestrationError(
                    "RUN_NOT_ACTIVE",
                    f"{kind} requires an ACTIVE run",
                    {"status": run_status},
                )
            if kind == "SEND_MESSAGE" and run_status not in {"ACTIVE", "WAITING"}:
                raise OrchestrationError(
                    "RUN_NOT_ACTIVE",
                    "SEND_MESSAGE requires an ACTIVE or WAITING run",
                    {"status": run_status},
                )
            controller_seq: int | None = None
            attempt_value: int | None = None
            if kind in {"CREATE_THREAD", "SEND_MESSAGE", "HANDOFF"} and worker_id is None:
                raise OrchestrationError("INVALID_FIELD", f"{kind} requires workerId")
            worker = None
            if worker_id is not None:
                worker = connection.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
            unresolved_operation = connection.execute(
                """
                SELECT operation_id, request_id, status
                FROM operations
                WHERE kind = ? AND worker_id IS ?
                  AND status IN ('INTENT', 'UNKNOWN')
                ORDER BY created_at, operation_id
                LIMIT 1
                """,
                (kind, worker_id),
            ).fetchone()
            if unresolved_operation is not None:
                raise OrchestrationError(
                    "PENDING_OPERATION_EXISTS",
                    "Reconcile the existing pending operation before recording another",
                    dict(unresolved_operation),
                )
            if kind == "SET_TITLE":
                current_title = (
                    run["controller_title"]
                    if request["target"] == "controller"
                    else (worker["title"] if worker is not None else None)
                )
                if request["target"] == "worker" and worker is None:
                    raise OrchestrationError(
                        "WORKER_NOT_FOUND",
                        f"Unknown Worker: {worker_id}",
                    )
                if current_title == request["title"]:
                    return result(
                        True,
                        "TITLE_UNCHANGED",
                        duplicate=True,
                        noChange=True,
                        requestId=request_id,
                        revision=run["revision"],
                        title=current_title,
                    )

            if kind == "CREATE_THREAD":
                task = connection.execute(
                    "SELECT * FROM planned_tasks WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                if task is None:
                    raise OrchestrationError("TASK_NOT_FOUND", "CREATE_THREAD requires a planned task")
                if task["status"] != "QUEUED":
                    raise OrchestrationError(
                        "TASK_NOT_READY",
                        "CREATE_THREAD requires a QUEUED task",
                        {"status": task["status"]},
                    )
                dependencies = decode_json(task["dependencies_json"], [])
                unsatisfied_dependencies: list[dict[str, Any]] = []
                for dependency in dependencies:
                    dependency_row = connection.execute(
                        "SELECT state FROM workers WHERE worker_id = ?",
                        (dependency,),
                    ).fetchone()
                    if dependency_row is None or dependency_row["state"] != "ACCEPTED":
                        unsatisfied_dependencies.append(
                            {
                                "workerId": dependency,
                                "state": (
                                    dependency_row["state"]
                                    if dependency_row is not None
                                    else "NOT_CREATED"
                                ),
                            }
                        )
                if unsatisfied_dependencies:
                    raise OrchestrationError(
                        "DEPENDENCIES_NOT_ACCEPTED",
                        "CREATE_THREAD dependencies are not accepted",
                        {"dependencies": unsatisfied_dependencies},
                    )
                actual_active_count = int(
                    connection.execute(
                        """
                        SELECT COUNT(*) FROM workers
                        WHERE state IN ('PROVISIONING','RUNNING','REVIEW','BLOCKED')
                        """
                    ).fetchone()[0]
                )
                if worker is None and actual_active_count >= int(run["max_active_workers"]):
                    raise OrchestrationError(
                        "ACTIVE_LIMIT_REACHED",
                        "CREATE_THREAD would exceed maxActiveWorkers",
                        {
                            "activeCount": actual_active_count,
                            "maxActiveWorkers": run["max_active_workers"],
                        },
                    )
                task_boundaries = decode_json(task["write_boundary_json"], [])
                if task_boundaries:
                    allowed_pairs: set[tuple[str, str]] = set()
                    if run["current_manifest_hash"]:
                        manifest_row = connection.execute(
                            "SELECT manifest_json FROM manifests WHERE manifest_hash = ?",
                            (run["current_manifest_hash"],),
                        ).fetchone()
                        if manifest_row is not None:
                            current_manifest = decode_json(
                                manifest_row["manifest_json"],
                                {},
                            )
                            for allowance in current_manifest.get(
                                "boundaryOverlapAllowances",
                                [],
                            ):
                                pair = allowance.get("workers", [])
                                if isinstance(pair, list) and len(pair) == 2:
                                    allowed_pairs.add(tuple(sorted(pair)))
                    active_tasks = connection.execute(
                        """
                        SELECT w.worker_id, p.write_boundary_json
                        FROM workers AS w
                        JOIN planned_tasks AS p ON p.task_id = w.task_id
                        WHERE w.state IN ('PROVISIONING','RUNNING','REVIEW','BLOCKED')
                          AND w.worker_id <> ?
                        ORDER BY w.worker_id
                        """,
                        (worker_id,),
                    ).fetchall()
                    boundary_conflicts: list[dict[str, Any]] = []
                    for active_task in active_tasks:
                        pair = tuple(sorted((worker_id, active_task["worker_id"])))
                        if pair in allowed_pairs:
                            continue
                        active_boundaries = decode_json(
                            active_task["write_boundary_json"],
                            [],
                        )
                        for candidate_boundary in task_boundaries:
                            for active_boundary in active_boundaries:
                                if boundaries_overlap(candidate_boundary, active_boundary):
                                    boundary_conflicts.append(
                                        {
                                            "workerId": active_task["worker_id"],
                                            "candidateBoundary": candidate_boundary,
                                            "activeBoundary": active_boundary,
                                        }
                                    )
                    if boundary_conflicts:
                        raise OrchestrationError(
                            "WRITE_BOUNDARY_CONFLICT",
                            "CREATE_THREAD conflicts with an active Worker write boundary",
                            {"conflicts": boundary_conflicts},
                        )
                if worker is None:
                    connection.execute(
                        """
                        INSERT INTO workers(
                            worker_id, task_id, state, health, objective,
                            closed_acceptance_json, remaining_acceptance_json,
                            prompt_version, prompt_hash, created_at, updated_at
                        ) VALUES (?, ?, 'PROVISIONING', 'HEALTHY', ?, '[]', '[]', ?, ?, ?, ?)
                        """,
                        (
                            worker_id,
                            task["task_id"],
                            task["objective"],
                            PROTOCOL_VERSION,
                            request.get("promptHash"),
                            now,
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO integrations(worker_id, state, evidence_json, updated_at)
                        VALUES (?, 'NONE', '[]', ?)
                        """,
                        (worker_id, now),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE workers
                        SET prompt_version = ?, prompt_hash = ?, updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (
                            PROTOCOL_VERSION,
                            request.get("promptHash"),
                            now,
                            worker_id,
                        ),
                    )
                connection.execute(
                    "UPDATE planned_tasks SET status = 'DISPATCHING', updated_at = ? WHERE worker_id = ?",
                    (now, worker_id),
                )

            elif kind == "SEND_MESSAGE":
                if worker is None or not worker["thread_id"]:
                    raise OrchestrationError(
                        "WORKER_NOT_ADDRESSABLE",
                        "SEND_MESSAGE requires a resolved Worker threadId",
                    )
                if worker["state"] in TERMINAL_STATES:
                    raise OrchestrationError(
                        "WORKER_TERMINAL",
                        "Cannot send orchestration commands to a terminal Worker",
                    )
                command = request["command"]
                decision_round_trips = int(worker["decision_round_trips"])
                if (
                    command in MICRO_CONTROL_COMMANDS
                    and decision_round_trips >= MAX_DECISION_ROUND_TRIPS
                ):
                    raise OrchestrationError(
                        "EFFICIENCY_REVIEW_REQUIRED",
                        (
                            "Three successful micro-control round trips require "
                            "CHECKPOINT, bounded REPLAN, or STOP before another "
                            "DECISION/REVISION"
                        ),
                        {
                            "workerId": worker_id,
                            "decisionRoundTrips": decision_round_trips,
                        },
                    )
                if (
                    command == "REPLAN"
                    and decision_round_trips >= MAX_DECISION_ROUND_TRIPS
                    and request.get("executionPlan") is None
                    and not request.get("stepContracts")
                ):
                    raise OrchestrationError(
                        "EFFICIENCY_REVIEW_REQUIRED",
                        (
                            "REPLAN must include stepContracts or a legacy bounded "
                            "executionPlan after three micro-control round trips"
                        ),
                        {
                            "workerId": worker_id,
                            "decisionRoundTrips": decision_round_trips,
                        },
                    )
                controller_seq = int(worker["last_controller_seq_reserved"]) + 1
                connection.execute(
                    """
                    UPDATE workers
                    SET last_controller_seq_reserved = ?, updated_at = ?
                    WHERE worker_id = ?
                    """,
                    (controller_seq, now, worker_id),
                )

            elif kind == "HANDOFF":
                if worker is None or not worker["thread_id"]:
                    raise OrchestrationError(
                        "WORKER_NOT_ADDRESSABLE",
                        "HANDOFF requires a resolved Worker threadId",
                    )
                if worker["state"] not in {"REVIEW", "BLOCKED"}:
                    raise OrchestrationError(
                        "WORKER_NOT_READY",
                        "HANDOFF requires a REVIEW or safely checkpointed BLOCKED Worker",
                        {"state": worker["state"]},
                    )

            elif kind == "SET_TITLE":
                target = request["target"]
                if target == "worker":
                    if worker_id is None or worker is None or not worker["thread_id"]:
                        raise OrchestrationError(
                            "WORKER_NOT_ADDRESSABLE",
                            "Worker title requires a resolved Worker threadId",
                        )

            operation_id, attempt = generate_operation_id(
                connection, kind, worker_id, controller_seq
            )
            attempt_value = attempt if kind != "SEND_MESSAGE" else 1
            connection.execute(
                """
                INSERT INTO operations(
                    operation_id, request_id, kind, worker_id, status,
                    request_hash, request_json, controller_seq, attempt,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'INTENT', ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    request_id,
                    kind,
                    worker_id,
                    request_hash,
                    canonical_json(request),
                    controller_seq,
                    attempt_value,
                    now,
                    now,
                ),
            )
            recompute_derived(connection, now)
            revision, _ = append_event(
                connection,
                idempotency_key=f"{operation_id}:intent",
                event_type="OPERATION_INTENT_RECORDED",
                payload={
                    "requestId": request_id,
                    "kind": kind,
                    "workerId": worker_id,
                    "requestHash": request_hash,
                    "controllerSeq": controller_seq,
                },
                created_at=now,
                worker_id=worker_id,
                operation_id=operation_id,
            )
    return result(
        True,
        "INTENT_RECORDED",
        duplicate=False,
        revision=revision,
        operationId=operation_id,
        requestId=request_id,
        kind=kind,
        workerId=worker_id,
        controllerSeq=controller_seq,
    )


def normalize_operation_response(
    kind: str,
    status: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(response)
    allowed_by_kind = {
        "CREATE_THREAD": {"threadId", "clientThreadId", "hostId", "summary"},
        "SEND_MESSAGE": {"summary"},
        "SET_TITLE": {"summary"},
        "HANDOFF": {"operationId", "summary"},
    }
    reject_unknown_fields(
        normalized,
        allowed=allowed_by_kind[kind],
        field=f"{kind} response",
    )
    if normalized.get("summary") is not None:
        normalized["summary"] = require_text(
            normalized["summary"],
            "summary",
        )
    if kind == "CREATE_THREAD":
        for field in ("threadId", "clientThreadId", "hostId"):
            if normalized.get(field) is not None:
                normalized[field] = require_text(
                    normalized[field],
                    field,
                    maximum=256,
                )
        if status == "SUCCEEDED" and not (
            normalized.get("threadId") or normalized.get("clientThreadId")
        ):
            raise OrchestrationError(
                "INVALID_OUTCOME",
                "CREATE_THREAD success requires threadId or clientThreadId",
            )
    elif kind == "HANDOFF" and normalized.get("operationId") is not None:
        normalized["operationId"] = require_text(
            normalized["operationId"],
            "operationId",
            maximum=256,
        )
    if kind == "HANDOFF" and status == "SUCCEEDED" and not normalized.get("operationId"):
        raise OrchestrationError(
            "INVALID_OUTCOME",
            "HANDOFF success requires operationId",
        )
    return normalized


def command_outcome(args: argparse.Namespace) -> dict[str, Any]:
    status = args.status
    if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
        raise OrchestrationError("INVALID_OPERATION_STATUS", f"Invalid outcome status: {status}")
    now = utc_now()
    database_path = Path(args.db)
    with connect_database(database_path) as connection:
        check_schema(connection)
        with transaction(connection):
            assert_owner(connection, args.controller_thread_id)
            operation = connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (args.operation_id,)
            ).fetchone()
            if operation is None:
                raise OrchestrationError("OPERATION_NOT_FOUND", "Unknown operationId")
            response = normalize_operation_response(
                operation["kind"],
                status,
                load_json_file(args.response_file) if args.response_file else {},
            )
            response_hash = stable_hash(response)
            if operation["status"] in {"SUCCEEDED", "FAILED"}:
                if operation["status"] == status and operation["response_hash"] == response_hash:
                    return result(
                        True,
                        "OUTCOME_ALREADY_RECORDED",
                        duplicate=True,
                        operationId=args.operation_id,
                        status=status,
                    )
                raise OrchestrationError(
                    "OUTCOME_CONFLICT",
                    "A final operation outcome is already recorded",
                    {"existingStatus": operation["status"], "providedStatus": status},
                )
            if operation["status"] == "UNKNOWN" and status == "UNKNOWN":
                if operation["response_hash"] == response_hash:
                    return result(
                        True,
                        "OUTCOME_ALREADY_RECORDED",
                        duplicate=True,
                        operationId=args.operation_id,
                        status=status,
                    )
                raise OrchestrationError(
                    "OUTCOME_CONFLICT",
                    "UNKNOWN outcome evidence cannot be overwritten; reconcile to a final status",
                )
            connection.execute(
                """
                UPDATE operations
                SET status = ?, response_hash = ?, response_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (status, response_hash, canonical_json(response), now, args.operation_id),
            )
            kind = operation["kind"]
            worker_id = operation["worker_id"]
            request = decode_json(operation["request_json"], {})

            if kind == "CREATE_THREAD":
                if status == "SUCCEEDED":
                    thread_id = response.get("threadId")
                    client_thread_id = response.get("clientThreadId")
                    new_state = "RUNNING" if thread_id else "PROVISIONING"
                    current = connection.execute(
                        """
                        SELECT state, thread_id, client_thread_id, host_id
                        FROM workers
                        WHERE worker_id = ?
                        """,
                        (worker_id,),
                    ).fetchone()
                    if current is None:
                        raise OrchestrationError("WORKER_NOT_FOUND", "CREATE_THREAD Worker is missing")
                    for response_value, column, field in (
                        (thread_id, "thread_id", "threadId"),
                        (client_thread_id, "client_thread_id", "clientThreadId"),
                        (response.get("hostId"), "host_id", "hostId"),
                    ):
                        if (
                            response_value is not None
                            and current[column] is not None
                            and response_value != current[column]
                        ):
                            raise OrchestrationError(
                                "OUTCOME_CONFLICT",
                                f"CREATE_THREAD {field} conflicts with recorded Worker",
                                {
                                    "recorded": current[column],
                                    "received": response_value,
                                },
                            )
                    if new_state not in ALLOWED_TRANSITIONS[current["state"]]:
                        raise OrchestrationError("ILLEGAL_TRANSITION", "Cannot apply create outcome")
                    connection.execute(
                        """
                        UPDATE workers
                        SET thread_id = COALESCE(?, thread_id),
                            client_thread_id = COALESCE(?, client_thread_id),
                            host_id = COALESCE(?, host_id),
                            state = ?, health = 'HEALTHY', updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (
                            thread_id,
                            client_thread_id,
                            response.get("hostId"),
                            new_state,
                            now,
                            worker_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE planned_tasks
                        SET status = 'DISPATCHED', updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (now, worker_id),
                    )
                elif status == "FAILED":
                    current = connection.execute(
                        "SELECT state FROM workers WHERE worker_id = ?", (worker_id,)
                    ).fetchone()
                    if current and "BLOCKED" in ALLOWED_TRANSITIONS[current["state"]]:
                        connection.execute(
                            """
                            UPDATE workers
                            SET state = 'BLOCKED', health = 'STALLED',
                                result_summary = ?, updated_at = ?
                            WHERE worker_id = ?
                            """,
                            (response.get("summary", "create_thread failed"), now, worker_id),
                        )

            elif kind == "SEND_MESSAGE" and status == "SUCCEEDED":
                command = request["command"]
                if command in MICRO_CONTROL_COMMANDS:
                    connection.execute(
                        """
                        UPDATE workers
                        SET decision_round_trips = decision_round_trips + 1,
                            health = CASE
                                WHEN health = 'STALLED' THEN health
                                WHEN decision_round_trips + 1 >= ? THEN 'AT_RISK'
                                ELSE health
                            END,
                            updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (MAX_DECISION_ROUND_TRIPS, now, worker_id),
                    )
                elif (
                    command == "REPLAN"
                    and (
                        request.get("stepContracts")
                        or request.get("executionPlan") is not None
                    )
                ):
                    connection.execute(
                        """
                        UPDATE workers
                        SET decision_round_trips = 0,
                            health = CASE
                                WHEN health = 'STALLED' THEN health
                                WHEN progress_without_completion >= 3
                                  OR scope_delta_count > 0
                                  OR timeout_count > 0
                                THEN 'AT_RISK'
                                ELSE 'HEALTHY'
                            END,
                            updated_at = ?
                        WHERE worker_id = ?
                        """,
                        (now, worker_id),
                    )

            elif kind == "SET_TITLE" and status == "SUCCEEDED":
                title = request.get("title")
                target = request.get("target", "worker" if worker_id else "controller")
                if title:
                    if target == "controller":
                        connection.execute(
                            "UPDATE run_state SET controller_title = ?, updated_at = ? WHERE singleton = 1",
                            (title, now),
                        )
                    elif worker_id:
                        connection.execute(
                            "UPDATE workers SET title = ?, updated_at = ? WHERE worker_id = ?",
                            (title, now, worker_id),
                        )

            elif kind == "HANDOFF":
                integration_state = "HANDOFF_RUNNING" if status == "SUCCEEDED" else (
                    "FAILED" if status == "FAILED" else "READY"
                )
                connection.execute(
                    """
                    INSERT INTO integrations(
                        worker_id, target_branch, state, external_operation_id,
                        evidence_json, updated_at
                    ) VALUES (?, ?, ?, ?, '[]', ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        target_branch = excluded.target_branch,
                        state = excluded.state,
                        external_operation_id = excluded.external_operation_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        worker_id,
                        request.get("targetBranch"),
                        integration_state,
                        response.get("operationId"),
                        now,
                    ),
                )

            recompute_derived(connection, now)
            revision, _ = append_event(
                connection,
                idempotency_key=f"{args.operation_id}:outcome:{status}",
                event_type="OPERATION_OUTCOME_RECORDED",
                payload={
                    "status": status,
                    "responseHash": response_hash,
                    "summary": response.get("summary"),
                },
                created_at=now,
                worker_id=worker_id,
                operation_id=args.operation_id,
            )
    return result(
        True,
        "OUTCOME_RECORDED",
        duplicate=False,
        revision=revision,
        operationId=args.operation_id,
        status=status,
    )


def row_to_worker(row: sqlite3.Row) -> dict[str, Any]:
    worker = {
        "workerId": row["worker_id"],
        "taskId": row["task_id"],
        "threadId": row["thread_id"],
        "clientThreadId": row["client_thread_id"],
        "hostId": row["host_id"],
        "state": row["state"],
        "health": row["health"],
        "title": row["title"],
        "objective": row["objective"],
        "currentMilestone": row["current_milestone"],
        "closedAcceptanceItems": decode_json(row["closed_acceptance_json"], []),
        "remainingAcceptanceItems": decode_json(row["remaining_acceptance_json"], []),
        "lastDetails": decode_json(row["last_details_json"], []),
        "nextActions": decode_json(row["next_actions_json"], []),
        "needs": decode_json(row["needs_json"], []),
        "evidence": decode_json(row["evidence_json"], []),
        "lastSeq": row["last_seq"],
        "lastControllerSeq": row["last_controller_seq"],
        "lastControllerSeqReserved": row["last_controller_seq_reserved"],
        "cursor": row["cursor"],
        "resultSummary": row["result_summary"],
        "lastUsefulProgressAt": row["last_useful_progress_at"],
        "estimatedRemaining": row["estimated_remaining"],
        "progressWithoutCompletion": row["progress_without_completion"],
        "decisionRoundTrips": row["decision_round_trips"],
        "scopeDeltaCount": row["scope_delta_count"],
        "timeoutCount": row["timeout_count"],
        "nextHealthReviewAt": row["next_health_review_at"],
        "archiveReady": bool(row["archive_ready"]),
        "terminalReason": row["terminal_reason"],
        "replacementWorkerId": row["replacement_worker_id"],
        "promptVersion": row["prompt_version"],
        "promptHash": row["prompt_hash"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
    if "task_spec_json" in row.keys():
        worker.update(
            {
                "priority": row["task_priority"],
                "dependencies": decode_json(row["task_dependencies_json"], []),
                "environment": row["task_environment"],
                "writeBoundary": decode_json(row["task_write_boundary_json"], []),
                "taskSpec": decode_json(row["task_spec_json"], {}),
                "taskSpecHash": row["task_spec_hash"],
            }
        )
    return worker


def build_status(
    connection: sqlite3.Connection,
    *,
    terminal_limit: int | None = 20,
    include_events: bool = False,
) -> dict[str, Any]:
    check_schema(connection)
    run = get_run(connection)
    active_rows = connection.execute(
        """
        SELECT
            w.*,
            p.priority AS task_priority,
            p.dependencies_json AS task_dependencies_json,
            p.environment AS task_environment,
            p.write_boundary_json AS task_write_boundary_json,
            p.spec_json AS task_spec_json,
            p.spec_hash AS task_spec_hash
        FROM workers AS w
        JOIN planned_tasks AS p ON p.task_id = w.task_id
        WHERE state IN ('PROVISIONING', 'RUNNING', 'REVIEW', 'BLOCKED')
        ORDER BY w.worker_id
        """
    ).fetchall()
    if terminal_limit is None:
        terminal_rows = connection.execute(
            """
            SELECT
                w.*,
                p.priority AS task_priority,
                p.dependencies_json AS task_dependencies_json,
                p.environment AS task_environment,
                p.write_boundary_json AS task_write_boundary_json,
                p.spec_json AS task_spec_json,
                p.spec_hash AS task_spec_hash
            FROM workers AS w
            JOIN planned_tasks AS p ON p.task_id = w.task_id
            WHERE state IN ('ACCEPTED', 'RETIRED')
            ORDER BY w.updated_at DESC, w.worker_id
            """
        ).fetchall()
    else:
        terminal_rows = connection.execute(
            """
            SELECT
                w.*,
                p.priority AS task_priority,
                p.dependencies_json AS task_dependencies_json,
                p.environment AS task_environment,
                p.write_boundary_json AS task_write_boundary_json,
                p.spec_json AS task_spec_json,
                p.spec_hash AS task_spec_hash
            FROM workers AS w
            JOIN planned_tasks AS p ON p.task_id = w.task_id
            WHERE state IN ('ACCEPTED', 'RETIRED')
            ORDER BY w.updated_at DESC, w.worker_id
            LIMIT ?
            """,
            (terminal_limit,),
        ).fetchall()
    task_rows = connection.execute(
        """
        SELECT * FROM planned_tasks
        WHERE status IN ('QUEUED', 'DISPATCHING')
        ORDER BY priority, worker_id
        """
    ).fetchall()
    pending_rows = connection.execute(
        """
        SELECT operation_id, request_id, kind, worker_id, status,
               controller_seq, attempt, created_at, updated_at
        FROM operations
        WHERE status IN ('INTENT', 'UNKNOWN')
        ORDER BY created_at, operation_id
        """
    ).fetchall()
    integration_rows = connection.execute(
        "SELECT * FROM integrations ORDER BY worker_id"
    ).fetchall()
    health_review = []
    for row in active_rows:
        reasons = []
        if row["progress_without_completion"] >= 3:
            reasons.append("PROGRESS_WITHOUT_COMPLETION")
        if row["decision_round_trips"] >= 3:
            reasons.append("DECISION_ROUND_TRIPS")
        if row["scope_delta_count"] > 0:
            reasons.append("SCOPE_DELTA")
        if row["timeout_count"] > 0:
            reasons.append("TIMEOUT")
        if reasons:
            health_review.append({"workerId": row["worker_id"], "reasons": reasons})
    status: dict[str, Any] = {
        "run": {
            "runId": run["run_id"],
            "protocolVersion": run["protocol_version"],
            "runLanguage": run["run_language"],
            "controllerThreadId": run["controller_thread_id"],
            "controllerHostId": run["controller_host_id"],
            "controllerEpoch": run["controller_epoch"],
            "controllerTitle": run["controller_title"],
            "status": run["status"],
            "maxActiveWorkers": run["max_active_workers"],
            "activeCount": run["active_count"],
            "queuedCount": run["queued_count"],
            "monitorGroups": decode_json(run["monitor_groups_json"], []),
            "oneToOneSince": run["one_to_one_since"],
            "currentCycle": run["current_cycle"],
            "persistenceMode": run["persistence_mode"],
            "reconstructed": bool(run["reconstructed"]),
            "currentManifestHash": run["current_manifest_hash"],
            "revision": run["revision"],
            "updatedAt": run["updated_at"],
        },
        "activeWorkers": [row_to_worker(row) for row in active_rows],
        "recentTerminalWorkers": [row_to_worker(row) for row in terminal_rows],
        "terminalWorkerStates": {
            row["worker_id"]: row["state"]
            for row in connection.execute(
                """
                SELECT worker_id, state
                FROM workers
                WHERE state IN ('ACCEPTED', 'RETIRED')
                ORDER BY worker_id
                """
            ).fetchall()
        },
        "taskStates": {
            row["worker_id"]: row["status"]
            for row in connection.execute(
                """
                SELECT worker_id, status
                FROM planned_tasks
                ORDER BY worker_id
                """
            ).fetchall()
        },
        "queuedTasks": [
            {
                "taskId": row["task_id"],
                "workerId": row["worker_id"],
                "priority": row["priority"],
                "status": row["status"],
                "objective": row["objective"],
                "dependencies": decode_json(row["dependencies_json"], []),
                "environment": row["environment"],
                "writeBoundary": decode_json(row["write_boundary_json"], []),
                "specHash": row["spec_hash"],
            }
            for row in task_rows
        ],
        "pendingOperations": [dict(row) for row in pending_rows],
        "integrations": [
            {
                "workerId": row["worker_id"],
                "targetBranch": row["target_branch"],
                "state": row["state"],
                "externalOperationId": row["external_operation_id"],
                "evidence": decode_json(row["evidence_json"], []),
                "updatedAt": row["updated_at"],
            }
            for row in integration_rows
        ],
        "healthReviewCandidates": health_review,
    }
    if include_events:
        status["events"] = [
            {
                "revision": row["revision"],
                "idempotencyKey": row["idempotency_key"],
                "eventType": row["event_type"],
                "workerId": row["worker_id"],
                "operationId": row["operation_id"],
                "payload": decode_json(row["payload_json"], {}),
                "createdAt": row["created_at"],
            }
            for row in connection.execute("SELECT * FROM events ORDER BY revision").fetchall()
        ]
        status["decisions"] = [
            {
                "decisionId": row["decision_id"],
                "source": row["source"],
                "summary": row["summary"],
                "scope": decode_json(row["scope_json"], []),
                "workerIds": decode_json(row["worker_ids_json"], []),
                "createdAt": row["created_at"],
            }
            for row in connection.execute("SELECT * FROM decisions ORDER BY created_at").fetchall()
        ]
        status["cycles"] = [
            {
                "cycleNumber": row["cycle_number"],
                "status": row["status"],
                "goalSummary": row["goal_summary"],
                "resultSummary": row["result_summary"],
                "validation": decode_json(row["validation_json"], []),
                "startedAt": row["started_at"],
                "completedAt": row["completed_at"],
            }
            for row in connection.execute("SELECT * FROM cycles ORDER BY cycle_number").fetchall()
        ]
    return status


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not isinstance(args.terminal_limit, int)
        or isinstance(args.terminal_limit, bool)
        or args.terminal_limit < 0
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            "terminal-limit must be a non-negative integer",
        )
    with connect_database(Path(args.db), readonly=True) as connection:
        status = build_status(connection, terminal_limit=args.terminal_limit)
    return result(True, "LEDGER_STATUS", databasePath=str(Path(args.db).resolve()), **status)


def build_pending_operations(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    check_schema(connection)
    rows = connection.execute(
        """
        SELECT operation_id, request_id, kind, worker_id, status, controller_seq,
               request_json, created_at, updated_at
        FROM operations
        WHERE status IN ('INTENT', 'UNKNOWN')
        ORDER BY created_at, operation_id
        """
    ).fetchall()
    return [
        {
            **{key: row[key] for key in row.keys() if key != "request_json"},
            "request": decode_json(row["request_json"], {}),
        }
        for row in rows
    ]


def command_pending(args: argparse.Namespace) -> dict[str, Any]:
    with connect_database(Path(args.db), readonly=True) as connection:
        operations = build_pending_operations(connection)
    return result(True, "PENDING_OPERATIONS", operations=operations)


def command_manifest(args: argparse.Namespace) -> dict[str, Any]:
    with connect_database(Path(args.db), readonly=True) as connection:
        check_schema(connection)
        run = get_run(connection)
        manifest_hash = run["current_manifest_hash"]
        if manifest_hash is None:
            raise OrchestrationError(
                "MANIFEST_NOT_FOUND",
                "The run has no active compiled manifest",
            )
        row = connection.execute(
            "SELECT manifest_json FROM manifests WHERE manifest_hash = ?",
            (manifest_hash,),
        ).fetchone()
        if row is None:
            raise OrchestrationError(
                "MANIFEST_NOT_FOUND",
                "The active manifest record is missing",
            )
        manifest = decode_json(row["manifest_json"], {})
    if args.output:
        output = write_json_file(args.output, manifest, overwrite=args.overwrite)
        return result(
            True,
            "MANIFEST_EXPORTED",
            manifestHash=manifest_hash,
            outputPath=str(output.resolve()),
        )
    return result(
        True,
        "ACTIVE_MANIFEST",
        manifestHash=manifest_hash,
        manifest=manifest,
    )


def verify_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    check_schema(connection)
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    integrity = [row[0] for row in integrity_rows]
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    if integrity != ["ok"]:
        errors.append({"code": "INTEGRITY_CHECK_FAILED", "details": integrity})
    foreign_key_issues = [
        list(row)
        for row in connection.execute("PRAGMA foreign_key_check").fetchall()
    ]
    if foreign_key_issues:
        errors.append(
            {
                "code": "FOREIGN_KEY_CHECK_FAILED",
                "details": foreign_key_issues,
            }
        )
    run = get_run(connection)
    if run["protocol_version"] != PROTOCOL_VERSION:
        errors.append(
            {
                "code": "PROTOCOL_MISMATCH",
                "stored": run["protocol_version"],
                "supported": PROTOCOL_VERSION,
            }
        )
    max_revision = int(connection.execute("SELECT COALESCE(MAX(revision), 0) FROM events").fetchone()[0])
    event_count = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
    if max_revision != run["revision"]:
        errors.append(
            {
                "code": "REVISION_MISMATCH",
                "runRevision": run["revision"],
                "eventRevision": max_revision,
            }
        )
    if event_count != max_revision:
        errors.append(
            {
                "code": "EVENT_REVISION_GAP",
                "eventCount": event_count,
                "maxRevision": max_revision,
            }
        )
    invalid_event_hashes: list[int] = []
    for row in connection.execute(
        "SELECT revision, payload_hash, payload_json FROM events ORDER BY revision"
    ).fetchall():
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            invalid_event_hashes.append(int(row["revision"]))
            continue
        if stable_hash(payload) != row["payload_hash"]:
            invalid_event_hashes.append(int(row["revision"]))
    if invalid_event_hashes:
        errors.append(
            {
                "code": "EVENT_HASH_MISMATCH",
                "revisions": invalid_event_hashes,
            }
        )
    invalid_manifests: list[str] = []
    for row in connection.execute(
        "SELECT manifest_hash, manifest_json FROM manifests ORDER BY manifest_hash"
    ).fetchall():
        try:
            manifest = json.loads(row["manifest_json"])
            normalized = normalize_manifest(manifest)
        except (json.JSONDecodeError, OrchestrationError):
            invalid_manifests.append(row["manifest_hash"])
            continue
        if (
            normalized != manifest
            or manifest.get("manifestHash") != row["manifest_hash"]
        ):
            invalid_manifests.append(row["manifest_hash"])
    if invalid_manifests:
        errors.append(
            {
                "code": "MANIFEST_HASH_MISMATCH",
                "manifests": invalid_manifests,
            }
        )
    invalid_task_hashes: list[str] = []
    for row in connection.execute(
        "SELECT task_id, spec_hash, spec_json FROM planned_tasks ORDER BY task_id"
    ).fetchall():
        try:
            specification = json.loads(row["spec_json"])
        except json.JSONDecodeError:
            invalid_task_hashes.append(row["task_id"])
            continue
        if stable_hash(specification) != row["spec_hash"]:
            invalid_task_hashes.append(row["task_id"])
    if invalid_task_hashes:
        errors.append(
            {
                "code": "TASK_HASH_MISMATCH",
                "tasks": invalid_task_hashes,
            }
        )
    invalid_operation_hashes: list[str] = []
    for row in connection.execute(
        """
        SELECT operation_id, request_hash, request_json, response_hash, response_json
        FROM operations
        ORDER BY operation_id
        """
    ).fetchall():
        try:
            request = json.loads(row["request_json"])
            request_matches = stable_hash(request) == row["request_hash"]
            response_matches = row["response_hash"] is None
            if row["response_json"] is not None:
                response = json.loads(row["response_json"])
                response_matches = stable_hash(response) == row["response_hash"]
        except json.JSONDecodeError:
            request_matches = False
            response_matches = False
        if not request_matches or not response_matches:
            invalid_operation_hashes.append(row["operation_id"])
    if invalid_operation_hashes:
        errors.append(
            {
                "code": "OPERATION_HASH_MISMATCH",
                "operations": invalid_operation_hashes,
            }
        )
    invalid_json_shapes: list[str] = []
    json_checks = [
        (
            "run_state",
            "singleton",
            "monitor_groups_json",
            list,
        ),
        (
            "planned_tasks",
            "task_id",
            "dependencies_json",
            list,
        ),
        (
            "planned_tasks",
            "task_id",
            "write_boundary_json",
            list,
        ),
        (
            "workers",
            "worker_id",
            "closed_acceptance_json",
            list,
        ),
        (
            "workers",
            "worker_id",
            "remaining_acceptance_json",
            list,
        ),
        ("workers", "worker_id", "last_details_json", list),
        ("workers", "worker_id", "next_actions_json", list),
        ("workers", "worker_id", "needs_json", list),
        ("workers", "worker_id", "evidence_json", list),
        ("integrations", "worker_id", "evidence_json", list),
        ("decisions", "decision_id", "scope_json", list),
        ("decisions", "decision_id", "worker_ids_json", list),
        ("cycles", "cycle_number", "validation_json", list),
    ]
    for table, identifier_column, json_column, expected_type in json_checks:
        rows = connection.execute(
            f"SELECT {identifier_column}, {json_column} FROM {table}"
        ).fetchall()
        for row in rows:
            try:
                decoded = json.loads(row[json_column])
            except json.JSONDecodeError:
                decoded = None
            if not isinstance(decoded, expected_type):
                invalid_json_shapes.append(
                    f"{table}:{row[identifier_column]}:{json_column}"
                )
    if invalid_json_shapes:
        errors.append(
            {
                "code": "JSON_SHAPE_MISMATCH",
                "fields": invalid_json_shapes,
            }
        )
    active = int(
        connection.execute(
            "SELECT COUNT(*) FROM workers WHERE state IN ('PROVISIONING','RUNNING','REVIEW','BLOCKED')"
        ).fetchone()[0]
    )
    queued = int(
        connection.execute(
            "SELECT COUNT(*) FROM planned_tasks WHERE status = 'QUEUED'"
        ).fetchone()[0]
    )
    if active != run["active_count"] or queued != run["queued_count"]:
        errors.append(
            {
                "code": "DERIVED_COUNT_MISMATCH",
                "storedActive": run["active_count"],
                "actualActive": active,
                "storedQueued": run["queued_count"],
                "actualQueued": queued,
            }
        )
    invalid_terminal = connection.execute(
        """
        SELECT worker_id FROM workers
        WHERE
            (state IN ('ACCEPTED','RETIRED') AND (archive_ready <> 1 OR terminal_reason IS NULL))
            OR (state NOT IN ('ACCEPTED','RETIRED') AND archive_ready <> 0)
        """
    ).fetchall()
    if invalid_terminal:
        errors.append(
            {
                "code": "ARCHIVE_GATE_INVARIANT",
                "workers": [row["worker_id"] for row in invalid_terminal],
            }
        )
    invalid_sequences = connection.execute(
        """
        SELECT worker_id FROM workers
        WHERE last_controller_seq_reserved < last_controller_seq
        """
    ).fetchall()
    if invalid_sequences:
        errors.append(
            {
                "code": "SEQUENCE_INVARIANT",
                "workers": [row["worker_id"] for row in invalid_sequences],
            }
        )
    invalid_health = connection.execute(
        """
        SELECT worker_id FROM workers
        WHERE health = 'STALLED' AND state <> 'BLOCKED'
        ORDER BY worker_id
        """
    ).fetchall()
    if invalid_health:
        errors.append(
            {
                "code": "HEALTH_STATE_INVARIANT",
                "workers": [row["worker_id"] for row in invalid_health],
            }
        )
    unresolved_active = connection.execute(
        """
        SELECT worker_id FROM workers
        WHERE state IN ('RUNNING', 'REVIEW', 'BLOCKED') AND thread_id IS NULL
        ORDER BY worker_id
        """
    ).fetchall()
    if unresolved_active:
        errors.append(
            {
                "code": "WORKER_ADDRESS_INVARIANT",
                "workers": [row["worker_id"] for row in unresolved_active],
            }
        )
    terminal_task_mismatch = connection.execute(
        """
        SELECT w.worker_id
        FROM workers AS w
        JOIN planned_tasks AS p ON p.task_id = w.task_id
        WHERE
            (w.state = 'ACCEPTED' AND p.status <> 'ACCEPTED')
            OR (w.state = 'RETIRED' AND p.status <> 'RETIRED')
        ORDER BY w.worker_id
        """
    ).fetchall()
    if terminal_task_mismatch:
        errors.append(
            {
                "code": "TERMINAL_TASK_INVARIANT",
                "workers": [row["worker_id"] for row in terminal_task_mismatch],
            }
        )
    pending = int(
        connection.execute(
            "SELECT COUNT(*) FROM operations WHERE status IN ('INTENT','UNKNOWN')"
        ).fetchone()[0]
    )
    if pending:
        warnings.append({"code": "PENDING_OPERATIONS", "count": pending})
    if run["current_manifest_hash"] is not None:
        manifest_exists = connection.execute(
            "SELECT 1 FROM manifests WHERE manifest_hash = ?",
            (run["current_manifest_hash"],),
        ).fetchone()
        if manifest_exists is None:
            errors.append(
                {
                    "code": "ACTIVE_MANIFEST_MISSING",
                    "manifestHash": run["current_manifest_hash"],
                }
            )
    return {
        "valid": not errors,
        "schemaVersion": LEDGER_SCHEMA_VERSION,
        "protocolVersion": run["protocol_version"],
        "revision": run["revision"],
        "errors": errors,
        "warnings": warnings,
    }


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    with connect_database(Path(args.db), readonly=True) as connection:
        verification = verify_connection(connection)
    if not verification["valid"]:
        raise OrchestrationError("LEDGER_INVALID", "Ledger verification failed", verification)
    return result(True, "LEDGER_VALID", **verification)


def command_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if (
        not isinstance(args.terminal_limit, int)
        or isinstance(args.terminal_limit, bool)
        or args.terminal_limit < 0
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            "terminal-limit must be a non-negative integer",
        )
    database_path = Path(args.db)
    with connect_database(database_path, readonly=True) as connection:
        connection.execute("BEGIN")
        verification = verify_connection(connection)
        if not verification["valid"]:
            raise OrchestrationError(
                "LEDGER_INVALID",
                "Ledger verification failed",
                verification,
            )
        status = build_status(connection, terminal_limit=args.terminal_limit)
        operations = build_pending_operations(connection)
    return result(
        True,
        "RECOVERY_SNAPSHOT",
        databasePath=str(database_path.resolve()),
        verification=verification,
        status=status,
        pendingOperations=operations,
    )


def command_export(args: argparse.Namespace) -> dict[str, Any]:
    with connect_database(Path(args.db), readonly=True) as connection:
        exported = build_status(connection, terminal_limit=None, include_events=True)
    if args.output:
        output = write_json_file(args.output, exported, overwrite=args.overwrite)
        return result(True, "LEDGER_EXPORTED", outputPath=str(output.resolve()))
    return result(True, "LEDGER_EXPORT", **exported)


def command_audit(args: argparse.Namespace) -> dict[str, Any]:
    observed = load_json_file(args.observed_file)
    observed_workers = observed.get("workers", [])
    if not isinstance(observed_workers, list):
        raise OrchestrationError("INVALID_FIELD", "observed.workers must be a list")
    by_worker: dict[str, dict[str, Any]] = {}
    for worker in observed_workers:
        if not isinstance(worker, dict):
            raise OrchestrationError("INVALID_FIELD", "Each observed Worker must be an object")
        worker_id = validate_identifier(worker.get("workerId"), "workerId")
        if worker_id in by_worker:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"Duplicate observed Worker: {worker_id}",
            )
        by_worker[worker_id] = worker
    discrepancies: list[dict[str, Any]] = []
    with connect_database(Path(args.db), readonly=True) as connection:
        check_schema(connection)
        run = get_run(connection)
        observed_controller = observed.get("controllerThreadId")
        if observed_controller and observed_controller != run["controller_thread_id"]:
            discrepancies.append(
                {
                    "type": "CONTROLLER_OWNER_MISMATCH",
                    "ledger": run["controller_thread_id"],
                    "observed": observed_controller,
                }
            )
        ledger_workers = {
            row["worker_id"]: row
            for row in connection.execute("SELECT * FROM workers").fetchall()
        }
        for worker_id, row in ledger_workers.items():
            seen = by_worker.get(worker_id)
            if seen is None:
                discrepancies.append({"type": "WORKER_NOT_OBSERVED", "workerId": worker_id})
                continue
            for observed_key, ledger_key in (
                ("threadId", "thread_id"),
                ("hostId", "host_id"),
                ("title", "title"),
            ):
                if seen.get(observed_key) is not None and seen.get(observed_key) != row[ledger_key]:
                    discrepancies.append(
                        {
                            "type": "WORKER_FIELD_MISMATCH",
                            "workerId": worker_id,
                            "field": observed_key,
                            "ledger": row[ledger_key],
                            "observed": seen.get(observed_key),
                        }
                    )
        for worker_id in sorted(set(by_worker) - set(ledger_workers)):
            discrepancies.append({"type": "UNTRACKED_WORKER", "workerId": worker_id})
    return result(
        True,
        "AUDIT_COMPLETE",
        consistent=not discrepancies,
        discrepancies=discrepancies,
        applyAutomatically=False,
    )


def command_backup(args: argparse.Namespace) -> dict[str, Any]:
    source_path = Path(args.db).expanduser().resolve()
    with connect_database(source_path, readonly=True) as source:
        verification = verify_connection(source)
        if not verification["valid"]:
            raise OrchestrationError("LEDGER_INVALID", "Refusing to back up an invalid ledger")
        run = get_run(source)
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
        else:
            output_path = (
                source_path.parent
                / "backups"
                / f"cycle-{run['current_cycle']:04d}-r{run['revision']:06d}.sqlite3"
            )
        if output_path.exists():
            raise OrchestrationError("OUTPUT_EXISTS", f"Refusing to overwrite: {output_path}")
        create_runtime_directory(output_path.parent)
        destination = sqlite3.connect(output_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
    with connect_database(output_path, readonly=True) as copied:
        copied_verification = verify_connection(copied)
    if not copied_verification["valid"]:
        raise OrchestrationError("BACKUP_INVALID", "Backup integrity verification failed")
    return result(
        True,
        "BACKUP_CREATED",
        sourcePath=str(source_path),
        outputPath=str(output_path),
        revision=copied_verification["revision"],
    )


def command_restore(args: argparse.Namespace) -> dict[str, Any]:
    source_path = Path(args.source).expanduser().resolve()
    target_path = Path(args.target).expanduser().resolve()
    if target_path.exists():
        raise OrchestrationError("OUTPUT_EXISTS", f"Refusing to overwrite: {target_path}")
    with connect_database(source_path, readonly=True) as source:
        source_verification = verify_connection(source)
        if not source_verification["valid"]:
            raise OrchestrationError("BACKUP_INVALID", "Source backup is invalid")
        create_runtime_directory(target_path.parent)
        destination = sqlite3.connect(target_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
    try:
        target_path.chmod(0o600)
    except OSError:
        pass
    with connect_database(target_path, readonly=True) as restored:
        restored_verification = verify_connection(restored)
    if not restored_verification["valid"]:
        raise OrchestrationError("RESTORE_INVALID", "Restored database is invalid")
    return result(
        True,
        "LEDGER_RESTORED",
        sourcePath=str(source_path),
        targetPath=str(target_path),
        revision=restored_verification["revision"],
        promoted=False,
    )


def command_takeover(args: argparse.Namespace) -> dict[str, Any]:
    reason = require_text(args.authorization_note, "authorizationNote")
    new_controller = require_text(
        args.new_controller_thread_id, "newControllerThreadId", maximum=256
    )
    new_controller_host_id = optional_text(
        args.new_controller_host_id,
        "newControllerHostId",
        maximum=256,
    )
    now = utc_now()
    database_path = Path(args.db)
    with connect_database(database_path) as connection:
        check_schema(connection)
        with transaction(connection):
            run = get_run(connection)
            if run["controller_thread_id"] == new_controller:
                return result(
                    True,
                    "TAKEOVER_ALREADY_APPLIED",
                    controllerThreadId=new_controller,
                    controllerEpoch=run["controller_epoch"],
                )
            if run["controller_thread_id"] != args.expected_controller_thread_id:
                raise OrchestrationError(
                    "OWNER_CONFLICT",
                    "Expected Controller does not match the ledger owner",
                )
            new_epoch = int(run["controller_epoch"]) + 1
            payload = {
                "oldControllerThreadId": run["controller_thread_id"],
                "newControllerThreadId": new_controller,
                "newControllerHostId": new_controller_host_id,
                "controllerEpoch": new_epoch,
                "takeoverReason": reason,
            }
            connection.execute(
                """
                UPDATE run_state
                SET controller_thread_id = ?, controller_host_id = ?,
                    controller_epoch = ?, updated_at = ?
                WHERE singleton = 1
                """,
                (new_controller, new_controller_host_id, new_epoch, now),
            )
            revision, _ = append_event(
                connection,
                idempotency_key=f"{run['run_id']}:takeover:{new_epoch}",
                event_type="CONTROLLER_TAKEOVER",
                payload=payload,
                created_at=now,
            )
    return result(
        True,
        "TAKEOVER_RECORDED",
        revision=revision,
        controllerThreadId=new_controller,
        controllerHostId=new_controller_host_id,
        controllerEpoch=new_epoch,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or reopen a run ledger")
    init_parser.add_argument("--project-root")
    init_parser.add_argument("--state-root")
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--run-language", choices=("en", "zh-CN"), required=True)
    init_parser.add_argument("--controller-thread-id", required=True)
    init_parser.add_argument("--controller-host-id")
    init_parser.add_argument("--max-active-workers", type=int, default=8)
    init_parser.add_argument("--goal-summary", required=True)
    init_parser.add_argument("--prepare-local-exclude", action="store_true")
    init_parser.add_argument("--reconstructed", action="store_true")
    init_parser.set_defaults(handler=command_init)

    record_parser = subparsers.add_parser("record", help="Apply one logical state event")
    record_parser.add_argument("--db", required=True)
    record_parser.add_argument("--controller-thread-id", required=True)
    record_parser.add_argument("--event-file", required=True)
    record_parser.set_defaults(handler=command_record)

    activate_parser = subparsers.add_parser(
        "activate-manifest",
        help="Atomically activate a compiled manifest and plan its Workers",
    )
    activate_parser.add_argument("--db", required=True)
    activate_parser.add_argument("--controller-thread-id", required=True)
    activate_parser.add_argument("--manifest-file", required=True)
    activate_parser.set_defaults(handler=command_activate_manifest)

    intent_parser = subparsers.add_parser("intent", help="Record an external operation intent")
    intent_parser.add_argument("--db", required=True)
    intent_parser.add_argument("--controller-thread-id", required=True)
    intent_parser.add_argument("--request-id", required=True)
    intent_parser.add_argument("--kind", choices=sorted(OPERATION_KINDS), required=True)
    intent_parser.add_argument("--worker-id")
    intent_parser.add_argument("--request-file", required=True)
    intent_parser.set_defaults(handler=command_intent)

    outcome_parser = subparsers.add_parser("outcome", help="Record an external operation outcome")
    outcome_parser.add_argument("--db", required=True)
    outcome_parser.add_argument("--controller-thread-id", required=True)
    outcome_parser.add_argument("--operation-id", required=True)
    outcome_parser.add_argument(
        "--status", choices=("SUCCEEDED", "FAILED", "UNKNOWN"), required=True
    )
    outcome_parser.add_argument("--response-file")
    outcome_parser.set_defaults(handler=command_outcome)

    status_parser = subparsers.add_parser("status", help="Read bounded ledger status")
    status_parser.add_argument("--db", required=True)
    status_parser.add_argument("--terminal-limit", type=int, default=20)
    status_parser.set_defaults(handler=command_status)

    pending_parser = subparsers.add_parser("pending", help="List unresolved external operations")
    pending_parser.add_argument("--db", required=True)
    pending_parser.set_defaults(handler=command_pending)

    manifest_parser = subparsers.add_parser(
        "manifest",
        help="Read or export the active compiled dispatch manifest",
    )
    manifest_parser.add_argument("--db", required=True)
    manifest_parser.add_argument("--output")
    manifest_parser.add_argument("--overwrite", action="store_true")
    manifest_parser.set_defaults(handler=command_manifest)

    verify_parser = subparsers.add_parser("verify", help="Validate ledger integrity and invariants")
    verify_parser.add_argument("--db", required=True)
    verify_parser.set_defaults(handler=command_verify)

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="Read verification, bounded status, and pending operations atomically",
    )
    snapshot_parser.add_argument("--db", required=True)
    snapshot_parser.add_argument("--terminal-limit", type=int, default=20)
    snapshot_parser.set_defaults(handler=command_snapshot)

    export_parser = subparsers.add_parser("export", help="Export a sanitized full JSON snapshot")
    export_parser.add_argument("--db", required=True)
    export_parser.add_argument("--output")
    export_parser.add_argument("--overwrite", action="store_true")
    export_parser.set_defaults(handler=command_export)

    audit_parser = subparsers.add_parser("audit", help="Compare ledger state with observed tasks")
    audit_parser.add_argument("--db", required=True)
    audit_parser.add_argument("--observed-file", required=True)
    audit_parser.set_defaults(handler=command_audit)

    backup_parser = subparsers.add_parser("backup", help="Create a consistent SQLite backup")
    backup_parser.add_argument("--db", required=True)
    backup_parser.add_argument("--output")
    backup_parser.set_defaults(handler=command_backup)

    restore_parser = subparsers.add_parser("restore", help="Restore a backup to a new file")
    restore_parser.add_argument("--source", required=True)
    restore_parser.add_argument("--target", required=True)
    restore_parser.set_defaults(handler=command_restore)

    takeover_parser = subparsers.add_parser("takeover", help="Transfer Controller ownership")
    takeover_parser.add_argument("--db", required=True)
    takeover_parser.add_argument("--expected-controller-thread-id", required=True)
    takeover_parser.add_argument("--new-controller-thread-id", required=True)
    takeover_parser.add_argument("--new-controller-host-id")
    takeover_parser.add_argument("--authorization-note", required=True)
    takeover_parser.set_defaults(handler=command_takeover)

    return parser


EXIT_CODES = {
    "INVALID": 10,
    "SENSITIVE": 10,
    "INPUT": 10,
    "VALUE": 10,
    "OWNER": 11,
    "IDEMPOTENCY": 12,
    "OUTCOME_CONFLICT": 12,
    "ILLEGAL": 13,
    "ARCHIVE": 13,
    "HEALTH": 13,
    "EFFICIENCY": 13,
    "INCIDENT": 13,
    "ACTIVE": 13,
    "DEPENDENCIES": 13,
    "UNSENT": 13,
    "WORKER_NOT_READY": 13,
    "WORKER_TERMINAL": 13,
    "WORKER_CONFLICT": 13,
    "PENDING_OPERATION": 13,
    "WRITE_BOUNDARY": 13,
    "TASK_TERMINAL": 13,
    "STALE_TASK": 13,
    "USER_DECISION": 13,
    "RUN_NOT": 13,
    "RUN_COMPLETE": 13,
    "CYCLE": 13,
    "MANIFEST_OMITS": 13,
    "TASK_NOT_READY": 13,
    "WORKER_NOT_ADDRESSABLE": 13,
    "LOCAL_EXCLUDE": 14,
    "LEDGER_PATH": 14,
    "STATE_ROOT": 14,
    "SCHEMA": 15,
    "CORRUPT": 15,
    "LEDGER_INVALID": 15,
}


def exit_code_for(error_code: str) -> int:
    for prefix, code in EXIT_CODES.items():
        if error_code.startswith(prefix):
            return code
    return 1


def main(argv: list[str] | None = None) -> int:
    configure_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        output = args.handler(args)
    except OrchestrationError as exc:
        print(exc.message, file=sys.stderr)
        print_result(
            result(False, exc.code, message=exc.message, details=exc.details)
        )
        return exit_code_for(exc.code)
    except sqlite3.DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        print_result(result(False, "SQLITE_ERROR", message=str(exc)))
        return 15
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        print_result(result(False, "FILESYSTEM_ERROR", message=str(exc)))
        return 14
    print_result(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
