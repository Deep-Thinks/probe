# Probe — vanilla HTML + Python stdlib server
# 单容器：同一镜像被 web service 和两个 cron service（purge / backup）共用，
# cron service 通过 JOB 环境变量分支到 scripts/cron-entrypoint.sh。

FROM python:3.12-slim

# tini：转发信号给主进程，确保 SIGTERM 时干净退出 worker 线程。
# ca-certificates：访问 stepfun / DeepSeek / qwen 的 HTTPS 接口。
# sqlite3 CLI：备份链路已改用 Python sqlite3.Connection.backup()，但保留
# 二进制以便生产应急下排查（sqlite3 /data/db.sqlite3 "..."）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 当前无 requirements.txt 依赖（plan: vanilla stdlib only），但保留以便未来添加。
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

ENV PYTHONUNBUFFERED=1 \
    PROBE_DB_PATH=/data/db.sqlite3 \
    PROBE_BACKUP_DIR=/data/backups \
    PORT=8080

EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
# 用 python3 而非 python：与 scripts/cron-entrypoint.sh 保持一致，避免依
# 赖 python 别名（slim 镜像有但其它基础镜像可能没有）。
CMD ["python3", "server.py"]
