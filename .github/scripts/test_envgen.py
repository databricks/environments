"""Unit tests for envgen.py (run: python -m unittest test_envgen)."""
import unittest

from envgen import _filtered, build_constraints, build_pyproject, dbconnect_pin, req


class FilterTest(unittest.TestCase):
    def test_keeps_local_version_packages(self):
        # A PEP 440 local version segment (+cu129 / +cpu / +db1) is NOT a reason to
        # drop: the base release is on PyPI, so the pin is kept and the segment is
        # stripped later by req(). Only GPU-only and system packages are dropped.
        pkgs = {"torch": "2.9.0+cu129", "flask": "1.1.2+db1", "numpy": "2.1.3"}
        self.assertEqual(_filtered(pkgs), pkgs)

    def test_drops_gpu_only_by_name(self):
        # Every nvidia-* distribution is a CUDA runtime component; triton /
        # flash-attn / deepspeed are GPU-only as well. _filtered receives keys already
        # PEP 503-normalized by parse_requirements/norm, so it compares lowercased.
        pkgs = {
            "nvidia-cublas-cu12": "12.6.4.1",
            "nvidia-cudnn-cu12": "9.5.1.17",
            "triton": "3.3.0",
            "flash-attn": "2.7.4.post1",
            "deepspeed": "0.16.5",
            "horovod": "0.28.1",
            "numpy": "2.1.3",
        }
        self.assertEqual(_filtered(pkgs), {"numpy": "2.1.3"})

    def test_drops_system_packages(self):
        # Ubuntu system packages carried in the image but not pip-installable. They
        # must be dropped by name so stripping local segments does not resurrect them.
        pkgs = {"python-apt": "2.7.7+ubuntu5.2", "distro-info": "1.7+build1", "numpy": "2.1.3"}
        self.assertEqual(_filtered(pkgs), {"numpy": "2.1.3"})

    def test_keeps_installable_pins(self):
        pkgs = {"ray": "2.37.0", "databricks-sdk": "0.67.0", "numpy": "2.1.3", "pyarrow": "21.0.0"}
        self.assertEqual(_filtered(pkgs), pkgs)


class ReqTest(unittest.TestCase):
    def test_strips_local_version_segment(self):
        # ~= is invalid with a local segment (PEP 440), and the segment names a build
        # that only exists off-index; strip it so the base release is pinned instead.
        self.assertEqual(req("torch", "2.9.0+cu129"), "torch~=2.9.0")
        self.assertEqual(req("torch", "2.7.0+cpu"), "torch~=2.7.0")
        self.assertEqual(req("flask", "1.1.2+db1"), "flask~=1.1.2")

    def test_compatible_release_default(self):
        self.assertEqual(req("numpy", "2.1.3"), "numpy~=2.1.3")

    def test_databricks_sdk_widened_to_major_minor(self):
        self.assertEqual(req("databricks-sdk", "0.67.0"), "databricks-sdk~=0.67")


class BuildArtifactsTest(unittest.TestCase):
    # torchmetrics is a real ML package that must survive; it also guards against a
    # bare-substring assertion mistaking "torch~=..." for "torchmetrics".
    pkgs = {
        "numpy": "2.1.3",
        "torch": "2.9.0+cu129",
        "torchmetrics": "1.6.0",
        "nvidia-cublas-cu12": "12.6.4.1",
        "triton": "3.3.0",
        "python-apt": "2.7.7+ubuntu5.2",
        "pyarrow": "21.0.0",
    }

    def _check(self, out):
        # +local stripped and pinned; ordinary pins kept.
        self.assertIn("torch~=2.9.0", out)
        self.assertIn("torchmetrics~=1.6.0", out)
        self.assertIn("numpy~=2.1.3", out)
        self.assertIn("pyarrow~=21.0.0", out)
        self.assertNotIn("+cu129", out)
        # Dropped by name — assert on the rendered pin, not a bare substring.
        self.assertNotIn("nvidia-cublas-cu12~=", out)
        self.assertNotIn("triton~=", out)
        self.assertNotIn("python-apt~=", out)

    def test_pyproject(self):
        self._check(build_pyproject(self.pkgs, "serverless-v4", "3.12.3"))

    def test_constraints(self):
        self._check(build_constraints(self.pkgs, "serverless-v4"))


class DbconnectPinTest(unittest.TestCase):
    def test_strips_local_version_segment(self):
        # databricks-connect is installed from the dev group as a plain PyPI release;
        # the pin is normalized to ~=MAJOR.0, so a local segment in the release-notes
        # version is discarded and never lands in an artifact. dbconnect_pin reads raw
        # pkgs (not _filtered), so this guards that the normalization does the stripping.
        self.assertEqual(
            dbconnect_pin({"databricks-connect": "17.3.1+db1"}),
            "databricks-connect~=17.0",
        )

    def test_none_when_absent(self):
        self.assertIsNone(dbconnect_pin({"numpy": "2.1.3"}))


if __name__ == "__main__":
    unittest.main()
