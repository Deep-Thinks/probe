"""SQLite 持久化层。

设计原则（来自 plan v2.2）：
- 单进程内共享一个连接池（threading.local），WAL 模式提升读写并发。
- schema 一次性 CREATE IF NOT EXISTS，所有 CHECK 约束在数据库层兜底。
- 业务事务通过 with db.transaction() 包装，自动 BEGIN/COMMIT/ROLLBACK。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
  reserved_count INTEGER NOT NULL DEFAULT 0
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
  created_at INTEGER NOT NULL
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
CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_per_project
  ON feedback(project_slug, wechat_id)
  WHERE wechat_id IS NOT NULL;
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


# ---- 种子写入（project_loader 用：JSON 仅作种子，已存在则不覆盖） ----


def seed_project(
    slug: str,
    name: str,
    description: str,
    trial_url: str,
    max_feedback_count: int,
    custom_questions_json: str | None,
) -> bool:
    """仅当 slug 不存在时插入。返回是否真正插入。

    ARCH-3：projects/*.json 降为可选种子；DB 是真相源，admin 编辑不被启动期覆盖。
    """
    now = int(time.time())
    with transaction() as tx:
        cur = tx.execute(
            """INSERT INTO projects(slug, name, description, trial_url,
                 max_feedback_count, custom_questions_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO NOTHING""",
            (slug, name, description, trial_url,
             max_feedback_count, custom_questions_json, now),
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
    """为每个已存在的项目确保一个非一次性公共 token。

    幂等：seed_invite_token 用 ON CONFLICT DO NOTHING，已存在则跳过。
    启动期在 project_loader.load_all 之后调用一次即可覆盖全部项目。
    """
    for p in list_projects():
        seed_invite_token(public_token_for(p["slug"]), p["slug"], 0)


# ---- admin：项目管理 / 招募工具 ----


def list_projects() -> list[sqlite3.Row]:
    return list(get_conn().execute(
        "SELECT * FROM projects ORDER BY created_at DESC, slug ASC"
    ))


def create_recruit_batch(project_slug: str, name: str) -> int:
    """新建招募批次，返回 batch id。"""
    now = int(time.time())
    with transaction() as tx:
        cur = tx.execute(
            "INSERT INTO recruit_batches(project_slug, name, created_at) VALUES (?,?,?)",
            (project_slug, name, now),
        )
        return cur.lastrowid


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
