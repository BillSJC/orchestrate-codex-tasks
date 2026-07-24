from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DISPATCH = (
    REPOSITORY_ROOT
    / ".agents"
    / "skills"
    / "orchestrate-codex-tasks"
    / "scripts"
    / "dispatch.py"
)


class DispatchCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(
        self,
        *arguments: str,
        expected_returncode: int = 0,
    ) -> dict[str, object]:
        process = subprocess.run(
            [sys.executable, str(DISPATCH), *arguments],
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

    def test_render_title_forces_utf8_output_when_host_stdio_is_gbk(self) -> None:
        title_file = self.write_json(
            "gbk-title.json",
            {
                "scope": "worker",
                "runLanguage": "zh-CN",
                "runId": "run-demo",
                "workerId": "W1",
                "action": "处理历史任务",
                "state": "RUNNING",
            },
        )

        process = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "render-title",
                "--input-file",
                str(title_file),
            ],
            cwd=REPOSITORY_ROOT,
            env={**os.environ, "PYTHONIOENCODING": "gbk:strict"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(
            process.returncode,
            0,
            msg=f"stderr: {process.stderr!r}\nstdout: {process.stdout!r}",
        )
        rendered = json.loads(process.stdout.decode("utf-8").splitlines()[-1])
        self.assertTrue(str(rendered["title"]).startswith("✍️"))

    def manifest(
        self,
        *,
        language: str = "zh-CN",
        controller_host_id: str | None = None,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "protocolVersion": 2,
            "runId": "run-demo",
            "runLanguage": language,
            "controllerThreadId": "controller-thread",
            "projectId": "project-from-list-projects",
            "maxActiveWorkers": 2,
            "workers": [
                {
                    "taskId": "task-W1",
                    "workerId": "W1",
                    "priority": 10,
                    "titleAction": "实现模块 A" if language == "zh-CN" else "Implement module A",
                    "objective": "实现模块 A" if language == "zh-CN" else "Implement module A",
                    "context": "接口已经冻结" if language == "zh-CN" else "The interface is frozen",
                    "inScope": ["src/a", "对应测试"],
                    "outOfScope": ["发布", "修改模块 B"],
                    "dependencies": [],
                    "workerHost": "local",
                    "environment": {"type": "worktree"},
                    "writesFiles": True,
                    "writeBoundary": ["src/a/**", "tests/a/**"],
                    "integrationPlan": "主控验收后执行 Handoff",
                    "deliverables": ["实现", "测试证据"],
                    "acceptance": ["单元测试通过", "边界内无额外改动"],
                    "milestones": ["完成实现", "完成测试"],
                    "healthCheckpoint": "首次测试后；长命令预计少于 2 分钟",
                },
                {
                    "taskId": "task-W2",
                    "workerId": "W2",
                    "priority": 30,
                    "titleAction": "审阅文档" if language == "zh-CN" else "Review documentation",
                    "objective": "审阅文档" if language == "zh-CN" else "Review documentation",
                    "context": "仅输出报告" if language == "zh-CN" else "Report only",
                    "inScope": ["公开文档"],
                    "outOfScope": ["修改项目文件"],
                    "dependencies": [],
                    "workerHost": "local",
                    "environment": {
                        "type": "projectless",
                        "directoryName": "documentation-review",
                    },
                    "writesFiles": False,
                    "writeBoundary": [],
                    "integrationPlan": "主控读取最终报告",
                    "deliverables": ["审阅报告"],
                    "acceptance": ["结论附证据"],
                    "milestones": ["收集资料", "形成结论"],
                    "healthCheckpoint": "完成资料清单后",
                },
                {
                    "taskId": "task-W3",
                    "workerId": "W3",
                    "priority": 20,
                    "titleAction": "扩展模块 A" if language == "zh-CN" else "Extend module A",
                    "objective": "在 W1 后扩展模块 A",
                    "context": "必须基于 W1 已验收成果",
                    "inScope": ["src/a 扩展"],
                    "outOfScope": ["并行修改 W1 的脏 worktree"],
                    "dependencies": ["W1"],
                    "workerHost": "local",
                    "environment": {"type": "worktree"},
                    "writesFiles": True,
                    "writeBoundary": ["src/a/extensions/**"],
                    "integrationPlan": "W1 合入后再创建 worktree，完成后 Handoff",
                    "deliverables": ["扩展实现"],
                    "acceptance": ["组合测试通过"],
                    "milestones": ["读取 W1 基线", "实现扩展", "组合验证"],
                    "healthCheckpoint": "完成基线读取后",
                },
            ],
        }
        if controller_host_id is not None:
            value["controllerHostId"] = controller_host_id
        return value

    def test_manifest_validation_and_plan_events(self) -> None:
        manifest_file = self.write_json("manifest.json", self.manifest())
        validated = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(manifest_file),
        )
        self.assertEqual(validated["topologicalOrder"], ["W1", "W2", "W3"])
        self.assertEqual(validated["workerCount"], 3)
        self.assertEqual(len(validated["sequentialBoundaryOverlaps"]), 1)

        planned = self.invoke(
            "plan-events",
            "--manifest-file",
            str(manifest_file),
        )
        self.assertEqual(len(planned["events"]), 3)
        self.assertEqual(planned["events"][0]["type"], "TASK_PLANNED")
        self.assertEqual(
            planned["events"][0]["idempotencyKey"],
            "run-demo:task:task-W1:planned:v2",
        )
        compiled_path = self.root / "compiled.json"
        compiled = self.invoke(
            "compile-manifest",
            "--manifest-file",
            str(manifest_file),
            "--output",
            str(compiled_path),
        )
        self.assertEqual(compiled["manifestHash"], validated["manifestHash"])
        saved = json.loads(compiled_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["manifestHash"], validated["manifestHash"])
        revalidated = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(compiled_path),
        )
        self.assertEqual(revalidated["manifestHash"], validated["manifestHash"])

    def test_ready_respects_dependencies_slots_and_existing_state(self) -> None:
        manifest_file = self.write_json("manifest.json", self.manifest())
        manifest_hash = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(manifest_file),
        )["manifestHash"]
        empty_status = self.write_json(
            "empty-status.json",
            {
                "run": {
                    "runId": "run-demo",
                    "protocolVersion": 2,
                    "runLanguage": "zh-CN",
                    "currentManifestHash": manifest_hash,
                    "status": "ACTIVE",
                    "maxActiveWorkers": 8,
                },
                "activeWorkers": [],
                "recentTerminalWorkers": [],
                "pendingOperations": [],
                "taskStates": {
                    "W1": "QUEUED",
                    "W2": "QUEUED",
                    "W3": "QUEUED",
                },
            },
        )
        ready = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(empty_status),
        )
        self.assertEqual(
            [worker["workerId"] for worker in ready["readyWorkers"]],
            ["W1", "W2"],
        )
        self.assertIn(
            "DEPENDENCIES_NOT_ACCEPTED:W1",
            ready["notReady"][0]["reasons"],
        )

        accepted_status = self.write_json(
            "accepted-status.json",
            {
                "run": {
                    "runId": "run-demo",
                    "protocolVersion": 2,
                    "runLanguage": "zh-CN",
                    "currentManifestHash": manifest_hash,
                    "status": "ACTIVE",
                    "maxActiveWorkers": 8,
                },
                "activeWorkers": [],
                "recentTerminalWorkers": [
                    {"workerId": "W1", "state": "ACCEPTED"}
                ],
                "pendingOperations": [],
                "taskStates": {
                    "W1": "ACCEPTED",
                    "W2": "QUEUED",
                    "W3": "QUEUED",
                },
            },
        )
        after_acceptance = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(accepted_status),
        )
        self.assertEqual(
            [worker["workerId"] for worker in after_acceptance["readyWorkers"]],
            ["W3", "W2"],
        )
        cancelled_status = self.write_json(
            "cancelled-status.json",
            {
                "run": {
                    "runId": "run-demo",
                    "protocolVersion": 2,
                    "runLanguage": "zh-CN",
                    "currentManifestHash": manifest_hash,
                    "status": "ACTIVE",
                    "maxActiveWorkers": 8,
                },
                "activeWorkers": [],
                "recentTerminalWorkers": [],
                "pendingOperations": [],
                "taskStates": {
                    "W1": "CANCELLED",
                    "W2": "QUEUED",
                    "W3": "QUEUED",
                },
            },
        )
        after_cancellation = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(cancelled_status),
        )
        by_worker = {
            item["workerId"]: item["reasons"]
            for item in after_cancellation["notReady"]
        }
        self.assertIn("TASK_NOT_QUEUED:CANCELLED", by_worker["W1"])

    def test_render_worker_is_localized_complete_and_host_aware(self) -> None:
        chinese_file = self.write_json("zh.json", self.manifest())
        chinese = self.invoke(
            "render-worker",
            "--manifest-file",
            str(chinese_file),
            "--worker-id",
            "W1",
        )
        self.assertTrue(str(chinese["title"]).startswith("✍️"))
        self.assertIn("你是一个独立 Codex Worker 任务", chinese["prompt"])
        self.assertIn("- protocolVersion: 2", chinese["prompt"])
        self.assertIn("主控是账本唯一写者", chinese["prompt"])
        self.assertNotIn("controllerHostId:", chinese["prompt"])
        self.assertNotIn('"hostId":', chinese["prompt"])
        self.assertNotIn("{{", chinese["prompt"])
        self.assertEqual(
            chinese["createThread"]["target"]["environment"]["type"],
            "worktree",
        )

        english_file = self.write_json(
            "en.json",
            self.manifest(language="en", controller_host_id="remote-host"),
        )
        english = self.invoke(
            "render-worker",
            "--manifest-file",
            str(english_file),
            "--worker-id",
            "W1",
        )
        self.assertIn("You are an independent Codex Worker task", english["prompt"])
        self.assertIn("- protocolVersion: 2", english["prompt"])
        self.assertIn("Controller is the sole ledger writer", english["prompt"])
        self.assertIn("- controllerHostId: remote-host", english["prompt"])
        self.assertIn('"hostId": "remote-host"', english["prompt"])
        self.assertEqual(english["promptHash"], english["ledgerIntentRequest"]["promptHash"])

    def test_worker_profiles_are_adaptive_and_strict_is_opt_in(self) -> None:
        default_file = self.write_json("profiles-default.json", self.manifest())
        standard = self.invoke(
            "render-worker",
            "--manifest-file",
            str(default_file),
            "--worker-id",
            "W1",
        )
        lean = self.invoke(
            "render-worker",
            "--manifest-file",
            str(default_file),
            "--worker-id",
            "W2",
        )
        self.assertEqual(standard["coordinationProfile"], "standard")
        self.assertEqual(lean["coordinationProfile"], "lean")
        self.assertIn("只在独立 worktree", standard["prompt"])
        self.assertIn("本任务只读", lean["prompt"])

        strict_manifest = self.manifest()
        strict_manifest["workers"][0]["coordinationProfile"] = "strict"
        strict_file = self.write_json("profiles-strict.json", strict_manifest)
        strict = self.invoke(
            "render-worker",
            "--manifest-file",
            str(strict_file),
            "--worker-id",
            "W1",
        )
        self.assertEqual(strict["coordinationProfile"], "strict")
        self.assertGreater(len(strict["prompt"]), len(standard["prompt"]))
        self.assertNotIn("{{", strict["prompt"])

        invalid_manifest = self.manifest()
        invalid_manifest["workers"][0]["coordinationProfile"] = "lean"
        invalid_file = self.write_json("profiles-invalid.json", invalid_manifest)
        invalid = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(invalid_file),
            expected_returncode=10,
        )
        self.assertEqual(invalid["code"], "INVALID_FIELD")

    def test_ready_respects_optional_resource_capacity_and_releases_blocked_claims(
        self,
    ) -> None:
        manifest = self.manifest()
        manifest["maxActiveWorkers"] = 3
        manifest["resourceCapacities"] = {"browser": 1}
        manifest["workers"][0]["resourceClaims"] = {"browser": 1}
        manifest["workers"][1]["resourceClaims"] = {"browser": 1}
        manifest_file = self.write_json("resource-manifest.json", manifest)
        manifest_hash = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(manifest_file),
        )["manifestHash"]

        def status(name: str, state: str | None) -> Path:
            active = [] if state is None else [{"workerId": "W1", "state": state}]
            return self.write_json(
                name,
                {
                    "run": {
                        "runId": "run-demo",
                        "protocolVersion": 2,
                        "runLanguage": "zh-CN",
                        "currentManifestHash": manifest_hash,
                        "status": "ACTIVE",
                        "maxActiveWorkers": 3,
                    },
                    "activeWorkers": active,
                    "recentTerminalWorkers": [],
                    "pendingOperations": [],
                    "taskStates": {
                        "W1": "QUEUED" if state is None else "DISPATCHED",
                        "W2": "QUEUED",
                        "W3": "QUEUED",
                    },
                },
            )

        empty = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(status("resource-empty.json", None)),
        )
        self.assertEqual(
            [worker["workerId"] for worker in empty["readyWorkers"]],
            ["W1"],
        )
        self.assertIn(
            "RESOURCE_CAPACITY_EXCEEDED:browser",
            {
                item["workerId"]: item["reasons"]
                for item in empty["notReady"]
            }["W2"],
        )

        running = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(status("resource-running.json", "RUNNING")),
        )
        self.assertEqual(running["resourceUsage"]["browser"]["holders"], ["W1"])
        self.assertEqual(running["readyWorkers"], [])

        blocked = self.invoke(
            "ready",
            "--manifest-file",
            str(manifest_file),
            "--status-file",
            str(status("resource-blocked.json", "BLOCKED")),
        )
        self.assertEqual(
            [worker["workerId"] for worker in blocked["readyWorkers"]],
            ["W2"],
        )
        self.assertEqual(blocked["resourceUsage"]["browser"]["holders"], ["W2"])

    def test_concurrent_boundary_conflict_and_local_write_are_rejected(self) -> None:
        conflict = self.manifest()
        conflict["workers"][2]["dependencies"] = []
        conflict_file = self.write_json("conflict.json", conflict)
        rejected = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(conflict_file),
            expected_returncode=13,
        )
        self.assertEqual(rejected["code"], "WRITE_BOUNDARY_CONFLICT")
        conflict["boundaryOverlapAllowances"] = [
            {
                "workers": ["W1", "W3"],
                "reason": "测试显式且有界的重叠授权",
                "coordinationPlan": "两个 Worker 在独立 worktree 中工作，由主控串行回收",
            }
        ]
        overlap_file = self.write_json("authorized-overlap.json", conflict)
        accepted_overlap = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(overlap_file),
        )
        self.assertTrue(accepted_overlap["ok"])

        local_write = self.manifest()
        local_write["workers"][0]["environment"] = {"type": "local"}
        local_file = self.write_json("local-write.json", local_write)
        rejected_local = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(local_file),
            expected_returncode=13,
        )
        self.assertEqual(rejected_local["code"], "WORKTREE_REQUIRED")
        local_write["workers"][0]["sharedLocalWriteAuthorized"] = True
        authorized_file = self.write_json("authorized-local.json", local_write)
        accepted = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(authorized_file),
        )
        self.assertTrue(accepted["ok"])

    def test_dependency_cycle_and_protocol_mismatch_are_rejected(self) -> None:
        cyclic = self.manifest()
        cyclic["workers"][0]["dependencies"] = ["W3"]
        cycle_file = self.write_json("cycle.json", cyclic)
        cycle = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(cycle_file),
            expected_returncode=12,
        )
        self.assertEqual(cycle["code"], "DEPENDENCY_CYCLE")

        wrong_protocol = self.manifest()
        wrong_protocol["protocolVersion"] = 1
        protocol_file = self.write_json("protocol.json", wrong_protocol)
        mismatch = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(protocol_file),
            expected_returncode=11,
        )
        self.assertEqual(mismatch["code"], "PROTOCOL_MISMATCH")

        unknown_field = self.manifest()
        unknown_field["workers"][0]["rawToolOutput"] = "must not be retained"
        unknown_file = self.write_json("unknown-worker-field.json", unknown_field)
        rejected_unknown = self.invoke(
            "validate-manifest",
            "--manifest-file",
            str(unknown_file),
            expected_returncode=10,
        )
        self.assertEqual(rejected_unknown["code"], "INVALID_FIELD")

    def test_render_command_and_titles_enforce_sequence_and_archive_gate(self) -> None:
        command_file = self.write_json(
            "command.json",
            {
                "protocolVersion": 2,
                "runId": "run-demo",
                "runLanguage": "zh-CN",
                "workerId": "W1",
                "threadId": "thread-W1",
                "controllerSeq": 4,
                "command": "CHECKPOINT",
                "decision": "none",
                "instructions": ["到下一个安全边界停止启动新阶段", "报告剩余验收项"],
                "acceptanceDelta": ["none"],
            },
        )
        command = self.invoke(
            "render-command",
            "--input-file",
            str(command_file),
        )
        self.assertIn("controllerSeq=004 command=CHECKPOINT", command["prompt"])
        self.assertNotIn("hostId", command["sendMessage"])
        replan_file = self.write_json(
            "bounded-replan-command.json",
            {
                "protocolVersion": 2,
                "runId": "run-demo",
                "runLanguage": "zh-CN",
                "workerId": "W1",
                "threadId": "thread-W1",
                "controllerSeq": 5,
                "command": "REPLAN",
                "instructions": ["按有界批次执行"],
                "executionPlan": {
                    "steps": ["运行测试", "根据结果修复", "复测"],
                    "stopOnFirstNonzero": True,
                    "stopOnTimeout": True,
                    "maxWallTimeMinutes": 20,
                },
            },
        )
        replan = self.invoke(
            "render-command",
            "--input-file",
            str(replan_file),
        )
        self.assertIn("executionPlan:", replan["prompt"])
        self.assertIn("maxWallTimeMinutes=20", replan["prompt"])

        controller_title_file = self.write_json(
            "controller-title.json",
            {
                "scope": "controller",
                "runId": "run-demo",
                "runLanguage": "zh-CN",
                "state": "TRACKING",
                "goal": "交付版本",
                "activeCount": 2,
            },
        )
        controller_title = self.invoke(
            "render-title",
            "--input-file",
            str(controller_title_file),
        )
        self.assertEqual(
            controller_title["title"],
            "👑 [run-demo] 跟进 2 个 Worker｜交付版本",
        )

        accepted_title = {
            "scope": "worker",
            "runId": "run-demo",
            "runLanguage": "en",
            "workerId": "W1",
            "state": "ACCEPTED",
            "action": "Implement module A",
            "archiveReady": False,
        }
        rejected_file = self.write_json("rejected-title.json", accepted_title)
        rejected = self.invoke(
            "render-title",
            "--input-file",
            str(rejected_file),
            expected_returncode=13,
        )
        self.assertEqual(rejected["code"], "ARCHIVE_GATE_FAILED")
        accepted_title["archiveReady"] = True
        accepted_file = self.write_json("accepted-title.json", accepted_title)
        accepted = self.invoke(
            "render-title",
            "--input-file",
            str(accepted_file),
        )
        self.assertTrue(str(accepted["title"]).startswith("✅"))

        invalid_efficiency_file = self.write_json(
            "invalid-efficiency-title.json",
            {
                "scope": "worker",
                "runId": "run-demo",
                "runLanguage": "en",
                "workerId": "W1",
                "state": "RUNNING",
                "action": "Implement module A",
                "efficiencyReview": "false",
            },
        )
        invalid_efficiency = self.invoke(
            "render-title",
            "--input-file",
            str(invalid_efficiency_file),
            expected_returncode=10,
        )
        self.assertEqual(invalid_efficiency["code"], "INVALID_FIELD")


if __name__ == "__main__":
    unittest.main()
