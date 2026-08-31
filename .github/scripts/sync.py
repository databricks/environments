#!/usr/bin/env python3
"""Weekly sync: discover newly published Databricks environments from the public
release notes, regenerate the pinned artifacts, and reconcile them against what is
committed in this repo.

Serverless is the reliable path: each environment-version release-notes page links a
downloadable ``requirements-env-N.txt`` (a clean ``name==version`` list). This script
discovers the available versions, downloads each list, applies the transformation
rules (see ``envgen``), and writes ``python/serverless/serverless-vN/{pyproject.toml,
constraints.txt}``.

DBR is a TODO: those pages list libraries inline in HTML (no downloadable file), so
parsing is more brittle and is intentionally left as a follow-up.

Modes:
    python scripts/sync.py            # regenerate into the working tree
    python scripts/sync.py --check    # regenerate, then exit 1 if anything changed
                                      # (drift / new versions) without leaving edits

Reconciliation is delegated to git: after regeneration, ``git status --porcelain``
on ``python/`` shows changed (drift) and untracked (new version) artifacts. In
``--check`` mode the script restores the working tree and returns non-zero so CI can
open a PR.
"""
import argparse
import hashlib
import html as html_lib          # aliased: functions here take an ``html`` parameter
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

import envgen

# Repo root, resolved independently of where this script lives.
REPO = subprocess.check_output(
    ["git", "-C", os.path.dirname(os.path.abspath(__file__)), "rev-parse", "--show-toplevel"],
    text=True,
).strip()
SERVERLESS_PAGE = "https://docs.databricks.com/aws/en/release-notes/serverless/environment-version/{word}"
DBR_INDEX = "https://docs.databricks.com/aws/en/release-notes/runtime/"
DBR_PAGE = "https://docs.databricks.com/aws/en/release-notes/runtime/{slug}"
DOCS_HOST = "https://docs.databricks.com"
# Serverless version pages use spelled-out ordinals in the URL (.../environment-version/four).
# The trailing-404 heuristic bounds discovery at the real last version; this list only
# needs headroom beyond the highest published version (currently v5).
WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen", "twenty"]

# Index entries that aren't a runtime version page.
DBR_NON_VERSION = {"maintenance-updates", "databricks-runtime-ver", "eos"}

# End-of-support runtime lines to publish anyway, as page slugs. EoS lines are dropped
# from the release-notes index (so discover_dbr never sees them) and skipped by
# dbr_point_releases' EoS check, and the 'eos' page links none — so they can't be
# discovered, only listed explicitly. Live clusters still request a few of them, and VPEX
# telemetry then records E_ENV_UNSUPPORTED (an owned target with no environment published),
# so we make a deliberate, narrow exception to the EoS-not-published policy for exactly the
# lines with real traffic. Add a slug ONLY when telemetry shows its exact env_key failing:
#   12.2 -> dbr/12.2.x-scala2.12   (8 events / 2 workspaces)
#   16.1 -> dbr/16.1.x-scala2.12   (1 event  / 1 workspace)
DBR_EOS_PUBLISH = ["12.2", "16.1"]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "databricks-environments-sync"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def fetch_opt(url):
    """fetch(), but return None on a genuine 404 (page doesn't exist). Transient
    errors (timeout, 5xx) re-raise so the caller doesn't mistake them for a 404."""
    try:
        return fetch(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def discover_serverless():
    """Return [(n, page_html)] for every serverless version page that exists.

    Only a genuine HTTP 404 counts as "this version doesn't exist" (end-of-list).
    Transient errors (timeout, 5xx) must not be mistaken for the end of the list —
    otherwise a flaky run silently drops every higher version — so they're logged
    and skipped without advancing the end-of-list counter.
    """
    found = []
    misses = 0
    for n, word in enumerate(WORDS, start=1):
        try:
            html = fetch(SERVERLESS_PAGE.format(word=word))
            found.append((n, html))
            misses = 0
        except urllib.error.HTTPError as e:
            if e.code == 404:
                misses += 1
                # Only treat trailing 404s as end-of-list. Retired early versions
                # (e.g. v1/v2 removed) must not stop us before reaching live ones,
                # so require at least one found version before honoring the break.
                if found and misses >= 2:
                    break
            else:
                print(f"  ! serverless v{n}: transient HTTP {e.code}; skipping (not end-of-list)")
        except Exception as e:
            print(f"  ! serverless v{n}: transient fetch error ({e}); skipping (not end-of-list)")
    return found


def serverless_python_version(html):
    """The Python version from the "System environment" list (``Python</strong>: 3.12.3``).
    Matched precisely rather than the first ``3.x.y`` on the page (the package table is
    full of versions); returns None if not found so the caller skips."""
    pv = (re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
          or re.search(r"Python version[^0-9]{0,40}?(\d+\.\d+\.\d+)", html))
    return pv.group(1) if pv else None


def serverless_requirements_url(html, kind, n):
    """Asset URL for a serverless requirements file, or None.

    ``kind`` is 'env' (standard) or 'ml'. A version page links the standard
    ``requirements-env-N.txt`` and, once the ML base environment exists for that
    version (serverless v5+), also ``requirements-ml-N.txt``.
    """
    m = re.search(rf"(/[\w/-]*assets/files/requirements-{kind}-{n}-[0-9a-f]+\.txt)", html)
    return DOCS_HOST + m.group(1) if m else None


def sync_serverless():
    written = []
    for n, html in discover_serverless():
        python_version = serverless_python_version(html)
        if not python_version:
            print(f"  ! serverless-v{n}: no python version; skipping")
            continue
        # Standard environment, plus the ML base environment when published (v5+).
        for kind, suffix in (("env", ""), ("ml", "-ml")):
            url = serverless_requirements_url(html, kind, n)
            if not url:
                continue
            env_name = f"serverless-v{n}{suffix}"
            try:
                pkgs = envgen.parse_requirements(fetch(url))
            except Exception as e:
                # Don't let one flaky download abort the rest of the sync.
                print(f"  ! {env_name}: download failed ({e}); skipping")
                continue
            out_dir = os.path.join(REPO, "python", "serverless", env_name)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
                f.write(envgen.build_pyproject(pkgs, env_name, python_version))
            with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
                f.write(envgen.build_constraints(pkgs, env_name))
            print(f"  + {env_name} (python {python_version}, {len(pkgs)} packages)")
            written.append(env_name)
    return written


def table_pkgs(html, anchor_id):
    """Parse the first ``<table>`` after the heading with ``id=<anchor_id>`` into
    {normalized_name: version}.

    Cells alternate Library, Version across however many column pairs the page uses
    (``<td><p>name<td><p>version ...``). We anchor on the heading's anchor id rather
    than its text — some pages mention the phrase earlier in a changelog and carry
    other tables (e.g. dated maintenance tables) we must not capture.
    """
    m = re.search(r'id=["\']?' + re.escape(anchor_id), html)
    if not m:
        return None
    t0 = html.find("<table>", m.end())
    t1 = html.find("</table>", t0)
    if t0 == -1 or t1 == -1:
        return None
    # Split on <td> and strip any inline tags (<p>, nested <a>/<code>, etc.) rather
    # than assuming every cell is exactly "<td><p>text" — a cell that deviates from
    # that shape would otherwise be dropped and shift name/version alignment for the
    # rest of the table. Header <th> cells are naturally excluded.
    cells = [re.sub(r"<[^>]+>", "", c).strip()
             for c in re.split(r"<td[^>]*>", html[t0:t1])[1:]]
    return {envgen.norm(cells[k]): cells[k + 1]
            for k in range(0, len(cells) - 1, 2) if cells[k]}


def dbr_title(html):
    """The page's own runtime <title>, e.g. 'Databricks Runtime 18.2 (EoS)', or ''.
    Anchored to <title> rather than the first in-body 'Databricks Runtime N' because
    sidebar/nav links to other runtimes appear earlier in the source."""
    m = re.search(r"<title[^>]*>(Databricks Runtime[^<|]*)", html)
    return m.group(1).strip() if m else ""


def is_eos(html):
    """True if the runtime is end-of-support. Read from the <title> only — an '(EoS)'
    marker elsewhere on the page (nav, changelog links to retired runtimes) is not this
    runtime's own status and would give false positives."""
    return "(EoS)" in dbr_title(html)


def dbr_scalas(html):
    """The Scala versions ('2.12', '2.13', ...) a runtime page's System environment
    lists, in page order and de-duplicated.

    Most runtimes ship one image ('Scala</strong>: 2.13.16' -> ['2.13']). A runtime in
    the 2.12->2.13 migration window publishes two images from a single page, rendered as
    'Scala</strong>: 2.12.15 <strong>or</strong> 2.13.10' -> ['2.12', '2.13']. Both
    images share one 'Installed Python libraries' table (only the Java/Scala libraries
    differ, which this repo doesn't consume), so the caller writes one folder per Scala
    version off the same package set.

    Capture the field value up to the list item's close, strip inline tags (mirroring
    ``table_pkgs``, so a digit inside an attribute/href -- e.g. a linked Spark version
    -- can't leak in) and unescape entities (so a ``&nbsp;`` delimiter becomes real
    whitespace). Then read only the leading enumeration: a version, or several joined by
    delimiters. Matching just that run stops a trailing annotation ('(Apache Spark 3.5)')
    from contributing a bogus version, while the delimiter class stays deliberately wide
    -- whitespace, ',', '/', or the words 'or'/'and' -- because narrowing it to the exact
    ' or ' the page happens to use today would silently drop the second variant (and
    resurrect the 404) the day a copy-edit changes the separator. Each MAJOR.MINOR is
    taken with the patch consumed by '(?:\\.\\d+)?' ('2.12.15' -> '2.12', not '2.12' plus
    a stray '.15'). Fall back to a single match if the item isn't delimited as expected,
    so a layout change degrades to today's behaviour rather than to nothing."""
    field = re.search(r"Scala</strong>\s*:(.*?)</li>", html, re.S)
    if not field:
        m = re.search(r"Scala</strong>\s*:\s*(\d+\.\d+)", html)
        return [m.group(1)] if m else []
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", field.group(1)))
    ver = r"\d+\.\d+(?:\.\d+)?"
    sep = r"(?:[\s,/]|\bor\b|\band\b)+"          # delimiter, never itself a version
    enum = re.match(rf"\s*({ver}(?:{sep}{ver})*)", text)
    if not enum:
        return []
    scalas = []
    for v in re.findall(r"(\d+\.\d+)(?:\.\d+)?", enum.group(1)):
        if v not in scalas:
            scalas.append(v)
    return scalas


def dbr_meta(html):
    """Return (key_ver, dbconnect_ver, scalas, python_version) from a standard runtime
    page, or None if any piece is missing. ``scalas`` is a non-empty list (see
    ``dbr_scalas``); the caller emits one '<key_ver>.x-scala<scala>' folder per entry.

    A point-release page carries the minor in its title ('Databricks Runtime 18.2'), so
    both versions are the real minor: ('18.2', '18.2', ['2.13'], '3.12.3') -> folder
    '18.2.x-scala2.13', databricks-connect~=18.2.0.

    An umbrella LTS page has no minor in its title ('Databricks Runtime 19 LTS'). Such a
    line is addressed by its bare major in a cluster's ``spark_version`` ('19.x-scala2.13'),
    so ``key_ver`` drops the minor to match that naming, while ``dbconnect_ver`` still
    defaults the minor to '.0' (databricks-connect is published per point release, so the
    pin needs a concrete minor): ('19', '19.0', ['2.13'], '3.12.3') -> folder
    '19.x-scala2.13', databricks-connect~=19.0.0. Feed point-release pages here (see
    ``dbr_point_releases``) so a real minor is used whenever one is published."""
    ver = re.search(r"<title[^>]*>Databricks Runtime\s+(\d+)(?:\.(\d+))?", html)
    scalas = dbr_scalas(html)
    pv = re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
    if not (ver and scalas and pv):
        return None
    major, minor = ver.group(1), ver.group(2)
    key_ver = f"{major}.{minor}" if minor else major
    dbconnect_ver = f"{major}.{minor or '0'}"
    return key_ver, dbconnect_ver, scalas, pv.group(1)


def parse_dbr_page(html):
    """Extract (pkgs, python_version) from a standard DBR runtime page."""
    pkgs = table_pkgs(html, "installed-python-libraries")
    pv = re.search(r"Python</strong>\s*:\s*(\d+\.\d+\.\d+)", html)
    return pkgs, (pv.group(1) if pv else None)


def discover_dbr(ml=False):
    """Enumerate runtime version slugs from the release-notes index.

    ml=False -> standard runtimes (e.g. '17.3lts', '19');
    ml=True  -> ML variants     (e.g. '17.3lts-ml', '19ml').
    """
    html = fetch(DBR_INDEX)
    slugs = re.findall(r"release-notes/runtime/([0-9][\w.-]*)", html)
    pat = r"^\d+(\.\d+)?(lts)?-?ml$" if ml else r"^\d+(\.\d+)?(lts)?$"
    out, seen = [], set()
    for s in slugs:
        s = s.rstrip("/")
        if s in seen or s in DBR_NON_VERSION or s.endswith("ml") != ml:
            continue
        if re.match(pat, s):
            seen.add(s)
            out.append(s)
    return out


def _umbrella_major(index_slug):
    """The bare major an umbrella-scheme line's folder is keyed by, or None for the
    pre-18 scheme (where the folder minor is read from the page title instead).

    DBR changed its versioning at 18: those index slugs are the bare major ('18', '18ml',
    '19', '19ml'), and a cluster addresses the whole line by that bare major in its
    ``spark_version`` ('18.x-scala2.13'). So the folder drops the minor to match — even
    though the packages, Scala/Python and databricks-connect pin come from the latest live
    point release page (see ``dbr_point_releases``). Pre-18 lines ('17.3lts', '16.4lts-ml')
    keep the old scheme (the index slug is itself the runtime page) and return None."""
    core = re.sub(r"(lts)?(-?ml)?$", "", index_slug)   # 18ml -> 18 ; 17.3lts-ml -> 17.3
    return core if re.fullmatch(r"\d+", core) else None


def dbr_point_releases(index_slug, ml=False, max_minor=50, max_leading_misses=5):
    """Return ``(pointrelease_slugs, umbrella_slug)`` for a runtime line: the page slugs to
    generate individual '<minor>.x' folders from, and the page the bare-major '<major>.x'
    umbrella folder is generated from (or None when there is none, or it isn't safe this run).

    DBR changed its versioning at 18: the index links a bare-major umbrella slug ('18' /
    '18ml') whose point releases live at their own pages ('18.0', '18.1', '18.2', ...).
    Clusters address such a line either by a specific point release ('18.2.x-...') or by the
    bare major ('18.x-...') depending on client version (telemetry shows both in live use),
    so we publish BOTH: one folder per live point release, and one bare-major umbrella folder
    taken from the LATEST live point release (whose databricks-connect minor it pins). Pre-18
    lines keep the old scheme — the index slug ('17.3lts') is itself the runtime page and has
    no umbrella form, so umbrella_slug is None.

    We probe '<major>.<minor>[ml]' from minor 0, stopping after two consecutive 404s once a
    page has been seen (leading 404s — e.g. a removed old page — don't end the list before a
    later release is found). Before any page is seen, a longer run of ``max_leading_misses``
    404s ends the probe instead, so a bare major with no point-release pages at all (e.g. DBR
    19 today) doesn't scan the full range; ``max_minor`` is only a runaway backstop set well
    beyond any real line's point-release count, not the normal terminator. An EoS point release exists
    (retired, not end-of-list), so it is skipped without ending the probe and is not
    published. The umbrella comes from the newest live point release, EXCEPT when a probe
    newer than that was left indeterminate by a transient error — then umbrella_slug is None
    so a flaky run can't downgrade the umbrella, while the point-release folders it did
    confirm are still returned. If the whole line is EoS, both are empty. If a bare major has
    no point-release pages at all (e.g. DBR 19 today), there are no point-release folders and
    the umbrella falls back to the umbrella page itself. Slugs are meant to be fed to dbr_meta.
    """
    major = _umbrella_major(index_slug)
    suffix = "ml" if ml else ""

    def live_page(slug):
        """Classify one probe, returning one of:
          'live'   — a live, non-EoS runtime page.
          'eos'    — the page exists but the runtime is end-of-support: skip it, but it is
                     NOT end-of-list, so keep probing higher minors.
          'absent' — a genuine 404: counts toward the end-of-list break.
          'error'  — a transient fetch error (timeout, 5xx): logged and never mistaken for
                     a 404, mirroring discover_serverless — a flaky run must not abort the
                     sync or be read as end-of-list.
        The page body isn't returned — sync_dbr / sync_dbr_ml refetch the pages they
        generate from, so a probe only needs the classification."""
        try:
            html = fetch_opt(DBR_PAGE.format(slug=slug))
        except Exception as e:
            print(f"  ! dbr [{slug}]: transient fetch error ({e}); skipping (not end-of-list)")
            return "error"
        if html is None:
            return "absent"
        if is_eos(html):
            return "eos"
        return "live"

    if not major:
        # Pre-18 scheme: the index slug is the runtime page; no umbrella form.
        return ([index_slug], None) if live_page(index_slug) == "live" else ([], None)

    live, transient_minors, saw_page, misses = [], [], False, 0
    for minor in range(0, max_minor + 1):
        kind = live_page(f"{major}.{minor}{suffix}")
        if kind == "error":
            # Record the minor, but don't advance the end-of-list counter (a flaky probe
            # is not a confirmed 404).
            transient_minors.append(minor)
            continue
        if kind == "eos":
            # Retired but present — not end-of-list; skip and keep probing.
            saw_page = True
            misses = 0
            continue
        if kind == "absent":
            misses += 1
            # Two kinds of 404 run end the list. Once a page (live or EoS) has been seen, two
            # consecutive 404s are the trailing end-of-list — mirrors the "require at least
            # one found" guard in discover_serverless, so a removed early page can't stop the
            # probe before a later live release. Before any page is seen, a longer run of
            # leading 404s (max_leading_misses) is the terminator instead: a bare major with
            # no point-release pages at all (e.g. DBR 19 today) would otherwise probe the full
            # 0..max_minor range every run. Only genuine 404s count here — an EoS page is a
            # hit (kind 'eos') that resets misses below. A line's point releases are numbered
            # contiguously from .0, so five consecutive leading 404s mean none were published
            # (the empty case), not a gap before a later release. Five sits comfortably above
            # the trailing slack of 2 while bounding that empty case; it could only skip a real
            # release if five early minors that once existed were later removed while a higher
            # one stayed live — but upstream retires a page by marking it '(EoS)' (a hit), not
            # by deleting it, so that doesn't arise.
            cap = 2 if saw_page else max_leading_misses
            if misses >= cap:
                break
            continue
        saw_page = True
        misses = 0
        live.append(minor)

    if live:
        pointrelease_slugs = [f"{major}.{m}{suffix}" for m in live]
        latest_minor = live[-1]
        if any(t > latest_minor for t in transient_minors):
            # A point release newer than our latest was left indeterminate by a transient
            # error this run. Regenerating the '<major>.x' umbrella from the older confirmed
            # release would silently downgrade the folder a bare-major spark_version serves,
            # so keep the confirmed point-release folders but skip the umbrella — a clean run
            # picks up the true latest (the existing umbrella folder stays untouched).
            print(f"  ! dbr [{major}.x]: a point release newer than {major}.{latest_minor} "
                  f"was indeterminate (transient error); keeping point releases, skipping "
                  f"the umbrella to avoid a downgrade")
            return pointrelease_slugs, None
        return pointrelease_slugs, f"{major}.{latest_minor}{suffix}"

    if transient_minors:
        # Nothing was confirmed live, but a probe was indeterminate. A transient could be
        # masking a real point release whose page is the correct umbrella source; falling
        # back to the umbrella index page instead could generate the folder from different
        # (wrong) metadata. So skip conservatively — the existing folder is left untouched
        # and a clean run resolves it. Trade-off: on a line that genuinely has no point
        # release (the umbrella-only case), a single flaky probe still no-ops the line for a
        # whole cycle; the leading-miss cap keeps that window small (a handful of probes).
        print(f"  ! dbr [{major}.x]: no live point release confirmed and a probe was "
              f"indeterminate (transient error); skipping this run")
        return [], None
    if saw_page:
        # Point-release pages exist but none are live — the line is fully EoS (retired).
        # EoS lines are not published, so don't fall back to the umbrella page (which may
        # not yet carry the EoS marker itself). Any folders already published for this line
        # are left as-is; pruning retired lines is out of scope (sync only writes).
        return [], None
    # No point-release page exists at all (e.g. DBR 19 today) — fall back to the umbrella page.
    return ([], index_slug) if live_page(index_slug) == "live" else ([], None)


def _write_env(key, pkgs, python_version, dbconnect):
    out_dir = os.path.join(REPO, "python", "dbr", key)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(envgen.build_pyproject(pkgs, key, python_version, dbconnect=dbconnect))
    with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
        f.write(envgen.build_constraints(pkgs, key))
    print(f"  + dbr/{key} (python {python_version}, {len(pkgs)} packages)")


def _sync_dbr_page(slug, point_release, umbrella_major):
    """Fetch one standard runtime page and write its folder(s).

    A live point release publishes its own '<key_ver>.x' folder (pre-18: the whole line;
    EoS allowlist lines: their own line). The newest live release of a DBR 18+ line ALSO
    publishes the bare-major '<major>.x' umbrella that a cluster's spark_version resolves
    to — in ADDITION to its own folder, so the umbrella page (18.2) yields both '18.2.x'
    and '18.x' (``umbrella_major`` set). A point-release folder pins databricks-connect to
    its exact minor (18.2 -> ~=18.2.0); the bare-major umbrella instead pins the whole major
    line (18 -> ~=18.0), so a cluster addressing the line by its bare major resolves the
    newest databricks-connect in the major."""
    try:
        html = fetch(DBR_PAGE.format(slug=slug))
    except Exception as e:
        print(f"  ! dbr [{slug}]: fetch failed ({e}); skipping")
        return
    meta = dbr_meta(html)
    pkgs, _ = parse_dbr_page(html)
    if not meta or not pkgs:
        print(f"  ! dbr [{slug}]: no meta / Python table; skipping")
        return
    key_ver, dbconnect_ver, scalas, python_version = meta
    folder_vers = ([key_ver] if point_release else []) + ([umbrella_major] if umbrella_major else [])
    for folder_ver in folder_vers:
        # The bare-major umbrella tracks the whole major line (18 -> ~=18.0), like a
        # serverless major; a point release (and every EoS allowlist line) keeps its exact
        # minor (18.2 -> ~=18.2.0). See the docstring.
        dbconnect = umbrella_major if folder_ver == umbrella_major else dbconnect_ver
        for scala in scalas:
            _write_env(f"{folder_ver}.x-scala{scala}", pkgs, python_version, dbconnect)


def sync_dbr():
    for index_slug in discover_dbr():
        major = _umbrella_major(index_slug)
        pointrelease_slugs, umbrella_slug = dbr_point_releases(index_slug, ml=False)
        # The umbrella slug is usually also one of the point releases — fetch each page once.
        for slug in dict.fromkeys(pointrelease_slugs + ([umbrella_slug] if umbrella_slug else [])):
            _sync_dbr_page(
                slug,
                point_release=slug in pointrelease_slugs,
                umbrella_major=major if slug == umbrella_slug else None,
            )


def sync_dbr_eos():
    """Publish the end-of-support lines in DBR_EOS_PUBLISH (see its comment for why they
    exist and the telemetry rule for adding one).

    These are dropped from the index and skipped by dbr_point_releases' EoS check, so they
    are fetched directly here rather than discovered. Each is a self-contained runtime page
    with the real minor in its title (pre-18-style), so it publishes only its own
    '<key_ver>.x' folder(s) and no bare-major umbrella. They can't collide with the
    index-driven sync_dbr (that path never yields an EoS slug)."""
    for slug in DBR_EOS_PUBLISH:
        _sync_dbr_page(slug, point_release=True, umbrella_major=None)


def ml_variant_pkgs(ml_html, variant):
    """Python packages for an ML cluster variant ('cpu' or 'gpu').

    Newer ML pages link a downloadable ``requirements-{cpu,gpu}-<slug>.txt``; older
    ones render the list inline under ``python-libraries-on-{cpu,gpu}-clusters``.
    """
    m = re.search(r"(/[\w/-]*assets/files/requirements-" + variant + r"-[\w.-]+\.txt)", ml_html)
    if m:
        return envgen.parse_requirements(fetch(DOCS_HOST + m.group(1)))
    return table_pkgs(ml_html, f"python-libraries-on-{variant}-clusters")


def sync_dbr_ml():
    for index_slug in discover_dbr(ml=True):
        major = _umbrella_major(index_slug)
        pointrelease_slugs, umbrella_slug = dbr_point_releases(index_slug, ml=True)
        for slug in dict.fromkeys(pointrelease_slugs + ([umbrella_slug] if umbrella_slug else [])):
            _sync_dbr_ml_page(
                slug,
                point_release=slug in pointrelease_slugs,
                umbrella_major=major if slug == umbrella_slug else None,
            )


def _sync_dbr_ml_page(slug, point_release, umbrella_major):
    base = re.sub(r"-?ml$", "", slug)        # 18.2ml -> 18.2 ; 17.3lts-ml -> 17.3lts ; 19ml -> 19
    try:
        base_html = fetch(DBR_PAGE.format(slug=base))
        ml_html = fetch(DBR_PAGE.format(slug=slug))
    except Exception as e:
        print(f"  ! dbr-ml [{slug}]: fetch failed ({e}); skipping")
        return
    meta = dbr_meta(base_html)               # ML pages lack System environment; use base
    if not meta:
        print(f"  ! dbr-ml [{slug}]: no base meta from {base}; skipping")
        return
    key_ver, dbconnect_ver, scalas, python_version = meta
    # A live point release publishes its own '<minor>.x' ML folders; the newest one also
    # publishes the bare-major '<major>.x' ML umbrella (DBR 18+). Both share dbconnect_ver.
    folder_vers = ([key_ver] if point_release else []) + ([umbrella_major] if umbrella_major else [])
    for variant in ("cpu", "gpu"):
        pkgs = ml_variant_pkgs(ml_html, variant)
        if not pkgs:
            print(f"  ! dbr-ml [{slug}] {variant}: no packages found; skipping")
            continue
        for folder_ver in folder_vers:
            # Bare-major umbrella tracks the whole major line (18 -> ~=18.0); point-release
            # folders pin their exact minor. See sync_dbr for the rationale.
            dbconnect = umbrella_major if folder_ver == umbrella_major else dbconnect_ver
            for scala in scalas:
                _write_env(f"{folder_ver}.x-{variant}-ml-scala{scala}", pkgs, python_version, dbconnect)


def git(*args):
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if r.returncode != 0:
        # Don't let a git failure (e.g. lock contention) be read as "no changes".
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def reconcile():
    """Print drift (modified) and new (untracked) artifacts under python/."""
    status = git("status", "--porcelain", "python/")
    lines = [l for l in status.splitlines() if l]
    if not lines:
        print("\nReconciliation: no changes — repo is in sync with published docs.")
        return False
    changed, new = [], []
    for line in lines:
        # Fixed-width porcelain: 2-char status code, space, path. Don't strip the
        # line — a worktree-modified/deleted file has a leading space in the code
        # (' M'/' D'), and stripping would drop the first path character.
        code, path = line[:2], line[3:]
        (new if "?" in code else changed).append(path)
    print("\nReconciliation: changes detected")
    for p in new:
        print(f"  NEW    {p}")
    for p in changed:
        print(f"  DRIFT  {p}")
    return True


def print_manifest():
    """Print a deterministic, network-free description of the committed python/ tree:
    one line per environment with its package count and the sha256 of each artifact.

    Two trees that produce identical manifests are byte-for-byte identical payloads,
    so this is the comparison primitive for the reproducibility/portability test
    (e.g. regenerate on a fresh repo, then diff its manifest against this one).
    """
    base = os.path.join(REPO, "python")
    rows = []
    for dirpath, _, files in os.walk(base):
        if "constraints.txt" not in files:
            continue
        rel = os.path.relpath(dirpath, REPO).replace(os.sep, "/")
        cbytes = open(os.path.join(dirpath, "constraints.txt"), "rb").read()
        pkgs = sum(1 for l in cbytes.decode().splitlines()
                   if l.strip() and not l.startswith("#"))
        csha = hashlib.sha256(cbytes).hexdigest()
        ppath = os.path.join(dirpath, "pyproject.toml")
        psha = hashlib.sha256(open(ppath, "rb").read()).hexdigest() if os.path.exists(ppath) else "-"
        rows.append((rel, pkgs, csha, psha))
    for rel, pkgs, csha, psha in sorted(rows):
        print(f"{rel}\tpkgs={pkgs}\tconstraints={csha}\tpyproject={psha}")
    print(f"# {len(rows)} environments")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="regenerate, report drift/new, restore tree, exit 1 if changed")
    ap.add_argument("--manifest", action="store_true",
                    help="print a sha256 manifest of the committed python/ tree and exit "
                         "(no fetch, no regeneration) — for reproducibility comparison")
    args = ap.parse_args()

    if args.manifest:
        print_manifest()
        return

    if args.check:
        # --check restores python/ afterwards (checkout + clean), which would destroy
        # any pre-existing uncommitted work there. Refuse rather than clobber it.
        pre = git("status", "--porcelain", "python/").strip()
        if pre:
            print("Refusing --check: python/ has uncommitted changes that would be "
                  "discarded by the post-check restore. Commit or stash them first.")
            sys.exit(2)

    print("Discovering serverless environments from docs.databricks.com ...")
    sync_serverless()
    print("Syncing DBR runtimes from docs.databricks.com ...")
    sync_dbr()
    print("Publishing end-of-support DBR runtimes still requested by clusters ...")
    sync_dbr_eos()
    print("Syncing DBR ML runtimes (CPU + GPU) from docs.databricks.com ...")
    sync_dbr_ml()
    changed = reconcile()

    if args.check:
        # Safe now: python/ was verified clean above, so this only reverts/removes
        # what this run generated. Restore tracked files (skip if python/ isn't
        # committed yet — e.g. a fresh bootstrap clone where it's all untracked),
        # then drop any untracked generation.
        if git("ls-files", "python/").strip():
            git("checkout", "--", "python/")
        git("clean", "-fdq", "python/")
        sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
