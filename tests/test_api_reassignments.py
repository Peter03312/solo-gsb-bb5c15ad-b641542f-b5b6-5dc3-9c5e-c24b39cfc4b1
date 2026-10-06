"""批量跨司机值勤调整 API 测试。

使用 sqlite 文件库 + dependency override，无需外部服务即可运行：
    python3 -m pytest tests/test_api_reassignments.py -q

sqlite 不支持 SELECT ... FOR UPDATE（SQLAlchemy 静默忽略，单行功能测试不受影响）；
并发用例通过 BEGIN IMMEDIATE 让写事务串行化，等价于行锁下的串行执行。
"""

import os
import tempfile
import threading
import unittest

# 防止导入 app 时按默认连接串初始化 postgres 引擎（测试实际使用下面的 sqlite 引擎）
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db", prefix="duty_unused_"))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_db
from app.main import app

TEST_DB_PATH = tempfile.mktemp(suffix=".db", prefix="duty_test_")
engine = create_engine(f"sqlite:///{TEST_DB_PATH}", connect_args={"timeout": 30})


@event.listens_for(engine, "connect")
def _connect(dbapi_conn, _):
    dbapi_conn.isolation_level = None  # 关闭 DBAPI 自动 BEGIN，由下面的 begin 事件控制


@event.listens_for(engine, "begin")
def _begin(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")  # 写事务串行化，避免 sqlite 升级锁死锁


TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)  # 不使用 with：不触发 lifespan（避免连真实数据库）

DAY = "2026-10-05"


def seg(segment_id, driver_id, start, end, typ="DRIVING"):
    return {
        "segment_id": segment_id,
        "driver_id": driver_id,
        "start_utc": f"{DAY}T{start}:00Z",
        "end_utc": f"{DAY}T{end}:00Z",
        "type": typ,
    }


def item(segment_id, rev, driver_id, start, end, typ="DRIVING"):
    return {
        "segment_id": segment_id,
        "expected_revision": rev,
        "driver_id": driver_id,
        "start_utc": f"{DAY}T{start}:00Z",
        "end_utc": f"{DAY}T{end}:00Z",
        "type": typ,
    }


class ReassignmentApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        for d in ("DRV-A", "DRV-B", "DRV-C"):
            r = client.post("/drivers", json={"driver_id": d, "name": "司机" + d})
            assert r.status_code == 201, r.text

    # ------------------------------------------------------------ 辅助

    def add_segment(self, segment_id, driver_id, start, end, typ="DRIVING"):
        r = client.post("/segments", json=seg(segment_id, driver_id, start, end, typ))
        assert r.status_code == 201, r.text
        return r.json()

    def get_segment(self, segment_id):
        r = client.get(f"/segments/{segment_id}")
        assert r.status_code == 200, r.text
        return r.json()

    def submit(self, batch_id, items):
        return client.post("/reassignments", json={"batch_id": batch_id, "items": items})

    # ------------------------------------------------------------ 成功路径

    def test_swap_same_time_segments_between_drivers(self):
        """两名司机交换同一时段的片段：最终排布检查，不受提交顺序影响。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "08:00", "10:00")

        r = self.submit(
            "BATCH-SWAP",
            [
                item("SEG-1", 1, "DRV-B", "08:00", "10:00"),
                item("SEG-2", 1, "DRV-A", "08:00", "10:00"),
            ],
        )
        self.assertEqual(r.status_code, 201, r.text)
        body = r.json()
        self.assertEqual(body["batch_id"], "BATCH-SWAP")
        changes = {c["segment_id"]: c for c in body["segment_changes"]}
        self.assertEqual(changes["SEG-1"], {"segment_id": "SEG-1", "previous_revision": 1, "revision": 2})
        self.assertEqual(changes["SEG-2"], {"segment_id": "SEG-2", "previous_revision": 1, "revision": 2})
        self.assertEqual({d["driver_id"] for d in body["drivers"]}, {"DRV-A", "DRV-B"})
        for d in body["drivers"]:
            self.assertTrue(d["before"]["compliant"])
            self.assertTrue(d["after"]["compliant"])

        # 落库状态：归属互换、修订号各 +1
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-B")
        self.assertEqual(self.get_segment("SEG-2")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 2)
        self.assertEqual(self.get_segment("SEG-2")["revision"], 2)

    def test_swap_adjacent_segments(self):
        """交换首尾相接的片段：相接合法。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "10:00", "12:00")
        r = self.submit(
            "BATCH-ADJ",
            [
                item("SEG-1", 1, "DRV-B", "08:00", "10:00"),
                item("SEG-2", 1, "DRV-A", "10:00", "12:00"),
            ],
        )
        self.assertEqual(r.status_code, 201, r.text)

    def test_move_and_retime_with_compliance_delta(self):
        """移动并重排时间：响应携带受影响司机调整前后的实时合规结论。"""
        # DRV-A 连续驾驶 4h（合规）；把 DRV-B 的 1h 驾驶并到 DRV-A 尾部 -> 累计 5h 越限
        self.add_segment("SEG-1", "DRV-A", "08:00", "12:00")
        self.add_segment("SEG-2", "DRV-B", "12:00", "13:00")
        r = self.submit(
            "BATCH-MOVE",
            [item("SEG-2", 1, "DRV-A", "12:00", "13:00")],
        )
        self.assertEqual(r.status_code, 201, r.text)
        drivers = {d["driver_id"]: d for d in r.json()["drivers"]}
        self.assertEqual(set(drivers), {"DRV-A", "DRV-B"})
        self.assertTrue(drivers["DRV-A"]["before"]["compliant"])
        self.assertFalse(drivers["DRV-A"]["after"]["compliant"])
        rules = {v["rule"] for v in drivers["DRV-A"]["after"]["violations"]}
        self.assertIn("CONTINUOUS_DRIVING_LIMIT", rules)
        self.assertEqual(drivers["DRV-A"]["after"]["violations"][0]["first_violation_at"], f"{DAY}T12:00:00Z")
        self.assertTrue(drivers["DRV-B"]["before"]["compliant"])
        self.assertTrue(drivers["DRV-B"]["after"]["compliant"])

    def test_type_can_change_in_batch(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit("BATCH-TYPE", [item("SEG-1", 1, "DRV-A", "08:00", "10:00", "REST")])
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.get_segment("SEG-1")["type"], "REST")

    # ------------------------------------------------------------ 幂等与冲突

    def test_idempotent_retry_same_content(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        payload = [item("SEG-1", 1, "DRV-B", "08:00", "10:00")]
        r1 = self.submit("BATCH-IDEM", payload)
        r2 = self.submit("BATCH-IDEM", payload)
        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json(), r2.json())  # 返回首次提交的结果
        self.assertEqual(self.get_segment("SEG-1")["revision"], 2)  # 只加一次

    def test_item_order_does_not_affect_idempotency(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "08:00", "10:00")
        r1 = self.submit(
            "BATCH-ORDER",
            [item("SEG-1", 1, "DRV-B", "08:00", "10:00"), item("SEG-2", 1, "DRV-A", "08:00", "10:00")],
        )
        r2 = self.submit(
            "BATCH-ORDER",
            [item("SEG-2", 1, "DRV-A", "08:00", "10:00"), item("SEG-1", 1, "DRV-B", "08:00", "10:00")],
        )
        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r1.json(), r2.json())

    def test_same_batch_id_different_content_conflicts(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r1 = self.submit("BATCH-CONFLICT", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        r2 = self.submit("BATCH-CONFLICT", [item("SEG-1", 2, "DRV-C", "08:00", "10:00")])
        self.assertEqual(r1.status_code, 201, r1.text)
        self.assertEqual(r2.status_code, 409, r2.text)
        self.assertEqual(r2.json()["detail"]["error"], "batch_conflict")
        # 冲突重放不产生任何副作用
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-B")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 2)

    def test_get_batch_result(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit("BATCH-GET", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        self.assertEqual(r.status_code, 201, r.text)
        g = client.get("/reassignments/BATCH-GET")
        self.assertEqual(g.status_code, 200, g.text)
        self.assertEqual(g.json(), r.json())
        self.assertEqual(client.get("/reassignments/NOPE").status_code, 404)

    # ------------------------------------------------------------ 整批拒绝

    def test_missing_segment_rejects_all(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit(
            "BATCH-MISS-SEG",
            [item("SEG-1", 1, "DRV-B", "08:00", "10:00"), item("SEG-GHOST", 1, "DRV-B", "09:00", "10:00")],
        )
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["detail"]["missing_segment_ids"], ["SEG-GHOST"])
        # 整批拒绝：合法条目也不生效
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 1)

    def test_stale_revision_rejects_all(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-A", "10:00", "11:00")
        r = self.submit(
            "BATCH-STALE",
            [item("SEG-1", 1, "DRV-B", "08:00", "10:00"), item("SEG-2", 7, "DRV-B", "10:00", "11:00")],
        )
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["error"], "revision_conflict")
        self.assertEqual(
            detail["conflicts"],
            [{"segment_id": "SEG-2", "current_revision": 1, "expected_revision": 7}],
        )
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 1)
        self.assertEqual(self.get_segment("SEG-2")["revision"], 1)

    def test_missing_target_driver_rejects_all(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit("BATCH-MISS-DRV", [item("SEG-1", 1, "DRV-GHOST", "08:00", "10:00")])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertEqual(r.json()["detail"]["missing_driver_ids"], ["DRV-GHOST"])
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 1)

    def test_final_overlap_rejects_all(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "09:00", "11:00")
        r = self.submit("BATCH-OVERLAP", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        self.assertEqual(r.status_code, 409, r.text)
        detail = r.json()["detail"]
        self.assertEqual(detail["error"], "segment_overlap")
        self.assertEqual(detail["driver_id"], "DRV-B")
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-1")["revision"], 1)

    def test_overlap_between_items_of_same_batch_rejects(self):
        """同批两条目移到同一司机且时间重叠：最终排布检查拒绝。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "09:00", "11:00")
        r = self.submit(
            "BATCH-SELF-OVERLAP",
            [item("SEG-1", 1, "DRV-C", "08:00", "10:00"), item("SEG-2", 1, "DRV-C", "09:00", "11:00")],
        )
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self.get_segment("SEG-1")["driver_id"], "DRV-A")
        self.assertEqual(self.get_segment("SEG-2")["driver_id"], "DRV-B")

    def test_failed_batch_does_not_consume_batch_id(self):
        """校验失败的批次不留记录：修正内容后可用同一批次号重新提交。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "09:00", "11:00")
        r1 = self.submit("BATCH-RETRY", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        self.assertEqual(r1.status_code, 409, r1.text)
        self.assertEqual(client.get("/reassignments/BATCH-RETRY").status_code, 404)
        r2 = self.submit("BATCH-RETRY", [item("SEG-1", 1, "DRV-B", "11:00", "12:00")])
        self.assertEqual(r2.status_code, 201, r2.text)

    # ------------------------------------------------------------ 入参校验（422）

    def test_duplicate_segment_in_batch_422(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit(
            "BATCH-DUP",
            [item("SEG-1", 1, "DRV-B", "08:00", "10:00"), item("SEG-1", 1, "DRV-C", "08:00", "10:00")],
        )
        self.assertEqual(r.status_code, 422, r.text)

    def test_invalid_time_422(self):
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit("BATCH-BADTIME", [item("SEG-1", 1, "DRV-B", "10:00", "08:00")])
        self.assertEqual(r.status_code, 422, r.text)
        naive = item("SEG-1", 1, "DRV-B", "08:00", "10:00")
        naive["start_utc"] = "2026-10-05T08:00:00"  # 缺时区
        r = self.submit("BATCH-NAIVE", [naive])
        self.assertEqual(r.status_code, 422, r.text)

    def test_empty_items_422(self):
        r = self.submit("BATCH-EMPTY", [])
        self.assertEqual(r.status_code, 422, r.text)

    # ------------------------------------------------------------ 并发

    def test_concurrent_identical_batches_apply_once(self):
        """并发同批次号同内容：一次 201 一次 200，修订号只加一。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        payload = {"batch_id": "BATCH-RACE-SAME", "items": [item("SEG-1", 1, "DRV-B", "08:00", "10:00")]}
        results = []

        def work():
            r = client.post("/reassignments", json=payload)
            results.append((r.status_code, r.json()))

        threads = [threading.Thread(target=work) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [200, 201], results)
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.get_segment("SEG-1")["revision"], 2)

    def test_concurrent_batches_cannot_bypass_revision_check(self):
        """并发异批次争用同一片段：一个成功，另一个修订号冲突，不得双双生效。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        bodies = [
            {"batch_id": "BATCH-RACE-1", "items": [item("SEG-1", 1, "DRV-B", "08:00", "10:00")]},
            {"batch_id": "BATCH-RACE-2", "items": [item("SEG-1", 1, "DRV-C", "08:00", "10:00")]},
        ]
        results = []

        def work(p):
            r = client.post("/reassignments", json=p)
            results.append((r.status_code, r.json()))

        threads = [threading.Thread(target=work, args=(p,)) for p in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [201, 409], results)
        loser = [b for c, b in results if c == 409][0]
        self.assertEqual(loser["detail"]["error"], "revision_conflict")
        # 只有一个批次生效
        final = self.get_segment("SEG-1")
        self.assertEqual(final["revision"], 2)
        self.assertIn(final["driver_id"], ("DRV-B", "DRV-C"))

    def test_concurrent_batches_cannot_bypass_overlap_check(self):
        """并发异批次向同一司机并入重叠片段：一个成功，另一个最终排布重叠被拒。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        self.add_segment("SEG-2", "DRV-B", "09:00", "11:00")
        bodies = [
            {"batch_id": "BATCH-RACE-OV1", "items": [item("SEG-1", 1, "DRV-C", "08:00", "10:00")]},
            {"batch_id": "BATCH-RACE-OV2", "items": [item("SEG-2", 1, "DRV-C", "09:00", "11:00")]},
        ]
        results = []

        def work(p):
            r = client.post("/reassignments", json=p)
            results.append((r.status_code, r.json()))

        threads = [threading.Thread(target=work, args=(p,)) for p in bodies]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(c for c, _ in results)
        self.assertEqual(codes, [201, 409], results)
        loser = [b for c, b in results if c == 409][0]
        self.assertEqual(loser["detail"]["error"], "segment_overlap")
        # DRV-C 最终只持有其中一个片段，无重叠
        segs_c = client.get("/drivers/DRV-C/segments").json()
        self.assertEqual(len(segs_c), 1)

    # ------------------------------------------------------------ 与既有功能共存

    def test_single_patch_and_queries_still_work(self):
        """原有单片段更正与查询在批量调整后继续可用。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        r = self.submit("BATCH-COEXIST", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        self.assertEqual(r.status_code, 201, r.text)
        # 单片段更正按新修订号继续可用
        p = client.patch("/segments/SEG-1", json={"expected_revision": 2, "end_utc": f"{DAY}T10:30:00Z"})
        self.assertEqual(p.status_code, 200, p.text)
        self.assertEqual(p.json()["revision"], 3)
        # 合规查询与片段列表正常
        self.assertEqual(client.get("/drivers/DRV-B/compliance").status_code, 200)
        self.assertEqual(len(client.get("/drivers/DRV-B/segments").json()), 1)

    def test_published_audits_untouched_by_batch(self):
        """已发布审核版本保持原样；compare 能反映批次带来的修订变化。"""
        self.add_segment("SEG-1", "DRV-A", "08:00", "10:00")
        pub = client.post("/drivers/DRV-A/audits")
        self.assertEqual(pub.status_code, 201, pub.text)
        audit_id = pub.json()["snapshot"]["audit_id"]

        r = self.submit("BATCH-AUDIT", [item("SEG-1", 1, "DRV-B", "08:00", "10:00")])
        self.assertEqual(r.status_code, 201, r.text)

        # 旧版本内容不变
        snap = client.get(f"/drivers/DRV-A/audits/{audit_id}").json()
        self.assertEqual(snap["input_revisions"], [{"segment_id": "SEG-1", "revision": 1}])
        # compare：SEG-1 已移出 DRV-A（removed），DRV-B 视角为 added
        cmp_a = client.get(f"/drivers/DRV-A/audits/{audit_id}/compare").json()
        self.assertEqual(cmp_a["input_changes"]["removed"], ["SEG-1"])
        pub_b = client.post("/drivers/DRV-B/audits")
        self.assertEqual(pub_b.status_code, 201, pub.text)
        self.assertEqual(
            pub_b.json()["snapshot"]["input_revisions"],
            [{"segment_id": "SEG-1", "revision": 2}],
        )


if __name__ == "__main__":
    unittest.main()
