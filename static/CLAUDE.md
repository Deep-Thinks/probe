[根目录](../CLAUDE.md) > **static**

# static/ — 静态资源（单文件极简 CSS）

## 模块职责

存放所有公开静态文件。当前 v1 dogfood 只有一份 `style.css`，纯白极简风，被 7 个 HTML 模板共享。**无 JS、无构建链、无图片资源**。

## 入口与启动

无独立入口。由 `server.Handler._serve_static()` 在 `GET /static/<name>` 路径上代为提供：

- 路径穿越防御：拒绝包含 `/`、`..` 或空名的请求
- MIME：`.css` → `text/css`，其它 → `application/octet-stream`
- 缓存：`Cache-Control: public, max-age=3600`（一小时浏览器缓存）

```python
STATIC_DIR = Path(__file__).parent / "static"  # 在 server.py 顶部定义
```

## 对外接口

| Method | Path | 行为 |
|---|---|---|
| `GET` | `/static/style.css` | 返回纯白极简风样式表 |
| `GET` | `/static/<其它名>` | 若文件存在则返回；否则 `error.html` 404 |
| `GET` | `/static/<含 `/`、`..`、空名>` | 直接 404（防目录穿越） |

## 关键依赖与配置

样式系统约定（`style.css` 顶部 `:root` 自定义属性）：

```css
--fg: #1a1a1a;     /* 主前景色 */
--muted: #666;     /* 次要文本 */
--line: #e5e5e5;   /* 边框/分隔线 */
--accent: #0a66ff; /* 主按钮蓝 */
--warn: #d9381e;   /* 错误/拒绝红 */
--bg-soft: #fafafa;/* 浅灰底 */
--max: 720px;      /* 内容最大宽 */
```

- **关键 class**：`.wrap`（720px 居中容器）、`.card`（圆角边框块）、`.btn` / `.btn-secondary` / `.btn-danger`、`.pill` / `.pill-done` / `.pill-failed` / `.pill-pending` / `.pill-paid` / `.pill-reject`、`.muted` / `.warn` / `.privacy` / `.row` / `form.inline` / `table`
- **admin 列表页拓宽**：`admin_list.html` 用 `style="max-width:1100px"` 内联覆盖 `.wrap` 默认 720px（表格需要更宽）
- **字体栈**：苹方/微软雅黑 fallback；移动端无额外 media query（极简风刻意不做响应式断点，原生 viewport meta 已经够用）

## 数据模型

无数据模型（静态资源）。

## 测试与质量

- 无自动化测试。
- 建议视觉回归：手工检查 `pill-*` 颜色对 `ai_status` / `payout_status` 全部状态值都有对应 class（避免新增状态后没有样式 fallback 到无色 pill）。
  - 已覆盖：`done`、`failed`、`pending`、`paid`、`reject`（注意 admin 后端真实状态是 `rejected`，但 CSS class 写的是 `pill-reject`——目前 admin_list 用的是 `payout_status` 直接拼接 `pill-{esc(payout_status)}` → 实际生成 `pill-rejected` **没有匹配 CSS**；这是一个**潜在的样式缺口**，建议下个迭代修正）

## 常见问题 (FAQ)

**Q: 为什么不用 Tailwind / SCSS？**
A: plan 显式要求 "vanilla HTML/CSS"，避免 dogfood 阶段引入 npm/构建链。

**Q: 加新的静态文件（如 favicon、JS）怎么办？**
A: 直接放到 `static/` 下，浏览器请求 `/static/<name>` 即可。注意 `_serve_static` 只识别 `.css` 的 MIME，新增文件类型需要在 `server.py` 的 `ctype` 判断里加分支。

## 相关文件清单

- `style.css`
- 关联实现：`/niuniu869_dev/probe/server.py::_serve_static`
- 引用方：`/niuniu869_dev/probe/templates/*.html` 全部 7 个文件

## 变更记录 (Changelog)

- **2026-05-16 02:07:03**：首次生成模块文档；记录 `pill-rejected` 缺 CSS class 的潜在视觉缺口。
