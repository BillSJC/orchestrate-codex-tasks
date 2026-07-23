# Controller 与 Worker 协作协议

## 目录

1. 运行身份
2. Worker Prompt 模板
3. Worker 到主控的消息
4. 主控到 Worker 的消息
5. 标题和状态机
6. 运行账本
7. 并发调整
8. 恢复与去重

## 1. 运行身份

每次运行生成：

- `runId`：短运行标识，例如 `R7K2`。
- `workerId`：稳定 Worker 标识，例如 `W1`。
- `controllerThreadId`：主控真实任务 ID。
- `controllerHostId`：跨主机时必填；同主机且工具不要求时可省略。

主控必须把自己的真实 `threadId` 直接写入每个 Worker Prompt。Worker 不猜测、不搜索主控地址。

## 2. Worker Prompt 模板

把以下模板完整填充后作为 `create_thread` 的初始 `prompt`：

```text
你是一个独立 Codex Worker 任务，不是 Codex 子 Agent。
你由一个主控任务派发，只负责下面定义的单一子任务。

协调地址
- runId: {{RUN_ID}}
- workerId: {{WORKER_ID}}
- controllerThreadId: {{CONTROLLER_THREAD_ID}}
- controllerHostId: {{CONTROLLER_HOST_ID_OR_OMIT}}

任务
- 目标：{{OBJECTIVE}}
- 背景与输入：{{CONTEXT}}
- 允许范围：{{IN_SCOPE}}
- 禁止范围：{{OUT_OF_SCOPE}}
- 前置依赖：{{DEPENDENCIES}}
- 执行主机：{{WORKER_HOST}}
- 执行环境：{{LOCAL_OR_WORKTREE_OR_PROJECTLESS}}
- worktree 起始状态：{{STARTING_STATE_OR_NOT_APPLICABLE}}
- 文件写入边界：{{WRITE_BOUNDARY}}
- 成果回收方式：{{INTEGRATION_PLAN}}
- 期望交付物：{{DELIVERABLES}}
- 验收与验证：{{ACCEPTANCE}}

强制协作协议
1. 这是主控拆出的独立子任务。不要创建子 Agent、其他 Codex 任务或新的 Worker。
2. 不要自行改名、归档或移动本任务；标题和生命周期由主控管理。
3. 如果 send_message_to_thread 未直接加载，先使用工具搜索发现它。
4. 开始后立即向主控发送 ACCEPTED，说明你理解的目标、计划和首个里程碑。
5. 遇到以下任一情况时不得自行猜测：
   - 需要产品、业务、架构或用户偏好决策；
   - 需求互相冲突或缺少关键输入；
   - 需要扩大权限、文件边界或外部影响；
   - 缺少依赖、环境或凭据；
   - 继续执行可能造成不可逆或高风险结果。
6. 发生阻塞时：
   - 暂停受影响的动作；
   - 使用 send_message_to_thread 向 controllerThreadId 发送 BLOCKED；
   - 提供原因、已确认事实、选项、推荐方案和不决策的影响；
   - 等待主控回复。只继续与阻塞无关且明确安全的工作。
7. 有实质性里程碑时发送 PROGRESS；不要发送无信息量心跳。
8. 完成时先发送 DONE，包含结果、证据、验证、文件或链接和残余风险，然后再结束本任务。
9. 主控负责最终决策和验收。不要向主人宣称总体工作已完成。

代码写入规则
- 只修改“文件写入边界”允许的路径。
- 默认在独立 worktree 中实现和测试。
- 不自行 Handoff 到 Local。
- 不提交、推送、开 PR 或发布，除非本 Prompt 明确授权。
- DONE 必须附 git status --short、变更文件清单、测试命令和结果。

消息调用
send_message_to_thread({
  "threadId": "{{CONTROLLER_THREAD_ID}}",
  "hostId": "{{CONTROLLER_HOST_ID_OR_OMIT}}",
  "prompt": "<按下列 Worker 消息格式填写>"
})

如果 send_message_to_thread 不可用，在最终输出开头写 BLOCKED，说明无法满足协调协议，并停止需要主控决策的工作。
```

若同一主机且 `hostId` 可省略，删除整个 `hostId` 字段，不传空字符串。

## 3. Worker 到主控的消息

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} seq={{SEQ}} type={{TYPE}}]
summary: <一句话状态>
details:
- <关键事实或产出>
next:
- <下一步；DONE 时写 none>
needs:
- <需要主控决定的事项；无则写 none>
evidence:
- <文件、命令、测试、链接或其他证据>
```

`TYPE` 只能是：

- `ACCEPTED`
- `PROGRESS`
- `BLOCKED`
- `DONE`

`seq` 从 `001` 开始单调增加。每个消息只表达一个主要状态变化。

### 3.1 BLOCKED 最低内容

```text
summary: <阻塞点>
details:
- facts: <已经确认的事实>
- cause: <为什么无法继续>
next:
- option-a: <方案和影响>
- option-b: <方案和影响>
needs:
- recommendation: <Worker 推荐>
- decision: <主控必须决定什么>
evidence:
- <相关文件、错误或工具结果>
```

### 3.2 DONE 最低内容

```text
summary: <完成了什么>
details:
- deliverables: <交付物>
- changed-files: <文件清单或 none>
- residual-risks: <残余风险或 none>
next:
- none
needs:
- none
evidence:
- commands: <验证命令>
- results: <验证结果>
- git-status: <git status --short 或 not-applicable>
```

## 4. 主控到 Worker 的消息

```text
[ORCH run={{RUN_ID}} worker={{WORKER_ID}} controllerSeq={{SEQ}} command={{COMMAND}}]
decision: <主控决定或 none>
instructions:
- <下一步>
acceptanceDelta:
- <验收条件变化；无则 none>
```

`COMMAND` 只能是：

- `DECISION`：回答阻塞问题。
- `REVISION`：验收失败，要求在原范围内修订。
- `SCOPE_UPDATE`：用户已经授权范围变化。
- `STOP`：用户明确停止，或原任务已失去价值。

主控不能用 `SCOPE_UPDATE` 自行扩大用户授权。

## 5. 标题和状态机

### 5.1 主控标题

```text
👑 [<runId>] 拆解｜<总体目标>
👑 [<runId>] 跟进 <N> 个 Worker｜<总体目标>
👑 [<runId>] 等待主人确认｜<总体目标>
👑 [<runId>] 汇总｜<总体目标>
👑 [<runId>] 完成｜<总体目标>
```

`👑` 必须是第一个字符。

### 5.2 Worker 标题

```text
✍️ [<runId>-<workerId>] <动宾短语>
⌛️ [<runId>-<workerId>] <动宾短语>｜<阻塞摘要>
✅ [<runId>-<workerId>] <动宾短语>
```

图标必须是第一个字符。Worker 不自行改名。

### 5.3 状态映射

| 状态 | 标题前缀 | 含义 |
|---|---|---|
| `PROVISIONING` | 暂无或 `⌛️` | 只有 `clientThreadId`，等待真实任务 |
| `RUNNING` | `✍️` | 正在执行或修订 |
| `BLOCKED` | `⌛️` | 等待决定、澄清、权限、依赖或故障处理 |
| `VALIDATING` | `✍️` | 主控正在验收，尚未确认完成 |
| `DONE` | `✅` | 主控验收及必要组合验证通过 |
| `STOPPED` | `⌛️` | 未完成而停止；标题后缀写明原因 |

允许转换：

```text
PROVISIONING -> RUNNING
PROVISIONING -> STOPPED
RUNNING -> BLOCKED
BLOCKED -> RUNNING
RUNNING -> VALIDATING
VALIDATING -> RUNNING
VALIDATING -> DONE
RUNNING/BLOCKED -> STOPPED
```

Worker 自称 `DONE` 只触发 `VALIDATING`。只有主控验收后才能进入 `DONE` 和 `✅`。

## 6. 运行账本

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

运行级保存：

| 字段 | 含义 |
|---|---|
| `runId` | 运行标识 |
| `controllerThreadId` | 主控 ID |
| `controllerHostId` | 主控主机 |
| `maxActiveWorkers` | 默认 8，用户可调 |
| `activeCount` | 非 `DONE/STOPPED` 数量 |
| `queuedCount` | 未创建的规划任务数 |
| `monitorGroups` | 每组最多 8 个 Worker |
| `localPreferred` | 默认 `true` |

## 7. 并发调整

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
- 每次改变都报告旧值、新值、活跃数和排队数。

## 8. 恢复与去重

1. 用 `runId` 搜索标题。
2. 列出任务并匹配 `runId-workerId`。
3. 读取近期任务状态。
4. 只接受比 `lastSeq` 更新的 Worker 消息。
5. `DONE` 后的旧 `PROGRESS` 不回退状态。
6. `REVISION` 保持相同 Worker ID，消息序号继续增加。
7. 不因上下文丢失重复创建 Worker。
8. 不因恢复、失败或停止而自动归档任何任务。
