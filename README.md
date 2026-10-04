# 国际中文联合培养履约协同

广西高校与东盟院校联合培养班的履约协同服务。四方在各自权限内提出条款、逐项
会签，形成**带生效期的协议版本**；生效后的任何变更只能通过**修订案**推进，不得
覆盖已经执行的承诺；招生批次、课程交付、师资派出、资源提供全部挂接到具体条款；
迟交、部分履行、争议、替代履行都有明确状态与证据链。

## 核心设计

系统采用**事件溯源（event sourcing）**：所有决定都是只追加事件，带前向 SHA-256
哈希链。状态由事件重放得到，历史永不被改写。

| 关注点 | 实现 |
| --- | --- |
| 条款化逐项会签 | `draft_version → open_for_signature → sign_clause(逐条) → finalize_version` |
| 带生效期的版本 | `VersionEffective.effective_at`；`effective_version_at(as_of)` 给当时适用版本 |
| 变更只能走修订案 | 修订必须显式 `CARRIED / MODIFIED / 新增替代`，且覆盖基础版本每一条款 |
| 已执行承诺不被覆盖 | 有履行进度的条款禁止 `MODIFIED`/被替代，只能 `CARRIED` |
| 并发修订隔离 | 乐观并发：修订必须基于最新生效版本，否则抛 `ConcurrencyError` |
| 过期/拒签不污染新决定 | 未成立版本不会成为基础；新草案回到其基础版本重新起草 |
| 履约状态机 | `PENDING → PARTIAL → FULFILLED / LATE_* → DISPUTED → PENDING / SUBSTITUTED` |
| 证据链 | 每次进度、争议、裁决、替代履行都必须附 `Evidence` |
| 提醒与升级 | 由**可控业务时钟**驱动（临期提醒 / 逾期升级 / 争议升级），扫描幂等 |
| 重启恢复 | 事件落盘 `fsync`，进程重启后从 JSONL 重放，待确认事项原样恢复 |
| 可核验决策记录 | 事件哈希链在载入时逐跳校验，篡改/截断/插入立即报错 |
| 历史时点查询 | 按培养批次 `cohort` + 历史时点 `as_of` 回放事件前缀 |

### 角色与权限

- `COORDINATOR`（主办高校）、`ACADEMIC_PARTNER`（东盟院校）：可在权限内提案、会签。
- `ADMIN`：只读/查询/裁决见证，**不能提案、不能会签**。

### 条款处置（修订案）

- `CARRIED`：原文延续，文本与到期日不得变更；
- `MODIFIED`：条款修改，仅向将来生效，已有履行进度时禁止；
- 新增条款（`ADDED`）并填写 `supersedes_clause_id`：以新条款替代旧条款，旧编号退出新版本。

## 目录

```
src/joint_program/
  contracts.py   基础契约(AgreementVersion / ObligationRecord)
  domain.py      领域模型、枚举、状态、可控时钟、不变量
  events.py      只追加事件与哈希
  aggregate.py   聚合根:所有业务规则的守门人(纯函数式状态机)
  store.py       JSONL 事件日志(fsync 追加 + 哈希链校验)
  service.py     应用服务:命令提交、提醒扫描、批次/时点查询
  http_api.py    标准库只读查询端点
  serve.py       端点启动入口
tests/           23 个测试,覆盖会签、修订、履约、争议、时钟、重启、篡改、HTTP
run_cli.py       端到端冒烟脚本
```

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 端到端冒烟(会签→提醒→修订→逾期升级→争议→替代履行→重启→时点查询)
python3 run_cli.py
```

启动只读查询端点（先用业务代码产生 `events.jsonl`）：

```bash
python3 -m joint_program.serve /path/to/events.jsonl --agreement A-JP-2026F --port 8080
```

端点：

- `GET /health`
- `GET /current` — 当时钟时点适用的协议版本（含待会签项）
- `GET /obligations?party=P-B&cohort=COHORT-2026F` — 各方未履约项
- `GET /history?as_of=2026-09-12T09:00:00&cohort=COHORT-2026F`
  — 当时适用版本、未履约项、各修订案影响、可核验决策记录
- `GET /decisions?as_of=...` — 截至时点的决策记录（响应中可逐跳核验哈希链）

## 业务时钟

提醒、升级、会签过期全部以注入的 `Clock` 为准，而非系统墙钟。测试用
`FixedClock` 可显式 `advance(...)` / `set(...)`；生产用 `SystemClock`。
`sweep()` 显式推进自动化判定，且可重复调用、结果幂等。
