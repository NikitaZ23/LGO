import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from lgo.exports import cached_export, convert_export, export_source, file_hash, prepare_export
from lgo.jobs import JobStore
from lgo.settings import load_config, PROJECT_ROOT
from scripts import run_hunyuan_job as runner
import lgo_server as server


class ExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = JobStore(self.root / "runs")
        self.job = self.store.create({"mode": "single", "formats": ["glb"]})
        self.output = Path(self.job["output_dir"])
        self.source = self.output / "white_mesh.glb"
        self.source.write_bytes(b"synthetic GLB")
        self.job.update(status="completed", outputs=[{"filename": self.source.name, "format": "glb"}])
        self.store.write(self.job)
        self.config = load_config()
        self.config["generation"]["allow_fbx"] = True
        self.config["paths"]["blender"] = str(self.source)  # Exists; execution is mocked except in round-trip tests.
        self.handler = object.__new__(server.LGOHandler)
        self.handler._json = Mock()
        self.generator = Mock()
        self.generator.start_export.return_value = {"process_id": 12345}
        self.generator.stop_process_tree.return_value = {"stopped": True}
        for name, value in (("STORE", self.store), ("CONFIG", self.config), ("GENERATOR", self.generator)):
            patcher = patch.object(server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        server.EXPORTS_STOPPING.clear()
        self.addCleanup(server.EXPORTS_STOPPING.clear)

    def request(self, source="white_mesh.glb", fmt="obj"):
        return prepare_export(self.job, self.config, source, fmt)

    def load_roundtrip(self, path):
        import trimesh
        scene = trimesh.load(path, force="scene", process=False)
        self.assertEqual(len(scene.graph.nodes_geometry), 1)
        transform, name = scene.graph[scene.graph.nodes_geometry[0]]
        mesh = scene.geometry[name]
        mesh.apply_transform(transform)
        return mesh

    def test_source_must_be_registered_and_inside_output(self):
        self.assertEqual(export_source(self.job, self.source.name), self.source.resolve())
        for filename in ("../white_mesh.glb", str(self.source), "missing.glb", "white_mesh.obj", "C:other.glb"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                export_source(self.job, filename)
        historic = self.output / "texture_versions" / "old" / "textured_mesh.glb"
        historic.parent.mkdir(parents=True)
        historic.write_bytes(b"old texture")
        self.job["texture_versions"] = [{"outputs": [{"filename": "texture_versions/old/textured_mesh.glb"}]}]
        self.assertEqual(export_source(self.job, "texture_versions/old/textured_mesh.glb"), historic.resolve())
        outside = self.root / "outside.glb"
        outside.write_bytes(b"outside")
        self.job["outputs"].append({"filename": "../outside.glb"})
        with self.assertRaises(ValueError):
            export_source(self.job, "../outside.glb")

    def test_validation_and_cache_invalidation(self):
        with self.assertRaises(ValueError):
            self.request(fmt="exe")
        self.config["generation"]["allow_fbx"] = False
        with self.assertRaises(ValueError):
            self.request(fmt="fbx")
        request = self.request()
        artifact = self.output / "exports" / "old.zip"
        artifact.parent.mkdir()
        artifact.write_bytes(b"archive")
        self.job["exports"] = [{**request, "filename": "exports/old.zip"}]
        self.assertIsNotNone(cached_export(self.job, request))
        self.source.write_bytes(b"changed GLB")
        self.assertIsNone(cached_export(self.job, self.request()))
        self.assertIsNone(cached_export(self.job, {**request, "source": "another.glb"}))
        artifact.unlink()
        self.assertIsNone(cached_export(self.job, request))

    def test_post_starts_export_only_and_tracks_worker(self):
        self.handler.path = f"/api/jobs/{self.job['id']}/export?format=fbx&source=white_mesh.glb"
        self.handler.do_POST()
        result, status = self.handler._json.call_args.args
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "converting_outputs")
        self.assertEqual(result["process_id"], 12345)
        self.assertEqual(result["export_request"]["source"], "white_mesh.glb")
        self.assertEqual(result["export_request"]["format"], "fbx")
        self.generator.start_export.assert_called_once()
        self.generator.start.assert_not_called()
        stopped = server._stop_service_work("Stopped in test")
        self.generator.stop_process_tree.assert_called_once_with(12345)
        self.assertEqual(len(stopped), 1)

    def test_get_does_not_start_export_and_busy_or_missing_are_rejected(self):
        self.handler.path = f"/api/jobs/{self.job['id']}/export?format=obj&source=white_mesh.glb"
        self.handler.do_GET()
        self.generator.start_export.assert_not_called()
        self.store.update(self.job, "converting_outputs", "Busy")
        for job_id, status in ((self.job["id"], 409), ("missing", 404), ("..", 400)):
            self.handler._export_result(job_id, {"format": ["obj"], "source": [self.source.name]})
            self.assertEqual(self.handler._json.call_args.args[1], status)
        server.EXPORTS_STOPPING.set()
        self.handler._export_result(self.job["id"], {})
        self.assertEqual(self.handler._json.call_args.args[1], 503)

    def test_failed_start_preserves_model_and_status(self):
        self.generator.start_export.side_effect = RuntimeError("Cannot start Blender")
        self.handler._export_result(self.job["id"], {"format": ["obj"], "source": [self.source.name]})
        job = self.store.get(self.job["id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["export_request"]["status"], "failed")
        self.assertTrue(self.source.exists())
        self.assertEqual(job["outputs"][0]["filename"], self.source.name)

    def test_cached_api_export_does_not_spawn_or_duplicate(self):
        request = self.request()
        archive = self.output / "exports" / "cached.zip"
        archive.parent.mkdir()
        archive.write_bytes(b"cached archive")
        self.job["exports"] = [{**request, "filename": "exports/cached.zip"}]
        self.store.write(self.job)
        self.handler._export_result(self.job["id"], {"format": ["obj"], "source": [self.source.name]})
        self.generator.start_export.assert_not_called()
        result = self.handler._json.call_args.args[0]
        self.assertEqual(result["export_request"]["status"], "completed")
        self.assertEqual(len(result["exports"]), 1)
        self.assertEqual(server._safe_job_output_path(result, "exports/cached.zip"), archive.resolve())

    def test_invalid_request_does_not_resurrect_interrupted_job(self):
        self.job["export_request"] = self.request()
        self.store.update(self.job, "failed", "Stopped")
        self.handler._export_result(self.job["id"], {"format": ["obj"], "source": ["missing.glb"]})
        self.assertEqual(self.handler._json.call_args.args[1], 400)
        self.assertEqual(self.store.get(self.job["id"])["status"], "failed")

    def test_export_worker_preserves_outputs_on_success_and_failure(self):
        self.job["export_request"] = self.request()
        self.store.update(self.job, "converting_outputs", "Export")
        job_path = Path(self.job["run_dir"]) / "job.json"
        artifact = {"filename": "exports/result.fbx"}
        with patch("lgo.exports.convert_export", return_value=artifact):
            runner._run_export_only(job_path, self.config, self.job)
        result = self.store.get(self.job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exports"], [artifact])
        self.assertEqual(result["outputs"][0]["filename"], self.source.name)
        with patch("lgo.exports.convert_export", side_effect=RuntimeError("Failed")), patch.object(runner.traceback, "print_exc"):
            runner._run_export_only(job_path, self.config, self.job)
        result = self.store.get(self.job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["export_request"]["error"], "Failed")
        self.assertEqual(result["exports"], [artifact])

    def test_export_main_does_not_load_generation_runtime(self):
        with patch("sys.argv", ["runner", "--job", "job.json", "--config", "config.json", "--export-only"]), \
                patch.object(runner, "_read_config", return_value=self.config), \
                patch.object(runner, "_read_json", return_value=self.job), \
                patch.object(runner, "_setup_runtime") as setup, patch.object(runner, "_run_export_only") as export:
            runner.main()
            setup.assert_not_called()
            export.assert_called_once()

    def test_changed_source_is_not_exported(self):
        self.job["export_request"] = self.request()
        self.source.write_bytes(b"changed")
        with patch("lgo.exports.subprocess.run") as run, self.assertRaises(ValueError):
            convert_export(self.job, self.config)
        run.assert_not_called()

    @unittest.skipUnless(os.environ.get("LGO_TEST_BLENDER") == "1", "Optional Blender round-trip")
    def test_real_obj_package_and_embedded_fbx_round_trip(self):
        import numpy as np
        import trimesh
        from PIL import Image
        self.config["paths"]["blender"] = load_config()["paths"]["blender"]
        mesh = trimesh.Trimesh(vertices=[[0, 0, 0], [3, 0, 0], [3, 2, 0], [0, 2, 0]],
                               faces=[[0, 1, 2], [0, 2, 3]], process=False)
        pixels = np.zeros((16, 16, 3), dtype=np.uint8)
        pixels[::2] = [180, 112, 32]
        pixels[1::2] = [24, 28, 35]
        mesh.visual = trimesh.visual.TextureVisuals(uv=[[0, 0], [1, 0], [1, 1], [0, 1]],
            material=trimesh.visual.material.PBRMaterial(baseColorTexture=Image.fromarray(pixels),
                                                       metallicFactor=0.3, roughnessFactor=0.7))
        mesh.export(self.source)
        original = file_hash(self.source)
        run = subprocess.run
        def quiet_run(*args, **kwargs):
            result = run(*args, **{**kwargs, "check": False, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT})
            self.assertEqual(result.returncode, 0, result.stdout.decode(errors="replace"))
            return result
        for fmt in ("obj", "fbx"):
            with self.subTest(fmt=fmt):
                self.job["export_request"] = self.request(fmt=fmt)
                with patch("lgo.exports.subprocess.run", side_effect=quiet_run):
                    result = convert_export(self.job, self.config)
                self.assertEqual(file_hash(self.source), original)
                artifact = self.output / result["filename"]
                if fmt == "obj":
                    with zipfile.ZipFile(artifact) as package:
                        names = package.namelist()
                        self.assertIn("white_mesh.obj", names)
                        self.assertIn("white_mesh.mtl", names)
                        self.assertNotIn("source.glb", names)
                        mtl = package.read("white_mesh.mtl").decode()
                        maps = [line.split(maxsplit=1)[1].replace("\\", "/") for line in mtl.splitlines() if line.startswith("map_Kd ")]
                        self.assertTrue(maps, mtl)
                        for name in maps:
                            self.assertIn(name, names)
                        package.extractall(self.root / "obj")
                        imported = self.root / "obj" / "white_mesh.obj"
                else:
                    imported = artifact
                roundtrip = self.root / f"roundtrip-{fmt}.glb"
                process = run([self.config["paths"]["blender"], "--background", "--factory-startup", "--python-exit-code", "1",
                               "--python", str(PROJECT_ROOT / "tools" / "blender_convert.py"), "--", str(imported),
                               str(roundtrip), "--no-postprocess"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
                self.assertEqual(process.returncode, 0, process.stdout.decode(errors="replace"))
                restored = self.load_roundtrip(roundtrip)
                np.testing.assert_allclose(restored.bounds, mesh.bounds, atol=1e-5)
                self.assertEqual(len(restored.faces), len(mesh.faces))
                self.assertIsNotNone(restored.visual.material.baseColorTexture)
                np.testing.assert_array_equal(np.asarray(restored.visual.material.baseColorTexture)[:, :, :3], pixels)
                for vertex, uv in zip(restored.vertices, restored.visual.uv):
                    nearest = np.argmin(np.linalg.norm(mesh.vertices - vertex, axis=1))
                    np.testing.assert_allclose(uv, mesh.visual.uv[nearest], atol=1e-5)

    @unittest.skipUnless(os.environ.get("LGO_TEST_BLENDER") == "1", "Optional Blender vertex color round-trip")
    def test_real_vertex_colored_legacy_results(self):
        import numpy as np
        import trimesh
        self.config["paths"]["blender"] = load_config()["paths"]["blender"]
        mesh = trimesh.Trimesh(vertices=[[0, 0, 0], [3, 0, 0], [0, 2, 0]], faces=[[0, 1, 2]],
                               vertex_colors=[[32, 64, 128, 255], [180, 112, 32, 255], [24, 28, 35, 255]], process=False)
        mesh.export(self.source)
        for fmt in ("obj", "fbx"):
            with self.subTest(fmt=fmt):
                self.job["export_request"] = self.request(fmt=fmt)
                result = convert_export(self.job, self.config)
                artifact = self.output / result["filename"]
                if fmt == "obj":
                    with zipfile.ZipFile(artifact) as package:
                        package.extractall(self.root / "legacy-obj")
                    artifact = self.root / "legacy-obj" / "white_mesh.obj"
                # Read the imported color attribute directly: a second GLB export
                # can replace it with white when FBX/OBJ materials do not wire it.
                probe = (f"import sys,json,bpy; sys.path.insert(0, {str(PROJECT_ROOT / 'tools')!r}); "
                         f"import blender_convert as c; from pathlib import Path; c._clear_scene(); c._import(Path({str(artifact)!r})); "
                         "print('COLOR_REPORT=' + json.dumps([[list(v.color) for v in o.data.color_attributes[0].data] "
                         "for o in bpy.context.scene.objects if o.type == 'MESH']))")
                process = subprocess.run([self.config["paths"]["blender"], "--background", "--factory-startup", "--python-exit-code", "1",
                                          "--python-expr", probe], capture_output=True, timeout=120)
                self.assertEqual(process.returncode, 0, process.stderr.decode(errors="replace"))
                line = next(line for line in process.stdout.decode().splitlines() if line.startswith("COLOR_REPORT="))
                colors = np.rint(np.asarray(json.loads(line.split("=", 1)[1])[0]) * 255)
                np.testing.assert_allclose(sorted(colors.tolist()), sorted(mesh.visual.vertex_colors.tolist()), atol=2)


if __name__ == "__main__":
    unittest.main()
