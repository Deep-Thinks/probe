[根目录](../CLAUDE.md) > **templates**

# templates/ — HTML 模板（极简 `{{var}}` 替换）

## 模块职责

存放 Probe 全部 7 个 HTML 页面模板。**没有 Jinja2、没有任何模板引擎**，仅靠 `server.render(name, vars)` 做最朴素的字符串 `replace("{{key}}", val)`。这意味着：

- **不支持条件、循环、过滤器**：所有复杂列表（feedback 表的行、追问 `<li>` 列表、自定义题块）都在 `server.py` / `ai_worker.py` 的 Python 代码里**拼好 HTML 字符串**，再以 `rows` / `followup_items` / `custom_block` 等"已经是 HTML"的变量塞进去。
- **不会自动转义**：所有插入用户输入的位置必须在 Python 端先调 `server.esc()`（= `html.escape(s, quote=True)`），模板里也务必用属性引号包裹（如 `value="{{wechat_id}}"`）。
- **模板缓存**：`server._template_cache` 一旦读过就常驻内存，**修改模板需重启服务**（v1 dogfood 接受此约束）。

## 入口与启动

无独立入口。由 `server.render(name, vars)` 在路由处理函数中读取并渲染：

```python
self._send_html(render("project_card.html", {
    "name": esc(project["name"]),
    "slots_left": project["max_feedback_count"] - project["reserved_count"],
    ...
}))
```

模板根目录 = `Path(__file__).parent / "templates"`（在 `server.py` 顶部定义 `TEMPLATE_DIR`）。

## 文件清单

| 模板 | 调用方 | 用途 | 主要变量 |
|---|---|---|---|
| `project_card.html` | `_handle_project_card` | tester 入口卡片（名额、试用链接、session 占位） | `name` `description` `trial_url` `slug` `session_id` `max_feedback_count` `reserved_count` `slots_left` |
| `feedback_form.html` | `_handle_feedback_form` | 4 固定题 + 0-2 自定义题 + 微信号表单 | `name` `slug` `session_id` `q1`-`q4` `wechat_id` `custom_questions_block` `error_block` |
| `receipt.html` | `_handle_receipt` | 提交完成的收据页 | `name` `session_id` `feedback_id` |
| `project_full.html` | `_handle_project_card` / `_handle_feedback_submit` | 项目已满员（`reserved_count >= max_feedback_count`） | `name` `max_feedback_count` |
| `error.html` | `_error_page` / `_require_same_origin` | 通用错误页（404 / 403 / 400 / 500） | `title` `message` |
| `admin_list.html` | `_handle_admin_list` | 所有反馈一览表（最多 500 条） | `total` `rows`（预渲染 HTML 表格行字符串） |
| `admin_detail.html` | `_handle_admin_detail` | 单条反馈详情 + AI 推测 + payout 动作区 | 见下 |

`admin_detail.html` 的完整变量列表（共 19 个 placeholder）：
`id` `project_slug` `submitted_at` `q1`-`q4` `custom_block` `ai_status` `ai_model_used` `ai_attempts` `ai_depth_score` `ai_depth_rationale` `ai_stuck_step` `ai_stuck_confidence` `followup_items` `risk_flags` `payout_status` `credit_suggested` `credit_confirmed` `wechat_id` `action_block`。其中 `action_block` 由 `_render_action_block()` 根据 `payout_status` 动态生成"一键确认/改值确认/拒绝/标记已转账"四种按钮组合，并对 `prompt_injection_attempt` 命中行**禁用一键确认按钮**（第 4 层防御）。

## 对外接口

无直接 HTTP 接口。模板由路由处理器选用：

- tester 端：`GET /p/<slug>` → `project_card.html`；`GET /p/<slug>/feedback` → `feedback_form.html`；`POST` 成功 → `receipt.html`；满员 → `project_full.html`
- admin 端：`GET /admin` → `admin_list.html`；`GET /admin/feedback/<id>` → `admin_detail.html`
- 异常路径：所有 `_error_page()` 调用 → `error.html`

## 关键依赖与配置

- **公共 CSS**：所有页面 `<link rel="stylesheet" href="/static/style.css">`，参见 [`static/CLAUDE.md`](../static/CLAUDE.md)
- **公共字体栈**：苹方/微软雅黑 fallback，定义在 `static/style.css` 的 `body` 选择器
- **隐私脚注**：tester 三个页面（`project_card` / `feedback_form`）含 `.privacy` 文案块，明确披露 wechat_id 30 天清理 + 备份 30 天滚动
- **footer**：所有页面统一 `Probe · probe.niuniu869.com`

## 测试与质量

- v1 无模板单元测试。
- 风险点：手工修改模板时若误删某个 `{{key}}` 会让 `render()` 留下未替换的 `{{key}}` 字面量直接显示给用户；Python 端若漏传 key 也不会报错（`str.replace` 找不到就什么都不做）。
- 建议人工 checklist：每改一处模板，对照路由处理函数的 `render(...)` 字典 grep 全部 placeholder。

## 常见问题 (FAQ)

**Q: 为什么不用 Jinja2？**
A: plan §技术栈 显式要求 "vanilla HTML + Python stdlib"，避免在 dogfood 阶段引入构建链和模板调试复杂性。模板逻辑刻意保持"只替换、不计算"。

**Q: 怎么加新模板？**
A:
1. 在 `templates/` 放 `<new_name>.html`，用 `{{key}}` 占位
2. 在 `server.py` 路由里调 `render("<new_name>.html", { ... })`
3. 修改后**重启服务**（`_template_cache` 不会自动失效）

**Q: 为什么 `admin_detail.html` 把 `<form>` 写在外层 Python 而不是模板里？**
A: `action_block` 需要根据 `payout_status` 出现 4 种完全不同的按钮组合 + 注入命中需要禁用一键确认。极简模板不支持条件，所以这种"分支型"片段全部在 `_render_action_block()` 里拼好 HTML 字符串再塞回模板的 `{{action_block}}`。

## 相关文件清单

- `project_card.html`、`feedback_form.html`、`receipt.html`、`project_full.html`、`error.html`、`admin_list.html`、`admin_detail.html`
- 关联渲染逻辑：`/niuniu869_dev/probe/server.py`（路由 `_handle_*` 和 `_render_action_block`）
- 关联样式：`/niuniu869_dev/probe/static/style.css`

## 变更记录 (Changelog)

- **2026-05-16 02:07:03**：首次生成模块文档。
