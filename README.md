# Codex Task Orchestrator

`orchestrate-codex-tasks` 是一个 Codex Skill，用于把当前任务作为主控（Controller），创建多个独立 Codex Worker 任务并通过跨任务消息协作。

Worker 是独立 Codex 任务，不是 Codex 子 Agent。

## 核心行为

- 主控标题以 `👑` 开头。
- Worker 运行中以 `✍️` 开头。
- Worker 阻塞或等待确认时以 `⌛️` 开头。
- Worker 经主控验收完成后以 `✅` 开头。
- 默认最多 8 个活跃 Worker，用户可以在运行前或运行中调整。
- 写代码 Worker 默认使用独立 worktree。
- 支持远程 `hostId`，但默认优先本地主机。
- Worker 遇到决策、澄清或阻塞时必须通知主控并暂停受影响工作。
- Worker 完成时必须先把结果和验证证据通知主控。
- Skill 永不自动归档任务。

## 安装

上传到 GitHub 后，在 Codex 中发送：

```text
请使用 $skill-installer，从下面的 GitHub 地址安装这个 Skill：
https://github.com/BillSJC/orchestrate-codex-tasks/tree/master/.agents/skills/orchestrate-codex-tasks
```

如果安装后没有立即出现，重启 Codex 后再检查。

### 仓库级使用

也可以把以下目录复制到目标仓库：

```text
.agents/skills/orchestrate-codex-tasks/
```

目标路径保持为：

```text
<target-repo>/.agents/skills/orchestrate-codex-tasks/
```

### 用户级手动安装

把 Skill 目录复制或链接到：

```text
$HOME/.agents/skills/orchestrate-codex-tasks/
```

私有 GitHub 仓库应使用机器已有的 GitHub 凭据。不要把访问令牌粘贴到普通 Codex Prompt 中。

## 使用

确定性触发：

```text
使用 $orchestrate-codex-tasks，把下面工作拆成独立 Codex Worker 任务并发执行，由当前任务统一协调、验收和汇总：……
```

也可以明确使用自然语言：

```text
创建几个独立 Codex 任务并行完成这些工作，使用主控 + Worker 模式，由当前任务通过跨任务消息统一协调。
```

“帮我快一点”“能不能并行”等弱信号不会直接创建新任务。

调整并发度：

```text
使用 $orchestrate-codex-tasks 执行下面任务，最大活跃 Worker 设置为 4：……
```

运行中也可以要求主控提高或降低并发上限。降低上限不会自动中断已经运行的 Worker。

## 文件

- [详细设计](DESIGN.md)
- [Skill 主流程](.agents/skills/orchestrate-codex-tasks/SKILL.md)
- [任务工具契约](.agents/skills/orchestrate-codex-tasks/references/tool-contracts.md)
- [Controller/Worker 协议](.agents/skills/orchestrate-codex-tasks/references/protocol.md)

## 运行要求

该 Skill 需要当前 Codex 表面提供独立任务创建、任务改名、跨任务消息、任务监控与读取能力。写代码编排还需要 worktree 和 Handoff 能力，或由用户明确认可其他成果回收方式。

如果预检发现关键能力不可用，Skill 会停止创建 Worker 并向用户说明，而不会假装已经进入独立任务编排模式。
