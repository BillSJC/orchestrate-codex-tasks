from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LEDGER = (
    REPOSITORY_ROOT
    / ".agents"
    / "skills"
    / "orchestrate-codex-tasks"
    / "scripts"
    / "ledger.py"
)
DISPATCH = LEDGER.with_name("dispatch.py")


class LedgerCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_root = self.root / "state"
        self.run_id = "run-test"
        self.controller = "controller-thread"
        self.database = (
            self.state_root / "runs" / self.run_id / "ledger.sqlite3"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self,
        *arguments: str,
        expected_returncode: int = 0,
    ) -> dict[str, object]:
        process = subprocess.run(
            [sys.executable, str(LEDGER), *arguments],
            cwd=REPOSITORY_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            expected_returncode,
            msg=f"stderr:\n{process.stderr}\nstdout:\n{process.stdout}",
        )
        self.assertTrue(process.stdout.strip(), msg=process.stderr)
        return json.loads(process.stdout.splitlines()[-1])

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return path

    def compile_manifest(
        self,
        manifest: dict[str, object],
        name: str,
    ) -> tuple[Path, dict[str, object]]:
        raw_path = self.write_json(f"{name}-raw.json", manifest)
        compiled_path = self.root / f"{name}-compiled.json"
        process = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "compile-manifest",
                "--manifest-file",
                str(raw_path),
                "--output",
                str(compiled_path),
            ],
            cwd=REPOSITORY_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=f"stderr:\n{process.stderr}\nstdout:\n{process.stdout}",
        )
        return compiled_path, json.loads(compiled_path.read_text(encoding="utf-8"))

    def initialize(self, *extra: str) -> dict[str, object]:
        return self.invoke(
            "init",
            "--state-root",
            str(self.state_root),
            "--run-id",
            self.run_id,
            "--run-language",
            "zh-CN",
            "--controller-thread-id",
            self.controller,
            "--max-active-workers",
            "8",
            "--goal-summary",
            "验证可恢复调度账本",
            *extra,
        )

    def worker_spec(
        self,
        worker_id: str,
        *,
        dependencies: list[str] | None = None,
        write_boundary: str | None = None,
    ) -> dict[str, object]:
        boundary = write_boundary or f"src/{worker_id.lower()}/**"
        return {
            "taskId": f"task-{worker_id}",
            "workerId": worker_id,
            "priority": 10,
            "titleAction": f"实现 {worker_id}",
            "objective": f"实现 {worker_id}",
            "context": "接口已冻结",
            "inScope": [boundary],
            "outOfScope": ["发布"],
            "dependencies": dependencies or [],
            "environment": {"type": "worktree"},
            "writesFiles": True,
            "writeBoundary": [boundary],
            "integrationPlan": "主控验收后 Handoff",
            "deliverables": ["实现"],
            "acceptance": ["测试通过"],
            "milestones": ["实现", "测试"],
            "healthCheckpoint": "首次测试后",
        }

    def manifest_for(self, *workers: dict[str, object]) -> dict[str, object]:
        return {
            "protocolVersion": 2,
            "runId": self.run_id,
            "runLanguage": "zh-CN",
            "controllerThreadId": self.controller,
            "projectId": "project-1",
            "maxActiveWorkers": 8,
            "workers": list(workers),
        }

    def record(
        self,
        idempotency_key: str,
        event_type: str,
        payload: dict[str, object],
        *,
        worker_id: str | None = None,
        expected_returncode: int = 0,
    ) -> dict[str, object]:
        event: dict[str, object] = {
            "idempotencyKey": idempotency_key,
            "type": event_type,
            "payload": payload,
        }
        if worker_id is not None:
            event["workerId"] = worker_id
        path = self.write_json(f"{idempotency_key.replace(':', '-')}.json", event)
        return self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(path),
            expected_returncode=expected_returncode,
        )

    def plan_worker(self) -> None:
        self.record(
            "plan-W1",
            "TASK_PLANNED",
            {
                "task": {
                    "taskId": "task-W1",
                    "workerId": "W1",
                    "priority": 10,
                    "objective": "实现独立模块",
                    "dependencies": [],
                    "environment": {"type": "worktree"},
                    "writeBoundary": ["src/module-a/**"],
                    "milestones": ["完成实现", "通过验收"],
                }
            },
        )

    def record_intent(
        self,
        kind: str,
        request_id: str,
        request: dict[str, object],
        *,
        worker_id: str | None = None,
    ) -> dict[str, object]:
        request_file = self.write_json(f"{request_id}.json", request)
        arguments = [
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            request_id,
            "--kind",
            kind,
            "--request-file",
            str(request_file),
        ]
        if worker_id is not None:
            arguments.extend(["--worker-id", worker_id])
        return self.invoke(*arguments)

    def record_outcome(
        self,
        operation_id: str,
        status: str,
        response: dict[str, object],
    ) -> dict[str, object]:
        response_file = self.write_json(f"{operation_id}-response.json", response)
        return self.invoke(
            "outcome",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--operation-id",
            operation_id,
            "--status",
            status,
            "--response-file",
            str(response_file),
        )

    def test_init_is_idempotent_and_status_is_bounded(self) -> None:
        created = self.initialize()
        reopened = self.initialize()
        self.assertEqual(created["code"], "LEDGER_CREATED")
        self.assertEqual(reopened["code"], "LEDGER_EXISTS")
        self.assertEqual(created["revision"], 1)
        status = self.invoke("status", "--db", str(self.database))
        self.assertEqual(status["run"]["runId"], self.run_id)
        self.assertEqual(status["run"]["activeCount"], 0)
        self.assertEqual(status["run"]["queuedCount"], 0)
        self.assertEqual(status["run"]["persistenceMode"], "LOCAL")

    def test_missing_database_is_never_created_by_runtime_commands(self) -> None:
        missing = self.root / "missing" / "ledger.sqlite3"
        event = self.write_json(
            "missing-ledger-event.json",
            {
                "idempotencyKey": "should-not-create",
                "type": "RUN_UPDATED",
                "payload": {"status": "WAITING"},
            },
        )
        rejected = self.invoke(
            "record",
            "--db",
            str(missing),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(event),
            expected_returncode=15,
        )
        self.assertEqual(rejected["code"], "SQLITE_ERROR")
        self.assertFalse(missing.exists())

    def test_dispatch_message_and_terminal_lifecycle(self) -> None:
        self.initialize()
        self.plan_worker()

        create = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {
                "environment": "worktree",
                "promptHash": "a" * 64,
                "title": "✍️ W1 独立模块",
            },
            worker_id="W1",
        )
        self.assertEqual(create["code"], "INTENT_RECORDED")
        create_again = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {
                "environment": "worktree",
                "promptHash": "a" * 64,
                "title": "✍️ W1 独立模块",
            },
            worker_id="W1",
        )
        self.assertTrue(create_again["duplicate"])
        self.assertEqual(create_again["operationId"], create["operationId"])

        self.record_outcome(
            str(create["operationId"]),
            "SUCCEEDED",
            {"threadId": "thread-W1", "hostId": "local-host"},
        )
        send = self.record_intent(
            "SEND_MESSAGE",
            "checkpoint-W1-1",
            {"command": "CHECKPOINT", "reason": "周期性健康检查"},
            worker_id="W1",
        )
        self.assertEqual(send["controllerSeq"], 1)
        send_again = self.record_intent(
            "SEND_MESSAGE",
            "checkpoint-W1-1",
            {"command": "CHECKPOINT", "reason": "周期性健康检查"},
            worker_id="W1",
        )
        self.assertEqual(send_again["controllerSeq"], 1)
        self.record_outcome(
            str(send["operationId"]),
            "SUCCEEDED",
            {"summary": "消息已发送"},
        )

        self.record(
            "worker-W1-progress-1",
            "WORKER_MESSAGE_APPLIED",
            {
                "seq": 1,
                "messageType": "PROGRESS",
                "summary": "核心逻辑已完成",
                "milestone": "实现",
                "completed": ["核心逻辑"],
                "remaining": ["测试"],
                "estimate": "1 个检查点",
                "appliedControllerSeq": 1,
                "usefulProgress": True,
            },
            worker_id="W1",
        )
        self.record(
            "worker-W1-done-2",
            "WORKER_MESSAGE_APPLIED",
            {
                "seq": 2,
                "messageType": "DONE",
                "summary": "全部验收项通过",
                "milestone": "验收",
                "completed": ["实现", "测试"],
                "remaining": [],
                "estimate": "完成",
                "details": ["实现位于独立 worktree"],
                "next": ["等待主控验收"],
                "needs": ["none"],
                "evidence": ["python3 -m unittest: passed"],
                "appliedControllerSeq": 1,
                "usefulProgress": True,
            },
            worker_id="W1",
        )
        self.record(
            "worker-W1-accepted",
            "WORKER_STATE_CHANGED",
            {
                "state": "ACCEPTED",
                "archiveReady": True,
                "terminalReason": "结果已验收并纳入主线",
            },
            worker_id="W1",
        )

        verified = self.invoke("verify", "--db", str(self.database))
        self.assertTrue(verified["valid"])
        status = self.invoke("status", "--db", str(self.database))
        self.assertEqual(status["run"]["activeCount"], 0)
        self.assertEqual(status["recentTerminalWorkers"][0]["state"], "ACCEPTED")
        self.assertTrue(status["recentTerminalWorkers"][0]["archiveReady"])
        self.assertEqual(
            status["recentTerminalWorkers"][0]["evidence"],
            ["python3 -m unittest: passed"],
        )
        self.assertEqual(
            status["recentTerminalWorkers"][0]["needs"],
            ["none"],
        )
        self.assertEqual(status["pendingOperations"], [])

    def test_owner_idempotency_and_terminal_guards(self) -> None:
        self.initialize()
        self.plan_worker()
        duplicate = self.record(
            "plan-W1",
            "TASK_PLANNED",
            {
                "task": {
                    "taskId": "task-W1",
                    "workerId": "W1",
                    "priority": 10,
                    "objective": "实现独立模块",
                    "dependencies": [],
                    "environment": {"type": "worktree"},
                    "writeBoundary": ["src/module-a/**"],
                    "milestones": ["完成实现", "通过验收"],
                }
            },
        )
        self.assertTrue(duplicate["duplicate"])
        wrong_worker_key = self.write_json(
            "same-event-key-different-worker.json",
            {
                "idempotencyKey": "plan-W1",
                "type": "TASK_PLANNED",
                "workerId": "W2",
                "payload": {
                    "task": {
                        "taskId": "task-W1",
                        "workerId": "W1",
                        "priority": 10,
                        "objective": "实现独立模块",
                        "dependencies": [],
                        "environment": {"type": "worktree"},
                        "writeBoundary": ["src/module-a/**"],
                        "milestones": ["完成实现", "通过验收"],
                    }
                },
            },
        )
        rejected_worker_reuse = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(wrong_worker_key),
            expected_returncode=10,
        )
        self.assertEqual(
            rejected_worker_reuse["code"],
            "INVALID_FIELD",
        )

        conflicting = self.write_json(
            "conflict.json",
            {
                "idempotencyKey": "plan-W1",
                "type": "TASK_PLANNED",
                "payload": {
                    "task": {
                        "taskId": "task-W1",
                        "workerId": "W1",
                        "objective": "不同目标",
                        "dependencies": [],
                        "environment": "worktree",
                        "writeBoundary": [],
                    }
                },
            },
        )
        conflict = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(conflicting),
            expected_returncode=12,
        )
        self.assertEqual(conflict["code"], "IDEMPOTENCY_CONFLICT")

        wrong_owner = self.write_json(
            "wrong-owner.json",
            {
                "idempotencyKey": "run-update",
                "type": "RUN_UPDATED",
                "payload": {"status": "WAITING"},
            },
        )
        rejected = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            "another-controller",
            "--event-file",
            str(wrong_owner),
            expected_returncode=11,
        )
        self.assertEqual(rejected["code"], "OWNER_CONFLICT")

        create = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {"promptHash": "b" * 64},
            worker_id="W1",
        )
        self.record_outcome(
            str(create["operationId"]),
            "SUCCEEDED",
            {"threadId": "thread-W1"},
        )
        invalid_terminal = self.record(
            "invalid-terminal",
            "WORKER_STATE_CHANGED",
            {
                "state": "RETIRED",
                "archiveReady": False,
                "terminalReason": "被替代",
            },
            worker_id="W1",
            expected_returncode=13,
        )
        self.assertEqual(invalid_terminal["code"], "ARCHIVE_GATE_FAILED")

    def test_explicit_takeover_increments_epoch_and_revokes_old_writer(self) -> None:
        self.initialize()
        taken = self.invoke(
            "takeover",
            "--db",
            str(self.database),
            "--expected-controller-thread-id",
            self.controller,
            "--new-controller-thread-id",
            "controller-thread-2",
            "--authorization-note",
            "用户明确要求由新主控接管",
        )
        self.assertEqual(taken["controllerEpoch"], 2)
        status = self.invoke("status", "--db", str(self.database))
        self.assertEqual(
            status["run"]["controllerThreadId"],
            "controller-thread-2",
        )
        event = self.write_json(
            "old-owner-after-takeover.json",
            {
                "idempotencyKey": "old-owner-update",
                "type": "RUN_UPDATED",
                "payload": {"status": "WAITING"},
            },
        )
        old_owner = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(event),
            expected_returncode=11,
        )
        self.assertEqual(old_owner["code"], "OWNER_CONFLICT")
        new_owner = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            "controller-thread-2",
            "--event-file",
            str(event),
        )
        self.assertEqual(new_owner["code"], "EVENT_APPLIED")

    def test_secret_like_input_is_rejected(self) -> None:
        self.initialize()
        event = self.write_json(
            "secret.json",
            {
                "idempotencyKey": "decision-secret",
                "type": "DECISION_RECORDED",
                "payload": {
                    "decisionId": "D1",
                    "source": "USER",
                    "summary": "不得保存密钥",
                    "apiToken": "do-not-store-this",
                },
            },
        )
        rejected = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(event),
            expected_returncode=10,
        )
        self.assertEqual(rejected["code"], "SENSITIVE_FIELD")
        raw_event = self.write_json(
            "raw-event-field.json",
            {
                "idempotencyKey": "decision-raw-output",
                "type": "DECISION_RECORDED",
                "payload": {
                    "decisionId": "D2",
                    "source": "CONTROLLER",
                    "summary": "只保存结论",
                    "rawToolOutput": "不得写入事件",
                },
            },
        )
        rejected_raw = self.invoke(
            "record",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--event-file",
            str(raw_event),
            expected_returncode=10,
        )
        self.assertEqual(rejected_raw["code"], "INVALID_FIELD")

    def test_operation_inputs_are_minimized_and_normalized(self) -> None:
        self.initialize()
        self.plan_worker()
        full_prompt = self.write_json(
            "full-prompt-intent.json",
            {
                "prompt": "完整 Worker Prompt 不应进入账本",
                "promptHash": "a" * 64,
            },
        )
        rejected_prompt = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "create-with-full-prompt",
            "--kind",
            "CREATE_THREAD",
            "--worker-id",
            "W1",
            "--request-file",
            str(full_prompt),
            expected_returncode=10,
        )
        self.assertEqual(rejected_prompt["code"], "SENSITIVE_FIELD")

        raw_output = self.write_json(
            "raw-output-intent.json",
            {
                "command": "CHECKPOINT",
                "rawToolOutput": "不应保存",
            },
        )
        rejected_raw = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "message-with-raw-output",
            "--kind",
            "SEND_MESSAGE",
            "--worker-id",
            "W1",
            "--request-file",
            str(raw_output),
            expected_returncode=10,
        )
        self.assertEqual(rejected_raw["code"], "INVALID_FIELD")

        title = self.record_intent(
            "SET_TITLE",
            "controller-title-normalized",
            {
                "target": "controller",
                "title": "  👑 [run-test] 拆解｜验证账本  ",
            },
        )
        self.assertEqual(title["code"], "INTENT_RECORDED")
        pending = self.invoke("pending", "--db", str(self.database))
        self.assertEqual(
            pending["operations"][0]["request"]["title"],
            "👑 [run-test] 拆解｜验证账本",
        )
        second_title_file = self.write_json(
            "second-controller-title.json",
            {
                "target": "controller",
                "title": "👑 [run-test] 跟进｜验证账本",
            },
        )
        blocked_second_title = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "controller-title-before-reconcile",
            "--kind",
            "SET_TITLE",
            "--request-file",
            str(second_title_file),
            expected_returncode=13,
        )
        self.assertEqual(
            blocked_second_title["code"],
            "PENDING_OPERATION_EXISTS",
        )

    def test_compiled_manifest_is_durable_and_exportable(self) -> None:
        self.initialize()
        manifest = {
            "protocolVersion": 2,
            "runId": self.run_id,
            "runLanguage": "zh-CN",
            "controllerThreadId": self.controller,
            "projectId": "project-1",
            "maxActiveWorkers": 8,
            "workers": [
                {
                    "taskId": "task-W1",
                    "workerId": "W1",
                    "titleAction": "实现模块",
                    "objective": "实现模块",
                    "context": "接口已冻结",
                    "inScope": ["src/module"],
                    "outOfScope": ["发布"],
                    "dependencies": [],
                    "environment": {"type": "worktree"},
                    "writesFiles": True,
                    "writeBoundary": ["src/module/**"],
                    "integrationPlan": "主控验收后 Handoff",
                    "deliverables": ["实现"],
                    "acceptance": ["测试通过"],
                    "milestones": ["实现", "测试"],
                    "healthCheckpoint": "首次测试后",
                }
            ],
        }
        compiled_path, compiled = self.compile_manifest(manifest, "manifest")
        activated = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(compiled_path),
        )
        self.assertEqual(activated["plannedWorkerCount"], 1)
        activated_again = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(compiled_path),
        )
        self.assertTrue(activated_again["duplicate"])
        status = self.invoke("status", "--db", str(self.database))
        self.assertEqual(
            status["run"]["currentManifestHash"],
            compiled["manifestHash"],
        )
        self.assertEqual(status["run"]["queuedCount"], 1)
        exported_path = self.root / "recovered-manifest.json"
        exported = self.invoke(
            "manifest",
            "--db",
            str(self.database),
            "--output",
            str(exported_path),
        )
        self.assertEqual(exported["manifestHash"], compiled["manifestHash"])
        self.assertEqual(
            json.loads(exported_path.read_text(encoding="utf-8")),
            compiled,
        )

    def test_manifest_cannot_silently_omit_nonterminal_tasks(self) -> None:
        self.initialize()
        first_path, _ = self.compile_manifest(
            self.manifest_for(
                self.worker_spec("W1"),
                self.worker_spec("W2"),
            ),
            "manifest-two-workers",
        )
        self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(first_path),
        )
        reduced_path, reduced = self.compile_manifest(
            self.manifest_for(self.worker_spec("W1")),
            "manifest-one-worker",
        )
        rejected = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(reduced_path),
            expected_returncode=13,
        )
        self.assertEqual(rejected["code"], "MANIFEST_OMITS_ACTIVE_TASK")
        self.assertEqual(rejected["details"]["tasks"][0]["workerId"], "W2")

        self.record(
            "cancel-W2-before-manifest-removal",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W2",
                "status": "CANCELLED",
                "reason": "用户授权范围内不再需要该尚未创建的任务",
            },
        )
        activated = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(reduced_path),
        )
        self.assertEqual(activated["manifestHash"], reduced["manifestHash"])

    def test_task_replan_must_precede_changed_manifest_activation(self) -> None:
        self.initialize()
        manifest = {
            "protocolVersion": 2,
            "runId": self.run_id,
            "runLanguage": "zh-CN",
            "controllerThreadId": self.controller,
            "projectId": "project-1",
            "maxActiveWorkers": 8,
            "workers": [
                {
                    "taskId": "task-W1",
                    "workerId": "W1",
                    "titleAction": "实现模块",
                    "objective": "实现模块",
                    "context": "第一版上下文",
                    "inScope": ["src/module"],
                    "outOfScope": ["发布"],
                    "dependencies": [],
                    "environment": {"type": "worktree"},
                    "writesFiles": True,
                    "writeBoundary": ["src/module/**"],
                    "integrationPlan": "主控验收后 Handoff",
                    "deliverables": ["实现"],
                    "acceptance": ["测试通过"],
                    "milestones": ["实现", "测试"],
                    "healthCheckpoint": "首次测试后",
                }
            ],
        }
        v1_path, v1 = self.compile_manifest(manifest, "manifest-v1")
        self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(v1_path),
        )
        manifest["workers"][0]["context"] = "第二版上下文"
        v2_path, v2 = self.compile_manifest(manifest, "manifest-v2")
        rejected = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(v2_path),
            expected_returncode=1,
        )
        self.assertEqual(rejected["code"], "TASK_CONFLICT")
        self.record(
            "replan-W1-v2",
            "TASK_REPLANNED",
            {
                "task": v2["workers"][0],
                "previousSpecHash": self.invoke(
                    "status",
                    "--db",
                    str(self.database),
                )["queuedTasks"][0]["specHash"],
                "reason": "补充不改变范围的上下文",
                "scopeChangeAuthorized": False,
            },
            worker_id="W1",
        )
        activated = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(v2_path),
        )
        self.assertEqual(activated["plannedWorkerCount"], 0)
        self.assertEqual(
            self.invoke("status", "--db", str(self.database))["run"][
                "currentManifestHash"
            ],
            v2["manifestHash"],
        )
        manifest["workers"][0]["writeBoundary"] = ["src/module-v3/**"]
        v3_path, v3 = self.compile_manifest(manifest, "manifest-v3")
        current_spec_hash = self.invoke(
            "status",
            "--db",
            str(self.database),
        )["queuedTasks"][0]["specHash"]
        unapproved_scope = self.record(
            "replan-W1-v3-without-user",
            "TASK_REPLANNED",
            {
                "task": v3["workers"][0],
                "previousSpecHash": current_spec_hash,
                "reason": "改变写边界",
                "scopeChangeAuthorized": False,
            },
            worker_id="W1",
            expected_returncode=13,
        )
        self.assertEqual(unapproved_scope["code"], "USER_DECISION_REQUIRED")
        self.record(
            "decision-W1-v3-boundary",
            "DECISION_RECORDED",
            {
                "decisionId": "D-W1-v3",
                "source": "USER",
                "summary": "用户明确同意调整 W1 写边界",
                "scope": ["task-W1.writeBoundary"],
                "workerIds": ["W1"],
            },
        )
        self.record(
            "replan-W1-v3-authorized",
            "TASK_REPLANNED",
            {
                "task": v3["workers"][0],
                "previousSpecHash": current_spec_hash,
                "reason": "按用户决定改变写边界",
                "scopeChangeAuthorized": True,
                "userDecisionId": "D-W1-v3",
            },
            worker_id="W1",
        )
        activated_v3 = self.invoke(
            "activate-manifest",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--manifest-file",
            str(v3_path),
        )
        self.assertEqual(activated_v3["manifestHash"], v3["manifestHash"])

    def test_create_intent_enforces_active_limit_dependency_and_boundaries(self) -> None:
        self.initialize("--max-active-workers", "1")
        self.plan_worker()
        self.record(
            "plan-W2",
            "TASK_PLANNED",
            {
                "task": {
                    "taskId": "task-W2",
                    "workerId": "W2",
                    "priority": 20,
                    "objective": "并行修改重叠模块",
                    "dependencies": [],
                    "environment": {"type": "worktree"},
                    "writeBoundary": ["src/module-a/child/**"],
                    "milestones": ["实现", "测试"],
                }
            },
            worker_id="W2",
        )
        create_w1 = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {"promptHash": "c" * 64},
            worker_id="W1",
        )
        limit = self.write_json("create-W2-limit.json", {"promptHash": "d" * 64})
        rejected_limit = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "create-W2-limit",
            "--kind",
            "CREATE_THREAD",
            "--worker-id",
            "W2",
            "--request-file",
            str(limit),
            expected_returncode=13,
        )
        self.assertEqual(rejected_limit["code"], "ACTIVE_LIMIT_REACHED")

        self.record(
            "increase-limit",
            "RUN_UPDATED",
            {"maxActiveWorkers": 2},
        )
        rejected_boundary = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "create-W2-boundary",
            "--kind",
            "CREATE_THREAD",
            "--worker-id",
            "W2",
            "--request-file",
            str(limit),
            expected_returncode=13,
        )
        self.assertEqual(rejected_boundary["code"], "WRITE_BOUNDARY_CONFLICT")

        self.record_outcome(
            str(create_w1["operationId"]),
            "SUCCEEDED",
            {"threadId": "thread-W1"},
        )
        self.record(
            "worker-W1-done",
            "WORKER_MESSAGE_APPLIED",
            {
                "seq": 1,
                "messageType": "DONE",
                "summary": "W1 完成",
                "milestone": "测试",
                "completed": ["实现", "测试"],
                "remaining": [],
                "estimate": "完成",
                "usefulProgress": True,
            },
            worker_id="W1",
        )
        self.record(
            "worker-W1-accepted",
            "WORKER_STATE_CHANGED",
            {
                "state": "ACCEPTED",
                "archiveReady": True,
                "terminalReason": "验收通过",
            },
            worker_id="W1",
        )
        create_w2 = self.record_intent(
            "CREATE_THREAD",
            "create-W2-after-W1",
            {"promptHash": "d" * 64},
            worker_id="W2",
        )
        self.assertEqual(create_w2["code"], "INTENT_RECORDED")

        self.record(
            "plan-W3",
            "TASK_PLANNED",
            {
                "task": {
                    "taskId": "task-W3",
                    "workerId": "W3",
                    "priority": 30,
                    "objective": "依赖未完成的 W2",
                    "dependencies": ["W2"],
                    "environment": {"type": "worktree"},
                    "writeBoundary": ["src/other/**"],
                    "milestones": ["实现", "测试"],
                }
            },
            worker_id="W3",
        )
        dependency_request = self.write_json(
            "create-W3.json",
            {"promptHash": "e" * 64},
        )
        rejected_dependency = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "create-W3",
            "--kind",
            "CREATE_THREAD",
            "--worker-id",
            "W3",
            "--request-file",
            str(dependency_request),
            expected_returncode=13,
        )
        self.assertEqual(
            rejected_dependency["code"],
            "DEPENDENCIES_NOT_ACCEPTED",
        )

    def test_message_sequence_title_and_handoff_guards(self) -> None:
        self.initialize()
        self.plan_worker()
        create = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {"promptHash": "f" * 64},
            worker_id="W1",
        )
        self.record_outcome(
            str(create["operationId"]),
            "SUCCEEDED",
            {"threadId": "thread-W1"},
        )
        unsent = self.record(
            "worker-W1-unsent-seq",
            "WORKER_MESSAGE_APPLIED",
            {
                "seq": 1,
                "messageType": "PROGRESS",
                "summary": "错误确认了未发送序号",
                "milestone": "实现",
                "completed": ["none"],
                "remaining": ["测试"],
                "estimate": "未知",
                "appliedControllerSeq": 1,
            },
            worker_id="W1",
            expected_returncode=13,
        )
        self.assertEqual(unsent["code"], "UNSENT_CONTROLLER_SEQUENCE")

        title = self.record_intent(
            "SET_TITLE",
            "title-W1-running",
            {"target": "worker", "title": "✍️ [run-test-W1] 实现独立模块"},
            worker_id="W1",
        )
        self.record_outcome(
            str(title["operationId"]),
            "SUCCEEDED",
            {"summary": "标题已更新"},
        )
        handoff_request = self.write_json(
            "handoff-running.json",
            {"targetBranch": "dev"},
        )
        rejected_handoff = self.invoke(
            "intent",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--request-id",
            "handoff-running",
            "--kind",
            "HANDOFF",
            "--worker-id",
            "W1",
            "--request-file",
            str(handoff_request),
            expected_returncode=13,
        )
        self.assertEqual(rejected_handoff["code"], "WORKER_NOT_READY")
        self.record(
            "worker-W1-done",
            "WORKER_MESSAGE_APPLIED",
            {
                "seq": 1,
                "messageType": "DONE",
                "summary": "全部完成",
                "milestone": "验收",
                "completed": ["实现", "测试"],
                "remaining": [],
                "estimate": "完成",
                "usefulProgress": True,
            },
            worker_id="W1",
        )
        handoff = self.record_intent(
            "HANDOFF",
            "handoff-review",
            {"targetBranch": "dev"},
            worker_id="W1",
        )
        self.assertEqual(handoff["code"], "INTENT_RECORDED")

    def test_cycle_completion_and_append_only_sql_guards(self) -> None:
        self.initialize()
        with sqlite3.connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE events SET event_type = 'CORRUPTED' WHERE revision = 1"
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM events WHERE revision = 1")
            connection.rollback()

        self.record(
            "cycle-1-complete",
            "CYCLE_COMPLETED",
            {
                "cycleNumber": 1,
                "resultSummary": "空队列演练完成",
                "validation": ["ledger verify"],
            },
        )
        self.record(
            "run-complete",
            "RUN_COMPLETED",
            {"summary": "运行完成"},
        )
        with sqlite3.connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE run_state SET status = 'ACTIVE' WHERE singleton = 1"
                )
            connection.rollback()
        rejected_reopen = self.record(
            "reopen-complete-run",
            "RUN_UPDATED",
            {"status": "ACTIVE"},
            expected_returncode=13,
        )
        self.assertEqual(rejected_reopen["code"], "RUN_COMPLETE")
        self.assertTrue(self.invoke("verify", "--db", str(self.database))["valid"])

    def test_verify_detects_hashed_state_tampering(self) -> None:
        self.initialize()
        self.plan_worker()
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE planned_tasks SET spec_json = '{}' WHERE task_id = 'task-W1'"
            )
            connection.commit()
        rejected = self.invoke(
            "verify",
            "--db",
            str(self.database),
            expected_returncode=15,
        )
        self.assertEqual(rejected["code"], "LEDGER_INVALID")
        error_codes = {
            item["code"] for item in rejected["details"]["errors"]
        }
        self.assertIn("TASK_HASH_MISMATCH", error_codes)

    def test_cycle_cannot_complete_with_unfinished_work(self) -> None:
        self.initialize()
        self.plan_worker()
        rejected = self.record(
            "cycle-1-too-early",
            "CYCLE_COMPLETED",
            {
                "cycleNumber": 1,
                "resultSummary": "不应完成",
                "validation": [],
            },
            expected_returncode=13,
        )
        self.assertEqual(rejected["code"], "CYCLE_NOT_COMPLETE")
        self.assertEqual(rejected["details"]["unfinishedTaskCount"], 1)

    def test_recovery_registration_rejects_terminal_task_and_invalid_health(self) -> None:
        self.initialize()
        self.plan_worker()
        self.record(
            "cancel-W1",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W1",
                "status": "CANCELLED",
                "reason": "恢复核对确认不再创建",
            },
        )
        terminal_registration = self.record(
            "register-cancelled-W1",
            "WORKER_REGISTERED",
            {
                "taskId": "task-W1",
                "threadId": "thread-W1",
                "state": "RUNNING",
            },
            worker_id="W1",
            expected_returncode=13,
        )
        self.assertEqual(terminal_registration["code"], "TASK_TERMINAL")

        self.record(
            "requeue-W1",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W1",
                "status": "QUEUED",
                "reason": "用户确认恢复已有任务",
            },
        )
        invalid_health = self.record(
            "register-stalled-running-W1",
            "WORKER_REGISTERED",
            {
                "taskId": "task-W1",
                "threadId": "thread-W1",
                "state": "RUNNING",
                "health": "STALLED",
            },
            worker_id="W1",
            expected_returncode=13,
        )
        self.assertEqual(invalid_health["code"], "HEALTH_STATE_CONFLICT")
        registered = self.record(
            "register-blocked-W1",
            "WORKER_REGISTERED",
            {
                "taskId": "task-W1",
                "threadId": "thread-W1",
                "state": "BLOCKED",
                "health": "STALLED",
                "closedAcceptanceItems": [],
                "remainingAcceptanceItems": ["确认恢复基线"],
            },
            worker_id="W1",
        )
        self.assertEqual(registered["code"], "EVENT_APPLIED")

    def test_outcome_rejects_unsanitized_tool_fields_without_consuming_intent(self) -> None:
        self.initialize()
        self.plan_worker()
        create = self.record_intent(
            "CREATE_THREAD",
            "create-W1",
            {"promptHash": "e" * 64},
            worker_id="W1",
        )
        raw_response = self.write_json(
            "create-W1-raw-response.json",
            {
                "threadId": "thread-W1",
                "rawToolOutput": "unbounded output",
            },
        )
        rejected = self.invoke(
            "outcome",
            "--db",
            str(self.database),
            "--controller-thread-id",
            self.controller,
            "--operation-id",
            str(create["operationId"]),
            "--status",
            "SUCCEEDED",
            "--response-file",
            str(raw_response),
            expected_returncode=10,
        )
        self.assertEqual(rejected["code"], "INVALID_FIELD")
        pending = self.invoke("pending", "--db", str(self.database))
        self.assertEqual(len(pending["operations"]), 1)
        self.record_outcome(
            str(create["operationId"]),
            "SUCCEEDED",
            {"threadId": "thread-W1", "summary": "任务已创建"},
        )
        self.assertEqual(
            self.invoke("pending", "--db", str(self.database))["operations"],
            [],
        )

    def test_create_retry_requires_confirmed_failure_and_worker_aware_cancellation(self) -> None:
        self.initialize()
        self.plan_worker()
        create = self.record_intent(
            "CREATE_THREAD",
            "create-W1-first-attempt",
            {"promptHash": "9" * 64},
            worker_id="W1",
        )
        premature_retry = self.record(
            "requeue-W1-before-create-outcome",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W1",
                "status": "QUEUED",
                "reason": "不应在未知结果时重试",
            },
            expected_returncode=1,
        )
        self.assertEqual(premature_retry["code"], "CREATE_NOT_FAILED")
        self.record_outcome(
            str(create["operationId"]),
            "FAILED",
            {"summary": "create_thread 已确认失败"},
        )
        requeued = self.record(
            "requeue-W1-after-create-failure",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W1",
                "status": "QUEUED",
                "reason": "确认失败后允许使用新 request ID 重试",
            },
        )
        self.assertEqual(requeued["code"], "EVENT_APPLIED")
        invalid_cancel = self.record(
            "cancel-W1-with-placeholder-worker",
            "TASK_STATUS_CHANGED",
            {
                "taskId": "task-W1",
                "status": "CANCELLED",
                "reason": "已有 Worker 时必须走终态门",
            },
            expected_returncode=1,
        )
        self.assertEqual(invalid_cancel["code"], "WORKER_ALREADY_CREATED")
        retired = self.record(
            "retire-W1-after-create-failure",
            "WORKER_STATE_CHANGED",
            {
                "state": "RETIRED",
                "archiveReady": True,
                "terminalReason": "创建失败且确认不再重试",
            },
            worker_id="W1",
        )
        self.assertEqual(retired["code"], "EVENT_APPLIED")

    def test_backup_and_restore_are_verified_and_non_destructive(self) -> None:
        self.initialize()
        backup = self.invoke("backup", "--db", str(self.database))
        backup_path = Path(str(backup["outputPath"]))
        self.assertTrue(backup_path.is_file())
        restored_path = self.root / "restored" / "ledger.sqlite3"
        restored = self.invoke(
            "restore",
            "--source",
            str(backup_path),
            "--target",
            str(restored_path),
        )
        self.assertFalse(restored["promoted"])
        verified = self.invoke("verify", "--db", str(restored_path))
        self.assertTrue(verified["valid"])
        refused = self.invoke(
            "restore",
            "--source",
            str(backup_path),
            "--target",
            str(restored_path),
            expected_returncode=1,
        )
        self.assertEqual(refused["code"], "OUTPUT_EXISTS")

    def test_git_project_requires_local_exclude_rule(self) -> None:
        project = self.root / "project"
        project.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(project)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        rejected = self.invoke(
            "init",
            "--project-root",
            str(project),
            "--run-id",
            self.run_id,
            "--run-language",
            "en",
            "--controller-thread-id",
            self.controller,
            "--goal-summary",
            "Validate local exclusion",
            expected_returncode=14,
        )
        self.assertEqual(rejected["code"], "LOCAL_EXCLUDE_REQUIRED")
        created = self.invoke(
            "init",
            "--project-root",
            str(project),
            "--run-id",
            self.run_id,
            "--run-language",
            "en",
            "--controller-thread-id",
            self.controller,
            "--goal-summary",
            "Validate local exclusion",
            "--prepare-local-exclude",
        )
        self.assertEqual(created["code"], "LEDGER_CREATED")
        exclude = Path(str(created["git"]["excludePath"])).read_text(encoding="utf-8")
        self.assertIn("/.codex/runtime/orchestrate-codex-tasks/", exclude)

    def test_project_ledger_rejects_symlink_escape(self) -> None:
        project = self.root / "project"
        outside = self.root / "outside"
        project.mkdir()
        outside.mkdir()
        (project / ".codex").symlink_to(outside, target_is_directory=True)
        subprocess.run(
            ["git", "init", "-q", str(project)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        rejected = self.invoke(
            "init",
            "--project-root",
            str(project),
            "--run-id",
            self.run_id,
            "--run-language",
            "en",
            "--controller-thread-id",
            self.controller,
            "--goal-summary",
            "Reject a state path that escapes the project",
            "--prepare-local-exclude",
            expected_returncode=14,
        )
        self.assertEqual(rejected["code"], "LEDGER_PATH_OUTSIDE_PROJECT")
        self.assertFalse(
            (
                outside
                / "runtime"
                / "orchestrate-codex-tasks"
                / "runs"
                / self.run_id
                / "ledger.sqlite3"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()
