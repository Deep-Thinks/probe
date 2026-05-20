"""柏青哥抽奖的核心数学单测（spec 2026-05-20 §9）。

- determinism：相同 (server_seed, client_seed, nonce) 三元组永远落同一 slot
- EV：HMAC 驱动的实际抽取，10k 次平均应接近闭式 EV（99.99%）
- 边界：CDF 单调递增 + 末位 = 1.0、slot ∈ [0, N_ROWS]

跑：python3 -m unittest tests.test_pachinko_draw
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


class TestPachinkoMath(unittest.TestCase):

    def test_cdf_invariants(self):
        # 末位严格 = 1.0（防止 bisect 错位）
        self.assertEqual(server._PACHINKO_CDF[-1], 1.0)
        # 严格单调递增
        for i in range(len(server._PACHINKO_CDF) - 1):
            self.assertLessEqual(server._PACHINKO_CDF[i],
                                 server._PACHINKO_CDF[i + 1])

    def test_multipliers_aligned_with_rows(self):
        self.assertEqual(len(server.PACHINKO_MULTIPLIERS),
                         server.PACHINKO_N_ROWS + 1)

    def test_closed_form_ev_near_100(self):
        n = server.PACHINKO_N_ROWS
        pmf = [comb(n, k) / 2 ** n for k in range(n + 1)]
        ev = sum(p * m for p, m in zip(pmf, server.PACHINKO_MULTIPLIERS))
        # 用户原话「1 金币 = 5 USD」=> 100%；允许 ±0.5% 偏差
        self.assertAlmostEqual(ev, 100.0, delta=0.5,
                               msg=f"PMF 闭式 EV 偏离 100%：{ev}")

    def test_draw_deterministic(self):
        ss = "a" * 64
        cs = "client-xyz"
        # 同三元组 100 次取必相同
        first = server.pachinko_draw_slot(ss, cs, 42)
        for _ in range(100):
            self.assertEqual(server.pachinko_draw_slot(ss, cs, 42), first)

    def test_slot_in_range(self):
        ss = "b" * 64
        for n in range(1000):
            s = server.pachinko_draw_slot(ss, "z", n)
            self.assertGreaterEqual(s, 0)
            self.assertLessEqual(s, server.PACHINKO_N_ROWS)

    def test_empirical_ev_near_target(self):
        """HMAC 驱动的 10k 次抽样平均应在 100% ± 5% 内（容忍小样本噪声）。"""
        mults = []
        for n in range(10_000):
            ss = secrets.token_hex(32)
            s = server.pachinko_draw_slot(ss, "seed", n)
            mults.append(server.PACHINKO_MULTIPLIERS[s])
        emp_ev = sum(mults) / len(mults)
        self.assertAlmostEqual(emp_ev, 100.0, delta=5.0,
                               msg=f"经验 EV 偏离过远：{emp_ev}")

    def test_usd_cents_arithmetic(self):
        """multiplier_pct × 5 USD baseline = usd_cents（整数运算）。"""
        # 90% × 500 / 100 = 450 (= $4.50)
        self.assertEqual(90 * server.USD_BASELINE_CENTS // 100, 450)
        # 4073% (头奖) × 5 = 20365 cents = $203.65
        self.assertEqual(4073 * server.USD_BASELINE_CENTS // 100, 20365)
        # 81% × 5 = 405 cents = $4.05
        self.assertEqual(81 * server.USD_BASELINE_CENTS // 100, 405)


if __name__ == "__main__":
    unittest.main()
