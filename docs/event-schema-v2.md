# 任务与调用事件契约 V2

`events_v2` 与旧 `calls` 并存。旧表继续服务兼容统计，事件表提供可跨工具关联、可区分缺失值的稳定事实；迁移不改写历史行，也不把空值补成零。

## 事件

| 类型 | 必填身份 | 语义 |
|---|---|---|
| `attempt.finished` | `command_id`；`attempt_id` 缺省时由 gnomon 生成 | 一次 CLI 调用结束，不声明业务任务完成 |
| `task.finished` | `task_id`、`task_outcome`、`task_outcome_source` | 由拥有任务语义的上层调用方写入；CLI 不得根据退出码代写 |

版本字段分为 producer、adapter、upstream 三层；未知版本存 `NULL`。`command_id` 是稳定命令身份，`invoked_as` 只保存公开命令路径或兼容别名，不保存业务参数值。

输出仅记录 `stdout_bytes`、`stderr_bytes` 和 `capture_status`。V2 不保存 stdout/stderr 正文；无法完整观测时必须使用 `python-tee`、`not-captured` 或 `unknown`，不得写成 `complete`。

两类事件字段严格隔离：attempt 不接受 `task_outcome*`；task 不接受 command、adapter、upstream、exit、耗时和输出捕获字段。`complete` 必须同时提供 stdout/stderr 字节数；计量值不得为负数，布尔值不得用字符串代替。

## 写入边界

- 本地 SQLite、best-effort；遥测失败不得改变命令退出码。
- `DO_NOT_TRACK` 与工具级 telemetry off 同时作用于旧表和 V2。
- event schema 不推断业务 outcome，不填充未知版本、字节数、耗时或 task ID。
- `context`、`meta` 只接受结构化对象；调用方负责不写凭证和业务正文。
