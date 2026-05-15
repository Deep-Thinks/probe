#!/usr/bin/env bash
# Probe 日常备份脚本（thin wrapper -> backup.py）。
#
# 调度约束（必须在 zeabur.json 配置中保证）：
#   1. 必须先于备份运行 purge_wechat.py 当天的清理，避免备份成为 PII 长期存档。
#      推荐顺序：cron-purge @ 01:00 → cron-backup @ 02:00。
#   2. 备份滚动保留 = wechat_id 保留窗口（默认 30 天），上限叠加期 ~30 + 30 = 60 天，
#      在隐私告知里向 tester 明确披露。

set -euo pipefail
exec python3 "$(dirname "$0")/backup.py"
