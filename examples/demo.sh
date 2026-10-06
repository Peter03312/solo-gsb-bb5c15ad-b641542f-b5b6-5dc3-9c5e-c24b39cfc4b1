#!/usr/bin/env bash
# 端到端调用示例：录入 -> 幂等/冲突 -> 重叠校验 -> 合规查询 -> 更正 -> 审核发布 -> 历史对照 -> 批量跨司机调整
# 用法: BASE_URL=http://localhost:8000 ./examples/demo.sh
set -uo pipefail

BASE="${BASE_URL:-http://localhost:8000}"

req() { # req METHOD PATH [JSON]
  local method="$1" path="$2" data="${3:-}"
  echo "==> ${method} ${BASE}${path}"
  [ -n "${data}" ] && echo "    ${data}"
  local out code body
  if [ -n "${data}" ]; then
    out=$(curl -s -X "${method}" "${BASE}${path}" -H 'Content-Type: application/json' -d "${data}" -w $'\n%{http_code}')
  else
    out=$(curl -s -X "${method}" "${BASE}${path}" -w $'\n%{http_code}')
  fi
  code="${out##*$'\n'}"; body="${out%$'\n'*}"
  echo "${body}" | python3 -m json.tool 2>/dev/null || echo "${body}"
  echo "HTTP ${code}"
  echo
}

echo "########## 0. 健康检查"
req GET /health

echo "########## 1. 录入司机（重复提交同内容 -> 幂等 200；异内容 -> 409）"
req POST /drivers '{"driver_id":"DRV-1001","name":"张三"}'
req POST /drivers '{"driver_id":"DRV-1001","name":"张三"}'
req POST /drivers '{"driver_id":"DRV-1001","name":"李四"}'

echo "########## 2. 录入片段：驾驶 08:00-10:00（重试幂等 / 异内容冲突 / 非正时长拒绝）"
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-A","driver_id":"DRV-1001","start_utc":"2026-10-05T08:00:00Z","end_utc":"2026-10-05T11:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-BAD","driver_id":"DRV-1001","start_utc":"2026-10-05T10:00:00Z","end_utc":"2026-10-05T09:00:00Z","type":"REST"}'

echo "########## 3. 重叠非法（409），相接合法（201）"
req POST /segments '{"segment_id":"SEG-X","driver_id":"DRV-1001","start_utc":"2026-10-05T09:00:00Z","end_utc":"2026-10-05T11:00:00Z","type":"OTHER_DUTY"}'
req POST /segments '{"segment_id":"SEG-E","driver_id":"DRV-1001","start_utc":"2026-10-05T07:30:00Z","end_utc":"2026-10-05T08:00:00Z","type":"REST"}'

echo "########## 4. 休息 20 分钟（不足 30 分钟，不清零）+ 继续驾驶 -> 累计驾驶越限"
req POST /segments '{"segment_id":"SEG-B","driver_id":"DRV-1001","start_utc":"2026-10-05T10:00:00Z","end_utc":"2026-10-05T10:20:00Z","type":"REST"}'
req POST /segments '{"segment_id":"SEG-C","driver_id":"DRV-1001","start_utc":"2026-10-05T10:20:00Z","end_utc":"2026-10-05T12:30:00Z","type":"DRIVING"}'
req GET /drivers/DRV-1001/compliance
# 期望：CONTINUOUS_DRIVING_LIMIT，首次越限 2026-10-05T12:20:00Z，参与片段 SEG-A、SEG-C

echo "########## 5. 按预期修订号更正：休息延长到 40 分钟（合格休息，清零重计）"
req PATCH /segments/SEG-B '{"expected_revision":1,"end_utc":"2026-10-05T10:40:00Z"}'
req PATCH /segments/SEG-C '{"expected_revision":1,"start_utc":"2026-10-05T10:40:00Z"}'
echo "---- 用过期的修订号再次更正 -> 409"
req PATCH /segments/SEG-C '{"expected_revision":1,"start_utc":"2026-10-05T10:45:00Z"}'
req GET /drivers/DRV-1001/compliance
# 期望：compliant=true（更正后按现存记录重算）

echo "########## 6. 补录其他值勤 13:00-19:30 -> 滚动 24h 值勤合计 10h20m 越限"
req POST /segments '{"segment_id":"SEG-D","driver_id":"DRV-1001","start_utc":"2026-10-05T13:00:00Z","end_utc":"2026-10-05T19:30:00Z","type":"OTHER_DUTY"}'
req GET /drivers/DRV-1001/compliance
# 期望：ROLLING_24H_DUTY_LIMIT，首次越限 2026-10-05T19:10:00Z，参与片段 SEG-A、SEG-C、SEG-D

echo "########## 7. 审核发布：固化当前输入修订与结论；同修订重复发布只留一版"
req POST /drivers/DRV-1001/audits
req POST /drivers/DRV-1001/audits

echo "########## 8. 更正 SEG-D 缩短到 19:00（旧版本不变），重算后合规"
req PATCH /segments/SEG-D '{"expected_revision":1,"end_utc":"2026-10-05T19:00:00Z"}'
req GET /drivers/DRV-1001/compliance

echo "########## 9. 再次发布产生新版本；列表查看历史版本"
req POST /drivers/DRV-1001/audits
req GET /drivers/DRV-1001/audits

echo "########## 10. 对照历史版本 1 与当前结果"
req GET /drivers/DRV-1001/audits/1/compare

echo "########## 11. 批量跨司机调整：两名司机交换同时段片段（一次操作，最终排布检查）"
req POST /drivers '{"driver_id":"DRV-2001","name":"王五"}'
req POST /drivers '{"driver_id":"DRV-2002","name":"赵六"}'
req POST /segments '{"segment_id":"SEG-P","driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}'
req POST /segments '{"segment_id":"SEG-Q","driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}'
# 交换归属：逐条提交必然中途重叠，整批按最终排布检查则合法（201，每条修订号 +1）
req POST /reassignments '{"batch_id":"BATCH-0001","items":[{"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"},{"segment_id":"SEG-Q","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

echo "########## 12. 批次幂等：同批次号同内容重试 -> 200 返回首次结果；异内容 -> 409"
req POST /reassignments '{"batch_id":"BATCH-0001","items":[{"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"},{"segment_id":"SEG-Q","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'
req POST /reassignments '{"batch_id":"BATCH-0001","items":[{"segment_id":"SEG-P","expected_revision":2,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'

echo "########## 13. 整批拒绝：修订号过期（409）/ 最终排布重叠（409），均不改任何记录"
req POST /reassignments '{"batch_id":"BATCH-0002","items":[{"segment_id":"SEG-P","expected_revision":1,"driver_id":"DRV-2001","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'
# 把 SEG-Q 并到 DRV-2002 同时段：与 SEG-P 在最终排布中重叠 -> 409，SEG-Q 保持原样
req POST /reassignments '{"batch_id":"BATCH-0003","items":[{"segment_id":"SEG-Q","expected_revision":2,"driver_id":"DRV-2002","start_utc":"2026-10-06T08:00:00Z","end_utc":"2026-10-06T10:00:00Z","type":"DRIVING"}]}'
# 期望：SEG-Q 仍属 DRV-2001 且修订号保持 2（整批拒绝未改任何记录）
req GET /segments/SEG-Q

echo "########## 14. 按批次号查询已提交批次的固化结果"
req GET /reassignments/BATCH-0001
