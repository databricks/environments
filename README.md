# databricks-environments

![Status: Work in Progress](https://img.shields.io/badge/status-work%20in%20progress-orange)

> ⚠️ **Work in progress.** This repo is under active development — its layout,
> artifact format, and sync mechanism may change dramatically over the coming weeks.
> Don't depend on anything here being stable yet.

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
    serverless-v5/                 # standard serverless
    serverless-v5-ml/              # ML serverless base environment (v5+)
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
Databricks release notes — see [what is intentionally not included](#what-is-intentionally-not-included) and
`.github/scripts/envgen.py` for the rules.

## What is intentionally not included

Not every package in the release-notes list is emitted verbatim. `envgen.py` drops
the ones that can't — or shouldn't — install on a developer machine, and strips
version markers that would make a pin unresolvable, so `uv sync` / `pip install -c`
stay resolvable. Applied to **both** `pyproject.toml` and `constraints.txt`:

- **System / OS packages** (dropped) — `pip`, `pyspark` (DB Connect supplies its own
  bundled build; `py4j` is kept), `dbus-python`, `pygobject`, `unattended-upgrades`,
  `python-apt`, `distro-info`.
- **setuptools-vendored** (dropped) — `more-itertools`, `jaraco-*`, `inflect`,
  `typeguard`, … — shipped inside setuptools, not installed standalone.
- **GPU-only distributions** (dropped) — the `nvidia-*` CUDA runtime components, plus
  `triton`, `flash-attn`, `deepspeed`, `horovod`: they need an NVIDIA GPU (and a CUDA
  toolchain / MPI to build) a dev machine lacks. (`nvidia-ml-py` is pure Python but
  useless without a driver, and is dropped by the same `nvidia-` prefix.)
- **Local version segments** (stripped, not dropped) — a `+cpu` / `+cuXXX` / `+db1`
  segment names a build published only off-index (`download.pytorch.org`) or rebuilt
  inside the image, and `~=` is invalid with a local segment. The segment is stripped
  and the base release pinned (`torch 2.9.0+cu129` → `torch~=2.9.0`, `flask 1.1.2+db1`
  → `flask~=1.1.2`), so `uv` resolves a platform-appropriate wheel. (Ubuntu system
  builds like `python-apt 2.7.7+ubuntu5.2` are dropped by name above instead, since
  their base version is not on PyPI.)
- **Environment-scoped drops** (dropped for named envs only) — a pin whose version
  has no wheel for that environment's Python, where no in-range version has one
  either. Today this is **`pandas` on DBR 16.4 and serverless-v3**: they are the only
  Python-3.12 runtimes still pinned to `pandas 1.5.3`, which has no cp312 wheel (pandas
  ships 3.12 wheels only from 2.1.1, so `~=1.5` can't reach one). The constraint is
  dropped for just those envs, letting `pandas` resolve to an installable version
  locally. Earlier runtimes (13.3/14.3/15.4) predate 3.12, and 17.3+ / serverless-v4+
  already ship pandas 2.x.

The exact lists live in `DROP` / `DROP_PREFIX` / `DROP_BY_ENV` and `_filtered()` /
`req()` in `.github/scripts/envgen.py`.

## How it stays in sync

A scheduled GitHub Action (`.github/workflows/sync.yml`) is the only mechanism that
maintains this repo. Weekly (and on-demand via *Run workflow*) it runs `.github/scripts/sync.py`
to regenerate every environment from the release notes, reconciles against what's
committed, and **opens a PR** when an environment drifts or a new version appears. A
maintainer reviews and merges that PR — the deliberate human gate, since docs parsing
is best-effort. Nobody hand-edits the `python/` artifacts.

`.github/scripts/sync.py` does the regeneration + reconciliation:

- **Serverless** — discovers the published environment versions and downloads each
  `requirements-env-N.txt`. When a version also publishes an ML base environment
  (`requirements-ml-N.txt`, serverless v5+), a separate `serverless-vN-ml` env is
  produced alongside the standard one.
- **DBR** — enumerates the standard runtime versions from the
  [runtime release-notes index](https://docs.databricks.com/aws/en/release-notes/runtime/),
  then for each fetches the page and parses the "Installed Python libraries" HTML
  table. The repo key (`<ver>.x-scala<scala>`) is built from the page's title and the
  Scala version in its System environment. DBR pages don't list `databricks-connect`,
  so its dev pin is derived from the runtime version.
- **DBR ML (CPU + GPU)** — for each `*-ml` runtime, a separate environment is produced
  per cluster type: `<ver>.x-cpu-ml-…` and `<ver>.x-gpu-ml-…`. Newer ML pages link
  downloadable `requirements-{cpu,gpu}-*.txt`; older ones render inline tables under
  `python-libraries-on-{cpu,gpu}-clusters`. The GPU set lists the CUDA builds
  (e.g. `torch …+cu118`) and the CPU set lists `…+cpu`; the generated artifacts strip
  the `+local` segment and pin the base release (see
  [what is intentionally not included](#what-is-intentionally-not-included)).

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

- [x] Serverless (v1–vN) — auto-discovered + synced; ML base environment (`-ml`) when published (v5+)
- [x] DBR standard runtimes — auto-discovered from the index + HTML-table parsing
- [x] DBR ML runtimes (CPU + GPU) — downloadable requirements or inline tables
- [ ] PyTorch index config in ML `pyproject.toml`. Today the `+cpu` / `+cuXXX`
      torch/torchvision builds are stripped to a base pin (see [what is intentionally not included](#what-is-intentionally-not-included));
      adding PyTorch's index would let `uv` fetch the exact `+cpu` / `+cuXXX` build the
      runtime ships, rather than a base-version wheel.
