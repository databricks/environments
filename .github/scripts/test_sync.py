"""Unit tests for sync.py page-parsing (run: python -m unittest test_sync)."""
import unittest
from unittest import mock

import sync
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

    def test_ignores_version_like_numbers_inside_tags(self):
        # A version-like number that lives only inside a tag (an href/attribute, not
        # the visible text) must not be read as a Scala version and spawn a bogus
        # environment. Here the visible value is 2.13.16; the '3.5' is only in a link.
        html = (
            "<li><strong>Scala</strong>: 2.13.16 "
            '<a href="https://spark.apache.org/docs/3.5/">docs</a></li>'
        )
        self.assertEqual(dbr_scalas(html), ["2.13"])

    def test_ignores_trailing_annotation(self):
        # Only the leading 'X or Y' enumeration is the Scala version list; a trailing
        # visible annotation (e.g. the Spark version) must not contribute a version.
        html = "<li><strong>Scala</strong>: 2.12.15 or 2.13.10 (Apache Spark 3.5)</li>"
        self.assertEqual(dbr_scalas(html), ["2.12", "2.13"])

    def test_dual_scala_alternate_separators(self):
        # The two versions must be picked up regardless of the delimiter the page uses
        # -- not only ' or '. A narrower match silently drops the second variant and
        # resurrects the 404 this fix exists to prevent.
        for sep in (" or ", " and ", ", ", ",", " / ", "/", "&nbsp;or&nbsp;"):
            html = f"<li><strong>Scala</strong>: 2.12.15{sep}2.13.10</li>"
            self.assertEqual(dbr_scalas(html), ["2.12", "2.13"], repr(sep))

    def test_alternate_separator_still_stops_at_annotation(self):
        # Widening the separator must not start swallowing trailing annotations.
        html = "<li><strong>Scala</strong>: 2.12.15, 2.13.10, see Spark 3.5 notes</li>"
        self.assertEqual(dbr_scalas(html), ["2.12", "2.13"])


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


def _titled_page(title):
    """Minimal page carrying only a <title> — enough for is_eos / dbr_point_releases,
    which key off the title and never parse the body of a probe page."""
    return f"<title>{title} | Databricks on AWS</title>"


def _runtime_page(title, scala="2.13", python="3.12.3", pkgs=(("numpy", "2.1.3"),)):
    """A standard runtime page with the fields dbr_meta / parse_dbr_page read: a
    <title>, a System-environment Scala/Python list, and an Installed Python libraries
    table."""
    rows = "".join(f"<td><p>{n}<td><p>{v}" for n, v in pkgs)
    return (
        f"<title>{title} | Databricks on AWS</title>"
        f"<li><strong>Scala</strong>: {scala}.16</li>"
        f"<li><strong>Python</strong>: {python}</li>"
        '<h2 id="installed-python-libraries">Installed Python libraries</h2>'
        f"<table>{rows}</table>"
    )


def _ml_page(pkgs=(("numpy", "2.1.3"),)):
    """An ML runtime page rendering the CPU and GPU library tables inline."""
    rows = "".join(f"<td><p>{n}<td><p>{v}" for n, v in pkgs)
    return (
        '<h2 id="python-libraries-on-cpu-clusters">CPU</h2>'
        f"<table>{rows}</table>"
        '<h2 id="python-libraries-on-gpu-clusters">GPU</h2>'
        f"<table>{rows}</table>"
    )


def _slug_of(url):
    """The runtime slug (last path segment) of a DBR_PAGE URL."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _fake_fetch(pages):
    """A fetch() replacement serving `pages` (slug -> html); KeyError on a miss so a
    test that requests an unexpected page fails loudly rather than silently."""
    return lambda url: pages[_slug_of(url)]


def _fake_fetch_opt(pages):
    """A fetch_opt() replacement: html for a known slug, None (a 404) otherwise."""
    return lambda url: pages.get(_slug_of(url))


class UmbrellaMajorTest(unittest.TestCase):
    def test_bare_major_lines_are_umbrella(self):
        # DBR 18+ index slugs are the bare major; clusters address the line by that
        # bare major in spark_version, so its folder is keyed by the major.
        self.assertEqual(sync._umbrella_major("18"), "18")
        self.assertEqual(sync._umbrella_major("18ml"), "18")
        self.assertEqual(sync._umbrella_major("19"), "19")
        self.assertEqual(sync._umbrella_major("19ml"), "19")

    def test_pre18_lts_lines_are_not_umbrella(self):
        # Pre-18 lines keep the old scheme: the minor is read from the page title, so
        # they are not umbrella-keyed.
        self.assertIsNone(sync._umbrella_major("17.3lts"))
        self.assertIsNone(sync._umbrella_major("17.3lts-ml"))
        self.assertIsNone(sync._umbrella_major("16.4lts"))


class DbrPointReleasesTest(unittest.TestCase):
    def test_umbrella_returns_all_live_point_releases_plus_latest_as_umbrella(self):
        # DBR 18: 18.0 EoS (skipped), 18.1 and 18.2 live. Both live point releases get their
        # own folder, and the latest (18.2) also sources the bare-major umbrella — clusters
        # address the line by either form depending on client version, so we publish both.
        pages = {
            "18.0": _titled_page("Databricks Runtime 18.0 (EoS)"),
            "18.1": _titled_page("Databricks Runtime 18.1"),
            "18.2": _titled_page("Databricks Runtime 18.2"),
        }
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.1", "18.2"], "18.2"))

    def test_umbrella_ml_returns_all_live_point_releases_plus_latest(self):
        pages = {
            "18.1ml": _titled_page("Databricks Runtime 18.1 ML"),
            "18.2ml": _titled_page("Databricks Runtime 18.2 ML"),
        }
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(
                sync.dbr_point_releases("18ml", ml=True), (["18.1ml", "18.2ml"], "18.2ml")
            )

    def test_umbrella_falls_back_to_umbrella_page_when_no_point_releases(self):
        # DBR 19 today: no point-release pages, so there are no per-minor folders and the
        # umbrella is taken from the umbrella page itself.
        pages = {"19": _titled_page("Databricks Runtime 19")}
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("19"), ([], "19"))

    def test_pre18_returns_index_slug_and_no_umbrella(self):
        pages = {"17.3lts": _titled_page("Databricks Runtime 17.3 LTS")}
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("17.3lts"), (["17.3lts"], None))

    def test_umbrella_keeps_point_releases_but_skips_umbrella_when_newest_indeterminate(self):
        # A transient error on a release NEWER than the newest confirmed live one leaves the
        # true latest unknown. Keep the confirmed point-release folder(s), but skip the
        # umbrella (umbrella_slug=None) so a flaky run can't downgrade the folder that a
        # bare-major spark_version serves.
        def fetch_opt(url):
            slug = _slug_of(url)
            if slug == "18.2":
                raise TimeoutError("transient")
            return {
                "18.0": _titled_page("Databricks Runtime 18.0 (EoS)"),
                "18.1": _titled_page("Databricks Runtime 18.1"),
            }.get(slug)

        with mock.patch.object(sync, "fetch_opt", fetch_opt):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.1"], None))

    def test_umbrella_probes_beyond_a_low_minor_cap(self):
        # A long-lived line accrues many point releases; the trailing-404 break — not an
        # arbitrary low minor cap — ends discovery, so every live one is returned and the
        # umbrella tracks the true latest (18.12).
        pages = {f"18.{m}": _titled_page(f"Databricks Runtime 18.{m}") for m in range(13)}
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(
                sync.dbr_point_releases("18"), ([f"18.{m}" for m in range(13)], "18.12")
            )

    def test_umbrella_sole_live_release_is_both_point_release_and_umbrella(self):
        pages = {"18.0": _titled_page("Databricks Runtime 18.0")}
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.0"], "18.0"))

    def test_umbrella_publishes_nothing_for_a_fully_eos_line(self):
        # Every point release is EoS (retired), even though the umbrella page isn't marked
        # EoS yet: publish nothing — neither point-release folders nor the umbrella.
        pages = {
            "18.0": _titled_page("Databricks Runtime 18.0 (EoS)"),
            "18.1": _titled_page("Databricks Runtime 18.1 (EoS)"),
            "18": _titled_page("Databricks Runtime 18 LTS"),  # umbrella not yet EoS
        }
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), ([], None))

    def test_umbrella_probes_past_leading_404s_to_a_later_live_release(self):
        # If the earliest point-release pages are genuinely absent (e.g. removed), the two
        # consecutive 404s must NOT be read as end-of-list before any page is found — a
        # later live release (18.2/18.3) must still be discovered. Mirrors the "require at
        # least one found before honoring the break" guard in discover_serverless.
        pages = {
            "18.2": _titled_page("Databricks Runtime 18.2"),
            "18.3": _titled_page("Databricks Runtime 18.3"),
        }
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.2", "18.3"], "18.3"))

    def test_umbrella_probes_past_eos_point_releases_to_the_live_latest(self):
        # Consecutive EoS releases must not stop the probe before a later live one.
        pages = {
            "18.0": _titled_page("Databricks Runtime 18.0 (EoS)"),
            "18.1": _titled_page("Databricks Runtime 18.1 (EoS)"),
            "18.2": _titled_page("Databricks Runtime 18.2"),
        }
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.2"], "18.2"))

    def test_umbrella_publishes_nothing_when_none_live_and_a_probe_indeterminate(self):
        # No release confirmed live, but a probe hit a transient error — we can't be sure the
        # line has no point release, so don't fall back to the umbrella page (wrong metadata).
        def fetch_opt(url):
            slug = _slug_of(url)
            if slug == "20.0":
                raise TimeoutError("transient")
            return {"20": _titled_page("Databricks Runtime 20 LTS")}.get(slug)

        with mock.patch.object(sync, "fetch_opt", fetch_opt):
            self.assertEqual(sync.dbr_point_releases("20"), ([], None))

    def test_umbrella_ignores_transient_below_the_latest_live_release(self):
        # A transient error OLDER than the confirmed latest can't change which is newest; the
        # umbrella still comes from the latest and the confirmed point releases are returned.
        def fetch_opt(url):
            slug = _slug_of(url)
            if slug == "18.1":
                raise TimeoutError("transient")
            return {
                "18.0": _titled_page("Databricks Runtime 18.0"),
                "18.2": _titled_page("Databricks Runtime 18.2"),
            }.get(slug)

        with mock.patch.object(sync, "fetch_opt", fetch_opt):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.0", "18.2"], "18.2"))

    def test_umbrella_only_line_bounds_probes_to_a_leading_miss_cap(self):
        # A bare major with NO point-release pages (DBR 19 today) must not probe the whole
        # minor range hunting for pages that don't exist. The trailing-404 break is gated on
        # having seen a page, so a line where none is ever seen relies on a separate cap on
        # consecutive LEADING 404s: after that many, give up and fall back to the umbrella
        # page. Bounds HTTP cost to a handful of requests rather than ~50 per bare major.
        # Regression guard for the saw_page-gated break running the full 0..max_minor range.
        pages = {"19": _titled_page("Databricks Runtime 19 LTS")}
        calls = []

        def counting(url):
            calls.append(_slug_of(url))
            return pages.get(_slug_of(url))

        with mock.patch.object(sync, "fetch_opt", counting):
            self.assertEqual(sync.dbr_point_releases("19"), ([], "19"))
        # Five consecutive leading 404s (19.0..19.4) trip the cap; 19.5 is never probed, and
        # the only further request is the umbrella page itself.
        self.assertIn("19.4", calls)
        self.assertNotIn("19.5", calls)
        self.assertIn("19", calls)

    def test_umbrella_leading_miss_cap_sits_above_the_short_gap_slack(self):
        # The leading cap must sit above the slack that lets a removed early page not hide a
        # later live release: four leading 404s then a live release is still discovered.
        pages = {"18.4": _titled_page("Databricks Runtime 18.4")}
        with mock.patch.object(sync, "fetch_opt", _fake_fetch_opt(pages)):
            self.assertEqual(sync.dbr_point_releases("18"), (["18.4"], "18.4"))

    def test_umbrella_leading_miss_cap_gives_up_past_its_bound(self):
        # The accepted trade-off boundary: a live release sitting past a full run of leading
        # 404s (18.0..18.4 all genuine 404s, 18.5 live) is NOT discovered — the cap fires at
        # 18.4 and 18.5 is never probed, so the line falls back to the umbrella page. This
        # can only bite if five consecutive point-release pages are genuinely *deleted*
        # (EoS pages return 'eos', which resets the counter), which upstream doesn't do. This
        # test locks the cap value so a future change to it is a deliberate, visible edit.
        pages = {"18.5": _titled_page("Databricks Runtime 18.5"),
                 "18": _titled_page("Databricks Runtime 18 LTS")}
        calls = []

        def counting(url):
            calls.append(_slug_of(url))
            return pages.get(_slug_of(url))

        with mock.patch.object(sync, "fetch_opt", counting):
            self.assertEqual(sync.dbr_point_releases("18"), ([], "18"))
        self.assertNotIn("18.5", calls)


class SyncDbrFolderKeyTest(unittest.TestCase):
    def test_umbrella_line_publishes_point_releases_and_the_bare_major_umbrella(self):
        # DBR 18: each live point release gets its own folder (keyed by its minor), and the
        # latest also produces the bare-major umbrella folder clusters request as 18.x.
        # Telemetry shows both naming forms are in live use, so we publish both.
        pages = {
            "18.1": _runtime_page("Databricks Runtime 18.1"),
            "18.2": _runtime_page("Databricks Runtime 18.2"),
        }
        writes, fetched = [], []
        base_fetch = _fake_fetch(pages)

        def counting_fetch(url):
            fetched.append(_slug_of(url))
            return base_fetch(url)

        with mock.patch.object(sync, "discover_dbr", return_value=["18"]), \
                mock.patch.object(sync, "dbr_point_releases",
                                  return_value=(["18.1", "18.2"], "18.2")), \
                mock.patch.object(sync, "fetch", counting_fetch), \
                mock.patch.object(sync, "_write_env",
                                  lambda key, pkgs, pv, dbconnect: writes.append((key, dbconnect))):
            sync.sync_dbr()
        # Point-release folders pin their exact minor (18.1 -> ~=18.1.0); the bare-major
        # umbrella tracks the whole major line like a serverless major (18 -> ~=18.0), so its
        # dbconnect is the bare major, not the latest point release's minor.
        self.assertEqual(
            sorted(writes),
            [("18.1.x-scala2.13", "18.1"),
             ("18.2.x-scala2.13", "18.2"),
             ("18.x-scala2.13", "18")],
        )
        # 18.2 is both a point release and the umbrella source, but is fetched only once.
        self.assertEqual(sorted(fetched), ["18.1", "18.2"])

    def test_pre18_line_publishes_only_its_minor_folder(self):
        # Regression guard: pre-18 lines are unaffected — one folder, keyed by the minor
        # from the page title, and no bare-major umbrella.
        pages = {"17.3lts": _runtime_page("Databricks Runtime 17.3 LTS")}
        writes = []
        with mock.patch.object(sync, "discover_dbr", return_value=["17.3lts"]), \
                mock.patch.object(sync, "dbr_point_releases", return_value=(["17.3lts"], None)), \
                mock.patch.object(sync, "fetch", _fake_fetch(pages)), \
                mock.patch.object(sync, "_write_env",
                                  lambda key, pkgs, pv, dbconnect: writes.append((key, dbconnect))):
            sync.sync_dbr()
        self.assertEqual(writes, [("17.3.x-scala2.13", "17.3")])

    def test_umbrella_only_line_publishes_bare_major_folder_end_to_end(self):
        # DBR 19 today: no point-release pages, so dbr_point_releases falls back to the
        # umbrella page. Driven end-to-end through sync_dbr, dbr_meta reads the bare major
        # from the title ('Databricks Runtime 19 LTS' -> key_ver '19'), and the single
        # bare-major umbrella folder is pinned to the whole major line (dbconnect '19' ->
        # ~=19.0), not to a '.0' point release. This is the path where dbr_meta defaults the
        # minor, so it's the one most worth an end-to-end assertion.
        pages = {"19": _runtime_page("Databricks Runtime 19 LTS")}
        writes = []
        with mock.patch.object(sync, "discover_dbr", return_value=["19"]), \
                mock.patch.object(sync, "dbr_point_releases", return_value=([], "19")), \
                mock.patch.object(sync, "fetch", _fake_fetch(pages)), \
                mock.patch.object(sync, "_write_env",
                                  lambda key, pkgs, pv, dbconnect: writes.append((key, dbconnect))):
            sync.sync_dbr()
        self.assertEqual(writes, [("19.x-scala2.13", "19")])


class SyncDbrMlFolderKeyTest(unittest.TestCase):
    def test_umbrella_ml_line_publishes_point_releases_and_umbrella(self):
        pages = {
            "18.1": _runtime_page("Databricks Runtime 18.1"),    # base pages for meta
            "18.2": _runtime_page("Databricks Runtime 18.2"),
            "18.1ml": _ml_page(),
            "18.2ml": _ml_page(),
        }
        writes = []
        with mock.patch.object(sync, "discover_dbr", return_value=["18ml"]), \
                mock.patch.object(sync, "dbr_point_releases",
                                  return_value=(["18.1ml", "18.2ml"], "18.2ml")), \
                mock.patch.object(sync, "fetch", _fake_fetch(pages)), \
                mock.patch.object(sync, "_write_env",
                                  lambda key, pkgs, pv, dbconnect: writes.append((key, dbconnect))):
            sync.sync_dbr_ml()
        # The bare-major ML umbrella folders track the whole major line (dbconnect "18" ->
        # ~=18.0), while the point-release ML folders keep their exact minor.
        self.assertEqual(
            sorted(writes),
            [("18.1.x-cpu-ml-scala2.13", "18.1"),
             ("18.1.x-gpu-ml-scala2.13", "18.1"),
             ("18.2.x-cpu-ml-scala2.13", "18.2"),
             ("18.2.x-gpu-ml-scala2.13", "18.2"),
             ("18.x-cpu-ml-scala2.13", "18"),
             ("18.x-gpu-ml-scala2.13", "18")],
        )


if __name__ == "__main__":
    unittest.main()
