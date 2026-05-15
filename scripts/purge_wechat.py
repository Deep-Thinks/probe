"""Probe 隐私清理 job。

逻辑（plan §隐私最小化）：
- UPDATE feedback SET wechat_id = NULL, wechat_id_purged_at = now
- WHERE submitted_at < (now - RETAIN_DAYS 天) AND wechat_id IS NOT NULL AND payout_status = 'paid'
- 仅清理已 paid 的反馈，防误删未付款的。

调度约束（必须在 zeabur.json 配置中保证）：
- 必须先于 backup.sh 当天运行，确保当天的 PII 清理在备份快照之前生效。
- 推荐顺序：cron-purge @ 01:00 → cron-backup @ 02:00。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

# 让脚本和主应用共用同一套 DB 路径解析（容器内 /data；本地 fallback 到仓库 data/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db  # noqa: E402

DB_PATH = _db.DB_PATH
_RETAIN_DAYS_ENV = os.environ.get("PROBE_WECHAT_RETAIN_DAYS", "30")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe.purge")


def main() -> int:
    if not Path(DB_PATH).exists():
        log.error("db not found: %s", DB_PATH)
        return 2

    # 拒绝负数 / 非整数：负数会把 cutoff 推向未来，立即清空所有已 paid 反馈
    # 的 wechat_id，违反保留窗口语义。
    try:
        retain_days = int(_RETAIN_DAYS_ENV)
    except (TypeError, ValueError):
        log.error("FATAL: PROBE_WECHAT_RETAIN_DAYS must be int, got %r",
                  _RETAIN_DAYS_ENV)
        return 3
    if retain_days < 0:
        log.error("FATAL: PROBE_WECHAT_RETAIN_DAYS must be >= 0, got %d", retain_days)
        return 3

    cutoff = int(time.time()) - retain_days * 86400
    now = int(time.time())

    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    try:
        # secure_delete=ON：将释放的页内容清零，否则 NULL 后旧的 wechat_id 字节
        # 仍可能残留在 free pages 内被 backup.py 按页复制带入备份。
        conn.execute("PRAGMA secure_delete=ON")
        cur = conn.execute(
            """UPDATE feedback
               SET wechat_id = NULL, wechat_id_purged_at = ?
               WHERE submitted_at < ?
                 AND wechat_id IS NOT NULL
                 AND payout_status = 'paid'""",
            (now, cutoff),
        )
        conn.commit()
        # 显式回收 free pages 中已被擦零的内容，缩小数据库文件，让备份不会再
        # 复制残留页（VACUUM 在 secure_delete=ON 时重写所有页面，保证 PII 抹除）。
        conn.execute("VACUUM")
        log.info("purged %d rows (retain=%dd, cutoff=%d)",
                 cur.rowcount, retain_days, cutoff)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
