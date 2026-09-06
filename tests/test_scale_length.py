import copy
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import numpy as np

from lgo.generation import GenerationService
from lgo.jobs import JobStore
from lgo.settings import load_config
from lgo_server import _target_length_value as parse_api_length
from lgo_server import LGOHandler
from scripts import run_hunyuan_job as runner


class BoundsMesh:
    def __init__(self, bounds):
        self.vertices = np.asarray(bounds, dtype=float)

    @property
    def bounds(self):
        return np.array([self.vertices.min(axis=0), self.vertices.max(axis=0)])

    def copy(self):
        return copy.deepcopy(self)

    def apply_scale(self, factors):
        self.vertices *= factors


class ScaleLengthTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.mesh = BoundsMesh([[-1, -1.5, -2], [1, 1.5, 2]])

    def scale(self, length=None):
        payload = {"scale_preset": "custom", "target_height_m": 6, "target_length_m": length}
        return runner._resolve_scale_settings(self.config, payload)

    def test_length_changes_only_longitudinal_axis_after_height_scale(self):
        mesh, report = runner._apply_mesh_scale(self.mesh, self.scale(10))
        np.testing.assert_allclose(np.diff(mesh.bounds, axis=0)[0], [4, 6, 10])
        np.testing.assert_allclose(np.diff(self.mesh.bounds, axis=0)[0], [2, 3, 4])
        self.assertEqual(report["axis_scale_factors"], [2, 2, 2.5])
        self.assertEqual(report["length_axis"], "z")
        self.assertEqual(report["final_length_m"], 10)

    def test_disabled_dimensions_ignore_even_invalid_stale_values(self):
        payload = {"apply_dimensions": False, "scale_preset": "custom",
                   "target_height_m": "bad", "target_length_m": "NaN"}
        scale = runner._resolve_scale_settings(self.config, payload)
        mesh, report = runner._apply_mesh_scale(self.mesh, scale)
        self.assertIs(mesh, self.mesh)
        self.assertFalse(report["applied"])
        self.assertFalse(scale["apply_dimensions"])
        self.assertIsNone(payload["target_height_m"])
        self.assertIsNone(payload["target_length_m"])
        np.testing.assert_array_equal(mesh.bounds, [[-1, -1.5, -2], [1, 1.5, 2]])

    def test_disabled_scale_never_reads_or_copies_geometry(self):
        sentinel = object()
        mesh, report = runner._apply_mesh_scale(sentinel, {"apply_dimensions": False})
        self.assertIs(mesh, sentinel)
        self.assertFalse(report["applied"])

    def test_api_flag_bypasses_dimensions_and_keeps_legacy_compatibility(self):
        cases = [("false", "bad", "NaN", 201), ("true", "6", "10", 201),
                 (None, "6", "10", 201), ("true", "6", "NaN", 400)]
        for flag, height, length, status in cases:
            with self.subTest(flag=flag, length=length), tempfile.TemporaryDirectory() as directory:
                fields = {"mode": "single", "scale_preset": "custom",
                          "target_height_m": height, "target_length_m": length}
                if flag is not None:
                    fields["apply_dimensions"] = flag
                body = urlencode(fields).encode()
                handler = LGOHandler.__new__(LGOHandler)
                handler.path = "/api/jobs"
                handler.headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))}
                handler.rfile = io.BytesIO(body)
                handler._json = Mock()
                handler._save_inputs = Mock()
                generator = Mock()
                generator.can_run_real_generation.return_value = False
                with patch("lgo_server.STORE", JobStore(Path(directory))), patch("lgo_server.GENERATOR", generator):
                    handler.do_POST()
                data, code = handler._json.call_args.args
                self.assertEqual(code, status)
                generator.start.assert_not_called()
                if status == 201:
                    payload = data["payload"]
                    enabled = flag != "false"
                    self.assertEqual(payload["apply_dimensions"], enabled)
                    self.assertEqual(payload["target_height_m"], 6 if enabled else None)
                    self.assertEqual(payload["target_length_m"], 10 if enabled else None)

    def test_auto_length_keeps_legacy_uniform_scale(self):
        for length in (None, "", "   "):
            with self.subTest(length=length):
                mesh, report = runner._apply_mesh_scale(self.mesh, self.scale(length))
                np.testing.assert_allclose(np.diff(mesh.bounds, axis=0)[0], [4, 6, 8])
                self.assertIsNone(report["target_length_m"])

    def test_length_can_shorten_model_without_changing_height(self):
        mesh, _ = runner._apply_mesh_scale(self.mesh, self.scale(1))
        np.testing.assert_allclose(np.diff(mesh.bounds, axis=0)[0], [4, 6, 1])

    def test_length_works_with_preset_height(self):
        payload = {"scale_preset": "building", "target_height_m": 3, "target_length_m": 60}
        scale = runner._resolve_scale_settings(self.config, payload)
        self.assertEqual(scale["target_height_m"], 24)
        self.assertEqual(scale["target_length_m"], 60)
        mesh, _ = runner._apply_mesh_scale(self.mesh, scale)
        np.testing.assert_allclose(np.diff(mesh.bounds, axis=0)[0], [16, 24, 60])

    def test_alternative_vertical_axis_does_not_collide_with_length(self):
        self.config["scale_presets"]["custom"]["vertical_axis"] = "z"
        scale = self.scale(10)
        self.assertEqual(scale["length_axis"], "x")
        mesh, _ = runner._apply_mesh_scale(self.mesh, scale)
        np.testing.assert_allclose(np.diff(mesh.bounds, axis=0)[0], [10, 4.5, 6])

    def test_degenerate_length_axis_is_not_silently_ignored(self):
        flat = BoundsMesh([[0, 0, 0], [2, 3, 0]])
        with self.assertRaisesRegex(ValueError, "no extent"):
            runner._apply_mesh_scale(flat, self.scale(10))

    def test_api_and_runner_validate_the_same_length_range(self):
        for parse in (parse_api_length, runner._target_length_value):
            for raw, expected in ((None, None), ("", None), ("12,5", 12.5), ("0.01", 0.01), ("10000", 10000)):
                with self.subTest(parser=parse.__module__, raw=raw):
                    self.assertEqual(parse(raw), expected)
            for raw in ("bad", "NaN", "Infinity", "-Infinity", 0, -1, 0.001, 10001):
                with self.subTest(parser=parse.__module__, invalid=raw), self.assertRaises(ValueError):
                    parse(raw)

    def test_length_survives_manifest_history_and_texture_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            payload = {"mode": "single", "scale_preset": "custom", "target_height_m": 6,
                       "target_length_m": 10, "texture": True, "formats": ["glb"], "input_files": {}}
            job = store.create(payload)
            manifest = GenerationService(self.config).prepare(job)
            self.assertEqual(manifest["target_length_m"], 10)
            self.assertEqual(store.summarize(job)["target_length_m"], 10)
            output_dir = Path(job["output_dir"])
            (output_dir / "textured_mesh.glb").write_bytes(b"snapshot fixture")
            snapshot = runner._snapshot_texture_version(Path(job["run_dir"]) / "job.json", output_dir,
                                                        "reworked", payload, None, None)
            self.assertEqual(snapshot["target_length_m"], 10)
            job["texture_versions"] = [snapshot]
            self.assertEqual(store.summarize(job)["texture_versions"][0]["target_length_m"], 10)
            snapshot["target_length_m"] = None
            self.assertIsNone(store.summarize(job)["texture_versions"][0]["target_length_m"])
            del payload["target_length_m"]
            self.assertIsNone(store.summarize(job)["target_length_m"])

    def test_dimension_flag_survives_manifest_history_and_texture_snapshot(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                store = JobStore(Path(directory))
                payload = {"mode": "single", "apply_dimensions": enabled, "texture": True,
                           "formats": ["glb"], "input_files": {}}
                job = store.create(payload)
                self.assertEqual(GenerationService(self.config).prepare(job)["apply_dimensions"], enabled)
                self.assertEqual(store.summarize(job)["apply_dimensions"], enabled)
                output_dir = Path(job["output_dir"])
                (output_dir / "textured_mesh.glb").write_bytes(b"snapshot fixture")
                snapshot = runner._snapshot_texture_version(Path(job["run_dir"]) / "job.json", output_dir,
                                                            "reworked", payload, None, None)
                self.assertEqual(snapshot["apply_dimensions"], enabled)
                job["texture_versions"] = [snapshot]
                payload["apply_dimensions"] = not enabled
                self.assertEqual(store.summarize(job)["texture_versions"][0]["apply_dimensions"], enabled)
                del payload["apply_dimensions"]
                self.assertTrue(store.summarize(job)["apply_dimensions"])


if __name__ == "__main__":
    unittest.main()
