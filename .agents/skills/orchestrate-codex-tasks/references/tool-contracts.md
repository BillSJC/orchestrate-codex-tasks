# Codex 独立任务工具契约

## 目录

1. 契约层级
2. 工具发现与预检
3. 创建独立任务
4. 标题和跨任务消息
5. 监控与读取
6. worktree 与起始状态
7. Handoff 与成果回收
8. 本地和远程主机
9. 失败边界
10. 官方依据

## 1. 契约层级

优先使用当前 Codex 运行时暴露的高层任务工具，不要在 Skill 中直接拼装 App Server JSON-RPC。

当前高层工具名是运行时契约，不是面向外部开发者保证永久不变的公共 API。每次运行都先查看实际 schema；字段冲突时以实际 schema 为准。

底层概念映射：

| 目的 | App Server 原语 | 当前高层工具 |
|---|---|---|
| 创建任务 | `thread/start` | `create_thread` |
| 设置标题 | `thread/name/set` | `set_thread_title` |
| 添加一轮 | `turn/start` | `send_message_to_thread` |
| 列出任务 | `thread/list` | `list_threads` |
| 读取任务 | `thread/read` | `read_thread` |
| 等待任务 | 线程/轮次事件 | `wait_threads` |
| 项目发现 | 客户端项目配置 | `list_projects` |
| 转移任务与代码 | 客户端 Git/worktree 流程 | `handoff_thread` |
| 查询转移 | 客户端操作状态 | `get_handoff_status` |

## 2. 工具发现与预检

需要时使用工具搜索发现：

- `create_thread`
- `set_thread_title`
- `send_message_to_thread`
- `list_threads`
- `read_thread`
- `wait_threads`
- `list_projects`
- 写入型任务需要的 `handoff_thread`
- `get_handoff_status`

缺少创建、改名、跨任务消息、等待或读取任一核心能力时，不创建无法满足协议的 Worker。写入型任务缺少 Handoff 时，在派发前确定用户认可的替代成果回收方式。

归档能力不属于自动编排工具集合。即使运行时暴露归档工具，也不得自动调用；`✅` 和 `🗑️` 仅表示任务通过归档就绪门、用户可以人工归档。用户明确要求由 Codex 归档时，应作为自动编排之外的独立、精确目标操作处理。

### 2.1 解析主控地址

优先使用当前 Codex 运行时直接提供的调用方 `threadId` 和 `hostId`。

如果运行时没有直接提供：

1. 生成本次唯一 `runId`。
2. 先按 protocol reference 确定 `runLanguage`，再给当前任务设置包含该 `runId` 的本地化 `👑` 标题；给当前任务改名时省略 `threadId`。
3. 使用 `list_threads` 搜索该 `runId`。
4. 只接受与当前项目、标题和运行状态一致的唯一匹配。
5. 从匹配结果记录主控 `threadId` 和 `hostId`。

零个或多个匹配都视为无法可靠寻址。此时不要创建 Worker，因为 Worker 无法保证把阻塞和完成状态发送回正确主控。

## 3. 创建独立任务

先列出项目，再使用返回的真实 `projectId`。

### 3.1 本地只读项目 Worker

```text
create_thread({
  "prompt": "<匹配 runLanguage 的完整 Worker Prompt>",
  "target": {
    "type": "project",
    "projectId": "<PROJECT_ID>",
    "environment": {
      "type": "local"
    }
  }
})
```

### 3.2 默认写入型 Worker

```text
create_thread({
  "prompt": "<匹配 runLanguage 的完整 Worker Prompt>",
  "target": {
    "type": "project",
    "projectId": "<PROJECT_ID>",
    "environment": {
      "type": "worktree"
    }
  }
})
```

### 3.3 projectless Worker

```text
create_thread({
  "prompt": "<匹配 runLanguage 的完整 Worker Prompt>",
  "target": {
    "type": "projectless",
    "directoryName": "<可选目录名>"
  }
})
```

除非用户明确指定模型或 reasoning effort，否则省略 `model` 和 `thinking`，继承用户配置。

直接创建通常返回 `threadId` 和 `hostId`。排队创建 worktree 时可能先返回 `clientThreadId`；它不是可等待或可改名的真实任务 ID。

## 4. 标题和跨任务消息

当前主控改名时省略 `threadId`：

```text
set_thread_title({
  "title": "👑 [R7K2] <匹配 runLanguage 的 PLANNING 标题>"
})
```

Worker 改名时使用真实 `threadId`：

```text
set_thread_title({
  "threadId": "<WORKER_THREAD_ID>",
  "title": "✍️ [R7K2-W1] <匹配 runLanguage 的动作短语>"
})
```

向 Worker 发消息：

```text
send_message_to_thread({
  "threadId": "<WORKER_THREAD_ID>",
  "hostId": "<WORKER_HOST_ID>",
  "prompt": "<结构化命令>"
})
```

Worker 向主控发消息：

```text
send_message_to_thread({
  "threadId": "<CONTROLLER_THREAD_ID>",
  "hostId": "<CONTROLLER_HOST_ID>",
  "prompt": "<结构化状态>"
})
```

同一主机且 schema 不要求 `hostId` 时可以省略。不能用空字符串代替缺失 ID。

## 5. 监控与读取

等待最多 8 个真实任务：

```text
wait_threads({
  "targets": [
    {
      "threadId": "<WORKER_THREAD_ID>",
      "hostId": "<WORKER_HOST_ID>",
      "afterCursor": "<可选 cursor>"
    }
  ],
  "timeoutMs": 30000
})
```

使用 `timeoutMs: 0` 获取即时快照。保存每个目标返回的 cursor，后续作为 `afterCursor` 传回。

`wait_threads` 在第一个任务完成或需要注意时返回；普通 commentary 不会唤醒等待。超时结果会包含紧凑进度。不要只依赖 Worker 主动发消息。

状态含糊或需要证据时：

```text
read_thread({
  "threadId": "<WORKER_THREAD_ID>",
  "hostId": "<WORKER_HOST_ID>",
  "turnLimit": 5,
  "includeOutputs": false
})
```

只有核对命令输出确实必要时才设置 `includeOutputs: true`，并保持输出上限紧凑。

### 5.1 超过 8 个活跃 Worker

按稳定 `workerId` 顺序切分为每组最多 8 个：

1. 对所有组执行 `timeoutMs: 0` 的增量快照。
2. 对下一轮轮转组执行一次不超过约 15 秒的等待。
3. 更新所有 cursor。
4. 保证每个活跃 Worker 至少每 60 秒被主动观察一次。

### 5.2 效率检查

`wait_threads` 快照只能证明任务存活或需要注意，不能证明有效进展。健康软阈值触发时：

1. 使用账本中的 `lastUsefulProgressAt`、里程碑、验收关闭数和长命令窗口判断，不能用 commentary 数量代替。
2. 对目标 Worker 执行一次紧凑 `read_thread`，默认 `turnLimit: 5`、`includeOutputs: false`；只有核验证据需要时才有限读取输出。
3. 使用 `send_message_to_thread` 发送 protocol reference 定义的 `CHECKPOINT`，取得安全边界快照后再决定是否 `REPLAN`。
4. 不通过提高轮询频率、重复读取大输出或持续发送催促消息制造“看起来很忙”的状态。

## 6. worktree 与起始状态

worktree 只适用于 Git 仓库。它为每个 Worker 提供独立 checkout，适合多个写代码任务并行。

默认省略 `startingState`，让工具从项目默认分支创建 worktree。

只有用户明确要求包含当前 checkout 和未提交修改时：

```text
"environment": {
  "type": "worktree",
  "startingState": {
    "type": "working-tree"
  }
}
```

只有用户明确指定现有分支或 ref 时：

```text
"environment": {
  "type": "worktree",
  "startingState": {
    "type": "branch",
    "branchName": "<EXISTING_BRANCH_OR_REF>"
  }
}
```

不要用该字段命名新分支。不要自动提交当前 checkout 以制造起始点。

受 Git 忽略但 worktree 必需的本地文件，只能依赖项目已有且受用户管理的 `.worktreeinclude`。不要擅自把凭据加入该文件。

## 7. Handoff 与成果回收

Worker 在 worktree 完成单任务实现和验证后，主控逐个回收：

```text
handoff_thread({
  "threadId": "<WORKER_THREAD_ID>"
})
```

Handoff 返回 `operationId` 和 `revision` 后：

```text
get_handoff_status({
  "operationId": "<OPERATION_ID>",
  "afterRevision": <REVISION>,
  "waitMs": 30000
})
```

规则：

- Handoff 会中断仍在运行的目标任务；只在 Worker 已停止写入后调用。
- 一次只回收一个 Worker，按依赖顺序处理。
- 操作返回 `operationId` 不代表已经成功；必须确认 Handoff 终态。
- 回收前检查 Local 当前改动和已集成成果。
- 成功后在 Local 运行组合验证。
- 冲突时保留 worktree，停止后续回收，报告冲突，不做 reset、checkout 覆盖或其他破坏性清理。
- 主控不能 Handoff 自己；只操作 Worker。

Codex 托管 worktree 可能受产品级保留和清理策略影响。“不自动归档”只约束本 Skill 的归档行为，不保证底层 worktree 永久存在。标记 `✅` 或 `🗑️` 前，确保需要保留的代码已 Handoff、已用用户认可方式持久化，或已明确记录不再采用；不能让仍需恢复的唯一成果滞留在临时 worktree。

效率重规划不能绕过成果回收：写入型 Worker 持有未提交唯一成果时，不得直接创建第二个写入 Worker 接管同一文件边界。先让原 Worker 到达安全 checkpoint，并使用原请求已授权的 Handoff、commit 或替代持久化方式形成稳定基线；只读分析和互不重叠的工作仍可独立拆分。

## 8. 本地和远程主机

`list_projects` 返回本地与已连接远程主机上的项目。使用返回的 `projectId` 创建任务，不构造 `hostId`。

默认顺序：

1. 用户明确指定的主机。
2. 本地匹配项目 worktree。
3. 本地只读环境。
4. 本地缺少必要能力时的唯一远程匹配。

创建成功后保存真实 `hostId`。跨主机消息、等待和读取都复用该值。

远程写入 Worker 完成后：

```text
handoff_thread({
  "threadId": "<REMOTE_WORKER_THREAD_ID>",
  "destinationHostId": "local"
})
```

确认完成并更新 Worker 的主机信息。若结果到达本地匹配项目 worktree，再按本地 Handoff 流程串行回收到 Local。

远程项目匹配不唯一、目标主机不可用或代码不能安全转移时，进入 `BLOCKED`。不得用自动 push 远程分支绕过确认。

## 9. 失败边界

### 9.1 只有 `clientThreadId`

- 记录 `PROVISIONING`。
- 使用任务列表解析真实 `threadId`。
- 有限重试后仍失败则报告，不创建重复 Worker。

### 9.2 Worker 未发送 `DONE`

- 等待工具发现结束后读取任务。
- 结果完整则正常验收。
- 结果不完整则向原 Worker 发送 `REVISION`。
- 原 Worker 不可恢复时最多创建 1 个替代 Worker；再次失败则报告。

### 9.3 Handoff 失败

- 查询操作终态。
- 保留 worktree。
- 标记 `⌛️` 并报告。
- 不自动清理或归档。

### 9.4 远程失联

- 用最后有效 `hostId` 做一次有限核对。
- 只读工作可以在授权范围内创建 1 个本地替代 Worker。
- 未同步的远程写入成果不能标记 `✅`。

### 9.5 Worker 低效或陷入长尾

- 时间阈值只触发 `CHECKPOINT`，不直接终止任务。
- 一次紧凑读取后，按 protocol reference 检查验收关闭、范围增长、逐步放行往返、timeout 和可拆分工作。
- 可在原始授权内批量放行已核对的有界 manifest，并使用首个 nonzero/timeout 即停。
- 一次有界 `REPLAN` 后仍无有效进展时进入 `STALLED/BLOCKED`；保护并回收成果后，最多创建 1 个替代 Worker。
- 不因 Worker 较慢就复制相同任务，也不让两个 Worker 同时修改同一脏 worktree。

## 10. 官方依据

- Codex App Server 生命周期：<https://learn.chatgpt.com/docs/app-server#lifecycle-overview>
- Codex App Server API：<https://learn.chatgpt.com/docs/app-server#api-overview>
- Codex Skills：<https://learn.chatgpt.com/docs/build-skills>
- Codex Worktrees 与 Handoff：<https://learn.chatgpt.com/docs/environments/git-worktrees>

官方文档描述底层产品行为；本文件中的高层工具字段来自当前 Codex 运行时 schema。二者冲突时，说明差异并优先遵守实际可调用 schema。
