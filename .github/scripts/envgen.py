"""Core transformation: official requirements list -> uv pyproject.toml + pip constraints.txt.

The published Databricks environment package list (one ``name==version`` per line,
as found in the release-notes "Installed Python libraries" section) is not used
verbatim. A consistent set of rules is applied so the artifacts install cleanly on
a developer machine:

  * Names normalized   - lowercased, ``_``/``.`` -> ``-`` (Cython -> cython).
  * ``==`` -> ``~=``    - allow security/patch bumps within a minor.
  * databricks-connect - pinned to ``~=MAJOR.MINOR.0`` and emitted into
                         ``[dependency-groups].dev`` of the pyproject (installed by
                         default under uv); omitted from constraints.txt so the pip
                         path is constraints-only.
  * Non-installable    - system/OS packages and the Spark client bundle that cannot
    packages dropped     be pip-installed locally or that ship vendored inside
                         setuptools (see DROP / DROP_PREFIX). py4j is kept; pyspark
                         is dropped so DB Connect supplies its own bundled build.
  * requires-python    - taken from the runtime's Python version (major.minor).

This module is imported by ``sync.py`` (the weekly discovery + reconciliation Action).
"""
import re

# Present in the environment image but not wanted as a local constraint: system
# libs, the spark client, pip itself, and deps vendored inside setuptools.
DROP = {
    "pyspark", "dbus-python", "pygobject", "pip", "unattended-upgrades",
    # setuptools-vendored
    "autocommand", "inflect", "typeguard", "backports-tarfile",
    "importlib-resources", "more-itertools",
}
DROP_PREFIX = ("jaraco-",)        # jaraco.collections / jaraco.context / ...


def norm(name):
    # Strip the '*' footnote marker the release-notes tables append to some package
    # names ('*' is not legal in a PEP 508 distribution name).
    return name.strip().lower().replace("*", "").strip().replace("_", "-").replace(".", "-")


def req(name, version):
    """Render one requirement. Compatible-release ``~=`` allows patch bumps, but it
    is invalid with a local version segment (PEP 440), and a local build like
    ``+cpu`` / ``+cu118`` / ``+db1`` is exactly what distinguishes CPU vs GPU ML
    images and Databricks-patched packages — so those are pinned exactly with ``==``.
    """
    return f"{name}=={version}" if "+" in version else f"{name}~={version}"


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


def _filtered(pkgs):
    return {n: v for n, v in pkgs.items()
            if n not in DROP and not n.startswith(DROP_PREFIX)}


def dbconnect_pin(pkgs):
    """Return the dev-group databricks-connect requirement, or None."""
    v = pkgs.get("databricks-connect")
    if not v:
        return None
    return f"databricks-connect~={'.'.join(v.split('.')[:2])}.0"


def build_pyproject(pkgs, env_name, python_version, dbconnect=None):
    """Build the pyproject.toml text.

    ``dbconnect`` optionally overrides the dev-group databricks-connect pin with a
    ``MAJOR.MINOR`` string (e.g. "17.3"). Serverless pages list databricks-connect
    in the package set, so the default (derive from pkgs) works there. DBR pages do
    not list it — the matching version is the runtime version, passed in explicitly.
    """
    mm = ".".join(python_version.split(".")[:2])     # 3.12.3 -> 3.12
    body = {n: v for n, v in _filtered(pkgs).items() if n != "databricks-connect"}
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
    body = {n: v for n, v in _filtered(pkgs).items() if n != "databricks-connect"}
    out = [f"# constraints.txt file for Databricks {_label(env_name)}", ""]
    out += [req(n, body[n]) for n in sorted(body)]
    return "\n".join(out) + "\n"


def _label(env_name):
    m = re.fullmatch(r"serverless-v(\d+)(-ml)?", env_name)
    if m:
        return f"Serverless environment version {m.group(1)}" + (" (ML)" if m.group(2) else "")
    if re.match(r"\d+\.\d+\.x", env_name):
        return f"Runtime {env_name}"
    return env_name.replace("-", " ")
