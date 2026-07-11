---
description: "gnomon 架构总览：项目定位、模块 codemap、事件契约、关键路径和开发入口。"
keywords: [gnomon, architecture, codemap, telemetry, events, sqlite]
kind: reference
---

# gnomon 架构

> 给新 session 的开发地图：这个仓是什么、各模块管什么、关键路径怎么走、改某类能力该从哪里入手。
> 设计取舍见 ADR-033。

## 1. 鸟瞰

`gnomon` 是本地 CLI 工具可复用的**共享遥测核心库**。它同时维护兼容旧消费者的调用账本和版本化事件表，供工具统计、跨工具任务关联与 Agent 工作面回放使用。

核心特性：
- **工具零耦合**：调用方传入 `Cfg(tool=...)` 即获得独立账本；核心对业务完全无知，`context` 字段是不透明的 per-tool JSON。
- **跨工具 union 分析**：所有账本共享同一 `calls` schema（v2），可通过 `ATTACH` + `UNION` 做跨工具序列挖掘。
- **attempt 与 task 分离**：CLI 只能记录调用事实；只有拥有任务语义的上层调用方才能声明任务结果。
- **本地-only、best-effort**：无网络、无副作用，任何写路径异常均被吞噬，不影响工具 exit code。

## 2. 模块地图

| 模块 | 路径 | 职责 |
|---|---|---|
| 包入口 | `src/gnomon/__init__.py` | 版本声明；re-export 调用账本与事件公开 API。 |
| 遥测核心 | `src/gnomon/telemetry.py` | 全部运行时逻辑：schema 定义、连接管理、v1→v2 迁移、两种 capture posture、stats 生成、caller 检测。 |
| 事件核心 | `src/gnomon/events.py` | `events_v2` schema、事件校验、best-effort 写入与读取。 |
| 测试 | `tests/` | pytest 单元测试，覆盖迁移、record、event、stats、detect_caller。 |
| 工程配置 | `pyproject.toml` | 依赖（typer 为可选 extra）、poe tasks、ruff、pytest、coverage 配置。 |

> `config.py` / `logging_setup.py` 是 seed 模板骨架遗留，当前版本**未使用**；核心配置通过 `Cfg` dataclass 和环境变量直接管理。

## 3. 核心不变量

- **`calls` schema 跨工具必须相同**：所有消费仓的账本必须满足 v2 schema（`command_path`/`caller`/`context` 等列存在），才能 ATTACH + UNION。破坏此约定导致跨工具分析失效。
- **`events_v2` 与 `calls` 独立演进**：事件表不能借旧表字段默认值伪造事实；未知版本、task ID、字节数和耗时必须保留为 `NULL`。
- **退出码不是任务结果**：`attempt.finished` 可记录 exit code 和结果分类，但不得据此自动生成 `task.finished`。任务事件必须由上层显式提供 `task_id`、`task_outcome` 和来源。
- **best-effort 不可逆**：`record` / `run_instrumented` 的所有异常路径均被 `except Exception: return` 吞噬。任何会向调用方抛出的改动都是 breaking change。
- **telemetry 不能改 exit code**：工具的 exit code 由工具自身决定；遥测层观察但不干预。
- **`context` 字段对核心不透明**：核心只做 `json.dumps`，不解析、不校验 key。业务数据留在各工具自己的 context schema，不硬编码进此库。
- **v1 账本就地升级，不丢历史**：`connect` 在首次打开时自动检测 `verb` 列存在 + `command_path` 缺失，执行 ALTER + UPDATE 回填，迁移幂等。
- **`uv run poe check` 是质量门**：lint + typecheck + test 串联；CI 同步跑。

## 4. 核心符号

全部从 `gnomon` 直接 import。

### `Cfg`

`src/gnomon/telemetry.py` — frozen dataclass，工具遥测身份。

| 字段 | 类型 | 说明 |
|---|---|---|
| `tool` | `str` | 工具名（kebab/snake），决定账本目录名和 stats 标题。 |
| `version` | `str` | 工具自身的 `__version__`，写入每行。默认 `""`。 |
| `env_prefix` | `str` | 环境变量前缀；默认 `tool.upper()`（`demo-tool` → `DEMO_TOOL`）。 |

衍生属性 `Cfg#prefix` 计算实际前缀，用于 `<PREFIX>_TELEMETRY_DB` 和 `<PREFIX>_TELEMETRY_OFF`。

### `db_path(cfg)`

`telemetry.db_path` — 解析账本路径。优先 `$<PREFIX>_TELEMETRY_DB` 覆盖，否则使用 XDG data 目录下的 `<tool>/telemetry.db`。

### `connect(path)`

`telemetry.connect` — 打开账本（WAL 模式，busy timeout 5s，`isolation_level=None`），执行 schema 建表 + v1→v2 迁移 + 索引。公开接口，工具可用 `connect(db_path(cfg))` 直接读账本做自定义分析。

### `record(rec, cfg, *, path=None)`

`telemetry.record` — 插入一条调用行，供**不能被 in-process 包裹**的工具（如 subprocess-dispatch shell）使用：工具自己计时、组装 `rec` dict 后调用。

`rec` 关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `command_path` | `list[str]` | 子命令层级，如 `["deploy", "pipeline", "run"]`。 |
| `caller` | `str` | `"agent"` 或 `"human"`；缺省时自动调用 `detect_caller()`。 |
| `context` | `dict` | 工具私有数据，核心不透明处理，`json.dumps` 存入。 |
| `exit_code` | `int` | 工具 exit code。 |
| `duration_ms` | `int` | 调用耗时（毫秒）。 |
| `stdout` / `stderr` | `str` | 输出采样（各有字节上限 `STDOUT_CAP=2048` / `STDERR_CAP=4096`）。 |

### `run_instrumented(app, argv, cfg, *, command_path=None, prog_name=None, meta=None, path=None)`

`telemetry.run_instrumented` — **in-process capture posture**，适合 Typer/Click 应用。把 `app` 在 telemetry 包裹下执行：`Tee` 劫持 stdout/stderr 计量输出字节 + 采样首段，计时，捕获 exit code，调用 `record`，返回 exit code。推荐作为 console-script 入口：

```python
def run() -> None:
    raise SystemExit(run_instrumented(app, sys.argv[1:], CFG))
```

`command_path` 缺省时从 `argv` 前导非 flag token 推导。`meta` dict 会与 `CLAUDE_CODE_SESSION_ID` session key 合并写入 `meta` 列，供跨工具序列挖掘。

### `detect_caller(env=None)`

`telemetry.detect_caller` — 识别调用来源，返回 `"agent"` 或 `"human"`。检测链：1) `_AGENT_ENV_KEYS` 环境变量（`AI_AGENT`/`CLAUDECODE`/`CLAUDE_CODE_SESSION_ID`/`CODEX_SANDBOX` 等）；2) `TERM_PROGRAM` 含 `claude`/`codex`；3) `env=None` 时递归扫 `_ancestor_commands`（ps 链，最深 8 层，处理 agent 子进程未透传 env 的场景）。传入自定义 `env` dict 则跳过进程扫描，用于测试隔离。

### `stats(cfg, *, path=None)`

`telemetry.stats` — 返回文本格式的 per-command 统计（count / p50·p95·max 延迟 / 错误数）及最近 10 条 fault。`_pctile` 用最近邻秩法。fault 定义：`err` 非空 OR `exit_code >= 2`（exit 1 无 err = 工具主动找到问题，不计 fault）。工具可在其 CLI 层组合追加自定义 section：`print(stats(cfg)); print(my_section())`。

### `record_event(event, cfg, *, path=None)`

`events.record_event` — 写入一个 `gnomon.event/v2` 事件。`attempt.finished` 必须提供稳定 `command_id`；`task.finished` 必须由上层显式提供 `task_id` 和 `task_outcome`。无效事件不落库，也不会向调用方抛错。

版本身份拆为 producer、adapter、upstream 三层；`invoked_as` 只保存公开命令路径或兼容别名。事件只记录输出字节数和捕获状态，不保存 stdout/stderr 正文。完整字段语义见 [任务与调用事件契约 V2](event-schema-v2.md)。

### `read_events(cfg=..., path=None)`

`events.read_events` — 按写入顺序读取本地事件并解码 `invoked_as`、`context`、`meta` JSON。账本不存在或不可读时返回空列表，主要用于工具自身统计和离线分析。

### `Tee`

`telemetry.Tee` — 透传 wrapper，劫持 `sys.stdout` / `sys.stderr`：写透到真实流，同时计量总字节（`Tee#total`）并保留首 `cap` 字节采样（`Tee#sample`）。遥测自身的任何计量异常被 `except Exception: pass` 吞噬，不进入写路径。

## 5. 存储契约

### 5.1 兼容调用账本

`calls` 表，所有工具账本共用同一 schema，可 ATTACH + UNION 跨工具分析。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 行 id。 |
| `ts` | TEXT | ISO 8601 带时区，ms 精度（本地时间）。 |
| `pid` | INTEGER | 工具进程 PID。 |
| `command_path` | TEXT | JSON 数组，子命令层级。v1 迁移时由 `verb` 字符串回填为 `json_array(verb)`。 |
| `args` | TEXT | JSON 数组，command_path 之后的 flag/参数。 |
| `exit_code` | INTEGER | 工具 exit code。 |
| `duration_ms` | INTEGER | 耗时（毫秒）。 |
| `out_bytes` | INTEGER | stdout 总字节数。 |
| `stdout` | TEXT | stdout 首 2048 字节采样。 |
| `stderr` | TEXT | stderr 首 4096 字节采样。 |
| `err` | TEXT | 非预期异常消息；工具正常 exit 1 时为空。 |
| `cwd` | TEXT | 调用时工作目录。 |
| `version` | TEXT | 工具版本（`Cfg.version`）。 |
| `is_tty` | INTEGER | stdout 是否为 TTY（0/1）。 |
| `is_ci` | INTEGER | `$CI` 是否为真（0/1）。 |
| `caller` | TEXT | `"agent"` 或 `"human"`。 |
| `context` | TEXT | 工具私有 JSON，核心不透明。 |
| `meta` | TEXT | 框架级 JSON；`run_instrumented` 写入 `session`（`CLAUDE_CODE_SESSION_ID`）。 |

索引：`calls_command_path ON calls(command_path)`。

**v1→v2 迁移**（`telemetry._migrate_v1_to_v2`）：检测到 `verb` 列存在且 `command_path` 缺失时，就地 ALTER ADD 缺失列 + `UPDATE calls SET command_path = json_array(verb)`。幂等；部分迁移账本（已有 `caller`/`context`）通过 `_ensure_v2_columns` 安全补全。

### 5.2 版本化事件

`events_v2` 与 `calls` 共用同一 SQLite 文件，但不从旧表反推事实。当前支持：

- `attempt.finished`：一次命令调用结束，可关联 `task_id`，但不声明任务完成。
- `task.finished`：上层任务结束，必须包含显式 outcome。

事件表以 `event_id` 唯一标识事件，以 `attempt_id` 关联一次调用，以 `task_id` 串联多次调用。schema 与隐私边界由 [任务与调用事件契约 V2](event-schema-v2.md)定义。

## 6. 两种采用 posture

| posture | 适用场景 | 入口 |
|---|---|---|
| `run_instrumented` | Typer/Click in-process 应用，stdout/stderr 可被 Tee 劫持 | `run_instrumented(app, sys.argv[1:], CFG)` 作为 console-script 入口 |
| `record` | subprocess-dispatch 工具（子进程直接写 fd，in-process capture 看不到输出），或工具已有自己的 exit 循环 | 工具自计时、组装 rec dict 后调用 `record(rec, cfg)` |

选错 posture 的后果：`run_instrumented` 用于 subprocess 场景会导致 stdout/stderr 采样为空（Tee 没捕到子进程输出）；`record` 用于 Typer 场景则需工具自行计时，容易漏 exception 分支。

## 7. 改 X 去哪

| 想改 / 加什么 | 从这里入手 | 备注 |
|---|---|---|
| 新增 `calls` 列 / 改 schema | `telemetry._SCHEMA_TABLE` + `_COLUMNS` + `_migrate_v1_to_v2` / `_ensure_v2_columns` | 必须幂等；不破坏跨工具 union 能力 |
| 新增事件字段 / 改事件约束 | `events._EVENTS_V2_TABLE` + `_EVENT_COLUMNS` + `_normalized_event` | 先改事件契约；未知值保持 `NULL`，不得从退出码推断任务结果 |
| 改 caller 检测逻辑 | `telemetry.detect_caller` + `_AGENT_ENV_KEYS` + `_ancestor_commands` | 注意 `env=None` vs 自定义 env 的分支 |
| 改统计输出格式 | `telemetry.stats` + `_cp_display` + `_pctile` + `_is_fault` | stats 返回文本，工具自行 print |
| 改 stdout/stderr 采样上限 | `telemetry.STDOUT_CAP` / `STDERR_CAP` | 改大会增大账本体积 |
| 改账本路径解析 | `telemetry.db_path` | 影响 `<PREFIX>_TELEMETRY_DB` 覆盖逻辑 |
| 加/改 in-process capture 行为 | `telemetry.run_instrumented` + `Tee` | 需要 `typer` extra；改 Tee 注意不能向写路径抛 |
| 工具集成（新消费方） | 新仓 `pyproject.toml` 依赖 + 建 `Cfg` 实例 + 选 posture；需要任务关联时再调用 `record_event` | 见 §6；typer extra 只在用 `run_instrumented` 时需要 |
| 改工程检查 | `pyproject.toml` poe tasks | `uv run poe check` 为质量门 |
| 补文档 | `docs/` | architecture.md = 当前真相；ADR = 取舍；spec = 约束 |

## 8. 非目标

- 本文档不是 README；安装和快速使用归 `README.md`。
- 本文档不是 ADR；设计取舍和选型理由归 ADR-033。
- 本文档不是 spec；可单条违反的约束归 `*-spec.md`。
- 本文档不是 runbook；连续操作步骤归 runbook/how-to。
- 不记录产品分析 / 上报外部服务：本库 local-only，无网络路径。
