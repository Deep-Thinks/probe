#!/usr/bin/env bash
# Zeabur cron service 容器入口。
# 用同一镜像跑 backup + purge，由 cron schedule 触发各一次。
# 用 JOB 环境变量分支：
#   JOB=backup -> scripts/backup.sh
#   JOB=purge  -> python3 scripts/purge_wechat.py
# 注意：python:3.x-slim 镜像里只有 `python3` 可执行文件，没有 `python` 别名。

set -euo pipefail

case "${JOB:-}" in
  backup)
    exec bash /app/scripts/backup.sh
    ;;
  purge)
    exec python3 /app/scripts/purge_wechat.py
    ;;
  *)
    echo "Unknown JOB='${JOB:-}'. Expected 'backup' or 'purge'." >&2
    exit 1
    ;;
esac
