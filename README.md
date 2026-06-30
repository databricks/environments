# databricks-environments

Per-compute **dependency constraint artifacts** for Databricks runtimes. Each
supported environment (a DBR version or a serverless environment version) gets a
pinned `pyproject.toml` (for uv / Poetry) and `constraints.txt` (for pip / conda) so
developers can reproduce the runtime's Python environment locally — matching the
exact Python version, `databricks-connect` version, and transitive dependency set.

This is the source of truth consumed by the Databricks CLI / VS Code extension when
setting up a local environment for a selected compute target.

## Layout

```
python/
  serverless/
    serverless-v4/
      pyproject.toml
      constraints.txt
    serverless-v5/
      ...
  dbr/
    17.3.x-scala2.13/             # standard runtime
      pyproject.toml
      constraints.txt
    17.3.x-cpu-ml-scala2.13/      # ML runtime, CPU clusters
    17.3.x-gpu-ml-scala2.13/      # ML runtime, GPU clusters (CUDA builds)
    16.4.x-scala2.12/
      ...
```

Top-level `python/` namespaces these as Python-ecosystem artifacts, leaving room for
other ecosystems later. Directory names mirror the identifiers the Databricks
platform exposes (`spark_version` for classic clusters, `serverless-vN` for
serverless), so resolving a target to its artifact is a deterministic lookup.

## Artifacts

- **`pyproject.toml`** (uv / Poetry) — `requires-python`, the `databricks-connect`
  pin in `[dependency-groups].dev` (installed by default under `uv sync`), and the
  full pinned set in `[tool.uv].constraint-dependencies`.
- **`constraints.txt`** (pip / conda) — flat `name~=version` pins, consumed via
  `PIP_CONSTRAINT` or `-c constraints.txt`. Does **not** list `databricks-connect`,
  so the pip path is constraints-only unless DB Connect is installed explicitly.

Both are a mechanical transform of the official package list published in the
Databricks release notes — see `.github/scripts/envgen.py` for the rules.

## How it stays in sync

A scheduled GitHub Action (`.github/workflows/sync.yml`) is the only mechanism that
maintains this repo. Weekly (and on-demand via *Run workflow*) it runs `.github/scripts/sync.py`
to regenerate every environment from the release notes, reconciles against what's
committed, and **opens a PR** when an environment drifts or a new version appears. A
maintainer reviews and merges that PR — the deliberate human gate, since docs parsing
is best-effort. Nobody hand-edits the `python/` artifacts.

`.github/scripts/sync.py` does the regeneration + reconciliation:

- **Serverless** — discovers the published environment versions, downloads each
  `requirements-env-N.txt`, and regenerates both artifacts.
- **DBR** — enumerates the standard runtime versions from the
  [runtime release-notes index](https://docs.databricks.com/aws/en/release-notes/runtime/),
  then for each fetches the page and parses the "Installed Python libraries" HTML
  table. The repo key (`<ver>.x-scala<scala>`) is built from the page's title and the
  Scala version in its System environment. DBR pages don't list `databricks-connect`,
  so its dev pin is derived from the runtime version.
- **DBR ML (CPU + GPU)** — for each `*-ml` runtime, a separate environment is produced
  per cluster type: `<ver>.x-cpu-ml-…` and `<ver>.x-gpu-ml-…`. Newer ML pages link
  downloadable `requirements-{cpu,gpu}-*.txt`; older ones render inline tables under
  `python-libraries-on-{cpu,gpu}-clusters`. The GPU set carries the CUDA builds
  (e.g. `torch==…+cu118`); the CPU set carries `…+cpu`. Local builds are pinned with
  `==` (compatible-release `~=` is invalid with a `+local` segment).

The Action runs it; you only need to run it locally to debug:

```bash
python .github/scripts/sync.py          # regenerate into the working tree
python .github/scripts/sync.py --check  # report drift / new versions, exit non-zero if any
python .github/scripts/sync.py --manifest  # sha256 manifest of python/ (no fetch)
```

`--manifest` prints one line per environment with its package count and the sha256
of each artifact — a network-free, deterministic fingerprint of the tree. Two trees
with identical manifests are byte-for-byte identical payloads, so it's the way to
verify reproducibility/portability: regenerate on a fresh repo, then diff its
manifest against this one.

This docs-parsing sync is an **interim** mechanism; the durable plan is for the
runtime/environments build pipeline to publish these files directly. See the design
doc for the full rationale.

## Status

- [x] Serverless (v1–vN) — auto-discovered + synced (`requirements-env-N.txt`)
- [x] DBR standard runtimes — auto-discovered from the index + HTML-table parsing
- [x] DBR ML runtimes (CPU + GPU) — downloadable requirements or inline tables
- [ ] PyTorch index config in ML `pyproject.toml` (so `uv` fetches the matching
      `+cpu` / `+cuXXX` torch build, not just pins it)
