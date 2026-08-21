"""Unit tests for envgen.py (run: python -m unittest test_envgen)."""
import unittest

from envgen import _filtered, build_constraints, build_pyproject, dbconnect_pin, req


class FilterNonLocalTest(unittest.TestCase):
    def test_drops_local_version_segment(self):
        # A PEP 440 local version segment (+cuNNN / +cpu / +db1) names a build
        # published only on an out-of-band index or rebuilt inside the image, so it
        # cannot resolve on a developer machine and must not be emitted.
        pkgs = {
            "torch": "2.9.0+cu129",
            "torchvision": "0.24.0+cu129",
            "flask": "1.1.2+db1",
            "horovod": "0.28.1+db1",
            "numpy": "2.1.3",
        }
        self.assertEqual(_filtered(pkgs), {"numpy": "2.1.3"})

    def test_drops_gpu_only_by_name(self):
        # Every nvidia-* distribution is a CUDA runtime component; triton /
        # flash-attn / deepspeed are GPU-only as well.
        pkgs = {
            "nvidia-cublas-cu12": "12.6.4.1",
            "nvidia-cudnn-cu12": "9.5.1.17",
            "triton": "3.3.0",
            "flash-attn": "2.7.4.post1",
            "deepspeed": "0.16.5",
            "numpy": "2.1.3",
        }
        self.assertEqual(_filtered(pkgs), {"numpy": "2.1.3"})

    def test_keeps_installable_pins(self):
        # A plain torch pin resolves to a CPU/macOS wheel; ray is on PyPI and usable
        # locally. Only the +local torch build is dropped, not torch itself.
        pkgs = {
            "torch": "2.7.0",
            "ray": "2.37.0",
            "databricks-sdk": "0.67.0",
            "numpy": "2.1.3",
            "pyarrow": "21.0.0",
        }
        self.assertEqual(_filtered(pkgs), pkgs)


class BuildArtifactsTest(unittest.TestCase):
    pkgs = {
        "numpy": "2.1.3",
        "torch": "2.9.0+cu129",
        "nvidia-cublas-cu12": "12.6.4.1",
        "triton": "3.3.0",
        "pyarrow": "21.0.0",
    }

    def test_pyproject_omits_dropped(self):
        out = build_pyproject(self.pkgs, "serverless-v4", "3.12.3")
        self.assertIn("numpy~=2.1.3", out)
        self.assertIn("pyarrow~=21.0.0", out)
        for gone in ("torch", "nvidia-cublas-cu12", "triton", "+cu129"):
            self.assertNotIn(gone, out)

    def test_constraints_omits_dropped(self):
        out = build_constraints(self.pkgs, "serverless-v4")
        self.assertIn("numpy~=2.1.3", out)
        self.assertIn("pyarrow~=21.0.0", out)
        for gone in ("torch", "nvidia-cublas-cu12", "triton", "+cu129"):
            self.assertNotIn(gone, out)


class ReqTest(unittest.TestCase):
    def test_compatible_release_default(self):
        self.assertEqual(req("numpy", "2.1.3"), "numpy~=2.1.3")

    def test_databricks_sdk_widened_to_major_minor(self):
        self.assertEqual(req("databricks-sdk", "0.67.0"), "databricks-sdk~=0.67")


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
