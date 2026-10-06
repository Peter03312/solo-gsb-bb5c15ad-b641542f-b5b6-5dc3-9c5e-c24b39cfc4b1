# 铁路司机值勤合规核算服务

基于 **FastAPI + PostgreSQL** 的司机值勤合规核算服务：录入司机与值勤片段（驾驶 / 其他值勤 / 休息），
按现存记录实时核算两项合规规则，支持按修订号更正、**跨司机批量调整（一次操作原子生效）**、审核发布固化历史版本并与当前结果对照。

## 合规规则

等于上限视为合规（越限 = 严格大于）。

| 规则 | 标识 | 内容 |
|---|---|---|
| 滚动 24 小时值勤上限 | `ROLLING_24H_DUTY_LIMIT` | 任意滚动 24 小时窗口内，驾驶 + 其他值勤合计 ≤ 10 小时 |
| 累计驾驶上限 | `CONTINUOUS_DRIVING_LIMIT` | 自首个驾驶片段起、及每次合格休息后，累计驾驶 ≤ 4 小时；只有**连续休息满 30 分钟**才清零重计（相接的休息片段合并计算；其他值勤与记录间隙打断连续休息） |

查询返回：违规类型、**首次越限时刻**（精确到越限发生的时间点）、**参与片段**。
补录或更正后，查询接口按现存记录实时重算。

## 快速开始

```bash
docker compose up --build
```

- API 地址： **http://localhost:8000**
- 交互式文档（Swagger UI）： **http://localhost:8000/docs**
- 健康检查： `GET http://localhost:8000/health`

Compose 会启动两个容器：`db`（PostgreSQL 16，端口 5432）与 `app`（FastAPI，端口 8000）。
应用启动时自动等待数据库就绪并**初始化建表**，无需手工迁移。

一键端到端演示（服务启动后执行）：

```bash
./examples/demo.sh            # 默认打 http://localhost:8000，可用 BASE_URL 覆盖
```

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://duty:duty@localhost:5432/duty` | SQLAlchemy 连接串；Compose 中注入为 `...@db:5432/duty` |
| `DB_CONNECT_RETRIES` | `60` | 启动时等待数据库的重试次数 |
| `DB_CONNECT_INTERVAL` | `1` | 重试间隔（秒） |

端口映射在 `docker-compose.yml` 中：`8000:8000`（API）、`5432:5432`（数据库），按需修改。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/drivers` | 录入司机；同 ID 同内容幂等（200），异内容 409 |
| GET | `/drivers` / `/drivers/{driver_id}` | 查询司机 |
| POST | `/segments` | 录入片段；同 ID 同内容幂等，异内容 409；重叠 409；非正时长 422 |
| GET | `/segments/{segment_id}` | 查询单一片段 |
| PATCH | `/segments/{segment_id}` | 按 `expected_revision` 更正；修订号不符 409；成功后修订号 +1 |
| GET | `/drivers/{driver_id}/segments` | 司机全部片段（按开始时间排序） |
| POST | `/reassignments` | 批量跨司机调整：一次操作提交一批已有片段的新归属/时间/类型，整批原子生效或整批拒绝；同批次号同内容重试幂等（200），异内容 409 |
| GET | `/reassignments/{batch_id}` | 按批次号查询已提交批次的固化结果 |
| GET | `/drivers/{driver_id}/compliance` | 按现存记录实时重算，返回违规类型 / 首次越限时刻 / 参与片段 |
| POST | `/drivers/{driver_id}/audits` | 审核发布：固化输入修订与结论；同修订并发/重复发布只留一版（返回既有版本，`created_new=false`） |
| GET | `/drivers/{driver_id}/audits` | 历史版本列表 |
| GET | `/drivers/{driver_id}/audits/{audit_id}` | 查看历史版本 |
| GET | `/drivers/{driver_id}/audits/{audit_id}/compare` | 历史版本 vs 当前重算结果对照（含输入修订差异） |

片段类型 `type`：`DRIVING`（驾驶）、`OTHER_DUTY`（其他值勤）、`REST`（休息）。
时间须为带时区的 ISO 8601（建议 UTC，如 `2026-10-05T08:00:00Z`），服务统一按 UTC 存储与计算。

## 调用示例

```bash
# 录入司机
curl -s -X POST http://localhost:8000/drivers \
  -H 'Content-Type: application/json' \
  -d '{"driver_id":"DRV-1001","name":"张三"}'

# 录入片段（驾驶 08:00-10:00 UTC）
curl -s -X POST http://localhost:8000/segments \
  -H 'Content-Type: application/json' \
  -d '{"segment_id":"SEG-A","driver_id":"DRV-1001",
       "start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'

# 按预期修订号更正（当前修订号不符时返回 409）
curl -s -X PATCH http://localhost:8000/segments/SEG-A \
  -H 'Content-Type: application/json' \
  -d '{"expected_revision":1,"end_utc":"2026-10-05T10:30:00Z"}'

# 合规查询（实时重算）
curl -s http://localhost:8000/drivers/DRV-1001/compliance

# 审核发布（固化输入修订与结论；同修订重复发布返回既有版本）
curl -s -X POST http://localhost:8000/drivers/DRV-1001/audits

# 历史版本与当前结果对照
curl -s http://localhost:8000/drivers/DRV-1001/audits/1/compare
```

### 批量跨司机调整（本轮新增）

调度员把同一批已有片段的调整作为**一次操作**提交：请求携带批次号，每条片段给出
ID、预期修订号、目标司机、起止时间与类型；同批不得重复指定片段。重叠按**所有调整
完成后的最终排布**检查，因此两名司机交换相接或互不重叠的片段不受提交顺序影响。
目标司机不存在（404）、片段缺失（404）、修订号过期（409）、最终排布重叠（409）时
**整批拒绝且不改任何记录**；成功后每条片段修订号只加一。

```bash
# 准备：两名司机各有一段同时段驾驶
curl -s -X POST http://localhost:8000/drivers -H 'Content-Type: application/json' \
  -d '{"driver_id":"DRV-2001","name":"王五"}'
curl -s -X POST http://localhost:8000/drivers -H 'Content-Type: application/json' \
  -d '{"driver_id":"DRV-2002","name":"赵六"}'
curl -s -X POST http://localhost:8000/segments -H 'Content-Type: application/json' \
  -d '{"segment_id":"SEG-P","driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}'
curl -s -X POST http://localhost:8000/segments -H 'Content-Type: application/json' \
  -d '{"segment_id":"SEG-Q","driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}'

# 一次操作交换两名司机的片段（201；逐条提交必然重叠，整批最终排布检查则合法）
curl -s -X POST http://localhost:8000/reassignments -H 'Content-Type: application/json' \
  -d '{"batch_id":"BATCH-0001","items":[
        {"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"},
        {"segment_id":"SEG-Q","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

# 同批次号同内容重试 -> 200，返回首次提交的结果（修订号不会重复累加）
curl -s -X POST http://localhost:8000/reassignments -H 'Content-Type: application/json' \
  -d '{"batch_id":"BATCH-0001","items":[
        {"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"},
        {"segment_id":"SEG-Q","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

# 同批次号异内容 -> 409 batch_conflict
curl -s -X POST http://localhost:8000/reassignments -H 'Content-Type: application/json' \
  -d '{"batch_id":"BATCH-0001","items":[
        {"segment_id":"SEG-P","expected_revision":2,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

# 修订号过期 -> 409 revision_conflict，整批拒绝、不改任何记录
curl -s -X POST http://localhost:8000/reassignments -H 'Content-Type: application/json' \
  -d '{"batch_id":"BATCH-0002","items":[
        {"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

# 按批次号查询已提交批次的固化结果
curl -s http://localhost:8000/reassignments/BATCH-0001
```

成功响应示例（201）：返回每条片段的修订变化，以及受影响司机（源 ∪ 目标）
调整前后的实时合规结论：

```json
{
  "batch_id": "BATCH-0001",
  "applied_at": "2026-10-06T09:00:00Z",
  "segment_changes": [
    {"segment_id": "SEG-P", "previous_revision": 1, "revision": 2},
    {"segment_id": "SEG-Q", "previous_revision": 1, "revision": 2}
  ],
  "drivers": [
    {"driver_id": "DRV-2001", "before": {"compliant": true, "violations": []},
     "after": {"compliant": true, "violations": []}},
    {"driver_id": "DRV-2002", "before": {"compliant": true, "violations": []},
     "after": {"compliant": true, "violations": []}}
  ]
}
```

校验失败的批次不留记录：同一批次号修正内容后可重新提交。已发布的审核版本不受
批量调整影响，可用 compare 接口对照批次带来的修订变化。

合规查询响应示例：

```json
{
  "driver_id": "DRV-1001",
  "evaluated_at": "2026-10-05T12:00:00Z",
  "compliant": false,
  "violations": [
    {
      "rule": "CONTINUOUS_DRIVING_LIMIT",
      "first_violation_at": "2026-10-05T12:20:00Z",
      "segment_ids": ["SEG-A", "SEG-C"],
      "detail": {"limit_seconds": 14400, "reset_rule": "连续休息满 1800 秒才清零重计"}
    }
  ]
}
```

## 测试

```bash
# 本地（合规引擎为纯标准库实现，无第三方依赖也可运行）
python3 -m unittest discover -s tests

# 或在容器内运行完整测试
docker compose run --rm app python -m pytest -q
```

## 设计要点

- **幂等录入**：`POST /drivers`、`POST /segments` 按业务主键判重，同内容重试返回 200 与既有记录，异内容返回 409。
- **乐观修订**：片段更正必须携带 `expected_revision`，与当前修订号不符即 409；更正走 `SELECT ... FOR UPDATE` 行锁，成功后修订号 +1。
- **重叠校验**：同司机片段按 `start < new_end AND end > new_start` 判重，首尾相接（`end == start`）合法；非正时长在入参校验阶段拒绝（422）。所有改动片段的写路径（录入、单片段更正、批量调整）先持有目标司机行锁，同一司机上的并发写串行化，重叠检查不会被并发事务绕过。
- **批量跨司机调整**：`POST /reassignments` 把一批已有片段的调整作为一次操作提交。批次内片段按 ID 排序逐条加行锁、受影响司机（源 ∪ 目标）按 ID 排序加行锁（固定顺序避免死锁）；先校验（片段存在、修订号相符、目标司机存在），再在内存中应用全部调整并按**最终排布**检查各司机重叠，任一失败整批回滚、不改任何记录；全部通过后每条修订号只加一，响应与调整同事务提交。
- **批次幂等**：成功批次按批次号落库（请求内容规范化后的 SHA-256 指纹 + 固化响应）；同批次号同内容重试（含并发重试，由批次行主键串行化）返回首次提交的结果（200），异内容返回 409；校验失败的批次不留记录，批次号可修正后重新提交。
- **实时重算**：合规结论不落库，查询时按现存记录重算，补录/更正立即反映。
- **滚动 24h 精确求值**：将“过去 24h 值勤时长”视为连续分段线性函数，对斜率变化事件扫描并线性插值，得到精确的首次越限时刻（而非按整点粗粒度判定）。
- **审核发布不可变**：发布时对全部输入片段的 `(segment_id, revision)` 计算 SHA-256 指纹并连同结论一起固化；`(driver_id, fingerprint)` 唯一约束保证同修订并发发布只留一版（冲突方拿到既有版本）；快照无任何修改入口，之后的更正不影响旧版，可随时用 compare 接口对照当前结果。
