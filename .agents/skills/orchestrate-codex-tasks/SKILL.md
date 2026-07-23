---
name: orchestrate-codex-tasks
description: Coordinate the current Codex task as a Controller with multiple independent Codex Worker tasks/threads through task creation, cross-task messages, status-title updates, monitoring, worktree isolation, and result synthesis. Run all visible coordination in English or Chinese to match the user's orchestration request. Use only when the user explicitly invokes this skill to execute work or clearly asks for separate, independent, or background Codex tasks, a Controller + Worker workflow, cross-task coordination, “主控 + Worker”, “独立任务并发”, or “跨任务并发”. Do not use for Codex subagents, generic requests to work faster, vague parallelism, shell/program concurrency, simple tightly coupled work, or requests that do not authorize creating new Codex tasks.
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
- 永不自动归档主控或 Worker。完成 Worker 只改名为 `✅` 并保留。
- 不用调度流程绕过审批、沙箱、凭据、外部写入或其他权限边界。
- 不自动提交、推送、开 PR、发布或删除，除非用户的原始请求明确包含相应动作。
- 保留单一决策中心：Worker 负责执行和提供证据，主控负责范围、决策、冲突处理、验收和最终汇总。

## 读取运行协议

在创建第一个 Worker 前：

1. 完整读取 [references/tool-contracts.md](references/tool-contracts.md)，确认当前任务工具、worktree、远程主机和 Handoff 契约。
2. 完整读取 [references/protocol.md](references/protocol.md)，使用其中的语言规则、消息格式、标题状态机和运行账本。
3. `runLanguage=en` 时完整读取 [references/worker-prompt.en.md](references/worker-prompt.en.md)；`runLanguage=zh-CN` 时完整读取 [references/worker-prompt.zh-CN.md](references/worker-prompt.zh-CN.md)。只加载并派发匹配语言的模板。
4. 如果当前运行时工具契约与 reference 不同，以当前可调用工具的 schema 为准，并用 `runLanguage` 向用户说明会影响行为的差异。

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
2. 生成短且本次唯一的 `runId`，例如 `R7K2`。
3. 立即使用 protocol reference 中匹配 `runLanguage` 的 `PLANNING` 模板给当前任务改名。

4. 获取并验证当前主控的 `threadId`；跨主机时同时获取 `hostId`。优先使用运行时直接提供的地址，否则用唯一 `runId` 标题从任务列表解析。不能得到唯一匹配时，停止并向用户报告。
5. 解析并发上限：
   - 默认 `maxActiveWorkers = 8`。
   - 接受用户明确给出的正整数覆盖值。
   - 并发值只是上限；不要为了填满槽位制造低价值 Worker。
6. 把工作拆成轻量 DAG。每个 Worker 必须具备单一目标、明确输入、独立验收条件、文件所有权和已知依赖。
7. 把高度耦合实现、共享接口定稿、用户偏好、风险接受和最终整合留给主控。
8. 使用项目列表选择执行位置：
   - 用户明确指定的主机优先于默认策略；
   - 否则优先本地同项目；
   - 仅在本地缺少必要项目、依赖或能力时选择明确匹配的远程项目。

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

1. 生成稳定 `workerId`，例如 `W1`。
2. 使用 protocol reference 中的完整 Worker Prompt，直接注入：
   - `runId`、`workerId`；
   - 主控 `threadId` 和需要时的 `hostId`；
   - 目标、范围、输入、禁止事项；
   - 主机、环境和 worktree 起始状态；
   - 文件写入边界、成果回收方式和验收命令。
3. 创建独立任务；除非用户明确指定，否则不覆盖 Worker 的模型或 reasoning 配置。
4. 记录返回的 `threadId` 或 `clientThreadId`、`hostId`、环境和状态。
5. 获得真实 `threadId` 后，立即使用匹配 `runLanguage` 的 `RUNNING` Worker 标题模板改名。
6. 首波派发完成后，使用匹配 `runLanguage` 的 `TRACKING` 模板更新主控标题。
7. 立即用 `runLanguage` 向用户报告 Worker 名称、目标、当前活跃数、排队数、并发上限、worktree 起始状态和任何远程主机选择。

worktree 只返回 `clientThreadId` 时，将 Worker 记为 `PROVISIONING`。在解析到真实 `threadId` 前，不对临时 ID 调用等待、改名或跨任务消息工具，也不重复创建相同 Worker。

## 5. 监控与进度汇报

同时使用两个通道：

- 推送：处理 Worker 发来的 `ACCEPTED`、`PROGRESS`、`BLOCKED` 和 `DONE`。
- 拉取：持续使用任务等待工具和 cursor 增量观察；状态含糊或需要证据时读取任务。

规则：

- 默认最多 8 个活跃 Worker，放入一个最多 8 目标的等待集合。
- 用户把并发调到 8 以上时，按稳定顺序分成每组最多 8 个目标；每轮先对所有组取即时增量快照，再对一个轮转组做不超过约 15 秒的等待。
- 保证每个活跃 Worker 至少每 60 秒被主动观察一次。
- 派发、阻塞、验收完成、重试、范围变化和替换发生时立即向用户汇报。
- 没有状态变化时，最长约每 60 秒发送一次有信息量的简短心跳。
- 不把 Worker 的自称完成直接视为验收通过。

把 `PROVISIONING`、`RUNNING`、`BLOCKED` 和 `VALIDATING` 都计入活跃数。`DONE` 或 `STOPPED` 才释放槽位。

## 6. 处理阻塞

收到 `BLOCKED` 或发现等待用户/外部条件时：

1. 立即使用匹配 `runLanguage` 的 `BLOCKED` Worker 标题模板改名。

2. 判断是否能在原始授权内安全决定。
3. 能决定时，记录决定并用 `DECISION` 消息回复；Worker 恢复后改回 `✍️`。
4. 需要用户决定时：
   - 使用匹配 `runLanguage` 的 `WAITING_FOR_USER` 模板更新主控标题；
   - 立即用 `runLanguage` 报告事实、选项、建议和不决策的影响；
   - 暂停受影响路径，继续不相关且安全的工作。
5. 不因阻塞自动扩大范围、权限或外部影响。

## 7. 验收与代码回收

收到 `DONE` 或观察到 Worker 结束后：

1. 读取 Worker 的最终结果、文件清单、命令和验证证据。
2. 对照 Worker Prompt 的验收条件独立核对。
3. 验收不通过时保持 `✍️`，发送 `REVISION`，并要求在原范围内修订。
4. 只读成果通过后直接纳入主控汇总。
5. 写入型成果通过后：
   - 确认 Worker 已停止写入；
   - 按依赖顺序一次只 Handoff 一个 Worker；
   - 跨主机时先转移到本地匹配项目 worktree，并更新账本中的 `hostId`；
   - 再回收到 Local；
   - 在 Local 上运行组合验证。
6. Handoff 或组合验证冲突时改为 `⌛️`，保留 worktree，不做破坏性 Git 清理，不让 Worker 无边界地修改 Local。
7. 只有主控验收和必要的 Local 组合验证都通过后，才使用匹配 `runLanguage` 的 `DONE` Worker 标题模板改名。

8. 标记 `✅` 后释放槽位并派发下一波，但保留该任务，不归档。

“允许写代码”不自动授权 commit、push、PR 或发布。若 Handoff 不可用，先取得用户认可的持久化方式；不得让重要成果只存在于可能被产品清理的临时 worktree 而不告知用户。

## 8. 完成运行

全部 Worker 已验收并完成总体组合验证后：

1. 使用匹配 `runLanguage` 的 `COMPLETE` 模板更新主控标题。
2. 用 `runLanguage` 向用户汇报总体结果、Worker 状态、关键决定、代码回收、验证证据和残余风险。
3. 保留所有 `✅`、`⌛️` 和主控任务。
4. 不调用任何归档工具。用户未来明确提出归档时，把它视为本 Skill 之外的独立操作。

## 9. 运行中调整并发

- 升高上限：更新账本，从已规划的就绪队列补充派发，不重复或重新拆分运行中的任务。
- 降低上限：停止新派发，让现有 Worker 自然完成；除非用户明确要求停止具体 Worker，否则不自动中断。
- 调到 8 以上：重建最多 8 目标的监控分组。
- 无法满足用户值时：报告 `requested`、可执行 `effective` 和原因，不静默替换。
- 每次调整都向用户报告旧值、新值、活跃数和排队数。

## 10. 恢复运行

主控上下文丢失或任务恢复时：

1. 使用 `runId` 搜索任务标题。
2. 用任务列表重建 Worker 集合。
3. 从运行账本恢复 `runLanguage`；账本不可用时，从主控标题和最近一条实质性用户请求恢复。
4. 用任务读取工具恢复近期状态和证据。
5. 按 `runId + workerId + seq` 去重消息。
6. 不因恢复失败创建重复 Worker。

始终把账本作为逻辑状态、标题作为用户可见投影。标题更新失败时有限重试并报告，但不要丢弃 Worker 成果或违反禁止归档规则。
