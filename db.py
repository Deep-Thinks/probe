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
  -- token 必须显式属于一个项目；复合唯一为下游表的复合外键提供 target。
  UNIQUE(token, project_slug)
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
    """启动期一次性初始化所有表与索引。"""
    conn = get_conn()
    conn.executescript(SCHEMA_SQL)


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
