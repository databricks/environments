# Contributing to databricks-environments

Thank you for your interest in contributing! This document explains how the repo
is maintained, how to make a change, and how to open a pull request.

## Found an issue?

If you find a bug or have a feature request, please file an issue:
https://github.com/databricks/environments/issues/new

When reporting incorrect or missing dependency pins, please include the affected
environment (e.g. `dbr/17.3.x-scala2.13` or `serverless-v5`) and the package(s)
in question.

## How this repo is maintained

The `python/` artifacts (`pyproject.toml` / `constraints.txt`) are **generated,
not hand-written**. A scheduled GitHub Action (`.github/workflows/sync.yml`) runs
`.github/scripts/sync.py` to regenerate every environment from the Databricks
release notes, reconciles the result against what's committed, and **opens a PR**
when an environment drifts or a new version appears. A maintainer reviews and
merges that PR.

**Please do not hand-edit files under `python/`** — changes there will be
overwritten by the next sync. If a generated artifact is wrong, the fix belongs
in the generator, not the output.

## Making a change

Most contributions change the generation logic:

- **`.github/scripts/envgen.py`** — the mechanical transform rules (how a package
  list becomes a `pyproject.toml` / `constraints.txt`).
- **`.github/scripts/sync.py`** — discovery and reconciliation (which environments
  exist, how release-notes pages are fetched and parsed).

See the [README](README.md) for the full description of the layout, artifacts,
and sync mechanism.

## Prerequisites

- **Python** `3.12` (the version CI runs `sync.py` with)

## Getting started

Run the sync locally to regenerate into the working tree:

```sh
python .github/scripts/sync.py            # regenerate into the working tree
python .github/scripts/sync.py --check    # report drift / new versions, exit non-zero if any
python .github/scripts/sync.py --manifest # sha256 manifest of python/ (no fetch)
```

Use `--check` to verify your change produces the expected tree, and `--manifest`
to compare two trees byte-for-byte (regenerate on a fresh checkout, then diff the
manifests to confirm reproducibility).

## Opening a pull request

1. Make your change to the generator (or docs) and regenerate any affected
   artifacts with `python .github/scripts/sync.py`.
2. Confirm `python .github/scripts/sync.py --check` is clean.
3. Open a pull request against `main`. CI and a maintainer review are required
   before merge.
