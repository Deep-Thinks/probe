[根目录](../CLAUDE.md) > **scripts**

# scripts/ — Zeabur cron 服务入口（备份 + 隐私清理）

## 模块职责

Zeabur 部署同一镜像启动 3 个 service（web + cron-purge + cron-backup），cron service 通过 `JOB` 环境变量分支到本目录下不同脚本：

- `cron-entrypoint.sh`：根据 `JOB=backup|purge` 分发到对应 Python 脚本
- `backup.sh` → `backup.py`：每日 02:00 用 `sqlite3.Connection.backup()` 做原子快照，30 天滚动清理
- `purge_wechat.py`：每日 01:00 把已 paid 反馈的 `wechat_id` 置 NULL，配合 `secure_delete=ON` + `VACUUM` 抹除底层页

**关键调度顺序**：purge 必须在 backup **之前** 1 小时，否则当天的 PII 清理会被早 1 小时的备份快照固化，违反 30 天清理承诺。这一点在 `zeabur.json`（`0 1 * * *` vs `0 2 * * *`）和 `backup.sh` / `purge_wechat.py` 顶部注释三处显式记录。

## 入口与启动

### Zeabur cron 路径

```
zeabur cron schedule
  ↓
docker run -e JOB=backup ... image  →  ENTRYPOINT tini --
  ↓
CMD ["python3", "server.py"]   ←  被 startCommand 覆盖
  ↓
startCommand: bash scripts/cron-entrypoint.sh
  ↓
case JOB in
  backup) exec bash scripts/backup.sh    →  exec python3 scripts/backup.py
  purge)  exec python3 scripts/purge_wechat.py
```

### 本地手工运行

```bash
cd /niuniu869_dev/probe
export PROBE_DB_PATH=./data/db.sqlite3

# 备份
JOB=backup bash scripts/cron-entrypoint.sh
# 或直接：
python3 scripts/backup.py

# 隐私清理
JOB=purge bash scripts/cron-entrypoint.sh
# 或直接：
python3 scripts/purge_wechat.py
```

## 对外接口

无 HTTP 接口（cron job 类）。退出码语义：

| 脚本 | exit 0 | exit 2 | exit 3 | 其它 |
|---|---|---|---|---|
| `backup.py` | 成功 | DB 文件不存在 | `PROBE_BACKUP_RETAIN_DAYS` 非整数 | 异常（半截 tmp 已清理后 raise） |
| `purge_wechat.py` | 成功 | DB 文件不存在 | `PROBE_WECHAT_RETAIN_DAYS` 非整数或负数 | sqlite3 异常透传 |
| `cron-entrypoint.sh` | 透传子进程 | — | — | `JOB` 不是 backup/purge → exit 1 |

## 关键依赖与配置

### `backup.py`

- **依赖路径解析**：`sys.path.insert(0, ..)` 后 `import db as _db`，共用主应用的 `DB_PATH` 解析逻辑
- **备份目录**：`PROBE_BACKUP_DIR`（默认 `/data/backups`）
- **保留天数**：`PROBE_BACKUP_RETAIN_DAYS`（默认 30，必须非负整数）
- **快照机制**：`sqlite3.connect(src).backup(dst)`，**不依赖外部 `sqlite3` CLI**（Dockerfile 仍装 sqlite3 CLI 是为了应急排查）
- **原子发布**：先写到 `.db-YYYYMMDD.sqlite3.tmp.<pid>` 再 `os.replace` 改名，失败时清掉 tmp
- **滚动清理**：超过 `retain_days` 的 `db-*.sqlite3` 删除；任何 `OSError` 向上传播让 cron 告警

### `purge_wechat.py`

- **保留天数**：`PROBE_WECHAT_RETAIN_DAYS`（默认 30；负数被显式拒绝，因为负数会让 cutoff 进入未来 → 一次性清空所有已 paid 反馈）
- **清理范围**：`submitted_at < now - retain_days*86400` AND `wechat_id IS NOT NULL` AND `payout_status = 'paid'`
- **secure_delete**：`PRAGMA secure_delete=ON` 让释放的页被清零，配合 `VACUUM` 重写所有页，确保 PII 真的从底层文件里抹除（否则 free pages 仍含明文，会被 `backup.py` 按页复制带入备份）

### `cron-entrypoint.sh`

- `set -euo pipefail`
- 用 `python3` 而非 `python`：`python:3.x-slim` 镜像里只有 `python3` 别名

## 数据模型

- **写入**：`backup.py` 不写主 DB，只产生 `/data/backups/db-YYYYMMDD.sqlite3` 文件；`purge_wechat.py` `UPDATE feedback SET wechat_id=NULL, wechat_id_purged_at=?`
- **读出**：两者都 `import db` 仅为复用 `DB_PATH` 解析

## 测试与质量

- 无自动化测试。
- 关键失败场景：
  - **purge 在 backup 之后跑** → 当天的 PII 已被备份固化，破坏 30 天承诺（**靠 cron schedule + 脚本顶部注释双重提醒**）
  - **backup 半途崩溃** → 已实现 tmp 清理 + raise，下次启动不会累计残留
  - **VACUUM 在 cron 中失败** → 当前没有专门处理；若磁盘满会让 `purge_wechat.py` 进程异常退出，cron 告警
- 建议 v2：
  1. 加 `tests/test_backup.py` 验证 tmp 残留清理路径
  2. 加 `tests/test_purge.py` 验证负数 retain_days 被拒
  3. backup 完成后做 `sqlite3 db-YYYYMMDD.sqlite3 "PRAGMA integrity_check"` 体检

## 常见问题 (FAQ)

**Q: 为什么不直接用 `sqlite3` CLI 的 `.backup`？**
A: `backup.py` 改用 `sqlite3.Connection.backup()` 是为了：(1) 不依赖外部 binary 在不同基础镜像下的可用性；(2) 与 main app 共用同一 DB 路径解析；(3) 异常处理路径在 Python 一处统一。

**Q: 为什么 cron schedule 是 01:00 / 02:00 而不是更靠近凌晨？**
A: plan §运维 选了一个"中国时区凌晨且早于 UTC 工作时间"的窗口，让用户睡觉时清理完成。两者间隔 1 小时是为了给 purge 的 VACUUM 留足执行时间（VACUUM 在 30 天数据量下大约几秒，但留出余量）。

**Q: 30 天备份 + 30 天 wechat_id 保留 = 实际 PII 暴露窗口多长？**
A: 最坏 60 天（"提交后第 30 天 paid → 第 1 天 purge 清理但当晚备份固化 → 这份备份保留 30 天"）。**已在 backup.sh 注释和隐私告知里明确披露**。

## 相关文件清单

- `cron-entrypoint.sh`（JOB 分发）
- `backup.sh`（thin wrapper）+ `backup.py`（实现）
- `purge_wechat.py`
- 配置：`/niuniu869_dev/probe/zeabur.json`（cron schedule）
- 共用 DB 路径：`/niuniu869_dev/probe/db.py::DB_PATH`

## 变更记录 (Changelog)

- **2026-05-16 02:07:03**：首次生成模块文档；记录 purge-before-backup 调度约束、secure_delete + VACUUM 隐私保证、60 天最坏 PII 暴露窗口。
