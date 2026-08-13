#!/usr/bin/env python3
"""Verify that every environment's ``pyproject.toml`` provisions with ``uv``.

For each ``python/**/pyproject.toml`` this runs ``uv sync`` in a throwaway copy
(so nothing lands in the repo) and reports whether the dependency set -- the
dev-group ``databricks-connect`` plus the ``[tool.uv].constraint-dependencies``
pins -- can be **resolved and installed** onto the environment's ``requires-python``.
This mirrors what the Databricks CLI / VS Code extension does when setting up a
local environment, so a failure here is the ``E_PROVISION`` a developer would hit.

``uv sync`` (not just ``uv lock``) is deliberate: resolving only proves a solution
exists, but provisioning also *builds/installs* every pin, and that is where the
real failures live -- e.g. a Python 3.12 environment pinning ``pandas~=1.5.3``
resolves fine yet fails to build (no cp312 wheel; the sdist errors on
``pkg_resources``). ``--mode resolve`` runs ``uv lock`` only for a fast bounds check.

Two failure classes this catches:
  * resolve  - contradictory pins, no solution (e.g. databricks-sdk vs the
               databricks-connect floor -- see issue #16).
  * build    - a pin with no installable wheel for the target Python that also
               fails to build from source (e.g. pandas 1.5.3 on py3.12).

Usage:
    python .github/scripts/verify_resolve.py                # all envs (sync)
    python .github/scripts/verify_resolve.py python/dbr/18.2.x-scala2.13
    python .github/scripts/verify_resolve.py --mode resolve --jobs 8
    python .github/scripts/verify_resolve.py --index-url https://pypi-proxy.cloud.databricks.com/simple

The index URL may also come from the ``UV_DEFAULT_INDEX`` / ``UV_INDEX_URL``
environment; ``--index-url`` overrides both. With none set, ``uv`` uses PyPI.

Exit code is non-zero if any environment fails.
"""
import argparse
import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def discover(paths):
    """Yield pyproject.toml files. ``paths`` may be dirs, files, or empty (=all)."""
    if not paths:
        yield from sorted((REPO_ROOT / "python").rglob("pyproject.toml"))
        return
    for p in paths:
        p = pathlib.Path(p)
        if p.is_dir():
            yield from sorted(p.rglob("pyproject.toml"))
        elif p.name == "pyproject.toml":
            yield p
        else:
            raise SystemExit(f"not a pyproject.toml or directory: {p}")


def env_label(pyproject):
    """'python/dbr/18.2.x-scala2.13/pyproject.toml' -> 'dbr/18.2.x-scala2.13'."""
    rel = pyproject.resolve().parent.relative_to(REPO_ROOT / "python")
    return str(rel)


def _uv_cmd(mode):
    # sync resolves + builds + installs (catches build failures); lock only resolves.
    # --no-install-project: the temp copy has no source tree, so skip the root package.
    if mode == "resolve":
        return ["uv", "lock"]
    return ["uv", "sync", "--no-install-project"]


def check_env(pyproject, index_url, mode):
    """Run uv for one env in a temp copy. Return (label, ok, secs, detail)."""
    label = env_label(pyproject)
    start = time.monotonic()
    env = os.environ.copy()
    if index_url:
        env["UV_DEFAULT_INDEX"] = index_url
    with tempfile.TemporaryDirectory(prefix="verify-resolve-") as tmp:
        shutil.copy(pyproject, pathlib.Path(tmp) / "pyproject.toml")
        try:
            proc = subprocess.run(
                _uv_cmd(mode),
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except FileNotFoundError:
            raise SystemExit("`uv` not found on PATH. Install uv: https://docs.astral.sh/uv/")
        except subprocess.TimeoutExpired:
            return label, False, time.monotonic() - start, "timed out after 1800s"
    secs = time.monotonic() - start
    if proc.returncode == 0:
        return label, True, secs, ""
    # uv writes the resolver proof / build error to stderr; keep the tail for the report.
    tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
    return label, False, secs, tail


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="env dirs or pyproject.toml files (default: all)")
    ap.add_argument("--mode", choices=("sync", "resolve"), default="sync",
                    help="sync = resolve+build+install (default, catches build failures); "
                         "resolve = uv lock only (fast bounds check)")
    ap.add_argument("--index-url", default=None,
                    help="package index for uv (overrides UV_DEFAULT_INDEX/UV_INDEX_URL)")
    ap.add_argument("--jobs", "-j", type=int, default=4, help="parallel jobs (default: 4)")
    args = ap.parse_args()

    pyprojects = list(discover(args.paths))
    if not pyprojects:
        raise SystemExit("no pyproject.toml files found")

    index_url = args.index_url or os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL")
    verb = "Provisioning (uv sync)" if args.mode == "sync" else "Resolving (uv lock)"
    print(f"{verb} {len(pyprojects)} environment(s)"
          + (f" via {index_url}" if index_url else " via default index (PyPI)")
          + f", jobs={args.jobs}\n")

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(check_env, p, index_url, args.mode): p for p in pyprojects}
        for fut in concurrent.futures.as_completed(futs):
            label, ok, secs, detail = fut.result()
            results.append((label, ok, secs, detail))
            print(f"  {'PASS' if ok else 'FAIL'}  {label:<40} {secs:5.1f}s")

    results.sort()
    failures = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failures)}/{len(results)} succeeded.")
    if failures:
        print("\nFailures:")
        for label, _ok, _secs, detail in failures:
            print(f"\n=== {label} ===\n{detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
