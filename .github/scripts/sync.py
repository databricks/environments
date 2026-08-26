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

    Capture the whole field value up to the list item's close, strip inline tags
    (mirroring ``table_pkgs``) so a digit inside an attribute/href -- e.g. a linked
    Spark version -- can't be read as a Scala version, then take each MAJOR.MINOR (the
    '(?:\\.\\d+)?' consumes the patch so '2.12.15' contributes '2.12', not '2.12' plus a
    stray '.15'); fall back to a single match if the item isn't delimited as expected
    so a layout change degrades to today's behaviour rather than to nothing."""
    field = re.search(r"Scala</strong>\s*:(.*?)</li>", html, re.S)
    if not field:
        m = re.search(r"Scala</strong>\s*:\s*(\d+\.\d+)", html)
        return [m.group(1)] if m else []
    text = re.sub(r"<[^>]+>", " ", field.group(1))
    scalas = []
    for v in re.findall(r"(\d+\.\d+)(?:\.\d+)?", text):
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


def dbr_point_releases(index_slug, ml=False, max_minor=10):
    """Expand an index slug into the concrete page slugs to generate a folder for.

    DBR changed its versioning scheme at 18: the index links a single umbrella slug
    ('18' / '18ml', titled 'Databricks Runtime 18 LTS ...') whose point releases live at
    their own pages ('18.0', '18.1', '18.2', and the ML '18.0ml' ...). Pre-18 lines keep
    the old scheme — the index slug ('17.3lts') is itself the runtime page.

    For a bare-major umbrella slug we probe '<major>.<minor>[ml]' for minor 0..max_minor,
    stopping after two consecutive 404s (end-of-list), and return the LIVE (non-EoS) ones.
    If a bare major has no point-release pages yet (e.g. DBR 19 today), we fall back to the
    umbrella slug itself so its folder is still generated. Non-umbrella slugs are returned
    as-is when live. Each returned slug is meant to be fed to dbr_meta so the minor is read
    from that page's own title rather than defaulted.
    """
    core = re.sub(r"(lts)?(-?ml)?$", "", index_slug)   # '18ml'->'18' ; '17.3lts-ml'->'17.3'
    suffix = "ml" if ml else ""

    def live_page(slug):
        """(html, ok): html of the page if live (None when 404 or EoS), and ok=False on a
        transient fetch error so the caller can distinguish "confirmed absent" from
        "couldn't tell". Transient errors (timeout, 5xx) are logged and never mistaken for
        a 404, mirroring discover_serverless — a flaky run must not abort the sync or be
        read as end-of-list."""
        try:
            html = fetch_opt(DBR_PAGE.format(slug=slug))
        except Exception as e:
            print(f"  ! dbr [{slug}]: transient fetch error ({e}); skipping (not end-of-list)")
            return None, False
        if html is None or is_eos(html):
            return None, True
        return html, True

    if not re.fullmatch(r"\d+", core):
        # Pre-18 scheme: the index slug is the runtime page. Keep it if it's live.
        html, _ = live_page(index_slug)
        return [index_slug] if html else []

    live, misses = [], 0
    for minor in range(0, max_minor + 1):
        slug = f"{core}.{minor}{suffix}"
        html, ok = live_page(slug)
        if not ok:
            # Transient error: skip this minor without advancing the end-of-list counter.
            continue
        if html is None:
            misses += 1
            if misses >= 2:
                break
            continue
        misses = 0
        live.append(slug)
    if live:
        return live
    # No point-release pages exist yet (e.g. DBR 19) — fall back to the umbrella page.
    html, _ = live_page(index_slug)
    return [index_slug] if html else []


def _write_env(key, pkgs, python_version, dbconnect):
    out_dir = os.path.join(REPO, "python", "dbr", key)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "pyproject.toml"), "w", encoding="utf-8") as f:
        f.write(envgen.build_pyproject(pkgs, key, python_version, dbconnect=dbconnect))
    with open(os.path.join(out_dir, "constraints.txt"), "w", encoding="utf-8") as f:
        f.write(envgen.build_constraints(pkgs, key))
    print(f"  + dbr/{key} (python {python_version}, {len(pkgs)} packages)")


def sync_dbr():
    for index_slug in discover_dbr():
        for slug in dbr_point_releases(index_slug, ml=False):
            try:
                html = fetch(DBR_PAGE.format(slug=slug))
            except Exception as e:
                print(f"  ! dbr [{slug}]: fetch failed ({e}); skipping")
                continue
            meta = dbr_meta(html)
            pkgs, _ = parse_dbr_page(html)
            if not meta or not pkgs:
                print(f"  ! dbr [{slug}]: no meta / Python table; skipping")
                continue
            key_ver, dbconnect_ver, scalas, python_version = meta
            for scala in scalas:
                _write_env(f"{key_ver}.x-scala{scala}", pkgs, python_version, dbconnect_ver)


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
        for slug in dbr_point_releases(index_slug, ml=True):
            _sync_dbr_ml_page(slug)


def _sync_dbr_ml_page(slug):
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
    for variant in ("cpu", "gpu"):
        pkgs = ml_variant_pkgs(ml_html, variant)
        if not pkgs:
            print(f"  ! dbr-ml [{slug}] {variant}: no packages found; skipping")
            continue
        for scala in scalas:
            _write_env(f"{key_ver}.x-{variant}-ml-scala{scala}", pkgs, python_version, dbconnect_ver)


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
