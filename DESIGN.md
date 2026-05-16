# Probe 设计系统（DESIGN.md）

> 本文件是 Probe 所有视觉与交互决策的**唯一基准**。任何 UI 改动先校准到这里。
> 风格方向：**编辑部式衬线（editorial serif）** —— 暖奶油纸底 + 衬线大标题 + 深墨绿强调色 + admin 深色侧栏。
> 由 `/plan-design-review` 于 2026-05-16 生成，源自 mockup 变体 A（见文末"批准的 mockup"）。

---

## 1. 设计原则（Probe 专属）

1. **信任在像素级别赢得。** Probe 让陌生人交出反馈、再等一笔真金白银的转账。界面"看起来被认真做过"本身就是信任机制。粗糙 = 不可信。
2. **tester 的时间值钱。** tester 花 5-10 分钟。每个屏幕都要回答"先看什么、再看什么"，绝不让他停下来想"我该点哪"。
3. **admin 是作者的指挥台。** 信息可以密，但必须平静、有层级。一眼看到"该处理什么"。
4. **克制优先。** 不挣钱的像素就删。一个强调色、两种字体、发丝线优先于重边框和阴影。
5. **空状态是功能。** "暂无数据"不是设计。每个空状态要有温度、一个主行动、一句上下文。
6. **反 AI slop。** 见 §10 红线。任何让 Probe 看起来像"又一个 AI 生成的 SaaS"的元素一律否决。

---

## 2. 色彩系统

所有颜色走 CSS 变量，定义在 `:root`。**禁止**在组件里写死十六进制。

```css
:root {
  /* —— 纸面 —— */
  --paper:        #F7F4ED;  /* 页面底色，暖奶油 */
  --surface:      #FFFFFF;  /* 抬起表面：卡片 / 面板 */
  --surface-2:    #FBF9F3;  /* 次级表面：表头、内嵌区 */

  /* —— 墨色（文字）—— */
  --ink:          #20251F;  /* 正文，暖黑（非纯黑）*/
  --ink-muted:    #6E726A;  /* 次要文字、说明 */
  --ink-faint:    #9A9C93;  /* 三级文字、输入占位 */

  /* —— 线 —— */
  --line:         #E2DECF;  /* 暖发丝线，默认分隔 */
  --line-strong:  #CFC9B4;  /* 强分隔 / 输入边框 */

  /* —— 强调色：深墨绿 —— */
  --accent:       #1E4E40;
  --accent-hover: #163A30;
  --accent-tint:  #E5EDE7;  /* 强调色浅底（选中、提示）*/
  --on-accent:    #FFFFFF;  /* 强调色之上的文字 */

  /* —— 语义色 —— */
  --risk:         #A8412A;  /* 风险 / 错误，暖砖红（非荧光红）*/
  --risk-tint:    #F4E4DD;
  --warn:         #9A6B12;  /* 警示，赭黄 */
  --warn-tint:    #F3E9D2;
  --ok:           #2F6B4F;  /* 成功 */
  --ok-tint:      #E3EDE6;

  /* —— admin 深色侧栏 —— */
  --sidebar:        #1B231E;
  --sidebar-ink:    #D6D8CC;
  --sidebar-muted:  #878B7F;
  --sidebar-active: #2A3A30;  /* 当前项背景 */
}
```

**对比度**：`--ink` 在 `--paper` 上对比度 ≥ 13:1；`--ink-muted` 在 `--paper` 上 ≥ 4.5:1（正文最低线）。`--ink-faint` 仅用于占位/装饰，不承载正文。

---

## 3. 字体系统

```css
:root {
  /* 衬线 —— 仅用于标题、指标大数字、品牌字标 */
  --font-display: "Source Han Serif SC", "Noto Serif SC", Songti SC, STSong, serif;
  /* 无衬线 —— 正文、表单、表格、导航 */
  --font-body: "Source Han Sans SC", "Noto Sans SC", "PingFang SC",
               "Microsoft YaHei", -apple-system, sans-serif;
  /* 等宽 —— token、session id、模型名、金额可选 */
  --font-mono: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}
```

> ⚠️ 衬线是整套识别的核心。中文系统字体默认**不带**好用的宋体级衬线（尤其安卓），必须**自托管一份子集化的衬线 woff2**（仅标题用，字形少，子集后约 50-120KB）。加载策略见 `REDESIGN_PLAN.md` 决策 1。在字体加载完成前用 `font-display: swap`，回退到系统 serif。

**字号阶梯**（16px 基准）：

| 角色 | 字号 | 字体 | 字重 | 行高 |
|---|---|---|---|---|
| 指标大数字 | 36px | display | 600 | 1.2 |
| h1 页面标题 | 28px | display | 600 | 1.3 |
| h2 区块标题 | 20px | display | 600 | 1.35 |
| h3 / 表单题号标签 | 15px | body | 600 | 1.4 |
| 正文 body | 16px | body | 400 | 1.7 |
| small 说明 | 14px | body | 400 | 1.6 |
| micro 脚注 / 标签 | 12px | body | 500 | 1.5 |

正文行高 1.7，让长反馈题读起来不挤。**正文永不小于 16px。**

---

## 4. 间距 · 圆角 · 阴影 · 容器

```css
:root {
  --s1: 4px;  --s2: 8px;  --s3: 12px; --s4: 16px;
  --s6: 24px; --s8: 32px; --s12: 48px; --s16: 64px;

  --r-sm: 3px;   /* 输入框、按钮 —— 编辑部式偏锐利 */
  --r-md: 6px;   /* 卡片、面板 */
  --r-pill: 999px;

  --shadow-1: 0 1px 2px rgba(30,30,20,.05);  /* 极克制，仅按钮/抬起项 */
  --container-tester: 640px;  /* tester 页阅读宽度 */
  --container-admin: 1240px;  /* admin 工作区宽度 */
}
```

**层级靠发丝线和留白，不靠阴影。** 阴影仅用于真正"浮起"的元素（主按钮 hover、下拉）。禁止给每张卡都加阴影。

---

## 5. 组件规范

### 5.1 按钮
- **主按钮**：`--accent` 底 + `--on-accent` 字，`padding: 11px 22px`，`--r-sm`，15px/600。hover → `--accent-hover`。
- **次按钮**：`--surface` 底 + `--line-strong` 边 + `--ink` 字。hover → `--surface-2`。
- **危险按钮**：`--risk` 底 + 白字。仅用于 reject。
- **文字按钮**：无底无边，`--accent` 字，hover 加下划线。
- 最小点击区 44×44px（移动端）。`:focus-visible` 必须有 2px `--accent` 描边。

### 5.2 输入框 / 文本域
- 边框 `1px --line-strong`，`--r-sm`，内边距 `10px 12px`，16px 字。
- `:focus` → 边框 `--accent` + 2px `--accent-tint` 外环。
- **标签永远可见**，在字段上方，15px/600。占位符（placeholder）只放示例提示，绝不当唯一标签。
- 文本域 `min-height: 96px`，`resize: vertical`，`line-height: 1.7`。

### 5.3 卡片 / 面板
- `--surface` 底，`1px --line` 边，`--r-md`，内边距 `--s6`。
- 区块标题用 h2（衬线）。标题与其内容的距离**必须小于**与上一区块的距离（防"标题悬浮"）。

### 5.4 状态 pill
- 圆角 `--r-pill`，`2px 10px`，12px/500，浅底 + 同色系深字 + 1px 同色边。
- `done/paid/ok` → `--ok-tint` / `--ok`；`pending/processing` → `--warn-tint` / `--warn`；`failed/rejected/risk` → `--risk-tint` / `--risk`；`na/中性` → `--surface-2` / `--ink-muted`。

### 5.5 表格（admin）
- 表头 `--surface-2` 底，12px/600 大写间距，`--ink-muted`。
- 行底 `1px --line` 分隔，行 hover → `--surface-2`。
- 数字列右对齐，金额用 `--font-mono` 或 tabular-nums 对齐。
- 行内可点：整行可点时给 hover 态 + 一个明确的"查看 →"列，不靠纯 hover 暴露。

### 5.6 进度条 / 漏斗（admin 看板核心）
- 细横条，高 6px，`--r-pill`，轨道 `--line`，已完成段 `--accent`。
- 漏斗分段（名额→已收→已确认→已付）用同一条上的分段色块 + 文字数值，不用饼图。

### 5.7 导航
- **tester 端**：无主导航。顶部只有 `Probe` 衬线字标（左上，圆形"P"徽标）。约定优先。
- **admin 端**：左侧深色固定侧栏（`--sidebar`），项："数据看板 / 反馈列表 / 招募工具 / 项目管理"。当前项 `--sidebar-active` 底 + 左侧 3px `--accent` 标记。底部放作者身份 + 退出。

### 5.8 品牌字标
`Probe` 用 `--font-display`，配一个圆形描边"P"徽标（`--accent` 描边）。落在每页左上。footer 用 micro 字号。

---

## 6. 交互状态（每个数据区块都要覆盖）

| 状态 | 规范 |
|---|---|
| **加载 loading** | stdlib 服务端渲染，整页直出，几乎无前端 loading。表单提交按钮点击后置灰 + 文案改"提交中…"防重复提交。 |
| **空 empty** | 必须有温度。例：admin 反馈列表 0 条 → 一句"还没有反馈。去『招募工具』生成邀请链接，发到微信群招 tester。"+ 一个指向招募工具的主按钮。绝不留裸表头。 |
| **错误 error** | 暖砖红 `--risk`，说人话 + 给下一步。表单校验错误显示在对应字段下方，不只顶部红条。 |
| **成功 success** | 收据页：大号衬线"反馈已提交"，明确告知"1-2 天内微信联系转账"，给反馈编号。admin 动作后回到详情页并高亮刚变更的状态。 |
| **部分 partial** | AI 评分 `processing`/`failed` 时，详情页该区块显示状态 pill + 占位文案 + 「重新分析」按钮，不显示空白。 |

---

## 7. 响应式

| 断点 | tester 端 | admin 端 |
|---|---|---|
| ≥ 1024px | 居中 640px 阅读列 | 侧栏 + 1240px 工作区 |
| 768–1023px | 居中，左右 `--s6` 边距 | 侧栏收窄为图标栏；表格保留 |
| < 768px | 单列，左右 `--s4` 边距，字标缩小 | 侧栏收为顶部抽屉（汉堡）；表格关键列优先，次要列折叠为详情行；金额/数值不挤压换行 |

移动端不是"桌面堆叠"——admin 表格在窄屏要主动选列，触控目标 ≥ 44px。

---

## 8. 无障碍（a11y）

- 所有交互元素键盘可达，`:focus-visible` 有 2px `--accent` 可见描边。
- admin 侧栏、主区用 `<nav>` / `<main>` 地标；表格用 `<th scope>`。
- 状态不只靠颜色：pill 同时有文字（"已确认""风险"）。
- 表单每个 `<label>` 用 `for` 绑定 `<input id>`；错误用 `aria-describedby` 关联。
- 正文对比度 ≥ 4.5:1，大字 ≥ 3:1。
- 链接的已访问/未访问态保留区分（admin 列表里"查看"链接）。

---

## 9. tester 端 vs admin 端的调性

同一套色彩与字体，两种气质：
- **tester 端**：全亮、奶油纸底、大量留白、衬线标题领衔。气质=被尊重、被认真对待。
- **admin 端**：深色侧栏锚定 + 亮色工作区。气质=指挥台、信息密但平静。
强调色、语义色、组件规范两端**完全一致**，保证是同一个产品。

---

## 10. 反 AI-slop 红线（违反即否决）

1. ❌ 紫/靛/蓝渐变底，蓝紫配色 → Probe 用奶油 + 墨绿。
2. ❌ 三栏"图标圆圈 + 标题 + 两行字"特性网格。
3. ❌ 彩色圆圈里塞图标当装饰。
4. ❌ 全部 `text-align:center`。标题左对齐。
5. ❌ 所有元素统一大圆角；Probe 偏锐利（3-6px）。
6. ❌ 装饰色块、漂浮圆、波浪 SVG 分割线。
7. ❌ emoji 当设计元素（✓ 这类极简符号在收据页可保留，但不滥用）。
8. ❌ 卡片左侧彩色竖边。
9. ❌ 套话文案（"欢迎来到…""释放…的力量"）。
10. ❌ `system-ui` / `-apple-system` 当**主**显示字体 —— 这是"放弃排版"信号。Probe 标题必须是真衬线。

---

## 变更记录

- **2026-05-16**：首次生成。源自 `/plan-design-review` mockup 变体 A（编辑部式衬线）。
  批准的 mockup：
  - `~/.gstack/projects/Deep-Thinks-probe/designs/probe-redesign-20260516/tester-form/variant-A.png`
  - `~/.gstack/projects/Deep-Thinks-probe/designs/probe-redesign-20260516/admin-dashboard/variant-A.png`
