# Probe 重设计实施计划

> 由 `/plan-design-review` 于 2026-05-16 生成。范围：tester 端 6 页 + admin 端全部重做，并新增 admin 三块功能（数据看板 / 招募工具 / 项目管理）。
> 视觉基准见 `DESIGN.md`（编辑部式衬线）。本计划是**实施蓝图**，不含代码改动；实现交给后续 PR。

---

## 0. 范围与约束

**目标**：把当前"功能可用但粗糙"的 UI 升级为面向用户的优秀交互体验。

**硬约束（不可破）**：
- 技术栈不变：Python 3.12 stdlib HTTP server + vanilla HTML + `{{var}}` 纯替换模板 + 单 `style.css`。
- `{{var}}` 模板**不支持循环/表达式**。所有列表（看板漏斗行、token 清单、二维码网格、项目列表、批次列表）继续在 Python 端拼好 HTML 字符串，模板只留一个 `{{slot}}`。
- 所有用户输入走 `esc()`；admin POST 走 `_require_admin` + `_require_same_origin`；CSV 走 `_safe_csv_cell`。重设计**不得**绕过这三层。
- prompt injection 命中时 UI 禁用「一键确认」—— plan 三层防御之一，详情页重做必须保留。
- 文案、注释、日志全简体中文。

**三项已决策**（见本计划末"决策记录"）：
- D1：自托管子集化衬线 woff2（仅标题）。
- D2：招募工具 = token 批量 + 二维码 + 批次分组（最完整）。
- D3：项目管理 = 网页内创建/编辑 → 写 DB。

---

## 1. 移交给 `/plan-eng-review` 的架构决策

本计划只定 UI/UX。以下三处涉及架构，**必须**在 `/plan-eng-review` 定清楚再实现：

| # | 来源 | 架构问题 |
|---|---|---|
| ARCH-1 | D2 二维码 | stdlib 无二维码库。需 vendored 一个纯 Python QR 生成器（~几百行），`requirements.txt` 仍空但项目不再"纯 stdlib"。需评估：vendored 模块放哪、许可证、是否生成 SVG（无依赖、可内联）而非 PNG。**建议生成 SVG 二维码**——纯字符串、可直接内联进 HTML、零图像库。 |
| ARCH-2 | D2 批次 | 新增 `recruit_batches` 表 + `invite_tokens` 增 `batch_id` 外键。涉及 schema 变更、`project_loader` 启动期 token 同步如何与 admin 生成的 token 共存。 |
| ARCH-3 | D3 项目管理 | 当前 `projects/*.json` 由 `project_loader` 启动期 upsert，"git push 即部署"。改为 admin 写 DB 后，部署模型变为"DB 是真相源"，`projects/*.json` 降为可选种子。需定：启动期 loader 与 admin 写入的优先级、并发、`projects` 表是否加 `source` 字段（json-seed / admin）。 |

---

## 2. Tester 端（6 页 + 1 新增）

调性：全亮奶油纸底、大量留白、衬线标题领衔。气质 = 被尊重、被认真对待。

### 2.1 `landing.html` `/`
- **信息层级**：品牌字标 → 一句衬线大字定位 → 「我是 tester」「我想测自己的产品」两个明确入口 → 隐私 + 源码。
- **改动**：删掉 3 张并排卡。改为编辑式分节：定位 hero（衬线 28-36px）+ 一段散文式"这是什么" + 两个左对齐入口块。隐私条用 `.privacy` 样式。
- **反 slop**：不用 3 卡网格、不居中、衬线标题。

### 2.2 `project_card.html` `/p/<slug>`
- **层级**：项目名（衬线 h1）→ 报酬/时长 meta 行 → description 段 → 名额进度条 → 「开始试用 ↗」主按钮 +「去提交反馈 →」次按钮 → session 有效期提示 → 隐私条。
- **新增**：名额用 §5.6 进度条可视化（已收 X / 上限 Y），替代纯文字。
- **状态**：满员 → `project_full.html`；token 无效 / session 过期 → `error.html`。

### 2.3 `feedback_form.html` `/p/<slug>/feedback` —— tester 核心体验
- **层级**：标题"提交反馈：项目名"（衬线）→ 一行引导（"问行为不问感受"）→ 顶部 4 题进度标记 → 4 题逐题 → 自定义题 → 微信号 → 提交。
- **进度标记**：顶部一个安静的步骤指示器，4 个编号 + 标签（第一眼 / 意图 / 卡点 / 未来），让 tester 感知"还剩几题"。这是**视觉锚**，不是真分步表单（stdlib 单页渲染）。当前题高亮（scrollspy）为可选 JS 增强，见 §7 TODO。
- **每题**：题号徽标 + 衬线小标题 + 题干说明（`--ink-muted`）+ 大文本域（`min-height:96px`，行高 1.7）。
- **改动**：从"4 个长 textarea 一字排开"改为有呼吸感的逐题块；题干与输入框成组。
- **状态**：
  - error → 每个出错字段**下方**红字 + 顶部汇总条；已填内容回填（`{{q1}}` 等已支持）。
  - 提交中 → 按钮置灰 + 文案"提交中…"，防重复提交（一小段 vanilla JS，非框架）。
- **a11y**：每 `<label>` 用 `for` 绑 `<textarea id>`；题干用 `aria-describedby` 关联。

### 2.4 `receipt.html` `/p/<slug>/receipt`
- 大号衬线"反馈已提交" + 一个克制的描边圆对勾标记。
- 明确告知："1-2 天内通过微信联系你转账 ¥3–¥15"。给反馈编号。
- **情感弧**：tester 花了 10 分钟，这页要让他觉得"值了、被记下了"——暖、有收尾感，不只是一个回执。

### 2.5 `project_full.html` —— 满员页
编辑式短页：一句话说明已满 + 引导关注作者后续项目。沿用衬线标题。

### 2.6 `error.html` —— 错误 / 失效页
衬线标题 + 暖砖红（`--risk`）说明 + 说人话 + 给下一步（如"向作者重新获取邀请链接"）。

### 2.7 【新增】session 过期状态
当前 session 30 分钟过期后行为不明。复用 `error.html`，给明确文案："会话已过期（链接 30 分钟有效），请重新打开作者给你的邀请链接。"

---

## 3. Admin 端 —— 重做 + 三块新功能

调性：深色侧栏锚定 + 亮色工作区。气质 = 指挥台。

**侧栏**（`--sidebar` 深色，固定左侧）：数据看板 / 反馈列表 / 招募工具 / 项目管理。当前项 `--sidebar-active` 底 + 左 3px `--accent` 标记。底部作者身份 + 退出。

### 3.1 【新增】数据看板 `/admin` —— "审阅当前数据"
admin 默认落地页。
- **顶部 3 指标**：待付总额 ¥ / 待审反馈数（ai done 且 payout suggested）/ 本周已付 ¥。衬线大数字（36px）。
- **各项目招募漏斗**：每项目一行，分段进度条 名额 → 已收 → 已确认 → 已付 + 数值。
- **AI 风险提示面板**：列出 `risk_flags` 含 `prompt_injection_attempt` 或 `depth_score=1` 的反馈，暖砖红标，点进详情。
- **批次转化**（来自 D2）：每个招募批次 token 发出数 vs 已用数。
- **空状态**：0 项目 → "还没有项目，去『项目管理』创建第一个。"；0 反馈 → "还没有反馈，去『招募工具』生成邀请链接。" 各配一个主按钮。

### 3.2 反馈列表 `/admin/feedback`（重做现 `admin_list.html`）
- §5.5 表格。列：# / 项目 / 提交时间 / AI / 建议¥ / 已确认¥ / Payout / 风险 / 动作。
- **新增筛选**：按项目、按 payout 状态、按 AI 状态（GET 查询参数，服务端过滤）。
- 顶部保留「导出 CSV」。
- 行 hover 态 + 明确"查看 →"列（不靠纯 hover 暴露可点）。
- **空状态**：有温度，指向招募工具。

### 3.3 反馈详情 `/admin/feedback/<id>`（重做现 `admin_detail.html`）
- 三区：**Tester 原文**（4 题 + 自定义）/ **AI 推测** / **Payout**。
- AI 推测区：`depth_score` 用 §5.6 风格 5 段标记可视化；模型名用 `--font-mono`；`processing`/`failed` 显示状态 pill + 占位 +「重新分析」。
- Payout 区：状态机动作按钮 —— confirm（主）/ reject（危险）/ mark-paid（次）/ retry-ai（次）。
- **保留硬约束**：`risk_flags` 命中 `prompt_injection_attempt` 时，「一键确认」按钮禁用 + 一句解释（plan 三层防御之一）。

### 3.4 【新增】招募工具 `/admin/recruit` —— D2 完整版
- **生成表单**：选项目 → 设本批 token 数量 + 批次名（如"原型A-微信群1"）→ 生成。
- **生成结果**：N 个 invite token + 邀请链接清单 + 每链接一个二维码（建议 SVG 内联，见 ARCH-1）+ 一段可复制招募文案。
- **招募文案模板**（自动填充项目名 / 报酬 / 时长 / 链接）：
  > 「{项目名} 招内测员：花 5-10 分钟试用并答 4 个问题，完成后微信转账 ¥3-¥15。每人专属链接：{链接}」
- **复制**：一键复制全部文案 / 单个 token 复制（vanilla JS clipboard）。
- **批次列表**：历史批次 + 每批转化（发出 N / 已用 M）。
- 架构见 ARCH-1（二维码）、ARCH-2（批次表）。

### 3.5 【新增】项目管理 `/admin/projects` —— D3 DB-backed CRUD
- **项目列表**：所有项目 + 状态（已收/上限、是否满员）+ 编辑入口 + 新建按钮。
- **创建/编辑表单**：`slug` / `name` / `description` / `trial_url` / `max_feedback_count`（1-100）/ `custom_questions`（≤2）/ `invite_tokens`。
- **校验**：服务端复用 `project_loader._validate` 的 11 项规则；错误显示在对应字段下方。
- 架构见 ARCH-3（写 DB、部署模型变更）。

---

## 4. 设计系统落地（`style.css` 重写）

- 重写 `static/style.css`，引入 DESIGN.md 全部 CSS 变量、字体、组件类。
- 自托管子集化衬线 woff2 放 `static/`，`@font-face` + `font-display:swap`。
- 新增组件类：`.sidebar` / `.metric` / `.funnel` / `.progress` / `.stepper`（表单进度）/ `.qr-grid` / `.field-error`。
- 保留并重做：`.btn` 系列、`.card`、`.pill-*`、`table`、`.privacy`、`footer`。
- 7 个模板全部按新结构改写；新增模板：`admin_dashboard.html` / `admin_recruit.html` / `admin_projects.html` / `admin_project_form.html`。

---

## 5. 七维设计检查（全部已并入上文）

| 维度 | 落点 |
|---|---|
| 信息架构 | 每页层级见 §2/§3；admin 侧栏导航 |
| 交互状态 | loading/empty/error/success/partial 见 DESIGN.md §6 + 各页"状态"小节 |
| 用户情感弧 | 反馈表单不再压迫；收据页有收尾感（§2.3/§2.4）|
| 反 AI slop | DESIGN.md §10 红线；编辑式分节替代卡网格 |
| 设计系统一致 | DESIGN.md 为单一基准；两端共用变量 |
| 响应式 | DESIGN.md §7 三断点；admin 表格窄屏选列 |
| 无障碍 | DESIGN.md §8；label 绑定、focus 描边、状态不只靠色 |

---

## 6. NOT in scope（明确不做）

- **暗色模式**（tester/admin 全局暗色）—— 编辑部式以纸感为核心，暗色是另一套系统，本轮不做。
- **自动微信转账** —— plan v1 锁定手动转账，重设计不碰支付。
- **多语言** —— 仅简体中文。
- **真正的多步表单**（分页提交）—— stdlib 单页渲染足够，进度标记是视觉锚即可。
- **构建链 / JS 框架** —— 仅允许零散 vanilla JS（防重复提交、复制、可选 scrollspy）。

---

## 7. TODOS（设计债 / 可选增强）

- **TODO-1：反馈表单进度 scrollspy**。当前题随滚动高亮 —— 纯增强，需一小段 IntersectionObserver JS。不做也不影响（编号标记已给方位感）。决策见下方 AskUserQuestion。

---

## 8. 决策记录

- **D1（2026-05-16）**：衬线标题字体 = 自托管子集化 woff2，仅标题用。理由：tester 多为国内学生，Google Fonts 不可靠；子集后体积小。
- **D2（2026-05-16）**：招募工具 = token 批量 + 二维码 + 批次分组（最完整）。引出 ARCH-1 / ARCH-2。
- **D3（2026-05-16）**：项目管理 = 网页内创建/编辑写 DB。引出 ARCH-3。

## 9. 批准的 mockup

| 屏幕 | 路径 | 方向 |
|---|---|---|
| tester 反馈表单 | `~/.gstack/projects/Deep-Thinks-probe/designs/probe-redesign-20260516/tester-form/variant-A.png` | 编辑部式衬线 |
| admin 数据看板 | `~/.gstack/projects/Deep-Thinks-probe/designs/probe-redesign-20260516/admin-dashboard/variant-A.png` | 编辑部式衬线 |

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | ISSUES_OPEN (FULL) | score: 2/10 → 9/10, 3 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **UNRESOLVED:** 3 架构决策（ARCH-1 二维码依赖 / ARCH-2 批次 schema / ARCH-3 项目管理写 DB）移交 `/plan-eng-review`。
- **VERDICT:** DESIGN CLEARED（设计 9/10）。Eng Review 未跑且为必需门禁 —— 实现前必须先跑 `/plan-eng-review` 定 ARCH-1/2/3。

