# Probe 远端部署文件（systemd + nginx）

这些是远端 `101.33.32.162:/opt/probe/` 的运维文件模板。**不含密钥**（.env 单独 scp）。

## 文件清单

| 文件 | 远端目标路径 |
|---|---|
| `probe.service` | `/etc/systemd/system/probe.service` |
| `probe-purge.service` | `/etc/systemd/system/probe-purge.service` |
| `probe-purge.timer` | `/etc/systemd/system/probe-purge.timer` |
| `probe-backup.service` | `/etc/systemd/system/probe-backup.service` |
| `probe-backup.timer` | `/etc/systemd/system/probe-backup.timer` |
| `nginx.probe.niuniu869.com.conf` | `/etc/nginx/sites-available/probe.niuniu869.com` |

## 部署架构

```
浏览器 (HTTPS)
   │
   ▼
[nginx :443/:80]   ← Let's Encrypt 证书，certbot.timer 每天自动续期
   │ 反代
   ▼
[server.py :127.0.0.1:19080]   ← Python 标准库，systemd 拉起，PROBE_BIND=127.0.0.1
   │
   ├── ai_worker（同进程后台线程，stepfun step-3.5-flash）
   └── SQLite @ /opt/probe/data/db.sqlite3

[probe-purge.timer] @ 01:00 daily → purge_wechat.py（清理过期 wechat_id）
[probe-backup.timer] @ 02:00 daily → backup.sh（SQLite 快照 + 滚动保留 30 天）
[certbot.timer]    @ system → 自动续 SSL
```

## 初次安装命令（远端 root）

```bash
cd /opt/probe
# 拷 systemd units
cp deploy/probe.service /etc/systemd/system/
cp deploy/probe-purge.service deploy/probe-purge.timer /etc/systemd/system/
cp deploy/probe-backup.service deploy/probe-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now probe.service
systemctl enable --now probe-purge.timer probe-backup.timer

# 拷 nginx vhost
cp deploy/nginx.probe.niuniu869.com.conf /etc/nginx/sites-available/probe.niuniu869.com
ln -s /etc/nginx/sites-available/probe.niuniu869.com /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# 签 SSL（自动 80→443 跳转 + 续期）
certbot --nginx -d probe.niuniu869.com --redirect --non-interactive --agree-tos -m <your-email>
systemctl list-timers certbot.timer
```
