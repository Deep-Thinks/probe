"""SQLite 持久化层。

设计原则（来自 plan v2.2）：
- 单进程内共享一个连接池（threading.local），WAL 模式提升读写并发。
- schema 一次性 CREATE IF NOT EXISTS，所有 CHECK 约束在数据库层兜底。
- 业务事务通过 with db.transaction() 包装，自动 BEGIN/COMMIT/ROLLBACK。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import antifraud

_DEFAULT_DB_PATH = "/data/db.sqlite3"
_env_db_path = os.environ.get("PROBE_DB_PATH")

if _env_db_path:
    # 显式配置优先；尊重用户指定路径，必要时创建父目录。
    DB_PATH = _env_db_path
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
elif Path(_DEFAULT_DB_PATH).parent.exists():
    # 容器内：/data 已由 Zeabur volume 挂载。
    DB_PATH = _DEFAULT_DB_PATH
else:
    # 容器外本地开发：回退到仓库内 data/。
    DB_PATH = str(Path(__file__).parent / "data" / "db.sqlite3")
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

# 金币哈希盐：把微信号转成可跨 30 天隐私清理存活的耐久身份（见 wechat_hash）。
# 一旦设定不可更改，否则历史金币哈希全部对不上。
_COIN_SECRET = os.environ.get("PROBE_COIN_SECRET", "probe-dev-coin-secret")

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    """每线程一个 connection，避免 SQLite 跨线程使用警告。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务上下文。"""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
  -- SQLite 不会隐式给 TEXT PRIMARY KEY 加 NOT NULL，显式声明以防 NULL slug 入库。
  slug TEXT PRIMARY KEY NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  trial_url TEXT NOT NULL,
  max_feedback_count INTEGER NOT NULL,
  custom_questions_json TEXT,
  created_at INTEGER NOT NULL,
  reserved_count INTEGER NOT NULL DEFAULT 0,
  -- listed=1 公开到任务大厅 /hall（任何人可参与）；0 仅通过定向邀请链接进入。
  listed INTEGER NOT NULL DEFAULT 0,
  -- version：项目当前版本号；反馈提交时快照到 feedback.project_version。
  version TEXT NOT NULL DEFAULT 'v1'
);

CREATE TABLE IF NOT EXISTS invite_tokens (
  token TEXT PRIMARY KEY NOT NULL,
  project_slug TEXT NOT NULL REFERENCES projects(slug),
  is_single_use INTEGER NOT NULL DEFAULT 0,
  consumed_by_session TEXT,
  consumed_at INTEGER,
  created_at INTEGER NOT NULL,
  -- batch_id：admin 招募工具生成的 token 归属的招募批次（NULL = JSON 种子 token）。
  batch_id INTEGER,
  -- token 必须显式属于一个项目；复合唯一为下游表的复合外键提供 target。
  UNIQUE(token, project_slug)
);

-- 招募批次：admin 招募工具一次生成的一组 token（对应"一个微信群"）。
CREATE TABLE IF NOT EXISTS recruit_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_slug TEXT NOT NULL REFERENCES projects(slug),
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  -- 批次级建议金额区间（定向链接专属定价）。两者同时 NULL = 用全局默认 ¥3-¥15。
  credit_min INTEGER,
  credit_max INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY NOT NULL,
  project_slug TEXT NOT NULL REFERENCES projects(slug),
  invite_token TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  -- 防御性约束：session 绑定的 token 必须属于同一项目。
  FOREIGN KEY (invite_token, project_slug)
    REFERENCES invite_tokens(token, project_slug),
  UNIQUE(session_id, project_slug)
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL UNIQUE,
  project_slug TEXT NOT NULL REFERENCES projects(slug),
  wechat_id TEXT,
  wechat_id_purged_at INTEGER,
  -- project_version：提交时项目版本快照；不同版本的反馈有效性不同。
  project_version TEXT,
  -- wechat_hash：微信号单向哈希，可跨 wechat_id 隐私清理存活，用于金币聚合。
  wechat_hash TEXT,
  q1_answer TEXT NOT NULL,
  q2_answer TEXT NOT NULL,
  q3_answer TEXT NOT NULL,
  q4_answer TEXT NOT NULL,
  q5_answer TEXT NOT NULL DEFAULT '',
  custom_answers_json TEXT,
  submitted_at INTEGER NOT NULL,
  ai_status TEXT NOT NULL DEFAULT 'pending',
  ai_attempts INTEGER NOT NULL DEFAULT 0,
  ai_depth_score INTEGER,
  ai_depth_rationale TEXT,
  ai_stuck_step TEXT,
  ai_stuck_confidence REAL,
  ai_followup_json TEXT,
  ai_risk_flags_json TEXT,
  ai_model_used TEXT,
  payout_status TEXT NOT NULL DEFAULT 'na',
  credit_suggested INTEGER,
  credit_confirmed INTEGER,
  payout_notes TEXT,
  payout_paid_at INTEGER,
  -- 反作弊（见 antifraud.py）：content_hash 精确查重键、content_digest
  -- 语义判重比对素材、time_on_task_sec 开卡到提交耗时、dup_of_feedback_id
  -- 判重命中时指向被复制的早先反馈。
  content_hash TEXT,
  content_digest TEXT,
  time_on_task_sec INTEGER,
  dup_of_feedback_id INTEGER,
  -- 防御性约束：feedback 必须属于 session 所登记的同一项目。
  FOREIGN KEY (session_id, project_slug)
    REFERENCES sessions(session_id, project_slug),
  CHECK (
    (ai_status = 'done' OR credit_suggested IS NULL) AND
    (payout_status != 'suggested' OR credit_suggested IS NOT NULL) AND
    (payout_status != 'confirmed' OR credit_confirmed IS NOT NULL)
  )
);

CREATE INDEX IF NOT EXISTS idx_feedback_project ON feedback(project_slug, submitted_at);
CREATE INDEX IF NOT EXISTS idx_feedback_ai_status ON feedback(ai_status);
CREATE INDEX IF NOT EXISTS idx_feedback_payout ON feedback(payout_status);

-- 游戏化闭环（spec 2026-05-20）：扣 1 复活公益站 —— 复活抽奖捐赠流水。
-- 一行 = 一次抽奖事件 = 1 枚金币捐给公益站。Provably Fair 三元组完整存档。
CREATE TABLE IF NOT EXISTS coin_donations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  -- 捐赠人耐久身份（同 feedback.wechat_hash）。跨 wechat_id 隐私清理存活。
  wechat_hash TEXT NOT NULL,
  -- 这枚金币所属的源反馈（用于校验 donation_balance）。
  source_feedback_id INTEGER NOT NULL REFERENCES feedback(id),
  -- 公开署名：自由文本，最多 32 字符。不绑微信号、不参与去匿名。
  donor_label TEXT NOT NULL,
  -- 复活抽奖落槽 0..13 + 该槽倍率（整数百分点）+ 捐入池子的 USD 整数分。
  slot_landed INTEGER NOT NULL,
  multiplier_pct INTEGER NOT NULL,
  usd_cents INTEGER NOT NULL,
  -- Provably Fair 三元组：抽奖时立即披露，用户可离线复算。
  server_seed TEXT NOT NULL,
  server_seed_hash TEXT NOT NULL,
  client_seed TEXT NOT NULL,
  nonce INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  -- 防 nonce 重放：同捐赠人 × 同反馈 × 同 nonce 只能一行。
  UNIQUE(wechat_hash, source_feedback_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_donations_recent ON coin_donations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_donations_donor  ON coin_donations(wechat_hash);
"""


def init_schema() -> None:
    """启动期一次性初始化所有表与索引，并执行向后兼容迁移。"""
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)
    # 迁移：旧 DB 的 invite_tokens 没有 batch_id 列（CREATE IF NOT EXISTS 不会补列）。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(invite_tokens)")}
    if "batch_id" not in cols:
        conn.execute("ALTER TABLE invite_tokens ADD COLUMN batch_id INTEGER")
    # 迁移：问卷从 4 题扩到 5 题，旧 DB 的 feedback 没有 q5_answer 列。
    # NOT NULL 列需带默认值，老反馈第 5 题留空。
    fcols = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    if "q5_answer" not in fcols:
        conn.execute("ALTER TABLE feedback ADD COLUMN q5_answer TEXT NOT NULL DEFAULT ''")
    # 迁移：新增公开大厅开关。默认 0 → 旧项目从大厅下架，作者需在 admin 显式上架。
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}
    if "listed" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN listed INTEGER NOT NULL DEFAULT 0")
    # 迁移：招募批次新增批次级金额区间。旧批次两列为 NULL → 沿用全局默认 ¥3-¥15。
    bcols = {r["name"] for r in conn.execute("PRAGMA table_info(recruit_batches)")}
    if "credit_min" not in bcols:
        conn.execute("ALTER TABLE recruit_batches ADD COLUMN credit_min INTEGER")
        conn.execute("ALTER TABLE recruit_batches ADD COLUMN credit_max INTEGER")
    # 迁移：项目引入版本字段（旧库 CREATE IF NOT EXISTS 不会补列）。
    if "version" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN version TEXT NOT NULL DEFAULT 'v1'")
    # 迁移：feedback 引入版本快照 + 微信号耐久哈希。
    fcols2 = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    if "project_version" not in fcols2:
        conn.execute("ALTER TABLE feedback ADD COLUMN project_version TEXT")
    if "wechat_hash" not in fcols2:
        conn.execute("ALTER TABLE feedback ADD COLUMN wechat_hash TEXT")
    # 回填 project_version：老反馈按其所属项目的当前版本号填。
    conn.execute(
        """UPDATE feedback SET project_version =
             (SELECT version FROM projects WHERE projects.slug = feedback.project_slug)
           WHERE project_version IS NULL"""
    )
    # 回填 wechat_hash：老反馈用现存 wechat_id 算哈希；已清理（NULL）的跳过。
    for r in conn.execute(
            "SELECT id, wechat_id FROM feedback "
            "WHERE wechat_hash IS NULL AND wechat_id IS NOT NULL").fetchall():
        conn.execute("UPDATE feedback SET wechat_hash = ? WHERE id = ?",
                     (wechat_hash(r["wechat_id"]), r["id"]))
    # 唯一索引升级：旧 (project_slug, wechat_id) → 新含版本三列。
    # 语义：同微信号在同项目同版本只能提交一次；但 v1 测过的人可合法再测 v2。
    conn.execute("DROP INDEX IF EXISTS uniq_wechat_per_project")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_per_project_version
             ON feedback(project_slug, project_version, wechat_id)
             WHERE wechat_id IS NOT NULL"""
    )
    # 防重复领钱加固：wechat_id 在 30 天隐私清理后被置 NULL，会退出上面那个
    # 部分唯一索引的覆盖，让同一人可在同项目同版本再领一次钱。wechat_hash
    # 是微信号的耐久哈希、跨清理永久存活，对它再建一道唯一索引堵住该窗口。
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_hash_per_project_version
             ON feedback(project_slug, project_version, wechat_hash)
             WHERE wechat_hash IS NOT NULL"""
    )
    # 迁移：反作弊查重 / 限时字段（旧库 CREATE IF NOT EXISTS 不会补列）。
    fcols3 = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    if "content_hash" not in fcols3:
        conn.execute("ALTER TABLE feedback ADD COLUMN content_hash TEXT")
    if "content_digest" not in fcols3:
        conn.execute("ALTER TABLE feedback ADD COLUMN content_digest TEXT")
    if "time_on_task_sec" not in fcols3:
        conn.execute("ALTER TABLE feedback ADD COLUMN time_on_task_sec INTEGER")
    if "dup_of_feedback_id" not in fcols3:
        conn.execute("ALTER TABLE feedback ADD COLUMN dup_of_feedback_id INTEGER")
    # 回填 content_hash：老反馈用现有 q1-q5 + 自定义题答案重算（确定性，可重跑）。
    for r in conn.execute(
            "SELECT id, q1_answer, q2_answer, q3_answer, q4_answer, q5_answer, "
            "custom_answers_json FROM feedback WHERE content_hash IS NULL").fetchall():
        try:
            custom = json.loads(r["custom_answers_json"]) if r["custom_answers_json"] else None
            if not isinstance(custom, list):
                custom = None
        except (json.JSONDecodeError, TypeError):
            custom = None
        text = antifraud.combined_text(
            r["q1_answer"], r["q2_answer"], r["q3_answer"],
            r["q4_answer"], r["q5_answer"], custom)
        conn.execute("UPDATE feedback SET content_hash = ? WHERE id = ?",
                     (antifraud.content_hash(text), r["id"]))
    # 回填 time_on_task_sec：用 session.started_at 关联（session 必存在，复合外键保证）。
    # content_digest 不回填——老反馈无摘要，不作为后续语义比对目标，可接受。
    conn.execute(
        """UPDATE feedback SET time_on_task_sec =
             (SELECT feedback.submitted_at - s.started_at FROM sessions s
               WHERE s.session_id = feedback.session_id)
           WHERE time_on_task_sec IS NULL"""
    )


# ---- 金币：微信号耐久哈希 + 余额聚合 ----


def wechat_hash(wechat_id: str) -> str:
    """微信号的单向哈希，作为可跨 30 天隐私清理存活的耐久身份。

    purge_wechat.py 会把 raw wechat_id 置 NULL，但 wechat_hash 永久保留，
    金币余额据此按人聚合。PROBE_COIN_SECRET 一旦设定不可更改。
    """
    norm = (wechat_id or "").strip().lower()
    return hashlib.sha256(
        (_COIN_SECRET + ":" + norm).encode("utf-8")
    ).hexdigest()


# 1 金币兑换 N 次抽奖（与 server.DRAWS_PER_COIN 单一来源同步）。
# 业务规则：1 金币 = 10 次抽奖；金币不抽就照常微信周末提现，自愿。
DRAWS_PER_COIN = 10


def coin_balance(wh: str) -> dict:
    """按 wechat_hash 聚合某 tester 跨所有项目/版本的金币与抽奖资格。

    扣 1 复活公益站（spec 2026-05-20 v8）：抽奖与金币耦合，每 1 金币换 10 次抽奖。
    - earned_total：AI 评过的全部金币（金币 = 人民币 1:1）
    - draws_earned = earned_total × DRAWS_PER_COIN（理论上能抽几次）
    - donated_count：已抽的次数（每次抽奖一条 coin_donations 行）
    - draws_remaining：还能抽几次（draws_earned - donated_count）
    - consumed_coins = ceil(donated_count / DRAWS_PER_COIN)：实际已被"吃掉"的整块金币
      （抽了 1-10 次 = 1 整块金币被锁定，11-20 = 2 块，以此类推）
    - withdrawable：confirmed 中尚未被抽奖消耗的整块金币（可周末微信提现）
      = max(0, confirmed_total - consumed_coins)
    """
    row = get_conn().execute(
        """SELECT
             COALESCE(SUM(CASE WHEN ai_status='done' AND credit_suggested IS NOT NULL
                               THEN credit_suggested END), 0) AS earned_total,
             COALESCE(SUM(CASE WHEN ai_status='done'
                               THEN 1 ELSE 0 END), 0)         AS done_feedback_count,
             COALESCE(SUM(CASE WHEN payout_status='confirmed'
                               THEN credit_confirmed END), 0) AS confirmed_total,
             COALESCE(SUM(CASE WHEN payout_status='paid'
                               THEN credit_confirmed END), 0) AS paid,
             COALESCE(SUM(CASE WHEN payout_status IN ('na','suggested')
                               THEN 1 ELSE 0 END), 0)         AS pending_count
           FROM feedback WHERE wechat_hash = ?""",
        (wh,),
    ).fetchone()
    don = get_conn().execute(
        """SELECT
             COALESCE(COUNT(*), 0)                  AS donated_count,
             COALESCE(SUM(usd_cents), 0)            AS donated_usd_cents
           FROM coin_donations WHERE wechat_hash = ?""",
        (wh,),
    ).fetchone()
    earned_total = row["earned_total"] or 0
    confirmed_total = row["confirmed_total"] or 0
    done_feedback_count = row["done_feedback_count"] or 0
    donated_count = don["donated_count"] or 0
    draws_earned = earned_total * DRAWS_PER_COIN
    # 已吃掉的整块金币：1-10 抽 = 1 块、11-20 = 2 块、…（ceiling 除法）
    consumed_coins = (donated_count + DRAWS_PER_COIN - 1) // DRAWS_PER_COIN
    return {
        # 历史口径（金币金额）：自愿献抽奖的整块金币不再可提现
        "withdrawable": max(0, confirmed_total - consumed_coins),
        "paid": row["paid"] or 0,
        "pending_count": row["pending_count"] or 0,
        # 公益站口径
        "earned_total": earned_total,
        "done_feedback_count": done_feedback_count,
        "draws_earned": draws_earned,
        "donated_count": donated_count,
        "consumed_coins": consumed_coins,
        "draws_remaining": max(0, draws_earned - donated_count),
        "donated_usd_cents": don["donated_usd_cents"] or 0,
        # 历史名（保留以免外部引用炸）：兼容老调用方
        "donatable_remaining": max(0, draws_earned - donated_count),
    }


# ---- 公益站捐赠（spec 2026-05-20） ----


def record_donation(
    wechat_hash: str,
    source_feedback_id: int,
    donor_label: str,
    slot_landed: int,
    multiplier_pct: int,
    usd_cents: int,
    server_seed: str,
    server_seed_hash: str,
    client_seed: str,
    nonce: int,
) -> int:
    """事务内：先校验 donation_balance ≥ 1，再插入 coin_donations 行。

    返回 donation_id。校验失败抛 ValueError，nonce 冲突抛 IntegrityError。
    """
    now = int(time.time())
    with transaction() as tx:
        bal = tx.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN ai_status='done' AND credit_suggested IS NOT NULL
                                   THEN credit_suggested END), 0) AS earned_total
               FROM feedback WHERE wechat_hash = ?""",
            (wechat_hash,),
        ).fetchone()
        donated = tx.execute(
            "SELECT COALESCE(COUNT(*), 0) AS c FROM coin_donations WHERE wechat_hash = ?",
            (wechat_hash,),
        ).fetchone()
        draws_earned = (bal["earned_total"] or 0) * DRAWS_PER_COIN
        remaining = draws_earned - (donated["c"] or 0)
        if remaining < 1:
            raise ValueError("抽奖次数已用完，先去完成一个新反馈再来。")
        cur = tx.execute(
            """INSERT INTO coin_donations(
                 wechat_hash, source_feedback_id, donor_label,
                 slot_landed, multiplier_pct, usd_cents,
                 server_seed, server_seed_hash, client_seed, nonce, created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (wechat_hash, source_feedback_id, donor_label,
             slot_landed, multiplier_pct, usd_cents,
             server_seed, server_seed_hash, client_seed, nonce, now),
        )
        return cur.lastrowid


def pool_total_usd_cents() -> int:
    """公益站累计捐入额度（USD 分）。"""
    row = get_conn().execute(
        "SELECT COALESCE(SUM(usd_cents), 0) AS total FROM coin_donations"
    ).fetchone()
    return row["total"] or 0


def pool_donor_count() -> int:
    """累计参与复活的独立捐赠人数（按 wechat_hash 去重）。"""
    row = get_conn().execute(
        "SELECT COUNT(DISTINCT wechat_hash) AS c FROM coin_donations"
    ).fetchone()
    return row["c"] or 0


def list_recent_donations(limit: int = 30) -> list[sqlite3.Row]:
    """最近的捐赠记录，按时间倒序。"""
    return list(get_conn().execute(
        """SELECT id, donor_label, slot_landed, multiplier_pct, usd_cents, created_at
           FROM coin_donations ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ))


def next_donation_nonce(wechat_hash: str, source_feedback_id: int) -> int:
    """为 (wechat_hash, source_feedback_id) 取下一个递增 nonce。

    存在并发风险：两次同时取 nonce 拿到同值时 INSERT 会因 UNIQUE 失败。
    上层捕获 IntegrityError 重试即可（极低概率，单用户行为）。
    """
    row = get_conn().execute(
        """SELECT COALESCE(MAX(nonce), -1) AS m FROM coin_donations
           WHERE wechat_hash = ? AND source_feedback_id = ?""",
        (wechat_hash, source_feedback_id),
    ).fetchone()
    return (row["m"] or -1) + 1


def fetch_donation(donation_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM coin_donations WHERE id = ?", (donation_id,)
    ).fetchone()


# ---- 项目/Token upsert（启动期同步 projects/*.json） ----


def upsert_project(
    slug: str,
    name: str,
    description: str,
    trial_url: str,
    max_feedback_count: int,
    custom_questions_json: str | None,
) -> None:
    now = int(time.time())
    with transaction() as tx:
        tx.execute(
            """
            INSERT INTO projects(slug, name, description, trial_url,
              max_feedback_count, custom_questions_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
              name=excluded.name,
              description=excluded.description,
              trial_url=excluded.trial_url,
              max_feedback_count=excluded.max_feedback_count,
              custom_questions_json=excluded.custom_questions_json
            """,
            (slug, name, description, trial_url,
             max_feedback_count, custom_questions_json, now),
        )


def upsert_invite_token(token: str, project_slug: str, is_single_use: int) -> None:
    now = int(time.time())
    with transaction() as tx:
        tx.execute(
            """
            INSERT INTO invite_tokens(token, project_slug, is_single_use, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(token) DO UPDATE SET
              project_slug=excluded.project_slug,
              is_single_use=excluded.is_single_use
            """,
            (token, project_slug, is_single_use, now),
        )


# ---- 查询辅助 ----


def fetch_project(slug: str) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM projects WHERE slug = ?", (slug,)
    ).fetchone()


def fetch_token(token: str) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM invite_tokens WHERE token = ?", (token,)
    ).fetchone()


def fetch_session(session_id: str) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()


def fetch_feedback(feedback_id: int) -> sqlite3.Row | None:
    return get_conn().execute(
        "SELECT * FROM feedback WHERE id = ?", (feedback_id,)
    ).fetchone()


def list_feedback(limit: int = 200) -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT * FROM feedback ORDER BY submitted_at DESC LIMIT ?",
        (limit,),
    ))


def list_pending_ai() -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT * FROM feedback WHERE ai_status = 'pending' ORDER BY id ASC"
    ))


# ---- 反作弊查重（见 antifraud.py / ai_worker._run_antifraud） ----


def find_duplicate_hash(project_slug: str, content_hash: str,
                        before_id: int) -> int | None:
    """同项目内是否有 id 更小、content_hash 相同的早先反馈。返回最早那条 id。"""
    row = get_conn().execute(
        """SELECT id FROM feedback
           WHERE project_slug = ? AND content_hash = ? AND id < ?
           ORDER BY id ASC LIMIT 1""",
        (project_slug, content_hash, before_id),
    ).fetchone()
    return row["id"] if row else None


def list_prior_digests(project_slug: str, before_id: int) -> list[sqlite3.Row]:
    """同项目内 id 更小、已生成 content_digest 的早先反馈 (id, content_digest)。"""
    return list(get_conn().execute(
        """SELECT id, content_digest FROM feedback
           WHERE project_slug = ? AND id < ?
             AND content_digest IS NOT NULL AND content_digest != ''
           ORDER BY id ASC""",
        (project_slug, before_id),
    ))


# ---- 种子写入（project_loader 用：JSON 仅作种子，已存在则不覆盖） ----


def seed_project(
    slug: str,
    name: str,
    description: str,
    trial_url: str,
    max_feedback_count: int,
    custom_questions_json: str | None,
    version: str = "v1",
    listed: int = 0,
) -> bool:
    """仅当 slug 不存在时插入。返回是否真正插入。

    ARCH-3：projects/*.json 降为可选种子；DB 是真相源，admin 编辑不被启动期覆盖。
    version / listed 仅在项目首次 seed 时生效，已存在项目尊重 DB 现值。
    """
    now = int(time.time())
    with transaction() as tx:
        cur = tx.execute(
            """INSERT INTO projects(slug, name, description, trial_url,
                 max_feedback_count, custom_questions_json, created_at,
                 version, listed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO NOTHING""",
            (slug, name, description, trial_url, max_feedback_count,
             custom_questions_json, now, version, 1 if listed else 0),
        )
        return cur.rowcount > 0


def seed_invite_token(token: str, project_slug: str, is_single_use: int) -> None:
    """仅当 token 不存在时插入（种子 token，batch_id 为 NULL）。"""
    now = int(time.time())
    with transaction() as tx:
        tx.execute(
            """INSERT INTO invite_tokens(token, project_slug, is_single_use, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(token) DO NOTHING""",
            (token, project_slug, is_single_use, now),
        )


# ---- 任务大厅：公共邀请 token ----

# 任务大厅「立即参与」入口复用现有 invite token 机制：每个项目有一个
# 非一次性的公共 token，token 名嵌入 slug 保证跨项目唯一。
PUBLIC_TOKEN_PREFIX = "public-"


def public_token_for(slug: str) -> str:
    """返回某项目的公共邀请 token（任务大厅「立即参与」入口用）。"""
    return f"{PUBLIC_TOKEN_PREFIX}{slug}"


def ensure_public_tokens() -> None:
    """为每个已公开到大厅的项目确保一个非一次性公共 token。

    幂等：seed_invite_token 用 ON CONFLICT DO NOTHING，已存在则跳过。
    只覆盖 listed=1 的项目：定向项目不需要公共 token，避免大厅入口泄漏。
    """
    for p in list_projects(listed_only=True):
        seed_invite_token(public_token_for(p["slug"]), p["slug"], 0)


# ---- admin：项目管理 / 招募工具 ----


def list_projects(listed_only: bool = False) -> list[sqlite3.Row]:
    """列出项目。listed_only=True 时只返回已公开到任务大厅的项目。"""
    sql = "SELECT * FROM projects"
    if listed_only:
        sql += " WHERE listed = 1"
    sql += " ORDER BY created_at DESC, slug ASC"
    return list(get_conn().execute(sql))


def set_project_listed(slug: str, listed: int) -> None:
    """设置项目是否公开到任务大厅（admin 项目表单调用）。"""
    with transaction() as tx:
        tx.execute("UPDATE projects SET listed = ? WHERE slug = ?",
                   (1 if listed else 0, slug))


# 全局默认建议金额区间（大厅 / 种子 token / 未设金额的批次共用）。
# depth_score 1-5 在 [min, max] 间线性插值，默认区间还原为 {1:3,2:6,3:9,4:12,5:15}。
DEFAULT_CREDIT_MIN = 3
DEFAULT_CREDIT_MAX = 15


def create_recruit_batch(project_slug: str, name: str,
                         credit_min: int | None = None,
                         credit_max: int | None = None) -> int:
    """新建招募批次，返回 batch id。

    credit_min/credit_max 同时为 None 时该批沿用全局默认金额；
    两者均为整数时作为该批次定向链接的专属金额区间。
    """
    now = int(time.time())
    with transaction() as tx:
        cur = tx.execute(
            """INSERT INTO recruit_batches(project_slug, name, created_at,
                 credit_min, credit_max) VALUES (?,?,?,?,?)""",
            (project_slug, name, now, credit_min, credit_max),
        )
        return cur.lastrowid


def credit_range_for_token(token: str) -> tuple[int, int]:
    """返回某 invite token 对应的建议金额区间 (min, max)。

    token 属于设了金额的招募批次 → 用批次区间；否则（种子 token、公共 token、
    未设金额的批次）回退到全局默认 ¥3-¥15。
    """
    row = get_conn().execute(
        """SELECT b.credit_min AS cmin, b.credit_max AS cmax
             FROM invite_tokens t
             LEFT JOIN recruit_batches b ON t.batch_id = b.id
            WHERE t.token = ?""",
        (token,),
    ).fetchone()
    if row is not None and row["cmin"] is not None and row["cmax"] is not None:
        return row["cmin"], row["cmax"]
    return DEFAULT_CREDIT_MIN, DEFAULT_CREDIT_MAX


def add_invite_token(token: str, project_slug: str,
                     is_single_use: int, batch_id: int | None) -> None:
    """招募工具新增一个 token（已知 token 唯一，直接 INSERT）。"""
    now = int(time.time())
    with transaction() as tx:
        tx.execute(
            """INSERT INTO invite_tokens(token, project_slug, is_single_use,
                 batch_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (token, project_slug, is_single_use, batch_id, now),
        )


def list_batches() -> list[sqlite3.Row]:
    """所有招募批次 + 每批 token 总数 / 已用数。"""
    return list(get_conn().execute(
        """SELECT b.id, b.project_slug, b.name, b.created_at,
                  b.credit_min, b.credit_max,
                  COUNT(t.token) AS token_count,
                  SUM(CASE WHEN t.consumed_by_session IS NOT NULL THEN 1 ELSE 0 END)
                    AS used_count
           FROM recruit_batches b
           LEFT JOIN invite_tokens t ON t.batch_id = b.id
           GROUP BY b.id
           ORDER BY b.id DESC"""
    ))


def list_batch_tokens(batch_id: int) -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT * FROM invite_tokens WHERE batch_id = ? ORDER BY created_at ASC",
        (batch_id,),
    ))
