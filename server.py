"""Probe HTTP server (Python stdlib only)。

实现 plan §API 路由（最小集）+ 启动时加载 projects/*.json + 启动 AI worker 线程。
"""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import ai_worker
import project_loader

# vendored 纯 Python 二维码库（招募工具用）。requirements.txt 保持空。
sys.path.insert(0, str(Path(__file__).parent / "vendor"))
import segno  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("probe.server")

PORT = int(os.environ.get("PORT", "8080"))
BIND_HOST = os.environ.get("PROBE_BIND", "0.0.0.0")
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
PROJECTS_DIR = Path(__file__).parent / "projects"
ADMIN_USER = os.environ.get("PROBE_ADMIN_USER", "admin")
_DEFAULT_ADMIN_PASS = "probe-dev-pass"
ADMIN_PASS = os.environ.get("PROBE_ADMIN_PASS", _DEFAULT_ADMIN_PASS)
SESSION_TTL_SEC = 30 * 60  # 30 分钟
MAX_BODY = 64 * 1024  # 64KB form 上限
# 允许 admin POST 的完整 origin 列表（CSRF Origin/Referer 校验用）。
# 必须含 scheme + host + port，e.g. "https://probe.niuniu869.com,http://localhost:8080"
# 仅比对 hostname 在同主机不同端口/不同 scheme 时会被绕过。
ALLOWED_ORIGINS = {
    o.strip().rstrip("/").lower()
    for o in os.environ.get("PROBE_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
}


class ProjectFullError(Exception):
    pass


class TokenAlreadyConsumedError(Exception):
    pass


# ---- 模板渲染（极简 {{var}} 替换） ----


_template_cache: dict[str, str] = {}


def render(name: str, vars: dict) -> str:
    if name not in _template_cache:
        _template_cache[name] = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    out = _template_cache[name]
    for key, val in vars.items():
        out = out.replace("{{" + key + "}}", str(val))
    return out


def esc(value) -> str:
    """HTML 转义。None -> 空串。"""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


# CSV formula injection 防御：Excel/WPS/Numbers 打开 CSV 时会把以
# = + - @ Tab \r 开头的单元格当公式执行，可能触发 `=cmd|...` 一类的命令。
# 这里给用户输入字段加单引号前缀，让单元格保持文本。
_CSV_DANGEROUS_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def _safe_csv_cell(value: str) -> str:
    if value and value[0] in _CSV_DANGEROUS_PREFIX:
        return "'" + value
    return value


def _feedback_source(invite_token: str | None) -> str:
    """根据 session 的 invite_token 推导反馈来源（归因）。

    public-* 前缀 = 任务大厅公开入口；其它 = 作者主动发的定向渠道 token。
    """
    if not invite_token:
        return "—"
    if invite_token.startswith(db.PUBLIC_TOKEN_PREFIX):
        return "公开大厅"
    return f"定向渠道 · {invite_token}"


def _credit_range_label(token: str | None) -> str:
    """tester 端展示用的建议金额区间文案，如「¥3–¥15」。

    定向链接展示其所属批次的专属区间，大厅 / 种子 token 展示全局默认。
    """
    if token:
        cmin, cmax = db.credit_range_for_token(token)
    else:
        cmin, cmax = db.DEFAULT_CREDIT_MIN, db.DEFAULT_CREDIT_MAX
    return f"¥{cmin}–¥{cmax}"


# ---- admin 侧栏 + 项目表单校验 ----

ADMIN_NAV = (
    ("dashboard", "/admin", "数据看板"),
    ("feedback", "/admin/feedback", "反馈列表"),
    ("recruit", "/admin/recruit", "招募工具"),
    ("projects", "/admin/projects", "项目管理"),
)


def _admin_sidebar(active: str) -> str:
    """渲染 admin 深色侧栏。active 为 ADMIN_NAV 的 key。"""
    links = "".join(
        f'<a href="{href}" class="{"is-active" if key == active else ""}">{label}</a>'
        for key, href, label in ADMIN_NAV
    )
    return (
        '<aside class="sidebar">'
        '<a class="brand" href="/admin"><span class="brand-mark">P</span>Probe Admin</a>'
        f'<nav class="sidebar-nav">{links}</nav>'
        f'<div class="sidebar-foot">作者 {esc(ADMIN_USER)}<br>probe.niuniu869.com</div>'
        "</aside>"
    )


def _validate_project_form(slug, name, description, trial_url,
                           max_raw, customs, tokens) -> str | None:
    """admin 项目表单校验，镜像 project_loader 规则（invite_tokens 可空）。"""
    if not project_loader.SLUG_RE.match(slug or ""):
        return f"slug 必须匹配 {project_loader.SLUG_RE.pattern}"
    if not name:
        return "项目名不能为空。"
    if not description:
        return "项目描述不能为空。"
    if not trial_url.lower().startswith(("http://", "https://")):
        return "试用 URL 必须以 http:// 或 https:// 开头。"
    try:
        max_n = int(max_raw)
    except ValueError:
        return "名额上限必须是整数。"
    if max_n < 1 or max_n > 100:
        return "名额上限必须在 1-100 之间。"
    if len(customs) > 2:
        return "自定义题最多 2 个。"
    for t in tokens:
        if not t:
            return "邀请 token 不能是空行。"
    return None


# ---- 业务逻辑：session 创建 / token 验证 / 反馈提交 ----


def create_session(project_slug: str, token: str) -> str:
    """登记一次新的 session。一次性 token 在此时不消费，等到 POST feedback 才消费。"""
    session_id = uuid.uuid4().hex
    now = int(time.time())
    with db.transaction() as tx:
        tx.execute(
            """INSERT INTO sessions(session_id, project_slug, invite_token,
                                    started_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, project_slug, token, now, now + SESSION_TTL_SEC),
        )
    return session_id


def submit_feedback(
    project_slug: str,
    session_id: str,
    wechat_id: str,
    q1: str, q2: str, q3: str, q4: str, q5: str,
    custom_answers: list[str] | None,
) -> int:
    """原子占用名额 + 一次性 token 消费 + 插入 feedback。返回 feedback_id。"""
    now = int(time.time())
    project = db.fetch_project(project_slug)
    if project is None:
        raise ValueError("项目不存在")
    project_version = project["version"]
    wh = db.wechat_hash(wechat_id)
    session = db.fetch_session(session_id)
    if session is None:
        raise ValueError("session 不存在或已过期")
    if session["expires_at"] < now:
        raise ValueError("session 已过期，请重新进入项目链接")
    if session["project_slug"] != project_slug:
        raise ValueError("session 与项目不匹配")

    token_row = db.fetch_token(session["invite_token"])
    if token_row is None:
        raise ValueError("invite token 不存在")

    custom_json = (
        json.dumps(custom_answers, ensure_ascii=False) if custom_answers else None
    )

    with db.transaction() as tx:
        # 1. 原子占用一个名额
        cur = tx.execute(
            """UPDATE projects
               SET reserved_count = reserved_count + 1
               WHERE slug = ? AND reserved_count < max_feedback_count""",
            (project_slug,),
        )
        if cur.rowcount == 0:
            raise ProjectFullError("本项目已收满")

        # 2. 一次性 token 消费（仅当 is_single_use=1）
        if token_row["is_single_use"]:
            cur = tx.execute(
                """UPDATE invite_tokens
                   SET consumed_by_session = ?, consumed_at = ?
                   WHERE token = ? AND consumed_by_session IS NULL""",
                (session_id, now, session["invite_token"]),
            )
            if cur.rowcount == 0:
                # 回退名额
                tx.execute(
                    "UPDATE projects SET reserved_count = reserved_count - 1 WHERE slug = ?",
                    (project_slug,),
                )
                raise TokenAlreadyConsumedError("invite token 已被使用")

        # 3. 插入 feedback
        try:
            cur = tx.execute(
                """INSERT INTO feedback(
                     session_id, project_slug, wechat_id, wechat_hash,
                     q1_answer, q2_answer, q3_answer, q4_answer, q5_answer,
                     custom_answers_json, submitted_at, project_version
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, project_slug, wechat_id, wh,
                 q1, q2, q3, q4, q5, custom_json, now, project_version),
            )
            feedback_id = cur.lastrowid
        except Exception:
            # 回退名额；token 若已消费让作者后台手动恢复
            tx.execute(
                "UPDATE projects SET reserved_count = reserved_count - 1 WHERE slug = ?",
                (project_slug,),
            )
            raise

    return feedback_id


# ---- 状态机校验：合法转移矩阵 ----

LEGAL_TRANSITIONS = {
    ("na", "suggested"): True,
    ("na", "confirmed"): True,
    ("na", "rejected"): True,
    ("suggested", "confirmed"): True,
    ("suggested", "rejected"): True,
    ("confirmed", "paid"): True,
}


def transition_payout(feedback_id: int, target: str, **fields) -> None:
    """合法状态转移 + 必要字段写入。"""
    row = db.fetch_feedback(feedback_id)
    if row is None:
        raise ValueError("feedback 不存在")
    src = row["payout_status"]
    if (src, target) not in LEGAL_TRANSITIONS:
        raise ValueError(f"非法状态转移 {src} -> {target}")

    sets = ["payout_status = ?"]
    args: list = [target]

    if target == "confirmed":
        credit = fields.get("credit_confirmed")
        if credit is None:
            credit = row["credit_suggested"]
        if credit is None:
            raise ValueError("confirmed 状态必须有 credit_confirmed")
        sets.append("credit_confirmed = ?")
        args.append(int(credit))

    if target == "rejected":
        sets.append("payout_notes = ?")
        args.append(fields.get("reason", "")[:500])

    if target == "paid":
        sets.append("payout_paid_at = ?")
        args.append(int(time.time()))

    # 并发安全：在 WHERE 中带原始 payout_status。两个并发 admin 请求都从
    # 'suggested' 读到状态时，只有先提交 UPDATE 的能命中条件（rowcount=1），
    # 后者 rowcount=0 → 抛错让前端重新读取并决定如何处理。
    args.extend([feedback_id, src])
    with db.transaction() as tx:
        cur = tx.execute(
            f"UPDATE feedback SET {', '.join(sets)} "
            f"WHERE id = ? AND payout_status = ?",
            args,
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"feedback {feedback_id} payout_status 已被其他请求修改，"
                "请回到列表页重新加载后再操作"
            )


# ---- HTTP Handler ----


class Handler(BaseHTTPRequestHandler):
    server_version = "Probe/1.0"

    # ---- 工具方法 ----

    def _send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, body: str, status: int = 200,
                   ctype: str = "text/plain; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def _error_page(self, title: str, message: str, status: int = 400) -> None:
        self._send_html(render("error.html", {
            "title": esc(title),
            "message": esc(message),
        }), status=status)

    def _require_admin(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                user, _, pw = decoded.partition(":")
                if (secrets.compare_digest(user, ADMIN_USER)
                        and secrets.compare_digest(pw, ADMIN_PASS)):
                    return True
            except (ValueError, base64.binascii.Error):
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="probe-admin"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"401 Unauthorized")
        return False

    def _require_same_origin(self) -> bool:
        """CSRF 防护：admin POST 必须从同源页面发起。

        校验完整 origin（scheme + host + port），不是只比 hostname——
        否则 `http://probe.example:9999` 到 `https://probe.example:8080`
        会被错误认为同源，浏览器照常重放 Basic Auth。

        本服务的 canonical origin 由 X-Forwarded-Proto（反代场景必备）+
        Host header 组成；额外允许 PROBE_ALLOWED_ORIGINS 显式白名单。
        """
        proto = self.headers.get("X-Forwarded-Proto", "http").lower()
        host = (self.headers.get("Host") or "").lower()
        canonical = f"{proto}://{host}" if host else ""

        candidate: str | None = self.headers.get("Origin")
        if not candidate:
            ref = self.headers.get("Referer", "")
            if ref:
                try:
                    p = urlparse(ref)
                    if p.scheme and p.netloc:
                        candidate = f"{p.scheme}://{p.netloc}"
                except ValueError:
                    candidate = None
        if not candidate:
            # 浏览器表单 POST 至少携带 Origin 或 Referer；都缺一律拒绝。
            self._error_page("CSRF check failed",
                             "缺少 Origin/Referer header；admin POST 必须从同源页面发起。",
                             status=403)
            return False

        candidate = candidate.rstrip("/").lower()
        allowed = {canonical} | ALLOWED_ORIGINS
        allowed.discard("")
        if candidate in allowed:
            return True
        self._error_page(
            "CSRF check failed",
            f"Origin {candidate!r} 不在允许列表 {sorted(allowed)!r}",
            status=403,
        )
        return False

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log.info("%s - %s", self.address_string(), format % args)

    # ---- 路由分发 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/":
            self._send_html(render("landing.html", {}))
            return
        if path == "/healthz":
            self._send_text("ok")
            return
        if path == "/hall":
            self._handle_task_hall()
            return
        if path == "/coins":
            self._handle_coins()
            return
        if path.startswith("/static/"):
            self._serve_static(path)
            return

        # tester 端
        if path.startswith("/p/"):
            parts = path.split("/")
            # /p/<slug> | /p/<slug>/feedback | /p/<slug>/receipt
            if len(parts) >= 3:
                slug = parts[2]
                tail = parts[3] if len(parts) >= 4 else ""
                if tail == "":
                    self._handle_project_card(slug, qs)
                    return
                if tail == "feedback":
                    self._handle_feedback_form(slug, qs)
                    return
                if tail == "receipt":
                    self._handle_receipt(slug, qs)
                    return

        # admin 端
        if path == "/admin" or path.startswith("/admin/"):
            if not self._require_admin():
                return
            if path == "/admin":
                self._handle_admin_dashboard()
            elif path == "/admin/feedback":
                self._handle_admin_list()
            elif path == "/admin/export.csv":
                self._handle_admin_export()
            elif path == "/admin/recruit":
                self._handle_admin_recruit(qs)
            elif path == "/admin/projects":
                self._handle_admin_projects()
            elif path == "/admin/projects/new":
                self._handle_admin_project_form(None)
            elif path.startswith("/admin/feedback/"):
                try:
                    fid = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    self._error_page("Not Found", "无效的反馈 ID", status=404)
                    return
                self._handle_admin_detail(fid)
            elif path.startswith("/admin/projects/") and path.endswith("/edit"):
                slug = path[len("/admin/projects/"):-len("/edit")]
                self._handle_admin_project_form(slug)
            else:
                self._error_page("Not Found", f"路径不存在：{path}", status=404)
            return

        self._error_page("Not Found", f"路径不存在：{path}", status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/p/") and path.endswith("/feedback"):
            slug = path.split("/")[2]
            self._handle_feedback_submit(slug)
            return
        if path == "/coins":
            self._handle_coins_lookup()
            return
        if path == "/admin" or path.startswith("/admin/"):
            if not self._require_admin():
                return
            # CSRF 防护：admin POST 必须从同源页面发起
            if not self._require_same_origin():
                return
            parts = path.split("/")
            # /admin/feedback/<id>/<action>
            if path.startswith("/admin/feedback/") and len(parts) >= 5:
                try:
                    fid = int(parts[3])
                except ValueError:
                    self._error_page("Bad Request", "无效的反馈 ID", status=400)
                    return
                self._handle_admin_action(fid, parts[4])
            elif path == "/admin/recruit":
                self._handle_admin_recruit_post()
            elif path == "/admin/projects/new":
                self._handle_admin_project_save(None)
            elif path.startswith("/admin/projects/") and path.endswith("/edit"):
                slug = path[len("/admin/projects/"):-len("/edit")]
                self._handle_admin_project_save(slug)
            elif path.startswith("/admin/projects/") and path.endswith("/release"):
                rel_slug = path[len("/admin/projects/"):-len("/release")]
                self._handle_admin_project_release(rel_slug)
            else:
                self._error_page("Not Found", f"路径不存在：{path}", status=404)
            return

        self._error_page("Not Found", f"路径不存在：{path}", status=404)

    # ---- 路由实现：tester ----

    def _serve_static(self, path: str) -> None:
        name = path[len("/static/"):]
        # 防目录穿越
        if "/" in name or ".." in name or not name:
            self._error_page("Not Found", "", status=404)
            return
        target = STATIC_DIR / name
        if not target.exists() or not target.is_file():
            self._error_page("Not Found", "", status=404)
            return
        data = target.read_bytes()
        ctype = "text/css" if name.endswith(".css") else "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        self.wfile.write(data)

    def _handle_task_hall(self) -> None:
        """公开任务大厅：陈列已公开项目 + 剩余名额，「立即参与」走公共 token。"""
        cards = []
        for p in db.list_projects(listed_only=True):
            slug = p["slug"]
            slots_left = p["max_feedback_count"] - p["reserved_count"]
            maxc = p["max_feedback_count"] or 1
            reserved_pct = min(100, round(p["reserved_count"] * 100 / maxc))
            if slots_left <= 0:
                slots_line = f'名额已满（{p["max_feedback_count"]} / {p["max_feedback_count"]}）'
                action = '<span class="pill">名额已满</span>'
            else:
                slots_line = (f'剩余 <strong>{slots_left}</strong> / '
                              f'{p["max_feedback_count"]} 人次体验资格')
                link = f'/p/{esc(slug)}?t={esc(db.public_token_for(slug))}'
                action = f'<a class="btn" href="{link}">立即参与 →</a>'
            cards.append(
                '<div class="card">'
                f'<h3>{esc(p["name"])}</h3>'
                f'<p>{esc(p["description"])}</p>'
                f'<p class="muted" style="margin-bottom:4px">{slots_line}</p>'
                f'<div class="progress"><span style="width:{reserved_pct}%"></span></div>'
                f'<p class="row mt">{action}'
                '<span class="muted">约 5-10 分钟 · 完成可获 ¥3–¥15 微信转账</span>'
                '</p>'
                '</div>'
            )
        self._send_html(render("task_hall.html", {
            "cards": "".join(cards) or (
                '<div class="empty"><p>当前没有开放的评测任务。</p>'
                '<p class="muted">作者上新项目后会自动出现在这里。</p></div>'
            ),
        }))

    def _handle_project_card(self, slug: str, qs: dict) -> None:
        project = db.fetch_project(slug)
        if project is None:
            self._error_page("Not Found", f"项目不存在：{slug}", status=404)
            return

        if project["reserved_count"] >= project["max_feedback_count"]:
            self._send_html(render("project_full.html", {
                "name": esc(project["name"]),
                "max_feedback_count": project["max_feedback_count"],
            }))
            return

        token = (qs.get("t") or [""])[0]
        token_row = db.fetch_token(token) if token else None
        if token_row is None or token_row["project_slug"] != slug:
            self._error_page("Invalid Link", "测试链接缺少有效 invite token。", status=403)
            return
        if token_row["is_single_use"] and token_row["consumed_by_session"]:
            self._error_page("Token 已用", "该一次性邀请链接已被使用。", status=403)
            return
        # 公共 token 的有效性绑定项目 listed：项目转为定向后立即关闭大厅入口，
        # 定向邀请 token 不受影响。
        if token.startswith(db.PUBLIC_TOKEN_PREFIX) and not project["listed"]:
            self._error_page("链接已失效",
                             "该项目已转为定向邀请，公开参与入口已关闭。", status=403)
            return

        session_id = create_session(slug, token)
        slots_left = project["max_feedback_count"] - project["reserved_count"]
        # 名额进度条宽度百分比（模板不支持表达式，在此算好）。
        max_count = project["max_feedback_count"] or 1
        reserved_pct = min(100, round(project["reserved_count"] * 100 / max_count))

        self._send_html(render("project_card.html", {
            "name": esc(project["name"]),
            "description": esc(project["description"]),
            "trial_url": esc(project["trial_url"]),
            "slug": esc(slug),
            "session_id": esc(session_id),
            "max_feedback_count": project["max_feedback_count"],
            "reserved_count": project["reserved_count"],
            "slots_left": slots_left,
            "reserved_pct": reserved_pct,
            "credit_range": _credit_range_label(token),
        }))

    def _handle_feedback_form(self, slug: str, qs: dict,
                              error: str = "",
                              prefill: dict | None = None) -> None:
        project = db.fetch_project(slug)
        if project is None:
            self._error_page("Not Found", f"项目不存在：{slug}", status=404)
            return
        session_id = (qs.get("s") or [""])[0]
        session = db.fetch_session(session_id) if session_id else None
        if session is None or session["project_slug"] != slug:
            self._error_page("Session 失效", "请回到项目卡页重新进入。", status=400)
            return

        custom_block = ""
        if project["custom_questions_json"]:
            try:
                custom_questions = json.loads(project["custom_questions_json"])
            except json.JSONDecodeError:
                custom_questions = []
            for i, q in enumerate(custom_questions):
                # 与 4 固定题一致的 .question 块结构（见 DESIGN.md / feedback_form.html）。
                custom_block += (
                    '<div class="question">'
                    f'<span class="qnum">自定义题 {i+1}</span>'
                    f'<h3>{esc(q)}</h3>'
                    f'<label for="custom_{i}">作者想额外了解的问题。</label>'
                    f'<textarea id="custom_{i}" name="custom_{i}" required></textarea>'
                    '</div>'
                )

        prefill = prefill or {}
        error_block = (
            f'<div class="form-error">{esc(error)}</div>' if error else ""
        )
        self._send_html(render("feedback_form.html", {
            "name": esc(project["name"]),
            "slug": esc(slug),
            "session_id": esc(session_id),
            "custom_questions_block": custom_block,
            "error_block": error_block,
            "q1": esc(prefill.get("q1", "")),
            "q2": esc(prefill.get("q2", "")),
            "q3": esc(prefill.get("q3", "")),
            "q4": esc(prefill.get("q4", "")),
            "q5": esc(prefill.get("q5", "")),
            "wechat_id": esc(prefill.get("wechat_id", "")),
        }))

    def _handle_feedback_submit(self, slug: str) -> None:
        form = self._read_form()
        session_id = (form.get("session_id") or [""])[0]
        wechat_id = (form.get("wechat_id") or [""])[0].strip()
        q1 = (form.get("q1") or [""])[0].strip()
        q2 = (form.get("q2") or [""])[0].strip()
        q3 = (form.get("q3") or [""])[0].strip()
        q4 = (form.get("q4") or [""])[0].strip()
        q5 = (form.get("q5") or [""])[0].strip()

        prefill = {"q1": q1, "q2": q2, "q3": q3, "q4": q4, "q5": q5,
                   "wechat_id": wechat_id}

        if not all([session_id, wechat_id, q1, q2, q3, q4, q5]):
            self._handle_feedback_form(
                slug, {"s": [session_id]},
                error="请填写所有题目和微信号。",
                prefill=prefill,
            )
            return

        project = db.fetch_project(slug)
        custom_answers = None
        if project and project["custom_questions_json"]:
            try:
                custom_qs = json.loads(project["custom_questions_json"])
            except json.JSONDecodeError:
                custom_qs = []
            answers = []
            for i in range(len(custom_qs)):
                ans = (form.get(f"custom_{i}") or [""])[0].strip()
                if not ans:
                    self._handle_feedback_form(
                        slug, {"s": [session_id]},
                        error="自定义题目未答完。",
                        prefill=prefill,
                    )
                    return
                answers.append(ans)
            custom_answers = answers if answers else None

        try:
            feedback_id = submit_feedback(
                project_slug=slug,
                session_id=session_id,
                wechat_id=wechat_id,
                q1=q1, q2=q2, q3=q3, q4=q4, q5=q5,
                custom_answers=custom_answers,
            )
        except ProjectFullError:
            self._send_html(render("project_full.html", {
                "name": esc(project["name"]) if project else esc(slug),
                "max_feedback_count": project["max_feedback_count"] if project else 0,
            }))
            return
        except TokenAlreadyConsumedError as exc:
            self._error_page("Token 已用", str(exc), status=403)
            return
        except sqlite3.IntegrityError as exc:
            # UNIQUE 约束（同 wechat 同项目、session_id 重复）走友好提示，
            # 不让用户看到 500。两个 UNIQUE 在 plan 里都有意义。
            msg = str(exc)
            if "uniq_wechat_per_project" in msg or "feedback.wechat_id" in msg:
                friendly = "你已经为这个项目提交过反馈了。"
            elif "feedback.session_id" in msg:
                friendly = "本次会话已经提交过反馈，请重新进入项目链接。"
            else:
                friendly = "提交冲突，请稍后重试。"
            self._handle_feedback_form(
                slug, {"s": [session_id]},
                error=friendly, prefill=prefill,
            )
            return
        except ValueError as exc:
            self._handle_feedback_form(
                slug, {"s": [session_id]},
                error=str(exc), prefill=prefill,
            )
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("submit_feedback failed: %s", exc)
            self._error_page("提交失败", "请稍后重试。", status=500)
            return

        self._send_redirect(f"/p/{slug}/receipt?s={session_id}&fid={feedback_id}")

    def _handle_receipt(self, slug: str, qs: dict) -> None:
        project = db.fetch_project(slug)
        session_id = (qs.get("s") or [""])[0]
        fid = (qs.get("fid") or [""])[0]
        session = db.fetch_session(session_id) if session_id else None
        token = session["invite_token"] if session else None

        # 金币块：用刚提交反馈的 wechat_hash 聚合该 tester 的余额。
        coin_block = ('<p class="muted">本次反馈的金额会在作者确认后并入'
                      '可提现金币。</p>')
        try:
            fb = db.fetch_feedback(int(fid)) if fid else None
        except ValueError:
            fb = None
        if fb is not None and fb["wechat_hash"]:
            bal = db.coin_balance(fb["wechat_hash"])
            coin_block = (
                f'<p>当前可提现 <strong>{bal["withdrawable"]}</strong> 金币 · '
                f'评估中 {bal["pending_count"]} 条（含本次）。</p>'
            )

        self._send_html(render("receipt.html", {
            "name": esc(project["name"]) if project else esc(slug),
            "session_id": esc(session_id),
            "feedback_id": esc(fid),
            "credit_range": _credit_range_label(token),
            "coin_block": coin_block,
        }))

    def _handle_coins(self, error: str = "", result: str = "",
                      wechat_prefill: str = "") -> None:
        self._send_html(render("coins.html", {
            "error_block": (f'<div class="form-error">{esc(error)}</div>'
                            if error else ""),
            "result_block": result,
            "wechat_prefill": esc(wechat_prefill),
        }))

    def _handle_coins_lookup(self) -> None:
        form = self._read_form()
        wechat_id = (form.get("wechat_id") or [""])[0].strip()
        if not wechat_id:
            self._handle_coins(error="请输入微信号。")
            return
        bal = db.coin_balance(db.wechat_hash(wechat_id))
        if bal["withdrawable"] >= 100:
            gate = ('<p>已达 100 金币门槛——可联系作者 <strong>niuniu869</strong> '
                    '用 100 金币兑换一次咨询，或周末微信提现。</p>')
        else:
            gate = (f'<p class="muted">距 100 金币门槛还差 '
                    f'{100 - bal["withdrawable"]} 金币。</p>')
        result = (
            '<div class="card">'
            f'<h3>{esc(wechat_id)} 的金币</h3>'
            f'<p style="font-size:28px;margin:8px 0">'
            f'<strong>{bal["withdrawable"]}</strong> 金币可提现</p>'
            f'<p class="muted">已提现 {bal["paid"]} 金币 · '
            f'{bal["pending_count"]} 条反馈评估中（金额未定）</p>'
            f'{gate}'
            '</div>'
        )
        self._handle_coins(result=result, wechat_prefill=wechat_id)

    # ---- 路由实现：admin ----

    def _handle_admin_list(self) -> None:
        rows_html = []
        for row in db.list_feedback(limit=500):
            risk = ""
            if row["ai_risk_flags_json"]:
                try:
                    flags = json.loads(row["ai_risk_flags_json"])
                    risk = ", ".join(flags)
                except json.JSONDecodeError:
                    risk = ""
            rows_html.append(
                "<tr>"
                f'<td><a href="/admin/feedback/{row["id"]}">#{row["id"]}</a></td>'
                f"<td>{esc(row['project_slug'])}</td>"
                f"<td>{esc(row['project_version'] or '—')}</td>"
                f"<td>{esc(time.strftime('%m-%d %H:%M', time.localtime(row['submitted_at'])))}</td>"
                f'<td><span class="pill pill-{esc(row["ai_status"])}">{esc(row["ai_status"])}'
                f"{' ' + str(row['ai_depth_score']) + '/5' if row['ai_depth_score'] else ''}</span></td>"
                f"<td>{('¥' + str(row['credit_suggested'])) if row['credit_suggested'] is not None else '—'}</td>"
                f"<td>{('¥' + str(row['credit_confirmed'])) if row['credit_confirmed'] is not None else '—'}</td>"
                f'<td><span class="pill pill-{esc(row["payout_status"])}">{esc(row["payout_status"])}</span></td>'
                f'<td><span class="muted">{esc(risk)}</span></td>'
                f'<td><a href="/admin/feedback/{row["id"]}">查看 →</a></td>'
                "</tr>"
            )
        total = len(rows_html)
        self._send_html(render("admin_list.html", {
            "sidebar": _admin_sidebar("feedback"),
            "total": total,
            "rows": "\n".join(rows_html) or (
                '<tr><td colspan="10"><div class="empty">'
                '<p>还没有反馈。</p>'
                '<p class="muted">招募 tester 后，提交的反馈会出现在这里。</p>'
                '</div></td></tr>'
            ),
        }))

    def _handle_admin_detail(self, fid: int) -> None:
        row = db.fetch_feedback(fid)
        if row is None:
            self._error_page("Not Found", "反馈不存在", status=404)
            return

        session = db.fetch_session(row["session_id"])
        source = _feedback_source(session["invite_token"] if session else None)

        followup_items = ""
        if row["ai_followup_json"]:
            try:
                items = json.loads(row["ai_followup_json"])
                followup_items = "".join(f"<li>{esc(x)}</li>" for x in items)
            except json.JSONDecodeError:
                followup_items = ""

        risk_flags = ""
        if row["ai_risk_flags_json"]:
            try:
                flags = json.loads(row["ai_risk_flags_json"])
                risk_flags = ", ".join(flags) or "（无）"
            except json.JSONDecodeError:
                risk_flags = ""

        custom_block = ""
        if row["custom_answers_json"]:
            try:
                cs = json.loads(row["custom_answers_json"])
                for i, a in enumerate(cs):
                    custom_block += f"<p><strong>自定义 {i+1}：</strong>{esc(a)}</p>"
            except json.JSONDecodeError:
                custom_block = ""

        # 动作区块：根据 payout_status 显示可执行操作
        action_block = self._render_action_block(row)

        self._send_html(render("admin_detail.html", {
            "sidebar": _admin_sidebar("feedback"),
            "id": row["id"],
            "project_slug": esc(row["project_slug"]),
            "project_version": esc(row["project_version"] or "—"),
            "submitted_at": esc(time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(row["submitted_at"]))),
            "source": esc(source),
            "q1": esc(row["q1_answer"]),
            "q2": esc(row["q2_answer"]),
            "q3": esc(row["q3_answer"]),
            "q4": esc(row["q4_answer"]),
            "q5": esc(row["q5_answer"]),
            "custom_block": custom_block,
            "ai_status": esc(row["ai_status"]),
            "ai_model_used": esc(row["ai_model_used"] or "—"),
            "ai_attempts": row["ai_attempts"],
            "ai_depth_score": row["ai_depth_score"] if row["ai_depth_score"] is not None else "—",
            "ai_depth_rationale": esc(row["ai_depth_rationale"] or "—"),
            "ai_stuck_step": esc(row["ai_stuck_step"] or "—"),
            "ai_stuck_confidence": (f"{row['ai_stuck_confidence']:.2f}"
                                     if row["ai_stuck_confidence"] is not None else "—"),
            "followup_items": followup_items or "<li class=muted>暂无</li>",
            "risk_flags": esc(risk_flags),
            "payout_status": esc(row["payout_status"]),
            "credit_suggested": row["credit_suggested"] if row["credit_suggested"] is not None else "—",
            "credit_confirmed": row["credit_confirmed"] if row["credit_confirmed"] is not None else "—",
            "wechat_id": esc(row["wechat_id"] or "（已清理）"),
            "action_block": action_block,
        }))

    def _render_action_block(self, row) -> str:
        """根据当前 payout_status 渲染可用动作表单。"""
        fid = row["id"]
        status = row["payout_status"]
        risk_flags = []
        if row["ai_risk_flags_json"]:
            try:
                risk_flags = json.loads(row["ai_risk_flags_json"])
            except json.JSONDecodeError:
                pass
        injection_hit = "prompt_injection_attempt" in risk_flags

        suggested = row["credit_suggested"]

        if status in ("na", "suggested"):
            # 一键确认（命中 injection 禁用一键确认按钮，强制手动改值）
            confirm_btn = ""
            if not injection_hit and suggested is not None:
                confirm_btn = (
                    f'<form class="inline" method="post" '
                    f'action="/admin/feedback/{fid}/confirm">'
                    f'<button class="btn" type="submit">一键确认 ¥{suggested}</button>'
                    "</form>"
                )
            elif injection_hit:
                confirm_btn = (
                    '<p class="warn">⚠ 检测到 prompt injection 试图，禁止一键确认。'
                    "请手动改值后确认。</p>"
                )

            override_form = (
                f'<form class="inline" method="post" '
                f'action="/admin/feedback/{fid}/confirm">'
                f'<label>手动金额（¥）</label>'
                f'<input type="text" name="override" value="{esc(suggested or "")}" '
                f'style="width:100px">'
                '<button class="btn btn-secondary" type="submit">改值并确认</button>'
                "</form>"
            )
            reject_form = (
                f'<form class="inline" method="post" '
                f'action="/admin/feedback/{fid}/reject">'
                '<label>拒绝原因</label>'
                '<input type="text" name="reason" required style="width:240px">'
                '<button class="btn btn-danger" type="submit">拒绝</button>'
                "</form>"
            )
            return (
                f'<div class="row">{confirm_btn}</div>'
                f'<div class="row" style="margin-top:12px">{override_form}</div>'
                f'<div class="row" style="margin-top:12px">{reject_form}</div>'
            )

        if status == "confirmed":
            return (
                f'<form class="inline" method="post" '
                f'action="/admin/feedback/{fid}/mark-paid">'
                f'<button class="btn" type="submit">标记已转账 (¥{row["credit_confirmed"]})</button>'
                "</form>"
            )

        if status == "paid":
            paid_at = time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(row["payout_paid_at"])) \
                      if row["payout_paid_at"] else "—"
            return f'<p class="muted">已于 {paid_at} 转账完成。</p>'

        if status == "rejected":
            return f'<p class="muted">已拒绝。原因：{esc(row["payout_notes"] or "—")}</p>'

        return ""

    def _handle_admin_action(self, fid: int, action: str) -> None:
        form = self._read_form()
        try:
            if action == "confirm":
                override = (form.get("override") or [""])[0].strip()
                if override:
                    try:
                        credit = int(override)
                    except ValueError:
                        self._error_page("Bad Request", "金额必须是整数", status=400)
                        return
                else:
                    credit = None  # 用 credit_suggested
                transition_payout(fid, "confirmed", credit_confirmed=credit)
            elif action == "reject":
                reason = (form.get("reason") or [""])[0].strip()
                transition_payout(fid, "rejected", reason=reason)
                # 释放名额
                row = db.fetch_feedback(fid)
                if row:
                    with db.transaction() as tx:
                        tx.execute(
                            "UPDATE projects SET reserved_count = MAX(0, reserved_count - 1) "
                            "WHERE slug = ?",
                            (row["project_slug"],),
                        )
            elif action == "mark-paid":
                transition_payout(fid, "paid")
            elif action == "retry-ai":
                ai_worker.reset_for_retry(fid)
            else:
                self._error_page("Bad Request", f"未知动作：{action}", status=400)
                return
        except ValueError as exc:
            self._error_page("Bad Request", str(exc), status=400)
            return

        self._send_redirect(f"/admin/feedback/{fid}")

    def _handle_admin_export(self) -> None:
        """导出 payout_status='confirmed' 的待付清单 CSV。"""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["feedback_id", "project_slug", "project_version",
                         "source", "wechat_id",
                         "credit_confirmed", "confirmed_at_human"])
        rows = db.get_conn().execute(
            """SELECT f.id, f.project_slug, f.project_version, f.wechat_id,
                      f.credit_confirmed, f.submitted_at, s.invite_token
               FROM feedback f
               JOIN sessions s ON f.session_id = s.session_id
                              AND f.project_slug = s.project_slug
               WHERE f.payout_status = 'confirmed'
               ORDER BY f.id ASC"""
        ).fetchall()
        for r in rows:
            writer.writerow([
                r["id"], r["project_slug"],
                _safe_csv_cell(r["project_version"] or ""),
                _safe_csv_cell(_feedback_source(r["invite_token"])),
                _safe_csv_cell(r["wechat_id"] or ""),
                r["credit_confirmed"] or "",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["submitted_at"])),
            ])
        data = "﻿" + buf.getvalue()  # BOM for Excel CN
        self._send_text(data, ctype="text/csv; charset=utf-8")

    # ---- 路由实现：admin 看板 / 招募工具 / 项目管理 ----

    def _base_origin(self) -> str:
        """从反代头推导本服务的 canonical origin（用于拼邀请链接 / 二维码）。"""
        proto = self.headers.get("X-Forwarded-Proto", "http").lower()
        host = self.headers.get("Host") or "localhost"
        return f"{proto}://{host}"

    def _handle_admin_dashboard(self) -> None:
        conn = db.get_conn()
        pending = conn.execute(
            "SELECT COALESCE(SUM(credit_confirmed),0) s FROM feedback "
            "WHERE payout_status='confirmed'"
        ).fetchone()["s"]
        review = conn.execute(
            "SELECT COUNT(*) c FROM feedback WHERE payout_status='suggested'"
        ).fetchone()["c"]
        week_ago = int(time.time()) - 7 * 86400
        paid = conn.execute(
            "SELECT COALESCE(SUM(credit_confirmed),0) s FROM feedback "
            "WHERE payout_status='paid' AND payout_paid_at >= ?", (week_ago,)
        ).fetchone()["s"]

        # 各项目漏斗（项目数量少，N+1 查询可接受）
        funnels = []
        for p in db.list_projects():
            slug = p["slug"]
            collected = conn.execute(
                "SELECT COUNT(*) c FROM feedback WHERE project_slug=?", (slug,)
            ).fetchone()["c"]
            confirmed = conn.execute(
                "SELECT COUNT(*) c FROM feedback WHERE project_slug=? "
                "AND payout_status IN ('confirmed','paid')", (slug,)
            ).fetchone()["c"]
            paid_n = conn.execute(
                "SELECT COUNT(*) c FROM feedback WHERE project_slug=? "
                "AND payout_status='paid'", (slug,)
            ).fetchone()["c"]
            maxc = p["max_feedback_count"] or 1
            pct = min(100, round(p["reserved_count"] * 100 / maxc))
            funnels.append(
                '<div class="funnel">'
                f'<div class="funnel-head"><strong>{esc(p["name"])}</strong> '
                f'<span class="muted">{esc(p["version"])}</span>'
                f'<span class="muted">已收 {p["reserved_count"]}/'
                f'{p["max_feedback_count"]}</span></div>'
                f'<div class="progress"><span style="width:{pct}%"></span></div>'
                '<div class="funnel-legend">'
                f'<span>名额 {p["max_feedback_count"]}</span>'
                f'<span>已收反馈 {collected}</span>'
                f'<span>已确认 {confirmed}</span>'
                f'<span>已转账 {paid_n}</span></div>'
                "</div>"
            )
        funnels_html = "".join(funnels) or (
            '<div class="empty"><p>还没有项目。</p>'
            '<p class="muted">去「项目管理」创建第一个。</p></div>'
        )

        # AI 风险项
        risk_rows = conn.execute(
            "SELECT id, project_slug, ai_depth_score, ai_risk_flags_json "
            "FROM feedback "
            "WHERE ai_risk_flags_json LIKE '%prompt_injection%' OR ai_depth_score = 1 "
            "ORDER BY submitted_at DESC LIMIT 20"
        ).fetchall()
        risks = []
        for r in risk_rows:
            flags = r["ai_risk_flags_json"] or ""
            label = ("prompt injection 嫌疑" if "prompt_injection" in flags
                     else f"深度评分 {r['ai_depth_score']}（疑似敷衍）")
            risks.append(
                '<div class="risk-item">'
                f'<a href="/admin/feedback/{r["id"]}">#{r["id"]}</a> '
                f'{esc(r["project_slug"])} · <span class="warn">{label}</span>'
                "</div>"
            )
        risks_html = "".join(risks) or (
            '<div class="empty"><p>没有风险项。</p>'
            '<p class="muted">AI 评分一切正常。</p></div>'
        )

        self._send_html(render("admin_dashboard.html", {
            "sidebar": _admin_sidebar("dashboard"),
            "metric_pending": pending,
            "metric_review": review,
            "metric_paid": paid,
            "funnels": funnels_html,
            "risks": risks_html,
            "batches": self._render_batches(db.list_batches()),
        }))

    def _render_batches(self, batches) -> str:
        if not batches:
            return ('<div class="empty"><p>还没有招募批次。</p>'
                    '<p class="muted">去「招募工具」生成第一批。</p></div>')
        rows = []
        for b in batches:
            total = b["token_count"] or 0
            used = b["used_count"] or 0
            pct = min(100, round(used * 100 / total)) if total else 0
            rng = (f'¥{b["credit_min"]}–¥{b["credit_max"]}'
                   if b["credit_min"] is not None else "默认 ¥3–¥15")
            rows.append(
                '<div class="funnel">'
                f'<div class="funnel-head"><strong>{esc(b["name"])}</strong>'
                f'<span class="muted">{esc(b["project_slug"])}</span></div>'
                f'<div class="progress"><span style="width:{pct}%"></span></div>'
                '<div class="funnel-legend">'
                f'<span>发出 {total}</span><span>已使用 {used}</span>'
                f'<span>金额 {rng}</span>'
                f'<span><a href="/admin/recruit?batch={b["id"]}">查看链接 →</a></span>'
                "</div></div>"
            )
        return "".join(rows)

    def _handle_admin_recruit(self, qs: dict, error: str = "") -> None:
        result_block = ""
        batch_raw = (qs.get("batch") or [""])[0]
        if batch_raw:
            try:
                result_block = self._render_recruit_result(int(batch_raw))
            except ValueError:
                result_block = ""
        options = "".join(
            f'<option value="{esc(p["slug"])}">{esc(p["name"])}</option>'
            for p in db.list_projects()
        ) or '<option value="">（请先在项目管理创建项目）</option>'
        self._send_html(render("admin_recruit.html", {
            "sidebar": _admin_sidebar("recruit"),
            "project_options": options,
            "batches": self._render_batches(db.list_batches()),
            "error_block": (f'<div class="form-error">{esc(error)}</div>'
                            if error else ""),
            "result_block": result_block,
        }))

    def _render_recruit_result(self, batch_id: int) -> str:
        """渲染某批次的招募文案 + 二维码网格（PRG 后用于展示生成结果）。"""
        tokens = db.list_batch_tokens(batch_id)
        if not tokens:
            return ""
        batch = next((b for b in db.list_batches() if b["id"] == batch_id), None)
        if batch is None:
            return ""
        project = db.fetch_project(batch["project_slug"])
        pname = project["name"] if project else batch["project_slug"]
        origin = self._base_origin()
        bmin = (batch["credit_min"] if batch["credit_min"] is not None
                else db.DEFAULT_CREDIT_MIN)
        bmax = (batch["credit_max"] if batch["credit_max"] is not None
                else db.DEFAULT_CREDIT_MAX)
        text = (
            f"「{pname}」招内测员\n\n"
            f"花 5-10 分钟试用并回答 4 个问题，完成后微信转账 ¥{bmin}-¥{bmax}。\n"
            "下面每个链接 / 二维码是一个专属名额，一人用一个，用过即失效。"
        )
        cards = []
        for t in tokens:
            url = f"{origin}/p/{batch['project_slug']}?t={t['token']}"
            qr = segno.make(url, error="m").svg_inline(scale=3, border=2)
            used = " · 已用" if t["consumed_by_session"] else ""
            cards.append(
                '<div class="token-card">'
                f"{qr}"
                f'<div class="tk">{esc(url)}{used}</div>'
                "</div>"
            )
        return (
            f'<h2>批次「{esc(batch["name"])}」· {len(tokens)} 个邀请名额</h2>'
            '<div class="card">'
            '<p class="muted">把下面这段文案发到微信群，再附上二维码图片。</p>'
            f'<div class="copybox" id="recruit-text">{esc(text)}</div>'
            '<p class="mt"><button class="btn btn-secondary" type="button" '
            "onclick=\"navigator.clipboard.writeText("
            "document.getElementById('recruit-text').innerText)\">"
            "复制文案</button></p>"
            f'<div class="qr-grid">{"".join(cards)}</div>'
            "</div>"
        )

    def _handle_admin_recruit_post(self) -> None:
        form = self._read_form()
        slug = (form.get("project_slug") or [""])[0].strip()
        batch_name = (form.get("batch_name") or [""])[0].strip()
        count_raw = (form.get("count") or [""])[0].strip()

        if db.fetch_project(slug) is None:
            self._handle_admin_recruit({}, error="请选择一个有效项目。")
            return
        if not batch_name:
            self._handle_admin_recruit({}, error="请填写批次名。")
            return
        try:
            count = int(count_raw)
        except ValueError:
            self._handle_admin_recruit({}, error="数量必须是整数。")
            return
        if count < 1 or count > 50:
            self._handle_admin_recruit({}, error="数量必须在 1-50 之间。")
            return

        # 批次级金额区间：两个都留空 → 用全局默认；都填 → 该批专属定价。
        cmin_raw = (form.get("credit_min") or [""])[0].strip()
        cmax_raw = (form.get("credit_max") or [""])[0].strip()
        credit_min = credit_max = None
        if cmin_raw or cmax_raw:
            if not (cmin_raw and cmax_raw):
                self._handle_admin_recruit(
                    {}, error="金额上下限要么都填、要么都留空（留空则用默认 ¥3-¥15）。")
                return
            try:
                credit_min = int(cmin_raw)
                credit_max = int(cmax_raw)
            except ValueError:
                self._handle_admin_recruit({}, error="金额必须是整数。")
                return
            if credit_min < 1 or credit_max > 200 or credit_min > credit_max:
                self._handle_admin_recruit(
                    {}, error="金额需满足 1 ≤ 最小值 ≤ 最大值 ≤ 200。")
                return

        batch_id = db.create_recruit_batch(slug, batch_name, credit_min, credit_max)
        for _ in range(count):
            token = "pb-" + secrets.token_urlsafe(9)
            db.add_invite_token(token, slug, is_single_use=1, batch_id=batch_id)
        # PRG：重定向到 GET，刷新不会重复生成批次。
        self._send_redirect(f"/admin/recruit?batch={batch_id}")

    def _handle_admin_projects(self) -> None:
        rows = []
        for p in db.list_projects():
            cq = 0
            if p["custom_questions_json"]:
                try:
                    cq = len(json.loads(p["custom_questions_json"]))
                except json.JSONDecodeError:
                    cq = 0
            rows.append(
                "<tr>"
                f"<td>{esc(p['name'])}</td>"
                f"<td>{esc(p['version'])}</td>"
                f"<td><code>{esc(p['slug'])}</code></td>"
                f"<td>{p['max_feedback_count']}</td>"
                f"<td>{p['reserved_count']}</td>"
                f"<td>{cq}</td>"
                f'<td>{"🟢 公开" if p["listed"] else "🔒 定向"}</td>'
                f'<td><a href="/admin/projects/{esc(p["slug"])}/edit">编辑 →</a></td>'
                "</tr>"
            )
        self._send_html(render("admin_projects.html", {
            "sidebar": _admin_sidebar("projects"),
            "rows": "\n".join(rows) or (
                '<tr><td colspan="8"><div class="empty">'
                '<p>还没有项目。</p>'
                '<p class="muted">点上面的「新建项目」开始。</p>'
                "</div></td></tr>"
            ),
        }))

    def _handle_admin_project_form(self, slug, error: str = "",
                                   prefill: dict | None = None) -> None:
        is_edit = slug is not None
        if is_edit and prefill is None:
            p = db.fetch_project(slug)
            if p is None:
                self._error_page("Not Found", f"项目不存在：{slug}", status=404)
                return
            customs = []
            if p["custom_questions_json"]:
                try:
                    customs = json.loads(p["custom_questions_json"])
                except json.JSONDecodeError:
                    customs = []
            # 公共 token（任务大厅自动管理）不在编辑表单里展示，避免误删误改。
            tokens = [r["token"] for r in db.get_conn().execute(
                "SELECT token FROM invite_tokens WHERE project_slug=? "
                "ORDER BY created_at", (slug,))
                if not r["token"].startswith(db.PUBLIC_TOKEN_PREFIX)]
            prefill = {
                "slug": p["slug"], "name": p["name"],
                "description": p["description"], "trial_url": p["trial_url"],
                "max_feedback_count": p["max_feedback_count"],
                "custom1": customs[0] if len(customs) > 0 else "",
                "custom2": customs[1] if len(customs) > 1 else "",
                "invite_tokens": "\n".join(tokens),
                "single_use": False,
                "listed": bool(p["listed"]),
                "version": p["version"],
            }
        prefill = prefill or {}
        # 发布新版本区块仅在编辑态出现（新建项目还没有版本可发布）。
        release_block = ""
        if is_edit:
            cur_ver = esc(prefill.get("version", "v1"))
            release_block = (
                '<div class="card">'
                '<h3>发布新版本</h3>'
                f'<p class="muted">当前版本 <strong>{cur_ver}</strong>。'
                '发布新版本会把名额计数（已收）<strong>归零</strong>，让新版本'
                '重新收集反馈；旧版本已有反馈保留其版本号不变。可同时更新'
                '试用 URL / 描述 / 名额，留空则沿用。</p>'
                f'<form method="post" action="/admin/projects/{esc(slug)}/release">'
                '<label for="new_version">新版本号（必填，例如 v2）</label>'
                '<input type="text" id="new_version" name="new_version" required>'
                '<label for="rel_trial_url">新试用 URL（选填）</label>'
                '<input type="text" id="rel_trial_url" name="trial_url">'
                '<label for="rel_description">新描述（选填）</label>'
                '<textarea id="rel_description" name="description"></textarea>'
                '<label for="rel_max">新名额上限（选填，1-100）</label>'
                '<input type="text" id="rel_max" name="max_feedback_count">'
                '<p class="mt"><button class="btn btn-secondary" type="submit">'
                '发布新版本（名额归零）</button></p>'
                '</form></div>'
            )
        self._send_html(render("admin_project_form.html", {
            "sidebar": _admin_sidebar("projects"),
            "heading": "编辑项目" if is_edit else "新建项目",
            "action": (f"/admin/projects/{esc(slug)}/edit" if is_edit
                       else "/admin/projects/new"),
            "error_block": (f'<div class="form-error">{esc(error)}</div>'
                            if error else ""),
            "slug": esc(prefill.get("slug", "")),
            "slug_readonly": "readonly" if is_edit else "",
            "name": esc(prefill.get("name", "")),
            "description": esc(prefill.get("description", "")),
            "trial_url": esc(prefill.get("trial_url", "")),
            "max_feedback_count": esc(prefill.get("max_feedback_count", "30")),
            "custom1": esc(prefill.get("custom1", "")),
            "custom2": esc(prefill.get("custom2", "")),
            "invite_tokens": esc(prefill.get("invite_tokens", "")),
            "single_use_checked": "checked" if prefill.get("single_use") else "",
            "listed_checked": "checked" if prefill.get("listed") else "",
            "release_block": release_block,
        }))

    def _handle_admin_project_save(self, slug) -> None:
        is_edit = slug is not None
        form = self._read_form()
        f_slug = slug if is_edit else (form.get("slug") or [""])[0].strip()
        name = (form.get("name") or [""])[0].strip()
        description = (form.get("description") or [""])[0].strip()
        trial_url = (form.get("trial_url") or [""])[0].strip()
        max_raw = (form.get("max_feedback_count") or [""])[0].strip()
        custom1 = (form.get("custom1") or [""])[0].strip()
        custom2 = (form.get("custom2") or [""])[0].strip()
        tokens_raw = (form.get("invite_tokens") or [""])[0]
        single_use = bool(form.get("single_use_tokens"))
        listed = bool(form.get("listed"))
        token_list = [t.strip() for t in tokens_raw.splitlines() if t.strip()]
        customs = [c for c in (custom1, custom2) if c]

        prefill = {
            "slug": f_slug, "name": name, "description": description,
            "trial_url": trial_url, "max_feedback_count": max_raw,
            "custom1": custom1, "custom2": custom2,
            "invite_tokens": "\n".join(token_list), "single_use": single_use,
            "listed": listed,
        }

        err = _validate_project_form(f_slug, name, description, trial_url,
                                     max_raw, customs, token_list)
        if err:
            self._handle_admin_project_form(slug, error=err, prefill=prefill)
            return
        if not is_edit and db.fetch_project(f_slug) is not None:
            self._handle_admin_project_form(
                slug, error=f"slug {f_slug!r} 已存在。", prefill=prefill)
            return
        # token 跨项目占用检查
        for t in token_list:
            existing = db.fetch_token(t)
            if existing is not None and existing["project_slug"] != f_slug:
                self._handle_admin_project_form(
                    slug,
                    error=f"token {t!r} 已被项目 {existing['project_slug']!r} 占用。",
                    prefill=prefill)
                return

        custom_json = json.dumps(customs, ensure_ascii=False) if customs else None
        db.upsert_project(f_slug, name, description, trial_url,
                          int(max_raw), custom_json)
        db.set_project_listed(f_slug, 1 if listed else 0)
        for t in token_list:
            db.upsert_invite_token(t, f_slug, 1 if single_use else 0)
        # 公开到大厅的项目即时拥有「立即参与」用的公共 token（无需等重启）。
        # 定向项目不 seed；转为定向后旧公共 token 由 _handle_project_card 拦截失效。
        if listed:
            db.seed_invite_token(db.public_token_for(f_slug), f_slug, 0)
        self._send_redirect("/admin/projects")

    def _handle_admin_project_release(self, slug) -> None:
        """发布新版本：更新 projects.version 并把 reserved_count 归零。

        走 do_POST 的 _require_admin + _require_same_origin。旧反馈的
        project_version 不变——历史数据按版本沉淀。
        """
        project = db.fetch_project(slug)
        if project is None:
            self._error_page("Not Found", f"项目不存在：{slug}", status=404)
            return
        form = self._read_form()
        new_version = (form.get("new_version") or [""])[0].strip()
        trial_url = (form.get("trial_url") or [""])[0].strip()
        description = (form.get("description") or [""])[0].strip()
        max_raw = (form.get("max_feedback_count") or [""])[0].strip()

        if not new_version:
            self._error_page("Bad Request", "新版本号不能为空。", status=400)
            return
        if new_version == project["version"]:
            self._error_page(
                "Bad Request",
                f"新版本号不能与当前版本 {project['version']!r} 相同。",
                status=400)
            return

        sets = ["version = ?", "reserved_count = 0"]
        args: list = [new_version]
        if trial_url:
            if not trial_url.lower().startswith(("http://", "https://")):
                self._error_page("Bad Request",
                                  "试用 URL 必须以 http:// 或 https:// 开头。",
                                  status=400)
                return
            sets.append("trial_url = ?")
            args.append(trial_url)
        if description:
            sets.append("description = ?")
            args.append(description)
        if max_raw:
            try:
                max_n = int(max_raw)
            except ValueError:
                self._error_page("Bad Request", "名额上限必须是整数。",
                                  status=400)
                return
            if max_n < 1 or max_n > 100:
                self._error_page("Bad Request",
                                  "名额上限必须在 1-100 之间。", status=400)
                return
            sets.append("max_feedback_count = ?")
            args.append(max_n)
        args.append(slug)
        with db.transaction() as tx:
            tx.execute(f"UPDATE projects SET {', '.join(sets)} WHERE slug = ?",
                       args)
        self._send_redirect(f"/admin/projects/{slug}/edit")


# ---- 启动入口 ----


def main() -> None:
    db.init_schema()
    count = project_loader.load_all(PROJECTS_DIR)
    log.info("loaded %d projects", count)
    # 为每个项目确保任务大厅「立即参与」用的公共 token。
    db.ensure_public_tokens()

    # 严格密码门禁：使用默认开发密码 + 非 loopback 绑定 = 拒绝启动。
    # 仅 IPv4 loopback 算"安全"——ThreadingHTTPServer 默认 AF_INET，
    # `::1` 会因 socket 族不匹配启动失败；若需 IPv6 开发请显式设置密码。
    if ADMIN_PASS == _DEFAULT_ADMIN_PASS and BIND_HOST not in ("127.0.0.1", "localhost"):
        log.error("=" * 60)
        log.error("拒绝启动：使用默认 admin 密码 %r 同时绑定 %r。",
                  _DEFAULT_ADMIN_PASS, BIND_HOST)
        log.error("生产环境必须设置 PROBE_ADMIN_PASS 环境变量；")
        log.error("或将 PROBE_BIND=127.0.0.1 进入 localhost-only 开发模式。")
        log.error("=" * 60)
        sys.exit(2)

    if ADMIN_PASS == _DEFAULT_ADMIN_PASS:
        log.warning("=" * 60)
        log.warning("使用默认开发密码 %r（仅因 PROBE_BIND=%s 才允许）",
                    _DEFAULT_ADMIN_PASS, BIND_HOST)
        log.warning("=" * 60)

    if not os.environ.get("PROBE_COIN_SECRET", ""):
        log.warning("PROBE_COIN_SECRET 未设置，金币哈希使用默认开发盐值；"
                    "生产请在 .env 设定一个随机值（设定后不可更改）。")

    ai_worker.start_in_background()

    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    log.info("Probe listening on %s:%d (admin user=%s)", BIND_HOST, PORT, ADMIN_USER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        ai_worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
