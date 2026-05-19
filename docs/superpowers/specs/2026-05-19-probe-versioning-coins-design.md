# Probe — 版本控制系统 + 金币余额 + 上线两个新项目

> 设计文档 · 2026-05-19 · 作者审定通过
> 目标：上线 cyber-council / oriself 两个公开项目；为项目引入轻量版本控制；
> 为 tester 引入金币余额查询；端到端验收后部署生产。

---

## 1. 背景与目标

Probe 是 ¥10 一次的 AI 产品众测平台。本次迭代解决三件事：

1. **上线两个新项目**：`cyber-council`（哲人议会 The Council）、`oriself`（OriSelf），
   各 30 个体验名额，公开到任务大厅 `/hall`。
2. **轻量版本控制**：同一产品发不同版本时，反馈必须与版本绑定——v1 的反馈对
   v2 不再有效。作者需要能"发布新版本"，旧反馈保留旧版本号。
3. **金币余额系统**：tester 无需登录，输入微信号即可查询"可提现金币余额"；
   前端展示"攒够 100 金币可兑换一次咨询，或周末微信提现"。

非目标（YAGNI）：tester 账户/登录体系；自动支付；多版本并发收集；金币流水账表。

---

## 2. 现状关键事实

- 技术栈：Python 3.12 stdlib，无第三方依赖；vanilla HTML `{{var}}` 模板。
- 状态机：`feedback` 双轴 `ai_status` × `payout_status`；payout 走
  `na → suggested → confirmed → paid`，全部经 `server.transition_payout()`。
- 隐私承诺：`wechat_id` 30 天后被 `scripts/purge_wechat.py` 清理（置 NULL）。
- 生产现状：唯一项目 `explorecipe-v2`，`max_feedback_count=1` 已收满，
  `feedback#1` 已核查为**有效样本**（depth 5/5，¥15，无注入）。
- ARCH-3：`projects/*.json` 是"种子"，DB 是真相源；已存在项目不被启动期覆盖。

---

## 3. 上线两个新项目

新增两份 `projects/*.json`（走 git 评审，符合"配置即代码"）：

### 3.1 `projects/cyber-council.json`
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

### 3.2 `projects/oriself.json`
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

### 3.3 配置加载改动

`project_loader.py`：
- `_validate` 新增两个**可选**字段校验：
  - `version`：若存在，必须非空字符串；缺失默认 `"v1"`。
  - `listed`：若存在，必须严格 `bool`；缺失默认 `false`。
- `load_all` 把 `version` / `listed` 透传给 `db.seed_project`。

`db.seed_project`：签名加 `version: str` 与 `listed: int` 两个参数。
- `INSERT` 时写入 `version`、`listed`。
- `ON CONFLICT(slug) DO NOTHING` 不变——已存在项目的 `listed`/`version`
  尊重 DB 现值（ARCH-3，admin 是真相源）。仅**首次 seed** 生效。

> 公开项目的"立即参与"公共 token 由现有 `db.ensure_public_tokens()` 自动
> seed，无需在 JSON 里手填。JSON 里的 `seed-*-001` 仅作可选定向链接。

---

## 4. 轻量版本控制系统

### 4.1 数据模型

- `projects` 新增 `version TEXT NOT NULL DEFAULT 'v1'`。
- `feedback` 新增 `project_version TEXT`（提交时从所属项目快照）。
- **防重复领钱唯一索引升级**：
  `uniq_wechat_per_project (project_slug, wechat_id)`
  → `uniq_wechat_per_project_version (project_slug, project_version, wechat_id)`
  仍带 `WHERE wechat_id IS NOT NULL` 部分条件。
  语义变化：同一微信号在**同一项目的同一版本**只能提交一次；但 v1 测过的
  人可以**合法地再测 v2 并再次获酬**——这正是版本控制的核心价值。

### 4.2 Schema 与迁移（`db.py`）

`SCHEMA_SQL` 改动：
- `projects` 的 `CREATE TABLE` 加 `version TEXT NOT NULL DEFAULT 'v1'`。
- `feedback` 的 `CREATE TABLE` 加 `project_version TEXT` 和 `wechat_hash TEXT`
  （见 §5）。
- **删除** `SCHEMA_SQL` 里的 `uniq_wechat_per_project` 索引定义——该唯一索引
  改由 `init_schema()` 迁移代码统一管理（因为它依赖 `project_version` 列，
  必须在 `ALTER TABLE` 之后才能建）。

`init_schema()` 迁移块按序执行：
1. `executescript(SCHEMA_SQL)`——建表（含新列，对新库生效）。
2. `ALTER TABLE projects ADD COLUMN version ...`（旧库缺列时）。
3. `ALTER TABLE feedback ADD COLUMN project_version TEXT`（旧库缺列时）。
4. `ALTER TABLE feedback ADD COLUMN wechat_hash TEXT`（旧库缺列时）。
5. 回填 `project_version`：
   `UPDATE feedback SET project_version =
     (SELECT version FROM projects WHERE projects.slug = feedback.project_slug)
    WHERE project_version IS NULL`。
6. 回填 `wechat_hash`：对每条 `wechat_hash IS NULL AND wechat_id IS NOT NULL`
   的行，用 §5 哈希函数算出并 `UPDATE`。
7. `DROP INDEX IF EXISTS uniq_wechat_per_project`。
8. `CREATE UNIQUE INDEX IF NOT EXISTS uniq_wechat_per_project_version
    ON feedback(project_slug, project_version, wechat_id)
    WHERE wechat_id IS NOT NULL`。

> 顺序保证幂等：新库走 1+8（2-7 全部 no-op）；旧库走 1-8 完整迁移。
> 迁移在 `init_schema()` 单次启动期执行，无并发。

### 4.3 "发布新版本"动作

admin 项目编辑页（`/admin/projects/<slug>/edit`）新增一个独立表单区块
「发布新版本」，POST 到新路由 `/admin/projects/<slug>/release`：

- 字段：`new_version`（必填，非空）；可选 `trial_url`、`description`、
  `max_feedback_count`。
- 校验：`new_version` 非空且不等于当前版本；`trial_url` 若填必须 http(s)；
  `max_feedback_count` 若填必须 1-100 整数。
- 事务内：`UPDATE projects SET version=?, reserved_count=0
  [, trial_url=?, description=?, max_feedback_count=?] WHERE slug=?`。
  `reserved_count` 归零 → 新版本拿到全新名额。
- 旧反馈 `project_version` 不变（历史数据按版本沉淀）。
- 必须经 `_require_admin` + `_require_same_origin`（CSRF）。

### 4.4 版本展示

- `admin_list.html` / `_handle_admin_list`：反馈行加「版本」列。
- `admin_detail.html` / `_handle_admin_detail`：详情加「版本」字段。
- `admin_projects.html` / `_handle_admin_projects`：项目行加「当前版本」列。
- `_handle_admin_dashboard` 漏斗 head 加版本标签。
- `_handle_admin_export` CSV 加 `project_version` 列。

### 4.5 explorecipe-v2 说明

`explorecipe-v2` 迁移后自动获 `version='v1'`，其 `feedback#1` 回填为
`project_version='v1'`。其 `max_feedback_count=1`（历史测试残留）**不在本次
代码改动范围内**——部署时若作者想继续收 explorecipe 反馈，可在 admin 用
「发布新版本」或编辑表单调大名额。实现阶段会在交付说明里提示。

---

## 5. 金币余额系统

### 5.1 核心约束与方案

微信号 30 天后被清理，但金币余额必须长期存活。方案：用**微信号的单向哈希**
作为可跨清理存活的耐久身份。

- `feedback` 新增 `wechat_hash TEXT`，提交时写入；`purge_wechat.py` 只清
  `wechat_id`，**不动 `wechat_hash`**（实现阶段需核对脚本，确认其 SQL 仅
  涉及 `wechat_id`）。
- 哈希函数 `db.wechat_hash(wechat_id) -> str`：
  `sha256((PROBE_COIN_SECRET + ":" + wechat_id.strip().lower()).encode()).hexdigest()`。
  `PROBE_COIN_SECRET` 取自环境变量；未设时用文档化的固定回退常量并打
  warning（哈希仅用于聚合与跨清理存活，非高敏安全边界）。
- `.env.example` 增加 `PROBE_COIN_SECRET` 条目与说明。

### 5.2 余额口径（复用现有 payout 状态机，不建新表）

给定 `wechat_hash`：

| 指标 | 定义 |
|---|---|
| 可提现余额 | `SUM(credit_confirmed)` WHERE `wechat_hash=?` AND `payout_status='confirmed'` |
| 已提现 | `SUM(credit_confirmed)` WHERE `wechat_hash=?` AND `payout_status='paid'` |
| 评估中 | `COUNT(*)` WHERE `wechat_hash=?` AND `payout_status IN ('na','suggested')` |

`db.coin_balance(wechat_hash) -> dict` 返回上述三项。金币与人民币 1:1，
"金币"是 ¥ credit 的前端友好叫法。

### 5.3 提交时写入

`server.submit_feedback`：插入 `feedback` 时同时写入
`project_version`（项目快照）与 `wechat_hash`（`db.wechat_hash(wechat_id)`）。
版本号与哈希从调用方已有的 `project` 行 + 表单 `wechat_id` 取得。

### 5.4 `/coins` 查询页（无登录）

- 路由 `GET /coins`：渲染 `coins.html`，只显示输入框。
- 路由 `POST /coins`：读 `wechat_id` → `db.wechat_hash` → `db.coin_balance`
  → 渲染 `coins.html` 带余额结果。
- 用 **POST** 提交，避免微信号出现在 URL/访问日志。此为公开只读查询、不改
  任何状态，故**不需要** `_require_same_origin`。
- 微信号为空时回显友好提示。

### 5.5 收据页展示

`receipt.html` / `_handle_receipt`：提交后展示——
- 本次预计金额区间（沿用现有 `credit_range`）。
- 「你在 Probe 的累计可提现金币」：用刚提交反馈的 `wechat_hash` 算
  `coin_balance`。注意此刻新反馈通常仍 `suggested`（AI 未评完），故收据页
  主要展示既有的可提现余额，文案说明本次金额稍后并入。
- 规则文案：
  > 可提现金币 ≥ 100 时，可以找作者用 100 金币兑换一次咨询，
  > 或攒着每周末微信提现。
- 提供「查询我的金币余额 →」链接到 `/coins`。

### 5.6 入口

`landing.html` 与 `task_hall.html` 增加一个指向 `/coins` 的链接
（"查我的金币"）。

---

## 6. 触及文件清单

| 文件 | 改动 |
|---|---|
| `projects/cyber-council.json` | 新增 |
| `projects/oriself.json` | 新增 |
| `db.py` | schema 加列；迁移块；删旧索引定义；`seed_project` 加参；新增 `wechat_hash()`、`coin_balance()` |
| `project_loader.py` | `_validate` 支持 `version`/`listed`；`load_all` 透传 |
| `server.py` | `submit_feedback` 写版本+哈希；`/coins` GET/POST；`/admin/projects/<slug>/release`；admin 列表/详情/项目页/看板/CSV 加版本列 |
| `templates/receipt.html` | 加余额块 + 规则文案 |
| `templates/coins.html` | 新增 |
| `templates/admin_list.html` | 加版本列 |
| `templates/admin_detail.html` | 加版本字段 |
| `templates/admin_projects.html` | 加版本列 |
| `templates/admin_project_form.html` | 加「发布新版本」表单区块 |
| `templates/landing.html` / `task_hall.html` | 加 `/coins` 入口 |
| `scripts/purge_wechat.py` | 核对：确认只清 `wechat_id`，不动 `wechat_hash` |
| `.env.example` | 加 `PROBE_COIN_SECRET` |
| `CLAUDE.md` | 更新架构/模块/Changelog（版本控制 + 金币） |

---

## 7. 端到端验收

本地 `PROBE_LLM_MOCK=1` + `PROBE_BIND=127.0.0.1` 起服务，验证：

1. `/hall` 列出 cyber-council、oriself（各 30 名额），不列定向项目。
2. 经公共 token 进 cyber-council 项目卡 → 反馈表单 → 提交 → 收据页。
3. 收据页显示累计金币 + ≥100 规则 + `/coins` 链接。
4. `/coins` 输入该微信号 → 显示余额（评估中/可提现/已提现）。
5. admin：反馈列表/详情显示版本号；对反馈评分→确认；`/coins` 余额随
   `confirmed` 增长。
6. admin「发布新版本」cyber-council 到 v2：`reserved_count` 归零；
   旧反馈仍显示 `v1`；**同一微信号能在 v2 再次提交**（验证唯一索引升级）。
7. `export.csv` 含 `project_version` 列且无公式注入。
8. 启动期对旧库迁移：核对 `feedback#1` 回填了 `project_version` 与
   `wechat_hash`，唯一索引已切换为三列版。

---

## 8. 部署（作者已授权"一条龙"）

1. 实现完成 + 本地端到端验收通过。
2. 交 Codex 审计；若有问题则修复后复审。
3. 审计通过 → `git add -A && git commit && git push`。
4. SSH 生产 `cd /opt/probe && git pull`（`post-merge` 钩子自动重启
   `probe.service`）。
5. 生产环境需在 `/opt/probe/.env` 增加 `PROBE_COIN_SECRET`（随机值），
   重启服务使其生效。
6. 线上 smoke：`/hall` 见两个新项目、`/coins` 可查、admin 正常。

---

## 9. 风险与对策

| 风险 | 对策 |
|---|---|
| 唯一索引迁移时机错位（列未建先建索引） | 索引从 `SCHEMA_SQL` 移出，迁移块严格在 `ALTER` 之后建 |
| `wechat_hash` 回填遗漏导致老反馈不计入余额 | 迁移块显式回填所有 `wechat_hash IS NULL` 行 |
| `PROBE_COIN_SECRET` 生产换值导致旧哈希对不上 | 一次设定后不再更改；`.env.example` 注明"设定后不可变" |
| 发布新版本误清 `reserved_count` 影响在收名额 | 「发布新版本」是显式动作、与普通「编辑」分离，文案明确告知名额归零 |
| 公开项目 JSON `listed` 被启动期反复覆盖 admin 设置 | `seed_project` 用 `ON CONFLICT DO NOTHING`，`listed` 仅首次 seed 生效 |
