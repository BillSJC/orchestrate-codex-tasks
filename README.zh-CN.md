[English](README.md) | **中文**

# Codex Task Orchestrator

`orchestrate-codex-tasks` 是一个 Codex Skill。它把当前任务作为唯一主控（Controller），创建多个可在任务列表中独立存在的 Worker 任务，并通过跨任务消息完成派发、阻塞处理、验收和结果汇总。

> Worker 是独立 Codex 任务，不是 Codex 子 Agent。

## 1. 使用效果

启动后，Codex 任务列表会形成一个可见的主控与 Worker 集合，例如：

```text
👑 [R7K2] 跟进 5 个 Worker｜完成发布准备
├── ✍️ [R7K2-W1] 实现发布脚本
├── 🔍 [R7K2-W2] 核对更新说明｜等待主控验收
├── ⌛️ [R7K2-W3] 测试部署｜等待用户确认目标环境
├── ✅ [R7K2-W4] 审核安全边界｜已验收，无合入物
└── 🗑️ [R7K2-W5] 复审旧候选｜已由 W6 取代
```

| 标记 | 含义 |
|---|---|
| `👑` | 当前任务是唯一主控，负责决策、监控、验收和汇总 |
| `✍️` | Worker 正在执行或修订 |
| `🔍` | Worker 已发送 `DONE`，主控正在验收或整合 |
| `⌛️` | Worker 因决策、澄清、权限、依赖或故障而阻塞 |
| `✅` | Worker 已完成 Prompt 范围并通过归档就绪门，可以直接人工归档 |
| `🗑️` | Worker 已取消、废弃、失效或被取代，并通过归档就绪门，可以直接人工归档 |

图标表示生命周期，不表示交付物类型。通过验收的报告、审计、设计、DAG 或候选包即使没有代码或合入物，也使用 `✅`；Skill 不使用 `📋` 作为状态。

用户可以预期看到：

- 派发后立即收到 Worker 名称、目标、活跃数、排队数、并发上限和运行位置。
- Worker 在接受任务、取得实质进展、发生阻塞和完成时通知主控。
- 主控持续观察全部活跃 Worker，并及时汇报关键变化。
- 需要用户决定时，受影响的 Worker 暂停，主控给出事实、选项和建议。
- 主控区分“消息频繁”和“有效里程碑关闭”；Worker 过重、重复或过慢时，先请求安全 checkpoint。
- 主控可以在不降低验收标准的前提下批量授权已核对的测试 manifest、删除冗余执行，或拆分独立剩余工作。
- Worker 发送 `DONE` 后先显示 `🔍`；只有主控独立验收并完成成果回收后才标记为 `✅`。
- 只有在有价值成果已经回收或明确不再采用、替代 Worker 已记录后，才标记为 `🗑️`。
- 写代码的 Worker 默认在相互隔离的 worktree 中工作，减少并发修改冲突。
- `✅` 和 `🗑️` 任务继续保留且永不自动归档；两种标记都表示用户可以直接人工归档。

健康度独立于生命周期图标。效率审查时，主控可以使用 `👑 [R7K2] 重规划｜...`；Worker 仍在推进时保持 `✍️ ...｜效率审查`，暂停等待新计划时使用 `⌛️ ...｜等待重规划`。时间阈值只触发审查，绝不会自动停止或废弃 Worker。

## 2. 安装与更新

### 推荐：让 Codex 自动安装

在任意 Codex 任务中发送：

```text
请使用 $skill-installer，从下面的 GitHub 地址安装这个 Skill：
https://github.com/BillSJC/orchestrate-codex-tasks/tree/master/.agents/skills/orchestrate-codex-tasks
```

安装完成后，在新的任务中使用 `$orchestrate-codex-tasks`。如果 Skill 没有立即出现，重启 Codex 后再检查。

可以用下面的 Prompt 做一次无副作用确认：

```text
请确认你已加载 $orchestrate-codex-tasks，并概述它的触发条件；不要创建 Worker。
```

### 仅在某个仓库中使用

把本仓库中的 Skill 目录复制到目标仓库的相同位置：

```text
<target-repo>/.agents/skills/orchestrate-codex-tasks/
```

Codex 从该仓库或其子目录启动时即可发现它。

### 用户级手动安装

也可以把 Skill 目录复制或链接到：

```text
$HOME/.agents/skills/orchestrate-codex-tasks/
```

私有 GitHub 仓库应使用机器上已有的 GitHub 凭据。不要把访问令牌或私钥粘贴到普通 Codex Prompt 中。

### 更新现有安装

仓库级安装只需更新包含 `.agents/skills/orchestrate-codex-tasks` 的仓库 checkout。Codex 会自动检测 Skill 文件变化；如果新说明没有出现，重启 Codex。

通过 `$skill-installer` 安装的用户级副本不能直接覆盖更新：目标目录已存在时，安装器会停止。先把旧副本移动到所有 Skill 扫描目录之外的备份位置，再从 GitHub URL 重新安装。内置安装器使用 `$CODEX_HOME/skills`（通常是 `$HOME/.codex/skills`）；手动创建的用户级副本也可能位于 `$HOME/.agents/skills`。

可以让 Codex 安全替换：

```text
请安全更新用户级 $orchestrate-codex-tasks 安装。
把现有副本移动到所有 Skill 扫描目录之外的备份位置，不要删除。
然后使用 $skill-installer 从下面的地址重新安装：
https://github.com/BillSJC/orchestrate-codex-tasks/tree/master/.agents/skills/orchestrate-codex-tasks
确认没有旧的同名用户级副本仍可被发现。
不要创建任何 Worker。
```

不要把包含相同 `SKILL.md` 的备份留在 `.agents/skills`、`$HOME/.agents/skills` 或 `$CODEX_HOME/skills` 内。Codex 不会合并同名 Skill；重复副本可能暴露不同版本。使用新任务验证更新后，只有在不再需要回滚时才删除扫描路径之外的备份。

## 3. 何时使用，如何使用

### 适合使用

- 一项工作能拆成两个或更多边界清晰、可以独立验收的子任务。
- 希望多个 Codex 任务在后台并行推进，并在任务列表中分别查看或继续交流。
- 希望由一个主控统一处理范围、依赖、决策、冲突和最终验收。
- 多个 Worker 需要分别研究资料、分析不同模块，或在独立 worktree 中编写代码。
- 需要跨本地或远程主机调度，同时让结果回到当前主控任务。

### 不适合使用

- 单一、短小或必须严格串行完成的工作。
- 子任务高度耦合，多个执行者会频繁修改同一接口或同一批文件。
- 只是希望普通程序、测试或 shell 命令并发运行。
- 用户明确要求的是 Codex 子 Agent，而不是独立任务。
- 只有“快一点”“能不能并行”等模糊信号，尚未明确授权创建新任务。

### 确定性触发

最可靠的方式是直接点名 Skill：

```text
使用 $orchestrate-codex-tasks，把下面工作拆成独立 Codex Worker 任务并发执行，
由当前任务统一协调、验收和汇总：……
```

也可以使用明确的自然语言：

```text
创建几个独立 Codex 任务并行完成这些工作。
使用主控 + Worker 模式，由当前任务通过跨任务消息统一协调。
```

### 设置并发度

默认最多有 8 个活跃 Worker，但不会为了填满 8 个槽位而制造无意义任务。可以在开始时指定其他上限：

```text
使用 $orchestrate-codex-tasks 执行下面任务，最大活跃 Worker 设置为 4：……
```

运行中也可以要求主控调整上限：

```text
把最大活跃 Worker 从 8 调整为 3。
```

降低上限不会自动中断已经运行的 Worker，只会暂停新的派发。

### 指定代码基线

写代码的 Worker 默认从项目默认分支创建独立 worktree。如果任务必须看到当前未提交修改，需要明确说明：

```text
Worker 必须以当前 working tree 为起点，而不是项目默认分支。
```

### 会话语言

Skill 会根据触发编排的用户请求选择协调语言：

- 中文请求：主控、Worker 标题、进度、阻塞和汇总使用中文。
- 英文请求：整个协调流程使用英文。
- 代码、路径、引用内容和交付物目标语言不参与判断。
- 交付物可以使用不同语言，不会改变协调语言。
- 运行中只有用户明确要求时才切换协调语言。

例如，用中文要求生成英文 README 时，主控与 Worker 仍使用中文沟通，README 使用英文。

## 4. 使用原理

```mermaid
flowchart TD
    U["用户"] --> C["👑 主控任务"]
    C --> P["拆解目标、依赖与文件边界"]
    P --> W1["✍️ Worker 1"]
    P --> W2["✍️ Worker 2"]
    P --> W3["✍️ Worker N"]
    W1 -->|"ACCEPTED / PROGRESS / DONE"| C
    W2 -->|"BLOCKED"| C
    W3 -->|"ACCEPTED / PROGRESS / DONE"| C
    W1 -->|"里程碑不再关闭"| H["主控效率审查"]
    H -->|"CHECKPOINT / REPLAN"| W1
    C -->|"需要用户决策"| U
    C --> V["🔍 主控验收与成果整合"]
    C -->|"STOP / 取代"| X["🗑️ 已废弃 Worker"]
    V --> R["组合验证与最终汇总"]
```

核心流程如下：

1. **授权判断**：只有显式调用 Skill，或用户明确要求“独立任务并发”“主控 + Worker”等模式时，才允许创建 Worker。
2. **语言与寻址**：主控确定 `runLanguage`，生成运行标识，并取得自己的 `threadId`；跨主机时同时记录 `hostId`。
3. **拆解与派发**：主控将工作整理成带依赖关系的子任务，选择匹配语言的 Worker Prompt，并写入范围、禁止事项、文件边界和验收条件。
4. **双向协调**：Worker 使用 `ACCEPTED`、`PROGRESS`、`BLOCKED` 和 `DONE` 消息主动回报；主控同时通过任务等待和读取能力主动观察。
5. **健康与重规划**：主控跟踪已关闭验收项、范围增长、逐步放行往返、timeout 和一对一长尾。触发软阈值时先 `CHECKPOINT`，必要时执行有界 `REPLAN`。
6. **阻塞决策**：Worker 不自行猜测产品、架构、权限或风险决策，而是暂停受影响工作并把选项发回主控。
7. **验收与回收**：主控核对交付物和测试证据。代码成果按依赖顺序回收到本地，并完成组合验证。
8. **终态但不自动归档**：通过验收的 Worker 标记为 `✅`，取消或被取代的 Worker 标记为 `🗑️`。两者通过归档就绪门后都可以人工归档，但 Skill 继续保留任务。

## 5. 安全边界与默认策略

| 项目 | 默认行为 |
|---|---|
| Worker 类型 | 独立 Codex 任务，不是子 Agent |
| 协调语言 | 匹配触发编排的用户请求，支持英文和中文 |
| 决策中心 | 只有主控可以处理范围、冲突和最终验收 |
| 最大活跃 Worker | 默认 8，用户可以在运行前或运行中调整 |
| 代码写入 | 优先使用独立 worktree，并声明文件边界 |
| 主机选择 | 本地主机优先，也支持明确的远程 `hostId` |
| 提交与发布 | 不自动 commit、push、开 PR 或发布，除非原请求明确授权 |
| 权限 | 不绕过审批、沙箱、凭据或外部写入限制 |
| Worker 扩散 | Worker 不得创建子 Agent、其他任务或新的 Worker |
| 效率审查 | 软阈值只请求安全 checkpoint，不自动停止 Worker，也不降低验证标准 |
| 重规划 | 可在原授权内批量放行安全步骤或拆分独立工作；另一个写入者接管前必须先保护脏 worktree 成果 |
| 完成标准 | Worker 的 `DONE` 先进入 `🔍`；通过主控验收和成果回收后才能标记 `✅` |
| 废弃标准 | 取消或被取代的任务只有在成果处置和替代关系记录后才能标记 `🗑️` |
| 自动归档 | 严禁；`✅` 和 `🗑️` 都是可人工归档的终态 |

## 6. 运行要求与兼容性

当前 Codex 表面需要提供：

- 创建、列出、读取、等待和改名独立任务的能力。
- 向其他任务发送消息的能力。
- 项目与执行主机发现能力。
- 对于代码任务，还需要 worktree 和 Handoff，或由用户明确认可其他成果回收方式。

如果预检发现关键能力不可用，Skill 会停止创建 Worker 并说明缺失项，不会假装已经进入编排模式。

运行时暴露的工具名称和参数可能随 Codex 版本变化。Skill 会优先使用当前实际工具 schema，仓库中的工具契约用于说明预期能力和降级边界。

## 7. 项目结构与进一步阅读

```text
.agents/
└── skills/
    └── orchestrate-codex-tasks/
        ├── SKILL.md
        ├── agents/
        │   └── openai.yaml
        └── references/
            ├── protocol.md
            ├── tool-contracts.md
            ├── worker-prompt.en.md
            └── worker-prompt.zh-CN.md
```

- [详细设计](DESIGN.md)
- [Skill 主流程](.agents/skills/orchestrate-codex-tasks/SKILL.md)
- [Controller/Worker 协议](.agents/skills/orchestrate-codex-tasks/references/protocol.md)
- [独立任务工具契约](.agents/skills/orchestrate-codex-tasks/references/tool-contracts.md)
