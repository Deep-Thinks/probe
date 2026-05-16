# Probe — TODOS

## 设计债 / 可选增强

### TODO-1：反馈表单进度 scrollspy
- **What**：`feedback_form.html` 顶部 4 题进度标记，随页面滚动高亮"当前题"。
- **Why**：进一步增强 tester 在长表单里的方位感与"还剩几题"的掌控感。
- **Pros**：交互更完整、更贴合 mockup 的"步骤推进"意图。
- **Cons**：需一小段 `IntersectionObserver` vanilla JS（~20 行）；当前编号 + 标签的静态标记已能给方位感，收益边际。
- **Context**：来自 `/plan-design-review`（2026-05-16）。重设计本轮先做静态编号标记；高亮为锦上添花。实现时挂在 `style.css` 重写那一 PR 之后即可。
- **Depends on**：feedback 表单重做完成（REDESIGN_PLAN.md §2.3）。
- **状态**：deferred（用户 2026-05-16 决定本轮不做，记录待办）。

### TODO-2：静态资源 cache-busting
- **What**：`server._serve_static` 给 `.css` 发 `Cache-Control: public, max-age=3600`。模板里 `<link href="/static/style.css">` 是固定路径。
- **Why**：部署重设计后，老访客最多 1 小时看到旧 CSS（新 HTML + 旧样式 = 错乱）。重设计实现期就因此踩过坑（换端口才看到新样式）。
- **Pros**：改完即时生效，部署无样式断层。
- **Cons**：需要一个版本号机制（构建期或手动 bump）。
- **Context**：来自重设计实现（2026-05-16）。最简做法：模板里写 `/static/style.css?v=N`，每次改 CSS 手动 +1；或 server 启动期算 CSS 文件 mtime/hash 注入。
- **状态**：deferred（建议下次部署前处理）。
