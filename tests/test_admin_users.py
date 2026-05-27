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

    def test_coins_consumed_reflected(self):
        """已抽公益站的 tester：coins_consumed 字段按 ceil(抽奖/10) 算（与 coin_balance 一致）。

        修 codex P1 #1：若不在面板扣减，作者会按毛额转账，多付已捐到公益站的金币。
        """
        _mk_session("s1")
        fid = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                               payout="confirmed", credit_suggested=10,
                               credit_confirmed=10)
        wh = db.wechat_hash("alice")
        # 抽 7 次 = 消耗 1 整块金币（向上取整）
        for i in range(7):
            db.record_donation(
                wechat_hash=wh, source_feedback_id=fid,
                donor_label="alice", slot_landed=6, multiplier_pct=100,
                usd_cents=50, server_seed="s" * 64,
                server_seed_hash="h" * 64, client_seed="c", nonce=i)
        rows = db.list_payout_users()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_confirmed"], 10)
        self.assertEqual(rows[0]["coins_consumed"], 1)

    def test_due_sort_uses_net_after_coin_consumption(self):
        """due 排序应按 net = suggested + max(0, confirmed - coins_consumed)。

        构造：alice confirmed ¥10 + 抽 1 次（净 ¥9），bob confirmed ¥8 + 0 抽（净 ¥8）。
        毛额：alice 10 > bob 8；净额：alice 9 > bob 8。仍是 alice 在前，但若以后
        某 tester 抽到 net 反超，这个 case 会保护我们不出错。
        """
        _mk_session("a1")
        _mk_session("b1")
        fid_a = _insert_feedback(sid="a1", slug="demo", wechat_id="alice",
                                 payout="confirmed", credit_suggested=10,
                                 credit_confirmed=10)
        _insert_feedback(sid="b1", slug="demo", wechat_id="bob",
                         payout="confirmed", credit_suggested=8,
                         credit_confirmed=8)
        db.record_donation(
            wechat_hash=db.wechat_hash("alice"), source_feedback_id=fid_a,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        rows = db.list_payout_users(sort="due", direction="desc")
        self.assertEqual(len(rows), 2)
        # alice 净 9 在前
        self.assertEqual(rows[0]["last_wechat_id"], "alice")
        self.assertEqual(rows[0]["coins_consumed"], 1)
        self.assertEqual(rows[1]["coins_consumed"], 0)


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


class TestMarkSpecificConfirmedToPaid(unittest.TestCase):
    """精确 ID 快照版本（修 codex 二轮 P1）：count 校验抵不住等量替换攻击。"""

    def setUp(self):
        _reset_db()
        _mk_project("demo")

    def test_exact_ids_match(self):
        """快照 ids 与 DB 完全一致 → 全部标 paid。"""
        ids = []
        for i, sid in enumerate(["s1", "s2", "s3"]):
            _mk_session(sid)
            ids.append(_insert_feedback(
                sid=sid, slug="demo", wechat_id="alice",
                payout="confirmed", credit_suggested=5, credit_confirmed=5,
                version=f"v{i+1}"))
        wh = db.wechat_hash("alice")
        n = db.mark_user_specific_confirmed_to_paid(wh, ids)
        self.assertEqual(n, 3)

    def test_extra_id_in_db_rejects(self):
        """快照只含 {1,2}，DB 实际还有第 3 个 confirmed（等量替换前奏）→ 拒绝。"""
        for i, sid in enumerate(["s1", "s2", "s3"]):
            _mk_session(sid)
            _insert_feedback(sid=sid, slug="demo", wechat_id="alice",
                             payout="confirmed", credit_suggested=5,
                             credit_confirmed=5, version=f"v{i+1}")
        wh = db.wechat_hash("alice")
        rows = db.get_conn().execute(
            "SELECT id FROM feedback WHERE wechat_hash=? "
            "AND payout_status='confirmed' ORDER BY id", (wh,)).fetchall()
        # 故意漏掉最后一个 id
        partial_ids = [r["id"] for r in rows[:-1]]
        with self.assertRaises(db.StaleSnapshotError):
            db.mark_user_specific_confirmed_to_paid(wh, partial_ids)
        # DB 完全没被动：仍是 3 条 confirmed
        cnt = db.get_conn().execute(
            "SELECT COUNT(*) AS c FROM feedback "
            "WHERE wechat_hash=? AND payout_status='confirmed'",
            (wh,)).fetchone()["c"]
        self.assertEqual(cnt, 3)

    def test_equal_count_different_set_rejects(self):
        """codex 二轮 P1 关键 case：A 行被付掉 + B 行新晋 → count 同 set 异 → 必须拒。"""
        for i, sid in enumerate(["s1", "s2"]):
            _mk_session(sid)
            _insert_feedback(sid=sid, slug="demo", wechat_id="alice",
                             payout="confirmed", credit_suggested=5,
                             credit_confirmed=5, version=f"v{i+1}")
        wh = db.wechat_hash("alice")
        snapshot_ids = [r["id"] for r in db.get_conn().execute(
            "SELECT id FROM feedback WHERE wechat_hash=? "
            "AND payout_status='confirmed' ORDER BY id", (wh,)).fetchall()]
        # 模拟「在另一 tab 把 snapshot_ids[0] 标已付 + 新晋一条 confirmed」
        db.get_conn().execute(
            "UPDATE feedback SET payout_status='paid', payout_paid_at=? "
            "WHERE id=?", (int(time.time()), snapshot_ids[0]))
        _mk_session("s3")
        new_id = _insert_feedback(
            sid="s3", slug="demo", wechat_id="alice",
            payout="confirmed", credit_suggested=5, credit_confirmed=5,
            version="v3")
        # 当前真实 confirmed = {snapshot_ids[1], new_id}，count = 2 = 旧快照 count
        # 但 set 完全不同 → 必须拒绝
        with self.assertRaises(db.StaleSnapshotError):
            db.mark_user_specific_confirmed_to_paid(wh, snapshot_ids)
        # 关键验证：new_id 没被悄悄付掉
        new_row = db.get_conn().execute(
            "SELECT payout_status FROM feedback WHERE id=?",
            (new_id,)).fetchone()
        self.assertEqual(new_row["payout_status"], "confirmed")

    def test_empty_ids_with_empty_db_returns_zero(self):
        """expected_ids=[] + DB 也 0 confirmed → 放行返 0（noop）。"""
        wh = db.wechat_hash("nobody")
        self.assertEqual(
            db.mark_user_specific_confirmed_to_paid(wh, []), 0)

    def test_cross_user_id_does_not_leak(self):
        """传入别人的 confirmed id → 拒（dataset 包含但 wechat 不匹配 → 视为 stale）。"""
        _mk_session("a1")
        _mk_session("b1")
        fid_a = _insert_feedback(sid="a1", slug="demo", wechat_id="alice",
                                 payout="confirmed", credit_suggested=5,
                                 credit_confirmed=5)
        fid_b = _insert_feedback(sid="b1", slug="demo", wechat_id="bob",
                                 payout="confirmed", credit_suggested=7,
                                 credit_confirmed=7)
        wh_alice = db.wechat_hash("alice")
        # 把 bob 的 fid_b 塞进 alice 的 snapshot —— actual_ids for alice = {fid_a}
        # expected = {fid_a, fid_b} → 不等 → 拒
        with self.assertRaises(db.StaleSnapshotError):
            db.mark_user_specific_confirmed_to_paid(
                wh_alice, [fid_a, fid_b])
        # bob 的 feedback 仍是 confirmed
        bob_row = db.get_conn().execute(
            "SELECT payout_status FROM feedback WHERE id=?",
            (fid_b,)).fetchone()
        self.assertEqual(bob_row["payout_status"], "confirmed")

    def test_only_touches_confirmed_status(self):
        """suggested / rejected / 已 paid 都不动；仅 expected_ids 列出的 confirmed → paid。"""
        # 同一 tester 5 条反馈跨 v1-v5
        rows_to_make = [
            ("s1", "suggested", 5, None),
            ("s2", "confirmed", 6, 7),
            ("s3", "confirmed", 8, 9),
            ("s4", "paid", 10, 10),
            ("s5", "rejected", None, None),
        ]
        confirmed_ids = []
        for i, (sid, payout, c_s, c_c) in enumerate(rows_to_make):
            _mk_session(sid)
            fid = _insert_feedback(sid=sid, slug="demo", wechat_id="alice",
                                   payout=payout,
                                   credit_suggested=c_s, credit_confirmed=c_c,
                                   version=f"v{i+1}")
            if payout == "confirmed":
                confirmed_ids.append(fid)
        wh = db.wechat_hash("alice")
        n = db.mark_user_specific_confirmed_to_paid(wh, confirmed_ids)
        self.assertEqual(n, 2)
        rows = {r["session_id"]: r for r in db.get_conn().execute(
            "SELECT session_id, payout_status, payout_paid_at "
            "FROM feedback WHERE wechat_hash=?", (wh,))}
        self.assertEqual(rows["s1"]["payout_status"], "suggested")
        self.assertEqual(rows["s2"]["payout_status"], "paid")
        self.assertIsNotNone(rows["s2"]["payout_paid_at"])
        self.assertEqual(rows["s3"]["payout_status"], "paid")
        self.assertIsNotNone(rows["s3"]["payout_paid_at"])
        self.assertEqual(rows["s4"]["payout_status"], "paid")
        self.assertEqual(rows["s5"]["payout_status"], "rejected")

    def test_settles_pending_donations_in_same_transaction(self):
        """mark-paid 同事务把该用户所有 settled_at IS NULL 的捐赠 → settled_at=NOW。

        修 codex 三轮 P1：用列状态而非时间戳比较，杜绝同秒竞争。
        """
        _mk_session("s1")
        fid = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                               payout="confirmed", credit_suggested=10,
                               credit_confirmed=10)
        wh = db.wechat_hash("alice")
        for i in range(3):
            db.record_donation(
                wechat_hash=wh, source_feedback_id=fid,
                donor_label="alice", slot_landed=6, multiplier_pct=100,
                usd_cents=50, server_seed="s" * 64,
                server_seed_hash="h" * 64, client_seed="c", nonce=i)
        # 三条捐赠 settled_at 都 NULL（record_donation 默认）
        rows = db.get_conn().execute(
            "SELECT settled_at FROM coin_donations WHERE wechat_hash=?",
            (wh,)).fetchall()
        self.assertTrue(all(r["settled_at"] is None for r in rows))
        # mark-paid 后全部 settled
        db.mark_user_specific_confirmed_to_paid(wh, [fid])
        rows = db.get_conn().execute(
            "SELECT settled_at FROM coin_donations WHERE wechat_hash=?",
            (wh,)).fetchall()
        self.assertTrue(all(r["settled_at"] is not None for r in rows))


class TestCrossCycleCoinSettlement(unittest.TestCase):
    """修 codex 二轮 P2：跨 payout 周期的金币归零（不再重复扣已结清的捐赠）。"""

    def setUp(self):
        _reset_db()
        _mk_project("demo")

    def test_coin_balance_withdrawable_resets_after_paid(self):
        """上一轮 payout 完成 → 之前的捐赠不再扣减下一轮的 withdrawable。

        场景：alice 在 v1 拿 ¥10 confirmed，捐 1 金币 → wd=¥9；author 付了 ¥9
        （feedback 转 paid）；alice 在 v2 又拿 ¥5 confirmed，没再捐 → wd 应是 ¥5，
        不能再扣那 1 已结清的金币。
        """
        # v1：confirmed ¥10
        _mk_session("s1")
        fid1 = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=10,
                                credit_confirmed=10, version="v1")
        wh = db.wechat_hash("alice")
        # 抽 1 次（消耗 1 金币）
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid1,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        # 中间断言：wd = 10 - 1 = 9
        self.assertEqual(db.coin_balance(wh)["withdrawable"], 9)
        # author 标已付（必须设置 payout_paid_at）
        db.mark_user_specific_confirmed_to_paid(wh, [fid1])
        # 此时 confirmed_total=0 → wd=0
        self.assertEqual(db.coin_balance(wh)["withdrawable"], 0)
        # v2：新增 ¥5 confirmed，并不再捐
        _mk_session("s2")
        _insert_feedback(sid="s2", slug="demo", wechat_id="alice",
                         payout="confirmed", credit_suggested=5,
                         credit_confirmed=5, version="v2")
        # 关键断言：v2 的 wd 应是 5（不扣已结清的 1 金币）
        bal = db.coin_balance(wh)
        self.assertEqual(bal["withdrawable"], 5)
        # consumed_coins (lifetime) 仍是 1（展示用，不重置）
        self.assertEqual(bal["consumed_coins"], 1)
        # consumed_coins_unsettled = 0（本期 0 捐赠）
        self.assertEqual(bal["consumed_coins_unsettled"], 0)

    def test_list_payout_users_same_cross_cycle_semantics(self):
        """同上场景在 admin 面板：v2 panel 不能再扣那 1 金币。"""
        _mk_session("s1")
        fid1 = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=10,
                                credit_confirmed=10, version="v1")
        wh = db.wechat_hash("alice")
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid1,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        db.mark_user_specific_confirmed_to_paid(wh, [fid1])
        _mk_session("s2")
        _insert_feedback(sid="s2", slug="demo", wechat_id="alice",
                         payout="confirmed", credit_suggested=5,
                         credit_confirmed=5, version="v2")
        rows = db.list_payout_users()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["amount_confirmed"], 5)
        # 本期消耗金币 = 0（v2 期间没捐过）
        self.assertEqual(r["coins_consumed"], 0)

    def test_new_donation_after_paid_still_deducts(self):
        """v2 期间新捐了金币 → 本期 wd 仍要扣（与历史一致）。

        改用 settled_at 列后无需 time.sleep —— 新 record_donation 写 settled_at=NULL，
        mark-paid 后只动旧的、留下新的为 NULL → 本期仍计入扣减。
        """
        _mk_session("s1")
        fid1 = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=10,
                                credit_confirmed=10, version="v1")
        wh = db.wechat_hash("alice")
        # v1 捐 1（先于 payout）
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid1,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        db.mark_user_specific_confirmed_to_paid(wh, [fid1])
        # v2：先确认 ¥5，再捐 1（settled_at IS NULL，本期未结清）
        _mk_session("s2")
        fid2 = _insert_feedback(sid="s2", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=5,
                                credit_confirmed=5, version="v2")
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid2,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=1)
        bal = db.coin_balance(wh)
        # 本期 v2 confirmed=5，本期新捐 1（未 settled）→ wd=4
        self.assertEqual(bal["withdrawable"], 4)
        # lifetime 累计：2 次抽奖 = ceil(2/10) = 1 整块金币
        self.assertEqual(bal["consumed_coins"], 1)
        self.assertEqual(bal["donated_count"], 2)

    def test_single_feedback_paid_path_also_settles(self):
        """单条 transition_payout(fid, 'paid') 也必须结算 donation —— 与批量路径同口径。

        修 codex 四轮 P2：admin 走详情页「标已转账」按钮的话，donations 会永远
        保持 unsettled → 下一轮 confirmed 又被扣 → 双重扣减复现。
        """
        # 由 server.transition_payout 测试需要先 import server；为避免 worker
        # 副作用直接拷贝核心逻辑：mark paid 并同事务设 settled_at。
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import server  # noqa: E402
        _mk_session("s1")
        fid = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                               payout="confirmed", credit_suggested=10,
                               credit_confirmed=10)
        wh = db.wechat_hash("alice")
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        # 走 server.transition_payout 单条 paid 路径
        server.transition_payout(fid, "paid")
        # 验证 donation 已 settled
        row = db.get_conn().execute(
            "SELECT settled_at FROM coin_donations WHERE wechat_hash=?",
            (wh,)).fetchone()
        self.assertIsNotNone(row["settled_at"])

    def test_same_second_donation_after_paid_not_misclassified(self):
        """同秒竞争：mark-paid 与新 donation 同秒发生，不会把新捐赠算作已结清。

        修 codex 三轮 P1：之前用 created_at > MAX(paid_at)，同秒边界判错；
        改用 settled_at 列后这个 case 必须正确。
        """
        _mk_session("s1")
        fid1 = _insert_feedback(sid="s1", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=10,
                                credit_confirmed=10, version="v1")
        wh = db.wechat_hash("alice")
        # mark-paid 先（同事务把当前 NULL 的全标 settled）
        db.mark_user_specific_confirmed_to_paid(wh, [fid1])
        # 紧接着同一秒发起捐赠（实测可能同秒）—— 不用 sleep
        _mk_session("s2")
        fid2 = _insert_feedback(sid="s2", slug="demo", wechat_id="alice",
                                payout="confirmed", credit_suggested=5,
                                credit_confirmed=5, version="v2")
        db.record_donation(
            wechat_hash=wh, source_feedback_id=fid2,
            donor_label="alice", slot_landed=6, multiplier_pct=100,
            usd_cents=50, server_seed="s" * 64,
            server_seed_hash="h" * 64, client_seed="c", nonce=0)
        # 新捐赠 settled_at IS NULL → 算未结清 → 扣减
        bal = db.coin_balance(wh)
        self.assertEqual(bal["withdrawable"], 4)
        # 把新捐赠的 settled_at 状态再次确认
        unsettled = db.get_conn().execute(
            "SELECT COUNT(*) AS c FROM coin_donations "
            "WHERE wechat_hash=? AND settled_at IS NULL",
            (wh,)).fetchone()["c"]
        self.assertEqual(unsettled, 1)


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
