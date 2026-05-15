"""Probe 备份脚本。

实现要点：
- 用 sqlite3.Connection.backup() 做原子快照（不依赖外部 sqlite3 CLI 二进制）
- 备份前先校验所有配置（DB 必须存在、RETAIN_DAYS 必须为非负整数）
- 滚动清理：保留期外的 db-*.sqlite3 删除，任何 OSError 都让 cron 失败告警
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# 让脚本和主应用共用同一套 DB 路径解析（容器内 /data；本地 fallback 到仓库 data/）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db as _db  # noqa: E402

DB_PATH = _db.DB_PATH
BACKUP_DIR = os.environ.get("PROBE_BACKUP_DIR", "/data/backups")
RETAIN_DAYS_ENV = os.environ.get("PROBE_BACKUP_RETAIN_DAYS", "30")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe.backup")


def main() -> int:
    # --- 前置校验：让任何配置错误在写出备份之前失败 ---
    if not Path(DB_PATH).is_file():
        log.error("FATAL: source database not found at %s", DB_PATH)
        log.error("refusing to back up — sqlite3 would silently create an empty file")
        return 2

    if not re.fullmatch(r"\d+", RETAIN_DAYS_ENV):
        log.error("FATAL: PROBE_BACKUP_RETAIN_DAYS must be non-negative integer, got %r",
                  RETAIN_DAYS_ENV)
        return 3
    retain_days = int(RETAIN_DAYS_ENV)

    backup_dir = Path(BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d", time.localtime())
    out = backup_dir / f"db-{ts}.sqlite3"
    tmp = backup_dir / f".db-{ts}.sqlite3.tmp.{os.getpid()}"
    log.info("starting backup -> %s (via %s)", out, tmp.name)

    # --- 原子发布：先写到 .tmp 后 os.replace，避免半截备份占用最终路径 ---
    if tmp.exists():
        tmp.unlink()
    try:
        src = sqlite3.connect(DB_PATH)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, out)
    except Exception:
        # 失败时清掉半截 tmp，避免下次启动把它当残留累计
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise

    # --- 清理过期备份；任何 OSError 都向上传播让 cron 告警 ---
    cutoff = time.time() - retain_days * 86400
    pruned = 0
    for f in backup_dir.glob("db-*.sqlite3"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            log.info("pruned %s", f.name)
            pruned += 1
    log.info("done (pruned %d files older than %dd)", pruned, retain_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
