"""用户面板 + 捐赠人面板的聚合查询与批量动作单测（spec 2026-05-27）。

覆盖：
- list_payout_users：聚合金额、cnt_* 分项小计、过滤口径、身份列回退
- list_donors：抽奖次数、消耗金币向上取整、最大单次出货、USD 累计
- mark_user_all_confirmed_to_paid：只动 confirmed、payout_paid_at 写入、并发安全 WHERE
- 排序白名单：未知 key 回退默认列、direction 仅识 asc/desc

跑：python3 -m unittest tests.test_admin_users
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 必须在 import db / server 之前指定 DB 路径，避免触碰 prod / 默认 /data。
_TMP_DB = Path(tempfile.mkdtemp(prefix="probe-test-")) / "test.sqlite3"
os.environ["PROBE_DB_PATH"] = str(_TMP_DB)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


def _reset_db() -> None:
    """每个 test case 起一个干净 DB（删表 + 重建）。"""
    conn = db.get_conn()
    # 关掉外键约束以便 DROP 顺序无所谓
    conn.execute("PRAGMA foreign_keys=OFF")
    for tbl in ("coin_donations", "feedback", "sessions",
                "invite_tokens", "recruit_batches", "projects"):
        conn.execute(f"DROP TABLE IF EXISTS {tbl}")
    conn.execute("PRAGMA foreign_keys=ON")
    db.init_schema()


def _mk_project(slug: str = "demo") -> None:
    db.upsert_project(slug, slug, "desc", "https://example.com/",
                      100, None)
    # invite_tokens 需要绑定 project 才能创建 session
    db.upsert_invite_token(f"tk-{slug}", slug, 0)


def _mk_session(sid: str, slug: str = "demo") -> None:
    now = int(time.time())
    db.get_conn().execute(
        "INSERT INTO sessions(session_id, project_slug, invite_token, "
        "started_at, expires_at) VALUES (?,?,?,?,?)",
        (sid, slug, f"tk-{slug}", now, now + 3600),
    )


def _insert_feedback(*, sid: str, slug: str, wechat_id: str | None,
                     payout: str, credit_suggested: int | None = None,
                     credit_confirmed: int | None = None,
                     submitted_at: int | None = None,
                     version: str = "v1") -> int:
    """直接 INSERT 绕过 server 业务层，方便构造各种状态。

    version 默认 v1；同 (slug, version, wechat_id) 有 UNIQUE 索引，因此
    模拟"同一人多条"需要传入不同版本或不同项目。
    """
    now = submitted_at or int(time.time())
    wh = db.wechat_hash(wechat_id) if wechat_id else None
    cur = db.get_conn().execute(
        """INSERT INTO feedback(session_id, project_slug, wechat_id,
             wechat_hash, project_version,
             q1_answer,q2_answer,q3_answer,q4_answer,
             submitted_at, ai_status, payout_status,
             credit_suggested, credit_confirmed)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, slug, wechat_id, wh, version,
         "a", "b", "c", "d",
         now, "done" if credit_suggested is not None else "pending",
         payout, credit_suggested, credit_confirmed),
    )
    return cur.lastrowid


class TestListPayoutUsers(unittest.TestCase):

    def setUp(self):
        _reset_db()
        _mk_project("demo")

    def test_aggregates_per_user(self):
        """同一 wechat_id 多条反馈（跨版本）：金额按 payout_status 分桶累加。"""
        _mk_session("s1")
        _mk_session("s2")
        _mk_session("s3")
        _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                         payout="suggested", credit_suggested=6, version="v1")
        _insert_feedback(sid="s2", slug="demo", wechat_id="alice",
                         payout="confirmed", credit_suggested=9,
                         credit_confirmed=10, version="v2")
        _insert_feedback(sid="s3", slug="demo", wechat_id="alice",
                         payout="paid", credit_suggested=12,
                         credit_confirmed=12, version="v3")
        rows = db.list_payout_users()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["total_feedback"], 3)
        self.assertEqual(r["cnt_suggested"], 1)
        self.assertEqual(r["cnt_confirmed"], 1)
        self.assertEqual(r["cnt_paid"], 1)
        self.assertEqual(r["amount_suggested"], 6)
        self.assertEqual(r["amount_confirmed"], 10)
        self.assertEqual(r["amount_paid"], 12)
        self.assertEqual(r["last_wechat_id"], "alice")

    def test_excludes_fully_paid_users(self):
        """全部 paid 完的人不出现（没有 suggested/confirmed）。"""
        _mk_session("s1")
        _insert_feedback(sid="s1", slug="demo", wechat_id="bob",
                         payout="paid", credit_suggested=5, credit_confirmed=5)
        self.assertEqual(db.list_payout_users(), [])

    def test_excludes_null_wechat_hash(self):
        """wechat_hash IS NULL（30 天 PII 清理后又没 hash 回填）的反馈忽略。"""
        _mk_session("s1")
        cur = db.get_conn().execute(
            """INSERT INTO feedback(session_id, project_slug, wechat_id,
                 wechat_hash, project_version,
                 q1_answer,q2_answer,q3_answer,q4_answer,
                 submitted_at, ai_status, payout_status, credit_suggested)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("s1", "demo", None, None, "v1", "a", "b", "c", "d",
             int(time.time()), "done", "suggested", 5),
        )
        self.assertEqual(db.list_payout_users(), [])

    def test_ident_falls_back_after_purge(self):
        """wechat_id 被清后 last_wechat_id=NULL，但行还在（hash 存活）。"""
        _mk_session("s1")
        _mk_session("s2")
        fid1 = _insert_feedback(sid="s1", slug="demo", wechat_id="cara",
                                payout="suggested", credit_suggested=5,
                                version="v1")
        # 第二条同一 wechat 跨版本（避开 uniq_wechat_hash_per_project_version）
        _insert_feedback(sid="s2", slug="demo", wechat_id="cara",
                         payout="confirmed", credit_suggested=7,
                         credit_confirmed=8, version="v2")
        # 清掋第一条的 wechat_id（模拟 30 天 purge 命中早条）
        db.get_conn().execute(
            "UPDATE feedback SET wechat_id=NULL WHERE id=?", (fid1,))
        rows = db.list_payout_users()
        self.assertEqual(len(rows), 1)
        # 仍能拿到非 NULL 的最新 wechat_id
        self.assertEqual(rows[0]["last_wechat_id"], "cara")

        # 把两条 wechat_id 都清掋 → last_wechat_id=NULL
        db.get_conn().execute("UPDATE feedback SET wechat_id=NULL")
        rows = db.list_payout_users()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["last_wechat_id"])
        # 但 wechat_hash 还在
        self.assertIsNotNone(rows[0]["wechat_hash"])

    def test_sort_whitelist_safe(self):
        """未知 sort key 回退默认列，不抛错也不让 SQL 注入。"""
        _mk_session("s1")
        _insert_feedback(sid="s1", slug="demo", wechat_id="d",
                         payout="suggested", credit_suggested=3)
        # 默认排序应等同
        a = db.list_payout_users()
        b = db.list_payout_users(sort="garbage; DROP TABLE feedback")
        self.assertEqual([r["wechat_hash"] for r in a],
                         [r["wechat_hash"] for r in b])
        # 表还在
        self.assertIsNotNone(db.get_conn().execute(
            "SELECT 1 FROM feedback").fetchone())

    def test_sort_direction_asc_vs_desc(self):
        """两人 due 不同，asc/desc 颠倒顺序。"""
        for i, (sid, w, c) in enumerate([("s1", "x", 5), ("s2", "y", 12)]):
            _mk_session(sid)
            _insert_feedback(sid=sid, slug="demo", wechat_id=w,
                             payout="confirmed", credit_suggested=c,
                             credit_confirmed=c)
        desc = [r["amount_confirmed"] for r in
                db.list_payout_users(sort="due", direction="desc")]
        asc = [r["amount_confirmed"] for r in
               db.list_payout_users(sort="due", direction="asc")]
        self.assertEqual(desc, [12, 5])
        self.assertEqual(asc, [5, 12])


class TestListDonors(unittest.TestCase):

    def setUp(self):
        _reset_db()
        _mk_project("demo")
        _mk_session("s1")
        self.fid = _insert_feedback(
            sid="s1", slug="demo", wechat_id="alice",
            payout="paid", credit_suggested=5, credit_confirmed=5)
        self.wh = db.wechat_hash("alice")

    def _donate(self, n: int, *, mult_pct: int = 100,
                usd_cents: int = 50) -> None:
        """模拟 n 次抽奖（每次都同样的 nonce 不行——这里逐次递增）。"""
        for i in range(n):
            db.record_donation(
                wechat_hash=self.wh, source_feedback_id=self.fid,
                donor_label="alice", slot_landed=6, multiplier_pct=mult_pct,
                usd_cents=usd_cents, server_seed="ss" * 32,
                server_seed_hash="hh" * 32, client_seed="c", nonce=i)

    def test_aggregation(self):
        # alice earned 5 coins → 50 抽奖额度
        self._donate(15, usd_cents=50)  # 15 抽 = ceil(15/10)=2 金币消耗
        rows = db.list_donors()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["donation_count"], 15)
        self.assertEqual(r["coins_consumed"], 2)
        self.assertEqual(r["donated_usd_cents"], 15 * 50)
        self.assertEqual(r["biggest_hit_cents"], 50)
        self.assertEqual(r["last_wechat_id"], "alice")

    def test_exact_boundary_10_consumes_1_coin(self):
        """边界：抽 10 次 = 消耗 1 金币（10 不向上取到 2）。"""
        self._donate(10)
        rows = db.list_donors()
        self.assertEqual(rows[0]["donation_count"], 10)
        self.assertEqual(rows[0]["coins_consumed"], 1)

    def test_exact_boundary_11_consumes_2_coins(self):
        """边界：抽 11 次 = 消耗 2 金币（开始锁第 2 枚）。"""
        self._donate(11)
        rows = db.list_donors()
        self.assertEqual(rows[0]["coins_consumed"], 2)

    def test_sort_whitelist(self):
        self._donate(3)
        a = db.list_donors()
        b = db.list_donors(sort="' OR 1=1 --")
        self.assertEqual([r["wechat_hash"] for r in a],
                         [r["wechat_hash"] for r in b])


class TestMarkUserAllConfirmedToPaid(unittest.TestCase):

    def setUp(self):
        _reset_db()
        _mk_project("demo")

    def test_only_touches_confirmed(self):
        """suggested / rejected / 已 paid 都不动，仅 confirmed → paid。"""
        # 同一 tester 5 条反馈跨 v1-v5（避开 uniq_wechat_hash_per_project_version）
        for i, (sid, payout, c_s, c_c) in enumerate([
            ("s1", "suggested", 5, None),
            ("s2", "confirmed", 6, 7),
            ("s3", "confirmed", 8, 9),
            ("s4", "paid", 10, 10),
            ("s5", "rejected", None, None),
        ]):
            _mk_session(sid)
            _insert_feedback(sid=sid, slug="demo", wechat_id="alice",
                             payout=payout,
                             credit_suggested=c_s, credit_confirmed=c_c,
                             version=f"v{i+1}")
        wh = db.wechat_hash("alice")
        n = db.mark_user_all_confirmed_to_paid(wh)
        self.assertEqual(n, 2)
        # 验证状态：s2/s3 → paid，其它不变
        rows = {r["session_id"]: r for r in
                db.get_conn().execute(
                    "SELECT session_id, payout_status, payout_paid_at "
                    "FROM feedback WHERE wechat_hash=?", (wh,))}
        self.assertEqual(rows["s1"]["payout_status"], "suggested")
        self.assertEqual(rows["s2"]["payout_status"], "paid")
        self.assertIsNotNone(rows["s2"]["payout_paid_at"])
        self.assertEqual(rows["s3"]["payout_status"], "paid")
        self.assertIsNotNone(rows["s3"]["payout_paid_at"])
        self.assertEqual(rows["s4"]["payout_status"], "paid")
        self.assertEqual(rows["s5"]["payout_status"], "rejected")

    def test_only_targets_given_user(self):
        """不影响其他 wechat_hash。"""
        _mk_session("a1")
        _mk_session("b1")
        _insert_feedback(sid="a1", slug="demo", wechat_id="alice",
                         payout="confirmed", credit_suggested=5,
                         credit_confirmed=5)
        _insert_feedback(sid="b1", slug="demo", wechat_id="bob",
                         payout="confirmed", credit_suggested=7,
                         credit_confirmed=7)
        wh_alice = db.wechat_hash("alice")
        wh_bob = db.wechat_hash("bob")
        n = db.mark_user_all_confirmed_to_paid(wh_alice)
        self.assertEqual(n, 1)
        bob_row = db.get_conn().execute(
            "SELECT payout_status FROM feedback WHERE wechat_hash=?",
            (wh_bob,)).fetchone()
        self.assertEqual(bob_row["payout_status"], "confirmed")

    def test_returns_zero_when_nothing_to_pay(self):
        """没有 confirmed 时返回 0，不抛错。"""
        wh = db.wechat_hash("nobody")
        self.assertEqual(db.mark_user_all_confirmed_to_paid(wh), 0)


class TestSortWhitelistMaps(unittest.TestCase):
    """PAYOUT_USER_SORT / DONOR_SORT 是常量字典，防止注入靠白名单。"""

    def test_keys_have_no_sql_chars(self):
        import re as _re
        safe = _re.compile(r"^[a-z_]+$")
        for k in list(db.PAYOUT_USER_SORT) + list(db.DONOR_SORT):
            self.assertRegex(k, safe.pattern)

    def test_values_have_no_semicolons(self):
        for v in (list(db.PAYOUT_USER_SORT.values())
                  + list(db.DONOR_SORT.values())):
            self.assertNotIn(";", v)
            self.assertNotIn("--", v)


if __name__ == "__main__":
    unittest.main()
