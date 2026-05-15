[根目录](../CLAUDE.md) > **projects**

# projects/ — 项目配置（git 管理的"最重要的一份代码"）

## 模块职责

存放所有 tester 可见的项目卡配置 JSON。**新增项目 = 在此目录加一份 `<slug>.json` + git push**，Zeabur 自动部署后服务启动期由 `project_loader.load_all()` 扫描、校验、`upsert` 到 SQLite 的 `projects` 表与 `invite_tokens` 表。

设计要点：
- **配置文件作为代码**：每次修改都走 git 评审（plan §"为什么不做 admin 建项目 UI"）
- **启动期校验失败 → 拒绝启动**：抛 `ProjectConfigError`，让 `server.main()` 退出而不是带病运行
- **两阶段加载**：Phase 1 全部解析+校验+跨项目 token 去重；Phase 2 全部通过后才 upsert DB，避免靠前文件已写、靠后失败导致部分应用

## 入口与启动

由 `server.main()` 在启动时调用：

```python
count = project_loader.load_all(PROJECTS_DIR)  # PROJECTS_DIR = Path(__file__).parent / "projects"
log.info("loaded %d projects", count)
```

详见 [`../project_loader.py`](../project_loader.py)。

## 配置 schema

```json
{
  "slug": "explorecipe-v2",                 // 必填：^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$
  "name": "项目名（中文）",                  // 必填：非空字符串
  "description": "≤100 字介绍",              // 必填：非空字符串
  "trial_url": "https://example.com/",       // 必填：必须以 http:// 或 https:// 开头
  "max_feedback_count": 30,                  // 必填：严格 int（拒 bool），1-100
  "custom_questions": ["可选自定义问题"],     // 可选：list[str]，≤2 条，每条非空
  "invite_tokens": ["token1", "token2"],     // 必填：非空字符串列表
  "single_use_tokens": false                 // 可选：严格 bool；true → 每个 token 只能用一次
}
```

### 校验规则汇总（`project_loader._validate`）

| 字段 | 类型 | 校验 |
|---|---|---|
| `slug` | str | 正则 `^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`（URL-safe，防路径穿越） |
| `name` | str | 非空 |
| `description` | str | 非空 |
| `trial_url` | str | 必须 `http://` 或 `https://` 开头（防 `javascript:` / `data:` / `file:` 注入 href） |
| `max_feedback_count` | int | **严格** int（`isinstance(x, bool)` 被显式拒），1-100 |
| `custom_questions` | list[str] | 可选；长度 ≤ 2；每条 `str` 且非空 |
| `invite_tokens` | list[str] | 非空；每条 `str` 且非空；**跨项目去重**（同 token 不能出现在两个项目） |
| `single_use_tokens` | bool | 可选；**严格** bool（`"false"` 字符串会被拒） |

跨项目 token 去重在 Phase 1 校验时通过 `seen_tokens: dict[str, str]` 实现；冲突直接抛错让启动失败。

## 对外接口

无 HTTP 接口（配置文件，不被网络访问）。被以下路径间接消费：

- 启动期：`project_loader.load_all` → `db.upsert_project` / `db.upsert_invite_token`
- 运行期：tester 通过 `/p/<slug>?t=<token>` 访问；服务从 `db.fetch_project(slug)` 和 `db.fetch_token(token)` 读取已 upsert 的副本
- 配置文件本身**不被运行时直接读**——上线后改 JSON 必须**重启服务**才生效

## 关键依赖与配置

- **运行时数据库副本**：`projects` 表（slug PK + reserved_count）+ `invite_tokens` 表（token PK + project_slug FK）
- **upsert 不会减字段**：删除 `projects/*.json` 文件**不会**从 DB 删项目（v1 假设：项目只新增不删除；下线项目靠 `max_feedback_count` 收满自然结束）
- **token 变更**：移除某 token 后，DB 里的 `invite_tokens` 行仍在；要彻底失效需手工 `DELETE FROM invite_tokens WHERE token=?`
- **改 `max_feedback_count`**：upsert 会覆盖；但**正在运行的 reserved_count 不会清零**，把上限改小到 ≤ reserved_count 会让新 tester 看到"满员"页

## 数据模型

参考 [`../db.py`](../db.py) 的 `SCHEMA_SQL`：

- `projects` 表：`slug PK NOT NULL`、`name/description/trial_url NOT NULL`、`max_feedback_count NOT NULL`、`custom_questions_json TEXT NULL`、`reserved_count INTEGER DEFAULT 0`
- `invite_tokens` 表：`token PK`、`project_slug FK → projects(slug)`、`is_single_use INT DEFAULT 0`、`consumed_by_session/consumed_at` 一次性消费记录、**复合 UNIQUE `(token, project_slug)`** 用于下游 `sessions` 复合外键

## 测试与质量

- 无自动化测试。
- 已有的"事实上单元测试"：`project_loader._validate` 的每条 raise 路径都对应一种攻击/误用：
  - slug 含 `/` → 路径穿越
  - trial_url = `javascript:alert(...)` → XSS
  - `max_feedback_count = True` → bool 当 int 的隐式陷阱
  - `single_use_tokens = "false"` → truthy 字符串
  - 两个项目声明同一 token → 访问控制错乱
- 建议 v2：把这些反例固化为 `tests/test_project_loader.py`。

## 现有项目清单

| 文件 | slug | 名额 | 自定义题 | tokens |
|---|---|---|---|---|
| `explorecipe-v2.json` | `explorecipe-v2` | 30 | 1 条 | `seed-wxgroup-001`, `seed-wxgroup-002`（非一次性） |

## 常见问题 (FAQ)

**Q: 怎么加新项目？**
A:
1. 在 `projects/` 加 `<your-slug>.json`，按 schema 填字段
2. `python3 server.py` 验证启动期校验通过（mock 模式 `PROBE_LLM_MOCK=1`）
3. `git add projects/<your-slug>.json && git commit && git push`
4. Zeabur 自动部署

**Q: 怎么暂停一个项目（停止接受新反馈）？**
A: v1 没有"暂停"开关。可以把 `max_feedback_count` 改成当前 `reserved_count` 即可让卡片显示"满员"。

**Q: 一次性 token 怎么用？**
A: 设 `"single_use_tokens": true`，并预生成大量 tokens（例如 `["t-001", "t-002", ...]`）。每个 token 在 `submit_feedback` 内被原子消费一次（`UPDATE invite_tokens ... WHERE consumed_by_session IS NULL`）。

**Q: 删 JSON 文件会怎样？**
A: DB 里的 project 行仍在，链接仍然可访问。要真正下线需手工 SQL 删除 + restart。

## 相关文件清单

- `explorecipe-v2.json`（当前唯一项目）
- 加载逻辑：`/niuniu869_dev/probe/project_loader.py`
- 数据库 schema：`/niuniu869_dev/probe/db.py::SCHEMA_SQL`
- tester 路由：`/niuniu869_dev/probe/server.py::_handle_project_card`

## 变更记录 (Changelog)

- **2026-05-16 02:07:03**：首次生成模块文档；当前仅 1 个项目（explorecipe-v2，30 名额）。
