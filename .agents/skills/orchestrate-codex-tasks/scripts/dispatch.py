#!/usr/bin/env python3
"""Validate dispatch manifests and render deterministic Codex task requests."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from orchestration_common import (  # noqa: E402
    PROTOCOL_VERSION,
    UNRESOLVED_PLACEHOLDER_RE,
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
    validate_identifier,
    validate_safe_data,
    write_json_file,
)

REFERENCES_DIR = SCRIPT_DIR.parent / "references"
WORKER_STATES = {
    "PROVISIONING",
    "RUNNING",
    "REVIEW",
    "BLOCKED",
    "ACCEPTED",
    "RETIRED",
}
CONTROLLER_STATES = {
    "PLANNING",
    "TRACKING",
    "REPLANNING",
    "WAITING_FOR_USER",
    "SYNTHESIZING",
    "COMPLETE",
}
COMMANDS = {
    "DECISION",
    "CHECKPOINT",
    "REPLAN",
    "REVISION",
    "SCOPE_UPDATE",
    "LANGUAGE_UPDATE",
    "STOP",
}
COORDINATION_PROFILES = {"lean", "standard", "strict"}
RESOURCE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")
MANIFEST_FIELDS = {
    "protocolVersion",
    "runId",
    "runLanguage",
    "controllerThreadId",
    "controllerHostId",
    "projectId",
    "maxActiveWorkers",
    "resourceCapacities",
    "workers",
    "boundaryOverlapAllowances",
    "topologicalOrder",
    "sequentialBoundaryOverlaps",
    "manifestHash",
}
WORKER_FIELDS = {
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
COMMAND_INPUT_FIELDS = {
    "protocolVersion",
    "runId",
    "runLanguage",
    "workerId",
    "threadId",
    "hostId",
    "controllerSeq",
    "command",
    "reason",
    "decision",
    "instructions",
    "acceptanceDelta",
    "executionPlan",
    "stepContracts",
}


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


def optional_text(value: Any, field: str, *, maximum: int = 8192) -> str | None:
    if value is None:
        return None
    return require_text(value, field, maximum=maximum)


def require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be a boolean")
    return value


def normalize_resource_map(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 32:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field} must be an object with between 1 and 32 resources",
        )
    normalized: dict[str, int] = {}
    for name, quantity in sorted(value.items()):
        if not isinstance(name, str) or RESOURCE_NAME_RE.fullmatch(name) is None:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{field} has an invalid resource name: {name!r}",
            )
        if (
            not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{field}.{name} must be a positive integer",
            )
        normalized[name] = quantity
    return normalized


def effective_coordination_profile(worker: dict[str, Any]) -> str:
    explicit = worker.get("coordinationProfile")
    if explicit is not None:
        return explicit
    return "standard" if worker["writesFiles"] else "lean"


def check_protocol(value: Any) -> None:
    if value != PROTOCOL_VERSION:
        raise OrchestrationError(
            "PROTOCOL_MISMATCH",
            f"Manifest protocolVersion must be {PROTOCOL_VERSION}",
            {"provided": value, "supported": PROTOCOL_VERSION},
        )


def normalize_starting_state(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise OrchestrationError("INVALID_FIELD", f"{field} must be an object")
    state_type = value.get("type")
    if state_type == "working-tree":
        if set(value) != {"type"}:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{field} working-tree state accepts only type",
            )
        return {"type": "working-tree"}
    if state_type == "branch":
        branch_name = require_text(value.get("branchName"), f"{field}.branchName", maximum=256)
        if set(value) != {"type", "branchName"}:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"{field} branch state accepts type and branchName",
            )
        return {"type": "branch", "branchName": branch_name}
    raise OrchestrationError(
        "INVALID_FIELD",
        f"{field}.type must be working-tree or branch",
    )


def normalize_environment(
    worker: dict[str, Any],
    *,
    project_id: str | None,
    worker_index: int,
) -> tuple[dict[str, Any], str | None]:
    field = f"workers[{worker_index}].environment"
    value = worker.get("environment")
    if isinstance(value, str):
        environment: dict[str, Any] = {"type": value}
    elif isinstance(value, dict):
        environment = dict(value)
    else:
        raise OrchestrationError("INVALID_FIELD", f"{field} must be text or an object")
    environment_type = environment.get("type")
    if environment_type not in {"local", "worktree", "projectless"}:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{field}.type must be local, worktree, or projectless",
        )
    allowed_environment_fields = {
        "local": {"type"},
        "worktree": {"type", "startingState"},
        "projectless": {"type", "directoryName"},
    }[environment_type]
    reject_unknown_fields(
        environment,
        allowed=allowed_environment_fields,
        field=field,
    )

    starting_state = normalize_starting_state(
        environment.get("startingState"),
        f"{field}.startingState",
    )
    if starting_state is not None and environment_type != "worktree":
        raise OrchestrationError(
            "INVALID_FIELD",
            "startingState is valid only for a worktree environment",
        )

    project_value = (
        worker.get("projectId")
        if environment_type == "projectless"
        else worker.get("projectId", project_id)
    )
    resolved_project_id = optional_text(
        project_value,
        f"workers[{worker_index}].projectId",
        maximum=256,
    )
    if environment_type in {"local", "worktree"} and not resolved_project_id:
        raise OrchestrationError(
            "PROJECT_ID_REQUIRED",
            f"{environment_type} Worker requires a projectId returned by list_projects",
        )
    if environment_type == "projectless" and resolved_project_id is not None:
        raise OrchestrationError(
            "INVALID_FIELD",
            "projectless Worker must not declare projectId",
        )

    normalized: dict[str, Any] = {"type": environment_type}
    if starting_state is not None:
        normalized["startingState"] = starting_state
    if environment_type == "projectless":
        directory_name = optional_text(
            environment.get("directoryName"),
            f"{field}.directoryName",
            maximum=128,
        )
        if directory_name is not None:
            normalized["directoryName"] = directory_name
    return normalized, resolved_project_id


def normalize_worker(
    worker: Any,
    *,
    index: int,
    project_id: str | None,
    resource_capacities: dict[str, int] | None,
    include_default_failure_policy: bool = True,
) -> dict[str, Any]:
    if not isinstance(worker, dict):
        raise OrchestrationError("INVALID_FIELD", f"workers[{index}] must be an object")
    prefix = f"workers[{index}]"
    reject_unknown_fields(
        worker,
        allowed=WORKER_FIELDS,
        field=prefix,
    )
    task_id = validate_identifier(worker.get("taskId"), "taskId")
    worker_id = validate_identifier(worker.get("workerId"), "workerId")
    priority = worker.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise OrchestrationError("INVALID_FIELD", f"{prefix}.priority must be an integer")

    environment, resolved_project_id = normalize_environment(
        worker,
        project_id=project_id,
        worker_index=index,
    )
    boundaries = [
        normalize_boundary(item)
        for item in require_string_list(
            worker.get("writeBoundary", []),
            f"{prefix}.writeBoundary",
        )
    ]
    if len(boundaries) != len(set(boundaries)):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{prefix}.writeBoundary must not contain duplicates",
        )
    writes_files_value = worker.get("writesFiles", bool(boundaries))
    writes_files = require_bool(writes_files_value, f"{prefix}.writesFiles")
    if writes_files and not boundaries:
        raise OrchestrationError(
            "WRITE_BOUNDARY_REQUIRED",
            f"{worker_id} writes files but has no writeBoundary",
        )
    if boundaries and not writes_files:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{worker_id} has writeBoundary but writesFiles is false",
        )
    shared_local_write = worker.get("sharedLocalWriteAuthorized", False)
    shared_local_write = require_bool(
        shared_local_write,
        f"{prefix}.sharedLocalWriteAuthorized",
    )
    if environment["type"] == "local" and writes_files and not shared_local_write:
        raise OrchestrationError(
            "WORKTREE_REQUIRED",
            (
                f"{worker_id} writes files in local mode; use an independent worktree "
                "or set sharedLocalWriteAuthorized=true from explicit user authorization"
            ),
        )
    if environment["type"] != "local" and shared_local_write:
        raise OrchestrationError(
            "INVALID_FIELD",
            "sharedLocalWriteAuthorized applies only to local environments",
        )
    coordination_profile = worker.get("coordinationProfile")
    if (
        coordination_profile is not None
        and coordination_profile not in COORDINATION_PROFILES
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{prefix}.coordinationProfile must be lean, standard, or strict",
        )
    if coordination_profile == "lean" and writes_files:
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{worker_id} cannot use the lean profile while writesFiles is true",
        )
    resource_claims = None
    if "resourceClaims" in worker:
        resource_claims = normalize_resource_map(
            worker["resourceClaims"],
            f"{prefix}.resourceClaims",
        )
        if resource_capacities is None:
            raise OrchestrationError(
                "RESOURCE_CAPACITY_REQUIRED",
                f"{worker_id} resourceClaims require manifest.resourceCapacities",
            )
        unknown_resources = sorted(set(resource_claims) - set(resource_capacities))
        if unknown_resources:
            raise OrchestrationError(
                "RESOURCE_CAPACITY_REQUIRED",
                f"{worker_id} claims undeclared resources",
                {"resources": unknown_resources},
            )
        oversized = {
            name: {
                "claim": quantity,
                "capacity": resource_capacities[name],
            }
            for name, quantity in resource_claims.items()
            if quantity > resource_capacities[name]
        }
        if oversized:
            raise OrchestrationError(
                "RESOURCE_CAPACITY_EXCEEDED",
                f"{worker_id} resource claim exceeds declared capacity",
                {"resources": oversized},
            )

    dependencies = require_string_list(
        worker.get("dependencies", []),
        f"{prefix}.dependencies",
    )
    if len(dependencies) != len(set(dependencies)):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{prefix}.dependencies must not contain duplicates",
        )
    for dependency in dependencies:
        validate_identifier(dependency, "workerId")
    milestones = require_string_list(
        worker.get("milestones"),
        f"{prefix}.milestones",
        minimum=2,
        maximum=5,
    )
    if len(milestones) != len(set(milestones)):
        raise OrchestrationError(
            "INVALID_FIELD",
            f"{prefix}.milestones must not contain duplicates",
        )
    normalized = {
        "taskId": task_id,
        "workerId": worker_id,
        "priority": priority,
        "titleAction": require_text(worker.get("titleAction"), f"{prefix}.titleAction", maximum=96),
        "objective": require_text(worker.get("objective"), f"{prefix}.objective"),
        "context": require_text(worker.get("context"), f"{prefix}.context"),
        "inScope": require_string_list(
            worker.get("inScope"),
            f"{prefix}.inScope",
            minimum=1,
        ),
        "outOfScope": require_string_list(
            worker.get("outOfScope"),
            f"{prefix}.outOfScope",
            minimum=1,
        ),
        "dependencies": dependencies,
        "workerHost": require_text(
            worker.get("workerHost", "local"),
            f"{prefix}.workerHost",
            maximum=256,
        ),
        "environment": environment,
        "projectId": resolved_project_id,
        "writesFiles": writes_files,
        "sharedLocalWriteAuthorized": shared_local_write,
        "writeBoundary": boundaries,
        "integrationPlan": require_text(
            worker.get("integrationPlan"),
            f"{prefix}.integrationPlan",
        ),
        "deliverables": require_string_list(
            worker.get("deliverables"),
            f"{prefix}.deliverables",
            minimum=1,
        ),
        "acceptance": require_string_list(
            worker.get("acceptance"),
            f"{prefix}.acceptance",
            minimum=1,
        ),
        "milestones": milestones,
        "healthCheckpoint": require_text(
            worker.get("healthCheckpoint"),
            f"{prefix}.healthCheckpoint",
        ),
    }
    if coordination_profile is not None:
        normalized["coordinationProfile"] = coordination_profile
    if resource_claims is not None:
        normalized["resourceClaims"] = resource_claims
    if "failurePolicy" in worker or include_default_failure_policy:
        normalized["failurePolicy"] = normalize_failure_policy(
            worker.get("failurePolicy"),
            f"{prefix}.failurePolicy",
        )
    return normalized


def topological_order(
    workers: list[dict[str, Any]],
) -> tuple[list[str], dict[str, set[str]]]:
    by_worker = {worker["workerId"]: worker for worker in workers}
    input_order = {worker["workerId"]: index for index, worker in enumerate(workers)}
    dependents: dict[str, list[str]] = {worker_id: [] for worker_id in by_worker}
    indegree: dict[str, int] = {}
    for worker in workers:
        worker_id = worker["workerId"]
        indegree[worker_id] = len(worker["dependencies"])
        for dependency in worker["dependencies"]:
            if dependency not in by_worker:
                raise OrchestrationError(
                    "UNKNOWN_DEPENDENCY",
                    f"{worker_id} depends on unknown Worker {dependency}",
                )
            if dependency == worker_id:
                raise OrchestrationError(
                    "DEPENDENCY_CYCLE",
                    f"{worker_id} cannot depend on itself",
                )
            dependents[dependency].append(worker_id)

    ready = sorted(
        (worker_id for worker_id, count in indegree.items() if count == 0),
        key=input_order.get,
    )
    ordered: list[str] = []
    while ready:
        worker_id = ready.pop(0)
        ordered.append(worker_id)
        for dependent in sorted(dependents[worker_id], key=input_order.get):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=input_order.get)
    if len(ordered) != len(workers):
        cycle_workers = sorted(
            worker_id for worker_id, count in indegree.items() if count > 0
        )
        raise OrchestrationError(
            "DEPENDENCY_CYCLE",
            "Worker dependency graph contains a cycle",
            {"workers": cycle_workers},
        )

    ancestors: dict[str, set[str]] = {worker_id: set() for worker_id in by_worker}
    for worker_id in ordered:
        for dependency in by_worker[worker_id]["dependencies"]:
            ancestors[worker_id].add(dependency)
            ancestors[worker_id].update(ancestors[dependency])
    return ordered, ancestors


def normalize_overlap_authorizations(
    value: Any,
    known_workers: set[str],
) -> tuple[set[tuple[str, str]], list[dict[str, Any]]]:
    if value is None:
        value = []
    if not isinstance(value, list):
        raise OrchestrationError(
            "INVALID_FIELD",
            "boundaryOverlapAllowances must be a list",
        )
    pairs: set[tuple[str, str]] = set()
    normalized: list[dict[str, Any]] = []
    for index, authorization in enumerate(value):
        if not isinstance(authorization, dict):
            raise OrchestrationError(
                "INVALID_FIELD",
                f"boundaryOverlapAllowances[{index}] must be an object",
            )
        reject_unknown_fields(
            authorization,
            allowed={"workers", "reason", "coordinationPlan"},
            field=f"boundaryOverlapAllowances[{index}]",
        )
        workers = require_string_list(
            authorization.get("workers"),
            f"boundaryOverlapAllowances[{index}].workers",
            minimum=2,
            maximum=2,
        )
        if workers[0] == workers[1] or any(worker not in known_workers for worker in workers):
            raise OrchestrationError(
                "INVALID_FIELD",
                f"Invalid overlap authorization pair: {workers}",
            )
        pair = tuple(sorted(workers))
        if pair in pairs:
            raise OrchestrationError(
                "INVALID_FIELD",
                f"Duplicate overlap authorization for {pair}",
            )
        pairs.add(pair)
        normalized.append(
            {
                "workers": list(pair),
                "reason": require_text(
                    authorization.get("reason"),
                    f"boundaryOverlapAllowances[{index}].reason",
                ),
                "coordinationPlan": require_text(
                    authorization.get("coordinationPlan"),
                    f"boundaryOverlapAllowances[{index}].coordinationPlan",
                ),
            }
        )
    return pairs, normalized


def find_manifest_boundary_conflicts(
    workers: list[dict[str, Any]],
    *,
    ancestors: dict[str, set[str]],
    authorized_pairs: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    conflicts: list[dict[str, Any]] = []
    sequential_overlaps: list[dict[str, Any]] = []
    for left_index, left in enumerate(workers):
        if not left["writesFiles"]:
            continue
        for right in workers[left_index + 1 :]:
            if not right["writesFiles"]:
                continue
            pair = tuple(sorted((left["workerId"], right["workerId"])))
            ordered_by_dependency = (
                left["workerId"] in ancestors[right["workerId"]]
                or right["workerId"] in ancestors[left["workerId"]]
            )
            for left_boundary in left["writeBoundary"]:
                for right_boundary in right["writeBoundary"]:
                    if not boundaries_overlap(left_boundary, right_boundary):
                        continue
                    overlap = {
                        "leftWorkerId": left["workerId"],
                        "leftBoundary": left_boundary,
                        "rightWorkerId": right["workerId"],
                        "rightBoundary": right_boundary,
                    }
                    if ordered_by_dependency:
                        sequential_overlaps.append(overlap)
                    elif pair not in authorized_pairs:
                        conflicts.append(overlap)
    return conflicts, sequential_overlaps


def normalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    validate_safe_data(manifest)
    reject_unknown_fields(
        manifest,
        allowed=MANIFEST_FIELDS,
        field="manifest",
    )
    check_protocol(manifest.get("protocolVersion"))
    run_language = manifest.get("runLanguage")
    if run_language not in {"en", "zh-CN"}:
        raise OrchestrationError("INVALID_FIELD", "runLanguage must be en or zh-CN")
    run_id = validate_identifier(manifest.get("runId"), "runId")
    max_active = manifest.get("maxActiveWorkers", 8)
    if (
        not isinstance(max_active, int)
        or isinstance(max_active, bool)
        or max_active <= 0
    ):
        raise OrchestrationError(
            "INVALID_FIELD",
            "maxActiveWorkers must be a positive integer",
        )
    controller_thread_id = require_text(
        manifest.get("controllerThreadId"),
        "controllerThreadId",
        maximum=256,
    )
    controller_host_id = optional_text(
        manifest.get("controllerHostId"),
        "controllerHostId",
        maximum=256,
    )
    project_id = optional_text(manifest.get("projectId"), "projectId", maximum=256)
    resource_capacities = None
    if "resourceCapacities" in manifest:
        resource_capacities = normalize_resource_map(
            manifest["resourceCapacities"],
            "resourceCapacities",
        )
    worker_values = manifest.get("workers")
    if not isinstance(worker_values, list) or not 1 <= len(worker_values) <= 256:
        raise OrchestrationError(
            "INVALID_FIELD",
            "workers must contain between 1 and 256 Worker specifications",
        )
    # Preserve protocol-2 manifests compiled before failurePolicy existed. New
    # drafts receive an explicit default; legacy compiled manifests get the
    # same default only when their Worker Prompt is rendered.
    include_default_failure_policy = "manifestHash" not in manifest
    workers = [
        normalize_worker(
            worker,
            index=index,
            project_id=project_id,
            resource_capacities=resource_capacities,
            include_default_failure_policy=include_default_failure_policy,
        )
        for index, worker in enumerate(worker_values)
    ]
    worker_ids = [worker["workerId"] for worker in workers]
    task_ids = [worker["taskId"] for worker in workers]
    if len(worker_ids) != len(set(worker_ids)):
        raise OrchestrationError("DUPLICATE_WORKER", "workerId values must be unique")
    if len(task_ids) != len(set(task_ids)):
        raise OrchestrationError("DUPLICATE_TASK", "taskId values must be unique")

    ordered, ancestors = topological_order(workers)
    authorized_pairs, authorizations = normalize_overlap_authorizations(
        manifest.get("boundaryOverlapAllowances"),
        set(worker_ids),
    )
    conflicts, sequential_overlaps = find_manifest_boundary_conflicts(
        workers,
        ancestors=ancestors,
        authorized_pairs=authorized_pairs,
    )
    if conflicts:
        raise OrchestrationError(
            "WRITE_BOUNDARY_CONFLICT",
            "Parallel Workers have overlapping write boundaries without authorization",
            {"conflicts": conflicts},
        )
    normalized = {
        "protocolVersion": PROTOCOL_VERSION,
        "runId": run_id,
        "runLanguage": run_language,
        "controllerThreadId": controller_thread_id,
        "controllerHostId": controller_host_id,
        "projectId": project_id,
        "maxActiveWorkers": max_active,
        "workers": workers,
        "topologicalOrder": ordered,
        "boundaryOverlapAllowances": authorizations,
        "sequentialBoundaryOverlaps": sequential_overlaps,
    }
    if resource_capacities is not None:
        normalized["resourceCapacities"] = resource_capacities
    normalized["manifestHash"] = stable_hash(normalized)
    return normalized


def get_worker(manifest: dict[str, Any], worker_id: str) -> dict[str, Any]:
    validate_identifier(worker_id, "workerId")
    for worker in manifest["workers"]:
        if worker["workerId"] == worker_id:
            return worker
    raise OrchestrationError("WORKER_NOT_FOUND", f"Unknown Worker: {worker_id}")


def list_text(items: list[str], language: str) -> str:
    separator = "；" if language == "zh-CN" else "; "
    return separator.join(items)


def failure_policy_text(policy: dict[str, int], language: str) -> str:
    budget = policy["localCorrectionBudget"]
    if language == "zh-CN":
        return (
            f"同一可恢复控制错误最多本地纠正 {budget} 次；预期 nonzero 必须匹配"
            "步骤结果契约；实际工作步骤 timeout、未知部分写入或预算耗尽按 "
            "WORK_BLOCKER 处理；任务控制 API timeout 始终按 CONTROL_DEGRADED 处理"
        )
    return (
        f"allow at most {budget} local correction attempt(s) for the same recoverable "
        "control error; an expected nonzero must match the step result contract; treat "
        "an actual work-step timeout, unknown partial write, or exhausted budget as "
        "WORK_BLOCKER; always treat a task-control API timeout as CONTROL_DEGRADED"
    )


def extract_text_template(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OrchestrationError("TEMPLATE_UNAVAILABLE", f"Cannot read template: {path}") from exc
    match = re.search(r"```text\n(.*?)\n```", content, flags=re.DOTALL)
    if match is None:
        raise OrchestrationError(
            "INVALID_TEMPLATE",
            f"Template has no text code block: {path}",
        )
    return match.group(1)


def worker_title(
    *,
    language: str,
    run_id: str,
    worker_id: str,
    action: str,
    state: str,
    blocker: str | None = None,
    terminal_reason: str | None = None,
    efficiency_review: bool = False,
) -> str:
    identity = f"[{run_id}-{worker_id}] {action}"
    if state == "PROVISIONING":
        suffix = "Waiting for task provisioning" if language == "en" else "等待任务创建"
        return f"⌛️ {identity}{' | ' if language == 'en' else '｜'}{suffix}"
    if state == "RUNNING":
        if efficiency_review:
            suffix = "Efficiency review" if language == "en" else "效率审查"
            return f"✍️ {identity}{' | ' if language == 'en' else '｜'}{suffix}"
        return f"✍️ {identity}"
    if state == "REVIEW":
        suffix = "Awaiting Controller acceptance" if language == "en" else "等待主控验收"
        return f"🔍 {identity}{' | ' if language == 'en' else '｜'}{suffix}"
    if state == "BLOCKED":
        blocker = require_text(blocker, "blocker", maximum=128)
        return f"⌛️ {identity}{' | ' if language == 'en' else '｜'}{blocker}"
    if state == "ACCEPTED":
        return f"✅ {identity}"
    if state == "RETIRED":
        terminal_reason = require_text(terminal_reason, "terminalReason", maximum=128)
        return f"🗑️ {identity}{' | ' if language == 'en' else '｜'}{terminal_reason}"
    raise OrchestrationError("INVALID_STATE", f"Invalid Worker title state: {state}")


def controller_title(
    *,
    language: str,
    run_id: str,
    goal: str,
    state: str,
    active_count: int | None = None,
) -> str:
    if state not in CONTROLLER_STATES:
        raise OrchestrationError("INVALID_STATE", f"Invalid Controller title state: {state}")
    if language == "en":
        labels = {
            "PLANNING": "Planning",
            "REPLANNING": "Replanning",
            "WAITING_FOR_USER": "Waiting for user decision",
            "SYNTHESIZING": "Synthesizing",
            "COMPLETE": "Complete",
        }
        if state == "TRACKING":
            if (
                not isinstance(active_count, int)
                or isinstance(active_count, bool)
                or active_count < 0
            ):
                raise OrchestrationError(
                    "INVALID_FIELD",
                    "TRACKING title requires non-negative activeCount",
                )
            label = f"Tracking {active_count} Workers"
        else:
            label = labels[state]
        return f"👑 [{run_id}] {label} | {goal}"
    labels = {
        "PLANNING": "拆解",
        "REPLANNING": "重规划",
        "WAITING_FOR_USER": "等待用户确认",
        "SYNTHESIZING": "汇总",
        "COMPLETE": "完成",
    }
    if state == "TRACKING":
        if (
            not isinstance(active_count, int)
            or isinstance(active_count, bool)
            or active_count < 0
        ):
            raise OrchestrationError(
                "INVALID_FIELD",
                "TRACKING title requires non-negative activeCount",
            )
        label = f"跟进 {active_count} 个 Worker"
    else:
        label = labels[state]
    return f"👑 [{run_id}] {label}｜{goal}"


def render_worker_prompt(
    manifest: dict[str, Any],
    worker: dict[str, Any],
    *,
    template_root: Path,
) -> dict[str, Any]:
    language = manifest["runLanguage"]
    coordination_profile = effective_coordination_profile(worker)
    suffix = "" if coordination_profile == "strict" else ".compact"
    template_name = (
        f"worker-prompt.en{suffix}.md"
        if language == "en"
        else f"worker-prompt.zh-CN{suffix}.md"
    )
    prompt = extract_text_template(template_root / template_name)
    controller_host_id = manifest["controllerHostId"]
    if controller_host_id is None:
        prompt = re.sub(
            r"(?m)^- controllerHostId: \{\{CONTROLLER_HOST_ID_OR_REMOVE_LINE\}\}\n?",
            "",
            prompt,
        )
        prompt = re.sub(
            r'(?m)^\s*"hostId": "\{\{CONTROLLER_HOST_ID_OR_REMOVE_FIELD\}\}",\n?',
            "",
            prompt,
        )
    starting_state = worker["environment"].get("startingState")
    not_applicable = "not applicable" if language == "en" else "不适用"
    profile_rules = {
        ("en", "lean"): (
            "- This is read-only: do not modify files or external state.\n"
            "- If completion requires a write, report BLOCKED and wait."
        ),
        ("en", "standard"): (
            "- Modify only the declared write boundary in the independent worktree.\n"
            "- Do not hand off, commit, push, open a PR, or publish unless explicitly authorized.\n"
            "- DONE includes git status, changed files, and validation evidence."
        ),
        ("zh-CN", "lean"): (
            "- 本任务只读：不得修改文件或外部状态。\n"
            "- 若完成目标必须写入，发送 BLOCKED 并等待主控。"
        ),
        ("zh-CN", "standard"): (
            "- 只在独立 worktree 的声明写入边界内修改。\n"
            "- 未明确授权时，不得 Handoff、commit、push、开 PR 或发布。\n"
            "- DONE 附 git status、变更文件与验证证据。"
        ),
    }.get((language, coordination_profile), "")
    values = {
        "PROTOCOL_VERSION": str(PROTOCOL_VERSION),
        "RUN_ID": manifest["runId"],
        "WORKER_ID": worker["workerId"],
        "CONTROLLER_THREAD_ID": manifest["controllerThreadId"],
        "CONTROLLER_HOST_ID_OR_REMOVE_LINE": controller_host_id or "",
        "CONTROLLER_HOST_ID_OR_REMOVE_FIELD": controller_host_id or "",
        "OBJECTIVE": worker["objective"],
        "CONTEXT": worker["context"],
        "IN_SCOPE": list_text(worker["inScope"], language),
        "OUT_OF_SCOPE": list_text(worker["outOfScope"], language),
        "DEPENDENCIES": list_text(worker["dependencies"], language)
        if worker["dependencies"]
        else ("none" if language == "en" else "无"),
        "WORKER_HOST": worker["workerHost"],
        "LOCAL_OR_WORKTREE_OR_PROJECTLESS": worker["environment"]["type"],
        "STARTING_STATE_OR_NOT_APPLICABLE": canonical_json(starting_state)
        if starting_state is not None
        else not_applicable,
        "WRITE_BOUNDARY": list_text(worker["writeBoundary"], language)
        if worker["writeBoundary"]
        else not_applicable,
        "INTEGRATION_PLAN": worker["integrationPlan"],
        "DELIVERABLES": list_text(worker["deliverables"], language),
        "ACCEPTANCE": list_text(worker["acceptance"], language),
        "MILESTONES": list_text(worker["milestones"], language),
        "HEALTH_CHECKPOINT": worker["healthCheckpoint"],
        "FAILURE_POLICY": failure_policy_text(
            normalize_failure_policy(
                worker.get("failurePolicy"),
                f"{worker['workerId']}.failurePolicy",
            ),
            language,
        ),
        "COORDINATION_PROFILE": coordination_profile,
        "PROFILE_RULES": profile_rules,
        "RESOURCE_CLAIMS": canonical_json(worker.get("resourceClaims", {}))
        if worker.get("resourceClaims")
        else not_applicable,
        "SEQ": "<SEQ>",
        "TYPE": "<TYPE>",
    }
    for key, value in values.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", value)
    unresolved = sorted(set(UNRESOLVED_PLACEHOLDER_RE.findall(prompt)))
    if unresolved:
        raise OrchestrationError(
            "UNRESOLVED_PLACEHOLDER",
            "Rendered Worker Prompt contains unresolved placeholders",
            {"placeholders": unresolved},
        )
    validate_safe_data(prompt)

    environment = dict(worker["environment"])
    if environment["type"] == "projectless":
        target: dict[str, Any] = {"type": "projectless"}
        if "directoryName" in environment:
            target["directoryName"] = environment["directoryName"]
    else:
        target = {
            "type": "project",
            "projectId": worker["projectId"],
            "environment": environment,
        }
    title = worker_title(
        language=language,
        run_id=manifest["runId"],
        worker_id=worker["workerId"],
        action=worker["titleAction"],
        state="RUNNING",
    )
    prompt_hash = stable_hash(prompt)
    return {
        "workerId": worker["workerId"],
        "taskId": worker["taskId"],
        "coordinationProfile": coordination_profile,
        "resourceClaims": worker.get("resourceClaims", {}),
        "title": title,
        "prompt": prompt,
        "promptHash": prompt_hash,
        "createThread": {"prompt": prompt, "target": target},
        "ledgerIntentRequest": {
            "environment": environment["type"],
            "promptHash": prompt_hash,
            "title": title,
            "targetHash": stable_hash(target),
        },
    }


def planned_event(manifest: dict[str, Any], worker: dict[str, Any]) -> dict[str, Any]:
    task = {
        key: worker[key]
        for key in (
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
        )
    }
    for optional_key in ("coordinationProfile", "resourceClaims", "failurePolicy"):
        if optional_key in worker:
            task[optional_key] = worker[optional_key]
    return {
        "idempotencyKey": (
            f"{manifest['runId']}:task:{worker['taskId']}:planned:v{PROTOCOL_VERSION}"
        ),
        "type": "TASK_PLANNED",
        "workerId": worker["workerId"],
        "payload": {"task": task},
    }


def worker_states(status: dict[str, Any]) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    expected_states = {
        "activeWorkers": {"PROVISIONING", "RUNNING", "REVIEW", "BLOCKED"},
        "recentTerminalWorkers": {"ACCEPTED", "RETIRED"},
    }
    for key, allowed_states in expected_states.items():
        values = status.get(key, [])
        if not isinstance(values, list):
            raise OrchestrationError("INVALID_STATUS", f"{key} must be a list")
        for worker in values:
            if not isinstance(worker, dict):
                raise OrchestrationError("INVALID_STATUS", f"{key} entries must be objects")
            worker_id = validate_identifier(worker.get("workerId"), "workerId")
            state = worker.get("state")
            if state not in allowed_states:
                raise OrchestrationError(
                    "INVALID_STATUS",
                    f"Invalid {key} state for {worker_id}: {state}",
                )
            if worker_id in states and states[worker_id].get("state") != state:
                raise OrchestrationError(
                    "INVALID_STATUS",
                    f"Conflicting Worker states for {worker_id}",
                )
            states[worker_id] = worker
    terminal_states = status.get("terminalWorkerStates", {})
    if not isinstance(terminal_states, dict):
        raise OrchestrationError("INVALID_STATUS", "terminalWorkerStates must be an object")
    for worker_id, state in terminal_states.items():
        validate_identifier(worker_id, "workerId")
        if state not in {"ACCEPTED", "RETIRED"}:
            raise OrchestrationError(
                "INVALID_STATUS",
                f"Invalid terminal state for {worker_id}: {state}",
            )
        existing = states.get(worker_id)
        if existing is not None and existing.get("state") != state:
            raise OrchestrationError(
                "INVALID_STATUS",
                f"Conflicting terminal state for {worker_id}",
            )
        states.setdefault(worker_id, {"workerId": worker_id, "state": state})
    return states


def ready_workers(manifest: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    status_run = status.get("run")
    if not isinstance(status_run, dict):
        raise OrchestrationError("INVALID_STATUS", "status.run must be an object")
    if status_run.get("runId") != manifest["runId"]:
        raise OrchestrationError("RUN_MISMATCH", "Manifest and ledger status runId differ")
    check_protocol(status_run.get("protocolVersion"))
    if status_run.get("runLanguage") != manifest["runLanguage"]:
        raise OrchestrationError(
            "LANGUAGE_MISMATCH",
            "Manifest and ledger status runLanguage differ",
        )
    if status_run.get("currentManifestHash") != manifest["manifestHash"]:
        raise OrchestrationError(
            "MANIFEST_MISMATCH",
            "ready requires the ledger's currently active compiled manifest",
            {
                "manifest": manifest["manifestHash"],
                "ledger": status_run.get("currentManifestHash"),
            },
        )
    if status_run.get("status") != "ACTIVE":
        raise OrchestrationError(
            "RUN_NOT_ACTIVE",
            "New Worker dispatch is allowed only while the run is ACTIVE",
            {"status": status_run.get("status")},
        )
    states = worker_states(status)
    task_states = status.get("taskStates")
    if not isinstance(task_states, dict):
        raise OrchestrationError("INVALID_STATUS", "taskStates must be an object")
    for worker_id, task_state in task_states.items():
        validate_identifier(worker_id, "workerId")
        if task_state not in {
            "QUEUED",
            "DISPATCHING",
            "DISPATCHED",
            "CANCELLED",
            "ACCEPTED",
            "RETIRED",
        }:
            raise OrchestrationError(
                "INVALID_STATUS",
                f"Invalid task state for {worker_id}: {task_state}",
            )
    pending_values = status.get("pendingOperations", [])
    if not isinstance(pending_values, list):
        raise OrchestrationError("INVALID_STATUS", "pendingOperations must be a list")
    pending_create: set[str] = set()
    for item in pending_values:
        if not isinstance(item, dict):
            raise OrchestrationError(
                "INVALID_STATUS",
                "pendingOperations entries must be objects",
            )
        if item.get("kind") != "CREATE_THREAD":
            continue
        pending_worker_id = item.get("worker_id", item.get("workerId"))
        pending_create.add(validate_identifier(pending_worker_id, "workerId"))
    active_ids = {
        worker_id
        for worker_id, worker in states.items()
        if worker.get("state") in {"PROVISIONING", "RUNNING", "REVIEW", "BLOCKED"}
    }
    accepted_ids = {
        worker_id
        for worker_id, worker in states.items()
        if worker.get("state") == "ACCEPTED"
    }
    retired_ids = {
        worker_id
        for worker_id, worker in states.items()
        if worker.get("state") == "RETIRED"
    }
    runtime_limit = status_run.get("maxActiveWorkers")
    if (
        not isinstance(runtime_limit, int)
        or isinstance(runtime_limit, bool)
        or runtime_limit <= 0
    ):
        raise OrchestrationError("INVALID_STATUS", "run.maxActiveWorkers must be positive")
    slots = max(
        0,
        min(runtime_limit, manifest["maxActiveWorkers"]) - len(active_ids),
    )
    by_worker = {worker["workerId"]: worker for worker in manifest["workers"]}
    resource_capacities = manifest.get("resourceCapacities", {})
    resource_usage = {name: 0 for name in resource_capacities}
    resource_holders: dict[str, list[str]] = {
        name: [] for name in resource_capacities
    }
    for active_worker_id in sorted(active_ids):
        active_state = states[active_worker_id].get("state")
        active_worker = by_worker.get(active_worker_id)
        if (
            active_state not in {"PROVISIONING", "RUNNING"}
            or active_worker is None
        ):
            continue
        for resource, quantity in active_worker.get("resourceClaims", {}).items():
            resource_usage[resource] += quantity
            resource_holders[resource].append(active_worker_id)
    unknown_active_ids = sorted(active_ids - set(by_worker))
    unknown_pending_create = sorted(
        worker_id
        for worker_id in pending_create
        if worker_id is not None and worker_id not in by_worker
    )
    order_index = {
        worker_id: index for index, worker_id in enumerate(manifest["topologicalOrder"])
    }
    candidates = sorted(
        manifest["workers"],
        key=lambda worker: (
            worker["priority"],
            order_index[worker["workerId"]],
            worker["workerId"],
        ),
    )
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for worker in candidates:
        worker_id = worker["workerId"]
        reasons: list[str] = []
        if unknown_active_ids:
            reasons.append("UNTRACKED_ACTIVE_WORKERS:" + ",".join(unknown_active_ids))
        if unknown_pending_create:
            reasons.append(
                "UNTRACKED_PENDING_CREATE:" + ",".join(unknown_pending_create)
            )
        task_state = task_states.get(worker_id)
        if task_state is None:
            reasons.append("TASK_NOT_PLANNED")
        elif task_state != "QUEUED":
            reasons.append(f"TASK_NOT_QUEUED:{task_state}")
        if worker_id in active_ids:
            reasons.append("ALREADY_ACTIVE")
        elif worker_id in accepted_ids:
            reasons.append("ALREADY_ACCEPTED")
        elif worker_id in retired_ids:
            reasons.append("RETIRED")
        if worker_id in pending_create:
            reasons.append("CREATE_INTENT_PENDING")
        missing = [
            dependency
            for dependency in worker["dependencies"]
            if dependency not in accepted_ids
        ]
        if missing:
            reasons.append("DEPENDENCIES_NOT_ACCEPTED:" + ",".join(missing))
        for other_id in active_ids | selected_ids:
            other = by_worker.get(other_id)
            if other is None or not worker["writesFiles"] or not other["writesFiles"]:
                continue
            authorized = any(
                set(authorization["workers"]) == {worker_id, other_id}
                for authorization in manifest["boundaryOverlapAllowances"]
            )
            if authorized:
                continue
            if any(
                boundaries_overlap(left, right)
                for left in worker["writeBoundary"]
                for right in other["writeBoundary"]
            ):
                reasons.append(f"ACTIVE_WRITE_BOUNDARY_CONFLICT:{other_id}")
        for resource, quantity in worker.get("resourceClaims", {}).items():
            if resource_usage[resource] + quantity > resource_capacities[resource]:
                reasons.append(f"RESOURCE_CAPACITY_EXCEEDED:{resource}")
        if reasons:
            blocked.append({"workerId": worker_id, "reasons": sorted(set(reasons))})
            continue
        if len(ready) >= slots:
            blocked.append({"workerId": worker_id, "reasons": ["NO_ACTIVE_SLOT"]})
            continue
        ready.append(
            {
                "workerId": worker_id,
                "taskId": worker["taskId"],
                "priority": worker["priority"],
                "dependencies": worker["dependencies"],
                "environment": worker["environment"]["type"],
                "writeBoundary": worker["writeBoundary"],
                "coordinationProfile": effective_coordination_profile(worker),
                "resourceClaims": worker.get("resourceClaims", {}),
            }
        )
        selected_ids.add(worker_id)
        for resource, quantity in worker.get("resourceClaims", {}).items():
            resource_usage[resource] += quantity
            resource_holders[resource].append(worker_id)
    return {
        "slotsAvailable": slots,
        "readyWorkers": ready,
        "notReady": blocked,
        "activeWorkerIds": sorted(active_ids),
        "acceptedWorkerIds": sorted(accepted_ids),
        "resourceUsage": {
            resource: {
                "used": resource_usage[resource],
                "capacity": capacity,
                "holders": resource_holders[resource],
            }
            for resource, capacity in resource_capacities.items()
        },
    }


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    manifest = normalize_manifest(load_json_file(args.manifest_file))
    return result(
        True,
        "MANIFEST_VALID",
        protocolVersion=PROTOCOL_VERSION,
        runId=manifest["runId"],
        runLanguage=manifest["runLanguage"],
        workerCount=len(manifest["workers"]),
        maxActiveWorkers=manifest["maxActiveWorkers"],
        topologicalOrder=manifest["topologicalOrder"],
        sequentialBoundaryOverlaps=manifest["sequentialBoundaryOverlaps"],
        manifestHash=manifest["manifestHash"],
    )


def command_compile(args: argparse.Namespace) -> dict[str, Any]:
    manifest = normalize_manifest(load_json_file(args.manifest_file))
    output = write_json_file(args.output, manifest, overwrite=args.overwrite)
    return result(
        True,
        "MANIFEST_COMPILED",
        runId=manifest["runId"],
        workerCount=len(manifest["workers"]),
        manifestHash=manifest["manifestHash"],
        outputPath=str(output.resolve()),
    )


def command_plan_events(args: argparse.Namespace) -> dict[str, Any]:
    manifest = normalize_manifest(load_json_file(args.manifest_file))
    return result(
        True,
        "PLAN_EVENTS_RENDERED",
        runId=manifest["runId"],
        events=[planned_event(manifest, worker) for worker in manifest["workers"]],
    )


def command_ready(args: argparse.Namespace) -> dict[str, Any]:
    manifest = normalize_manifest(load_json_file(args.manifest_file))
    status = load_json_file(args.status_file)
    scheduling = ready_workers(manifest, status)
    return result(True, "READY_WORKERS", runId=manifest["runId"], **scheduling)


def command_render_worker(args: argparse.Namespace) -> dict[str, Any]:
    manifest = normalize_manifest(load_json_file(args.manifest_file))
    worker = get_worker(manifest, args.worker_id)
    template_root = Path(args.template_root).resolve() if args.template_root else REFERENCES_DIR
    rendered = render_worker_prompt(manifest, worker, template_root=template_root)
    return result(True, "WORKER_PROMPT_RENDERED", **rendered)


def command_render_command(args: argparse.Namespace) -> dict[str, Any]:
    value = load_json_file(args.input_file)
    reject_unknown_fields(
        value,
        allowed=COMMAND_INPUT_FIELDS,
        field="Controller command",
    )
    check_protocol(value.get("protocolVersion"))
    run_id = validate_identifier(value.get("runId"), "runId")
    worker_id = validate_identifier(value.get("workerId"), "workerId")
    language = value.get("runLanguage")
    if language not in {"en", "zh-CN"}:
        raise OrchestrationError("INVALID_FIELD", "runLanguage must be en or zh-CN")
    command = value.get("command")
    if command not in COMMANDS:
        raise OrchestrationError("INVALID_FIELD", f"Invalid command: {command}")
    sequence = value.get("controllerSeq")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence <= 0
    ):
        raise OrchestrationError("INVALID_SEQUENCE", "controllerSeq must be positive")
    thread_id = require_text(value.get("threadId"), "threadId", maximum=256)
    host_id = optional_text(value.get("hostId"), "hostId", maximum=256)
    reason = optional_text(value.get("reason"), "reason") or "none"
    decision = optional_text(value.get("decision"), "decision") or "none"
    instructions = require_string_list(
        value.get("instructions"),
        "instructions",
        minimum=1,
    )
    acceptance_delta = require_string_list(
        value.get("acceptanceDelta", ["none"]),
        "acceptanceDelta",
        minimum=1,
    )
    execution_plan = None
    if value.get("executionPlan") is not None:
        if command != "REPLAN":
            raise OrchestrationError(
                "INVALID_FIELD",
                "executionPlan is allowed only for REPLAN",
            )
        execution_plan = normalize_execution_plan(value["executionPlan"])
    step_contracts = normalize_step_contracts(value.get("stepContracts"))
    if step_contracts and command != "REPLAN":
        raise OrchestrationError(
            "INVALID_FIELD",
            "stepContracts is allowed only for REPLAN",
        )
    if execution_plan is not None and step_contracts:
        raise OrchestrationError(
            "INVALID_FIELD",
            "Use stepContracts or legacy executionPlan, not both",
        )
    prompt_lines = [
        (
            f"[ORCH run={run_id} worker={worker_id} "
            f"controllerSeq={sequence:03d} command={command}]"
        ),
        f"language: {language}",
        f"reason: {reason}",
        f"decision: {decision}",
        "instructions:",
        *[f"- {instruction}" for instruction in instructions],
        "acceptanceDelta:",
        *[f"- {item}" for item in acceptance_delta],
    ]
    if execution_plan is not None:
        prompt_lines.extend(
            [
                "executionPlan:",
                *[f"- {step}" for step in execution_plan["steps"]],
                (
                    "executionLimits: "
                    f"maxWallTimeMinutes={execution_plan['maxWallTimeMinutes']}; "
                    "stopOnFirstNonzero=true; stopOnTimeout=true"
                ),
            ]
        )
    if step_contracts:
        prompt_lines.extend(
            [
                "stepContracts:",
                *[f"- {canonical_json(contract)}" for contract in step_contracts],
            ]
        )
    prompt = "\n".join(prompt_lines)
    validate_safe_data(prompt)
    send_message: dict[str, Any] = {"threadId": thread_id, "prompt": prompt}
    if host_id is not None:
        send_message["hostId"] = host_id
    return result(
        True,
        "CONTROLLER_COMMAND_RENDERED",
        runId=run_id,
        workerId=worker_id,
        controllerSeq=sequence,
        command=command,
        executionPlan=execution_plan,
        stepContracts=step_contracts,
        prompt=prompt,
        promptHash=stable_hash(prompt),
        sendMessage=send_message,
    )


def command_render_title(args: argparse.Namespace) -> dict[str, Any]:
    value = load_json_file(args.input_file)
    language = value.get("runLanguage")
    if language not in {"en", "zh-CN"}:
        raise OrchestrationError("INVALID_FIELD", "runLanguage must be en or zh-CN")
    run_id = validate_identifier(value.get("runId"), "runId")
    scope = value.get("scope")
    if scope == "controller":
        state = value.get("state")
        title = controller_title(
            language=language,
            run_id=run_id,
            goal=require_text(value.get("goal"), "goal", maximum=128),
            state=state,
            active_count=value.get("activeCount"),
        )
    elif scope == "worker":
        state = value.get("state")
        if state not in WORKER_STATES:
            raise OrchestrationError("INVALID_STATE", f"Invalid Worker state: {state}")
        if state in {"ACCEPTED", "RETIRED"}:
            require_bool(value.get("archiveReady"), "archiveReady")
            if value.get("archiveReady") is not True:
                raise OrchestrationError(
                    "ARCHIVE_GATE_FAILED",
                    "Terminal title requires archiveReady=true",
                )
        efficiency_review = value.get("efficiencyReview", False)
        require_bool(efficiency_review, "efficiencyReview")
        title = worker_title(
            language=language,
            run_id=run_id,
            worker_id=validate_identifier(value.get("workerId"), "workerId"),
            action=require_text(value.get("action"), "action", maximum=96),
            state=state,
            blocker=optional_text(value.get("blocker"), "blocker", maximum=128),
            terminal_reason=optional_text(
                value.get("terminalReason"),
                "terminalReason",
                maximum=128,
            ),
            efficiency_review=efficiency_review,
        )
    else:
        raise OrchestrationError("INVALID_FIELD", "scope must be controller or worker")
    return result(True, "TITLE_RENDERED", scope=scope, title=title)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate-manifest",
        help="Validate a bounded Worker dispatch manifest",
    )
    validate_parser.add_argument("--manifest-file", required=True)
    validate_parser.set_defaults(handler=command_validate)

    compile_parser = subparsers.add_parser(
        "compile-manifest",
        help="Validate and save a canonical recovery manifest",
    )
    compile_parser.add_argument("--manifest-file", required=True)
    compile_parser.add_argument("--output", required=True)
    compile_parser.add_argument("--overwrite", action="store_true")
    compile_parser.set_defaults(handler=command_compile)

    events_parser = subparsers.add_parser(
        "plan-events",
        help="Render idempotent TASK_PLANNED ledger events",
    )
    events_parser.add_argument("--manifest-file", required=True)
    events_parser.set_defaults(handler=command_plan_events)

    ready_parser = subparsers.add_parser(
        "ready",
        help="Choose dependency-, slot-, and boundary-safe Workers",
    )
    ready_parser.add_argument("--manifest-file", required=True)
    ready_parser.add_argument("--status-file", required=True)
    ready_parser.set_defaults(handler=command_ready)

    worker_parser = subparsers.add_parser(
        "render-worker",
        help="Render one localized Worker Prompt and create_thread request",
    )
    worker_parser.add_argument("--manifest-file", required=True)
    worker_parser.add_argument("--worker-id", required=True)
    worker_parser.add_argument("--template-root")
    worker_parser.set_defaults(handler=command_render_worker)

    command_parser = subparsers.add_parser(
        "render-command",
        help="Render one sequenced Controller-to-Worker message",
    )
    command_parser.add_argument("--input-file", required=True)
    command_parser.set_defaults(handler=command_render_command)

    title_parser = subparsers.add_parser(
        "render-title",
        help="Render a localized lifecycle title",
    )
    title_parser.add_argument("--input-file", required=True)
    title_parser.set_defaults(handler=command_render_title)
    return parser


EXIT_CODES = {
    "INVALID": 10,
    "SENSITIVE": 10,
    "VALUE": 10,
    "INPUT": 10,
    "PROTOCOL": 11,
    "DEPENDENCY": 12,
    "UNKNOWN_DEPENDENCY": 12,
    "WRITE_BOUNDARY": 13,
    "WORKTREE": 13,
    "RESOURCE": 13,
    "ARCHIVE": 13,
    "PROJECT_ID": 14,
    "TEMPLATE": 15,
    "UNRESOLVED": 15,
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
        print_result(result(False, exc.code, message=exc.message, details=exc.details))
        return exit_code_for(exc.code)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        print_result(result(False, "FILESYSTEM_ERROR", message=str(exc)))
        return 14
    print_result(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
