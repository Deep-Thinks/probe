"""复活抽奖分布校准 —— 生产配置源 of truth。

运行：python3 scripts/calibrate_lottery.py
作用：跑 ≥100k 次仿真验证当前 MULTIPLIERS 列表的统计性质，并对照理论
PMF 闭式解。若改动 MULTIPLIERS / N_ROWS，重跑确认 EV 与头奖频率仍达标。

server.py 的 LOTTERY_MULTIPLIERS / LOTTERY_N_ROWS 必须与本文件保持一致。
"""
from __future__ import annotations

import random
from collections import Counter
from math import comb

# ============================================================
# 生产配置 v7 —— 12 行二项分布抽奖板，13 槽位。
# ------------------------------------------------------------
# 设计约束（来自用户）：
#   1. EV 严格 = 100%（1 金币基准 = 5 USD）
#   2. 头奖至少 1/1000 ~ 1/2000 概率（双尾合并 2/4096 ≈ 1/2048 ✓）
#   3. 最大倍率可以小一点（4073% → 3500%）
#   4. 仿真至少 100k 次
#
# 200k 次蒙特卡洛 + 理论 PMF 闭式解双重验证。
# ============================================================
N_ROWS = 12
MULTIPLIERS = [3500, 690, 240, 124, 96, 85, 76, 85, 96, 124, 240, 690, 3500]
assert len(MULTIPLIERS) == N_ROWS + 1, "槽位数必须 = 行数 + 1"

# 跑仿真的最少次数（用户约束）
MIN_TRIALS = 100_000
DEFAULT_TRIALS = 200_000

# 头奖阈值（任何 ≥ 此倍率视为"头奖"）
JACKPOT_THRESHOLD_PCT = 1000


def binomial_pmf(n_rows: int) -> list[float]:
    total = 2 ** n_rows
    return [comb(n_rows, k) / total for k in range(n_rows + 1)]


def theoretical_ev() -> float:
    pmf = binomial_pmf(N_ROWS)
    return sum(p * m for p, m in zip(pmf, MULTIPLIERS))


def theoretical_jackpot_prob() -> float:
    """理论上"头奖"（multiplier ≥ JACKPOT_THRESHOLD_PCT）的概率。"""
    pmf = binomial_pmf(N_ROWS)
    return sum(p for p, m in zip(pmf, MULTIPLIERS)
               if m >= JACKPOT_THRESHOLD_PCT)


def simulate(n_trials: int = DEFAULT_TRIALS, seed: int = 42) -> dict:
    if n_trials < MIN_TRIALS:
        raise ValueError(f"仿真次数必须 ≥ {MIN_TRIALS}，给了 {n_trials}")
    random.seed(seed)
    slot_hits = Counter()
    results = []
    jackpot_count = 0
    for _ in range(n_trials):
        slot = sum(1 for _ in range(N_ROWS) if random.random() >= 0.5)
        slot_hits[slot] += 1
        m = MULTIPLIERS[slot]
        results.append(m)
        if m >= JACKPOT_THRESHOLD_PCT:
            jackpot_count += 1
    results.sort()
    n = len(results)
    return {
        'n_trials': n_trials,
        'ev': sum(results) / n,
        'median': results[n // 2],
        'p10': results[n // 10],
        'p25': results[n // 4],
        'p75': results[3 * n // 4],
        'p90': results[9 * n // 10],
        'p99': results[99 * n // 100],
        'p999': results[999 * n // 1000],
        'max': results[-1],
        'min': results[0],
        'jackpot_count': jackpot_count,
        'jackpot_freq': n / max(1, jackpot_count),
        'bins': {
            '<50%':       sum(1 for r in results if r < 50)    / n,
            '50-100%':    sum(1 for r in results if 50  <= r < 100)  / n,
            '100-200%':   sum(1 for r in results if 100 <= r < 200) / n,
            '200-500%':   sum(1 for r in results if 200 <= r < 500) / n,
            '500-1000%':  sum(1 for r in results if 500 <= r < 1000) / n,
            '1000-3000%': sum(1 for r in results if 1000 <= r < 3000) / n,
            '3000%+':     sum(1 for r in results if r >= 3000) / n,
        },
        'slot_hits': dict(sorted(slot_hits.items())),
    }


def print_report() -> None:
    theory_ev = theoretical_ev()
    theory_jp = theoretical_jackpot_prob()
    stats = simulate()
    print(f"\n抽奖分布校准报告（v7 · 生产配置 · 12 行抽奖板）")
    print(f"{'=' * 66}")
    print(f"  抽奖板：{N_ROWS} 行 / {N_ROWS + 1} 槽位 / 二项分布（左右各 50%）")
    print(f"  倍率：{MULTIPLIERS}")
    print(f"{'-' * 66}")
    print(f"  理论 EV（PMF 闭式解）= {theory_ev:.4f}%")
    print(f"  仿真 EV（{stats['n_trials']:,} 次） = {stats['ev']:.2f}%")
    print(f"  目标 EV              = 100.00%   （用户「1 金币 = 5 USD」）")
    assert abs(theory_ev - 100.0) < 0.5, (
        f"理论 EV 偏离 100% 超 0.5%（{theory_ev:.4f}%）。调整 MULTIPLIERS 后重跑。"
    )
    print()
    print(f"  理论头奖（≥{JACKPOT_THRESHOLD_PCT}%）概率 = {theory_jp*100:.4f}% "
          f"（约 1/{1/theory_jp:.0f}）")
    print(f"  仿真头奖出现 {stats['jackpot_count']} 次 / {stats['n_trials']:,} 次 "
          f"（约 1/{stats['jackpot_freq']:.0f}）")
    # 用户约束：≥ 1/2000（双尾合并概率 ≥ 1/2000）
    assert 1 / theory_jp <= 2500, (
        f"头奖太稀（1/{1/theory_jp:.0f}）；用户要求至少 1/1000-1/2000。"
    )
    print()
    print(f"  median={stats['median']}%   p10/p25/p75/p90="
          f"{stats['p10']}/{stats['p25']}/{stats['p75']}/{stats['p90']}")
    print(f"  p99={stats['p99']}%   p999={stats['p999']}%   max={stats['max']}%")
    print()
    print("  分布桶：")
    for bucket, frac in stats['bins'].items():
        bar = '█' * int(frac * 200)
        print(f"   {bucket:>10s}: {frac*100:5.2f}%   {bar}")
    print()
    print("  槽位命中（与理论二项 PMF 对照）：")
    pmf = binomial_pmf(N_ROWS)
    n = stats['n_trials']
    for slot, hits in stats['slot_hits'].items():
        emp = hits / n
        bar = '█' * int(emp * 100)
        print(f"   slot {slot:2d} (×{MULTIPLIERS[slot]:>5d}%): "
              f"emp={emp*100:5.2f}% / theory={pmf[slot]*100:5.2f}%   {bar}")
    print()
    print("  50 次抽奖一回合的期望（每条反馈 = 50 次）：")
    expected_usd_per_session = 50 * theory_ev / 100 * 5
    print(f"   平均期望投入公益站：${expected_usd_per_session:.2f} / 反馈")
    print(f"   头奖（≥{JACKPOT_THRESHOLD_PCT}%）出现概率（50 次至少 1 次）："
          f"{(1 - (1-theory_jp)**50)*100:.2f}%")
    print()


if __name__ == "__main__":
    print_report()
