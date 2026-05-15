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
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import ai_worker
import project_loader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("probe.server")

PORT = int(os.environ.get("PORT", "8080"))
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
PROJECTS_DIR = Path(__file__).parent / "projects"
ADMIN_USER = os.environ.get("PROBE_ADMIN_USER", "admin")
_DEFAULT_ADMIN_PASS = "probe-dev-pass"
ADMIN_PASS = os.environ.get("PROBE_ADMIN_PASS", _DEFAULT_ADMIN_PASS)
SESSION_TTL_SEC = 30 * 60  # 30 分钟
MAX_BODY = 64 * 1024  # 64KB form 上限


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
    q1: str, q2: str, q3: str, q4: str,
    custom_answers: list[str] | None,
) -> int:
    """原子占用名额 + 一次性 token 消费 + 插入 feedback。返回 feedback_id。"""
    now = int(time.time())
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
                     session_id, project_slug, wechat_id,
                     q1_answer, q2_answer, q3_answer, q4_answer,
                     custom_answers_json, submitted_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (session_id, project_slug, wechat_id,
                 q1, q2, q3, q4, custom_json, now),
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

    args.append(feedback_id)
    with db.transaction() as tx:
        tx.execute(f"UPDATE feedback SET {', '.join(sets)} WHERE id = ?", args)


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

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        log.info("%s - %s", self.address_string(), format % args)

    # ---- 路由分发 ----

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)

        if path == "/":
            self._send_html("<h1>Probe</h1><p>probe.niuniu869.com</p>")
            return
        if path == "/healthz":
            self._send_text("ok")
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
        if path == "/admin":
            if not self._require_admin():
                return
            self._handle_admin_list()
            return
        if path == "/admin/export.csv":
            if not self._require_admin():
                return
            self._handle_admin_export()
            return
        if path.startswith("/admin/feedback/"):
            if not self._require_admin():
                return
            try:
                fid = int(path.rsplit("/", 1)[-1])
            except ValueError:
                self._error_page("Not Found", "无效的反馈 ID", status=404)
                return
            self._handle_admin_detail(fid)
            return

        self._error_page("Not Found", f"路径不存在：{path}", status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/p/") and path.endswith("/feedback"):
            slug = path.split("/")[2]
            self._handle_feedback_submit(slug)
            return
        if path.startswith("/admin/feedback/"):
            if not self._require_admin():
                return
            parts = path.split("/")
            # /admin/feedback/<id>/<action>
            if len(parts) >= 5:
                try:
                    fid = int(parts[3])
                except ValueError:
                    self._error_page("Bad Request", "无效的反馈 ID", status=400)
                    return
                action = parts[4]
                self._handle_admin_action(fid, action)
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

        session_id = create_session(slug, token)
        slots_left = project["max_feedback_count"] - project["reserved_count"]

        self._send_html(render("project_card.html", {
            "name": esc(project["name"]),
            "description": esc(project["description"]),
            "trial_url": esc(project["trial_url"]),
            "slug": esc(slug),
            "session_id": esc(session_id),
            "max_feedback_count": project["max_feedback_count"],
            "reserved_count": project["reserved_count"],
            "slots_left": slots_left,
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
                custom_block += (
                    f'<label>自定义 {i+1}：{esc(q)}</label>'
                    f'<textarea name="custom_{i}" required></textarea>'
                )

        prefill = prefill or {}
        error_block = (
            f'<p class="warn">{esc(error)}</p>' if error else ""
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

        prefill = {"q1": q1, "q2": q2, "q3": q3, "q4": q4, "wechat_id": wechat_id}

        if not all([session_id, wechat_id, q1, q2, q3, q4]):
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
                q1=q1, q2=q2, q3=q3, q4=q4,
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
        except ValueError as exc:
            # 检查是否是 UNIQUE 约束（同 wechat 同项目）
            msg = str(exc)
            if "UNIQUE" in msg or "unique" in msg:
                msg = "你已经为这个项目提交过反馈了。"
            self._handle_feedback_form(
                slug, {"s": [session_id]},
                error=msg, prefill=prefill,
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
        self._send_html(render("receipt.html", {
            "name": esc(project["name"]) if project else esc(slug),
            "session_id": esc(session_id),
            "feedback_id": esc(fid),
        }))

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
            "total": total,
            "rows": "\n".join(rows_html) or "<tr><td colspan=9 class=muted>暂无反馈</td></tr>",
        }))

    def _handle_admin_detail(self, fid: int) -> None:
        row = db.fetch_feedback(fid)
        if row is None:
            self._error_page("Not Found", "反馈不存在", status=404)
            return

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
            "id": row["id"],
            "project_slug": esc(row["project_slug"]),
            "submitted_at": esc(time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(row["submitted_at"]))),
            "q1": esc(row["q1_answer"]),
            "q2": esc(row["q2_answer"]),
            "q3": esc(row["q3_answer"]),
            "q4": esc(row["q4_answer"]),
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
        writer.writerow(["feedback_id", "project_slug", "wechat_id",
                         "credit_confirmed", "confirmed_at_human"])
        rows = db.get_conn().execute(
            """SELECT id, project_slug, wechat_id, credit_confirmed, submitted_at
               FROM feedback WHERE payout_status = 'confirmed'
               ORDER BY id ASC"""
        ).fetchall()
        for r in rows:
            writer.writerow([
                r["id"], r["project_slug"], r["wechat_id"] or "",
                r["credit_confirmed"] or "",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["submitted_at"])),
            ])
        data = "﻿" + buf.getvalue()  # BOM for Excel CN
        self._send_text(data, ctype="text/csv; charset=utf-8")


# ---- 启动入口 ----


def main() -> None:
    db.init_schema()
    count = project_loader.load_all(PROJECTS_DIR)
    log.info("loaded %d projects", count)

    # 默认 admin 密码用于本地开发；生产环境必须通过 env 覆盖，否则任何
    # 知道 probe.niuniu869.com 的人都能用默认凭据登录后台。
    if ADMIN_PASS == _DEFAULT_ADMIN_PASS:
        log.warning("=" * 60)
        log.warning("PROBE_ADMIN_PASS 未设置，使用默认开发密码 %r", _DEFAULT_ADMIN_PASS)
        log.warning("生产环境必须设置 PROBE_ADMIN_PASS 环境变量！")
        log.warning("=" * 60)

    ai_worker.start_in_background()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("Probe listening on :%d (admin user=%s)", PORT, ADMIN_USER)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
        ai_worker.stop()
        server.server_close()


if __name__ == "__main__":
    main()
