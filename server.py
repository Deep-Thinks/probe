"""Probe HTTP server (Python stdlib only)。

实现 plan §API 路由（最小集）+ 启动时加载 projects/*.json + 启动 AI worker 线程。
"""

from __future__ import annotations

import base64
import bisect
import csv
import hashlib
import hmac
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import comb
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import antifraud
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


# ---- 复活抽奖（spec 2026-05-20，v7 = 12 行抽奖板） ----
# 详细数学：scripts/calibrate_lottery.py。改本表必须同步该脚本并重跑校准。
# - 理论 EV = 100.03%（按倍率口径；用户「1 金币 = 5 USD」）
# - 头奖（≥1000%）双尾合并概率 ≈ 1/2048（满足"至少 1/1000-1/2000"约束）
LOTTERY_N_ROWS = 12
LOTTERY_MULTIPLIERS = (3500, 690, 240, 124, 96, 85, 76,
                        85, 96, 124, 240, 690, 3500)
# 1 金币兑换 N 次抽奖（N=10）；1 金币 EV 在公益站 = $5 USD（与人民币 1:1 对位）。
# 每次抽奖基准 USD 分 = 5 USD / 10 抽 = 50 cents/抽。倍率（百分点）× 50 / 100 = USD 分。
DRAWS_PER_COIN = 10
USD_BASELINE_CENTS = 50
# 公益站捐赠人面板「折合人民币」用的固定汇率。dogfood 用硬编码；
# 偏差几毛对 admin 心算口径影响可忽略。要精确随实时汇率走时改这里即可。
USD_TO_CNY_RATE = 7.2

# 二项分布 CDF（启动期一次性算好，供 HMAC → slot 反查用）。
_LOTTERY_PMF = [comb(LOTTERY_N_ROWS, k) / (2 ** LOTTERY_N_ROWS)
                 for k in range(LOTTERY_N_ROWS + 1)]
_LOTTERY_CDF = []
_acc = 0.0
for _p in _LOTTERY_PMF:
    _acc += _p
    _LOTTERY_CDF.append(_acc)
# 末位强制为 1.0 防浮点累加误差让 bisect 错位。
_LOTTERY_CDF[-1] = 1.0
del _acc, _p

# 启动期对齐校验：槽位数 = 行数 + 1，避免静默不一致。
assert len(LOTTERY_MULTIPLIERS) == LOTTERY_N_ROWS + 1


def compute_lottery_slot(server_seed_hex: str, client_seed: str, nonce: int) -> int:
    """Provably Fair：HMAC-SHA256(server_seed, client_seed:nonce) → uniform [0,1)
    → 反查二项 CDF 得 slot (0..LOTTERY_N_ROWS)。

    用户可离线复算：见 /revive/verify。
    """
    digest = hmac.new(
        bytes.fromhex(server_seed_hex),
        f"{client_seed}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    rand_u = int.from_bytes(digest[:8], "big") / (1 << 64)
    return bisect.bisect_left(_LOTTERY_CDF, rand_u)


# 抽奖人署名长度上限（与 spec 一致；UI 端 maxlength 一并约束）。
DONOR_LABEL_MAX = 32
DONOR_LABEL_DEFAULT_PREFIX = "匿名好人"


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


# ---- Agent API 调用说明（贴给外部 AI agent 用的整段文案） ----

# 占位符在运行时用 .replace() 而非 .format() 替换，避免 JSON 大括号冲突。
# 内容刻意做成"agent 一眼能用、无需上下游解释"的体例：先给完整 curl
# 命令，再列字段语义，最后补业务背景，方便丢进 system prompt / 临时
# 工具说明里。
AGENT_API_INSTRUCTIONS = """# Probe API · Agent 接入说明

你正在调用 Probe 的 API。Probe 是一个 ¥10 一次的 AI 产品众测平台：
tester（普通用户）花 5-10 分钟答 4 个固定题 + 0-2 个自定义题，
后台 AI 自动按反馈深度打 1-5 分（映射 ¥3-¥15 报酬）。

Probe 给你两组接口：
  · 读 API：拉所有反馈数据做下游分析
  · 写 API：上线 / 更新 / 发布新版本（让 agent 替作者管项目）

## 鉴权（两组接口共用 HTTP Basic Auth）

  用户名：{user}
  密  码：{pw}

写接口本身**跳过同源 CSRF 校验**，agent 可以从任何环境直接调。

=============================================================
# 一、读 API：拉反馈数据
=============================================================

## 端点

GET {origin}/admin/api/feedback.json

## 调用示例

curl -u '{user}:{pw}' \\
  '{origin}/admin/api/feedback.json?status=done&limit=100'

## 过滤参数（全可选）

  since=<Unix ts>   只返回 submitted_at >= since 的反馈
  until=<Unix ts>   只返回 submitted_at <  until 的反馈
  project=<slug>    按项目 slug 过滤（见下方"项目列表"）
  status=<enum>     ai_status：pending | processing | done | failed
  payout=<enum>     payout_status：na | suggested | confirmed | paid | rejected
  limit=<N>         返回条数，默认 100，最大 500
  order=asc|desc    按 id 排序，默认 desc

## 返回结构

{
  "count": 100,                               // 本次返回条数
  "items": [{
    "id": 14,                                 // 反馈唯一 ID
    "project_slug": "oriself",                // 哪个项目
    "project_version": "v1",                  // 项目版本快照（同产品不同版本反馈互相独立）
    "source": "公开大厅 | 定向渠道 · <token>", // 反馈来源归因
    "wechat_hash": "<sha256>",                // 微信号单向哈希（可用于跨反馈聚合同一 tester）
    "submitted_at": 1779288123,               // Unix 时间戳
    "time_on_task_sec": 483,                  // 用户开卡到提交耗时（秒）

    "answers": {                              // tester 填的内容
      "q1": "第一印象 / 你以为这是什么",
      "q2": "你试了什么操作，按什么顺序",
      "q3": "在哪一步卡住，期待什么",
      "q4": "什么情况下你会关掉",
      "q5": "如果让你改一个地方，改什么",
      "custom": ["...", "..."]                // 作者自定义题答案（0-2 条）
    },

    "ai": {                                   // AI worker 评分结果
      "status": "done",                       // pending/processing/done/failed
      "depth_score": 4,                       // 反馈深度 1-5
                                              //   1 = 表面赞，无细节（"挺好的"）
                                              //   3 = 指出了某个具体页面或操作
                                              //   5 = 给出具体页面/操作/期望差异/改进建议四件套
      "depth_rationale": "...",               // AI 给出的打分理由
      "stuck_step": "登录后的引导第二屏",     // AI 推测的卡点
      "stuck_confidence": 0.85,               // AI 对卡点判断的自信度 0-1
      "followup_questions": ["...", "..."],   // AI 建议作者去追问的问题
      "risk_flags": [                         // 风险标记（命中任何一个 → 后台禁用一键确认）
        "prompt_injection_attempt",           // tester 试图给 AI 注入指令
        "duplicate_content",                  // 内容与早先反馈完全重复
        "semantic_duplicate",                 // 疑似改写（语义重复）
        "submitted_too_fast",                 // 提交太快，疑似未真实体验
        "no_specifics"                        // 反馈太泛，无具体细节
      ],
      "model_used": "deepseek/deepseek-chat",
      "attempts": 1
    },

    "payout": {                               // 付款状态（与 ai 状态独立的另一根管线）
      "status": "suggested",                  // na | suggested | confirmed | paid | rejected
      "credit_suggested": 12,                 // AI 建议金额（¥）
      "credit_confirmed": null,               // 作者确认金额（确认后才有值）
      "notes": null,
      "paid_at": null                         // Unix ts，标记已转账时填
    },

    "antifraud": {                            // 反作弊
      "content_digest": "...",                // 语义指纹（用于检测同义改写）
      "dup_of_feedback_id": null              // 若被判同义重复，指向被复制的早先反馈 ID
    }
  }]
}

## 错误返回

  401 + "401 Unauthorized"           鉴权失败
  400 + {"error": "..."}             参数非法

## 隐私边界

- 接口**不返回** wechat_id 原文。
- wechat_hash 是单向哈希，跨 30 天 PII 清理仍存活，可用于聚合同一
  tester 的多条反馈。
- 30 天后原始 wechat_id 会被擦除，但 wechat_hash 保留。

## 业务关键概念（让你理解数据语义）

- 1 个项目 = 1 个被评测的 AI 产品
- depth_score 是核心信号：5 分意味着 tester 给出了具体页面 + 具体操作
  + 期望差异 + 改进建议四件套；1 分意味着"挺好的 / 没问题"这类表面赞
- 反馈双轴状态机：AI 管道（ai_status）× 付款管道（payout_status）
  互相独立——AI 评分完成 ≠ 已付款
- 来源（source）：「公开大厅」= 任务大厅 /hall 的随便参与流量；
  「定向渠道 · <token>」= 作者主动发出的专属邀请链接

=============================================================
# 二、写 API：上线 / 更新 / 发布新版本
=============================================================

## A. 创建新项目（上线一个新产品给 tester 评测）

POST {origin}/admin/api/projects
Content-Type: application/json

请求体（JSON 对象）：
  slug                必填，URL 友好的项目唯一 ID，匹配
                      ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$
  name                必填，项目展示名（中文 OK）
  description         必填，给 tester 看的项目说明（一两句话）
  trial_url           必填，http(s) 试用链接
  max_feedback_count  必填，名额上限 1-100（标准额度推荐 10）
  custom_questions    可选，0-2 条字符串数组（额外想问 tester 的问题）
  listed              可选 bool（默认 false）。
                      true  → 公开到任务大厅 /hall，谁都能参与
                      false → 定向项目，仅持有作者发的邀请 token 可参与
  version             可选，默认 "v1"

成功 201：
  { "ok": true, "slug": "...", "version": "v1", "listed": true,
    "card_url": "...", "admin_url": "...", "hall_url": "..." }

失败 400 / 409：{ "error": "..." }

调用示例（公开到大厅、10 名额、零自定义题）：

curl -u '{user}:{pw}' -X POST \\
  '{origin}/admin/api/projects' \\
  -H 'Content-Type: application/json' \\
  -d '{
    "slug": "my-product",
    "name": "我的 AI 产品",
    "description": "5 分钟体验后告诉我你的卡点。",
    "trial_url": "https://my-product.example.com/",
    "max_feedback_count": 10,
    "custom_questions": [],
    "listed": true
  }'

## B. 更新项目（部分字段 patch）

POST {origin}/admin/api/projects/<slug>
Content-Type: application/json

可选字段（未提供保持原值）：name / description / trial_url /
max_feedback_count / custom_questions / listed。

注意：slug 不可改（slug 是项目身份）；版本号变更走 /release。

成功 200：{ "ok": true, "slug": "...", "updated_fields": [...] }

调用示例（只改描述 + 关闭大厅，转为定向）：

curl -u '{user}:{pw}' -X POST \\
  '{origin}/admin/api/projects/my-product' \\
  -H 'Content-Type: application/json' \\
  -d '{"description": "新描述", "listed": false}'

## C. 发布新版本（version 自增 + reserved_count 归零）

POST {origin}/admin/api/projects/<slug>/release
Content-Type: application/json

请求体：
  version              必填，新版本号（不能与当前相同）
  trial_url            可选，顺便更新试用链接
  description          可选，顺便更新描述
  max_feedback_count   可选，顺便更新名额上限

副作用：reserved_count 归零（新版本重新开放名额）；旧反馈的
project_version 不变，按版本沉淀。

成功 200：{ "ok": true, "old_version": "v1", "new_version": "v2",
            "reserved_count_reset": true }

调用示例（v1 → v2 同时换试用链接）：

curl -u '{user}:{pw}' -X POST \\
  '{origin}/admin/api/projects/my-product/release' \\
  -H 'Content-Type: application/json' \\
  -d '{"version": "v2", "trial_url": "https://v2.my-product.example.com/"}'
"""


def _render_agent_api_block(origin: str) -> str:
    """渲染 /admin/projects 上的 Agent 接入说明卡片（折叠 + 一键复制）。

    把 module-level 的 `AGENT_API_INSTRUCTIONS` 模板填充凭据与
    origin，塞进一个 textarea，加复制按钮。整段在 admin 鉴权之
    后才能访问到，但仍含明文密码 → 顶部加 ⚠ 提示。
    """
    payload = (AGENT_API_INSTRUCTIONS
               .replace("{origin}", origin)
               .replace("{user}", ADMIN_USER)
               .replace("{pw}", ADMIN_PASS))
    body = esc(payload)
    return (
        '<div class="card">'
        '<h3>Agent 接入说明（一键复制给你的 AI 助手）</h3>'
        '<p>把下面这段完整文案复制粘贴给任意 AI agent（Claude / GPT / '
        '本地脚本…），它就能直接调 Probe 的只读 API 拉取所有反馈数据。'
        '内容含 endpoint、字段语义、业务背景，agent 看完即可独立调用。</p>'
        '<p class="muted">⚠ 文案中包含 admin 明文密码，请勿截图外传或'
        '粘贴到不可信的第三方系统。仅给你自己的 agent / 私有脚本使用。</p>'
        '<p>'
        '<button class="btn" type="button" id="copy-agent-instr">'
        '复制全部</button> '
        '<span class="muted" id="copy-agent-status"></span>'
        '</p>'
        '<details>'
        '<summary class="muted">展开查看完整文案（读 + 写 API 两组接口）</summary>'
        f'<textarea id="agent-instr-text" readonly rows="22" '
        'style="width:100%;font-family:ui-monospace,Menlo,Consolas,'
        'monospace;font-size:12px;line-height:1.5;white-space:pre;'
        'overflow:auto;margin-top:8px;padding:10px;border:1px solid '
        f'#ddd;border-radius:6px;background:#fafafa">{body}</textarea>'
        '</details>'
        '<script>'
        '(function(){'
        'var btn=document.getElementById("copy-agent-instr");'
        'var ta=document.getElementById("agent-instr-text");'
        'var st=document.getElementById("copy-agent-status");'
        'if(!btn||!ta)return;'
        'btn.addEventListener("click",function(){'
        'var txt=ta.value;'
        'var done=function(){st.textContent="已复制 ✓";'
        'setTimeout(function(){st.textContent="";},2000);};'
        'var fail=function(){'
        'ta.focus();ta.select();'
        'try{document.execCommand("copy");done();}'
        'catch(e){st.textContent="复制失败，请手动选中文本复制";}};'
        'if(navigator.clipboard&&navigator.clipboard.writeText){'
        'navigator.clipboard.writeText(txt).then(done,fail);}'
        'else{fail();}'
        '});'
        '})();'
        '</script>'
        '</div>'
    )


# ---- /coins 无认证端点的 IP 级频率限制 ----

# /coins 是无登录公开查询，输微信号即返回余额，存在微信号枚举去匿名化面。
# 这里做 IP 级滑动窗口限流，把批量枚举成本拉高到不可用。
# ThreadingHTTPServer 每请求一线程，故计数器必须加锁。
_COINS_RATE_LIMIT = 8          # 每窗口允许的查询次数
_COINS_RATE_WINDOW = 60        # 窗口长度（秒）
_coins_rate_lock = threading.Lock()
_coins_rate_hits: dict[str, list[float]] = {}


def _coins_rate_ok(client_ip: str) -> bool:
    """滑动窗口频率检查。返回 True 放行、False 限流。线程安全。"""
    now = time.time()
    with _coins_rate_lock:
        hits = [t for t in _coins_rate_hits.get(client_ip, [])
                if now - t < _COINS_RATE_WINDOW]
        if len(hits) >= _COINS_RATE_LIMIT:
            _coins_rate_hits[client_ip] = hits
            return False
        hits.append(now)
        _coins_rate_hits[client_ip] = hits
        # 顺手回收久未活动的 IP，防止 dict 在长期运行下无限增长。
        if len(_coins_rate_hits) > 4096:
            stale = [ip for ip, ts in _coins_rate_hits.items()
                     if all(now - t >= _COINS_RATE_WINDOW for t in ts)]
            for ip in stale:
                del _coins_rate_hits[ip]
        return True


# ---- admin 侧栏 + 项目表单校验 ----

ADMIN_NAV = (
    ("dashboard", "/admin", "数据看板"),
    ("feedback", "/admin/feedback", "反馈列表"),
    ("users", "/admin/users", "用户面板"),
    ("donors", "/admin/donors", "捐赠人"),
    ("recruit", "/admin/recruit", "招募工具"),
    ("projects", "/admin/projects", "项目管理"),
)

# wechat_hash 是 SHA-256 hex（见 db.wechat_hash），统一用此正则兜底校验。
# 防止 URL 路径段塞入任意字符直接喂 SQL，或在 access.log 留下意外 PII。
WECHAT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


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

    # 反作弊：内容哈希（精确查重键）+ 开卡到提交耗时。提交期只记录、不拦截，
    # 检测与判定全部在 ai_worker._run_antifraud 内进行。
    content_hash = antifraud.content_hash(
        antifraud.combined_text(q1, q2, q3, q4, q5, custom_answers))
    time_on_task = now - session["started_at"]

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
                     custom_answers_json, submitted_at, project_version,
                     content_hash, time_on_task_sec
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, project_slug, wechat_id, wh,
                 q1, q2, q3, q4, q5, custom_json, now, project_version,
                 content_hash, time_on_task),
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


# ---- 反作弊风险标签：命中任一即在 admin 详情页禁用一键确认 ----
# 由 ai_worker._run_antifraud 写入 feedback.ai_risk_flags_json（见 antifraud.py）。
ANTIFRAUD_FLAGS = {
    "prompt_injection_attempt",
    "duplicate_content",
    "semantic_duplicate",
    "submitted_too_fast",
}


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
    """合法状态转移 + 必要字段写入。

    设计取舍（修 codex 五轮 P1，2026-05-27）：paid 转移**不**触碰 coin_donations。
    - 单条详情页路径只动一笔反馈，无法判断"该笔付款"是否含金币扣减；强行批量
      settle 该用户全部 donations 会让金币被早付反馈"全权吸收"，admin 在剩余
      confirmed 上按毛额转账时无扣减信号 → overpay。
    - donation 结算只在用户面板批量路径里发生（mark_user_specific_confirmed_to_paid），
      因为那里是按 net 转账的、定义良好的"周期"边界。
    - 单条 mark-paid 在路由层有 guard：tester 存在未结清捐赠时 409，强制走批量。
    """
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

    def _send_json(self, obj: dict, status: int = 200) -> None:
        """JSON 响应（用于 /revive/draw 与 /p/<slug>/eval_status）。"""
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def _read_json_body(self) -> tuple[dict | None, str | None]:
        """读 JSON 请求体（admin write API 用）。返回 (obj, error_msg)。

        请求体必须是 JSON 对象（非数组、非标量）；超 MAX_BODY 直接拒。
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None, "请求体为空"
        if length > MAX_BODY:
            return None, f"请求体过大（>{MAX_BODY} 字节）"
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败：{e}"
        if not isinstance(obj, dict):
            return None, "请求体必须是 JSON 对象"
        return obj, None

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

    def _client_ip(self) -> str:
        """取真实客户端 IP。生产走 nginx 反代，信任 X-Forwarded-For 最右一跳
        （nginx 用 $proxy_add_x_forwarded_for 追加的真实 IP，客户端伪造的前缀
        无法影响最右值）；无反代时回退到 socket 对端地址。"""
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
        return self.client_address[0]

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
        if path == "/revive":
            self._handle_revive(qs)
            return
        if path == "/revive/verify":
            self._handle_revive_verify(qs)
            return
        if path.startswith("/static/"):
            self._serve_static(path)
            return

        # tester 端
        if path.startswith("/p/"):
            parts = path.split("/")
            # /p/<slug> | /p/<slug>/feedback | /p/<slug>/receipt | /p/<slug>/eval_status
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
                if tail == "eval_status":
                    self._handle_eval_status(slug, qs)
                    return

        # admin 端
        if path == "/admin" or path.startswith("/admin/"):
            if not self._require_admin():
                return
            if path == "/admin":
                self._handle_admin_dashboard()
            elif path == "/admin/feedback":
                self._handle_admin_list(qs)
            elif path == "/admin/users":
                self._handle_admin_users(qs)
            elif path == "/admin/donors":
                self._handle_admin_donors(qs)
            elif path == "/admin/export.csv":
                self._handle_admin_export()
            elif path == "/admin/api/feedback.json":
                self._handle_admin_api_feedback(qs)
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
        if path == "/revive/draw":
            self._handle_revive_draw()
            return
        if path == "/admin" or path.startswith("/admin/"):
            if not self._require_admin():
                return
            # admin write API：调用方非浏览器，没有 origin 概念；Basic Auth
            # 本身已是 bearer-like secret，跳过 CSRF same-origin 校验。
            if path.startswith("/admin/api/"):
                if path == "/admin/api/projects":
                    self._handle_admin_api_project_create()
                elif (path.startswith("/admin/api/projects/")
                      and path.endswith("/release")):
                    rel_slug = path[len("/admin/api/projects/"):-len("/release")]
                    self._handle_admin_api_project_release(rel_slug)
                elif path.startswith("/admin/api/projects/"):
                    up_slug = path[len("/admin/api/projects/"):]
                    self._handle_admin_api_project_update(up_slug)
                else:
                    self._send_json(
                        {"error": f"路径不存在：{path}"}, status=404)
                return
            # CSRF 防护：浏览器 admin POST 必须从同源页面发起
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
            elif (path.startswith("/admin/users/")
                  and path.endswith("/mark-all-paid")):
                wh = path[len("/admin/users/"):-len("/mark-all-paid")]
                self._handle_admin_mark_user_paid(wh)
            elif (path.startswith("/admin/users/")
                  and path.endswith("/pay-now")):
                wh = path[len("/admin/users/"):-len("/pay-now")]
                self._handle_admin_pay_user_now(wh)
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
        # 仅识别白名单后缀；其余一律 octet-stream 防 MIME sniff。
        if name.endswith(".css"):
            ctype = "text/css"
        elif name.endswith(".js"):
            ctype = "application/javascript"
        else:
            ctype = "application/octet-stream"
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
            if ("uniq_wechat_per_project" in msg
                    or "feedback.wechat_id" in msg
                    or "feedback.wechat_hash" in msg):
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

        try:
            fb = db.fetch_feedback(int(fid)) if fid else None
        except ValueError:
            fb = None

        # 历史金币块（向后兼容）：余额聚合摘要。
        coin_block = ('<p class="muted">本次反馈的金额会在作者确认后并入'
                      '可提现金币。</p>')
        if fb is not None and fb["wechat_hash"]:
            bal = db.coin_balance(fb["wechat_hash"])
            coin_block = (
                f'<p>当前可提现 <strong>{bal["withdrawable"]}</strong> 金币 · '
                f'评估中 {bal["pending_count"]} 条（含本次）。</p>'
            )

        # 扣 1 复活公益站（spec 2026-05-20）：AI 评估剧场 + 自愿入口。
        # 三态：pending / done / 缺 fid（理论上不该发生）。
        if fb is None:
            revive_block = ""
        elif fb["ai_status"] == "done":
            # 已评估完：展示金币 + 引导（自愿献给公益站换抽奖，不抽就周末微信提现）。
            credit = fb["credit_suggested"] or 0
            draws_avail = credit * DRAWS_PER_COIN
            revive_block = (
                '<div class="card revive-card">'
                '<p class="meta">公益站 · 所有人帮助所有人</p>'
                f'<p style="font-size:24px;margin:0 0 4px">'
                f'你本次拿到 <strong>{credit}</strong> 金币（≈ ¥{credit}）</p>'
                '<p class="muted" style="margin:0 0 12px">'
                f'金币默认周末由作者微信转给你。<strong>愿意的话</strong>'
                f'可把 1 金币换成 {DRAWS_PER_COIN} 次抽奖（每抽 ≈ $0.50 EV，'
                '0%-3500% 倍率），抽到的额度全部投入「所有人帮助所有人」公益站。'
                '献多献少由你说了算，没抽完的金币照常微信提现。</p>'
                f'<p class="row"><a class="btn lottery-btn" '
                f'href="/revive?fid={int(fid)}&amp;s={esc(session_id)}">'
                f'去看看公益站（{draws_avail} 次抽奖待用）→</a></p>'
                '</div>'
            )
        else:
            # AI 评估中：骨架屏 + 自动 poll。JS 在 done 时一次性重排 DOM。
            revive_block = (
                '<div class="card revive-card" id="revive-card" '
                f'data-fid="{int(fid)}" data-slug="{esc(slug)}" '
                f'data-session-id="{esc(session_id)}">'
                '<p class="meta">公益站 · 所有人帮助所有人</p>'
                '<p style="font-size:18px;margin:0 0 8px">'
                'AI 正在估算你的金币…</p>'
                '<div class="coin-skeleton">'
                '<span></span><span></span><span></span>'
                '</div>'
                '<p class="muted" style="margin:8px 0 0">'
                '一般 10-30 秒。估好之后这里会显示你赚到的金币。</p>'
                '</div>'
                '<script>(function(){\n'
                '  var card=document.getElementById("revive-card");\n'
                '  if(!card)return;\n'
                '  var fid=card.dataset.fid;\n'
                '  var slug=card.dataset.slug;\n'
                '  var sid=card.dataset.sessionId;\n'
                f'  var DRAWS_PER_COIN={DRAWS_PER_COIN};\n'
                '  function render(credit){\n'
                '    var draws = credit * DRAWS_PER_COIN;\n'
                '    card.innerHTML=""+\n'
                '    "<p class=\\"meta\\">公益站 · 所有人帮助所有人</p>"+\n'
                '    "<div class=\\"coin-burst-wrap\\">"+\n'
                '      "<canvas class=\\"coin-burst-canvas\\" aria-hidden=\\"true\\"></canvas>"+\n'
                '      "<p class=\\"coin-burst\\">你本次拿到 <strong>"+credit+"</strong> 金币（≈ ¥"+credit+"）</p>"+\n'
                '    "</div>"+\n'
                '    "<p class=\\"muted\\" style=\\"margin:0 0 12px\\">金币默认周末由作者微信转给你。<strong>愿意的话</strong>可把 1 金币换成 "+DRAWS_PER_COIN+" 次抽奖（每抽 ≈ $0.50 EV，0%-3500% 倍率），抽到的额度全部投入「所有人帮助所有人」公益站。献多献少由你说了算，没抽完的金币照常微信提现。</p>"+\n'
                '    "<p class=\\"row\\"><a class=\\"btn lottery-btn\\" href=\\"/revive?fid="+fid+"&s="+encodeURIComponent(sid)+"\\">去看看公益站（"+draws+" 次抽奖待用）→</a></p>";\n'
                '    /* 爆金币粒子：N=credit 枚向上飞抛 + 重力 + 自旋 + 淡出。 */\n'
                '    var cv = card.querySelector(".coin-burst-canvas");\n'
                '    if(cv && window.ProbeFX){\n'
                '      var sys = window.ProbeFX.makeCoinSystem(cv);\n'
                '      setTimeout(function(){\n'
                '        sys.spawn({\n'
                '          x: sys.getW()/2,\n'
                '          y: sys.getH()*0.55,\n'
                '          n: Math.min(credit, 15),\n'
                '          radius: 12,\n'
                '          spawnSpread: 36,\n'
                '          vyMin: 4, vyMax: 8,\n'
                '          vxRange: 7,\n'
                '          life: 1500\n'
                '        });\n'
                '      }, 200);\n'
                '    }\n'
                '  }\n'
                '  function poll(){\n'
                '    fetch("/p/"+encodeURIComponent(slug)+"/eval_status?fid="+fid,{cache:"no-store"})\n'
                '      .then(function(r){return r.json()})\n'
                '      .then(function(d){\n'
                '        if(d && d.ai_status==="done" && d.credit_suggested!=null){\n'
                '          render(d.credit_suggested);\n'
                '        }else if(d && d.ai_status==="failed"){\n'
                '          card.innerHTML="<p class=\\"meta\\">AI 评估失败</p><p class=\\"muted\\">作者会人工兜底处理，依然按 ¥3–¥15 转账。</p>";\n'
                '        }else{\n'
                '          setTimeout(poll, 3000);\n'
                '        }\n'
                '      })\n'
                '      .catch(function(){ setTimeout(poll, 5000); });\n'
                '  }\n'
                '  setTimeout(poll, 2000);\n'
                '})();</script>'
            )

        self._send_html(render("receipt.html", {
            "name": esc(project["name"]) if project else esc(slug),
            "session_id": esc(session_id),
            "feedback_id": esc(fid),
            "credit_range": _credit_range_label(token),
            "coin_block": coin_block,
            "revive_block": revive_block,
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
        # 无认证端点：先过 IP 级限流，防微信号余额枚举。
        if not _coins_rate_ok(self._client_ip()):
            self._handle_coins(error="查询过于频繁，请稍后再试。")
            return
        form = self._read_form()
        wechat_id = (form.get("wechat_id") or [""])[0].strip()
        if not wechat_id:
            self._handle_coins(error="请输入微信号。")
            return
        wh = db.wechat_hash(wechat_id)
        bal = db.coin_balance(wh)
        if bal["withdrawable"] >= 100:
            gate = ('<p>已达 100 金币门槛——可联系作者 <strong>niuniu869</strong> '
                    '用 100 金币兑换一次咨询，或周末微信提现。</p>')
        else:
            gate = (f'<p class="muted">距 100 金币门槛还差 '
                    f'{100 - bal["withdrawable"]} 金币。</p>')
        # 公益站行（spec 2026-05-20）：捐过 → 显示成绩 + 入口；没捐过 → 引导。
        if bal["donated_count"] > 0:
            donation_line = (
                f'<p style="margin-top:8px">'
                f'已为公益站投入 <strong>${bal["donated_usd_cents"]/100:.2f}</strong>'
                f'（{bal["donated_count"]} 次抽奖）· '
                f'<a href="/revive?wh={esc(wh)}">继续抓 →</a></p>'
            )
        elif bal["draws_remaining"] >= 1:
            donation_line = (
                f'<p style="margin-top:8px" class="muted">'
                f'还能去公益站摇 {bal["draws_remaining"]} 次复活抽奖 · '
                f'<a href="/revive?wh={esc(wh)}">去看看 →</a></p>'
            )
        else:
            donation_line = ""
        result = (
            '<div class="card">'
            f'<h3>{esc(wechat_id)} 的金币</h3>'
            f'<p style="font-size:28px;margin:8px 0">'
            f'<strong>{bal["withdrawable"]}</strong> 金币可提现</p>'
            f'<p class="muted">已提现 {bal["paid"]} 金币 · '
            f'{bal["pending_count"]} 条反馈评估中（金额未定）</p>'
            f'{gate}'
            f'{donation_line}'
            '</div>'
        )
        self._handle_coins(result=result, wechat_prefill=wechat_id)

    # ---- 路由实现：公益站 / 复活抽奖（spec 2026-05-20） ----

    def _resolve_donor_context(self, qs: dict) -> dict | None:
        """从 querystring 推导捐赠人上下文。

        支持两条入口：
        - ?fid=<feedback_id>&s=<session_id>：从 receipt 跳转，校验 session 后取 wechat_hash
        - ?wh=<wechat_hash>：从 /coins 跳转或他处分享（hash 是不可逆的，无 PII 风险）

        返回 None = 未识别捐赠人（页面要求用户输入微信号或回到 receipt）。
        """
        fid = (qs.get("fid") or [""])[0]
        sid = (qs.get("s") or [""])[0]
        wh = (qs.get("wh") or [""])[0]
        if fid and sid:
            try:
                fb = db.fetch_feedback(int(fid))
            except ValueError:
                return None
            if fb is None or fb["session_id"] != sid:
                return None
            if not fb["wechat_hash"]:
                return None
            return {
                "wechat_hash": fb["wechat_hash"],
                "source_feedback_id": fb["id"],
                "from_receipt": True,
            }
        if wh and len(wh) == 64 and all(c in "0123456789abcdef" for c in wh):
            # 来自 /coins 跳转：wechat_hash 已经存在，找一条最新已评估反馈作为
            # source_feedback_id。若无（用户从未做过反馈），无法捐。
            row = db.get_conn().execute(
                """SELECT id FROM feedback
                   WHERE wechat_hash = ? AND ai_status = 'done'
                   ORDER BY id DESC LIMIT 1""",
                (wh,),
            ).fetchone()
            if row is None:
                return None
            return {
                "wechat_hash": wh,
                "source_feedback_id": row["id"],
                "from_receipt": False,
            }
        return None

    def _render_donations_wall(self, rows) -> str:
        """渲染「最近的复活者」墙（Python 端拼 HTML，模板不支持循环）。"""
        if not rows:
            return ('<p class="muted" style="margin:0">'
                    '还没有人复活过公益站。也许就由你开张？</p>')
        chunks = []
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"]))
            usd = r["usd_cents"] / 100
            mult = r["multiplier_pct"]
            highlight = (' style="color:var(--warn);font-weight:600"'
                         if mult >= 1000 else "")
            chunks.append(
                '<li class="donation-row">'
                f'<span class="donor">{esc(r["donor_label"])}</span>'
                f' 抓到 <span class="mult"{highlight}>{mult}%</span>'
                f' → 投入 <span class="usd">${usd:.2f}</span>'
                f' <span class="ts">· {esc(ts)}</span>'
                '</li>'
            )
        return '<ul class="donation-wall">' + "".join(chunks) + '</ul>'

    def _handle_revive(self, qs: dict) -> None:
        """公益站主场页（GET /revive）。"""
        donor = self._resolve_donor_context(qs)
        pool_cents = db.pool_total_usd_cents()
        pool_usd = pool_cents / 100
        donor_count = db.pool_donor_count()
        recent_rows = db.list_recent_donations(limit=20)
        wall_html = self._render_donations_wall(recent_rows)

        if donor is None:
            # 无捐赠人上下文：直接显示池子状态 + 引导回 receipt 或 /coins。
            donor_block = (
                '<div class="card">'
                '<h3>怎么参与？</h3>'
                '<p>公益站只接受「做完反馈赚到的金币」。先去任务大厅领一个评测做完，'
                '回到 receipt 页就能看到「扣 1 复活」入口。</p>'
                '<p class="row">'
                '<a class="btn" href="/hall">去任务大厅 →</a>'
                '<a href="/coins">查我的金币余额</a>'
                '</p>'
                '</div>'
            )
            self._send_html(render("revive.html", {
                "pool_usd": f"{pool_usd:,.2f}",
                "donor_count": donor_count,
                "donations_wall": wall_html,
                "donor_block": donor_block,
                "stage_block": "",
                "verify_anchor": "",
            }))
            return

        bal = db.coin_balance(donor["wechat_hash"])
        next_nonce = db.next_donation_nonce(
            donor["wechat_hash"], donor["source_feedback_id"])
        client_seed = secrets.token_hex(8)
        # 默认署名占位（用户可改）。N 用 donor_count + 1 让默认名字不撞车。
        default_label = f"{DONOR_LABEL_DEFAULT_PREFIX} #{donor_count + 1}"

        if bal["draws_remaining"] < 1:
            donor_block = (
                '<div class="card">'
                f'<h3>你的抽奖情况</h3>'
                f'<p>已抽 <strong>{bal["donated_count"]}</strong> 次 · '
                f'已为公益站投入 <strong>$'
                f'{bal["donated_usd_cents"]/100:.2f}</strong>。</p>'
                f'<p class="muted">本轮抽奖次数已用完。再做一个反馈，每个金币就能再换 '
                f'{DRAWS_PER_COIN} 次新机会。</p>'
                '<p class="row">'
                '<a class="btn" href="/hall">再去做一个 →</a>'
                '</p>'
                '</div>'
            )
            stage_block = ""
        else:
            donor_block = (
                '<div class="card">'
                f'<h3>你的抽奖情况</h3>'
                f'<p>还能抽 <strong>{bal["draws_remaining"]}</strong> 次 · '
                f'已抽 {bal["donated_count"]} 次 · '
                f'累计投入公益站 <strong>$'
                f'{bal["donated_usd_cents"]/100:.2f}</strong>。</p>'
                f'<p class="muted">规则：<strong>1 金币 = {DRAWS_PER_COIN} 次抽奖</strong>'
                f'（自愿）。已被抽奖消耗的整块金币 <strong>{bal["consumed_coins"]}</strong> · '
                f'仍可周末微信提现的金币 <strong>{bal["withdrawable"]}</strong>。'
                '不想抽？直接关掉本页，金币周末照常打你微信。</p>'
                '</div>'
            )
            # 抽奖舞台：抽奖板 canvas + 署名输入 + 按钮。
            stage_block = (
                '<div class="card lottery-stage">'
                '<h3>复活抽奖板</h3>'
                '<canvas id="lottery-board" width="360" height="480"></canvas>'
                '<form id="donate-form" class="donate-form">'
                f'<input type="hidden" name="wh" value="{esc(donor["wechat_hash"])}">'
                f'<input type="hidden" name="fid" '
                f'value="{donor["source_feedback_id"]}">'
                f'<input type="hidden" name="client_seed" value="{esc(client_seed)}">'
                f'<input type="hidden" name="nonce" value="{next_nonce}">'
                '<label for="donor_label">署名（自由文本，重名无所谓）</label>'
                f'<input type="text" id="donor_label" name="donor_label" '
                f'maxlength="{DONOR_LABEL_MAX}" '
                f'value="{esc(default_label)}" required>'
                '<p class="row mt">'
                '<button type="submit" class="btn lottery-btn">'
                '摇一次 →</button>'
                ' <button type="button" id="bulk-draw-btn" class="btn-secondary"'
                ' style="margin-left:8px">⚡ 摇 10 次</button>'
                '<span id="draw-status" class="muted"></span>'
                '</p>'
                '<div id="draw-result"></div>'
                '</form>'
                '</div>'
            )

        # 验证抽屉（Provably Fair 简介）。
        verify_anchor = (
            '<details class="verify-drawer">'
            '<summary>这个抽奖凭什么公平？</summary>'
            '<div class="verify-body">'
            '<p>每次抽奖都用 <strong>Provably Fair</strong> 三元组：'
            '<code>server_seed</code> + <code>client_seed</code> + <code>nonce</code>。'
            '抽奖时立即披露 <code>server_seed</code>，你可用 Python 离线复算：</p>'
            '<pre><code>import hmac, hashlib, bisect\n'
            'from math import comb\n'
            f'PMF = [comb({LOTTERY_N_ROWS}, k) / 2**{LOTTERY_N_ROWS} '
            f'for k in range({LOTTERY_N_ROWS + 1})]\n'
            'CDF = [sum(PMF[:i+1]) for i in range(len(PMF))]\n'
            'digest = hmac.new(bytes.fromhex(server_seed),\n'
            '    f"{client_seed}:{nonce}".encode(), hashlib.sha256).digest()\n'
            'rand = int.from_bytes(digest[:8], "big") / (1 &lt;&lt; 64)\n'
            'slot = bisect.bisect_left(CDF, rand)\n'
            '</code></pre>'
            f'<p>抽奖板配置（13 行 14 槽）倍率表：<code>'
            f'{", ".join(str(m) for m in LOTTERY_MULTIPLIERS)}</code>'
            '</p>'
            '</div>'
            '</details>'
        )

        self._send_html(render("revive.html", {
            "pool_usd": f"{pool_usd:,.2f}",
            "donor_count": donor_count,
            "donations_wall": wall_html,
            "donor_block": donor_block,
            "stage_block": stage_block,
            "verify_anchor": verify_anchor,
        }))

    def _handle_revive_draw(self) -> None:
        """POST /revive/draw：执行一次抽奖。请求 form，返回 JSON。"""
        form = self._read_form()
        wh = (form.get("wh") or [""])[0].strip()
        fid_raw = (form.get("fid") or [""])[0].strip()
        client_seed = (form.get("client_seed") or [""])[0].strip()
        nonce_raw = (form.get("nonce") or [""])[0].strip()
        donor_label = (form.get("donor_label") or [""])[0].strip()

        if not wh or len(wh) != 64 or not all(c in "0123456789abcdef" for c in wh):
            return self._send_json({"error": "wechat_hash 格式不对"}, status=400)
        try:
            fid = int(fid_raw)
            nonce = int(nonce_raw)
        except ValueError:
            return self._send_json({"error": "fid / nonce 不是整数"}, status=400)
        if nonce < 0 or len(client_seed) < 4 or len(client_seed) > 64:
            return self._send_json({"error": "client_seed / nonce 参数非法"},
                                   status=400)
        if not donor_label:
            return self._send_json({"error": "署名不能为空"}, status=400)
        if len(donor_label) > DONOR_LABEL_MAX:
            donor_label = donor_label[:DONOR_LABEL_MAX]

        # source_feedback_id 必须属于这个 wechat_hash（防止 fid 注入）。
        fb = db.fetch_feedback(fid)
        if fb is None or fb["wechat_hash"] != wh:
            return self._send_json({"error": "源反馈不存在或归属不符"}, status=403)
        if fb["ai_status"] != "done":
            return self._send_json({"error": "源反馈尚未评估完成"}, status=400)

        # 生成 server_seed + 计算 slot + 入库。
        server_seed = secrets.token_hex(32)
        server_seed_hash = hashlib.sha256(server_seed.encode("utf-8")).hexdigest()
        slot = compute_lottery_slot(server_seed, client_seed, nonce)
        multiplier_pct = LOTTERY_MULTIPLIERS[slot]
        usd_cents = multiplier_pct * USD_BASELINE_CENTS // 100

        try:
            donation_id = db.record_donation(
                wechat_hash=wh,
                source_feedback_id=fid,
                donor_label=donor_label,
                slot_landed=slot,
                multiplier_pct=multiplier_pct,
                usd_cents=usd_cents,
                server_seed=server_seed,
                server_seed_hash=server_seed_hash,
                client_seed=client_seed,
                nonce=nonce,
            )
        except ValueError as exc:
            return self._send_json({"error": str(exc)}, status=400)
        except sqlite3.IntegrityError:
            return self._send_json({"error": "nonce 冲突，请刷新页面重试"},
                                   status=409)

        log.info("donation %d wh=%s... slot=%d mult=%d%% usd_cents=%d label=%r",
                 donation_id, wh[:8], slot, multiplier_pct, usd_cents, donor_label)

        # 抽完返回最新池子状态 + 新余额。
        pool_cents = db.pool_total_usd_cents()
        bal = db.coin_balance(wh)
        return self._send_json({
            "donation_id": donation_id,
            "slot": slot,
            "multiplier_pct": multiplier_pct,
            "usd_cents": usd_cents,
            "server_seed": server_seed,
            "server_seed_hash": server_seed_hash,
            "client_seed": client_seed,
            "nonce": nonce,
            "pool_usd": pool_cents / 100,
            "remaining": bal["draws_remaining"],
            "donated_count": bal["donated_count"],
            "donated_usd": bal["donated_usd_cents"] / 100,
        })

    def _handle_revive_verify(self, qs: dict) -> None:
        """GET /revive/verify?d=<donation_id>：单次抽奖的可验证披露。"""
        d_raw = (qs.get("d") or [""])[0]
        try:
            d_id = int(d_raw)
        except ValueError:
            self._error_page("Not Found", "无效的捐赠 ID", status=404)
            return
        d = db.fetch_donation(d_id)
        if d is None:
            self._error_page("Not Found", f"找不到捐赠 #{d_id}", status=404)
            return
        # 服务器现场用披露的 seed 再算一次 slot，给用户对照。
        recomputed = compute_lottery_slot(d["server_seed"], d["client_seed"],
                                        d["nonce"])
        match = "✓ 一致" if recomputed == d["slot_landed"] else "✗ 不一致"
        self._send_html(render("revive_verify.html", {
            "donation_id": d["id"],
            "donor_label": esc(d["donor_label"]),
            "server_seed": esc(d["server_seed"]),
            "server_seed_hash": esc(d["server_seed_hash"]),
            "client_seed": esc(d["client_seed"]),
            "nonce": d["nonce"],
            "slot_landed": d["slot_landed"],
            "slot_recomputed": recomputed,
            "match": esc(match),
            "multiplier_pct": d["multiplier_pct"],
            "usd_cents": d["usd_cents"],
            "n_rows": LOTTERY_N_ROWS,
            "multipliers_str": esc(", ".join(str(m) for m in LOTTERY_MULTIPLIERS)),
        }))

    def _handle_eval_status(self, slug: str, qs: dict) -> None:
        """GET /p/<slug>/eval_status?fid=...：极简 polling，返回 AI 评估状态。

        receipt 页在 AI 评估期（10-30s）每 3s poll 一次，状态变 done 时一次性
        渲染金币爆破 + 扣 1 CTA。
        """
        fid_raw = (qs.get("fid") or [""])[0]
        try:
            fid = int(fid_raw)
        except ValueError:
            return self._send_json({"error": "invalid fid"}, status=400)
        fb = db.fetch_feedback(fid)
        if fb is None or fb["project_slug"] != slug:
            return self._send_json({"error": "feedback not found"}, status=404)
        payload = {
            "ai_status": fb["ai_status"],
            "ai_attempts": fb["ai_attempts"],
            "credit_suggested": fb["credit_suggested"],
            "depth_score": fb["ai_depth_score"],
        }
        if fb["wechat_hash"]:
            bal = db.coin_balance(fb["wechat_hash"])
            payload["draws_remaining"] = bal["draws_remaining"]
        return self._send_json(payload)

    # ---- 路由实现：admin ----

    def _handle_admin_list(self, qs: dict[str, list[str]] | None = None) -> None:
        # 可选 wechat_hash 过滤（来自 /admin/users / /admin/donors 的「查看反馈」跳转）。
        # 不合法格式直接 400，避免把任意字符喂进 SQL 或留在 access.log。
        wh_filter: str | None = None
        if qs:
            wh_raw = (qs.get("wechat_hash", [""])[0] or "").lower()
            if wh_raw:
                if not WECHAT_HASH_RE.match(wh_raw):
                    self._error_page(
                        "Bad Request", "wechat_hash 格式不合法", status=400)
                    return
                wh_filter = wh_raw

        rows_html = []
        for row in db.list_feedback(limit=500, wechat_hash=wh_filter):
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
        filter_banner = ""
        if wh_filter:
            filter_banner = (
                f'<div class="card" style="border-left:4px solid #f39c12">'
                f'<p>已按 wechat_hash <code>{esc(wh_filter[:12])}…</code> 过滤 · '
                f'<a href="/admin/feedback">清除过滤 ×</a></p></div>'
            )
        self._send_html(render("admin_list.html", {
            "sidebar": _admin_sidebar("feedback"),
            "total": total,
            "filter_banner": filter_banner,
            "rows": "\n".join(rows_html) or (
                '<tr><td colspan="10"><div class="empty">'
                '<p>还没有反馈。</p>'
                '<p class="muted">招募 tester 后，提交的反馈会出现在这里。</p>'
                '</div></td></tr>'
            ),
        }))

    # ---- 用户面板 / 捐赠人面板（spec 2026-05-27） ----

    @staticmethod
    def _sort_header(label: str, key: str | None,
                     active: str, direction: str) -> str:
        """渲染可点击排序的 <th>：当前列点击翻转 dir，其他列默认 desc。"""
        if key is None:
            return f"<th>{label}</th>"
        if key == active:
            arrow = " ↑" if direction == "asc" else " ↓"
            new_dir = "asc" if direction == "desc" else "desc"
        else:
            arrow = ""
            new_dir = "desc"
        return f'<th><a href="?sort={key}&amp;dir={new_dir}">{esc(label)}{arrow}</a></th>'

    @staticmethod
    def _ident_cell(last_wechat_id: str | None, last_session_id: str | None) -> str:
        """统一渲染用户/捐赠人面板的「身份」列：wechat_id + 最近 session_id 前 8 位。"""
        sid_short = (last_session_id or "")[:8] or "—"
        if last_wechat_id:
            return (f'{esc(last_wechat_id)} '
                    f'<span class="muted">· sid {esc(sid_short)}</span>')
        return (f'<span class="muted">（已清理）· sid {esc(sid_short)}</span>')

    def _handle_admin_users(self, qs: dict[str, list[str]]) -> None:
        """GET /admin/users —— 待付款用户面板。"""
        sort = (qs.get("sort", [""])[0] or "due").lower()
        direction = (qs.get("dir", [""])[0] or "desc").lower()
        if sort not in db.PAYOUT_USER_SORT:
            sort = "due"
        if direction not in ("asc", "desc"):
            direction = "desc"

        rows = db.list_payout_users(sort=sort, direction=direction)

        total_users = len(rows)
        # 应付合计走 net 口径：金币从「总欠款」扣，不只从 confirmed 扣
        # （修 codex P1 二次：与行级 net_due 同口径，否则 suggested-only +
        # 已捐金币场景汇总会高估应付）。
        total_due = sum(
            max(0, (r["amount_suggested"] or 0)
                + (r["amount_confirmed"] or 0)
                - (r["coins_consumed"] or 0))
            for r in rows)
        total_suggested = sum(r["amount_suggested"] or 0 for r in rows)

        rows_html = []
        for r in rows:
            wh = r["wechat_hash"]
            ident = self._ident_cell(r["last_wechat_id"], r["last_session_id"])
            last_at = time.strftime(
                "%m-%d %H:%M", time.localtime(r["last_submitted_at"] or 0))
            coins = r["coins_consumed"] or 0
            sug_gross = r["amount_suggested"] or 0
            confirmed_gross = r["amount_confirmed"] or 0
            # 修 codex P1（2026-05-27 二次）：金币从总欠款扣，不只从 confirmed 扣。
            # 之前 suggested-only 用户被双重补偿：UI 默认填 suggested 全额，事务
            # 又把对应金币 settled → tester 双拿。
            net_due = max(0, sug_gross + confirmed_gross - coins)
            # 「仅标 confirmed」按钮的局部 net（事务也结算金币所以扣减）
            confirmed_net = max(0, confirmed_gross - coins)
            # 「待付（毛）」列只展示 confirmed 毛额；金币消耗放在「应付合计」上扣减
            confirmed_cell = f"<strong>¥{confirmed_gross}</strong>"
            if coins > 0:
                confirmed_cell += (
                    f' <span class="muted" title="该 tester 累计抽奖消耗 '
                    f'{coins} 枚金币，从总应付里扣">(金 -{coins})</span>'
                )
            # 「打钱」按钮 —— 一键全付：自动 confirm suggested + mark 全部 paid。
            # 适用于「不想逐条人工审核就直接付清」的高信任场景。
            # 隐藏字段：suggested_ids + confirmed_ids 双重快照（set 比对防 TOCTOU）。
            pay_btn = ""
            if (r["cnt_suggested"] or 0) + (r["cnt_confirmed"] or 0) > 0:
                sug_ids = r["suggested_ids"] or ""
                conf_ids = r["confirmed_ids"] or ""
                # 默认填净额（admin 可改）。
                pay_btn = (
                    f'<form method="POST" '
                    f'action="/admin/users/{wh}/pay-now" '
                    f'style="display:inline; white-space:nowrap" '
                    f'onsubmit="return confirm(\'确认转账 ¥\' + this.amount.value + '
                    f'\' 给该 tester 并一次性付清 {r["cnt_suggested"]+r["cnt_confirmed"]} 笔（自动 confirm '
                    f'{r["cnt_suggested"]} suggested + {r["cnt_confirmed"]} confirmed）？\');">'
                    f'<input type="hidden" name="expected_suggested_ids" '
                    f'value="{esc(sug_ids)}">'
                    f'<input type="hidden" name="expected_confirmed_ids" '
                    f'value="{esc(conf_ids)}">'
                    f'<input type="number" name="amount" min="0" max="9999" '
                    f'required value="{net_due}" '
                    f'style="width:64px;padding:4px 6px;font-size:13px" '
                    f'title="实际转账人民币金额（默认 = 净额）"> '
                    f'<button type="submit" class="btn btn-small" '
                    f'title="自动 confirm suggested + 全部标已付 + 结算金币">'
                    f'打钱</button></form><br>'
                )

            # 「仅标 confirmed」按钮：仅当 (a) 有 confirmed 反馈 且 (b)「金币会被
            # 安全分摊到 confirmed」时才提供。
            # 修 codex 三次 P1（2026-05-27）：当 suggested>0 且 coins>0 时，
            # 「仅标 confirmed」会把所有金币 settled 但 suggested 部分没补偿 →
            # 后续 suggested 转 paid 时不再扣金币，admin 总转账多付。
            # 此时禁用按钮，强制走「打钱」（一键全付）。
            mark_btn = ""
            if (r["cnt_confirmed"] or 0) > 0:
                if (r["cnt_suggested"] or 0) > 0 and coins > 0:
                    mark_btn = (
                        '<button class="btn btn-small btn-secondary" disabled '
                        f'title="该 tester 同时有 suggested + 已捐金币 → 用「打钱」'
                        f'一次性结算；单独标 confirmed 会让金币提前结清、suggested '
                        f'后续付款漏扣">'
                        '仅标 confirmed</button> '
                    )
                else:
                    ids_csv = r["confirmed_ids"] or ""
                    mark_btn = (
                        f'<form method="POST" '
                        f'action="/admin/users/{wh}/mark-all-paid" '
                        f'style="display:inline" '
                        f'onsubmit="return confirm(\'把这位 tester 的 '
                        f'{r["cnt_confirmed"]} 笔 confirmed 反馈标为已转账'
                        f'（净额 ¥{confirmed_net}）？\');">'
                        f'<input type="hidden" name="expected_ids" '
                        f'value="{esc(ids_csv)}">'
                        f'<button type="submit" class="btn btn-small btn-secondary">'
                        f'仅标 confirmed</button></form> '
                    )
            rows_html.append(
                "<tr>"
                f"<td>{ident}</td>"
                f'<td>{r["total_feedback"]} '
                f'<span class="muted">(待审 {r["cnt_suggested"]} / '
                f'待付 {r["cnt_confirmed"]} / 已付 {r["cnt_paid"]})</span></td>'
                f"<td>{esc(last_at)}</td>"
                f'<td><span class="muted">¥{r["amount_suggested"] or 0}</span></td>'
                f"<td>{confirmed_cell}</td>"
                f"<td><strong>¥{net_due}</strong></td>"
                f'<td><span class="muted">¥{r["amount_paid"] or 0}</span></td>'
                f'<td>{pay_btn}{mark_btn}'
                f'<a href="/admin/feedback?wechat_hash={wh}">查看反馈 →</a></td>'
                "</tr>"
            )

        headers = (
            self._sort_header("身份", None, sort, direction)
            + self._sort_header("反馈数", "count", sort, direction)
            + self._sort_header("最后提交", "last_at", sort, direction)
            + self._sort_header("待审金额", None, sort, direction)
            + self._sort_header("待付（净）", "confirmed", sort, direction)
            + self._sort_header("应付合计", "due", sort, direction)
            + self._sort_header("已转账", None, sort, direction)
            + self._sort_header("动作", None, sort, direction)
        )

        # flash 提示：批量动作后回跳带 ?flash=<kind>:...
        flash_html = ""
        flash = qs.get("flash", [""])[0]
        if flash.startswith("marked:"):
            try:
                n = int(flash.split(":", 1)[1])
                flash_html = (
                    '<div class="card" '
                    'style="border-left:4px solid #2ecc71">'
                    f'<p>✅ 已把 {n} 笔反馈标为已转账。</p></div>'
                )
            except ValueError:
                pass
        elif flash.startswith("paid_lumpsum:"):
            # paid_lumpsum:<amount>:<count>:<donations_settled>
            try:
                _, amt, cnt, dons = flash.split(":", 3)
                amt = int(amt); cnt = int(cnt); dons = int(dons)
                extra = (f"，结算 {dons} 个金币捐赠"
                         if dons > 0 else "")
                flash_html = (
                    '<div class="card" '
                    'style="border-left:4px solid #2ecc71">'
                    f'<p>✅ 一键全付完成：已记录转账 ¥{amt} / '
                    f'付清 {cnt} 笔反馈{extra}。</p></div>'
                )
            except (ValueError, IndexError):
                pass

        self._send_html(render("admin_users.html", {
            "sidebar": _admin_sidebar("users"),
            "flash_block": flash_html,
            "headers": headers,
            "metric_users": str(total_users),
            "metric_due": str(total_due),
            "metric_suggested": str(total_suggested),
            "rows": "\n".join(rows_html) or (
                '<tr><td colspan="8"><div class="empty">'
                '<p>暂无未付清的 tester。</p>'
                '<p class="muted">作者在反馈详情页点「一键确认」后，'
                '该 tester 会出现在这里等周末转账。</p>'
                '</div></td></tr>'
            ),
        }))

    def _handle_admin_donors(self, qs: dict[str, list[str]]) -> None:
        """GET /admin/donors —— 公益站捐赠人面板。"""
        sort = (qs.get("sort", [""])[0] or "usd").lower()
        direction = (qs.get("dir", [""])[0] or "desc").lower()
        if sort not in db.DONOR_SORT:
            sort = "usd"
        if direction not in ("asc", "desc"):
            direction = "desc"

        rows = db.list_donors(sort=sort, direction=direction)

        total_donors = len(rows)
        total_usd_cents = sum(r["donated_usd_cents"] or 0 for r in rows)
        total_draws = sum(r["donation_count"] or 0 for r in rows)
        # 双口径 RMB 合计（spec 2026-05-27）：
        # - 金币口径：sum(coins_consumed) × 1，作者本应付给 tester 但 tester 自愿
        #   献给公益站的 RMB（tester 损失 / 作者节省）
        # - USD 口径：USD 池子 × 固定汇率，公益站实收金额折算 RMB（含倍率随机）
        total_rmb_by_coins = sum(r["coins_consumed"] or 0 for r in rows)
        total_rmb_by_usd = (total_usd_cents / 100) * USD_TO_CNY_RATE

        rows_html = []
        for r in rows:
            wh = r["wechat_hash"]
            ident = self._ident_cell(r["last_wechat_id"], r["last_session_id"])
            last_at = time.strftime(
                "%m-%d %H:%M", time.localtime(r["last_donation_at"] or 0))
            usd = (r["donated_usd_cents"] or 0) / 100
            biggest = (r["biggest_hit_cents"] or 0) / 100
            coins = r["coins_consumed"] or 0
            rmb_by_coins = coins  # 1 金币 = ¥1
            rmb_by_usd = usd * USD_TO_CNY_RATE
            rows_html.append(
                "<tr>"
                f"<td>{ident}</td>"
                f'<td>{r["donation_count"]}</td>'
                f'<td>{coins} <span class="muted">枚</span></td>'
                f'<td>¥{rmb_by_coins}</td>'
                f'<td><strong>${usd:.2f}</strong></td>'
                f'<td>¥{rmb_by_usd:.2f}</td>'
                f'<td>${biggest:.2f}</td>'
                f"<td>{esc(last_at)}</td>"
                f'<td><a href="/admin/feedback?wechat_hash={wh}">查看反馈 →</a></td>'
                "</tr>"
            )

        headers = (
            self._sort_header("身份", None, sort, direction)
            + self._sort_header("抽奖次数", "count", sort, direction)
            + self._sort_header("消耗金币", "coins", sort, direction)
            + self._sort_header("折合 ¥(金币)", None, sort, direction)
            + self._sort_header("捐赠 USD", "usd", sort, direction)
            + self._sort_header("折合 ¥(USD)", None, sort, direction)
            + self._sort_header("最大单次", "biggest", sort, direction)
            + self._sort_header("最近捐赠", "last_at", sort, direction)
            + self._sort_header("动作", None, sort, direction)
        )

        self._send_html(render("admin_donors.html", {
            "sidebar": _admin_sidebar("donors"),
            "headers": headers,
            "metric_donors": str(total_donors),
            "metric_usd": f"{total_usd_cents / 100:.2f}",
            "metric_draws": str(total_draws),
            "metric_rmb_coins": str(total_rmb_by_coins),
            "metric_rmb_usd": f"{total_rmb_by_usd:.2f}",
            "usd_rate": f"{USD_TO_CNY_RATE:.2f}",
            "rows": "\n".join(rows_html) or (
                '<tr><td colspan="9"><div class="empty">'
                '<p>暂无公益站捐赠记录。</p>'
                '<p class="muted">tester 在收据页点「扣 1 复活公益站」抽奖后，'
                '会出现在这里。</p>'
                '</div></td></tr>'
            ),
        }))

    # 防御性常量：单次批量动作最多允许的 id 数。1000 远超任何合理 dogfood 上限，
    # 但能挡住「构造超大 id 列表把事务卡死」的恶意提交。
    MAX_BULK_PAYOUT_IDS = 1000

    def _handle_admin_pay_user_now(self, wh: str) -> None:
        """POST /admin/users/<wh>/pay-now：一键全付（含自动 confirm suggested）。

        表单字段：
          amount                  ：实际转账人民币（整数，0-9999；0 表示"标为已付但未转钱"）
          expected_suggested_ids  ：页面渲染时的 suggested id 逗号串
          expected_confirmed_ids  ：页面渲染时的 confirmed id 逗号串

        事务内 set 严格比对快照；任一不符 → 409，提示 admin 刷新页面。
        """
        if not WECHAT_HASH_RE.match(wh):
            self._error_page(
                "Bad Request", "wechat_hash 格式不合法", status=400)
            return
        form = self._read_form()
        # 解析金额
        amount_raw = form.get("amount", [""])[0]
        try:
            amount = int(amount_raw)
        except ValueError:
            self._error_page(
                "Bad Request", "amount 必须是整数", status=400)
            return
        if amount < 0 or amount > 9999:
            self._error_page(
                "Bad Request",
                "amount 越界（应在 0-9999 之间）",
                status=400)
            return
        # 解析两份快照 ids
        def _parse_ids(raw: str, label: str) -> list[int] | None:
            parts = [p for p in (raw or "").split(",") if p]
            if len(parts) > self.MAX_BULK_PAYOUT_IDS:
                self._error_page(
                    "Bad Request",
                    f"{label} 数量过多（>{self.MAX_BULK_PAYOUT_IDS}）",
                    status=400)
                return None
            try:
                return [int(p) for p in parts]
            except ValueError:
                self._error_page(
                    "Bad Request",
                    f"{label} 必须是整数逗号串", status=400)
                return None

        sug_ids = _parse_ids(
            form.get("expected_suggested_ids", [""])[0],
            "expected_suggested_ids")
        if sug_ids is None:
            return
        conf_ids = _parse_ids(
            form.get("expected_confirmed_ids", [""])[0],
            "expected_confirmed_ids")
        if conf_ids is None:
            return
        try:
            result = db.pay_user_lump_sum(
                wh, amount, sug_ids, conf_ids)
        except db.StaleSnapshotError as e:
            self._error_page(
                "Conflict",
                f"{e} 请回 /admin/users 刷新后再操作。",
                status=409)
            return
        except ValueError as e:
            self._error_page("Bad Request", str(e), status=400)
            return
        except Exception as e:  # noqa: BLE001
            log.exception("pay_user_lump_sum failed")
            self._error_page(
                "Internal Server Error", f"操作失败：{e}", status=500)
            return
        n = result["total_paid"]
        # flash 把 amount 和 笔数都带回，让作者看见"打了 ¥X / N 笔已付清 / 结算 K 个金币"
        self._send_redirect(
            f"/admin/users?flash=paid_lumpsum:{amount}:{n}:"
            f"{result['donations_settled']}")

    def _handle_admin_mark_user_paid(self, wh: str) -> None:
        """POST /admin/users/<wechat_hash>/mark-all-paid：批量 confirmed → paid。

        必带表单字段 expected_ids（页面渲染时的 confirmed id 列表，逗号串）。
        事务内 set 比对，不在快照里的 id 永远不会被动；新增/消失即抛
        StaleSnapshotError → 409 + 提示作者刷新页面。
        """
        if not WECHAT_HASH_RE.match(wh):
            self._error_page(
                "Bad Request", "wechat_hash 格式不合法", status=400)
            return
        form = self._read_form()
        ids_raw = form.get("expected_ids", [""])[0]
        # 空串 → 空列表（兜底「该用户当前应该 0 条 confirmed」的 noop case）。
        parts = [p for p in (ids_raw or "").split(",") if p]
        if len(parts) > self.MAX_BULK_PAYOUT_IDS:
            self._error_page(
                "Bad Request",
                f"expected_ids 数量过多（>{self.MAX_BULK_PAYOUT_IDS}）",
                status=400)
            return
        try:
            expected_ids = [int(p) for p in parts]
        except ValueError:
            self._error_page(
                "Bad Request",
                "expected_ids 必须是整数逗号串，请刷新页面后重试。",
                status=400)
            return
        # 服务端 guard（修 codex 三次 P1）：suggested + 已捐金币的组合下
        # 「仅标 confirmed」会让金币过早结清而 suggested 漏扣，强制走「打钱」。
        # UI 已禁用按钮；这是防伪造 POST 的二次防御。
        guard = db.get_conn().execute(
            "SELECT "
            "  SUM(CASE WHEN payout_status='suggested' THEN 1 ELSE 0 END) AS sug, "
            "  SUM(CASE WHEN payout_status='confirmed' THEN 1 ELSE 0 END) AS conf "
            "FROM feedback WHERE wechat_hash=?", (wh,)).fetchone()
        if (guard["sug"] or 0) > 0:
            unsettled = db.get_conn().execute(
                "SELECT COUNT(*) AS c FROM coin_donations "
                "WHERE wechat_hash=? AND settled_at IS NULL", (wh,)
            ).fetchone()["c"] or 0
            if unsettled > 0:
                self._error_page(
                    "Conflict",
                    f"该 tester 同时有 suggested + 未结清金币（{unsettled} 个）。"
                    "请去 /admin/users 用「打钱」一次性结算，"
                    "单独标 confirmed 会让金币提前结清、suggested 后续漏扣。",
                    status=409)
                return

        try:
            n = db.mark_user_specific_confirmed_to_paid(wh, expected_ids)
        except db.StaleSnapshotError as e:
            self._error_page(
                "Conflict",
                f"{e} 请回 /admin/users 刷新后再操作。",
                status=409)
            return
        except Exception as e:  # noqa: BLE001 - admin-only 兜底
            log.exception("mark_user_specific_confirmed_to_paid failed")
            self._error_page(
                "Internal Server Error", f"操作失败：{e}", status=500)
            return
        self._send_redirect(f"/admin/users?flash=marked:{n}")

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

        flags_list = []
        risk_flags = ""
        if row["ai_risk_flags_json"]:
            try:
                flags_list = json.loads(row["ai_risk_flags_json"])
                risk_flags = ", ".join(flags_list) or "（无）"
            except json.JSONDecodeError:
                flags_list = []
                risk_flags = ""

        # 反作弊：命中横幅 + 开卡到提交耗时展示。
        af_hit = [f for f in flags_list if f in ANTIFRAUD_FLAGS]
        antifraud_banner = ""
        if af_hit:
            label_map = {
                "prompt_injection_attempt": "Prompt injection 试图",
                "duplicate_content": "内容与早先反馈完全重复",
                "semantic_duplicate": "疑似早先反馈的同义改写",
                "submitted_too_fast": "提交耗时过短，疑似未真实体验",
            }
            items = "".join(
                f"<li>{esc(label_map.get(f, f))}</li>" for f in af_hit)
            dup_line = ""
            if row["dup_of_feedback_id"]:
                did = row["dup_of_feedback_id"]
                dup_line = (f'<p>疑似复制自 '
                            f'<a href="/admin/feedback/{did}">#{did}</a></p>')
            antifraud_banner = (
                '<div class="card" style="border-left:4px solid #c0392b">'
                '<p><strong>⚠ 疑似作弊</strong></p>'
                f'<ul>{items}</ul>{dup_line}'
                '<p class="muted">已禁用一键确认，请人工核对后再决定。</p>'
                '</div>'
            )
        tot = row["time_on_task_sec"]
        time_on_task = "—" if tot is None else f"{tot // 60} 分 {tot % 60} 秒"

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
            "time_on_task": esc(time_on_task),
            "antifraud_banner": antifraud_banner,
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
        antifraud_hit = any(f in ANTIFRAUD_FLAGS for f in risk_flags)

        suggested = row["credit_suggested"]

        if status in ("na", "suggested"):
            # 一键确认（命中任一反作弊信号即禁用一键确认按钮，强制人工核对）
            confirm_btn = ""
            if not antifraud_hit and suggested is not None:
                confirm_btn = (
                    f'<form class="inline" method="post" '
                    f'action="/admin/feedback/{fid}/confirm">'
                    f'<button class="btn" type="submit">一键确认 ¥{suggested}</button>'
                    "</form>"
                )
            elif antifraud_hit:
                confirm_btn = (
                    '<p class="warn">⚠ 检测到疑似作弊信号（注入 / 内容重复 / 提交过快），'
                    "禁止一键确认。请人工核对后手动改值确认。</p>"
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
            # 修 codex 五轮 P1：若该 tester 有未结清的金币捐赠，单条详情页按
            # credit_confirmed 毛额付款会绕过净额扣减，引发跨周期账务漂移。
            # 禁用本按钮 + 引导到用户面板（含净额计算 + 同事务结算）。
            unsettled = 0
            wh = row["wechat_hash"]
            if wh:
                r = db.get_conn().execute(
                    "SELECT COUNT(*) AS c FROM coin_donations "
                    "WHERE wechat_hash=? AND settled_at IS NULL",
                    (wh,),
                ).fetchone()
                unsettled = r["c"] or 0
            if unsettled > 0:
                return (
                    '<div class="card" style="border-left:4px solid #f39c12">'
                    '<p><strong>⚠ 该 tester 有未结清的金币捐赠</strong></p>'
                    f'<p>未结清抽奖次数：{unsettled} 次。本页毛额付款会跳过金币扣减。'
                    '请去 <a href="/admin/users">用户面板</a> 用「一键标已转账」'
                    '按净额批量付款，同事务自动结算捐赠。</p>'
                    '<p><button class="btn" disabled '
                    f'title="禁用：该 tester 有 {unsettled} 个未结清捐赠">'
                    f'标记已转账 (¥{row["credit_confirmed"]} 毛额)</button></p>'
                    '</div>'
                )
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
                # 修 codex 五轮 P1：服务端 guard。详情页毛额单条付款 + 未结清
                # 捐赠的组合会让 admin 实际转账多于 net；强制走用户面板批量路径。
                row = db.fetch_feedback(fid)
                if row and row["wechat_hash"]:
                    unsettled = db.get_conn().execute(
                        "SELECT COUNT(*) AS c FROM coin_donations "
                        "WHERE wechat_hash=? AND settled_at IS NULL",
                        (row["wechat_hash"],),
                    ).fetchone()["c"] or 0
                    if unsettled > 0:
                        self._error_page(
                            "Conflict",
                            f"该 tester 有 {unsettled} 个未结清的金币捐赠。"
                            "请去用户面板 /admin/users 按净额批量付款。",
                            status=409)
                        return
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
        """导出 payout_status='confirmed' 的待付清单 CSV。

        修 codex 五轮 P1：加 tester_coins_unsettled 列。该列 > 0 的行表示该
        tester 有未结清的金币捐赠，按 credit_confirmed 毛额转账会绕过净额扣减。
        作者应优先用 /admin/users 用户面板批量付款；若坚持用本 CSV，需手动
        减去 ceil(tester_coins_unsettled / 10) 元。
        """
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["feedback_id", "project_slug", "project_version",
                         "source", "wechat_id",
                         "credit_confirmed", "tester_coins_unsettled",
                         "confirmed_at_human",
                         "time_on_task_sec"])
        rows = db.get_conn().execute(
            """SELECT f.id, f.project_slug, f.project_version, f.wechat_id,
                      f.wechat_hash, f.credit_confirmed, f.submitted_at,
                      f.time_on_task_sec, s.invite_token,
                      COALESCE(
                        (SELECT COUNT(*) FROM coin_donations cd
                          WHERE cd.wechat_hash = f.wechat_hash
                            AND cd.settled_at IS NULL), 0)
                        AS tester_coins_unsettled
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
                r["tester_coins_unsettled"] or 0,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["submitted_at"])),
                r["time_on_task_sec"] if r["time_on_task_sec"] is not None else "",
            ])
        data = "﻿" + buf.getvalue()  # BOM for Excel CN
        self._send_text(data, ctype="text/csv; charset=utf-8")

    def _handle_admin_api_feedback(self, qs: dict) -> None:
        """只读 JSON API：给外部 agent 拉反馈数据用。

        查询参数（全部可选）：
          since   submitted_at >= since（整数 Unix 时间戳）
          until   submitted_at <  until（整数 Unix 时间戳）
          project 按 project_slug 过滤
          status  按 ai_status 过滤（pending/processing/done/failed）
          payout  按 payout_status 过滤（na/suggested/confirmed/paid/rejected）
          limit   默认 100，最大 500
          order   asc | desc（默认 desc by id）

        隐私边界：不返回 wechat_id 原文，仅返回 wechat_hash；
        custom_answers_json / ai_followup_json / ai_risk_flags_json
        预解析成数组。
        """
        def _one(key: str) -> str | None:
            vals = qs.get(key) or []
            return vals[0] if vals else None

        where: list[str] = []
        args: list = []

        for key, col in (("since", "submitted_at >= ?"),
                         ("until", "submitted_at < ?")):
            raw = _one(key)
            if raw:
                try:
                    args.append(int(raw))
                except ValueError:
                    return self._send_json(
                        {"error": f"{key} 必须是整数 Unix 时间戳"}, status=400)
                where.append(col)

        project = _one("project")
        if project:
            where.append("f.project_slug = ?")
            args.append(project)

        status = _one("status")
        if status:
            if status not in ("pending", "processing", "done", "failed"):
                return self._send_json(
                    {"error": "status 取值非法"}, status=400)
            where.append("f.ai_status = ?")
            args.append(status)

        payout = _one("payout")
        if payout:
            if payout not in ("na", "suggested", "confirmed",
                              "paid", "rejected"):
                return self._send_json(
                    {"error": "payout 取值非法"}, status=400)
            where.append("f.payout_status = ?")
            args.append(payout)

        try:
            limit = int(_one("limit") or 100)
        except ValueError:
            return self._send_json(
                {"error": "limit 必须是整数"}, status=400)
        limit = max(1, min(limit, 500))

        order = (_one("order") or "desc").lower()
        if order not in ("asc", "desc"):
            return self._send_json(
                {"error": "order 必须是 asc 或 desc"}, status=400)

        sql = ("SELECT f.*, s.invite_token FROM feedback f "
               "LEFT JOIN sessions s ON f.session_id = s.session_id "
               "                    AND f.project_slug = s.project_slug")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY f.id {order.upper()} LIMIT ?"
        args.append(limit)

        rows = db.get_conn().execute(sql, args).fetchall()

        def _arr(text) -> list:
            if not text:
                return []
            try:
                v = json.loads(text)
                return v if isinstance(v, list) else []
            except (json.JSONDecodeError, TypeError):
                return []

        items = []
        for r in rows:
            items.append({
                "id": r["id"],
                "project_slug": r["project_slug"],
                "project_version": r["project_version"],
                "source": _feedback_source(r["invite_token"]),
                "wechat_hash": r["wechat_hash"],
                "wechat_id_purged_at": r["wechat_id_purged_at"],
                "submitted_at": r["submitted_at"],
                "time_on_task_sec": r["time_on_task_sec"],
                "answers": {
                    "q1": r["q1_answer"],
                    "q2": r["q2_answer"],
                    "q3": r["q3_answer"],
                    "q4": r["q4_answer"],
                    "q5": r["q5_answer"],
                    "custom": _arr(r["custom_answers_json"]),
                },
                "ai": {
                    "status": r["ai_status"],
                    "attempts": r["ai_attempts"],
                    "model_used": r["ai_model_used"],
                    "depth_score": r["ai_depth_score"],
                    "depth_rationale": r["ai_depth_rationale"],
                    "stuck_step": r["ai_stuck_step"],
                    "stuck_confidence": r["ai_stuck_confidence"],
                    "followup_questions": _arr(r["ai_followup_json"]),
                    "risk_flags": _arr(r["ai_risk_flags_json"]),
                },
                "payout": {
                    "status": r["payout_status"],
                    "credit_suggested": r["credit_suggested"],
                    "credit_confirmed": r["credit_confirmed"],
                    "notes": r["payout_notes"],
                    "paid_at": r["payout_paid_at"],
                },
                "antifraud": {
                    "content_digest": r["content_digest"],
                    "dup_of_feedback_id": r["dup_of_feedback_id"],
                },
            })

        self._send_json({"count": len(items), "items": items})

    # ---- admin write API：项目 CRUD + 发布新版本 ----

    def _handle_admin_api_project_create(self) -> None:
        """POST /admin/api/projects — 创建一个新项目（agent 上线产品用）。

        请求 JSON：
          slug, name, description, trial_url, max_feedback_count (1-100),
          custom_questions (可选，最多 2 条字符串),
          listed (可选，bool；默认 false=定向；true=公开到任务大厅),
          version (可选，默认 "v1")
        """
        body, err = self._read_json_body()
        if err:
            return self._send_json({"error": err}, status=400)

        slug = str(body.get("slug") or "").strip()
        name = str(body.get("name") or "").strip()
        description = str(body.get("description") or "").strip()
        trial_url = str(body.get("trial_url") or "").strip()
        max_raw = body.get("max_feedback_count")
        customs_raw = body.get("custom_questions") or []
        listed = bool(body.get("listed", False))
        version = str(body.get("version") or "v1").strip() or "v1"

        if not isinstance(customs_raw, list):
            return self._send_json(
                {"error": "custom_questions 必须是字符串数组"}, status=400)
        customs = [str(c).strip() for c in customs_raw if str(c).strip()]

        err = _validate_project_form(slug, name, description, trial_url,
                                     str(max_raw), customs, [])
        if err:
            return self._send_json({"error": err}, status=400)
        if db.fetch_project(slug) is not None:
            return self._send_json(
                {"error": f"slug {slug!r} 已存在，更新请用 "
                          f"POST /admin/api/projects/{slug}"},
                status=409)

        custom_json = (json.dumps(customs, ensure_ascii=False)
                       if customs else None)
        db.upsert_project(slug, name, description, trial_url,
                          int(max_raw), custom_json)
        db.set_project_listed(slug, 1 if listed else 0)
        if version != "v1":
            with db.transaction() as tx:
                tx.execute("UPDATE projects SET version=? WHERE slug=?",
                           (version, slug))
        if listed:
            db.seed_invite_token(db.public_token_for(slug), slug, 0)

        origin = self._base_origin()
        self._send_json({
            "ok": True,
            "slug": slug,
            "version": version,
            "listed": listed,
            "card_url": f"{origin}/p/{slug}",
            "admin_url": f"{origin}/admin/projects/{slug}/edit",
            "hall_url": f"{origin}/hall" if listed else None,
        }, status=201)

    def _handle_admin_api_project_update(self, slug: str) -> None:
        """POST /admin/api/projects/<slug> — 部分字段更新项目。

        可选字段：name / description / trial_url / max_feedback_count /
        custom_questions / listed。未提供的字段保持原值。

        注意：版本号变更请走 /release 端点（会归零 reserved_count）；
        slug 不可改（slug 是项目身份）。
        """
        if "/" in slug or not slug:
            return self._send_json({"error": "slug 无效"}, status=400)
        project = db.fetch_project(slug)
        if project is None:
            return self._send_json(
                {"error": f"项目不存在：{slug}"}, status=404)

        body, err = self._read_json_body()
        if err:
            return self._send_json({"error": err}, status=400)

        # 取新值，未提供则用旧值
        name = str(body.get("name", project["name"]) or "").strip()
        description = str(body.get(
            "description", project["description"]) or "").strip()
        trial_url = str(body.get(
            "trial_url", project["trial_url"]) or "").strip()
        max_raw = body.get("max_feedback_count", project["max_feedback_count"])

        if "custom_questions" in body:
            cq = body["custom_questions"]
            if not isinstance(cq, list):
                return self._send_json(
                    {"error": "custom_questions 必须是数组"}, status=400)
            customs = [str(c).strip() for c in cq if str(c).strip()]
        else:
            try:
                customs = json.loads(project["custom_questions_json"] or "[]")
            except json.JSONDecodeError:
                customs = []

        err = _validate_project_form(slug, name, description, trial_url,
                                     str(max_raw), customs, [])
        if err:
            return self._send_json({"error": err}, status=400)

        custom_json = (json.dumps(customs, ensure_ascii=False)
                       if customs else None)
        db.upsert_project(slug, name, description, trial_url,
                          int(max_raw), custom_json)

        if "listed" in body:
            new_listed = bool(body["listed"])
            db.set_project_listed(slug, 1 if new_listed else 0)
            if new_listed:
                db.seed_invite_token(db.public_token_for(slug), slug, 0)

        self._send_json({
            "ok": True,
            "slug": slug,
            "updated_fields": [k for k in body.keys()
                               if k in ("name", "description", "trial_url",
                                        "max_feedback_count",
                                        "custom_questions", "listed")],
        })

    def _handle_admin_api_project_release(self, slug: str) -> None:
        """POST /admin/api/projects/<slug>/release — 发布新版本。

        请求 JSON：
          version (必填，新版本号，不能与当前相同)
          trial_url / description / max_feedback_count (可选，同时更新)

        副作用：reserved_count 归零（新版本重新开放名额）；
        旧反馈的 project_version 不变（按版本沉淀）。
        """
        if "/" in slug or not slug:
            return self._send_json({"error": "slug 无效"}, status=400)
        project = db.fetch_project(slug)
        if project is None:
            return self._send_json(
                {"error": f"项目不存在：{slug}"}, status=404)

        body, err = self._read_json_body()
        if err:
            return self._send_json({"error": err}, status=400)

        new_version = str(body.get("version") or "").strip()
        if not new_version:
            return self._send_json({"error": "version 不能为空"}, status=400)
        if new_version == project["version"]:
            return self._send_json(
                {"error": f"新版本号不能与当前版本 "
                          f"{project['version']!r} 相同"},
                status=400)

        sets = ["version = ?", "reserved_count = 0"]
        args: list = [new_version]
        if "trial_url" in body:
            url = str(body["trial_url"] or "").strip()
            if not url.lower().startswith(("http://", "https://")):
                return self._send_json(
                    {"error": "trial_url 必须以 http:// 或 https:// 开头"},
                    status=400)
            sets.append("trial_url = ?")
            args.append(url)
        if "description" in body:
            sets.append("description = ?")
            args.append(str(body["description"]))
        if "max_feedback_count" in body:
            try:
                mc = int(body["max_feedback_count"])
            except (TypeError, ValueError):
                return self._send_json(
                    {"error": "max_feedback_count 必须是整数"}, status=400)
            if mc < 1 or mc > 100:
                return self._send_json(
                    {"error": "max_feedback_count 必须在 1-100"}, status=400)
            sets.append("max_feedback_count = ?")
            args.append(mc)
        args.append(slug)
        with db.transaction() as tx:
            tx.execute(
                f"UPDATE projects SET {', '.join(sets)} WHERE slug = ?",
                args)

        self._send_json({
            "ok": True,
            "slug": slug,
            "old_version": project["version"],
            "new_version": new_version,
            "reserved_count_reset": True,
        })

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
            "agent_block": _render_agent_api_block(self._base_origin()),
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
