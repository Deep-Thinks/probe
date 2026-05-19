"""antifraud 纯函数单元测试。stdlib unittest，无第三方依赖。

运行：python3 -m unittest discover tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import antifraud  # noqa: E402


class TestNormalize(unittest.TestCase):
    def test_idempotent(self):
        for s in ["  Hello  World ", "abc", "多 行\n文 本", ""]:
            once = antifraud.normalize(s)
            self.assertEqual(once, antifraud.normalize(once))

    def test_case_fold(self):
        self.assertEqual(antifraud.normalize("ABC"), antifraud.normalize("abc"))

    def test_whitespace_collapse(self):
        self.assertEqual(antifraud.normalize("a   b\t\nc"), "a b c")

    def test_none(self):
        self.assertEqual(antifraud.normalize(None), "")


class TestContentHash(unittest.TestCase):
    def test_stable(self):
        self.assertEqual(antifraud.content_hash("hello"),
                         antifraud.content_hash("hello"))

    def test_normalized_equivalent(self):
        # 大小写 / 空白差异归一化后应得同哈希
        self.assertEqual(antifraud.content_hash("Hello  World"),
                         antifraud.content_hash("hello world"))

    def test_different_input_different_hash(self):
        self.assertNotEqual(antifraud.content_hash("aaa"),
                            antifraud.content_hash("bbb"))

    def test_hex_length(self):
        self.assertEqual(len(antifraud.content_hash("x")), 64)


class TestCombinedText(unittest.TestCase):
    def test_joins_all(self):
        t = antifraud.combined_text("a", "b", "c", "d", "e", ["f", "g"])
        for token in "abcdefg":
            self.assertIn(token, t)

    def test_no_custom(self):
        t = antifraud.combined_text("a", "b", "c", "d", "e", None)
        self.assertEqual(t, "a\nb\nc\nd\ne")


class TestTooFast(unittest.TestCase):
    def test_boundary(self):
        thr = antifraud.MIN_TASK_SECONDS
        self.assertTrue(antifraud.too_fast(thr - 1))
        self.assertFalse(antifraud.too_fast(thr))
        self.assertFalse(antifraud.too_fast(thr + 1))

    def test_none_and_negative(self):
        self.assertFalse(antifraud.too_fast(None))
        self.assertFalse(antifraud.too_fast(-5))

    def test_zero(self):
        # 0 秒提交极端快，应判定太快
        self.assertTrue(antifraud.too_fast(0))


class TestParseDedupResult(unittest.TestCase):
    def test_valid_match(self):
        self.assertEqual(
            antifraud.parse_dedup_result('{"duplicate_of": 3}', {1, 2, 3}), 3)

    def test_null(self):
        self.assertIsNone(
            antifraud.parse_dedup_result('{"duplicate_of": null}', {1, 2}))

    def test_id_not_in_candidates(self):
        # 幻觉 id：不在候选集 → 丢弃
        self.assertIsNone(
            antifraud.parse_dedup_result('{"duplicate_of": 99}', {1, 2, 3}))

    def test_malformed_json(self):
        self.assertIsNone(antifraud.parse_dedup_result("not json", {1}))

    def test_code_fence(self):
        self.assertEqual(
            antifraud.parse_dedup_result(
                '```json\n{"duplicate_of": 2}\n```', {2}), 2)

    def test_bool_rejected(self):
        # True 是 int 子类，但不是合法 feedback id
        self.assertIsNone(
            antifraud.parse_dedup_result('{"duplicate_of": true}', {1}))


class TestBuildDedupPrompt(unittest.TestCase):
    def test_contains_digests(self):
        p = antifraud.build_dedup_prompt("新摘要", [(1, "旧A"), (2, "旧B")])
        self.assertIn("新摘要", p)
        self.assertIn("1: 旧A", p)
        self.assertIn("2: 旧B", p)


if __name__ == "__main__":
    unittest.main()
