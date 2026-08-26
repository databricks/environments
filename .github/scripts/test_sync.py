"""Unit tests for sync.py page-parsing (run: python -m unittest test_sync)."""
import unittest

from sync import dbr_meta, dbr_scalas

# Minimal fragments mirroring the runtime pages' System environment list. The real
# pages are ~200 KB; only the shape dbr_scalas keys off is reproduced here.
SINGLE = (
    "<title>Databricks Runtime 17.3 LTS | Databricks on AWS</title>"
    "<li><strong>Java</strong>: Zulu17.54+21-CA</li>"
    "<li><strong>Scala</strong>: 2.13.16</li>"
    "<li><strong>Python</strong>: 3.12.3</li>"
)
# A dual-image release renders the two Scala versions in one field, joined by
# '<strong>or</strong>', each with a patch segment.
DUAL = (
    "<title>Databricks Runtime 16.4 LTS | Databricks on AWS</title>"
    "<li><strong>Java</strong>: Zulu17.54+21-CA</li>"
    "<li><strong>Scala</strong>: 2.12.15 <strong>or</strong> 2.13.10</li>"
    "<li><strong>Python</strong>: 3.12.3</li>"
)


class DbrScalasTest(unittest.TestCase):
    def test_single_scala(self):
        self.assertEqual(dbr_scalas(SINGLE), ["2.13"])

    def test_dual_scala_in_page_order(self):
        # Both images off one page; patch segments (.15/.10) must not leak in as
        # extra entries, and order follows the page.
        self.assertEqual(dbr_scalas(DUAL), ["2.12", "2.13"])

    def test_no_scala_field(self):
        self.assertEqual(dbr_scalas("<title>Databricks Runtime 19</title>"), [])

    def test_falls_back_when_list_item_not_closed(self):
        # If the field isn't delimited by </li> as expected, degrade to a single
        # match rather than to nothing.
        self.assertEqual(dbr_scalas("<strong>Scala</strong>: 2.13.16"), ["2.13"])


class DbrMetaTest(unittest.TestCase):
    def test_dual_variant_yields_both_scalas(self):
        key_ver, dbconnect_ver, scalas, python_version = dbr_meta(DUAL)
        self.assertEqual(key_ver, "16.4")
        self.assertEqual(dbconnect_ver, "16.4")
        self.assertEqual(scalas, ["2.12", "2.13"])
        self.assertEqual(python_version, "3.12.3")

    def test_single_variant(self):
        self.assertEqual(dbr_meta(SINGLE)[2], ["2.13"])

    def test_none_when_scala_missing(self):
        self.assertIsNone(dbr_meta("<title>Databricks Runtime 19</title>"))


if __name__ == "__main__":
    unittest.main()
