# Controller 与 Worker 协作协议

## 目录

1. 运行身份
2. 运行语言
3. Worker Prompt 模板
4. Worker 到主控的消息
5. 主控到 Worker 的消息
6. 本地化标题和状态机
7. 运行账本
8. 并发调整
9. 运行中切换语言
10. 恢复与去重

## 1. 运行身份

每次运行生成：

- `runId`：短运行标识，例如 `R7K2`。
- `runLanguage`：`en` 或 `zh-CN`。
- `workerId`：稳定 Worker 标识，例如 `W1`。
- `controllerThreadId`：主控真实任务 ID。
- `controllerHostId`：跨主机时必填；同主机且工具不要求时可省略。

主控必须把自己的真实 `threadId` 和 `runLanguage` 直接写入每个 Worker Prompt。Worker 不猜测、不搜索主控地址，也不自行选择初始协调语言。

## 2. 运行语言

在首次用户可见汇报、任务改名或 Worker 创建前解析 `runLanguage`：

1. 用户明确指定协调或会话语言时，以该指令为准。
2. 否则判断触发本次编排的用户自然语言。
3. 忽略代码、命令、路径、ID、引用材料和交付物的目标语言。
4. 中文请求映射为 `zh-CN`，英文请求映射为 `en`。
5. 中英文混合且仍然含糊时，使用最后一个有实际语义的用户自然语言句子；不要只为语言选择阻塞运行。

`runLanguage` 控制：

- 主控对用户的回复、进度、阻塞和最终汇总；
- 主控与 Worker 标题中的人类可读文本；
- Worker Prompt 和双向消息正文；
- 决策、验收与残余风险说明。

以下内容不本地化：

- 协议枚举和命令；
- 结构化字段名；
- 工具名和参数名；
- `runId`、`workerId`、`threadId`、`hostId`；
- 路径、命令、代码和用户提供的专有名称。

交付物语言独立于 `runLanguage`。例如，中文请求要求生成英文 README 时，协调语言仍为中文，README 使用英文。

## 3. Worker Prompt 模板

按 `runLanguage` 只读取并使用一份模板：

- `en`：[worker-prompt.en.md](worker-prompt.en.md)
- `zh-CN`：[worker-prompt.zh-CN.md](worker-prompt.zh-CN.md)

派发规则：

1. 完整读取匹配语言的模板。
2. 填充所有占位符，并让任务目标、范围、边界和验收说明使用 `runLanguage`；路径、命令、代码和交付物要求保持原样。
3. 不临时翻译另一份模板，也不把两种模板混合进同一个 Worker Prompt。
4. 将填充后的模板正文作为 `create_thread` 初始 `prompt`。
5. 同一主机且 `hostId` 可省略时，删除整个 `controllerHostId` 行和消息调用中的整个 `hostId` 字段，不传空字符串。

## 4. Worker 到主控的消息

结构化字段保持英文，字段内容使用当前 `runLanguage`：

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} seq={{SEQ}} type={{TYPE}}]
summary: <one-line status in runLanguage>
details:
- <key fact or result in runLanguage>
next:
- <next action in runLanguage; use none for DONE>
needs:
- <Controller decision needed in runLanguage; use none when not needed>
evidence:
- <file, command, test, link, or other evidence>
```

`TYPE` 只能是：

- `ACCEPTED`
- `PROGRESS`
- `BLOCKED`
- `DONE`

`seq` 从 `001` 开始单调增加。每个消息只表达一个主要状态变化。

### 4.1 BLOCKED 最低内容

```text
summary: <blocker in runLanguage>
details:
- facts: <confirmed facts in runLanguage>
- cause: <why work cannot continue in runLanguage>
next:
- option-a: <option and impact in runLanguage>
- option-b: <option and impact in runLanguage>
needs:
- recommendation: <Worker recommendation in runLanguage>
- decision: <decision required from the Controller in runLanguage>
evidence:
- <relevant file, error, or tool result>
```

### 4.2 DONE 最低内容

```text
summary: <completed result in runLanguage>
details:
- deliverables: <deliverables>
- changed-files: <file list or none>
- residual-risks: <residual risks in runLanguage or none>
next:
- none
needs:
- none
evidence:
- commands: <validation commands>
- results: <validation results in runLanguage>
- git-status: <git status --short or not-applicable>
```

## 5. 主控到 Worker 的消息

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} controllerSeq={{SEQ}} command={{COMMAND}}]
language: {{RUN_LANGUAGE}}
decision: <Controller decision in runLanguage or none>
instructions:
- <next action in runLanguage>
acceptanceDelta:
- <acceptance change in runLanguage or none>
```

`COMMAND` 只能是：

- `DECISION`：回答阻塞问题。
- `REVISION`：验收失败，要求在原范围内修订。
- `SCOPE_UPDATE`：用户已经授权范围变化。
- `LANGUAGE_UPDATE`：用户明确要求切换协调语言。
- `STOP`：用户明确停止，或原任务已失去价值；主控完成成果处置审计后进入 `RETIRED`。

主控不能用 `SCOPE_UPDATE` 自行扩大用户授权，也不能把交付物语言变化误写成 `LANGUAGE_UPDATE`。

## 6. 本地化标题和状态机

### 6.1 英文主控标题

以下五行依次对应 `PLANNING`、`TRACKING`、`WAITING_FOR_USER`、`SYNTHESIZING` 和 `COMPLETE`：

```text
👑 [<runId>] Planning | <overall goal>
👑 [<runId>] Tracking <N> Workers | <overall goal>
👑 [<runId>] Waiting for user decision | <overall goal>
👑 [<runId>] Synthesizing | <overall goal>
👑 [<runId>] Complete | <overall goal>
```

### 6.2 中文主控标题

以下五行依次对应 `PLANNING`、`TRACKING`、`WAITING_FOR_USER`、`SYNTHESIZING` 和 `COMPLETE`：

```text
👑 [<runId>] 拆解｜<总体目标>
👑 [<runId>] 跟进 <N> 个 Worker｜<总体目标>
👑 [<runId>] 等待用户确认｜<总体目标>
👑 [<runId>] 汇总｜<总体目标>
👑 [<runId>] 完成｜<总体目标>
```

`👑` 必须是第一个字符。目标摘要使用 `runLanguage`，但路径、命令和专有名称保持原样。

### 6.3 英文 Worker 标题

以下五行依次对应 `RUNNING`、`REVIEW`、`BLOCKED`、`ACCEPTED` 和 `RETIRED`：

```text
✍️ [<runId>-<workerId>] <action phrase>
🔍 [<runId>-<workerId>] <action phrase> | Awaiting Controller acceptance
⌛️ [<runId>-<workerId>] <action phrase> | <blocker summary>
✅ [<runId>-<workerId>] <action phrase>
🗑️ [<runId>-<workerId>] <action phrase> | <retirement reason>
```

### 6.4 中文 Worker 标题

以下五行依次对应 `RUNNING`、`REVIEW`、`BLOCKED`、`ACCEPTED` 和 `RETIRED`：

```text
✍️ [<runId>-<workerId>] <动宾短语>
🔍 [<runId>-<workerId>] <动宾短语>｜等待主控验收
⌛️ [<runId>-<workerId>] <动宾短语>｜<阻塞摘要>
✅ [<runId>-<workerId>] <动宾短语>
🗑️ [<runId>-<workerId>] <动宾短语>｜<废弃或取代原因>
```

图标必须是第一个字符。Worker 不自行改名。不得使用 `📋` 表达报告、审计、设计或“无合入物”；交付物类型写在标题后缀，生命周期通过验收后使用 `✅`。

### 6.5 状态映射

| 状态 | 标题前缀 | 含义 |
|---|---|---|
| `PROVISIONING` | 暂无或 `⌛️` | 只有 `clientThreadId`，等待真实任务 |
| `RUNNING` | `✍️` | 正在执行或修订 |
| `REVIEW` | `🔍` | Worker 已声明完成，主控正在验收或整合 |
| `BLOCKED` | `⌛️` | 等待决定、澄清、权限、依赖或故障处理 |
| `ACCEPTED` | `✅` | 成功完成、通过归档就绪门，可以人工归档 |
| `RETIRED` | `🗑️` | 已取消、废弃、失效或被取代，通过归档就绪门，可以人工归档 |

允许转换：

```text
PROVISIONING -> RUNNING
PROVISIONING -> RETIRED
RUNNING -> BLOCKED
BLOCKED -> RUNNING
RUNNING -> REVIEW
REVIEW -> RUNNING
REVIEW -> BLOCKED
REVIEW -> ACCEPTED
PROVISIONING/RUNNING/REVIEW/BLOCKED -> RETIRED
```

Worker 自称 `DONE` 只触发 `REVIEW` 和 `🔍`。只有主控验收并通过归档就绪门后才能进入 `ACCEPTED` 和 `✅`。停止、取消或取代也必须通过归档就绪门，才能进入 `RETIRED` 和 `🗑️`。

### 6.6 归档就绪门

`ACCEPTED` 与 `RETIRED` 都是终态，并设置 `archiveReady=true`；两者都允许用户直接人工归档，但本 Skill 不自动调用归档工具。

进入 `ACCEPTED` 前确认：

1. 原 Prompt 范围已完成，主控验收和必要组合验证通过。
2. 原范围要求的 Handoff 或合入已完成；原范围不要求合入时，报告、审计、设计、候选包等交付物已确认可访问。
3. 没有待决策事项，也没有仅存在于临时 worktree 的必要未回收成果。
4. 已记录 `terminalReason`。

进入 `RETIRED` 前确认：

1. 任务不再需要继续执行或等待。
2. 替代任务存在时已记录 `replacementWorkerId`。
3. 有价值成果已回收，或已明确记录不再采用。
4. 没有仍需恢复的唯一未提交成果；否则保持 `BLOCKED`。
5. 已记录 `terminalReason`。

归档任务与删除分支、worktree 或文件是不同操作。`✅` 和 `🗑️` 都不授权自动清理。

## 7. 运行账本

每个 Worker 保存：

| 字段 | 含义 |
|---|---|
| `workerId` | 稳定 ID |
| `threadId` | 真实任务 ID |
| `clientThreadId` | 创建中的临时 ID |
| `hostId` | 当前主机 |
| `state` | 当前逻辑状态 |
| `title` | 期望标题 |
| `objective` | 单一目标 |
| `dependencies` | 前置 Worker |
| `environment` | `local/worktree/projectless` |
| `startingState` | worktree 起始状态 |
| `writeBoundary` | 文件所有权 |
| `integrationPlan` | Handoff 或用户认可的替代方式 |
| `lastSeq` | 已处理 Worker 消息序号 |
| `cursor` | 等待工具 cursor |
| `result` | 验收结果摘要 |
| `archiveReady` | 仅 `ACCEPTED/RETIRED` 且归档就绪门通过时为 `true` |
| `terminalReason` | 完成摘要或废弃原因 |
| `replacementWorkerId` | 被取代时填写，否则 `none` |

运行级保存：

| 字段 | 含义 |
|---|---|
| `runId` | 运行标识 |
| `runLanguage` | `en` 或 `zh-CN` |
| `controllerThreadId` | 主控 ID |
| `controllerHostId` | 主控主机 |
| `maxActiveWorkers` | 默认 8，用户可调 |
| `activeCount` | 非 `ACCEPTED/RETIRED` 数量 |
| `queuedCount` | 未创建的规划任务数 |
| `monitorGroups` | 每组最多 8 个 Worker |
| `localPreferred` | 默认 `true` |

## 8. 并发调整

解析：

```text
requested = 用户明确值，否则 8
effective = min(requested, 值得独立派发且已就绪的任务数, 当前环境已知上限)
```

不要把 8 当成必须创建的 Worker 数量。

- 升高时从既有队列补充。
- 降低时不自动停止现有 Worker，只暂停新派发。
- 大于 8 时重建监控分组。
- 非正整数、含糊范围或环境不支持时请求修正或报告可执行值。
- 每次改变都使用 `runLanguage` 报告旧值、新值、活跃数和排队数。

## 9. 运行中切换语言

初始 `runLanguage` 在本次运行中保持稳定。普通术语切换、代码、引用材料或交付物语言变化都不触发协调语言切换。

只有用户明确要求切换会话或协调语言时：

1. 更新运行账本中的 `runLanguage`。
2. 立即用新语言回复用户，并更新主控标题。
3. 向所有活跃 Worker 发送 `LANGUAGE_UPDATE`，其中 `language` 为新值。
4. Worker 从下一条消息开始使用新语言，不重写历史消息。
5. 新创建的 Worker 使用新语言对应的完整 Worker Prompt。

## 10. 恢复与去重

1. 用 `runId` 搜索标题。
2. 列出任务并匹配 `runId-workerId`。
3. 从运行账本恢复 `runLanguage`；账本缺失时，从主控标题和最近一条实质性用户请求恢复。
4. 读取近期任务状态。
5. 只接受比 `lastSeq` 更新的 Worker 消息。
6. `ACCEPTED/RETIRED` 后的旧 `PROGRESS` 或 `DONE` 不回退状态。
7. `REVISION` 保持相同 Worker ID，消息序号继续增加。
8. 不因上下文丢失重复创建 Worker。
9. 不因恢复、失败、完成或停止而自动归档任何任务；只恢复已有 `archiveReady` 值，不推断或自动执行归档。
