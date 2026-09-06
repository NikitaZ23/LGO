import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image
import trimesh

from lgo.settings import load_config
from scripts import run_hunyuan_job as runner


class TextureFidelityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config, _ = runner._apply_object_type_preset(load_config(), {"object_type": "hard_surface"})
        self.config["postprocess"]["shade_smooth"]["enabled"] = False
        self.shape = trimesh.Trimesh(vertices=[[0, 0, 0], [3, 0, 0], [3, 2, 0], [0, 2, 0]],
                                     faces=[[0, 1, 2], [0, 2, 3]], process=False)
        self.source = self.shape.copy()
        self.uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=float)
        self.source.visual = trimesh.visual.TextureVisuals(uv=self.uv)
        self.obj = self.root / "textured_mesh.obj"
        self.source.export(self.obj)
        self.albedo = self.root / "textured_mesh.png"
        self.pixels = np.zeros((32, 32, 3), dtype=np.uint8)
        self.pixels[::2, ::2] = [180, 112, 32]
        self.pixels[1::2, :] = [24, 28, 35]
        Image.fromarray(self.pixels).save(self.albedo)
        Image.new("L", (32, 32), 160).save(self.root / "textured_mesh_metallic.jpg")
        Image.new("L", (32, 32), 75).save(self.root / "textured_mesh_roughness.jpg")
        self.output = self.root / "result.glb"

    def bake(self, config=None, require_uv=True):
        return runner._bake_texture_to_shape_mesh(self.shape, self.obj, self.albedo, self.output,
                                                  config or self.config, shade_smooth=False, require_uv=require_uv)

    def test_presets_preserve_details_and_do_not_change_colors_automatically(self):
        for name in ("hard_surface", "building"):
            config, _ = runner._apply_object_type_preset(load_config(), {"object_type": name})
            self.assertTrue(config["generation"]["texture_preserve_geometry"])
            self.assertFalse(config["postprocess"]["smooth"]["enabled"])
            adjusted, _ = runner._adjust_baked_vertex_colors(self.pixels.reshape(-1, 3), config, None)
            np.testing.assert_array_equal(adjusted, self.pixels.reshape(-1, 3))
            prompt, _ = runner._prepare_texture_prompt_image(Image.fromarray(self.pixels), config, self.root)
            np.testing.assert_array_equal(np.asarray(prompt.convert("RGB")), self.pixels)
            self.assertFalse(runner._stabilize_pbr_textures({}, config)["enabled"])

    def test_uv_export_preserves_high_frequency_pixels_and_pbr_maps(self):
        original_hash = hashlib.sha256(self.albedo.read_bytes()).digest()
        with patch.object(runner, "_transfer_albedo_by_nearest_surface", side_effect=AssertionError("No vertex bake")):
            report = self.bake()
        self.assertTrue(report["applied"], report)
        self.assertEqual(report["method"], "original_geometry_uv")
        mesh = runner._load_single_mesh(self.output)
        self.assertIsNotNone(runner._match_uv_geometry(self.shape, mesh))
        self.assertEqual(mesh.visual.kind, "texture")
        material = mesh.visual.material
        np.testing.assert_array_equal(np.asarray(material.baseColorTexture)[:, :, :3], self.pixels)
        packed = np.asarray(material.metallicRoughnessTexture)
        self.assertTrue(np.all(packed[:, :, 1] == 75))
        self.assertTrue(np.all(packed[:, :, 2] == 160))
        self.assertEqual(material.metallicFactor, 1)
        self.assertEqual(hashlib.sha256(self.albedo.read_bytes()).digest(), original_hash)

    def test_rebake_uses_albedo_and_color_without_cumulative_changes(self):
        config, _ = runner._apply_rebake_albedo_override(self.config, {"rebake_albedo": 0.5})
        config, _ = runner._apply_texture_color_override(config, {"texture_color": 0.35})
        for _ in range(2):
            self.assertTrue(self.bake(config)["applied"])
            pixels = np.asarray(runner._load_single_mesh(self.output).visual.material.baseColorTexture)
            luma = runner._rgb_luma(self.pixels.reshape(-1, 3)).reshape(32, 32, 1)
            expected = np.rint((luma + (self.pixels - luma) * 0.35) * 0.5)
            np.testing.assert_array_equal(pixels, expected)
        self.assertTrue(self.bake()["applied"])
        np.testing.assert_array_equal(np.asarray(runner._load_single_mesh(self.output).visual.material.baseColorTexture), self.pixels)

    def test_geometry_match_accepts_uv_seams_reordered_faces_and_small_roundoff(self):
        split = trimesh.Trimesh(vertices=self.shape.vertices[self.shape.faces].reshape(-1, 3) + 1e-8,
                                faces=[[3, 4, 5], [1, 2, 0]], process=False)
        aligned = runner._match_uv_geometry(self.shape, split)
        self.assertIsNotNone(aligned)
        np.testing.assert_array_equal(aligned, self.shape.vertices[self.shape.faces].reshape(-1, 3))

    def test_geometry_match_rejects_changed_shape_topology_and_winding(self):
        displaced = self.source.copy()
        displaced.vertices[1, 2] += 0.1
        reversed_mesh = self.source.copy()
        reversed_mesh.faces = reversed_mesh.faces[:, ::-1]
        different_faces = self.source.copy()
        different_faces.faces = [[0, 1, 3], [1, 2, 3]]
        for changed in (displaced, reversed_mesh, different_faces):
            self.assertIsNone(runner._match_uv_geometry(self.shape, changed))

    def test_mismatch_or_failed_export_keeps_previous_result(self):
        self.output.write_bytes(b"previous result")
        self.source.vertices[1, 2] += 0.2
        self.source.export(self.obj)
        self.assertFalse(self.bake()["applied"])
        self.assertEqual(self.output.read_bytes(), b"previous result")
        self.source = self.shape.copy()
        self.source.visual = trimesh.visual.TextureVisuals(uv=self.uv)
        self.source.export(self.obj)
        with patch("trimesh.Trimesh.export", side_effect=RuntimeError("disk full")), patch.object(runner.traceback, "print_exc"):
            self.assertFalse(self.bake()["applied"])
        self.assertEqual(self.output.read_bytes(), b"previous result")
        self.assertEqual(list(self.root.glob("*.tmp.glb")), [])

    def test_legacy_vertex_color_fallback_encodes_linear_not_srgb(self):
        np.testing.assert_array_equal(runner._srgb_to_linear_vertex_colors([[0, 128, 255]]), [[0, 55, 255]])
        Image.new("RGB", (32, 32), (128, 128, 128)).save(self.albedo)
        with patch.object(runner, "_match_uv_geometry", return_value=None):
            report = self.bake(require_uv=False)
        self.assertTrue(report["applied"], report)
        self.assertEqual(report["color"]["vertex_color_space"], "linear")
        self.assertTrue(any("Legacy" in item for item in report["warnings"]))
        mesh = runner._load_single_mesh(self.output)
        np.testing.assert_array_equal(mesh.visual.vertex_attributes["color"][:, :3], np.full((4, 3), 55))
        self.assertEqual(mesh.visual.material.metallicFactor, 0)

    @unittest.skipUnless(os.environ.get("LGO_TEST_BLENDER") == "1", "Optional Blender export test")
    def test_blender_conversion_preserves_uv_geometry_and_pbr(self):
        self.assertTrue(self.bake()["applied"])
        target = self.root / "converted.glb"
        converter = Path(__file__).resolve().parents[1] / "tools" / "blender_convert.py"
        subprocess.run([self.config["paths"]["blender"], "--background", "--factory-startup",
                        "--python-exit-code", "1", "--python", str(converter), "--", str(self.output), str(target)],
                       capture_output=True, text=True, check=True, timeout=120)
        converted = runner._load_single_mesh(target)
        self.assertIsNotNone(runner._match_uv_geometry(self.shape, converted))
        np.testing.assert_array_equal(np.asarray(converted.visual.material.baseColorTexture)[:, :, :3], self.pixels)
        self.assertIn(converted.visual.material.metallicFactor, (None, 1))
        packed = np.asarray(converted.visual.material.metallicRoughnessTexture)
        self.assertTrue(np.all(packed[:, :, 1] == 75))
        self.assertTrue(np.all(packed[:, :, 2] == 160))


if __name__ == "__main__":
    unittest.main()
