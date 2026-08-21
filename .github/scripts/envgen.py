"""Core transformation: official requirements list -> uv pyproject.toml + pip constraints.txt.

The published Databricks environment package list (one ``name==version`` per line,
as found in the release-notes "Installed Python libraries" section) is not used
verbatim. A consistent set of rules is applied so the artifacts install cleanly on
a developer machine:

  * Names normalized   - lowercased, ``_``/``.`` -> ``-`` (Cython -> cython).
  * ``==`` -> ``~=``    - allow security/patch bumps within a minor.
  * databricks-sdk     - widened to ``~=MAJOR.MINOR`` (``>=X.Y, <X+1``) instead of the
                         default single-patch-line pin, because databricks-connect
                         (installed from the dev group) declares its own databricks-sdk
                         floor which can be newer than the release-notes table lists.
                         See ``req`` for the full rationale.
  * databricks-connect - pinned to ``~=MAJOR.MINOR.0`` and emitted into
                         ``[dependency-groups].dev`` of the pyproject (installed by
                         default under uv); omitted from constraints.txt so the pip
                         path is constraints-only.
  * Non-installable    - system/OS packages and the Spark client bundle that cannot
    packages dropped     be pip-installed locally or that ship vendored inside
                         setuptools (see DROP / DROP_PREFIX). py4j is kept; pyspark
                         is dropped so DB Connect supplies its own bundled build.
  * Local segments     - a PEP 440 local version segment (``+cu129`` / ``+cpu`` /
    stripped             ``+db1``) is stripped and the base release pinned
                         (``torch 2.9.0+cu129`` -> ``torch~=2.9.0``); ``~=`` is
                         invalid with a local segment and the build exists only
                         off-index, while its base is an ordinary PyPI release. See ``req``.
  * GPU-only dropped   - ``nvidia-*`` CUDA wheels, triton, flash-attn and deepspeed
                         need a GPU a dev machine lacks (see DROP / DROP_PREFIX).
  * Env-scoped drops   - a pin dropped for specific environments only (DROP_BY_ENV)
                         when its version has no wheel for that env's Python and no
                         in-range version does either — e.g. pandas 1.5.3 on the
                         Python 3.12 runtimes DBR 16.4 / serverless-v3 (issue #18).
  * requires-python    - taken from the runtime's Python version (major.minor).

This module is imported by ``sync.py`` (the weekly discovery + reconciliation Action).
"""
import re

# Present in the environment image but not wanted as a local constraint: system
# libs, the spark client, pip itself, and deps vendored inside setuptools.
DROP = {
    "pyspark", "dbus-python", "pygobject", "pip", "unattended-upgrades",
    # Ubuntu system packages carried in the image but not pip-installable. Named
    # explicitly so they stay dropped even though req() now strips local segments
    # (their +ubuntu / +build local builds would otherwise resurrect as a base pin).
    "python-apt", "distro-info",
    # setuptools-vendored
    "autocommand", "inflect", "typeguard", "backports-tarfile",
    "importlib-resources", "more-itertools",
    # GPU-only: need an NVIDIA GPU + CUDA toolchain a dev machine does not have.
    # (The nvidia-* CUDA runtime libs are dropped by prefix below.) horovod is the
    # same class — a source-only distribution needing MPI/NCCL + a compiler to build.
    "triton", "flash-attn", "deepspeed", "horovod",
}
DROP_PREFIX = (
    "jaraco-",        # jaraco.collections / jaraco.context / ... (setuptools-vendored)
    "nvidia-",        # nvidia-* CUDA wheels (and nvidia-ml-py): GPU tooling, no local use
)

# Environment-scoped drops: removed for the named environments only (DROP is global).
# pandas 1.5.3 has no cp312 wheel — pandas ships CPython 3.12 wheels only from 2.1.1 —
# so `uv sync` on a Python 3.12 runtime can't install it and falls back to a failing
# source build. DBR 16.4 and serverless-v3 are the only 3.12 runtimes still pinned to
# pandas 1.5.x: 13.3/14.3/15.4 predate 3.12, and 17.3+ / serverless-v4+ already ship
# pandas 2.x (which has cp312 wheels). No in-range (~=1.5) version has a cp312 wheel,
# so the pin can't be salvaged by widening — it is dropped for just these envs, and
# pandas resolves to an installable version locally. See issue #18.
DROP_BY_ENV = {
    "16.4.x-scala2.12": {"pandas"},
    "16.4.x-cpu-ml-scala2.12": {"pandas"},
    "16.4.x-gpu-ml-scala2.12": {"pandas"},
    "serverless-v3": {"pandas"},
}


def norm(name):
    # Strip the '*' footnote marker the release-notes tables append to some package
    # names ('*' is not legal in a PEP 508 distribution name).
    return name.strip().lower().replace("*", "").strip().replace("_", "-").replace(".", "-")


def req(name, version):
    """Render one requirement. Compatible-release ``~=`` allows patch bumps.

    A PEP 440 local version segment (``+cpu`` / ``+cu118`` / ``+db1``) is stripped:
    ``~=`` is invalid with a local segment, and the segment names a build published
    only off-index (``download.pytorch.org``) or rebuilt inside the image, while its
    base release is an ordinary PyPI version. So the base is pinned instead
    (``torch 2.9.0+cu129`` -> ``torch~=2.9.0``, ``flask 1.1.2+db1`` -> ``flask~=1.1.2``).
    Ubuntu system builds like ``python-apt 2.7.7+ubuntu5.2`` would strip to a bogus
    PyPI pin, so those are dropped by name in ``DROP`` before reaching here.

    ``databricks-sdk`` is a special case. It moves in lockstep with
    ``databricks-connect``, which is installed from PyPI in the dev group and declares
    its own ``databricks-sdk`` floor — and that floor can be *newer* than the version
    the release-notes "Installed Python libraries" table lists (e.g. DBR 18.2 lists
    ``databricks-sdk 0.67.0``, but ``databricks-connect~=18.2.0`` requires
    ``databricks-sdk>=0.93,<1``). Pinning it to a single patch line (``~=0.67.0`` →
    ``>=0.67.0,<0.68``) then makes ``uv sync`` unresolvable. Widening it to its
    ``MAJOR.MINOR`` (``~=0.67`` → ``>=0.67,<1``) keeps the runtime's version as the
    floor while letting databricks-connect's own metadata govern the exact version
    within that window. See issue #16.
    """
    version = version.split("+", 1)[0]
    if name == "databricks-sdk":
        version = ".".join(version.split(".")[:2])
    return f"{name}~={version}"


def parse_requirements(text):
    """Parse ``name==version`` lines into {normalized_name: version}."""
    pkgs = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        if m:
            pkgs[norm(m.group(1))] = m.group(2)
    return pkgs


def _filtered(pkgs, env_name=None):
    # Inclusion only: drop the global name-based DROP set and DROP_PREFIX, plus any
    # environment-scoped drops (DROP_BY_ENV) for env_name. A PEP 440 local version
    # segment is NOT a reason to drop — its base release is on PyPI — so those pins
    # are kept here and req() strips the segment when rendering.
    env_drop = DROP_BY_ENV.get(env_name, frozenset())
    return {n: v for n, v in pkgs.items()
            if n not in DROP and n not in env_drop and not n.startswith(DROP_PREFIX)}


def dbconnect_pin(pkgs):
    """Return the dev-group databricks-connect requirement, or None.

    Serverless lists a concrete databricks-connect (e.g. '17.3.1'), but a serverless
    environment version tracks a whole major line, not a single point release. So the
    pin is by bare major — ``~=MAJOR.0`` (e.g. '17.3.1' -> ``databricks-connect~=17.0``,
    resolving ``>=17.0, <18.0``) — to pick up the latest release within that major.
    """
    v = pkgs.get("databricks-connect")
    if not v:
        return None
    return f"databricks-connect~={v.split('.')[0]}.0"


def build_pyproject(pkgs, env_name, python_version, dbconnect=None):
    """Build the pyproject.toml text.

    ``dbconnect`` optionally overrides the dev-group databricks-connect pin with a
    ``MAJOR.MINOR`` string (e.g. "17.3"). Serverless pages list databricks-connect
    in the package set, so the default (derive from pkgs) works there. DBR pages do
    not list it — the matching version is the runtime version, passed in explicitly.
    """
    mm = ".".join(python_version.split(".")[:2])     # 3.12.3 -> 3.12
    body = {n: v for n, v in _filtered(pkgs, env_name).items() if n != "databricks-connect"}
    dev = f"databricks-connect~={dbconnect}.0" if dbconnect else dbconnect_pin(pkgs)
    project = "constraint-env-" + re.sub(r"[^a-z0-9]+", "-", env_name.lower()).strip("-")
    out = [
        f"# pyproject.toml file for Databricks {_label(env_name)}",
        "",
        "[project]",
        f'name = "{project}"',
        'version = "0.1.0"',
        f'requires-python = "=={mm}.*"',
        "",
        "[dependency-groups]",
        "dev = [",
        *([f'    "{dev}",'] if dev else []),
        "]",
        "",
        "[tool.uv]",
        "constraint-dependencies = [",
        *[f'    "{req(n, body[n])}",' for n in sorted(body)],
        "]",
    ]
    return "\n".join(out) + "\n"


def build_constraints(pkgs, env_name):
    body = {n: v for n, v in _filtered(pkgs, env_name).items() if n != "databricks-connect"}
    out = [f"# constraints.txt file for Databricks {_label(env_name)}", ""]
    out += [req(n, body[n]) for n in sorted(body)]
    return "\n".join(out) + "\n"


def _label(env_name):
    m = re.fullmatch(r"serverless-v(\d+)(-ml)?", env_name)
    if m:
        return f"Serverless environment version {m.group(1)}" + (" (ML)" if m.group(2) else "")
    # Point-release folders are '18.2.x-...'; umbrella LTS folders drop the minor
    # to match a cluster's spark_version ('19.x-...'). Match both.
    if re.match(r"\d+(\.\d+)?\.x", env_name):
        return f"Runtime {env_name}"
    return env_name.replace("-", " ")
