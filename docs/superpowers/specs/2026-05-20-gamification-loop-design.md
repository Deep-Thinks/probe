# Probe 游戏化闭环设计（gamification loop）

> 由 brainstorming 于 2026-05-20 与用户对话产出。
> 用户授权跳过 writing-plans 阶段，直接开工改代码。本文作为"先 spec 后码"留痕，便于回溯设计意图。

---

## 1. 愿景

把 Probe 从"冷冰冰的反馈任务"变成"让人想再玩一次的游戏化闭环"。一个统一叙事：

```
认真填反馈 → AI 评分 → 金币爆出（剧场感）
  → 复活抽奖抓奖（每 1 金币一抽，0%-10000% 倍率）
  → 抓到的倍率 × 5 USD 自动投入「所有人帮助所有人」公益站额度池
  → 用户公开署名留痕，下一个人来玩感到「这里有人来过」
```

**核心叙事**：「所有人帮助所有人」。完全利他 —— 贡献者本人无回报（除了爽感、留名、和"我赢了"的瞬间）。

## 2. 范围 / 约束 / 反目标

**范围（v1 本次）**：
- 受 brainstorm 时间限制，本 spec 同时承担"设计 + 实施记录"两份职责
- 单一新流程：扣 1 → 抓复活抽奖 → 入池 + 留名
- 涉及：receipt 页融合入口、新 /revive 主场页、新 /revive/draw 抽奖 endpoint、2 张新表

**约束（来自项目硬基线）**：
- 后端：Python stdlib only（`hmac` / `hashlib` / `secrets` / `random`）
- 前端：vanilla HTML + 单 `style.css` + **内联 canvas + 内联 JS**（复活抽奖动画必需）
- 模板：`{{var}}` 纯替换，无循环表达式 → 列表在 Python 拼 HTML
- 文案 / 注释 / 日志全简体中文
- 所有用户输入走 `esc()`、admin POST 走 `_require_admin` + `_require_same_origin`

**反目标（明确 v1 不做）**：
- ❌ 鉴权 / 密码 / 账号系统（用户原话："不做密码、转账、鉴权"）
- ❌ 公益站额度的"消费/兑现"追踪（用户原话："你不用管具体是什么"）
- ❌ 服务端可重放的物理仿真（v1 用 Provably Fair 哈希链，前端动画是 cosmetic 重放）
- ❌ 防作弊深度（用户原话："完全利他没关系，公益站额度作者随时可以补"）
- ❌ 复杂的捐赠状态机（捐了就算捐了，不可撤回）

## 3. 用户流程

```
1. tester 提交反馈
   → 跳到 receipt 页
   → 显示「AI 正在估算你的金币...」骨架屏 + 自动 poll
   → 10-30s 后金币数字爆出（基于实测延迟，见 §6.2）
   → 显示「你赚到了 N 金币，要不要扣 1 复活公益站？」CTA

2. 点 CTA → /revive?fid=<feedback_id>&s=<session_id>
   → 服务端从 session/feedback 拿到 wechat_hash
   → 显示当前公益站额度池余量 + 最近复活者墙 + 复活抽奖抽奖板

3. 输入署名（默认占位「匿名好人 #N」，可自由文本，≤32 字符）
   → 点「扣 1，抓！」按钮
   → 后端 POST /revive/draw
     - 验 donation_balance ≥ 1
     - 生成 server_seed / client_seed / nonce（Provably Fair 三元组）
     - HMAC-SHA256 → 取前 8 字节 → /2^64 得 [0,1) 均匀分布
     - 反查 13 行二项分布 CDF → 得到 slot（0..13）
     - 写入 coin_donations 行（事务）
     - 返回 JSON：slot / multiplier / usd_cents / seed_reveal / pool_new_total

4. 前端 canvas 接收 slot → 播放复活抽奖动画（钢珠掉落 + 钉子弹跳 + 落槽）
   → 动画末尾"磁吸"到指定 slot
   → 显示倍率 × $5 = $X.XX 进入公益站
   → 公益站额度数字滚动累加

5. tester 可继续抽（如还有金币）or 离开
```

## 4. 数学：复活抽奖分布

**正本**：`scripts/calibrate_lottery.py` —— 校准脚本 + 文档化 EV / 分位数 / 桶分布 / 头奖频率。

**配置 v8（12 行二项分布抽奖板，13 槽位 + 1 金币 = 10 次抽奖）**：
```python
N_ROWS = 12
MULTIPLIERS = [3500, 690, 240, 124, 96, 85, 76, 85, 96, 124, 240, 690, 3500]
DRAWS_PER_COIN = 10        # 1 金币换 10 次抽奖（自愿）
USD_BASELINE_CENTS = 50    # 每抽基准 = $5 / 10 = $0.50 cents/抽
```

**用户约束**（2026-05-20 多轮迭代后定稿）：
- 1 金币 = 10 次抽奖额度（耦合，每抽 EV = $0.50；1 金币 EV = $5 USD）
- 头奖（≥1000%）至少 1/1000-1/2000 概率
- 最大倍率适度（3500%）
- **完全自愿**：金币不抽就周末微信原样转，抽不抽都行
- 不出现「复活抽奖」「抽奖板」等说法（用户原话「很不好」）

**统计性质**（200k 次蒙特卡洛 + 理论 PMF 闭式解双重验证）：
- 理论 EV = 100.03%（倍率口径）/ 1 金币 EV = $5.0013（与目标 $5 偏差 0.03%）
- 单抽 EV = $0.50 / max = $17.50（3500% jackpot）
- median 倍率 = 85% / 单抽 median 投入 = $0.43
- 85% 抽奖 ∈ [50%, 100%]、11% ∈ [100%, 200%]、3.2% ∈ [200%, 500%]、0.6% ∈ [500%, 1000%]
- 头奖（≥1000%）出现 ≈ 1/1739（双尾 2/4096，合用户约束）
- 一个 depth=4 反馈（12 金币 / 120 抽）期望投入公益站 $60、含头奖概率 5.67%

**为什么 EV=100% 仍强制大部分抽奖 <100%**：
范围 0%-3500% + EV=100% 是几率守恒约束。3500% 头奖必然稀有（双尾 2/4096），
否则期望破百。这条曲线把"愉悦感"通过 50 次反复抽来累积（每次 EV=5 USD，
50 次累计期望 250 USD，且头奖概率每回合 2.41%），单次抽奖的中位 85% 不再
是主要情绪锚点 —— 整体节奏才是。

**改 MULTIPLIERS 的纪律**：必须重跑 `python3 scripts/calibrate_lottery.py`，
理论 EV 偏离 100% 超 0.5% 会触发 `assert` 拒启动；头奖频率 < 1/2500 同样
触发 assert。仿真至少 100k 次。

## 5. Provably Fair：服务端权威 + 用户可验证

**威胁模型**：v1 dogfood，没有真金白银结算，用户篡改获利动机弱。但用 Provably Fair 标准化的好处是：未来若公益站对接真实兑现，无需重构。

**三元组**：
- `server_seed` — 服务端在抽奖时随机生成 32 字节 hex
- `client_seed` — 客户端浏览器生成或从 cookie 取
- `nonce` — `(wechat_hash, source_feedback_id)` 维度下递增整数

**slot 决定式**：
```python
import hmac, hashlib, bisect
from math import comb

# 二项分布 CDF（13 行 = 14 槽，启动期一次性算好）
PMF = [comb(13, k) / 2**13 for k in range(14)]
CDF = [sum(PMF[:i+1]) for i in range(14)]  # [≈0.0001, ..., 1.0]

def draw_slot(server_seed_hex: str, client_seed: str, nonce: int) -> int:
    digest = hmac.new(
        bytes.fromhex(server_seed_hex),
        f"{client_seed}:{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    rand_u = int.from_bytes(digest[:8], "big") / (1 << 64)  # uniform [0, 1)
    return bisect.bisect_left(CDF, rand_u)
```

**披露时机**：抽奖时立即返回 `server_seed`、`client_seed`、`nonce`、`server_seed_hash`。

**用户验证路径**：
- /revive 页底部「验证抽奖公平性」抽屉
- 给出公式 + 一段离线 Python 片段，用户 copy-paste 即可复算

## 6. 架构

### 6.1 数据库

新建两张表：

```sql
CREATE TABLE coin_donations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  wechat_hash TEXT NOT NULL,             -- 捐赠人耐久身份（同 feedback.wechat_hash）
  source_feedback_id INTEGER NOT NULL,   -- 这枚金币的源反馈
  donor_label TEXT NOT NULL,             -- 自由文本署名，最多 32 字符
  slot_landed INTEGER NOT NULL,          -- 0..13
  multiplier_pct INTEGER NOT NULL,       -- 倍率，整数百分点
  usd_cents INTEGER NOT NULL,            -- multiplier_pct × 5 = usd_cents
  server_seed TEXT NOT NULL,             -- 抽奖时披露的 32 hex 字符
  server_seed_hash TEXT NOT NULL,        -- SHA256(server_seed)
  client_seed TEXT NOT NULL,
  nonce INTEGER NOT NULL,
  created_at INTEGER NOT NULL,
  FOREIGN KEY (source_feedback_id) REFERENCES feedback(id),
  UNIQUE (wechat_hash, source_feedback_id, nonce)
);

CREATE INDEX idx_donations_recent ON coin_donations(created_at DESC);
CREATE INDEX idx_donations_donor ON coin_donations(wechat_hash);
```

**为什么不要 pool 表**：池子余额 = `SUM(usd_cents from coin_donations)`，无消费追踪（用户授权）。一行 SQL 聚合即可，没必要冗余。

**余额模型**（v8 重耦合：1 金币 = 10 次抽奖）：
- `earned_total` = AI 评过反馈的金币金额合计
- `draws_earned` = `earned_total × DRAWS_PER_COIN`（= earned × 10）
- `donated_count` = 已抽次数
- `draws_remaining` = `max(0, draws_earned - donated_count)`
- `consumed_coins` = `ceil(donated_count / DRAWS_PER_COIN)`（被抽奖锁定的整块金币）
- `withdrawable` = `max(0, confirmed_total - consumed_coins)`

**自愿语义（关键）**：tester 不抽就是不抽，金币会照常作为 confirmed → paid 周末微信
转账。一旦开始抽，每用完 10 次抽奖即吃掉 1 整块金币（这块不再可提现）。半截
（抽 1-9 次中断）的金币也按 1 整块计入消耗，因为抽奖是 commit 动作。

**信任语义**：tester 可以在 AI 评估完成后立即抽（不等作者 confirmed）。若作者后续
rejected 该反馈，已抽奖仍生效（贡献已进池子），作者自行承担差额。

### 6.2 服务端路由

| Method | Path | 职责 |
|---|---|---|
| GET  | `/revive` | 公益站主场页（可选参数 `?wh=` 或 `?fid=&s=`） |
| POST | `/revive/draw` | 执行一次抽奖（写库 + 返回 JSON） |
| GET  | `/revive/verify?d=<donation_id>` | 抽奖公平性验证页 |
| GET  | `/p/<slug>/receipt?...` | （更新）加入「扣 1 入口」block + AI 评估 polling |
| GET  | `/p/<slug>/eval_status?fid=` | 极简 polling endpoint：返回 AI 评估状态 JSON |

**前端 polling**：receipt 页 `setInterval` 每 3s 调 `/p/<slug>/eval_status`，状态变 `done` 时一次性渲染金币爆破动画 + 扣 1 CTA。

### 6.3 前端文件

| 新增 | 用途 |
|---|---|
| `templates/revive.html` | 公益站主场页（抽奖板 canvas + 池子余额 + 最近复活者墙 + 署名输入） |
| `templates/revive_verify.html` | 抽奖验证页（披露 seed + 提供离线复算片段） |

**修改**：
- `templates/receipt.html` —— AI 评估骨架屏 + 金币爆破 + 扣 1 CTA
- `static/style.css` —— 新增 `.lottery-board` / `.pool-counter` / `.donation-wall` / `.coin-burst` 等组件
- `server.py` —— 路由 + 抽奖逻辑（约 +300 行）
- `db.py` —— 新表 + 查询函数（约 +120 行）

### 6.4 复活抽奖 canvas 动画

**画布**：宽 360 / 高 480 / 设备像素比 2x（移动端清晰）
**抽奖板**：13 行 × 6 ~ 13 列（行 N 有 N+1 个钉位）+ 14 个底槽
**球物理**：
- 服务端拍板 `target_slot`，前端用 RAF（`requestAnimationFrame`）逐帧绘制
- 球 y 速度从重力（恒定加速）取
- 球 x 在每个钉子位置做"看似随机"的小幅左右偏移（视觉抖动）
- 最后 3 行用"软磁吸"把球 x 平滑导向 `target_slot` 列中心
- 落入底槽时小幅弹一下 + 标签亮起

**为什么不用 matter.js / three.js**：
- matter.js 真实物理 ≈ 80KB，不可控碰撞 → 服务端不能简单复现 slot
- three.js 是 3D，对 2D 复活抽奖过度设计
- 手写 canvas ≈ 200 行 JS，落槽完全可控，体积接近零

**视觉风格**：保持编辑部式衬线基调（DESIGN.md），抽奖板用深墨绿 + 暖砖红落槽高亮，球用暖琥珀。复活抽奖的"反差感"由动作（弹跳 / 数字爆破 / 池子滚动）提供，不靠霓虹色破坏整体调性。

## 7. 错误与降级

- 抽奖时 donation_balance ≤ 0 → 友好错误页「先去完成一个反馈再来」
- 网络中断 / draw API 失败 → 客户端不开始动画，显示「再试一次」按钮
- 同 nonce 重复提交 → UNIQUE 索引兜底，503 或 idempotent 返回已存在记录
- 浏览器禁 JS → canvas 不显示，但 fallback 显示「请开启 JavaScript 查看抽奖动画 / 或直接点提交按钮，结果以文字给出」（核心捐赠功能不依赖 JS）

## 8. 隐私

- `donor_label` 是用户自填自由文本，**不绑微信号、不绑 wechat_hash**（用户原话："爱写什么写什么，重名就重"）
- 内部用 `wechat_hash` 锚定身份（与现有金币系统一致）
- 30 天 wechat_id 隐私清理（purge_wechat.py）**不影响** coin_donations 表（捐赠记录里没存 wechat_id，只存 wechat_hash）

## 9. 测试与烟测

**自动测试**（v1 时间紧，仅核心数学）：
- `tests/test_lottery_draw.py` —— `draw_slot` 在固定 seed 下 deterministic
- `tests/test_lottery_draw.py` —— 100k 次 HMAC-CDF 抽取的 EV 与理论 PMF 一致
- `tests/test_coin_balance.py` —— donations 正确扣减 withdrawable

**手工烟测**（用户回来执行）：
```bash
PROBE_LLM_MOCK=1 PROBE_ADMIN_USER=admin PROBE_ADMIN_PASS=devpass \
  PROBE_BIND=127.0.0.1 python3 server.py
# 跑完整 tester → receipt → /revive → 抽奖 → 池子更新 闭环
```

## 10. 上线后留意

- 公益站额度池增长速度 vs 作者"补给"频率
- median 90% 是否真让用户感到"小亏" → 若 60% 用户首次抽完不再回访，重新评估分布
- 头奖（≥3000%，约 0.02%）出现频次。当前估算：每 5000 次抽奖出一个 → 项目活跃后约每周一次

## 11. 致谢

- 用户（@niuniu869）提供"扣 1 复活"概念 + 1 金币 = 5 USD 数学锚 + 「所有人帮助所有人」叙事
- Explore agent 提供 Provably Fair 哈希链方案参考
- The Coding Train 的 Plinko 教程为前端 canvas 实现提供思路（未直接复用代码）
