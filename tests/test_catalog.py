import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from adaptive_exam.bank import build_bank
from adaptive_exam.catalog import VersionCatalog, CatalogError, load_catalog
from adaptive_exam.contracts import VersionInfo
from adaptive_exam.graph import build_graph
from adaptive_exam.rules import default_rules
from adaptive_exam.versioning import QualifiedVersionId

SEED = Path(__file__).parents[1] / "examples" / "catalog_seed.json"


def graph_only_catalog():
    catalog = VersionCatalog()
    catalog.register_graph(
        build_graph(VersionInfo("G", "图谱", 1), [("S", "技能", ())])
    )
    return catalog


class CatalogTests(unittest.TestCase):
    def test_bank_cannot_cover_unknown_skill(self):
        catalog = graph_only_catalog()
        bank = build_bank(
            VersionInfo("B", "题库", 1),
            [("q", "题", "答", ("NOPE",), 0.5, None, 0)],
        )
        with self.assertRaises(ValueError):
            catalog.register_bank(bank, "G:1")

    def test_duplicate_version_is_rejected(self):
        catalog = graph_only_catalog()
        with self.assertRaises(ValueError):
            catalog.register_graph(
                build_graph(VersionInfo("G", "图谱", 1), [("X", "x", ())])
            )

    def test_unknown_version_raises_catalog_error(self):
        catalog = VersionCatalog()
        with self.assertRaises(CatalogError):
            catalog.require_bank("GHOST:9")

    def test_content_hash_is_stable_across_loads(self):
        first = load_catalog(SEED)
        second = load_catalog(SEED)
        self.assertEqual(
            first.banks["BANK-CN:1"].content_hash,
            second.banks["BANK-CN:1"].content_hash,
        )
        self.assertEqual(
            first.graphs["GRAPH-CN:1"].content_hash,
            second.graphs["GRAPH-CN:1"].content_hash,
        )
        self.assertEqual(
            first.rules["RULES-CN:1"].content_hash,
            second.rules["RULES-CN:1"].content_hash,
        )

    def test_revisions_coexist_and_have_distinct_hashes(self):
        catalog = graph_only_catalog()
        b1 = build_bank(
            VersionInfo("B", "题库", 1), [("q", "旧题", "旧答", ("S",), 0.1, None, 0)]
        )
        b2 = build_bank(
            VersionInfo("B", "题库", 2), [("q", "新题", "新答", ("S",), 0.9, None, 0)]
        )
        catalog.register_bank(b1, "G:1")
        catalog.register_bank(b2, "G:1")
        self.assertIn("B:1", catalog.banks)
        self.assertIn("B:2", catalog.banks)
        self.assertNotEqual(b1.content_hash, b2.content_hash)
        catalog.register_rules(default_rules(VersionInfo("R", "规则", 1)))
        bundle = catalog.bundle("G:1", "B:1", "R:1")
        self.assertEqual(bundle.as_reference()["bank_qvid"], "B:1")

    def test_qualified_version_id_roundtrip(self):
        q = QualifiedVersionId.parse("BANK-CN:7")
        self.assertEqual((q.entity_id, q.revision), ("BANK-CN", 7))
        self.assertEqual(str(q), "BANK-CN:7")
        with self.assertRaises(ValueError):
            QualifiedVersionId.parse("无冒号")


if __name__ == "__main__":
    unittest.main()
