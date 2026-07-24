# 调度清单与渲染脚本

## 目录

1. 责任边界
2. 清单结构
3. 校验和编译
4. 依赖与就绪选择
5. Worker Prompt 与 create request
6. 主控命令
7. 生命周期标题
8. 写边界与重规划

## 1. 责任边界

`scripts/dispatch.py` 是确定性调度助手。它负责：

- 校验协议版本、语言、ID 和最大活跃数；
- 校验 Worker DAG、2–5 个里程碑和必需验收字段；
- 强制写入型 Worker 默认使用独立 worktree；
- 检测可能同时运行的 Worker 写边界冲突；
- 根据账本快照选择依赖满足、槽位和可选资源容量可用的 Worker；
- 渲染匹配 `runLanguage` 与协调档位的 Worker Prompt、标题和结构化消息；
- 输出可传给高层 Codex 工具的参数对象。

它不负责：

- 调用 `create_thread`、`send_message_to_thread`、`set_thread_title` 或 Handoff；
- 写 SQLite；
- 读取或修改 Worker worktree；
- 决定用户未授权的范围变化；
- 提交、推送、发布、归档或删除。

脚本 stdout 是候选请求，不是外部操作成功证据。真实调用必须遵循 [ledger.md](ledger.md) 的 intent/outcome 顺序。

## 2. 清单结构

清单必须是 JSON，`protocolVersion` 固定为 `2`：

```json
{
  "protocolVersion": 2,
  "runId": "R7K2",
  "runLanguage": "zh-CN",
  "controllerThreadId": "<CONTROLLER_THREAD_ID>",
  "controllerHostId": "<OMIT_WHEN_NOT_NEEDED>",
  "projectId": "<REAL_ID_FROM_LIST_PROJECTS>",
  "maxActiveWorkers": 8,
  "resourceCapacities": {
    "simulator": 1
  },
  "workers": [
    {
      "taskId": "core-api",
      "workerId": "W1",
      "priority": 10,
      "titleAction": "实现核心 API",
      "objective": "实现并验证冻结规格中的核心 API",
      "context": "接口规格位于 docs/api.md，基线已经通过现有测试",
      "inScope": [
        "实现 src/api",
        "增加对应单元测试"
      ],
      "outOfScope": [
        "发布",
        "修改数据库 schema"
      ],
      "dependencies": [],
      "workerHost": "local",
      "environment": {
        "type": "worktree"
      },
      "writesFiles": true,
      "coordinationProfile": "standard",
      "resourceClaims": {
        "simulator": 1
      },
      "sharedLocalWriteAuthorized": false,
      "writeBoundary": [
        "src/api/**",
        "tests/api/**"
      ],
      "integrationPlan": "主控验收后串行 Handoff 到 Local",
      "deliverables": [
        "实现",
        "测试证据"
      ],
      "acceptance": [
        "目标测试通过",
        "写入不越界"
      ],
      "milestones": [
        "完成接口实现",
        "完成测试",
        "提交 DONE 证据"
      ],
      "healthCheckpoint": "首次测试后；已知长命令预计不超过 3 分钟"
    }
  ],
  "boundaryOverlapAllowances": []
}
```

字段规则：

| 字段 | 规则 |
|---|---|
| `projectId` | 只能使用 `list_projects` 返回的真实值；projectless Worker 不继承它 |
| `priority` | 越小越先考虑，但依赖、槽位和边界优先 |
| `dependencies` | 引用同一清单中的 `workerId`，必须无环 |
| `environment.type` | `local`、`worktree` 或 `projectless` |
| `startingState` | 只允许 worktree 使用，且必须来自用户明确要求 |
| `writesFiles` | 写任何项目文件时为 `true` |
| `writeBoundary` | 相对路径，不允许绝对路径或 `..` |
| `sharedLocalWriteAuthorized` | 仅用户明确接受共享 Local 写入风险时为 `true` |
| `coordinationProfile` | 可选 `lean/standard/strict`；省略时只读推断 `lean`，写入推断 `standard` |
| `resourceCapacities` | 可选运行级正整数容量表；仅在确有稀缺资源时声明 |
| `resourceClaims` | 可选 Worker 正整数需求表，名称必须已在运行级容量中声明 |
| `milestones` | 2–5 个可观察里程碑 |
| `healthCheckpoint` | 首个复查点和已知长命令墙钟时间 |

projectless Worker 的环境可以包含：

```json
{
  "type": "projectless",
  "directoryName": "bounded-research"
}
```

worktree 起始状态只有两种：

```json
{"type": "working-tree"}
```

```json
{"type": "branch", "branchName": "<EXISTING_BRANCH_OR_REF>"}
```

默认省略 `startingState`。

`lean` 只能用于 `writesFiles=false`。推断出的默认档位不会写回规范化 manifest，因此旧 manifest 的内容与 `manifestHash` 保持兼容；显式字段才进入编译结果。

## 3. 校验和编译

只校验：

```text
python3 <SKILL_DIR>/scripts/dispatch.py validate-manifest \
  --manifest-file <DRAFT_JSON>
```

规范化并保存：

```text
python3 <SKILL_DIR>/scripts/dispatch.py compile-manifest \
  --manifest-file <DRAFT_JSON> \
  --output <RUN_DIRECTORY>/manifest.current.json
```

编译输出包含：

- 规范化 Worker 字段；
- 稳定拓扑顺序；
- 顺序依赖造成的合法写边界重叠；
- `manifestHash`。

manifest、Worker、environment、starting state 和 overlap allowance 都使用封闭字段集合；未知字段会失败，避免拼写错误或原始日志被悄悄丢弃/传播。编译默认拒绝覆盖。修改计划时写新文件或显式使用 `--overwrite`，再通过账本记录 replan 和新 manifest。不要手工编辑编译输出；任何修改都回到 draft 后重新编译。

`plan-events` 可以只读预览清单对应的 `TASK_PLANNED` 事件：

```text
python3 <SKILL_DIR>/scripts/dispatch.py plan-events \
  --manifest-file <COMPILED_OR_DRAFT_JSON>
```

正式落账优先使用 `ledger.py activate-manifest`，它会原子保存清单和 task。

## 4. 依赖与就绪选择

先从账本读取状态并保存为私有临时 JSON，再运行：

```text
python3 <SKILL_DIR>/scripts/dispatch.py ready \
  --manifest-file <COMPILED_MANIFEST_JSON> \
  --status-file <LEDGER_STATUS_JSON>
```

`readyWorkers` 只包含：

- ledger run 为 `ACTIVE`，且 `runLanguage/currentManifestHash` 与输入 manifest 一致；
- 尚未活跃、验收或废弃；
- 所有依赖已经 `ACCEPTED`；
- 没有 pending `CREATE_THREAD` intent；
- 当前活跃数未超过 manifest 和账本两者较小的上限；
- 可选 `resourceClaims` 不超过本批原子预留后的 `resourceCapacities`；
- 写边界不与活跃或本批已选 Worker 冲突。

`notReady` 给出机器可读原因，资源不足使用 `RESOURCE_CAPACITY_EXCEEDED:<name>`。`PROVISIONING/RUNNING` 占用资源，`BLOCKED/REVIEW` 释放计算资源但仍计入活跃槽位并保留写边界。存在 manifest 之外的活跃 Worker 或 pending create 时，脚本保守阻止新派发，等待 Controller 先执行 audit/recovery。主控不得因为槽位空闲绕过这些原因，也不得为了凑满并发制造 Worker。

每次创建成功、Worker 终态、依赖变化、用户调整并发或 manifest 重规划后重新计算。脚本只选择候选；Controller 仍逐个执行 intent、create、outcome 和改名。

## 5. Worker Prompt 与 create request

对每个 `readyWorker`：

```text
python3 <SKILL_DIR>/scripts/dispatch.py render-worker \
  --manifest-file <COMPILED_MANIFEST_JSON> \
  --worker-id <WORKER_ID>
```

输出包括：

| 字段 | 用途 |
|---|---|
| `prompt` | 按语言与协调档位渲染的 Worker Prompt |
| `coordinationProfile` | 实际采用的 `lean/standard/strict` 档位 |
| `resourceClaims` | 本 Worker 的可选资源需求 |
| `promptHash` | 账本核对 |
| `title` | Worker 获得真实 `threadId` 后设置的 `✍️` 标题 |
| `createThread` | 实际 `create_thread` 参数候选 |
| `ledgerIntentRequest` | 写 `CREATE_THREAD` intent 的最小化请求 |

顺序：

1. 用 `ledgerIntentRequest` 写 `CREATE_THREAD` intent；
2. 用 `createThread` 调用实际 `create_thread`；
3. 把稳定 ID 写 outcome；
4. 真实 `threadId` 可用后，对标题重复 intent/tool/outcome；
5. `clientThreadId` 不能用于等待、改名或跨任务消息。

渲染器会：

- 只选择匹配 `runLanguage` 的模板；
- 默认让只读 Worker 使用 compact `lean`、写入 Worker 使用 compact `standard`；只有显式 `strict` 使用原完整模板；
- 填入 `protocolVersion=2`、Controller 地址、范围、边界和验收；
- 同主机不需要 `hostId` 时删除整个字段；
- 把 Worker 消息示例的动态 `seq/type` 写为 `<SEQ>/<TYPE>`；
- 拒绝未解析占位符和明显敏感内容。

## 6. 主控命令

先把最小语义写入 `SEND_MESSAGE` intent：

```json
{
  "command": "CHECKPOINT",
  "reason": "<runLanguage reason>"
}
```

从 intent stdout 取得 `controllerSeq` 后，准备渲染输入：

```json
{
  "protocolVersion": 2,
  "runId": "R7K2",
  "runLanguage": "zh-CN",
  "workerId": "W1",
  "threadId": "<WORKER_THREAD_ID>",
  "hostId": "<OMIT_WHEN_NOT_NEEDED>",
  "controllerSeq": 4,
  "command": "CHECKPOINT",
  "decision": "none",
  "instructions": [
    "到下一个安全边界停止启动新阶段",
    "报告已完成和剩余验收项"
  ],
  "acceptanceDelta": [
    "none"
  ]
}
```

渲染：

```text
python3 <SKILL_DIR>/scripts/dispatch.py render-command \
  --input-file <COMMAND_JSON>
```

使用输出的 `sendMessage` 调用实际消息工具，再写对应 outcome。序号来自账本，不从聊天上下文推断。

`DECISION/REVISION` 只有在 outcome 为 `SUCCEEDED` 时才自动增加 `decisionRoundTrips`；失败、未知和重复 outcome 不增加。累计 3 次后，新的微放行会被拒绝，`CHECKPOINT` 仍可使用；后续 `REPLAN` 必须同时在 intent 和渲染输入中携带：

```json
{
  "executionPlan": {
    "steps": [
      "运行目标测试",
      "根据首个失败修复",
      "复测并整理证据"
    ],
    "stopOnFirstNonzero": true,
    "stopOnTimeout": true,
    "maxWallTimeMinutes": 30
  }
}
```

有界 `REPLAN` 成功后自动清零往返计数。未达到阈值时不强制新字段，以兼容已有运行。

## 7. 生命周期标题

标题输入示例：

```json
{
  "scope": "worker",
  "runId": "R7K2",
  "runLanguage": "zh-CN",
  "workerId": "W1",
  "state": "BLOCKED",
  "action": "实现核心 API",
  "blocker": "等待接口决策"
}
```

```text
python3 <SKILL_DIR>/scripts/dispatch.py render-title \
  --input-file <TITLE_JSON>
```

Controller 支持 `PLANNING/TRACKING/REPLANNING/WAITING_FOR_USER/SYNTHESIZING/COMPLETE`。Worker 支持 `PROVISIONING/RUNNING/REVIEW/BLOCKED/ACCEPTED/RETIRED`。

`ACCEPTED` 和 `RETIRED` 标题输入必须有 `archiveReady=true`；`RETIRED` 还必须有 `terminalReason`。脚本只渲染标题，不能代替账本终态门禁，也不会归档任务。

## 8. 写边界与重规划

默认规则：

- 任意写入型 Worker 使用独立 worktree；
- 可能同时活跃的 Worker 不得有前缀重叠的写边界；
- 有依赖顺序的 Worker 可以在前置 Worker 已 `ACCEPTED` 后使用重叠边界；
- 同一脏 worktree 的唯一成果未持久化前，不创建第二写入者接管相同边界。

极少数确需同时重叠时，清单必须显式写：

```json
{
  "boundaryOverlapAllowances": [
    {
      "workers": ["W1", "W2"],
      "reason": "<why overlap is unavoidable>",
      "coordinationPlan": "<how simultaneous writes remain safe>"
    }
  ]
}
```

这只表达用户授权范围内的工程协调，不扩大文件、权限或外部影响。主控仍应优先重新拆边界或串行执行。

重规划时：

1. 先对受影响 Worker 执行 `CHECKPOINT`；
2. 保护唯一成果；
3. 写 `TASK_REPLANNED` 和必要的用户决定；
4. 重新编译并激活 manifest；
5. 重算 ready 集合；
6. 对现有 Worker 用 `REPLAN` 或用户授权后的 `SCOPE_UPDATE` 同步；
7. 不用新 manifest 静默改变已经派发的 Prompt。
