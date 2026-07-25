# Controller 与 Worker 协作协议

协议版本：`2`

## 目录

1. 运行身份
2. 运行语言
3. Worker Prompt 模板
4. Worker 到主控的消息
5. 主控到 Worker 的消息
6. 本地化标题和状态机
7. 运行账本
8. 效率检查与重规划
9. 并发调整
10. 运行中切换语言
11. 恢复与去重

## 1. 运行身份

每次运行生成：

- `protocolVersion`：固定为 `2`。
- `runId`：短运行标识，例如 `R7K2`。
- `runLanguage`：`en` 或 `zh-CN`。
- `workerId`：稳定 Worker 标识，例如 `W1`。
- `controllerThreadId`：主控真实任务 ID。
- `controllerHostId`：跨主机时必填；同主机且工具不要求时可省略。

主控必须把 `protocolVersion`、自己的真实 `threadId` 和 `runLanguage` 直接写入每个 Worker Prompt。Worker 不猜测、不搜索主控地址，也不自行选择初始协调语言。协议版本不匹配时，Worker 立即用 `BLOCKED` 报告并停止需要协调的动作。

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

按 `runLanguage` 和 `coordinationProfile` 选择模板：

- `en` compact：[worker-prompt.en.compact.md](worker-prompt.en.compact.md)
- `zh-CN` compact：[worker-prompt.zh-CN.compact.md](worker-prompt.zh-CN.compact.md)
- 显式 `strict`：使用同语言的 [worker-prompt.en.md](worker-prompt.en.md) 或 [worker-prompt.zh-CN.md](worker-prompt.zh-CN.md)

派发规则：

1. 未显式指定档位时，只读任务推断为 `lean`，写入任务推断为 `standard`；两者使用 compact 模板。只有高风险或强审计任务显式使用 `strict` 完整模板。
2. `lean` 禁止写文件；`standard` 保留 worktree、写边界与 Git 证据规则；三档都保留地址、消息序号、阻塞、完成和账本单写者规则。
3. 完整读取实际选中的模板，填充所有占位符，并让任务目标、范围、边界和验收说明使用 `runLanguage`；路径、命令、代码和交付物要求保持原样。
4. 不临时翻译另一份模板，也不把两种语言或两种档位混合进同一个 Worker Prompt。
5. 优先使用 [dispatch.md](dispatch.md) 中的 renderer 填充模板，并将完成后的模板正文作为 `create_thread` 初始 `prompt`。
6. 同一主机且 `hostId` 可省略时，删除整个 `controllerHostId` 行和消息调用中的整个 `hostId` 字段，不传空字符串。

## 4. Worker 到主控的消息

结构化字段保持英文，字段内容使用当前 `runLanguage`：

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} seq={{SEQ}} type={{TYPE}}]
summary: <one-line status in runLanguage>
details:
- <key fact or result in runLanguage>
milestone: <current observable milestone in runLanguage>
completed:
- <acceptance item closed since the previous message, or none>
remaining:
- <remaining acceptance item>
estimate: <estimated remaining time, or unknown with a reason>
incidentClass: <NONE|EXPECTED_RESULT|RECOVERABLE_CONTROL|CONTROL_DEGRADED|WORK_BLOCKER>
localCorrectionAttempts: <local correction attempts used, or 0>
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

`incidentClass` 的含义：

- `NONE`：没有异常。
- `EXPECTED_RESULT`：步骤契约接受的退出码或失败签名，例如无匹配搜索或签名正确的 TDD Red。
- `RECOVERABLE_CONTROL`：引号、路径、参数、解析器、命令形状、已知 wrapper，或已经证明无未知部分写入的 patch/formatter 控制错误。
- `CONTROL_DEGRADED`：标题、cursor、wait、renderer、临时 JSON 或消息传输等控制面暂时不可用。
- `WORK_BLOCKER`：需要决定、权限、凭据、依赖或范围变化，存在不可逆风险、timeout、未知部分写入，或本地纠错预算已经耗尽。

只有 `WORK_BLOCKER` 可以发送 `TYPE=BLOCKED`。前三类用 `PROGRESS` 报告并继续契约允许的安全工作。

### 4.1 ACCEPTED 与 PROGRESS 最低内容

- `ACCEPTED` 必须给出 2–5 个可观察里程碑、首个健康检查点和已知长命令的预期墙钟时间。
- `PROGRESS` 必须说明本次关闭了哪个验收项；没有关闭时写 `none` 并解释仍有价值的进展。
- 长命令开始前发送一次 `PROGRESS`，写明命令、预期墙钟时间、不可安全中断的边界和超时处理。
- 消息频繁不等于有效进展；主控用 `completed` 和 `lastUsefulProgressAt` 判断健康度。

### 4.2 BLOCKED 最低内容

```text
summary: <blocker in runLanguage>
details:
- facts: <confirmed facts in runLanguage>
- cause: <why work cannot continue in runLanguage>
milestone: <blocked milestone>
completed:
- <last closed acceptance item or none>
remaining:
- <work blocked by this decision>
estimate: blocked
incidentClass: WORK_BLOCKER
localCorrectionAttempts: <used attempts or 0>
next:
- option-a: <option and impact in runLanguage>
- option-b: <option and impact in runLanguage>
needs:
- recommendation: <Worker recommendation in runLanguage>
- decision: <decision required from the Controller in runLanguage>
evidence:
- <relevant file, error, or tool result>
```

### 4.3 DONE 最低内容

```text
summary: <completed result in runLanguage>
details:
- deliverables: <deliverables>
- changed-files: <file list or none>
- residual-risks: <residual risks in runLanguage or none>
milestone: complete
completed:
- <all closed acceptance items>
remaining:
- none
estimate: none
next:
- none
needs:
- none
evidence:
- commands: <validation commands>
- results: <validation results in runLanguage>
- git-status: <git status --short or not-applicable>
```

### 4.4 失败分类与局部纠错

1. 先核对步骤结果契约；命中的预期 nonzero 不消耗纠错预算。
2. 对 `RECOVERABLE_CONTROL`，先证明没有未知部分写入、边界或权限变化，再在 Worker 的 `failurePolicy.localCorrectionBudget` 内本地修正。成功后发送 PROGRESS，不进入 BLOCKED。
3. `CONTROL_DEGRADED` 保留真实工作状态；继续不依赖新决定的安全工作。工具恢复后发送一条合并后的 PROGRESS。
4. timeout、未知部分写入、权限/范围变化或预算耗尽立即升级为 `WORK_BLOCKER`。
5. 同一错误不得通过改写命令外观无限重试；纠错次数按根因累计。

### 4.5 低噪声观察

- 持续等待只覆盖 `PROVISIONING/RUNNING`；`REVIEW/BLOCKED` 仍计入活跃槽位，但在验收、决定或外部条件变化时按需读取。
- 内部观察不产生逐轮用户心跳；状态未变化时，只有仍值得用户关注的长运行才约每 10 分钟汇报一次有信息量摘要。
- 正常等待 timeout 不是失败。任务服务临时异常按 5/15/30/60 秒上限退避；连续 2 次 timeout/无响应后熔断 2 分钟，不切换 `list/read/wait` 变体轮询。
- 只有 cursor 改变时才落账。timeout 不能证明 Worker 消失或工作阻塞，不得据此重复创建、发送、改标题或进入终态。

## 5. 主控到 Worker 的消息

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} controllerSeq={{SEQ}} command={{COMMAND}}]
language: {{RUN_LANGUAGE}}
reason: <reason in runLanguage or none>
decision: <Controller decision in runLanguage or none>
instructions:
- <next action in runLanguage>
acceptanceDelta:
- <acceptance change in runLanguage or none>
stepContracts:
- {"step":"<bounded step>","acceptedExitCodes":[0],"expectedFailureSignature":null,"timeoutSeconds":120,"partialWriteCheck":"<verification>"}
```

没有有界批次时省略整个 `stepContracts` 段。

`COMMAND` 只能是：

- `DECISION`：回答阻塞问题。
- `CHECKPOINT`：要求 Worker 在下一个安全边界暂停新阶段，并用 `PROGRESS` 回报已完成/剩余验收、文件与未提交成果、冗余工作、可拆分单元和预计剩余时间。
- `REPLAN`：只在原始授权内调整执行顺序、批量授权、文件所有权或剩余职责；不能放宽验收或扩大外部影响。
- `REVISION`：验收失败，要求在原范围内修订。
- `SCOPE_UPDATE`：用户已经授权范围变化。
- `LANGUAGE_UPDATE`：用户明确要求切换协调语言。
- `STOP`：用户明确停止，或原任务已失去价值；主控完成成果处置审计后进入 `RETIRED`。

`controllerSeq` 从 `001` 开始严格单调增加。Worker 保存 `lastControllerSeq`，只执行更大的序号；重复或更旧的命令不得重复执行，只用 `PROGRESS` 确认已忽略并给出已应用的最新序号。

成功发送 `DECISION/REVISION` 会由账本自动累计微放行往返。达到 3 次后拒绝更多微放行，主控先用 `CHECKPOINT` 获取安全边界，再发送带 `stepContracts` 的 `REPLAN`。每步必须声明可接受退出码、可选预期失败签名、timeout 和部分写入核对；只有契约外失败、timeout 或未知部分写入停止。该有界消息成功后清零计数；失败、未知和重复 outcome 不计数。历史 pending `executionPlan` 仍可恢复执行，但新命令不再生成它。

主控不能用 `REPLAN` 或 `SCOPE_UPDATE` 自行扩大用户授权，也不能把交付物语言变化误写成 `LANGUAGE_UPDATE`。

## 6. 本地化标题和状态机

### 6.1 英文主控标题

以下六行依次对应 `PLANNING`、`TRACKING`、`REPLANNING`、`WAITING_FOR_USER`、`SYNTHESIZING` 和 `COMPLETE`：

```text
👑 [<runId>] Planning | <overall goal>
👑 [<runId>] Tracking <N> Workers | <overall goal>
👑 [<runId>] Replanning | <overall goal>
👑 [<runId>] Waiting for user decision | <overall goal>
👑 [<runId>] Synthesizing | <overall goal>
👑 [<runId>] Complete | <overall goal>
```

### 6.2 中文主控标题

以下六行依次对应 `PLANNING`、`TRACKING`、`REPLANNING`、`WAITING_FOR_USER`、`SYNTHESIZING` 和 `COMPLETE`：

```text
👑 [<runId>] 拆解｜<总体目标>
👑 [<runId>] 跟进 <N> 个 Worker｜<总体目标>
👑 [<runId>] 重规划｜<总体目标>
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

健康度不增加新的生命周期图标。效率审查时可使用：

```text
✍️ [<runId>-<workerId>] <action phrase> | Efficiency review
✍️ [<runId>-<workerId>] <动宾短语>｜效率审查
⌛️ [<runId>-<workerId>] <action phrase> | Waiting for replanning
⌛️ [<runId>-<workerId>] <动宾短语>｜等待重规划
```

### 6.5 状态映射

| 状态 | 标题前缀 | 含义 |
|---|---|---|
| `PROVISIONING` | 暂无或 `⌛️` | 只有 `clientThreadId`，等待真实任务 |
| `RUNNING` | `✍️` | 正在执行或修订 |
| `REVIEW` | `🔍` | Worker 已声明完成，主控正在验收或整合 |
| `BLOCKED` | `⌛️` | 等待决定、澄清、权限、依赖，或处理 timeout/未知部分写入等真实工作阻塞 |
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

### 6.6 调度健康度

健康度与生命周期正交：

| 健康度 | 含义 |
|---|---|
| `HEALTHY` | 正在按计划关闭里程碑，或处于已声明的合法长命令窗口 |
| `AT_RISK` | 触发软阈值，主控正在 checkpoint 或重规划；不代表失败 |
| `STALLED` | 一次有界重规划后仍没有有效进展，或确实等待外部决定；进入 `BLOCKED` |

时间只能触发审查，不能直接触发 `STOP`、`RETIRED` 或替代 Worker。

### 6.7 归档就绪门

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

账本必须是 [ledger.md](ledger.md) 定义的本地 SQLite 数据库，而不是仅存在于主控上下文中的 Markdown 表格或内存对象。

- 默认路径为 `<PROJECT_ROOT>/.codex/runtime/orchestrate-codex-tasks/runs/<runId>/ledger.sqlite3`。
- 只有 Controller 通过 `scripts/ledger.py` 写入；Worker 不访问账本。
- 编译 manifest、任务规范、地址、生命周期、健康、序号、cursor、决定、整合、cycle 和外部 operation 都必须持久化。
- `create_thread`、跨任务消息、标题和 Handoff 使用 `intent -> tool call -> outcome`，恢复时先核对 pending operation，不能盲目重试。
- 所有事件使用稳定 idempotency key，事件表 append-only。
- 每次上下文恢复先执行一次原子 `snapshot`，再用观察事实执行 `audit`；只有专项诊断才拆成 `verify/status/pending`。
- `✅/🗑️` 仍只代表 `archiveReady=true`；账本和脚本都不自动归档。

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
| `lastControllerSeq` | Worker 已应用的最新主控命令序号 |
| `cursor` | 等待工具 cursor |
| `result` | 验收结果摘要 |
| `health` | `HEALTHY/AT_RISK/STALLED` |
| `currentMilestone` | 当前可观察里程碑 |
| `closedAcceptanceItems` | 已关闭的验收项 |
| `remainingAcceptanceItems` | 剩余验收项 |
| `lastDetails/nextActions/needs/evidence` | 最近一条 Worker 协议消息的事实、下一步、待决事项和证据 |
| `lastUsefulProgressAt` | 最近一次关闭验收项或产生可复核成果的时间 |
| `estimatedRemaining` | Worker 当前估计及理由 |
| `decisionRoundTrips` | 可预见的主控逐步放行往返数 |
| `scopeDeltaCount` | 新增验收族、子系统或写入边界次数 |
| `timeoutCount` | timeout 次数 |
| `nextHealthReviewAt` | 下一次效率复查点 |
| `archiveReady` | 仅 `ACCEPTED/RETIRED` 且归档就绪门通过时为 `true` |
| `terminalReason` | 完成摘要或废弃原因 |
| `replacementWorkerId` | 被取代时填写，否则 `none` |
| `promptVersion/promptHash` | Worker Prompt 协议与内容指纹 |

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
| `oneToOneSince` | 只有一个活跃 Worker 且无队列时的起始时间；否则为空 |
| `currentManifestHash` | 当前规范化调度清单 |
| `controllerEpoch` | 单写者接管代次 |
| `revision` | append-only 事件修订号 |

账本 schema、命令、备份、恢复、隐私和降级规则以 [ledger.md](ledger.md) 为准；清单结构和渲染规则以 [dispatch.md](dispatch.md) 为准。

## 8. 效率检查与重规划

满足任一条件时把 Worker 标记为 `AT_RISK` 并进行审查：

1. 约 30 分钟没有关闭验收项，且不在已声明长命令窗口；长命令超过预期约 2 倍或已无执行迹象也触发。
2. 连续 3 条 `PROGRESS` 没有关闭里程碑。
3. 账本自动记录 3 次成功的可预见逐步放行往返。
4. 新增原计划之外的验收族、顶层子系统或写入边界。
5. 出现 timeout、重复诊断、上下文压缩、序号重复或同一失败路径反复尝试。
6. `activeCount=1`、`queuedCount=0` 持续约 15 分钟，且剩余工作可独立拆分。

主控执行：

1. 三次微放行自动进入 `AT_RISK`；其他触发由主控记录。使用 `REPLANNING` 标题，向 Worker 发送 `CHECKPOINT`。
2. 核对已完成/剩余验收、文件与未提交成果、ETA、冗余执行、可拆分单元和下一条不可中断命令。
3. 在原始授权内选择：继续并设下一检查点；发送 `REPLAN` 批量授权有界 manifest；删除被更强证据覆盖的重复执行；拆分独立剩余工作；或在成果保护后替换 Worker。
4. 有未提交唯一成果的写入型 Worker 必须先完成授权内的 checkpoint、Handoff 或其他持久化，不能直接创建第二个写入者接管同一边界。
5. 向用户报告触发原因、决定、并发变化、下一检查点和风险。

批量授权必须使用逐步 `stepContracts`，把 `rg` 无匹配、签名正确的 TDD Red 等预期 nonzero 明确列入契约，并为每步设置 timeout 与部分写入检查。不要使用全局“首个 nonzero 即停”。`REPLAN` 不得降低验收、扩大范围、自动提交或制造低价值并发。只有一次有界重规划后仍无有效进展，才进入 `STALLED/BLOCKED`。

## 9. 并发调整

解析：

```text
requested = 用户明确值，否则 8
effective = min(requested, 值得独立派发且已就绪的任务数, 当前环境已知上限)
```

不要把 8 当成必须创建的 Worker 数量。

- 升高时同时更新 ledger 和当前 manifest 的上限、重新编译激活，再从既有队列补充。
- 降低时同时更新 ledger 和 manifest，不自动停止现有 Worker，只暂停新派发。
- 大于 8 时重建监控分组。
- 非正整数、含糊范围或环境不支持时请求修正或报告可执行值。
- 每次改变都使用 `runLanguage` 报告旧值、新值、活跃数和排队数。

## 10. 运行中切换语言

初始 `runLanguage` 在本次运行中保持稳定。普通术语切换、代码、引用材料或交付物语言变化都不触发协调语言切换。

只有用户明确要求切换会话或协调语言时：

1. 记录用户语言决定，更新运行账本中的 `runLanguage`。
2. 对尚未创建的 task 用 `TASK_REPLANNED` 只翻译人类可读规范，不改变范围或验收含义；重新编译并激活新语言 manifest。
3. 立即用新语言回复用户，并更新主控标题。
4. 向所有活跃 Worker 发送 `LANGUAGE_UPDATE`，其中 `language` 为新值。
5. Worker 从下一条消息开始使用新语言，不重写历史消息。
6. 新创建的 Worker 使用新语言与既定协调档位对应的 Worker Prompt。

## 11. 恢复与去重

1. 从稳定运行目录打开账本，执行一次只读事务 `snapshot`，同时取得 verification、边界化 status 和详细 pending operations。
2. 从账本恢复 `runId`、`runLanguage`、Controller、当前 manifest、Worker 地址、cursor 和序号。
3. 列出任务并匹配 `runId-workerId`，用即时等待快照和必要的紧凑读取形成 observed facts，再执行只读 `audit`；任务服务 timeout 时遵循 4.4 的熔断规则。
4. 先核对 pending `INTENT/UNKNOWN`；没有外部证据前不重试创建、消息、标题或 Handoff。
5. 只接受比 `lastSeq` 更新的 Worker 消息。
6. 新命令只使用 ledger 分配的更大 `controllerSeq`，绝不复用旧序号。
7. Worker 忽略重复或更旧的 `controllerSeq`，主控也不因确认消息再次执行相同决定。
8. `ACCEPTED/RETIRED` 后的旧 `PROGRESS` 或 `DONE` 不回退状态。
9. `REVISION` 保持相同 Worker ID，消息序号继续增加。
10. 恢复健康字段；缺失时从最近的可复核成果、timeout 和范围变化保守重建，不把消息频繁推断为 `HEALTHY`。
11. 账本缺失或损坏时按 ledger reference 从备份恢复或保守重建；无法唯一核对就阻塞。
12. 不因上下文丢失重复创建 Worker，不把内存状态冒充持久账本。
13. 不因恢复、失败、完成或停止而自动归档任何任务；只恢复已有 `archiveReady` 值，不推断或自动执行归档。
