# Feature 记录

## Feature 1 — 跨代码页稳定的 UTF-8 CLI 输出

- 状态：已实现
- 记录日期：2026-07-24
- 问题：`ledger.py` 和 `dispatch.py` 继承宿主默认代码页时，GBK 等编码无法输出任务标题中的 emoji，导致状态读取或标题渲染在最终 JSON 输出阶段触发 `UnicodeEncodeError`。
- 行为：两个 CLI 在启动边界统一把 stdout 固定为 UTF-8，并让 stderr 使用 UTF-8 的安全错误回退；调用方不再需要额外传入 `python -X utf8`。
- 兼容性：不修改 SQLite schema、账本内容、事务语义、退出码或 JSON 字段。
- 验证：在 `PYTHONIOENCODING=gbk:strict` 下覆盖带 `👑` 的 `ledger status` 和带 `✍️` 的 `dispatch render-title`；完整测试套件通过。
- 实现：
  - `.agents/skills/orchestrate-codex-tasks/scripts/orchestration_common.py`
  - `.agents/skills/orchestrate-codex-tasks/scripts/ledger.py`
  - `.agents/skills/orchestrate-codex-tasks/scripts/dispatch.py`
  - `tests/test_ledger.py`
  - `tests/test_dispatch.py`

## Feature 2 — 轻量控制面与自动微放行护栏

- 状态：已实现
- 记录日期：2026-07-24
- 问题：真实运行中，主控可能连续使用“放行一步、等待一步”的微调消息，恢复时还要分别执行 `verify/status/pending`；任务服务 timeout 后切换多种读取工具会进一步放大控制面噪声。
- 行为：
  - `DECISION/REVISION` 仅在消息实际发送成功后自动累计 `decisionRoundTrips`；失败、未知或重复 outcome 不重复计数。
  - 连续 3 次后拒绝新的微放行；`CHECKPOINT` 仍可用，后续 `REPLAN` 必须携带有步骤、墙钟上限、首个 nonzero/timeout 即停的 `executionPlan`。有界 `REPLAN` 成功后自动清零计数。
  - 新增只读事务命令 `ledger.py snapshot`，一次返回校验、边界化状态和带语义 request 的 pending operations。
  - 监控协议区分内部观察与用户汇报，并规定连续 2 次任务服务 timeout 后熔断 2 分钟。
- 兼容性：SQLite schema 和协议版本不变；原有 `verify/status/pending` 命令、未达到阈值的旧 `REPLAN` 请求继续有效。
- 验证：覆盖失败与重复 outcome、三次阈值、`CHECKPOINT` 旁路、有界重规划复位和原子恢复快照。

## Feature 3 — 自适应 Worker Prompt 与资源感知调度

- 状态：已实现
- 记录日期：2026-07-24
- 问题：所有 Worker 都加载同一份完整协议，导致只读任务也承担写入、Handoff 和完整恢复规则；仅按 Worker 数量限流也无法避免浏览器、模拟器或高成本服务争用。
- 行为：
  - 新增 `lean/standard/strict` 协调档位。未显式配置时，只读 Worker 自动使用 `lean`，写入 Worker 自动使用 `standard`；只有显式 `strict` 才加载原完整长模板。
  - `lean` 禁止文件写入；`standard` 保留 worktree、写边界和 Git 证据规则；三档都保留寻址、序号、阻塞、完成与账本单写者边界。
  - manifest 可选声明 `resourceCapacities`，Worker 可选声明 `resourceClaims`；`ready` 在选择同批 Worker 时原子预留资源，并让 `BLOCKED/REVIEW` Worker 释放计算资源但不释放生命周期槽位。
- 兼容性：新字段完全可选；旧 manifest 规范化结果和 `manifestHash` 不因推断出的默认档位而变化。
- 验证：覆盖默认档位、显式严格档位、只读/写入约束、资源争用、运行中占用和阻塞后释放。当前完整测试套件共 33 项通过。
