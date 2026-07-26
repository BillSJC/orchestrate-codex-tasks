---
name: orchestrate-codex-tasks
description: Coordinate the current Codex task as a Controller with multiple independent Codex Worker tasks/threads through a durable local SQLite ledger, validated dispatch manifests, task creation, cross-task messages, status-title updates, health checks, adaptive replanning, worktree isolation, and result synthesis. Run all visible coordination in English or Chinese to match the user's orchestration request. Use only when the user explicitly invokes this skill to execute work or clearly asks for separate, independent, or background Codex tasks, a Controller + Worker workflow, cross-task coordination, “主控 + Worker”, “独立任务并发”, or “跨任务并发”. Do not use for Codex subagents, generic requests to work faster, vague parallelism, shell/program concurrency, simple tightly coupled work, or requests that do not authorize creating new Codex tasks.
---

# Orchestrate Codex Tasks

把当前 Codex 任务作为唯一主控（Controller），把适合独立执行的工作派发给新建的 Codex Worker 任务。Worker 必须是独立任务，不是子 Agent。

## 运行语言

在首次用户可见汇报、任务改名或 Worker 创建之前确定 `runLanguage`：

1. 用户明确指定协调或会话语言时，使用该语言。
2. 否则只判断触发本次编排的用户自然语言；忽略代码、路径、引用内容，以及交付物自身的目标语言。
3. 中文请求使用 `zh-CN`，英文请求使用 `en`。中英文混合且无法明确判断时，使用最后一个有实际语义的用户自然语言句子，不要仅为语言选择阻塞运行。
4. 把 `runLanguage` 写入运行账本，并在本次运行中保持不变。用户之后明确要求切换时，按 protocol reference 的 `LANGUAGE_UPDATE` 流程更新主控和所有活跃 Worker。

把 `runLanguage` 应用于主控回复、进度汇报、任务标题、Worker Prompt、消息正文、阻塞选项、验收和最终汇总。协议枚举、字段名、工具名、ID、路径、命令和代码保持原样。交付物语言由任务要求决定，不得反向改变协调语言；例如，中文请求编写英文 README 时，协调仍使用中文。

## 强制边界

- 只使用独立任务工具创建 Worker；不得调用 `spawn_agent`，也不得让 Worker 派生其他 Worker 或子 Agent。
- 把新任务创建限制在用户本次明确授权的工作范围内。
- 永不自动归档主控或 Worker。`✅` 和 `🗑️` 只表示任务已通过归档就绪门、可以由用户人工归档；图标本身不触发归档、删除或 worktree 清理。
- 不用调度流程绕过审批、沙箱、凭据、外部写入或其他权限边界。
- 不自动提交、推送、开 PR、发布或删除，除非用户的原始请求明确包含相应动作。
- 保留单一决策中心：Worker 负责执行和提供证据，主控负责范围、决策、冲突处理、验收和最终汇总。
- 主控是本地 SQLite 账本的唯一写者。Worker 不读取、写入、复制或删除主控账本，只通过跨任务消息报告。
- 只通过 `scripts/ledger.py` 写账本；不得直接执行 SQL 修改状态。`scripts/dispatch.py` 只验证和渲染，不能替代真实 Codex 工具调用。
- 用户要求持续推进、监控到完成或已经启动 Goal 时，控制面故障不得结束运行或把恢复责任转给用户；主控必须保持 `DEGRADED` 并使用当前任务的自主重试或 heartbeat 无限续跑。重试次数、连续失败次数和总等待时长均无上限，退避只降低调用频率，不是停止、`BLOCKED`、goal blocked 或 final 的条件。只有控制面恢复、出现与控制面无关的真实用户决定，或用户明确暂停/取消才能结束该恢复循环。

## 读取运行协议

在创建第一个 Worker 前：

1. 完整读取 [references/tool-contracts.md](references/tool-contracts.md)，确认当前任务工具、worktree、远程主机和 Handoff 契约。
2. 完整读取 [references/protocol.md](references/protocol.md)，使用其中的语言规则、消息格式和标题状态机。
3. 完整读取 [references/ledger.md](references/ledger.md)，按其中顺序建立本地 SQLite 事实源、记录外部操作并恢复运行。
4. 完整读取 [references/dispatch.md](references/dispatch.md)，使用规范化清单、DAG、写边界和渲染脚本。
5. 默认完整读取匹配 `runLanguage` 的 compact 模板（[references/worker-prompt.en.compact.md](references/worker-prompt.en.compact.md) 或 [references/worker-prompt.zh-CN.compact.md](references/worker-prompt.zh-CN.compact.md)）。只有 manifest 中存在显式 `coordinationProfile=strict` 时，再完整读取同语言的 strict 模板（[references/worker-prompt.en.md](references/worker-prompt.en.md) 或 [references/worker-prompt.zh-CN.md](references/worker-prompt.zh-CN.md)）。不得加载另一种语言。
6. 如果当前运行时工具契约与 reference 不同，以当前可调用工具的 schema 为准，并用 `runLanguage` 向用户说明会影响行为的差异。

## 1. 执行授权门

只有以下情况允许调用新任务创建工具：

- 用户显式调用 `$orchestrate-codex-tasks` 并要求执行具体工作。
- 用户明确要求创建独立、后台或单独的 Codex 任务，并由当前任务统一协调。
- 用户明确要求“主控 + Worker”“独立任务并发”或“跨任务并发”。

以下弱信号只允许提出拆分建议，不得创建任务：

- “能不能并行？”
- “帮我快一点。”
- “你自己决定是否拆分。”
- 没有说明是独立 Codex 任务的“并发执行一下”。

遇到弱信号时，给出建议 Worker 数量和拆分摘要，等待用户明确授权。用户要求子 Agent、普通程序并发、单一短动作、强耦合工作或禁止新任务时，不执行本流程。

## 2. 预检与拆解

1. 发现并确认以下核心能力可用：
   - 创建、列出、读取、等待和改名 Codex 任务；
   - 向其他任务发送消息；
   - 列出项目；
   - 写代码时还需具备 Handoff 及其状态查询能力，或在派发前确定用户认可的替代回收方案。
   - 发现当前任务 heartbeat/automation 能力；用户要求持续推进、监控到完成或启动 Goal 时，把它视为跨 turn 无限恢复的核心能力。缺失时必须在派发前说明环境限制，并在当前 Goal/turn 内保持无限退避重试，不能在发生控制面故障后用 blocked/final 代替续跑。
2. 生成短且本次唯一的 `runId`，例如 `R7K2`。
3. 获取并验证当前主控的 `threadId`；跨主机时同时获取 `hostId`。优先使用运行时直接提供的地址；缺失时才按 ledger reference 的受限 bootstrap 设置唯一 `runId` 标题并从任务列表解析。任务 API timeout 或失联导致无法核对时按 5.1 无限恢复；只有控制面健康但存在多个真实匹配、因而无法唯一寻址时才停止并请求修正。
4. 按 ledger reference 初始化稳定的本地 SQLite 账本，并读取 stdout 返回的 `databasePath`。Git 项目必须用本地 `.git/info/exclude` 排除运行目录。初始账本无法安全创建时不派发 Worker。
5. 使用 dispatch script 渲染匹配 `runLanguage` 的 `PLANNING` 标题。除寻址 bootstrap 外，先写 `SET_TITLE` intent，再调用实际标题工具并写 outcome。
6. 解析并发上限：
   - 默认 `maxActiveWorkers = 8`。
   - 接受用户明确给出的正整数覆盖值。
   - 并发值只是上限；不要为了填满槽位制造低价值 Worker。
7. 把工作拆成轻量 DAG。每个 Worker 必须具备单一目标、明确输入、2–5 个可观察里程碑、独立验收条件、文件所有权和已知依赖。只读任务默认省略档位并推断为 `lean`，写入任务推断为 `standard`；只有高风险、强审计需求才显式使用 `strict`。
8. 派发前进行任务重量审查。跨越多个顶层子系统、包含多个可独立验收目标、同时承担实现/规格/TDD/完整回归，或存在大量未知前置时，优先继续拆分；确实不可拆时，记录原因、首个健康检查点和预计最慢合法命令。
9. 把高度耦合实现、共享接口定稿、用户偏好、风险接受和最终整合留给主控。
10. 使用项目列表选择执行位置：
   - 用户明确指定的主机优先于默认策略；
   - 否则优先本地同项目；
   - 仅在本地缺少必要项目、依赖或能力时选择明确匹配的远程项目。
11. 写 draft manifest，使用 dispatch script 校验并编译，再用 ledger script 原子激活。浏览器、模拟器、GPU 或限流服务确实稀缺时才声明 `resourceCapacities/resourceClaims`；不要为普通任务制造资源配置。清单校验、持久化和 task 规划完成前不创建 Worker。

## 3. 选择 Worker 环境

- 一般研究或资料比较：使用 `projectless`。
- 只读代码分析：优先使用本地项目 `local`。
- 任何项目文件写入：默认使用独立 `worktree`。
- 非 Git 项目不能使用 worktree；先向用户说明降级方案。
- 即使使用不同 worktree，也要为每个 Worker 声明不重叠的文件边界和共享接口规则。
- 只有 worktree 不可用、用户仍明确要求继续且文件边界完全不重叠时，才允许写入型 Worker 降级到共享 `local`；必须先报告风险。

默认省略 worktree 的 `startingState`，从项目默认分支开始。只有用户明确要求基于当前 checkout（包括未提交修改）或某个现有分支时，才设置对应起始状态。不得擅自提交用户改动或假设默认 worktree 能看到未提交文件。

## 4. 派发 Worker

对依赖已满足的 Worker，最多派发到 `maxActiveWorkers`：

1. 从 ledger `status` 和当前编译 manifest 运行 dispatch `ready`；只处理 `readyWorkers`，不得仅凭上下文估算槽位、依赖、写边界或资源容量。
2. 为 Worker 使用清单中的稳定 `workerId`，例如 `W1`。
3. 使用 dispatch `render-worker` 生成匹配语言和自适应档位的 Worker Prompt、`promptHash`、标题和 create request。`lean/standard` 使用 compact 模板，显式 `strict` 使用完整模板。Prompt 必须注入：
   - `runId`、`workerId`；
   - 主控 `threadId` 和需要时的 `hostId`；
   - 目标、范围、输入、禁止事项；
   - 主机、环境和 worktree 起始状态；
   - 文件写入边界、成果回收方式和验收命令。
   - 2–5 个可观察里程碑、首个健康检查点和已知长命令的预期墙钟时间。
   - 失败分类策略和本地纠错预算。默认允许对已经证明无部分写入、无边界变化、无权限扩张的控制/命令输入错误本地纠正一次。
4. 用渲染结果的最小化 request 写 `CREATE_THREAD` intent；之后直接调用实际高层创建工具，再用清理后的稳定 ID 写 outcome。不得把 `create_thread` 或后续 `list/read/wait` 包进可能被终止的 shell、PTY 或 yielded exec cell，也不得通过终止外层等待取消含糊的在途创建。除非用户明确指定，否则不覆盖 Worker 的模型或 reasoning 配置。
5. 记录返回的 `threadId` 或 `clientThreadId`、`hostId`、环境和状态。工具超时或结果含糊时保留 pending/unknown，先核对外部事实，不重复创建。
6. 获得真实 `threadId` 后，按相同 intent/tool/outcome 顺序设置渲染好的 `RUNNING` 标题。
7. 首波派发完成后，以 ledger 中的实际活跃数渲染 `TRACKING` 主控标题，并按 intent/tool/outcome 更新。
8. 立即用 `runLanguage` 向用户报告 Worker 名称、目标、当前活跃数、排队数、并发上限、worktree 起始状态和任何远程主机选择。

worktree 只返回 `clientThreadId` 时，将 Worker 记为 `PROVISIONING`。在解析到真实 `threadId` 前，不对临时 ID 调用等待、改名或跨任务消息工具，也不重复创建相同 Worker。

## 5. 监控与进度汇报

同时使用两个通道：

- 推送：处理 Worker 发来的 `ACCEPTED`、`PROGRESS`、`BLOCKED` 和 `DONE`。
- 拉取：持续使用任务等待工具和 cursor 增量观察；状态含糊或需要证据时读取任务。

规则：

- 默认最多 8 个活跃 Worker。持续等待集合只放 `PROVISIONING/RUNNING`；`BLOCKED/REVIEW` 仍计入活跃槽位，但按事件或验收需要读取，避免静止任务制造轮询压力。
- 用户把并发调到 8 以上时，按稳定顺序分成每组最多 8 个目标；每轮先对所有组取即时增量快照，再对一个轮转组做不超过约 15 秒的等待。
- `PROVISIONING/RUNNING` Worker 最迟每 60 秒主动观察一次；`REVIEW` 在验收动作发生时读取；`BLOCKED` 只在收到决定、外部条件变化或用户要求时复核。内部观察不等于用户可见消息，正常等待 timeout 也不是控制系统阻塞。
- 派发、阻塞、验收完成、重试、范围变化和替换发生时立即向用户汇报。
- 没有状态变化时，不为每轮观察发送消息；只有运行仍需要用户关注时，最长约每 10 分钟发送一次有信息量的摘要。
- 任务服务返回临时不可用、限流或传输错误时，保持 Worker 状态并按 5/15/30/60 秒退避；连续 2 次 timeout 或无响应后停止切换 `list/read/wait` 变体探测 2 分钟，之后每 2 分钟做一次单一恢复核对并无限循环。只有状态确实含糊时做一次任务列表或紧凑读取核对，不并发重试、不改标题。timeout 不是 Worker 消失或工作阻塞的证据；没有最大重试次数或最长恢复时长。
- 不把 Worker 的自称完成直接视为验收通过。
- 不把消息频繁等同于有效进展。每次 `PROGRESS` 更新当前里程碑、已经关闭和剩余的验收项、预计剩余时间，以及正在运行的已声明长命令。
- 每条可接受的 Worker 消息先以 `WORKER_MESSAGE_APPLIED` 落账，再执行标题、回复、Handoff 或下一波派发。旧 `seq` 只确认忽略，不重复状态变化。
- 只有 cursor 确实变化时才保存。跨任务消息先由账本分配 `controllerSeq`，再用 dispatch script 渲染，按 intent/tool/outcome 发送。

把 `PROVISIONING`、`RUNNING`、`BLOCKED` 和 `REVIEW` 都计入活跃数。`ACCEPTED` 或 `RETIRED` 才释放槽位。

### 5.1 控制面自主恢复与 heartbeat

当 `create/list/read/wait/send/title/handoff-status` 任一任务控制调用超时、失联或结果含糊时：

1. 保留 Worker 的真实生命周期；外部结果含糊的 operation 写 `UNKNOWN`，并用 `RUN_UPDATED status=DEGRADED` 记录运行级降级。控制面失败无论重复多少次、持续多久，都不得累计成 Worker `AT_RISK/STALLED/BLOCKED`，不得调用 `update_goal(status=blocked)`，不得输出等待用户恢复的 final。
2. 先继续不依赖新决定或新外部副作用的安全工作；按 5/15/30/60 秒退避，随后每 2 分钟执行一次单一恢复核对并无限循环，不在 `list/read/wait` 之间高频切换。熔断表示降频，不表示终止。
3. 原目标尚未完成且当前 turn 将结束时，若当前任务 heartbeat 工具可用，必须创建或更新一个附着到当前主控任务的分钟级 heartbeat。名称包含 `runId` 且可唯一查找；Prompt 要求从 ledger `snapshot` 恢复、核对所有 `INTENT/UNKNOWN`、只在外部事实明确后重试，并在恢复或运行终态后停用/删除自身。heartbeat 不是 Worker，不占并发槽位，不得创建新任务。
4. heartbeat 创建前检查是否已有同名活动项并优先更新，禁止重复。创建后可向用户报告 `DEGRADED + 自动重试已启用`，但不得要求用户发送“继续”。
5. heartbeat 工具不可用时，主控在当前 Goal/turn 内继续每 2 分钟的无限退避重试；不得因为 heartbeat 缺失、产品控制调用持续失败、重试次数或经过时长进入 blocked/final。控制面恢复本身不是用户决定，用户也不是恢复调度器。
6. 恢复后先用任务列表、即时快照、必要的紧凑读取和 worktree 事实核对原 operation。只在原 `CREATE_THREAD` 已确认 `FAILED` 且目标 Worker 不存在时，恢复 task 为 `QUEUED` 并使用新 request ID 发起一次新的受控尝试；若该尝试再次明确失败，重新核对后继续同样循环且总次数无上限。每轮只允许一个在途 create，`UNKNOWN` 永不直接重建。
7. 恢复闭合后用 `RUN_UPDATED status=ACTIVE`，更新 ledger outcome/cursor，停用或删除恢复 heartbeat，再继续原 DAG。运行完成、用户暂停或取消时同样清理该 heartbeat。
8. operation 处于 `INTENT/UNKNOWN` 时，无限循环只重复只读恢复审计，不重复含糊的外部写操作。只有外部事实排除副作用并把上一轮闭合为 `FAILED` 后，才用新 request ID 发起一个新的受控写尝试；“核对 → 明确失败 → 单次新尝试”循环本身没有次数或时长上限。`UNKNOWN`、幂等门、文件写边界、权限和不可逆操作规则始终有效，任何时刻不得并发或盲目重复创建、发送、Handoff 或改名。

## 6. 效率审查与重规划

健康度与生命周期正交，账本使用 `HEALTHY/AT_RISK/STALLED`；不要增加新的生命周期图标。时间阈值只触发审查，不自动停止 Worker。

出现以下任一软触发条件时立即进行效率审查：

- 连续约 30 分钟没有关闭验收项，且不处于已声明的合法长命令窗口；长命令超过预期约 2 倍或已无可观察执行迹象时也触发。
- 连续 3 条 `PROGRESS` 没有关闭里程碑，或账本自动记录 3 次成功的“主控放行一步、Worker 执行一步”往返。
- 新增原计划之外的验收族、顶层子系统、写入边界或未知架构工作。
- 出现 timeout、重复诊断、上下文压缩、消息序号重复或同一失败路径反复尝试。
- `activeCount=1` 且 `queuedCount=0` 持续约 15 分钟，而剩余工作仍包含可独立执行单元。

审查流程：

1. 三次微放行由成功的 `SEND_MESSAGE` outcome 自动把 Worker 标为 `AT_RISK`；其他触发条件用 `WORKER_HEALTH_CHANGED` 记录。再使用 dispatch script 渲染 `REPLANNING` 主控标题；Worker 继续执行时保留 `✍️` 并加“效率审查”后缀，暂停等待重规划时使用 `⌛️`。
2. 发送 `CHECKPOINT`，要求 Worker 在安全边界暂停新阶段并回报：已完成/剩余验收项、当前里程碑、文件与未提交成果、重复或冗余工作、可拆分单元、预计剩余时间和下一条不可中断命令。
3. 主控在原始授权内选择并记录一种处理；task 规格变化使用 `TASK_REPLANNED`，新 manifest 重新编译并激活：
   - 继续当前计划，但给出理由、下一个可验证里程碑和复查时间；
   - 发送带 `stepContracts` 的 `REPLAN`，一次性授权已经核对的有界步骤；每步声明 `acceptedExitCodes`、可选 `expectedFailureSignature`、`timeoutSeconds` 和 `partialWriteCheck`。无匹配搜索、签名正确的 TDD Red 等预期 nonzero 不停止；只有契约外失败、timeout 或未知部分写入才停止。历史 pending `executionPlan` 仅用于恢复兼容，不再生成；
   - 删除被更强证据覆盖的重复执行，不能降低原验收标准；
   - 将独立剩余工作拆给新 Worker，并收窄原 Worker；新发现且不属于原范围的工作必须请求用户授权；
   - 一次有界重规划后仍无有效进展时，保护并回收成果，再按终止与取代流程替换 Worker。
4. 写入型 Worker 有未提交唯一成果时，不得创建第二个写入者接管同一边界。先完成用户已授权的 checkpoint、Handoff 或其他持久化，再从稳定基线拆分。
5. 向用户报告触发原因、已完成成果、处理决定、并发变化、下一检查点和残余风险。

`CHECKPOINT/REPLAN` 只能调整原范围内的执行形状，不得扩大权限、放宽验收、自动提交或制造无意义并发。只有经过一次有界重规划后仍无法产生有效进展，或确实等待外部决定时，才把健康度记为 `STALLED` 并进入 `BLOCKED`。

## 7. 处理阻塞

收到 `BLOCKED` 或发现等待用户/外部条件时：

1. 先按 Worker 证据分类：步骤契约接受的结果是 `EXPECTED_RESULT`；引号、路径、参数、解析器、命令形状、已知 wrapper 或已证明无部分写入的 patch 错误是 `RECOVERABLE_CONTROL`；标题、cursor、任务 `create/list/read/wait/send/title/handoff-status` timeout、renderer、临时 JSON 或消息传输失败是 `CONTROL_DEGRADED`；只有需要决定/权限/依赖/边界变化、存在不可逆风险、实际工作步骤 timeout、未知部分写入或纠错预算耗尽才是 `WORK_BLOCKER`。
2. Worker 把前三类误报为 `BLOCKED` 时，保留原始消息用于审计，但以 `blockerDisposition=RECOVERABLE` 落账并保持 `RUNNING`；只有 `WORK_BLOCKER` 使用 `blockerDisposition=BLOCK`、进入生命周期 `BLOCKED` 并改为 `⌛️`。
3. 对真实工作阻塞，先落账状态和证据，再使用 dispatch script 渲染匹配 `runLanguage` 的 `BLOCKED` 标题，按 intent/tool/outcome 改名。
4. 判断是否能在原始授权内安全决定。能决定时，记录决定；先写消息 intent 取得 `controllerSeq`，再渲染并发送 `DECISION`，Worker 恢复后改回 `✍️`。
5. 需要用户决定时：
   - 使用 dispatch script 渲染匹配 `runLanguage` 的 `WAITING_FOR_USER` 标题并按外部操作流程更新；
   - 立即用 `runLanguage` 报告事实、选项、建议和不决策的影响；
   - 暂停受影响路径，继续不相关且安全的工作。
6. 不因阻塞自动扩大范围、权限或外部影响。

### 7.1 控制面故障不得冒充工作阻塞

- 标题、cursor、等待快照、renderer 输入、临时 JSON、命令引号或任务消息传输属于控制面；它们失败时保留 Worker 的真实生命周期。
- 对确认无外部副作用的控制错误，在本地纠错预算内修正一次；任务服务失败按无限退避和 heartbeat 规则处理。未恢复时始终保持运行 `DEGRADED`，继续不依赖新决定的安全工作，不把 Worker 或 Goal 改成 `BLOCKED`，不输出 final，也不把“恢复任务控制面连接”伪装成需要用户批准的决定。
- 账本持久化失败时停止新的外部副作用并进入运行级 `DEGRADED` 恢复；它是控制系统故障，不是 Worker 工作阻塞。

## 8. 终止、废弃与取代

用户取消任务、目标失去价值，或原 Worker 无法安全恢复且已决定使用替代 Worker 时：

1. Worker 可寻址时先发送 `STOP`，要求停止受影响工作并报告可恢复成果；不可寻址时用已有证据完成主控侧审计。
2. 只有以下归档就绪条件全部满足后，才进入 `RETIRED`：
   - 不再需要该 Worker 继续执行或等待；
   - 替代任务存在时已经记录 `replacementWorkerId`；
   - 有价值成果已经回收，或者主控在原始授权内明确记录不再采用；
   - 不存在仍需恢复的唯一未提交成果；否则保持 `⌛️`；
   - 账本已记录 `terminalReason` 和 `archiveReady=true`。
3. 先用账本状态事件通过终态门，再渲染匹配 `runLanguage` 的 `RETIRED` 标题，将任务改为 `🗑️` 并释放槽位。
4. 立即向用户说明终止原因、替代 Worker、成果处置和残余风险。
5. `🗑️` 表示逻辑终止且可人工归档，不代表自动归档、删除任务、删除分支或清理 worktree。

## 9. 验收与代码回收

收到 `DONE` 或观察到 Worker 结束后：

1. 立即在账本进入 `REVIEW`，再使用 dispatch script 渲染匹配 `runLanguage` 的 `REVIEW` 标题并按外部操作流程改为 `🔍`；Worker 的 `DONE` 只是完成声明，不是验收结果。
2. 读取 Worker 的最终结果、文件清单、命令和验证证据。
3. 对照 Worker Prompt 的验收条件独立核对。
4. 验收不通过时恢复 `RUNNING` 和 `✍️`，发送 `REVISION`，并要求在原范围内修订；需要外部决定时进入 `BLOCKED`。
5. 只读成果通过后直接纳入主控汇总。
6. 写入型成果通过后：
   - 确认 Worker 已停止写入；
   - 按依赖顺序一次只 Handoff 一个 Worker；
   - 跨主机时先转移到本地匹配项目 worktree，并更新账本中的 `hostId`；
   - 再回收到 Local；
   - 在 Local 上运行组合验证。
7. 只有 Handoff 已取得明确失败结果，或组合验证发现真实冲突时，才进入 `BLOCKED` 并改为 `⌛️`；Handoff/status timeout 或结果含糊按 5.1 保持 `UNKNOWN + DEGRADED` 无限核对。始终保留 worktree，不做破坏性 Git 清理，不让 Worker 无边界地修改 Local。
8. 只有以下归档就绪条件全部满足后，才进入 `ACCEPTED`：
   - 原 Worker Prompt 范围已经完成，且不存在待决策事项；
   - 主控验收和必要的 Local 组合验证通过；
   - 需要合入或 Handoff 的成果已经按原始范围完成整合；
   - 原范围本来就不要求合入时，已确认报告、审计、设计或候选包等交付物可访问；
   - 不存在仅滞留在临时 worktree 中、仍需回收的必要成果；
   - 账本已记录 `terminalReason` 和 `archiveReady=true`。
9. 先用账本状态事件通过终态门，再渲染匹配 `runLanguage` 的 `ACCEPTED` 标题改为 `✅`，释放槽位并从 `ready` 结果派发下一波，但保留该任务，不自动归档。

不要使用 `📋` 区分报告、审计、设计或“无合入物”任务。图标表达生命周期，而不是交付物类型；此类任务通过验收后同样使用 `✅`，可在标题后缀说明“无合入物”。

“允许写代码”不自动授权 commit、push、PR 或发布。若 Handoff 不可用，先取得用户认可的持久化方式；不得让重要成果只存在于可能被产品清理的临时 worktree 而不告知用户。

## 10. 完成运行

全部 Worker 已验收并完成总体组合验证后：

1. 完成当前 cycle 并记录 `RUN_COMPLETED`。
2. 使用 dispatch script 渲染匹配 `runLanguage` 的 `COMPLETE` 标题，按 intent/tool/outcome 更新主控标题。
3. 完成 ledger `verify`，确认没有 pending operation，再创建一致 SQLite backup。
4. 用 `runLanguage` 向用户汇报总体结果、Worker 状态、关键决定、代码回收、验证证据和残余风险。
5. 保留所有 `✅`、`🗑️`、`⌛️` 和主控任务。
6. 不调用任何归档工具。`✅` 和 `🗑️` 明确表示用户可以直接人工归档；用户明确要求由 Codex 归档时，把它视为本次自动编排之外的独立、精确目标操作。

## 11. 运行中调整并发

- 升高上限：更新账本和当前 manifest 的 `maxActiveWorkers`，重新编译并激活，从已规划的就绪队列补充派发，不重复或重新拆分运行中的任务。
- 降低上限：更新账本和 manifest 后停止新派发，让现有 Worker 自然完成；除非用户明确要求停止具体 Worker，否则不自动中断。
- 调到 8 以上：重建最多 8 目标的监控分组。
- 无法满足用户值时：报告 `requested`、可执行 `effective` 和原因，不静默替换。
- 每次调整都向用户报告旧值、新值、活跃数和排队数。

## 12. 恢复运行

主控上下文丢失或任务恢复时：

1. 从稳定运行目录找到 ledger；先执行一次 `snapshot`，在同一只读事务中取得 verification、边界化 status 和详细 pending operations，并恢复 `runId`、`runLanguage`、Controller、manifest、Worker、cursor、消息序号和 operation。只有专项诊断才分别调用 `verify/status/pending`。
2. 使用任务列表、即时等待快照和必要的紧凑读取收集外部观察事实，再用 ledger `audit` 比较；标题不能覆盖账本。
3. 先核对所有 `INTENT/UNKNOWN` 外部操作，写入真实 outcome；没有证明失败前不重试创建、消息、改名或 Handoff。
4. 需要时用 ledger `manifest` 导出当前编译清单，并用 dispatch `ready` 重新计算，不从压缩后的记忆重建队列。
5. 只应用更大的 Worker `seq`；新主控命令继续账本分配的 `controllerSeq`。
6. 账本损坏或丢失时按 ledger reference 的备份或保守重建流程执行；无法唯一核对时阻塞并向用户报告。
7. 不因恢复失败创建重复 Worker，也不把内存状态冒充持久账本。
8. 若存在本次运行的恢复 heartbeat，先更新而非重复创建；控制面恢复、运行完成、用户暂停或取消后停用/删除它，避免无人值守的残留唤醒。

始终把 SQLite 账本作为逻辑状态、标题作为用户可见投影。账本写入失败时停止新的外部副作用并进入 `DEGRADED` 无限恢复；标题更新失败时纳入同一无限退避循环，但不要丢弃 Worker 成果、重复含糊写操作或违反禁止归档规则。
