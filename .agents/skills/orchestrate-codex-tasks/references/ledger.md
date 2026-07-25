# 本地持久化运行账本

## 目录

1. 角色与边界
2. 存储位置与 Git 隔离
3. 初始化
4. 编译并激活调度清单
5. 单写者与事件模型
6. 外部副作用的 intent/outcome
7. 状态、消息与健康记录
8. 恢复、核对与接管
9. 备份、恢复和导出
10. SQLite schema
11. 隐私与故障边界

## 1. 角色与边界

本地 SQLite 账本是一次编排运行的逻辑事实源，用来抵抗主控上下文压缩、任务消息遗漏和进程重启。标题只是用户可见投影，不能替代账本。

- 只有当前 Controller 可以写账本。
- Worker 不读取、写入、复制或删除账本；Worker 只用协议消息报告状态。
- `ledger.py` 是唯一写入入口。不要用 `sqlite3`、Python 临时代码或 SQL 客户端直接修改数据库。
- `dispatch.py` 只校验、选择和渲染请求，不调用 Codex 任务工具，也不写 SQLite。
- `create_thread`、`set_thread_title`、`send_message_to_thread` 和 `handoff_thread` 等真实外部操作仍由 Controller 使用当前运行时高层工具执行。
- 账本不授权归档、提交、推送、发布、删除、扩大权限或绕过审批。

协议版本为 `2`，初始 ledger schema 版本为 `1`。版本不匹配时停止新派发，不要猜测兼容性。

## 2. 存储位置与 Git 隔离

项目运行默认位置：

```text
<PROJECT_ROOT>/.codex/runtime/orchestrate-codex-tasks/runs/<runId>/ledger.sqlite3
```

要求：

1. 使用稳定、可写、非临时目录。不要把唯一账本放在 `/tmp`、系统临时目录或 Worker worktree。
2. 数据库目录权限尽力设为 `0700`，数据库和导出文件尽力设为 `0600`。
3. 项目属于 Git 仓库时，`init --prepare-local-exclude` 把 `/.codex/runtime/orchestrate-codex-tasks/` 加入本仓库的 `.git/info/exclude`。
4. 不为账本修改受版本控制的 `.gitignore`。
5. project run 的状态目录解析后必须仍在 `project-root` 内；拒绝通过 `.codex` 或父目录 symlink 逃逸到项目外。
6. 如果运行路径已经被 Git 跟踪、无法建立本地排除规则，或位于不稳定目录，停止初始派发并向用户报告。
7. 不把账本、备份、编译清单或导出快照提交、推送、上传或附到问题报告。

非 Git 项目可以显式使用稳定 `--state-root`。有项目根时仍优先传 `--project-root`，以便执行跟踪和忽略检查。

## 3. 初始化

在首次可避免的外部副作用之前初始化。`<SKILL_DIR>` 指本 Skill 根目录：

```text
python3 <SKILL_DIR>/scripts/ledger.py init \
  --project-root <PROJECT_ROOT> \
  --run-id <RUN_ID> \
  --run-language <en-or-zh-CN> \
  --controller-thread-id <CONTROLLER_THREAD_ID> \
  --controller-host-id <CONTROLLER_HOST_ID-if-needed> \
  --max-active-workers <N> \
  --goal-summary <RUN_LANGUAGE_SUMMARY> \
  --prepare-local-exclude
```

同主机不需要 `controllerHostId` 时省略整个参数。读取 JSON stdout 中的 `databasePath`，后续所有写入都使用这个确切路径。

重复相同初始化返回 `LEDGER_EXISTS`，不会重建数据库。`runId`、Controller 或语言不同则返回 `INIT_CONFLICT`。

如果运行时没有直接提供当前 Controller 的 `threadId`，允许一次受限的 bootstrap 例外：

1. 先设置包含唯一 `runId` 的 `👑` 标题；
2. 用任务列表唯一解析当前任务；
3. 立即初始化账本；
4. 用 `RUN_UPDATED` 记录已观察到的 Controller 标题。

除该寻址 bootstrap 外，初始化前不创建 Worker、不发跨任务消息、不启动 Handoff。

## 4. 编译并激活调度清单

先按 [dispatch.md](dispatch.md) 编写原始清单，再编译为规范化版本：

```text
python3 <SKILL_DIR>/scripts/dispatch.py compile-manifest \
  --manifest-file <DRAFT_MANIFEST_JSON> \
  --output <RUN_DIRECTORY>/manifest.current.json
```

激活编译清单：

```text
python3 <SKILL_DIR>/scripts/ledger.py activate-manifest \
  --db <DB> \
  --controller-thread-id <CONTROLLER_THREAD_ID> \
  --manifest-file <RUN_DIRECTORY>/manifest.current.json
```

激活在一个 SQLite 事务中完成：

- 要求 run 处于 `ACTIVE`；等待用户、降级或完成状态不得换清单；
- 校验规范化清单、`manifestHash`、协议、运行、语言和 Controller；
- 保存完整编译清单；
- 将其设为当前清单；
- 为首次出现的 Worker 写入幂等 `TASK_PLANNED` 事件；
- 重算排队数和监控派生字段。

已有 task 的规格不可被另一个 `TASK_PLANNED` 静默覆盖。原授权内重规划时使用 `TASK_REPLANNED`，携带：

```json
{
  "idempotencyKey": "<runId>:task:<taskId>:replan:<unique-id>",
  "type": "TASK_REPLANNED",
  "workerId": "W1",
  "payload": {
    "task": {},
    "previousSpecHash": "<current-spec-hash>",
    "reason": "<runLanguage reason>",
    "scopeChangeAuthorized": false
  }
}
```

若确有用户授权的范围变化，先用 `DECISION_RECORDED` 保存 `source=USER` 的决定，再设置 `scopeChangeAuthorized=true` 和对应 `userDecisionId`。不得用 replan 推断用户授权。

脚本会机械识别环境、project、写入方式、写边界和主要范围/交付/验收列表长度的结构变化；这些变化在没有 USER decision 时直接失败。文本含义是否扩大仍由 Controller 审查，机械检查不能替代授权判断。

新 manifest 也不能静默删除任何 `QUEUED/DISPATCHING/DISPATCHED` 或活跃 Worker task。先显式取消未创建 task，或按成果处置和终态门将 Worker 置为 `RETIRED`，再激活不含该 task 的 manifest。

## 5. 单写者与事件模型

所有通用逻辑更新使用：

```text
python3 <SKILL_DIR>/scripts/ledger.py record \
  --db <DB> \
  --controller-thread-id <CONTROLLER_THREAD_ID> \
  --event-file <EVENT_JSON>
```

每个事件必须有稳定且唯一的 `idempotencyKey`。重复相同 key 和相同内容返回 `EVENT_ALREADY_APPLIED`；同 key 不同内容返回 `IDEMPOTENCY_CONFLICT`。

event 顶层和每种 payload 都使用封闭字段集合；task spec 也只接受 manifest 定义的字段。Worker 状态类事件必须使用顶层 `workerId`，task 事件的顶层值若存在必须与 `task.workerId` 一致。未知字段会被拒绝而不是被悄悄保存到 append-only 审计轨迹。幂等身份同时绑定 event type、payload、Worker 和 operation 目标。

核心事件：

| 事件 | 用途 |
|---|---|
| `MANIFEST_ACTIVATED` | 由 `activate-manifest` 自动写入 |
| `TASK_PLANNED` | 由 `activate-manifest` 自动写入 |
| `TASK_REPLANNED` | 更新非终态 task 的规范 |
| `TASK_STATUS_CHANGED` | 显式排队、取消或重试状态 |
| `WORKER_REGISTERED` | 手工恢复时登记 Worker |
| `WORKER_RESOLVED` | `clientThreadId` 解析为真实 `threadId` |
| `WORKER_MESSAGE_APPLIED` | 应用新的 Worker 协议消息 |
| `WORKER_STATE_CHANGED` | 生命周期转换和终态门禁 |
| `WORKER_HEALTH_CHANGED` | `HEALTHY/AT_RISK/STALLED` |
| `CURSOR_UPDATED` | 保存等待工具 cursor |
| `RUN_UPDATED` | 语言、并发、标题或运行状态 |
| `DECISION_RECORDED` | 保存用户或 Controller 决定 |
| `INTEGRATION_UPDATED` | Handoff、组合验证和整合状态 |
| `CYCLE_STARTED/CYCLE_COMPLETED` | 迭代循环 |
| `RUN_COMPLETED` | 所有 task 终态后的运行完成 |
| `RECOVERY_RECONSTRUCTED` | 从任务事实重建新账本 |

事件表只追加；SQLite trigger 禁止 UPDATE 和 DELETE。其他 trigger 禁止 `ACCEPTED/RETIRED` Worker、对应 terminal task 和 `COMPLETE` run 回退。派生表由 `ledger.py` 在同一事务中更新。

`WORKER_REGISTERED` 只用于恢复已经观察到的非终态 task，必须至少携带 `threadId/clientThreadId` 之一；非 `PROVISIONING` 状态必须有真实 `threadId`。`STALLED` 只能与 `BLOCKED` 同时登记。

## 6. 外部副作用的 intent/outcome

对以下外部写操作统一执行：

```text
intent -> 高层 Codex 工具调用 -> outcome
```

适用种类：

- `CREATE_THREAD`
- `SEND_MESSAGE`
- `SET_TITLE`
- `HANDOFF`

### 6.1 写 intent

```text
python3 <SKILL_DIR>/scripts/ledger.py intent \
  --db <DB> \
  --controller-thread-id <CONTROLLER_THREAD_ID> \
  --request-id <STABLE_REQUEST_ID> \
  --kind <KIND> \
  --worker-id <WORKER_ID-if-needed> \
  --request-file <SANITIZED_REQUEST_JSON>
```

request 只保存重试和核对所需的最小语义，不保存完整 Prompt、凭据、原始工具输出或个人联系/支付信息。

允许的 request 字段是封闭集合：

- create：`environment`、`promptHash`、`title`、`targetHash`；
- message：`command`、`reason`、`decision`、`instructions`、`acceptanceDelta`、`stepContracts`；历史 pending `executionPlan` 仅用于恢复兼容；
- title：`title`、`target`；
- Handoff：`targetBranch`、`destinationHostId`。

未知字段会被拒绝，不能把原始工具参数对象直接当作账本 request。

`SEND_MESSAGE` intent 会原子分配 `controllerSeq`。必须先取得该序号，再用 `dispatch.py render-command` 渲染消息；不得自行猜测或复用序号。

`CREATE_THREAD` intent 会把 task 从 `QUEUED` 变为 `DISPATCHING` 并预留一个 `PROVISIONING` Worker，从而避免超额派发和重复创建。

同一 kind 和目标同时只允许一个 `INTENT/UNKNOWN` operation。已有 pending create/message/title/Handoff 未核对前，新 request ID 也不能绕过门禁。

若 `SET_TITLE` 的目标标题已经等于账本投影，返回 `TITLE_UNCHANGED/noChange=true`，不创建 operation。若 `CURSOR_UPDATED` 的值未变化，返回 `CURSOR_UNCHANGED/noChange=true`，不追加事件或 revision。

### 6.2 调用实际工具

使用 intent 对应的脚本渲染结果调用当前运行时高层工具。脚本 stdout 不是工具调用，也不能被记作成功。

### 6.3 写 outcome

```text
python3 <SKILL_DIR>/scripts/ledger.py outcome \
  --db <DB> \
  --controller-thread-id <CONTROLLER_THREAD_ID> \
  --operation-id <OPERATION_ID> \
  --status <SUCCEEDED-or-FAILED-or-UNKNOWN> \
  --response-file <SANITIZED_RESPONSE_JSON>
```

只保存稳定 ID、主机、摘要和必要状态：

- create：`threadId`、`clientThreadId`、`hostId`、简短 `summary`；
- title/message：简短 `summary`；
- Handoff：`operationId`、简短 `summary`。

outcome 同样使用封闭字段集合；不要直接保存原始工具返回。create 成功必须有 `threadId/clientThreadId`，Handoff 启动成功必须有 `operationId`。

工具调用超时、返回含糊或 Controller 在写 outcome 前中断时，不能直接重试。恢复后先查看 pending operations 并核对外部事实，再写 `SUCCEEDED`、`FAILED` 或 `UNKNOWN`。

只有 `CREATE_THREAD` 已确认 `FAILED` 时，才可用带非空 `reason` 的 `TASK_STATUS_CHANGED` 将 `DISPATCHING` task 恢复为 `QUEUED`，再使用新的 request ID 重试。`UNKNOWN` 不能重置排队状态。

## 7. 状态、消息与健康记录

处理 Worker 消息前：

1. 核对 `runId` 和 `workerId`；
2. 只接受大于账本 `lastSeq` 的序号；
3. 核对 `incidentClass`。Worker 把预期结果、可恢复控制错误或控制面降级误写为 `BLOCKED` 时，保留原始 `messageType=BLOCKED` 作为审计事实，但设置 `blockerDisposition=RECOVERABLE`；只有真实 `WORK_BLOCKER` 使用 `blockerDisposition=BLOCK`；
4. 将结构化字段转换为 `WORKER_MESSAGE_APPLIED`；
5. 事务成功后再更新标题、回复或继续调度。

`DONE` 只把 Worker 置为 `REVIEW`。`BLOCKED + blockerDisposition=RECOVERABLE` 保持或恢复 `RUNNING`，不会进入生命周期 `BLOCKED`；该兼容路径用于旧 Worker 的错误分类，新 Worker 应在本地预算内自行纠正并发送 `PROGRESS`。`ACCEPTED` 和 `RETIRED` 必须通过 `WORKER_STATE_CHANGED` 写入 `archiveReady=true` 和非空 `terminalReason`；账本不会归档任务。

每条 Worker event 还把当前 `details/next/needs/evidence` 的有界列表投影到 Worker 行；上下文恢复后不会只剩一个标题或一句 summary。健康事件保存有效进展、范围变化、timeout 和下次复查时间。成功发送 `DECISION/REVISION` 时账本自动增加决策往返，失败、未知或重复 outcome 不增加；累计 3 次自动进入 `AT_RISK` 并拒绝更多微放行，成功发送带 `stepContracts` 或历史兼容 `executionPlan` 的 `REPLAN` 后清零。`STALLED` 必须与 `BLOCKED` 生命周期一致。

旧 canonical manifest 若没有 `failurePolicy`，激活、恢复和 `verify` 均保持原 `manifestHash` 与 task spec hash；默认预算只在 Prompt 渲染时注入。新 manifest 则把该策略写入账本，便于审计。

允许的异常字段：

```json
{
  "incidentClass": "RECOVERABLE_CONTROL",
  "localCorrectionAttempts": 1,
  "blockerDisposition": "RECOVERABLE"
}
```

`blockerDisposition` 仅可用于 `messageType=BLOCKED`。省略时为向后兼容默认 `BLOCK`；因此 Controller 必须在应用旧 Worker 的 `BLOCKED` 前先分类。`BLOCK` 只接受 `incidentClass=WORK_BLOCKER`。

读取紧凑快照：

```text
python3 <SKILL_DIR>/scripts/ledger.py status --db <DB>
```

快照包含当前 manifest hash、活跃 Worker、全部终态的最小状态映射、最近终态详情、排队 task、pending operations、整合状态和健康审查候选。

cycle 只能按 `currentCycle + 1` 顺序启动。只有所有 Worker/task 已终态且没有 pending/unknown operation 时，当前 cycle 才能完成；`RUN_COMPLETED` 还要求没有活跃 cycle。完成后的 run 不能重新打开，但仍允许用独立 title intent 投影最终主控标题。

## 8. 恢复、核对与接管

任何上下文恢复或主控重新进入运行时，优先在一个只读事务中执行：

```text
python3 <SKILL_DIR>/scripts/ledger.py snapshot \
  --db <DB> \
  --terminal-limit 20
```

`snapshot` 同时返回 `verification`、边界化 `status` 和包含规范化 request 的详细 `pendingOperations`，避免三次读取跨越不同 revision。`verify/status/pending` 仍保留用于专项诊断。

需要重新生成当前清单文件时：

```text
python3 <SKILL_DIR>/scripts/ledger.py manifest \
  --db <DB> \
  --output <NEW_MANIFEST_PATH>
```

然后用 `list_threads`、`wait_threads timeoutMs:0` 和必要的紧凑 `read_thread` 收集观察事实，写成：

```json
{
  "controllerThreadId": "<observed-controller>",
  "workers": [
    {
      "workerId": "W1",
      "threadId": "<observed-thread>",
      "hostId": "<observed-host>",
      "title": "<observed-title>"
    }
  ]
}
```

只读核对：

```text
python3 <SKILL_DIR>/scripts/ledger.py audit \
  --db <DB> \
  --observed-file <OBSERVED_JSON>
```

`audit` 永不自动修改账本。先解释差异，再使用幂等事件或 outcome 修正。

只有用户明确授权更换 Controller 时才能执行：

```text
python3 <SKILL_DIR>/scripts/ledger.py takeover \
  --db <DB> \
  --expected-controller-thread-id <OLD_CONTROLLER> \
  --new-controller-thread-id <NEW_CONTROLLER> \
  --new-controller-host-id <HOST-if-needed> \
  --authorization-note <USER_AUTHORIZED_REASON>
```

接管增加 `controllerEpoch`，旧 Controller 之后的写入会被 `OWNER_CONFLICT` 拒绝。

账本丢失但任务仍存在时，不覆盖原路径：

1. 从标题和任务列表确定唯一 run；
2. 创建新的 `runId` 或明确的 recovered run ledger，并使用 `--reconstructed`；
3. 读取每个任务的真实状态和近期证据；
4. 从观察事实编译并激活保守 manifest，先重建 planned task；
5. 用 `WORKER_REGISTERED`、`WORKER_RESOLVED`、状态和 cursor 事件保守重建；
6. 写 `RECOVERY_RECONSTRUCTED`；
7. 完成 audit 后才恢复派发。

无法唯一匹配时保持阻塞，不创建重复 Worker。

## 9. 备份、恢复和导出

在每个 cycle 完成、重大重规划前或接管前创建一致备份：

```text
python3 <SKILL_DIR>/scripts/ledger.py backup --db <DB>
```

默认输出到运行目录的 `backups/`，不会覆盖已有文件，并在复制后重新验证。

恢复只复制到一个不存在的新文件：

```text
python3 <SKILL_DIR>/scripts/ledger.py restore \
  --source <BACKUP_DB> \
  --target <NEW_DB>
```

`restore` 不会自动替换当前账本，返回 `promoted=false`。核对后需要切换时必须由用户明确决定。

完整 JSON 导出：

```text
python3 <SKILL_DIR>/scripts/ledger.py export \
  --db <DB> \
  --output <LOCAL_PRIVATE_JSON>
```

导出仍是私有运行数据，不得自动上传。

## 10. SQLite schema

schema 文件位于 [../scripts/sql/001_initial.sql](../scripts/sql/001_initial.sql)。

| 表 | 内容 |
|---|---|
| `run_state` | Controller owner、语言、并发、计数、cycle、manifest、revision |
| `manifests` | 规范化调度清单 |
| `planned_tasks` | task spec、依赖、环境、写边界和队列状态 |
| `workers` | 地址、生命周期、健康、序号、cursor、验收和终态 |
| `operations` | 外部操作 intent/outcome 和幂等 request |
| `integrations` | Handoff 与组合验证 |
| `decisions` | 用户和 Controller 决定 |
| `cycles` | 持续迭代状态 |
| `events` | append-only 事件审计轨迹 |

`PRAGMA user_version` 必须等于脚本支持的 schema version。不要自行编辑 schema version 或手动迁移。

`verify` 除 SQLite integrity/foreign-key 检查外，还核对事件 revision 连续性、event/manifest/task/operation 内容哈希、派生计数、地址、健康度、消息序号与终态一致性。任何错误都先进入恢复流程，不继续派发。

## 11. 隐私与故障边界

脚本拒绝明显的密钥字段、私钥块、常见 API key 形态、超大 JSON 和超长字符串。该检测是最后防线，不代表可以把未知原始输出交给脚本。

永不写入：

- 密码、token、cookie、Authorization header、私钥或凭据；
- 原始模型推理、完整工具日志或完整 Worker Prompt；
- 个人直接联系方式、支付信息或无关用户数据；
- 未经最小化的环境变量和配置文件内容。

初始账本无法安全初始化时，不派发 Worker。运行中写入失败时：

1. 停止新的外部副作用和新 Worker 派发；
2. 不重复已有工具调用；
3. 保留当前安全边界和本轮观察事实；
4. 用 `runLanguage` 立即向用户报告 `DEGRADED`、受影响 operation 和恢复选项；
5. 修复或从已验证备份恢复并完成 `snapshot + audit` 后再继续。

不得把内存记忆冒充持久账本，也不得因账本故障自动归档、删除或重建 Worker。

标题、cursor、等待快照、renderer 输入、临时 JSON、命令引号和消息传输属于控制面。它们失败时不得写入 Worker 生命周期 `BLOCKED`；确认无外部副作用的控制错误在预算内纠正，任务服务异常按退避处理。持久化不可用时把运行标记为 `DEGRADED` 并停止新的外部副作用，Worker 的真实工作状态保持不变。
