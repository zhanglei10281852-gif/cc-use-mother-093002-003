import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.contracts import VersionInfo
from adaptive_exam.graph import build_graph


def graph():
    return build_graph(
        VersionInfo("G", "图谱", 1),
        [
            ("PY", "拼音", ()),
            ("VB", "词汇", ("PY",)),
            ("GB", "语法", ("VB",)),
            ("RB", "阅读", ("GB", "VB")),
        ],
    )


class GraphTests(unittest.TestCase):
    def test_rejects_cycle(self):
        with self.assertRaises(ValueError):
            build_graph(
                VersionInfo("G2", "有环图谱", 1),
                [("A", "甲", ("B",)), ("B", "乙", ("A",))],
            )

    def test_rejects_dangling_prerequisite(self):
        with self.assertRaises(ValueError):
            build_graph(VersionInfo("G3", "悬空", 1), [("A", "甲", ("X",))])

    def test_gap_propagates_through_prerequisite_chain(self):
        gaps = graph().propagate_gaps({"RB"})
        self.assertEqual(gaps["RB"], "direct")
        # 阅读是缺口 -> 语法、词汇、拼音全部推断为缺口
        self.assertEqual(gaps["GB"], "inferred")
        self.assertEqual(gaps["VB"], "inferred")
        self.assertEqual(gaps["PY"], "inferred")

    def test_direct_gap_overrides_inferred(self):
        gaps = graph().propagate_gaps({"RB", "PY"})
        self.assertEqual(gaps["PY"], "direct")
        self.assertEqual(gaps["GB"], "inferred")

    def test_gap_chain_explains_propagation_path(self):
        chains = graph().gap_chains({"RB"})
        # 推断缺口保留一条到直接缺口的解释链
        self.assertEqual(chains["PY"][0], "RB")
        self.assertEqual(chains["PY"][-1], "PY")
        self.assertIn("VB", chains["PY"])

    def test_unknown_gap_rejected(self):
        with self.assertRaises(ValueError):
            graph().propagate_gaps({"NOPE"})


if __name__ == "__main__":
    unittest.main()
