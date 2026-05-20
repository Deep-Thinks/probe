"""复活抽奖的核心数学单测（spec 2026-05-20 §9）。

- determinism：相同 (server_seed, client_seed, nonce) 三元组永远落同一 slot
- EV：HMAC 驱动的实际抽取，10k 次平均应接近闭式 EV（99.99%）
- 边界：CDF 单调递增 + 末位 = 1.0、slot ∈ [0, N_ROWS]

跑：python3 -m unittest tests.test_lottery_draw
"""
from __future__ import annotations

import os
import secrets
import sys
import unittest
from math import comb
from pathlib import Path

# 确保 import 路径正确（仓库根）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 测试不应触碰 prod DB；为静态属性测试切到 :memory:
os.environ.setdefault("PROBE_DB_PATH", ":memory:")

import server  # noqa: E402


class TestLotteryMath(unittest.TestCase):

    def test_cdf_invariants(self):
        # 末位严格 = 1.0（防止 bisect 错位）
        self.assertEqual(server._LOTTERY_CDF[-1], 1.0)
        # 严格单调递增
        for i in range(len(server._LOTTERY_CDF) - 1):
            self.assertLessEqual(server._LOTTERY_CDF[i],
                                 server._LOTTERY_CDF[i + 1])

    def test_multipliers_aligned_with_rows(self):
        self.assertEqual(len(server.LOTTERY_MULTIPLIERS),
                         server.LOTTERY_N_ROWS + 1)

    def test_closed_form_ev_near_100(self):
        n = server.LOTTERY_N_ROWS
        pmf = [comb(n, k) / 2 ** n for k in range(n + 1)]
        ev = sum(p * m for p, m in zip(pmf, server.LOTTERY_MULTIPLIERS))
        # 用户原话「1 金币 = 5 USD」=> 100%；允许 ±0.5% 偏差
        self.assertAlmostEqual(ev, 100.0, delta=0.5,
                               msg=f"PMF 闭式 EV 偏离 100%：{ev}")

    def test_draw_deterministic(self):
        ss = "a" * 64
        cs = "client-xyz"
        # 同三元组 100 次取必相同
        first = server.compute_lottery_slot(ss, cs, 42)
        for _ in range(100):
            self.assertEqual(server.compute_lottery_slot(ss, cs, 42), first)

    def test_slot_in_range(self):
        ss = "b" * 64
        for n in range(1000):
            s = server.compute_lottery_slot(ss, "z", n)
            self.assertGreaterEqual(s, 0)
            self.assertLessEqual(s, server.LOTTERY_N_ROWS)

    def test_empirical_ev_near_target(self):
        """HMAC 驱动的 10k 次抽样平均应在 100% ± 5% 内（容忍小样本噪声）。"""
        mults = []
        for n in range(10_000):
            ss = secrets.token_hex(32)
            s = server.compute_lottery_slot(ss, "seed", n)
            mults.append(server.LOTTERY_MULTIPLIERS[s])
        emp_ev = sum(mults) / len(mults)
        self.assertAlmostEqual(emp_ev, 100.0, delta=5.0,
                               msg=f"经验 EV 偏离过远：{emp_ev}")

    def test_usd_cents_arithmetic(self):
        """1 金币换 10 抽，单抽基准 50 cents：multiplier_pct × 50 / 100 = cents/抽。"""
        # USD_BASELINE_CENTS == 50（每 1 金币的 1/10）
        self.assertEqual(server.USD_BASELINE_CENTS, 50)
        self.assertEqual(server.DRAWS_PER_COIN, 10)
        # 100% × 50 / 100 = 50 cents = $0.50（基准单抽）
        self.assertEqual(100 * server.USD_BASELINE_CENTS // 100, 50)
        # 76% × 50 / 100 = 38 cents = $0.38（中心槽）
        self.assertEqual(76 * server.USD_BASELINE_CENTS // 100, 38)
        # 3500% (头奖) × 50 / 100 = 1750 cents = $17.50
        self.assertEqual(3500 * server.USD_BASELINE_CENTS // 100, 1750)
        # 1 金币 EV = 10 抽 × $0.50 = $5.00（与用户原话「1 金币 = 5 USD」对位）
        # 用闭式 EV 算一次：每抽 EV cents × 10 抽 应 ≈ 500 cents
        from math import comb
        n = server.LOTTERY_N_ROWS
        pmf = [comb(n, k) / 2 ** n for k in range(n + 1)]
        avg_mult = sum(p * m for p, m in zip(pmf, server.LOTTERY_MULTIPLIERS))
        ev_per_coin_cents = avg_mult * server.USD_BASELINE_CENTS / 100 * server.DRAWS_PER_COIN
        self.assertAlmostEqual(ev_per_coin_cents, 500.0, delta=5.0,
                               msg=f"1 金币 EV 偏离 $5.00：{ev_per_coin_cents/100:.4f} USD")


if __name__ == "__main__":
    unittest.main()
