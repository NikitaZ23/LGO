import io
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from lgo.generation import GenerationService
from lgo.jobs import JobStore
from lgo_server import LGOHandler
from scripts import run_hunyuan_job as runner


class SixViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = {
            view: Image.new("RGBA", (16, 16), (30 + index * 25, 60, 90, 255))
            for index, view in enumerate(runner.TEXTURE_VIEW_ORDER)
        }

    def make_form(self):
        form = {}
        for view, image in self.images.items():
            contents = io.BytesIO()
            image.save(contents, format="PNG")
            contents.seek(0)
            form[view] = SimpleNamespace(filename=f"{view}.png", file=contents)
        return form

    def save_inputs(self, mode="sixview", form=None):
        payload = {"mode": mode, "input_files": {}, "remove_background": False}
        LGOHandler._save_inputs(None, self.make_form() if form is None else form, {"input_dir": str(self.root)}, payload)
        return payload

    def test_upload_preprocess_and_reload_keep_all_six_views(self):
        payload = self.save_inputs()
        images, report = runner._load_images(
            payload, {"preprocess": {"enabled": False, "save_cleaned": True}}, self.root / "job.json",
        )
        reloaded = runner._load_existing_images_for_texture({"payload": payload, "preprocessing": report})
        self.assertEqual(tuple(images), runner.TEXTURE_VIEW_ORDER)
        self.assertEqual(tuple(reloaded), runner.TEXTURE_VIEW_ORDER)
        for view in images:
            self.assertEqual(reloaded[view].getpixel((0, 0)), self.images[view].getpixel((0, 0)))

    def test_missing_bottom_is_rejected_before_any_file_is_saved(self):
        form = self.make_form()
        del form["bottom"]
        with self.assertRaisesRegex(ValueError, "bottom"):
            self.save_inputs(form=form)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_four_view_upload_does_not_save_inactive_top_or_bottom(self):
        payload = self.save_inputs("multiview")
        self.assertEqual(tuple(payload["input_files"]), runner.VIEW_ORDER)
        self.assertFalse((self.root / "top.png").exists())

    def test_shape_receives_only_the_four_supported_views(self):
        pipeline = Mock(return_value=["mesh"])
        self.assertEqual(runner._generate_shape(pipeline, self.images, {"generation": {}}, None), "mesh")
        self.assertEqual(tuple(pipeline.call_args.kwargs["image"]), runner.VIEW_ORDER)
        pipeline.assert_called_once()

    def test_single_image_shape_is_unchanged(self):
        pipeline = Mock(return_value=["mesh"])
        runner._generate_shape(pipeline, self.images["front"], {"generation": {}}, None)
        self.assertIs(pipeline.call_args.kwargs["image"], self.images["front"])

    def test_saved_cleaned_top_is_used_for_texture_rerun(self):
        payload = self.save_inputs()
        cleaned = self.root / "top-cleaned.png"
        Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(cleaned)
        job = {"payload": payload, "preprocessing": {"saved": {"top": str(cleaned)}}}
        self.assertEqual(runner._load_existing_images_for_texture(job)["top"].getpixel((0, 0)), (1, 2, 3, 255))
        (self.root / "bottom.png").unlink()
        with self.assertRaises(FileNotFoundError):
            runner._load_existing_images_for_texture(job)

    def test_paint_receives_six_references_after_vendor_single_image_slice(self):
        raw_pipeline = Mock()
        raw_pipeline.unet = SimpleNamespace(use_ra=True)
        raw_pipeline.view_size = 512
        adapter = runner._TextureReferencePipeline(raw_pipeline, self.images)
        adapter([self.images["front"]], width=32, height=32, num_inference_steps=15)
        supplied = raw_pipeline.call_args.args[0]
        self.assertEqual(len(supplied), 6)
        self.assertEqual([image.getpixel((0, 0)) for image in supplied],
                         [image.getpixel((0, 0))[:3] for image in self.images.values()])
        self.assertTrue(all(image.size == (32, 32) and image.mode == "RGB" for image in supplied))
        self.assertEqual(raw_pipeline.call_args.kwargs["num_inference_steps"], 15)
        self.assertEqual(adapter.calls, 1)

    def test_paint_references_require_reference_attention(self):
        with self.assertRaisesRegex(RuntimeError, "reference attention"):
            runner._TextureReferencePipeline(SimpleNamespace(unet=SimpleNamespace(use_ra=False)), self.images)

    def test_texture_pass_connects_reference_adapter_only_for_six_views(self):
        for six_views in (False, True):
            with self.subTest(six_views=six_views), ExitStack() as stack:
                raw_pipeline = Mock()
                raw_pipeline.unet = SimpleNamespace(use_ra=True)
                raw_pipeline.view_size = 512
                configs = []

                class FakePaint:
                    def __init__(self, config):
                        configs.append(config)
                        self.models = {"multiview_model": SimpleNamespace(pipeline=raw_pipeline)}

                    def __call__(self, **kwargs):
                        self.models["multiview_model"].pipeline([kwargs["image_path"]], width=384, height=384)

                vendor = SimpleNamespace(
                    Hunyuan3DPaintConfig=lambda max_num_view, resolution: SimpleNamespace(
                        max_selected_view_num=max_num_view, resolution=resolution),
                    Hunyuan3DPaintPipeline=FakePaint,
                )
                stack.enter_context(patch.dict("sys.modules", {
                    "textureGenPipeline": vendor,
                    "hy3dpaint": SimpleNamespace(),
                    "hy3dpaint.convert_utils": SimpleNamespace(create_glb_with_pbr_materials=Mock()),
                }))
                for name in ("_patch_torchvision_functional_tensor", "_patch_snapshot_download",
                             "_patch_texture_remesh_target", "_append_log"):
                    stack.enter_context(patch.object(runner, name))
                stack.enter_context(patch.object(runner, "_texture_runtime_ready", return_value=True))
                stack.enter_context(patch.object(runner, "_prepare_mesh_for_export", return_value=Mock()))
                stack.enter_context(patch.object(runner, "_stabilize_pbr_textures", return_value={}))
                stack.enter_context(patch.object(runner, "_bake_texture_to_shape_mesh", return_value={"applied": True}))
                config = {"generation": {"texture_views": 4, "texture_resolution": 384},
                          "paths": {"hunyuan_source_dir": str(self.root)},
                          "models": {"paint_root": str(self.root), "realesrgan": "upscale"}}
                images = self.images if six_views else {view: self.images[view] for view in runner.VIEW_ORDER}
                output, warning, report = runner._try_texture(config, self.root, Mock(), images)
                self.assertIsNone(warning)
                self.assertEqual(output, self.root / "textured_mesh.glb")
                self.assertEqual(len(raw_pipeline.call_args.args[0]), 6 if six_views else 1)
                self.assertEqual(configs[0].max_selected_view_num, 6 if six_views else 4)
                self.assertEqual(configs[0].resolution, 384)
                if six_views:
                    self.assertEqual(report["texture_prompt"]["reference_count"], 6)

    def test_texture_prompt_adjustments_apply_to_every_reference(self):
        config = {"postprocess": {"texture_prompt": {"enabled": True, "brightness": 0.5}}}
        references, reports = runner._prepare_texture_references(self.images, config, self.root)
        for view in self.images:
            self.assertLess(references[view].getpixel((0, 0))[0], self.images[view].getpixel((0, 0))[0])
            self.assertTrue(Path(reports[view]["path"]).is_file())
        self.assertEqual(len({report["path"] for report in reports.values()}), 6)

    def test_history_and_manifest_distinguish_six_view_mode(self):
        store = JobStore(self.root / "runs")
        payload = self.save_inputs()
        payload.update(texture=True, formats=["glb"])
        job = store.create(payload)
        config = {"models": {"single_shape": "single", "multiview_shape": "mv", "paint_model": "paint",
                             "vae": "vae", "hy3dpaint": "paint", "realesrgan": "upscale"},
                  "paths": {"blender": "blender", "hunyuan_source_dir": "source"}, "generation": {}}
        manifest = GenerationService(config).prepare(job)
        self.assertEqual(manifest["models"]["shape"], "mv")
        self.assertEqual(tuple(manifest["shape_input_views"]), runner.VIEW_ORDER)
        self.assertEqual(tuple(manifest["texture_reference_views"]), runner.TEXTURE_VIEW_ORDER)
        self.assertIn("6 views", store.display_name(job))


if __name__ == "__main__":
    unittest.main()
