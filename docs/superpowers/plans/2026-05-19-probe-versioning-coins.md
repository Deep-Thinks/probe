# Probe 版本控制 + 金币余额 + 上线两个新项目 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上线 cyber-council / oriself 两个公开众测项目；为项目引入轻量版本控制（反馈与版本绑定）；为 tester 引入无登录的金币余额查询。

**Architecture:** 沿用现有 stdlib-only 单进程架构。版本控制 = `projects.version` + `feedback.project_version` 快照 + 唯一索引升级为三列；金币余额 = `feedback.wechat_hash` 耐久身份 + 复用现有 payout 状态机聚合，不建新表。

**Tech Stack:** Python 3.12 stdlib（`http.server` + `sqlite3` + `hashlib`），vanilla HTML `{{var}}` 模板。

**测试约定（重要）：** 本项目 v1 dogfood **刻意无自动化测试框架**（CLAUDE.md §6/§7：stdlib-only、`requirements.txt` 为空、`tests/` 留待 v2）。因此本计划的"验证"步骤一律用 stdlib `python3 -c` 脚本、`sqlite3` 查询、`curl` 完成，**不引入 pytest、不新建 `tests/` 目录**。

**提交约定（重要）：** 作者要求"做好之后给 Codex 审计，没问题再 commit/push/上线"。因此 Task 1-10 **不做任何 git 提交**；Task 11 在 Codex 审计通过后一次性 commit + push + 部署。

**设计文档：** `docs/superpowers/specs/2026-05-19-probe-versioning-coins-design.md`

---

## Task 1: db.py — schema 列 + 迁移 + 哈希 + 余额查询 + seed_project 扩展

**Files:**
- Modify: `db.py`

- [ ] **Step 1: 加 `hashlib` 导入与金币盐常量**

`db.py` 顶部 `import os` 一行下方新增 `import hashlib`，并在 `_local = threading.local()` 一行**之前**插入：

```python
# 金币哈希盐：把微信号转成可跨 30 天隐私清理存活的耐久身份（见 wechat_hash）。
# 一旦设定不可更改，否则历史金币哈希全部对不上。
_COIN_SECRET = os.environ.get("PROBE_COIN_SECRET", "probe-dev-coin-secret")
```

- [ ] **Step 2: SCHEMA_SQL 加列、移除旧唯一索引**

`projects` 表 `CREATE TABLE` 内，把 `listed INTEGER NOT NULL DEFAULT 0` 一行后补一行（注意上一行末尾逗号）：

```sql
  listed INTEGER NOT NULL DEFAULT 0,
  -- version：项目当前版本号；反馈提交时快照到 feedback.project_version。
  version TEXT NOT NULL DEFAULT 'v1'
```

`feedback` 表 `CREATE TABLE` 内，把 `wechat_id_purged_at INTEGER,` 一行后补两行：

```sql
  wechat_id_purged_at INTEGER,
  -- project_version：提交时项目版本快照；不同版本的反馈有效性不同。
  project_version TEXT,
  -- wechat_hash：微信号单向哈希，可跨 wechat_id 隐私清理存活，用于金币聚合。
  wechat_hash TEXT,
```

把 `SCHEMA_SQL` 末尾这段**整体删除**（唯一索引改由 `init_schema` 迁移代码管理，因其依赖新列）：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_per_project
  ON feedback(project_slug, wechat_id)
  WHERE wechat_id IS NOT NULL;
```

保留 `idx_feedback_project` / `idx_feedback_ai_status` / `idx_feedback_payout` 三个普通索引不动。

- [ ] **Step 3: init_schema 末尾追加迁移块**

`init_schema()` 函数内，在现有 `recruit_batches` 迁移块（`if "credit_min" not in bcols:` 那段）**之后**、函数结束前追加：

```python
    # 迁移：项目引入版本字段（旧库 CREATE IF NOT EXISTS 不会补列）。
    if "version" not in pcols:
        conn.execute("ALTER TABLE projects ADD COLUMN version TEXT NOT NULL DEFAULT 'v1'")
    # 迁移：feedback 引入版本快照 + 微信号耐久哈希。
    fcols2 = {r["name"] for r in conn.execute("PRAGMA table_info(feedback)")}
    if "project_version" not in fcols2:
        conn.execute("ALTER TABLE feedback ADD COLUMN project_version TEXT")
    if "wechat_hash" not in fcols2:
        conn.execute("ALTER TABLE feedback ADD COLUMN wechat_hash TEXT")
    # 回填 project_version：老反馈按其所属项目的当前版本号填。
    conn.execute(
        """UPDATE feedback SET project_version =
             (SELECT version FROM projects WHERE projects.slug = feedback.project_slug)
           WHERE project_version IS NULL"""
    )
    # 回填 wechat_hash：老反馈用现存 wechat_id 算哈希；已清理（NULL）的跳过。
    for r in conn.execute(
            "SELECT id, wechat_id FROM feedback "
            "WHERE wechat_hash IS NULL AND wechat_id IS NOT NULL").fetchall():
        conn.execute("UPDATE feedback SET wechat_hash = ? WHERE id = ?",
                     (wechat_hash(r["wechat_id"]), r["id"]))
    # 唯一索引升级：旧 (project_slug, wechat_id) → 新含版本三列。
    # 语义：同微信号在同项目同版本只能提交一次；但 v1 测过的人可合法再测 v2。
    conn.execute("DROP INDEX IF EXISTS uniq_wechat_per_project")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_per_project_version
             ON feedback(project_slug, project_version, wechat_id)
             WHERE wechat_id IS NOT NULL"""
    )
```

> `pcols` 已在现有 `listed` 迁移块里定义（`pcols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)")}`），此处直接复用。

- [ ] **Step 4: 新增 wechat_hash 与 coin_balance 函数**

在 `db.py` 的 `init_schema()` 函数**之后**、`upsert_project` **之前**插入：

```python
# ---- 金币：微信号耐久哈希 + 余额聚合 ----


def wechat_hash(wechat_id: str) -> str:
    """微信号的单向哈希，作为可跨 30 天隐私清理存活的耐久身份。

    purge_wechat.py 会把 raw wechat_id 置 NULL，但 wechat_hash 永久保留，
    金币余额据此按人聚合。PROBE_COIN_SECRET 一旦设定不可更改。
    """
    norm = (wechat_id or "").strip().lower()
    return hashlib.sha256(
        (_COIN_SECRET + ":" + norm).encode("utf-8")
    ).hexdigest()


def coin_balance(wh: str) -> dict:
    """按 wechat_hash 聚合某 tester 跨所有项目/版本的金币情况。

    复用 payout 状态机：confirmed=可提现，paid=已提现，na/suggested=评估中。
    """
    row = get_conn().execute(
        """SELECT
             COALESCE(SUM(CASE WHEN payout_status='confirmed'
                               THEN credit_confirmed END), 0) AS withdrawable,
             COALESCE(SUM(CASE WHEN payout_status='paid'
                               THEN credit_confirmed END), 0) AS paid,
             COALESCE(SUM(CASE WHEN payout_status IN ('na','suggested')
                               THEN 1 ELSE 0 END), 0) AS pending_count
           FROM feedback WHERE wechat_hash = ?""",
        (wh,),
    ).fetchone()
    return {
        "withdrawable": row["withdrawable"],
        "paid": row["paid"],
        "pending_count": row["pending_count"],
    }
```

- [ ] **Step 5: 扩展 seed_project 支持 version / listed**

把 `db.py` 的 `seed_project` 函数整体替换为：

```python
def seed_project(
    slug: str,
    name: str,
    description: str,
    trial_url: str,
    max_feedback_count: int,
    custom_questions_json: str | None,
    version: str = "v1",
    listed: int = 0,
) -> bool:
    """仅当 slug 不存在时插入。返回是否真正插入。

    ARCH-3：projects/*.json 降为可选种子；DB 是真相源，admin 编辑不被启动期覆盖。
    version / listed 仅在项目首次 seed 时生效，已存在项目尊重 DB 现值。
    """
    now = int(time.time())
    with transaction() as tx:
        cur = tx.execute(
            """INSERT INTO projects(slug, name, description, trial_url,
                 max_feedback_count, custom_questions_json, created_at,
                 version, listed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(slug) DO NOTHING""",
            (slug, name, description, trial_url, max_feedback_count,
             custom_questions_json, now, version, 1 if listed else 0),
        )
        return cur.rowcount > 0
```

- [ ] **Step 6: 验证 schema + 迁移 + 函数（全新库）**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 python3 -c "
import db
db.init_schema()
c = db.get_conn()
pcols = {r['name'] for r in c.execute('PRAGMA table_info(projects)')}
fcols = {r['name'] for r in c.execute('PRAGMA table_info(feedback)')}
idx = {r['name'] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='index'\")}
assert 'version' in pcols, pcols
assert 'project_version' in fcols and 'wechat_hash' in fcols, fcols
assert 'uniq_wechat_per_project_version' in idx, idx
assert 'uniq_wechat_per_project' not in idx, idx
h = db.wechat_hash('Foo_123'); assert h == db.wechat_hash(' foo_123 '), 'norm fail'
assert len(h) == 64, h
print('balance empty:', db.coin_balance(h))
print('TASK1 FRESH OK')
"
```
Expected: 末行 `TASK1 FRESH OK`，无 AssertionError。

- [ ] **Step 7: 验证迁移旧库（拉生产库副本）**

Run:
```bash
cd /niuniu869_dev/probe && \
sshpass -p 'a1b2c3d4<>++' scp -o StrictHostKeyChecking=accept-new \
  root@101.33.32.162:/opt/probe/data/db.sqlite3 /tmp/pv-prod.sqlite3 && \
PROBE_DB_PATH=/tmp/pv-prod.sqlite3 python3 -c "
import db
db.init_schema()
c = db.get_conn()
row = c.execute('SELECT id, wechat_id, project_version, wechat_hash FROM feedback WHERE id=1').fetchone()
assert row['project_version'] == 'v1', row['project_version']
assert row['wechat_hash'] == db.wechat_hash(row['wechat_id']), 'hash backfill mismatch'
idx = {r['name'] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='index'\")}
assert 'uniq_wechat_per_project_version' in idx and 'uniq_wechat_per_project' not in idx, idx
print('balance for sample tester:', db.coin_balance(row['wechat_hash']))
print('TASK1 MIGRATE OK')
"
```
Expected: 末行 `TASK1 MIGRATE OK`；`pending_count` 为 1（样本反馈处于 `suggested`）。

---

## Task 2: project_loader.py 校验扩展 + 两个新项目 JSON

**Files:**
- Modify: `project_loader.py`
- Create: `projects/cyber-council.json`
- Create: `projects/oriself.json`

- [ ] **Step 1: `_validate` 增加 version / listed 校验**

`project_loader.py` 的 `_validate` 函数内，`single_use_tokens` 校验块（`sut = cfg.get(...)` 那段）**之后**、函数结束前追加：

```python
    # version 可选：若提供必须非空字符串（缺失由 seed_project 默认 'v1'）。
    if "version" in cfg:
        ver = cfg["version"]
        if not isinstance(ver, str) or not ver.strip():
            raise ProjectConfigError(
                f"{source}: version must be non-empty string, got {ver!r}"
            )

    # listed 可选：必须严格 bool（缺失默认 false）。
    listed = cfg.get("listed", False)
    if not isinstance(listed, bool):
        raise ProjectConfigError(
            f"{source}: listed must be true/false, got {listed!r}"
        )
```

- [ ] **Step 2: `load_all` Phase 2 透传 version / listed**

`load_all` 函数 Phase 2 循环里，把 `db.seed_project(...)` 调用整体替换为：

```python
        inserted = db.seed_project(
            slug=cfg["slug"],
            name=cfg["name"],
            description=cfg["description"],
            trial_url=cfg["trial_url"].strip(),
            max_feedback_count=cfg["max_feedback_count"],
            custom_questions_json=custom_json,
            version=(cfg.get("version") or "v1"),
            listed=1 if cfg.get("listed", False) else 0,
        )
```

- [ ] **Step 3: 创建 `projects/cyber-council.json`**

```json
{
  "slug": "cyber-council",
  "name": "哲人议会 The Council",
  "description": "输入一个开放问题，7 位思想史上的哲人辩两三轮；AI 议长记录分歧、合并阵营，最后给你一份可带走的「判词」。试用约 5-10 分钟，帮我们看看哪一步让你卡住。",
  "trial_url": "https://www.cyber-council.com/",
  "max_feedback_count": 30,
  "version": "v1",
  "listed": true,
  "custom_questions": [
    "从你提问到看到判词的整个过程，哪一步最让你犹豫、甚至想退出？"
  ],
  "invite_tokens": ["seed-cyber-council-001"],
  "single_use_tokens": false
}
```

- [ ] **Step 4: 创建 `projects/oriself.json`**

```json
{
  "slug": "oriself",
  "name": "OriSelf",
  "description": "一个对话驱动的自我发现工具——从你说出口的话里，长出你本来的样子。试用约 5-10 分钟，帮我们看看哪一步让你不顺。",
  "trial_url": "https://next.oriself.com/",
  "max_feedback_count": 30,
  "version": "v1",
  "listed": true,
  "custom_questions": [
    "用完之后，你觉得它真的让你更看清自己了一点吗？是哪一步让你产生或没产生这种感觉？"
  ],
  "invite_tokens": ["seed-oriself-001"],
  "single_use_tokens": false
}
```

- [ ] **Step 5: 验证加载 + 公开标记**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 python3 -c "
import db, project_loader
from pathlib import Path
db.init_schema()
n = project_loader.load_all(Path('projects'))
print('loaded', n, 'projects')
for slug in ('cyber-council', 'oriself'):
    p = db.fetch_project(slug)
    assert p is not None, slug
    assert p['listed'] == 1, (slug, 'listed')
    assert p['version'] == 'v1', (slug, 'version')
    assert p['max_feedback_count'] == 30, slug
db.ensure_public_tokens()
assert db.fetch_token('public-cyber-council') is not None
assert db.fetch_token('public-oriself') is not None
print('TASK2 OK')
"
```
Expected: 末行 `TASK2 OK`，`loaded 3 projects`。

---

## Task 3: server.py — submit_feedback 快照版本 + 写哈希

**Files:**
- Modify: `server.py`（`submit_feedback`）

- [ ] **Step 1: submit_feedback 取项目版本 + 算哈希**

`submit_feedback` 函数内，`session = db.fetch_session(session_id)` 一行**之前**插入：

```python
    project = db.fetch_project(project_slug)
    if project is None:
        raise ValueError("项目不存在")
    project_version = project["version"]
    wh = db.wechat_hash(wechat_id)
```

- [ ] **Step 2: INSERT 写入 project_version + wechat_hash**

把 `submit_feedback` 内第 3 步的 `INSERT INTO feedback(...)` 语句整体替换为：

```python
            cur = tx.execute(
                """INSERT INTO feedback(
                     session_id, project_slug, wechat_id, wechat_hash,
                     q1_answer, q2_answer, q3_answer, q4_answer, q5_answer,
                     custom_answers_json, submitted_at, project_version
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, project_slug, wechat_id, wh,
                 q1, q2, q3, q4, q5, custom_json, now, project_version),
            )
```

- [ ] **Step 3: 验证提交写入新字段**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 python3 -c "
import db, project_loader, server
from pathlib import Path
db.init_schema(); project_loader.load_all(Path('projects')); db.ensure_public_tokens()
sid = server.create_session('cyber-council', 'public-cyber-council')
fid = server.submit_feedback('cyber-council', sid, 'tester_wx_001',
    'q1 第一眼','q2 路径','q3 卡点','q4 放弃','q5 改动', ['自定义答'])
row = db.fetch_feedback(fid)
assert row['project_version'] == 'v1', row['project_version']
assert row['wechat_hash'] == db.wechat_hash('tester_wx_001'), 'hash'
print('TASK3 OK fid=', fid)
"
```
Expected: 末行 `TASK3 OK fid= 1`。

---

## Task 4: `/coins` 余额查询页（无登录）

**Files:**
- Create: `templates/coins.html`
- Modify: `server.py`（路由 + 两个 handler）

- [ ] **Step 1: 创建 `templates/coins.html`**

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>我的金币 · Probe</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="wrap">
  <a class="brand" href="/"><span class="brand-mark">P</span>Probe</a>

  <h1>查询我的金币余额</h1>
  <p class="meta">输入你提交反馈时填写的微信号即可查询累计金币。金币与人民币 1:1，无需登录。</p>

  {{error_block}}
  <div class="card">
    <form method="post" action="/coins">
      <label for="wechat_id">微信号</label>
      <input type="text" id="wechat_id" name="wechat_id" value="{{wechat_prefill}}" required>
      <p class="mt"><button class="btn" type="submit">查询余额</button></p>
    </form>
  </div>

  {{result_block}}

  <div class="card">
    <h3>金币怎么用？</h3>
    <p>当可提现金币 ≥ <strong>100</strong> 时，你可以找作者用 100 金币兑换<strong>一次咨询</strong>，或者攒着每个周末通过微信提现。</p>
    <p class="muted">金币只在作者确认你的反馈金额后才计入「可提现」。刚提交、AI 还在评估的反馈会显示为「评估中」。</p>
  </div>

  <div class="privacy">
    本平台仅在你提交反馈后收集反馈内容和微信号（仅用于转账）。完成转账 30 天后自动删除原始反馈中的微信号。
  </div>

  <footer>Probe · <a href="/">首页</a> · probe.niuniu869.com</footer>
</div>
</body>
</html>
```

- [ ] **Step 2: 注册 GET / POST `/coins` 路由**

`server.py` 的 `do_GET` 内，`if path == "/hall":` 区块**之后**插入：

```python
        if path == "/coins":
            self._handle_coins()
            return
```

`do_POST` 内，`if path.startswith("/p/") and path.endswith("/feedback"):` 区块**之后**插入：

```python
        if path == "/coins":
            self._handle_coins_lookup()
            return
```

> `/coins` 是公开只读查询、不改任何状态，**无需** `_require_same_origin`（该校验只用于 admin POST）。用 POST 提交是为了让微信号不进 URL/访问日志。

- [ ] **Step 3: 实现两个 handler**

`server.py` 的 `_handle_receipt` 方法**之后**插入：

```python
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
```

- [ ] **Step 4: 验证 `/coins` 端到端**

Run（后台起 mock 服务）:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 PROBE_BIND=127.0.0.1 \
  PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass PORT=8099 \
  python3 server.py > /tmp/pv-server.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8099/coins | grep -q '查询我的金币余额' && echo 'GET /coins OK'
curl -s -X POST http://127.0.0.1:8099/coins -d 'wechat_id=nobody_xyz' | grep -q '金币可提现' && echo 'POST /coins OK'
curl -s -X POST http://127.0.0.1:8099/coins -d 'wechat_id=' | grep -q '请输入微信号' && echo 'POST empty OK'
kill %1 2>/dev/null
```
Expected: `GET /coins OK`、`POST /coins OK`、`POST empty OK` 三行都出现。

---

## Task 5: receipt.html — 金币余额展示

**Files:**
- Modify: `templates/receipt.html`
- Modify: `server.py`（`_handle_receipt`）

- [ ] **Step 1: receipt.html 加金币卡片**

`templates/receipt.html` 中，`<p>如有疑问，可联系作者 <strong>niuniu869</strong>。</p>` 一行**之前**插入：

```html
  <div class="card">
    <p><strong>你在 Probe 的金币</strong></p>
    {{coin_block}}
    <p class="muted">可提现金币 ≥ 100 时，可找作者用 100 金币兑换一次咨询，或周末微信提现。</p>
    <p><a href="/coins">查询我的金币余额 →</a></p>
  </div>
```

- [ ] **Step 2: `_handle_receipt` 计算 coin_block**

把 `server.py` 的 `_handle_receipt` 方法整体替换为：

```python
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
```

- [ ] **Step 3: 验证收据页含金币块**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 PROBE_BIND=127.0.0.1 \
  PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass PORT=8099 \
  python3 server.py > /tmp/pv-server.log 2>&1 &
sleep 2
SID=$(curl -s -c /tmp/pv.cookie "http://127.0.0.1:8099/p/cyber-council?t=public-cyber-council" \
  | grep -o 'name="session_id" value="[a-f0-9]*"' | grep -o '[a-f0-9]\{32\}')
FID_REDIR=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST \
  "http://127.0.0.1:8099/p/cyber-council/feedback" \
  --data-urlencode "session_id=$SID" --data-urlencode 'wechat_id=rcpt_test_1' \
  --data-urlencode 'q1=a' --data-urlencode 'q2=b' --data-urlencode 'q3=c' \
  --data-urlencode 'q4=d' --data-urlencode 'q5=e' --data-urlencode 'custom_0=f')
curl -s "http://127.0.0.1:8099${FID_REDIR}" | grep -q '你在 Probe 的金币' && echo 'RECEIPT COIN OK'
kill %1 2>/dev/null
```
Expected: `RECEIPT COIN OK`。

---

## Task 6: admin 侧版本展示（列表 / 详情 / 项目页 / 看板 / CSV）

**Files:**
- Modify: `templates/admin_list.html`, `templates/admin_detail.html`, `templates/admin_projects.html`
- Modify: `server.py`（`_handle_admin_list`, `_handle_admin_detail`, `_handle_admin_projects`, `_handle_admin_dashboard`, `_handle_admin_export`）

- [ ] **Step 1: admin_list.html 加「版本」表头**

`templates/admin_list.html` 的 `<th>项目</th>` 一行**之后**插入 `<th>版本</th>`。

- [ ] **Step 2: `_handle_admin_list` 行加版本单元格 + 修 colspan**

`_handle_admin_list` 内，把 `<td>{esc(row['project_slug'])}</td>` 一行替换为：

```python
                f"<td>{esc(row['project_slug'])}</td>"
                f"<td>{esc(row['project_version'] or '—')}</td>"
```

并把空状态那行的 `colspan="9"` 改成 `colspan="10"`。

- [ ] **Step 3: admin_detail.html meta 行加版本**

`templates/admin_detail.html` 的 meta 行替换为：

```html
  <p class="meta">项目 <strong>{{project_slug}}</strong> · 版本 <strong>{{project_version}}</strong> · 提交于 {{submitted_at}} · 来源 <strong>{{source}}</strong></p>
```

- [ ] **Step 4: `_handle_admin_detail` 传 project_version**

`_handle_admin_detail` 的 `render("admin_detail.html", {...})` 字典里，`"project_slug": esc(row["project_slug"]),` 一行**之后**插入：

```python
            "project_version": esc(row["project_version"] or "—"),
```

- [ ] **Step 5: admin_projects.html 加「当前版本」列**

`templates/admin_projects.html` 的表头行替换为：

```html
      <tr><th>项目</th><th>当前版本</th><th>slug</th><th>名额</th><th>已收</th><th>自定义题</th><th>公开</th><th>动作</th></tr>
```

- [ ] **Step 6: `_handle_admin_projects` 行加版本 + 修 colspan**

`_handle_admin_projects` 内，把 `f"<td>{esc(p['name'])}</td>"` 一行替换为：

```python
                f"<td>{esc(p['name'])}</td>"
                f"<td>{esc(p['version'])}</td>"
```

并把空状态那行的 `colspan="7"` 改成 `colspan="8"`。

- [ ] **Step 7: 看板漏斗 head 加版本标签**

`_handle_admin_dashboard` 的 funnels 循环里，把 `f'<div class="funnel-head"><strong>{esc(p["name"])}</strong>'` 一行替换为：

```python
                f'<div class="funnel-head"><strong>{esc(p["name"])}</strong> '
                f'<span class="muted">{esc(p["version"])}</span>'
```

- [ ] **Step 8: export.csv 加 project_version 列**

`_handle_admin_export` 内：
- `writer.writerow([...])` 表头加 `"project_version"`，放在 `"project_slug"` 之后。
- SQL `SELECT` 加 `f.project_version`（放 `f.project_slug,` 之后）。
- 数据行 `writer.writerow([...])` 在 `r["project_slug"],` 之后加 `_safe_csv_cell(r["project_version"] or ""),`。

替换后的 `_handle_admin_export` 关键三处：

```python
        writer.writerow(["feedback_id", "project_slug", "project_version",
                         "source", "wechat_id",
                         "credit_confirmed", "confirmed_at_human"])
```
```python
        rows = db.get_conn().execute(
            """SELECT f.id, f.project_slug, f.project_version, f.wechat_id,
                      f.credit_confirmed, f.submitted_at, s.invite_token
               FROM feedback f
               JOIN sessions s ON f.session_id = s.session_id
                              AND f.project_slug = s.project_slug
               WHERE f.payout_status = 'confirmed'
               ORDER BY f.id ASC"""
        ).fetchall()
```
```python
        for r in rows:
            writer.writerow([
                r["id"], r["project_slug"],
                _safe_csv_cell(r["project_version"] or ""),
                _safe_csv_cell(_feedback_source(r["invite_token"])),
                _safe_csv_cell(r["wechat_id"] or ""),
                r["credit_confirmed"] or "",
                time.strftime("%Y-%m-%d %H:%M", time.localtime(r["submitted_at"])),
            ])
```

- [ ] **Step 9: 验证 admin 页面渲染**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 PROBE_BIND=127.0.0.1 \
  PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass PORT=8099 \
  python3 server.py > /tmp/pv-server.log 2>&1 &
sleep 2
A='-u admin:devpass'
curl -s $A http://127.0.0.1:8099/admin/projects | grep -q '当前版本' && echo 'PROJECTS COL OK'
curl -s $A http://127.0.0.1:8099/admin/feedback | grep -q '<th>版本</th>' && echo 'LIST COL OK'
curl -s $A http://127.0.0.1:8099/admin/export.csv | head -1 | grep -q 'project_version' && echo 'CSV COL OK'
kill %1 2>/dev/null
```
Expected: `PROJECTS COL OK`、`LIST COL OK`、`CSV COL OK` 三行。

---

## Task 7: 「发布新版本」动作

**Files:**
- Modify: `templates/admin_project_form.html`
- Modify: `server.py`（`_handle_admin_project_form`, `do_POST` 路由, 新增 `_handle_admin_project_release`）

- [ ] **Step 1: admin_project_form.html 加 release 占位**

`templates/admin_project_form.html` 中，主 `<div class="card">...</div>`（含表单的那个）**之后**、`</main>` **之前**插入一行：

```html
  {{release_block}}
```

- [ ] **Step 2: `_handle_admin_project_form` 编辑态读 version + 生成 release_block**

`_handle_admin_project_form` 内，编辑态 `prefill = {...}` 字典里 `"listed": bool(p["listed"]),` 一行**之后**加一行：

```python
                "version": p["version"],
```

然后在 `prefill = prefill or {}` 一行**之后**、`self._send_html(...)` **之前**插入：

```python
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
```

最后在 `render("admin_project_form.html", {...})` 字典末尾（`"listed_checked": ...` 之后）加一行：

```python
            "release_block": release_block,
```

- [ ] **Step 3: do_POST 注册 release 路由**

`server.py` 的 `do_POST` 内，admin 分支里 `elif path.startswith("/admin/projects/") and path.endswith("/edit"):` 区块**之后**插入：

```python
            elif path.startswith("/admin/projects/") and path.endswith("/release"):
                rel_slug = path[len("/admin/projects/"):-len("/release")]
                self._handle_admin_project_release(rel_slug)
```

- [ ] **Step 4: 实现 `_handle_admin_project_release`**

`server.py` 的 `_handle_admin_project_save` 方法**之后**插入：

```python
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
```

- [ ] **Step 5: 验证发布新版本 + 旧反馈版本不变 + 同微信号可再测**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 python3 -c "
import db, project_loader, server
from pathlib import Path
db.init_schema(); project_loader.load_all(Path('projects')); db.ensure_public_tokens()
# v1 提交一条
s1 = server.create_session('oriself', 'public-oriself')
f1 = server.submit_feedback('oriself', s1, 'dup_wx', 'a','b','c','d','e', ['x'])
# 发布 v2：直接走 DB 更新模拟 release（reserved_count 归零 + version=v2）
with db.transaction() as tx:
    tx.execute(\"UPDATE projects SET version='v2', reserved_count=0 WHERE slug='oriself'\")
# 同微信号在 v2 再测——应成功（唯一索引含版本）
s2 = server.create_session('oriself', 'public-oriself')
f2 = server.submit_feedback('oriself', s2, 'dup_wx', 'a2','b2','c2','d2','e2', ['x2'])
assert db.fetch_feedback(f1)['project_version'] == 'v1', 'old version changed'
assert db.fetch_feedback(f2)['project_version'] == 'v2', 'new version wrong'
# 同版本重复提交应被唯一索引拒绝
s3 = server.create_session('oriself', 'public-oriself')
try:
    server.submit_feedback('oriself', s3, 'dup_wx', 'a','b','c','d','e', ['x'])
    raise SystemExit('FAIL: duplicate in same version was allowed')
except Exception as e:
    assert 'uniq_wechat_per_project_version' in str(e) or 'UNIQUE' in str(e), e
print('TASK7 OK: v1 keeps v1, v2 re-test allowed, same-version dup rejected')
"
```
Expected: 末行 `TASK7 OK: ...`。

---

## Task 8: 入口链接 + .env.example + 启动告警 + purge 脚本核对

**Files:**
- Modify: `templates/landing.html`, `templates/task_hall.html`
- Modify: `.env.example`
- Modify: `server.py`（`main()`）
- Verify-only: `scripts/purge_wechat.py`

- [ ] **Step 1: landing.html 加 /coins 入口**

`templates/landing.html` 的 tester 入口块里，`<p>点上面的 <a href="/hall">进入任务大厅</a>...</p>` 一行**之后**插入：

```html
    <p>已经提交过反馈？<a href="/coins">查询我的金币余额</a>，攒够 100 金币可兑换咨询或周末提现。</p>
```

- [ ] **Step 2: task_hall.html 加 /coins 入口**

`templates/task_hall.html` 中 `{{cards}}` 一行**之前**插入：

```html
  <p class="meta"><a href="/coins">查询我的金币余额 →</a></p>
```

- [ ] **Step 3: .env.example 加 PROBE_COIN_SECRET**

`.env.example` 末尾追加：

```
# 金币哈希盐：把微信号转成可跨 30 天隐私清理存活的耐久身份（用于金币余额聚合）。
# 一旦设定不可更改，否则历史金币哈希全部对不上。生产务必设一个随机值。
PROBE_COIN_SECRET=
```

- [ ] **Step 4: server.main() 加未设盐告警**

`server.py` 的 `main()` 内，`ai_worker.start_in_background()` 一行**之前**插入：

```python
    if not os.environ.get("PROBE_COIN_SECRET", ""):
        log.warning("PROBE_COIN_SECRET 未设置，金币哈希使用默认开发盐值；"
                    "生产请在 .env 设定一个随机值（设定后不可更改）。")
```

- [ ] **Step 5: 核对 purge 脚本不动 wechat_hash**

Run:
```bash
grep -n 'wechat_hash' /niuniu869_dev/probe/scripts/purge_wechat.py || echo 'PURGE OK: 不涉及 wechat_hash'
```
Expected: `PURGE OK: 不涉及 wechat_hash`（脚本只 `UPDATE ... SET wechat_id=NULL`，无需改动）。

- [ ] **Step 6: 验证服务能正常启动**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 PROBE_BIND=127.0.0.1 \
  PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass PORT=8099 \
  python3 server.py > /tmp/pv-server.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8099/healthz | grep -q ok && echo 'BOOT OK'
grep -q 'PROBE_COIN_SECRET 未设置' /tmp/pv-server.log && echo 'WARN OK'
kill %1 2>/dev/null
```
Expected: `BOOT OK`、`WARN OK`。

---

## Task 9: 更新 CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: §4.2 db.py 描述补充**

在 §4.2 `db.py` 的「核心表」条目后补一句：

```
- **金币聚合**：`coin_balance(wechat_hash)` 复用 payout 状态机聚合（confirmed=可提现 / paid=已提现 / na+suggested=评估中）；`wechat_hash()` 是微信号单向哈希，跨 30 天隐私清理存活
- **版本控制**：`projects.version` + `feedback.project_version` 快照；唯一索引升级为 `uniq_wechat_per_project_version(project_slug, project_version, wechat_id)`——同微信号同项目同版本仅一条，但换版本可再测
```

- [ ] **Step 2: §9 Changelog 追加条目**

§9 Changelog 末尾追加：

```
- **2026-05-19**（版本控制 + 金币余额 + 上线两个公开项目）
  - 新增 `projects.version`（DEFAULT 'v1'）+ `feedback.project_version` 快照：同一产品不同版本的反馈有效性分离；admin 列表/详情/项目页/看板/CSV 加版本列
  - 唯一索引 `uniq_wechat_per_project` → `uniq_wechat_per_project_version`（三列）：v1 测过的人可合法再测 v2
  - admin 项目编辑页加「发布新版本」动作（`POST /admin/projects/<slug>/release`）：更新版本号 + 名额计数归零
  - 新增 `feedback.wechat_hash`（微信号单向哈希，跨隐私清理存活）+ `/coins` 无登录余额查询页 + 收据页金币展示；金币与人民币 1:1，复用 payout 状态机
  - 新增环境变量 `PROBE_COIN_SECRET`（金币哈希盐，设定后不可更改）
  - 上线 `cyber-council`（哲人议会）、`oriself`（OriSelf）两个公开项目，各 30 名额
```

- [ ] **Step 3: 验证（无可执行验证，目视检查）**

Run:
```bash
grep -c '2026-05-19' /niuniu869_dev/probe/CLAUDE.md
```
Expected: 输出 ≥ 1。

---

## Task 10: 端到端验收

**Files:** 无改动，纯验收。

- [ ] **Step 1: 起一个干净的 mock 服务**

Run:
```bash
cd /niuniu869_dev/probe && rm -f /tmp/pv.sqlite3 && \
PROBE_DB_PATH=/tmp/pv.sqlite3 PROBE_LLM_MOCK=1 PROBE_BIND=127.0.0.1 \
  PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass PORT=8099 \
  PROBE_COIN_SECRET=e2e-test-salt \
  python3 server.py > /tmp/pv-server.log 2>&1 &
sleep 2
```

- [ ] **Step 2: 任务大厅列出两个新项目**

Run:
```bash
curl -s http://127.0.0.1:8099/hall | grep -o '哲人议会\|OriSelf' | sort -u
```
Expected: 两行 `OriSelf` 与 `哲人议会`。

- [ ] **Step 3: 完整 tester 流程 → 收据页**

Run:
```bash
SID=$(curl -s "http://127.0.0.1:8099/p/cyber-council?t=public-cyber-council" \
  | grep -o '[a-f0-9]\{32\}' | head -1)
RED=$(curl -s -o /dev/null -w '%{redirect_url}' -X POST \
  "http://127.0.0.1:8099/p/cyber-council/feedback" \
  --data-urlencode "session_id=$SID" --data-urlencode 'wechat_id=e2e_user' \
  --data-urlencode 'q1=第一眼' --data-urlencode 'q2=路径' --data-urlencode 'q3=卡点' \
  --data-urlencode 'q4=放弃' --data-urlencode 'q5=改动' --data-urlencode 'custom_0=答')
curl -s "http://127.0.0.1:8099${RED}" | grep -q '你在 Probe 的金币' && echo 'E2E receipt OK'
```
Expected: `E2E receipt OK`。

- [ ] **Step 4: 等 AI worker 评分，admin 确认，验证金币进 /coins**

Run:
```bash
sleep 35   # AI worker 30s 扫一次
A='-u admin:devpass'
# 取最新反馈 id
FID=$(curl -s $A http://127.0.0.1:8099/admin/feedback | grep -o '/admin/feedback/[0-9]*' | head -1 | grep -o '[0-9]*')
# 一键确认（带 Origin 满足同源校验）
curl -s $A -H 'Origin: http://127.0.0.1:8099' -X POST \
  "http://127.0.0.1:8099/admin/feedback/$FID/confirm" -o /dev/null
# /coins 查询应显示可提现 > 0
curl -s -X POST http://127.0.0.1:8099/coins -d 'wechat_id=e2e_user' \
  | grep -o '<strong>[0-9]*</strong> 金币可提现'
kill %1 2>/dev/null
```
Expected: 输出形如 `<strong>9</strong> 金币可提现`（mock LLM 对该反馈打 3 分 → ¥9；数值 > 0 即通过）。

- [ ] **Step 5: 旧库迁移回归（生产副本）**

Run:
```bash
cd /niuniu869_dev/probe && \
sshpass -p 'a1b2c3d4<>++' scp -o StrictHostKeyChecking=accept-new \
  root@101.33.32.162:/opt/probe/data/db.sqlite3 /tmp/pv-prod2.sqlite3 && \
PROBE_DB_PATH=/tmp/pv-prod2.sqlite3 python3 -c "
import db
db.init_schema()
r = db.get_conn().execute('SELECT project_version, wechat_hash FROM feedback WHERE id=1').fetchone()
assert r['project_version'] and r['wechat_hash'], r
print('E2E migrate OK')
"
```
Expected: `E2E migrate OK`。

- [ ] **Step 6: 汇总验收结论**

确认 Step 2-5 全部通过。若任一步失败，回到对应 Task 修复后重跑本 Task。清理临时文件：

```bash
rm -f /tmp/pv*.sqlite3 /tmp/pv.cookie /tmp/pv-server.log
```

---

## Task 11: Codex 审计 → 提交 → 推送 → 部署

**Files:** 无代码改动。

- [ ] **Step 1: 交 Codex 审计**

向作者报告 Task 1-10 完成情况，并把改动交 Codex 审计。**审计未通过 → 回到对应 Task 修复并重跑 Task 10，不进入 Step 2。**

- [ ] **Step 2: 审计通过后，一次性提交**

```bash
cd /niuniu869_dev/probe
git add -A
git status   # 人工确认改动范围：db.py project_loader.py server.py
             # templates/* projects/*.json .env.example CLAUDE.md docs/
git commit -m "feat: 项目版本控制 + 金币余额查询 + 上线 cyber-council/oriself

- projects.version + feedback.project_version 快照，唯一索引升级含版本
- admin「发布新版本」动作；列表/详情/项目页/看板/CSV 加版本列
- feedback.wechat_hash 耐久身份 + /coins 无登录余额查询 + 收据页金币展示
- 新增 PROBE_COIN_SECRET 环境变量
- 上线 cyber-council、oriself 两个公开项目（各 30 名额）"
```

- [ ] **Step 3: 推送 + 生产 pull**

```bash
cd /niuniu869_dev/probe && git push
sshpass -p 'a1b2c3d4<>++' ssh -o StrictHostKeyChecking=accept-new \
  root@101.33.32.162 'cd /opt/probe && git pull'
# post-merge 钩子自动 systemctl restart probe.service
```

- [ ] **Step 4: 生产配置 PROBE_COIN_SECRET 并重启**

在 `/opt/probe/.env` 增加一行 `PROBE_COIN_SECRET=<随机值>`（用 `python3 -c "import secrets;print(secrets.token_urlsafe(24))"` 生成），然后重启：

```bash
SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
sshpass -p 'a1b2c3d4<>++' ssh root@101.33.32.162 \
  "echo 'PROBE_COIN_SECRET=$SECRET' >> /opt/probe/.env && \
   chmod 600 /opt/probe/.env && systemctl restart probe.service"
```

> 注意：生产 `feedback#1` 的 `wechat_hash` 在首次 `git pull` 重启时已用**默认盐**回填。设置真实 `PROBE_COIN_SECRET` 后该行哈希将与新盐不一致——但该样本反馈的 tester 不需要跨项目金币聚合，影响可忽略。**为保持一致**，可在设盐重启后手动重算该行：见 Step 5。

- [ ] **Step 5: 重算样本反馈哈希（保持一致）**

```bash
sshpass -p 'a1b2c3d4<>++' ssh root@101.33.32.162 \
  'cd /opt/probe && set -a && . ./.env && set +a && \
   python3 -c "
import db
r = db.get_conn().execute(\"SELECT id,wechat_id FROM feedback WHERE wechat_hash IS NOT NULL AND wechat_id IS NOT NULL\").fetchall()
with db.transaction() as tx:
    for x in r:
        tx.execute(\"UPDATE feedback SET wechat_hash=? WHERE id=?\", (db.wechat_hash(x[\"wechat_id\"]), x[\"id\"]))
print(\"rehashed\", len(r), \"rows\")
"'
```
Expected: `rehashed 1 rows`。

- [ ] **Step 6: 线上 smoke 测试**

```bash
curl -s https://probe.niuniu869.com/hall | grep -o '哲人议会\|OriSelf' | sort -u
curl -s https://probe.niuniu869.com/coins | grep -q '查询我的金币余额' && echo 'COINS LIVE OK'
curl -s -u 'admin:<生产密码>' https://probe.niuniu869.com/admin/projects | grep -q '当前版本' && echo 'ADMIN LIVE OK'
```
Expected: 大厅含两个新项目；`COINS LIVE OK`；`ADMIN LIVE OK`。

---

## 自查清单（计划完成后）

- **Spec 覆盖**：§3 上线项目→Task 2；§4 版本控制→Task 1/3/6/7；§5 金币→Task 1/3/4/5；§6 文件清单→全 Task；§7 端到端→Task 10；§8 部署→Task 11。无遗漏。
- **类型一致性**：`db.wechat_hash()` / `db.coin_balance()` 在 Task 1 定义，Task 3/4/5 调用签名一致；`coin_balance` 返回键 `withdrawable`/`paid`/`pending_count` 在 Task 4/5 使用一致；`seed_project` 新参 `version`/`listed` 在 Task 1 定义、Task 2 透传一致。
- **占位符**：无 TBD/TODO；每个代码步骤含完整代码。
