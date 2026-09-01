from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import types
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageStat


VIEW_ORDER = ("front", "back", "left", "right")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one LGO Hunyuan3D generation job.")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--texture-only", action="store_true")
    parser.add_argument("--rebake-texture", action="store_true")
    args = parser.parse_args()

    config = _read_config(args.config)
    job = _read_json(args.job)
    _setup_runtime(config)

    if args.dry_run:
        _update_job(args.job, "dry_run_ok", "Runner dry-run passed.", dry_run=True)
        return

    try:
        if args.rebake_texture:
            _run_texture_rebake(args.job, config, job)
        elif args.texture_only:
            _run_texture_only(args.job, config, job)
        else:
            _run_generation(args.job, config, job)
    except Exception as exc:  # noqa: BLE001 - job files should preserve full local failure detail.
        traceback.print_exc()
        _update_job(args.job, "failed", str(exc), error=traceback.format_exc())
        raise


def _run_generation(job_path: Path, config: dict[str, Any], job: dict[str, Any]) -> None:
    started_at = time.time()
    payload = job["payload"]
    config, quality = _apply_quality_preset(config, payload)
    config, object_type = _apply_object_type_preset(config, payload)
    output_dir = Path(job["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    _update_job(
        job_path,
        "preprocessing_images",
        f"Cleaning input background and shadows. Quality: {quality['label']}. Object type: {object_type['label']}.",
        quality=quality,
        object_type=object_type,
    )
    images, preprocessing = _load_images(payload, config, job_path)
    warnings.extend(preprocessing.get("warnings", []))

    _update_job(job_path, "loading_shape_model", "Loading Hunyuan3D shape model.")
    pipeline = _load_shape_pipeline(config, payload["mode"])

    seed = int(config["generation"].get("seed", 1234))
    generator = _make_generator(seed)

    _update_job(job_path, "generating_shape", "Generating mesh shape.")
    mesh = _generate_shape(pipeline, images, config, generator)

    _update_job(job_path, "postprocessing_mesh", "Cleaning and refining mesh.")
    if config.get("postprocess", {}).get("floor_plate", {}).get("save_raw_mesh", True):
        raw_glb = output_dir / "raw_mesh.glb"
        mesh.export(str(raw_glb))
    mesh, postprocessing = _postprocess_mesh(mesh, config)
    warnings.extend(postprocessing.get("warnings", []))

    shape_glb = output_dir / "white_mesh.glb"
    mesh.export(str(shape_glb))
    postprocessing["shade_smooth"] = _shade_smooth_glb(config, shape_glb)

    primary_glb = shape_glb
    outputs = [_output("glb", shape_glb, "White mesh GLB")]

    texture_quality = None
    if payload.get("texture"):
        texture_config, texture_quality = _apply_texture_quality_preset(config, payload)
        texture_config, _ = _apply_object_type_preset(texture_config, payload)
        texture_config, texture_color = _apply_texture_color_override(texture_config, payload)
        _update_job(
            job_path,
            "applying_texture",
            f"Applying PBR texture. Texture speed: {texture_quality['label']}. Color: {texture_color:.2f}x.",
            texture_quality=texture_quality,
            object_type=object_type,
            texture_color=texture_color,
        )
        textured_glb, warning, textured_postprocess = _try_texture(texture_config, output_dir, mesh, images)
        if textured_postprocess:
            postprocessing["texture_bake"] = textured_postprocess
            if textured_postprocess.get("fallback_remesh"):
                postprocessing["textured_subdivide"] = textured_postprocess["fallback_remesh"]
        if textured_glb is not None:
            primary_glb = textured_glb
            outputs = [
                _output("glb", shape_glb, "White mesh GLB"),
                _output("glb", textured_glb, "Textured mesh GLB"),
            ]
        if warning:
            warnings.append(warning)

    requested_extra_formats = {item.lower() for item in payload.get("formats", [])} - {"glb"}
    if requested_extra_formats:
        _update_job(job_path, "converting_outputs", "Converting output formats.")
    outputs.extend(_convert_extra_formats(config, primary_glb, payload.get("formats", []), output_dir))
    elapsed = round(time.time() - started_at, 2)

    status = "completed_with_warnings" if warnings else "completed"
    message = f"Generation completed in {elapsed}s."
    if warnings:
        message += " " + " ".join(warnings)
    final_update = {
        "outputs": outputs,
        "warnings": warnings,
        "preprocessing": preprocessing,
        "postprocessing": postprocessing,
        "quality": quality,
        "object_type": object_type,
        "elapsed_seconds": elapsed,
    }
    if texture_quality:
        final_update["texture_quality"] = texture_quality
        final_update["texture_color"] = payload.get("texture_color")
    _update_job(job_path, status, message, **final_update)


def _run_texture_only(job_path: Path, config: dict[str, Any], job: dict[str, Any]) -> None:
    started_at = time.time()
    payload = job["payload"]
    payload["texture"] = True
    config, texture_quality = _apply_texture_quality_preset(config, payload)
    config, object_type = _apply_object_type_preset(config, payload)
    config, texture_color = _apply_texture_color_override(config, payload)
    output_dir = Path(job["output_dir"])
    shape_glb = output_dir / "white_mesh.glb"
    if not shape_glb.exists():
        raise FileNotFoundError(f"White mesh was not found: {shape_glb}")

    start_update = {
        "payload": payload,
        "texture_quality": texture_quality,
        "object_type": object_type,
        "texture_color": texture_color,
    }
    if job.get("quality"):
        start_update["quality"] = job["quality"]
    _update_job(
        job_path,
        "applying_texture",
        f"Applying PBR texture to existing mesh. Texture speed: {texture_quality['label']}. Color: {texture_color:.2f}x.",
        **start_update,
    )

    mesh = _load_mesh(shape_glb)
    images = _load_existing_images_for_texture(job)
    textured_glb, warning, textured_postprocess = _try_texture(config, output_dir, mesh, images)

    warnings = [item for item in job.get("warnings", []) if "Texture failed" not in item]
    postprocessing = job.get("postprocessing", {})
    if textured_postprocess:
        postprocessing["texture_bake"] = textured_postprocess
        if textured_postprocess.get("fallback_remesh"):
            postprocessing["textured_subdivide"] = textured_postprocess["fallback_remesh"]

    primary_glb = shape_glb
    outputs = [_output("glb", shape_glb, "White mesh GLB")]
    if textured_glb is not None:
        primary_glb = textured_glb
        outputs = [
            _output("glb", shape_glb, "White mesh GLB"),
            _output("glb", textured_glb, "Textured mesh GLB"),
        ]
    if warning:
        warnings.append(warning)

    requested_extra_formats = {item.lower() for item in payload.get("formats", [])} - {"glb"}
    if requested_extra_formats:
        converting_update = {
            "payload": payload,
            "texture_quality": texture_quality,
            "object_type": object_type,
            "texture_color": texture_color,
        }
        if job.get("quality"):
            converting_update["quality"] = job["quality"]
        _update_job(job_path, "converting_outputs", "Converting output formats.", **converting_update)
    outputs.extend(_convert_extra_formats(config, primary_glb, payload.get("formats", []), output_dir))

    elapsed = round(time.time() - started_at, 2)
    status = "completed_with_warnings" if warnings else "completed"
    message = f"Texture pass completed in {elapsed}s." if textured_glb else f"Texture pass finished in {elapsed}s."
    if warnings:
        message += " " + " ".join(warnings)

    final_update = {
        "payload": payload,
        "outputs": outputs,
        "warnings": warnings,
        "postprocessing": postprocessing,
        "texture_quality": texture_quality,
        "object_type": object_type,
        "texture_color": texture_color,
        "texture_added": True,
        "texture_elapsed_seconds": elapsed,
    }
    if job.get("quality"):
        final_update["quality"] = job["quality"]
    _update_job(job_path, status, message, **final_update)


def _run_texture_rebake(job_path: Path, config: dict[str, Any], job: dict[str, Any]) -> None:
    started_at = time.time()
    payload = job["payload"]
    payload["texture"] = True
    config, texture_quality = _apply_texture_quality_preset(config, payload)
    config, object_type = _apply_object_type_preset(config, payload)
    config, rebake_albedo = _apply_rebake_albedo_override(config, payload)
    config, texture_color = _apply_texture_color_override(config, payload)
    output_dir = Path(job["output_dir"])
    shape_glb = output_dir / "white_mesh.glb"
    textured_obj = output_dir / "textured_mesh.obj"
    albedo_path = output_dir / "textured_mesh.jpg"
    textured_glb = output_dir / "textured_mesh.glb"

    for required_path in (shape_glb, textured_obj, albedo_path):
        if not required_path.exists():
            raise FileNotFoundError(f"Texture re-bake input was not found: {required_path}")

    update = {
        "payload": payload,
        "texture_quality": texture_quality,
        "object_type": object_type,
        "rebake_albedo": rebake_albedo,
        "texture_color": texture_color,
    }
    if job.get("quality"):
        update["quality"] = job["quality"]
    _update_job(
        job_path,
        "rebaking_texture",
        f"Re-baking existing texture colors. Texture speed: {texture_quality['label']}. Albedo: {rebake_albedo:.2f}x. Color: {texture_color:.2f}x.",
        **update,
    )

    mesh = _load_mesh(shape_glb)
    images = _load_existing_images_for_texture(job)
    textured_postprocess = _bake_texture_to_shape_mesh(
        mesh,
        textured_obj,
        albedo_path,
        textured_glb,
        config,
        _primary_image(images),
        shade_smooth=False,
    )

    warnings = [
        item
        for item in job.get("warnings", [])
        if "Texture bake to white mesh failed" not in item and "Texture color re-bake failed" not in item
    ]
    postprocessing = job.get("postprocessing", {})
    postprocessing["texture_bake"] = textured_postprocess

    outputs = [_output("glb", shape_glb, "White mesh GLB")]
    if textured_postprocess.get("applied") and textured_glb.exists():
        outputs.append(_output("glb", textured_glb, "Textured mesh GLB"))
    else:
        if textured_glb.exists():
            outputs.append(_output("glb", textured_glb, "Textured mesh GLB"))
        warnings.append(
            "Texture color re-bake failed; kept previous textured output."
        )

    outputs.extend(
        output for output in job.get("outputs", [])
        if output.get("filename") not in {"white_mesh.glb", "textured_mesh.glb"}
    )

    elapsed = round(time.time() - started_at, 2)
    status = "completed_with_warnings" if warnings else "completed"
    message = f"Texture color re-bake completed in {elapsed}s."
    if warnings:
        message += " " + " ".join(warnings)

    final_update = {
        "payload": payload,
        "outputs": outputs,
        "warnings": warnings,
        "postprocessing": postprocessing,
        "texture_quality": texture_quality,
        "object_type": object_type,
        "texture_added": True,
        "texture_rebaked": True,
        "rebake_albedo": rebake_albedo,
        "texture_color": texture_color,
        "texture_rebake_elapsed_seconds": elapsed,
    }
    if job.get("quality"):
        final_update["quality"] = job["quality"]
    _update_job(job_path, status, message, **final_update)


def _apply_quality_preset(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    presets = config.get("quality_presets", {})
    default_quality = str(config.get("generation", {}).get("default_quality", "balanced")).lower()
    selected = str(payload.get("quality") or default_quality).lower()
    if selected not in presets:
        selected = default_quality if default_quality in presets else "balanced"
    if selected not in presets and presets:
        selected = next(iter(presets))

    effective = copy.deepcopy(config)
    preset = copy.deepcopy(presets.get(selected, {}))
    for section in ("generation", "preprocess", "postprocess"):
        if section in preset:
            target = effective.setdefault(section, {})
            _deep_update(target, preset[section])

    return effective, {
        "selected": selected,
        "label": preset.get("label", selected.title()),
        "generation": preset.get("generation", {}),
        "postprocess": preset.get("postprocess", {}),
    }


def _apply_texture_quality_preset(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    presets = config.get("texture_quality_presets", {})
    default_quality = str(config.get("generation", {}).get("default_texture_quality", "fast")).lower()
    selected = str(payload.get("texture_quality") or default_quality).lower()
    if selected not in presets:
        selected = default_quality if default_quality in presets else "fast"
    if selected not in presets and presets:
        selected = next(iter(presets))

    effective = copy.deepcopy(config)
    preset = copy.deepcopy(presets.get(selected, {}))
    for section in ("generation", "postprocess"):
        if section in preset:
            target = effective.setdefault(section, {})
            _deep_update(target, preset[section])

    return effective, {
        "selected": selected,
        "label": preset.get("label", selected.title()),
        "generation": preset.get("generation", {}),
        "postprocess": preset.get("postprocess", {}),
    }


def _apply_object_type_preset(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    presets = config.get("object_type_presets", {})
    default_type = str(config.get("generation", {}).get("default_object_type", "organic")).lower()
    selected = str(payload.get("object_type") or default_type).lower()
    if selected not in presets:
        selected = default_type if default_type in presets else "organic"
    if selected not in presets and presets:
        selected = next(iter(presets))

    effective = copy.deepcopy(config)
    preset = copy.deepcopy(presets.get(selected, {}))
    for section in ("generation", "preprocess", "postprocess"):
        if section in preset:
            target = effective.setdefault(section, {})
            _deep_update(target, preset[section])

    return effective, {
        "selected": selected,
        "label": preset.get("label", selected.replace("_", " ").title()),
        "generation": preset.get("generation", {}),
        "preprocess": preset.get("preprocess", {}),
        "postprocess": preset.get("postprocess", {}),
    }


def _apply_rebake_albedo_override(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
    albedo_gain = _rebake_albedo_value(payload.get("rebake_albedo", 1.0))
    payload["rebake_albedo"] = albedo_gain
    effective = copy.deepcopy(config)
    bake_settings = effective.setdefault("postprocess", {}).setdefault("texture_bake", {})
    bake_settings["albedo_gain"] = albedo_gain
    return effective, albedo_gain


def _apply_texture_color_override(config: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
    texture_color = _texture_color_value(payload.get("texture_color", 1.0))
    payload["texture_color"] = texture_color
    effective = copy.deepcopy(config)
    bake_settings = effective.setdefault("postprocess", {}).setdefault("texture_bake", {})
    bake_settings["saturation"] = texture_color
    if texture_color < 1.0:
        palette_blend = float(bake_settings.get("palette_blend", 0.55))
        bake_settings["palette_blend"] = round(palette_blend * texture_color, 4)
    return effective, texture_color


def _rebake_albedo_value(value: Any) -> float:
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        parsed = 1.0
    return round(max(0.5, min(1.8, parsed)), 2)


def _texture_color_value(value: Any) -> float:
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        parsed = 1.0
    return round(max(0.35, min(1.6, parsed)), 2)


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _install_hunyuan_shape_aliases() -> None:
    import importlib

    root = sys.modules.setdefault("hy3dgen", types.ModuleType("hy3dgen"))
    if not hasattr(root, "__path__"):
        root.__path__ = []

    aliases = {
        "hy3dgen.shapegen": "hy3dshape",
        "hy3dgen.shapegen.models": "hy3dshape.models",
        "hy3dgen.shapegen.schedulers": "hy3dshape.schedulers",
        "hy3dgen.shapegen.preprocessors": "hy3dshape.preprocessors",
        "hy3dgen.shapegen.pipelines": "hy3dshape.pipelines",
    }
    for alias, target in aliases.items():
        module = importlib.import_module(target)
        sys.modules[alias] = module

    for alias in aliases:
        parent_name, child_name = alias.rsplit(".", 1)
        parent = sys.modules.get(parent_name)
        child = sys.modules.get(alias)
        if parent is not None and child is not None:
            setattr(parent, child_name, child)


def _load_shape_pipeline(config: dict[str, Any], mode: str):
    source_dir = Path(config["paths"]["hunyuan_source_dir"])
    if not source_dir.exists():
        raise FileNotFoundError(f"Hunyuan3D source folder was not found: {source_dir}")
    os.chdir(source_dir)

    try:
        from torchvision_fix import apply_fix

        apply_fix()
    except Exception as exc:  # noqa: BLE001 - compatibility fix is helpful but not mandatory.
        print(f"Warning: torchvision compatibility fix failed: {exc}")

    import torch
    from hy3dshape import Hunyuan3DDiTFlowMatchingPipeline

    _install_hunyuan_shape_aliases()

    model_subfolder = "Hunyuan3D-DiT-v2-mv" if mode == "multiview" else "Hunyuan3D-DiT-v2-1"
    use_safetensors = mode == "multiview"
    pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
        ".",
        subfolder=model_subfolder,
        use_safetensors=use_safetensors,
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    if config["generation"].get("low_vram", True) and torch.cuda.is_available():
        try:
            pipeline.enable_model_cpu_offload()
        except Exception as exc:  # noqa: BLE001 - keep generation available if offload setup is unavailable.
            print(f"Warning: low VRAM offload failed: {exc}")
    return pipeline


def _load_images(payload: dict[str, Any], config: dict[str, Any], job_path: Path):
    remove_background = payload.get("remove_background", True)
    settings = config.get("preprocess", {})
    enabled = bool(settings.get("enabled", True))
    save_cleaned = bool(settings.get("save_cleaned", True))
    cleaned_dir = job_path.parent / "input" / "cleaned"
    report: dict[str, Any] = {
        "enabled": enabled,
        "remove_background": remove_background,
        "saved": {},
        "images": {},
        "warnings": [],
    }

    if payload["mode"] == "multiview":
        images: dict[str, Image.Image] = {
            view: Image.open(payload["input_files"][view]).convert("RGBA")
            for view in VIEW_ORDER
            if view in payload["input_files"]
        }
        if remove_background:
            try:
                images = _remove_background(images)
            except Exception as exc:  # noqa: BLE001 - generation can continue from original images.
                report["warnings"].append(f"Background removal failed, original views used: {exc}")
        images = {
            view: _prepare_image_for_generation(view, image, settings, enabled, save_cleaned, cleaned_dir, report)
            for view, image in images.items()
        }
        return images, report

    image = Image.open(payload["input_files"]["single"]).convert("RGBA")
    if remove_background:
        try:
            image = _remove_background(image)
        except Exception as exc:  # noqa: BLE001 - generation can continue from original image.
            report["warnings"].append(f"Background removal failed, original image used: {exc}")
    image = _prepare_image_for_generation("single", image, settings, enabled, save_cleaned, cleaned_dir, report)
    return image, report


def _remove_background(images):
    from hy3dshape.rembg import BackgroundRemover

    remover = BackgroundRemover()
    if isinstance(images, dict):
        return {name: remover(image.convert("RGB")).convert("RGBA") for name, image in images.items()}
    return remover(images.convert("RGB")).convert("RGBA")


def _prepare_image_for_generation(
    name: str,
    image: Image.Image,
    settings: dict[str, Any],
    enabled: bool,
    save_cleaned: bool,
    cleaned_dir: Path,
    report: dict[str, Any],
) -> Image.Image:
    image = image.convert("RGBA")
    image_report: dict[str, Any] = {"size": image.size}
    if enabled:
        image, cleanup_report = _strengthen_foreground_alpha(image, settings)
        image_report.update(cleanup_report)
        image, detail_report = _enhance_generation_detail(image, settings)
        image_report["detail_enhance"] = detail_report
    if save_cleaned:
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        target = cleaned_dir / f"{name}.png"
        image.save(target)
        report["saved"][name] = str(target)
    report["images"][name] = image_report
    return image


def _enhance_generation_detail(image: Image.Image, settings: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    detail = settings.get("detail_enhance", {})
    if not bool(detail.get("enabled", True)):
        return image, {"enabled": False}

    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    contrast = float(detail.get("contrast", 1.08))
    if contrast > 0 and abs(contrast - 1.0) > 0.001:
        rgb = ImageEnhance.Contrast(rgb).enhance(contrast)

    radius = float(detail.get("sharpness_radius", 1.2))
    percent = int(detail.get("sharpness_percent", 115))
    threshold = int(detail.get("sharpness_threshold", 4))
    if percent > 0 and radius > 0:
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold))

    rgb.putalpha(rgba.getchannel("A"))
    return rgb.convert("RGBA"), {
        "enabled": True,
        "contrast": contrast,
        "sharpness_radius": radius,
        "sharpness_percent": percent,
        "sharpness_threshold": threshold,
    }


def _strengthen_foreground_alpha(image: Image.Image, settings: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    import numpy as np

    rgba = np.array(image.convert("RGBA"), dtype=np.uint8)
    alpha = rgba[:, :, 3].copy()
    original_foreground = int((alpha > 0).sum())

    alpha_threshold = int(settings.get("alpha_threshold", 56))
    shadow_alpha_threshold = int(settings.get("shadow_alpha_threshold", 150))
    shadow_brightness_threshold = int(settings.get("shadow_brightness_threshold", 90))
    shadow_saturation_threshold = int(settings.get("shadow_saturation_threshold", 42))

    rgb = rgba[:, :, :3].astype(np.int16)
    brightness = rgb.mean(axis=2)
    saturation = rgb.max(axis=2) - rgb.min(axis=2)
    weak_alpha = alpha < alpha_threshold
    soft_shadow = (
        (alpha < shadow_alpha_threshold)
        & (brightness < shadow_brightness_threshold)
        & (saturation < shadow_saturation_threshold)
    )
    alpha[weak_alpha | soft_shadow] = 0

    if settings.get("harden_alpha", True):
        alpha_float = alpha.astype(np.float32) / 255.0
        cutoff = alpha_threshold / 255.0
        alpha_float = np.where(alpha > 0, np.clip((alpha_float - cutoff) / max(1.0 - cutoff, 0.001), 0.0, 1.0), 0.0)
        alpha = (np.power(alpha_float, float(settings.get("alpha_gamma", 0.75))) * 255.0).astype(np.uint8)

    alpha_image = Image.fromarray(alpha, mode="L")
    morph_size = int(settings.get("morph_open_size", 3))
    if morph_size >= 3:
        if morph_size % 2 == 0:
            morph_size += 1
        alpha_image = alpha_image.filter(ImageFilter.MinFilter(morph_size)).filter(ImageFilter.MaxFilter(morph_size))

    if settings.get("keep_largest_component", True):
        alpha_image = _keep_largest_alpha_component(alpha_image)

    edge_feather = float(settings.get("edge_feather", 0.4))
    if edge_feather > 0:
        alpha_image = alpha_image.filter(ImageFilter.GaussianBlur(edge_feather))

    rgba[:, :, 3] = np.array(alpha_image, dtype=np.uint8)
    rgba[rgba[:, :, 3] == 0, :3] = 255
    foreground_after = int((rgba[:, :, 3] > 0).sum())
    cleaned = Image.fromarray(rgba, mode="RGBA")
    return cleaned, {
        "foreground_pixels_before": original_foreground,
        "foreground_pixels_after": foreground_after,
        "removed_pixels": max(original_foreground - foreground_after, 0),
    }


def _keep_largest_alpha_component(alpha_image: Image.Image) -> Image.Image:
    try:
        import cv2
        import numpy as np
    except Exception:
        return alpha_image

    alpha = np.array(alpha_image, dtype=np.uint8)
    mask = (alpha > 0).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    if count <= 2:
        return alpha_image
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    alpha[labels != largest] = 0
    return Image.fromarray(alpha, mode="L")


def _generate_shape(pipeline, images, config: dict[str, Any], generator):
    generation = config["generation"]
    outputs = pipeline(
        image=images,
        num_inference_steps=int(generation.get("num_inference_steps", 50)),
        guidance_scale=float(generation.get("guidance_scale", 5.0)),
        generator=generator,
        octree_resolution=int(generation.get("octree_resolution", 256)),
        num_chunks=int(generation.get("num_chunks", 200000)),
        output_type="trimesh",
    )
    if isinstance(outputs, list):
        return outputs[0]
    return outputs


def _postprocess_mesh(mesh, config: dict[str, Any]):
    postprocess = config.get("postprocess", {})
    floor_settings = postprocess.get("floor_plate", {})
    finger_settings = postprocess.get("finger_guard", {})
    subdivide_settings = postprocess.get("subdivide", {})
    smooth_settings = postprocess.get("smooth", {})
    toe_settings = postprocess.get("toe_guard", {})
    report: dict[str, Any] = {
        "floor_plate": {
            "enabled": bool(floor_settings.get("enabled", True)),
            "removed": False,
        },
        "finger_guard": {
            "enabled": bool(finger_settings.get("enabled", True)),
            "applied": False,
        },
        "subdivide": {
            "enabled": bool(subdivide_settings.get("enabled", True)),
            "applied": False,
        },
        "smooth": {
            "enabled": bool(smooth_settings.get("enabled", True)),
            "applied": False,
        },
        "toe_guard": {
            "enabled": bool(toe_settings.get("enabled", True)),
            "applied": False,
        },
        "warnings": [],
    }
    processed = mesh

    if report["floor_plate"]["enabled"]:
        try:
            processed, floor_report = _remove_floor_plate(processed, floor_settings)
            report["floor_plate"].update(floor_report)
        except Exception as exc:  # noqa: BLE001 - keep generated mesh if postprocess cannot run.
            warning = f"Floor plate cleanup skipped: {exc}"
            report["warnings"].append(warning)
            report["floor_plate"]["error"] = str(exc)

    if report["finger_guard"]["enabled"]:
        try:
            processed, finger_report = _limit_finger_count(processed, finger_settings)
            report["finger_guard"].update(finger_report)
        except Exception as exc:  # noqa: BLE001 - keep generated mesh if anatomy guard cannot run.
            warning = f"Finger guard skipped: {exc}"
            report["warnings"].append(warning)
            report["finger_guard"]["error"] = str(exc)

    if report["subdivide"]["enabled"]:
        try:
            processed, subdivide_report = _subdivide_mesh(processed, subdivide_settings)
            report["subdivide"].update(subdivide_report)
        except Exception as exc:  # noqa: BLE001 - keep generated mesh if refinement cannot run.
            warning = f"Mesh subdivision skipped: {exc}"
            report["warnings"].append(warning)
            report["subdivide"]["error"] = str(exc)

    if report["smooth"]["enabled"]:
        try:
            processed, smooth_report = _smooth_mesh(processed, smooth_settings)
            report["smooth"].update(smooth_report)
        except Exception as exc:  # noqa: BLE001 - keep generated mesh if smoothing cannot run.
            warning = f"Mesh smoothing skipped: {exc}"
            report["warnings"].append(warning)
            report["smooth"]["error"] = str(exc)

    if report["toe_guard"]["enabled"]:
        try:
            processed, toe_report = _separate_toe_grooves(processed, toe_settings)
            report["toe_guard"].update(toe_report)
        except Exception as exc:  # noqa: BLE001 - keep generated mesh if toe guard cannot run.
            warning = f"Toe guard skipped: {exc}"
            report["warnings"].append(warning)
            report["toe_guard"]["error"] = str(exc)

    processed = _prepare_mesh_for_export(processed)
    return processed, report


def _subdivide_mesh(mesh, settings: dict[str, Any]):
    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh, {"applied": False, "reason": "unsupported mesh type"}
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, {"applied": False, "reason": "empty mesh"}

    iterations = max(0, int(settings.get("iterations", 1)))
    if iterations <= 0:
        return mesh, {"applied": False, "iterations": 0, "reason": "disabled by iteration count"}

    max_iterations = max(1, int(settings.get("max_iterations", 2)))
    iterations = min(iterations, max_iterations)
    before_faces = int(len(mesh.faces))
    before_vertices = int(len(mesh.vertices))
    max_faces = int(settings.get("max_faces", 800000))
    projected_faces = before_faces * (4 ** iterations)
    if max_faces > 0 and projected_faces > max_faces:
        return mesh, {
            "applied": False,
            "iterations": iterations,
            "faces_before": before_faces,
            "projected_faces": projected_faces,
            "max_faces": max_faces,
            "reason": "projected face count exceeds limit",
        }

    refined = mesh.copy()
    for _ in range(iterations):
        refined = refined.subdivide()
    refined.remove_unreferenced_vertices()

    return refined, {
        "applied": True,
        "iterations": iterations,
        "faces_before": before_faces,
        "faces_after": int(len(refined.faces)),
        "vertices_before": before_vertices,
        "vertices_after": int(len(refined.vertices)),
    }


def _limit_finger_count(mesh, settings: dict[str, Any]):
    import numpy as np

    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh, {"applied": False, "reason": "unsupported mesh type"}
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, {"applied": False, "reason": "empty mesh"}

    max_fingers = max(1, int(settings.get("max_fingers_per_hand", 5)))
    if max_fingers <= 0:
        return mesh, {"applied": False, "reason": "disabled by max finger count"}

    working = mesh.copy()
    bounds = np.asarray(working.bounds, dtype=np.float64)
    extents = bounds[1] - bounds[0]
    if float(extents.max()) <= 1e-6:
        return mesh, {"applied": False, "reason": "flat mesh bounds"}

    vertical_axis = _axis_index(settings.get("vertical_axis", "y"), 1)
    side_axis = _axis_index(settings.get("side_axis", "x"), -1)
    if side_axis < 0:
        horizontal_axes = [axis for axis in range(3) if axis != vertical_axis]
        side_axis = max(horizontal_axes, key=lambda axis: float(extents[axis]))
    projection_axes = [axis for axis in range(3) if axis != side_axis]
    if vertical_axis in projection_axes:
        projection_axes = [vertical_axis] + [axis for axis in projection_axes if axis != vertical_axis]

    report: dict[str, Any] = {
        "applied": False,
        "max_fingers_per_hand": max_fingers,
        "side_axis": ["x", "y", "z"][side_axis],
        "sides": [],
        "removed_faces": 0,
    }

    for sign in (1, -1):
        vertices = np.asarray(working.vertices)
        bounds = np.asarray(working.bounds, dtype=np.float64)
        extents = bounds[1] - bounds[0]
        axis_reports = [
            _find_finger_silhouette_peaks(vertices, bounds, extents, side_axis, sign, projection_axis, vertical_axis, settings)
            for projection_axis in projection_axes
        ]
        best = max(axis_reports, key=lambda item: len(item["peaks"]))
        side_report: dict[str, Any] = {
            "side": "+" if sign > 0 else "-",
            "projection_axis": best["projection_axis"],
            "detected": len(best["peaks"]),
            "removed": [],
        }

        if len(best["peaks"]) > max_fingers:
            extras = len(best["peaks"]) - max_fingers
            candidates = sorted(best["peaks"], key=lambda item: (item["prominence"], item["points"]))[:extras]
            for candidate in candidates:
                face_mask = _finger_tip_face_mask(working, side_axis, sign, best["projection_axis_index"], vertical_axis, candidate, settings)
                removed_faces = int(face_mask.sum())
                if removed_faces <= 0:
                    side_report["removed"].append({"projection": candidate["projection"], "removed_faces": 0, "reason": "no matching tip faces"})
                    continue
                face_ratio = removed_faces / max(int(len(working.faces)), 1)
                max_face_ratio = float(settings.get("max_removed_face_ratio", 0.035))
                if face_ratio > max_face_ratio:
                    side_report["removed"].append({
                        "projection": candidate["projection"],
                        "removed_faces": removed_faces,
                        "face_ratio": round(face_ratio, 5),
                        "reason": "removal would be too large",
                    })
                    continue

                working.update_faces(~face_mask)
                working.remove_unreferenced_vertices()
                report["removed_faces"] += removed_faces
                report["applied"] = True
                side_report["removed"].append({
                    "projection": candidate["projection"],
                    "removed_faces": removed_faces,
                    "face_ratio": round(face_ratio, 5),
                })

        report["sides"].append(side_report)

    if report["applied"] and settings.get("keep_largest_component", True):
        parts = working.split(only_watertight=False)
        if len(parts) > 1:
            largest = max(parts, key=lambda item: len(item.faces))
            if len(largest.faces) >= len(working.faces) * 0.45:
                working = largest

    if not report["applied"]:
        report["reason"] = "no side exceeded the finger limit"
        return mesh, report

    report["faces_after"] = int(len(working.faces))
    return _prepare_mesh_for_export(working), report


def _find_finger_silhouette_peaks(
    vertices,
    bounds,
    extents,
    side_axis: int,
    sign: int,
    projection_axis: int,
    vertical_axis: int,
    settings: dict[str, Any],
) -> dict[str, Any]:
    import numpy as np

    side_values = sign * vertices[:, side_axis]
    side_extent = float(extents[side_axis])
    projection_extent = float(extents[projection_axis])
    if side_extent <= 1e-6 or projection_extent <= 1e-6:
        return {"projection_axis": ["x", "y", "z"][projection_axis], "projection_axis_index": projection_axis, "peaks": []}

    side_limit = float(side_values.max()) - side_extent * float(settings.get("search_side_fraction", 0.32))
    mask = side_values >= side_limit
    if vertical_axis != projection_axis:
        vertical_min = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_min_fraction", 0.16)))
        vertical_max = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_max_fraction", 0.78)))
        mask &= (vertices[:, vertical_axis] >= vertical_min) & (vertices[:, vertical_axis] <= vertical_max)

    points = vertices[mask]
    if len(points) < int(settings.get("min_search_vertices", 320)):
        return {"projection_axis": ["x", "y", "z"][projection_axis], "projection_axis_index": projection_axis, "peaks": []}

    bins = max(24, int(settings.get("silhouette_bins", 96)))
    edges = np.linspace(float(bounds[0, projection_axis]), float(bounds[1, projection_axis]), bins + 1)
    values = np.full(bins, np.nan, dtype=np.float64)
    counts = np.zeros(bins, dtype=np.int64)
    projection = points[:, projection_axis]
    side = sign * points[:, side_axis]
    index = np.clip(np.searchsorted(edges, projection, side="right") - 1, 0, bins - 1)
    min_bin_points = max(1, int(settings.get("min_bin_points", 14)))
    for bin_index in range(bins):
        bin_mask = index == bin_index
        counts[bin_index] = int(bin_mask.sum())
        if counts[bin_index] >= min_bin_points:
            values[bin_index] = float(side[bin_mask].max())

    smooth = _smooth_series(values, max(3, int(settings.get("smooth_window", 5))))
    peaks = _local_silhouette_peaks(smooth, counts, edges, side_extent, projection_extent, settings)
    return {
        "projection_axis": ["x", "y", "z"][projection_axis],
        "projection_axis_index": projection_axis,
        "peaks": peaks,
    }


def _smooth_series(values, window: int):
    import numpy as np

    if window % 2 == 0:
        window += 1
    radius = window // 2
    smooth = values.copy()
    for index in range(len(values)):
        start = max(0, index - radius)
        end = min(len(values), index + radius + 1)
        segment = values[start:end]
        finite = segment[np.isfinite(segment)]
        smooth[index] = float(finite.mean()) if len(finite) else np.nan
    return smooth


def _local_silhouette_peaks(values, counts, edges, side_extent: float, projection_extent: float, settings: dict[str, Any]):
    import numpy as np

    min_prominence = side_extent * float(settings.get("min_prominence_fraction", 0.018))
    min_spacing = projection_extent * float(settings.get("min_peak_spacing_fraction", 0.035))
    window = max(2, int(settings.get("prominence_window", 5)))
    raw = []
    valid = np.isfinite(values) & (counts > 0)
    run_start = None
    for index, is_valid in enumerate(valid):
        if is_valid and run_start is None:
            run_start = index
        if (not is_valid or index == len(valid) - 1) and run_start is not None:
            run_end = index if is_valid and index == len(valid) - 1 else index - 1
            run_counts = int(counts[run_start:run_end + 1].sum())
            if run_counts >= int(settings.get("min_run_points", 18)):
                segment = values[run_start:run_end + 1]
                local_index = int(np.nanargmax(segment))
                peak_index = run_start + local_index
                projection = float((edges[peak_index] + edges[peak_index + 1]) * 0.5)
                raw.append({
                    "index": peak_index,
                    "projection": projection,
                    "side_value": float(values[peak_index]),
                    "prominence": min_prominence,
                    "points": run_counts,
                    "bin_width": float(edges[peak_index + 1] - edges[peak_index]),
                })
            run_start = None

    for index in range(1, len(values) - 1):
        value = values[index]
        if not np.isfinite(value):
            continue
        left = values[max(0, index - window):index]
        right = values[index + 1:min(len(values), index + window + 1)]
        left_finite = left[np.isfinite(left)]
        right_finite = right[np.isfinite(right)]
        if len(left_finite) == 0 or len(right_finite) == 0:
            continue
        if value < values[index - 1] or value < values[index + 1]:
            continue
        valley = max(float(left_finite.min()), float(right_finite.min()))
        prominence = float(value - valley)
        if prominence < min_prominence:
            continue
        projection = float((edges[index] + edges[index + 1]) * 0.5)
        raw.append({
            "index": index,
            "projection": projection,
            "side_value": float(value),
            "prominence": prominence,
            "points": int(counts[index]),
            "bin_width": float(edges[index + 1] - edges[index]),
        })

    selected = []
    for peak in sorted(raw, key=lambda item: item["side_value"], reverse=True):
        if all(abs(peak["projection"] - kept["projection"]) >= min_spacing for kept in selected):
            selected.append(peak)
    return sorted(selected, key=lambda item: item["projection"])


def _finger_tip_face_mask(mesh, side_axis: int, sign: int, projection_axis: int, vertical_axis: int, peak: dict[str, Any], settings: dict[str, Any]):
    import numpy as np

    vertices = np.asarray(mesh.vertices)
    faces = np.asarray(mesh.faces)
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    extents = bounds[1] - bounds[0]
    side_extent = float(extents[side_axis])
    projection_extent = float(extents[projection_axis])
    side_cut = float(peak["side_value"]) - side_extent * float(settings.get("remove_depth_fraction", 0.035))
    band = max(
        projection_extent * float(settings.get("remove_band_fraction", 0.032)),
        float(peak.get("bin_width", 0.0)) * float(settings.get("remove_bin_widths", 2.2)),
    )

    vertex_mask = (sign * vertices[:, side_axis] >= side_cut) & (np.abs(vertices[:, projection_axis] - float(peak["projection"])) <= band)
    if vertical_axis != projection_axis:
        vertical_min = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_min_fraction", 0.16)))
        vertical_max = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_max_fraction", 0.78)))
        vertex_mask &= (vertices[:, vertical_axis] >= vertical_min) & (vertices[:, vertical_axis] <= vertical_max)

    face_votes = vertex_mask[faces].sum(axis=1)
    centers = np.asarray(mesh.triangles_center)
    center_mask = (
        (sign * centers[:, side_axis] >= side_cut)
        & (np.abs(centers[:, projection_axis] - float(peak["projection"])) <= band)
    )
    if vertical_axis != projection_axis:
        center_mask &= (
            centers[:, vertical_axis] >= bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_min_fraction", 0.16))
        ) & (
            centers[:, vertical_axis] <= bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("vertical_max_fraction", 0.78))
        )
    return (face_votes >= int(settings.get("min_tip_face_vertices", 2))) | center_mask


def _axis_index(value, fallback: int) -> int:
    axis_map = {"x": 0, "y": 1, "z": 2}
    if isinstance(value, str):
        return axis_map.get(value.lower(), fallback)
    try:
        index = int(value)
    except Exception:
        return fallback
    return index if 0 <= index <= 2 else fallback


def _separate_toe_grooves(mesh, settings: dict[str, Any]):
    import numpy as np

    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh, {"applied": False, "reason": "unsupported mesh type"}
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, {"applied": False, "reason": "empty mesh"}

    toe_count = max(2, int(settings.get("toe_count", 5)))
    groove_count = toe_count - 1
    working = mesh.copy()
    vertices = np.asarray(working.vertices).copy()
    bounds = np.asarray(working.bounds, dtype=np.float64)
    extents = bounds[1] - bounds[0]
    if float(extents.max()) <= 1e-6:
        return mesh, {"applied": False, "reason": "flat mesh bounds"}

    vertical_axis = _axis_index(settings.get("vertical_axis", "y"), 1)
    width_axis = _axis_index(settings.get("width_axis", "x"), 0)
    depth_axis = _axis_index(settings.get("depth_axis", "z"), 2)
    report: dict[str, Any] = {
        "applied": False,
        "toe_count": toe_count,
        "feet": [],
        "vertical_axis": ["x", "y", "z"][vertical_axis],
        "width_axis": ["x", "y", "z"][width_axis],
        "depth_axis": ["x", "y", "z"][depth_axis],
    }

    low_min = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("foot_min_height_fraction", 0.0)))
    low_max = float(bounds[0, vertical_axis] + extents[vertical_axis] * float(settings.get("foot_max_height_fraction", 0.26)))
    base_mask = (vertices[:, vertical_axis] >= low_min) & (vertices[:, vertical_axis] <= low_max)
    if int(base_mask.sum()) < int(settings.get("min_foot_vertices", 700)):
        return mesh, {**report, "reason": "not enough lower-foot vertices", "lower_vertices": int(base_mask.sum())}

    width_center = float((bounds[0, width_axis] + bounds[1, width_axis]) * 0.5)
    foot_masks = []
    for side_sign in (-1, 1):
        side_mask = base_mask & ((vertices[:, width_axis] - width_center) * side_sign >= 0)
        if int(side_mask.sum()) >= int(settings.get("min_foot_vertices", 700)):
            foot_masks.append((side_sign, side_mask))
    if not foot_masks:
        foot_masks.append((0, base_mask))

    total_moved = 0
    for side_sign, foot_mask in foot_masks:
        foot_report = _apply_toe_grooves_to_foot(
            vertices,
            foot_mask,
            bounds,
            extents,
            side_sign,
            vertical_axis,
            width_axis,
            depth_axis,
            groove_count,
            settings,
        )
        total_moved += int(foot_report.get("moved_vertices", 0))
        report["feet"].append(foot_report)

    if total_moved <= 0:
        report["reason"] = "no foot tip region found"
        return mesh, report

    working.vertices = vertices
    working = _prepare_mesh_for_export(working)
    report["applied"] = True
    report["moved_vertices"] = total_moved
    report["faces_after"] = int(len(working.faces))
    return working, report


def _apply_toe_grooves_to_foot(
    vertices,
    foot_mask,
    bounds,
    extents,
    side_sign: int,
    vertical_axis: int,
    width_axis: int,
    depth_axis: int,
    groove_count: int,
    settings: dict[str, Any],
) -> dict[str, Any]:
    import numpy as np

    foot_indices = np.flatnonzero(foot_mask)
    foot_vertices = vertices[foot_indices]
    if len(foot_vertices) == 0:
        return {"side": side_sign, "applied": False, "reason": "empty foot mask"}

    depth_values = foot_vertices[:, depth_axis]
    depth_scores = []
    for depth_sign in (-1, 1):
        signed_depth = depth_sign * depth_values
        tip_threshold = float(np.quantile(signed_depth, float(settings.get("tip_quantile", 0.68))))
        tip_count = int((signed_depth >= tip_threshold).sum())
        depth_scores.append((tip_count, float(signed_depth.max() - np.median(signed_depth)), depth_sign, tip_threshold))
    _, _, depth_sign, tip_threshold = max(depth_scores, key=lambda item: (item[0], item[1]))

    signed_depth_all = depth_sign * vertices[:, depth_axis]
    tip_mask = foot_mask & (signed_depth_all >= tip_threshold)
    tip_indices = np.flatnonzero(tip_mask)
    if len(tip_indices) < int(settings.get("min_tip_vertices", 160)):
        return {
            "side": side_sign,
            "applied": False,
            "reason": "not enough toe tip vertices",
            "tip_vertices": int(len(tip_indices)),
        }

    tip_vertices = vertices[tip_indices]
    width_values = tip_vertices[:, width_axis]
    width_low = float(np.quantile(width_values, float(settings.get("width_low_quantile", 0.08))))
    width_high = float(np.quantile(width_values, float(settings.get("width_high_quantile", 0.92))))
    width_span = width_high - width_low
    min_width_span = float(extents[width_axis]) * float(settings.get("min_foot_width_fraction", 0.055))
    if width_span <= min_width_span:
        return {
            "side": side_sign,
            "applied": False,
            "reason": "toe tip width is too narrow",
            "width_span": round(width_span, 6),
        }

    depth_start = float(np.quantile(depth_sign * foot_vertices[:, depth_axis], float(settings.get("groove_start_quantile", 0.54))))
    depth_end = float((depth_sign * foot_vertices[:, depth_axis]).max())
    groove_length = max(depth_end - depth_start, 1e-6)
    groove_width = width_span * float(settings.get("groove_width_fraction", 0.055))
    depth_strength = float(extents[depth_axis]) * float(settings.get("depth_strength_fraction", 0.028))
    vertical_strength = float(extents[vertical_axis]) * float(settings.get("vertical_strength_fraction", 0.01))
    vertical_top = float(np.quantile(foot_vertices[:, vertical_axis], float(settings.get("top_surface_quantile", 0.48))))
    moved_vertices = set()
    grooves = []

    for groove in np.linspace(width_low, width_high, groove_count + 2)[1:-1]:
        width_distance = np.abs(vertices[:, width_axis] - groove)
        depth_along = np.clip((signed_depth_all - depth_start) / groove_length, 0.0, 1.0)
        groove_mask = foot_mask & (signed_depth_all >= depth_start) & (width_distance <= groove_width * 2.8)
        if int(groove_mask.sum()) < int(settings.get("min_groove_vertices", 24)):
            grooves.append({"width": round(float(groove), 6), "moved_vertices": 0})
            continue

        width_weight = np.exp(-((width_distance[groove_mask] / max(groove_width, 1e-6)) ** 2))
        length_weight = np.sin(depth_along[groove_mask] * np.pi * 0.5) ** float(settings.get("length_falloff_power", 1.2))
        weight = width_weight * length_weight
        masked_indices = np.flatnonzero(groove_mask)
        vertices[masked_indices, depth_axis] -= depth_sign * depth_strength * weight

        top_weight = np.clip(
            (vertices[masked_indices, vertical_axis] - vertical_top)
            / max(float(extents[vertical_axis]) * float(settings.get("top_falloff_fraction", 0.035)), 1e-6),
            0.0,
            1.0,
        )
        vertices[masked_indices, vertical_axis] -= vertical_strength * weight * top_weight
        moved_vertices.update(int(index) for index in masked_indices)
        grooves.append({"width": round(float(groove), 6), "moved_vertices": int(len(masked_indices))})

    return {
        "side": side_sign,
        "applied": bool(moved_vertices),
        "depth_sign": depth_sign,
        "tip_vertices": int(len(tip_indices)),
        "width_span": round(width_span, 6),
        "moved_vertices": int(len(moved_vertices)),
        "grooves": grooves,
    }


def _smooth_mesh(mesh, settings: dict[str, Any]):
    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh, {"applied": False, "reason": "unsupported mesh type"}
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, {"applied": False, "reason": "empty mesh"}

    iterations = max(0, int(settings.get("iterations", 6)))
    if iterations <= 0:
        return mesh, {"applied": False, "iterations": 0, "reason": "disabled by iteration count"}

    import trimesh

    method = str(settings.get("method", "humphrey")).lower()
    smoothed = mesh.copy()
    before_faces = int(len(smoothed.faces))
    before_vertices = int(len(smoothed.vertices))

    if method == "taubin":
        trimesh.smoothing.filter_taubin(
            smoothed,
            lamb=float(settings.get("lamb", 0.35)),
            nu=float(settings.get("nu", 0.34)),
            iterations=iterations,
        )
    else:
        method = "humphrey"
        trimesh.smoothing.filter_humphrey(
            smoothed,
            alpha=float(settings.get("alpha", 0.12)),
            beta=float(settings.get("beta", 0.45)),
            iterations=iterations,
        )

    smoothed = _prepare_mesh_for_export(smoothed)
    return smoothed, {
        "applied": True,
        "method": method,
        "iterations": iterations,
        "faces_before": before_faces,
        "faces_after": int(len(smoothed.faces)),
        "vertices_before": before_vertices,
        "vertices_after": int(len(smoothed.vertices)),
    }


def _prepare_mesh_for_export(mesh):
    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh
    prepared = mesh.copy()
    try:
        prepared.remove_unreferenced_vertices()
    except Exception:
        pass
    try:
        prepared.fix_normals()
    except Exception:
        pass
    try:
        _ = prepared.vertex_normals
    except Exception:
        pass
    return prepared


def _remove_floor_plate(mesh, settings: dict[str, Any]):
    import numpy as np

    if not hasattr(mesh, "faces") or not hasattr(mesh, "vertices"):
        return mesh, {"removed": False, "reason": "unsupported mesh type"}
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return mesh, {"removed": False, "reason": "empty mesh"}

    working = mesh.copy()
    bounds = np.asarray(working.bounds, dtype=np.float64)
    extents = bounds[1] - bounds[0]
    axis_map = {"x": 0, "y": 1, "z": 2}
    vertical_axis = axis_map.get(str(settings.get("vertical_axis", "y")).lower(), 1)
    vertical_extent = float(extents[vertical_axis])
    if vertical_extent <= 1e-6:
        return mesh, {"removed": False, "reason": "flat vertical extent"}

    horizontal_axes = [axis for axis in range(3) if axis != vertical_axis]
    face_centers = np.asarray(working.triangles_center)
    face_normals = np.asarray(working.face_normals)
    face_areas = np.asarray(working.area_faces)
    total_area = float(face_areas.sum())
    if total_area <= 0:
        return mesh, {"removed": False, "reason": "zero surface area"}

    min_v = float(bounds[0, vertical_axis])
    search_fraction = float(settings.get("search_height_fraction", 0.24))
    search_limit = min_v + vertical_extent * search_fraction
    normal_threshold = float(settings.get("normal_threshold", 0.72))
    flat_bottom = (
        (face_centers[:, vertical_axis] <= search_limit)
        & (np.abs(face_normals[:, vertical_axis]) >= normal_threshold)
    )
    flat_area_ratio = float(face_areas[flat_bottom].sum() / total_area)
    min_flat_area_ratio = float(settings.get("min_flat_area_ratio", 0.045))
    if not np.any(flat_bottom) or flat_area_ratio < min_flat_area_ratio:
        return mesh, {
            "removed": False,
            "reason": "no large flat bottom surface",
            "flat_area_ratio": round(flat_area_ratio, 4),
        }

    bin_fraction = float(settings.get("plane_bin_fraction", 0.012))
    bin_width = max(vertical_extent * bin_fraction, 1e-5)
    bins = np.arange(min_v, search_limit + bin_width * 2, bin_width)
    if len(bins) < 3:
        return mesh, {"removed": False, "reason": "not enough plane bins"}

    weights, edges = np.histogram(face_centers[flat_bottom, vertical_axis], bins=bins, weights=face_areas[flat_bottom])
    if len(weights) == 0 or float(weights.max()) <= 0:
        return mesh, {"removed": False, "reason": "no weighted flat plane"}

    strongest = float(weights.max())
    candidates = np.where(weights >= strongest * float(settings.get("plane_strength_ratio", 0.35)))[0]
    selected_bin = int(candidates[-1] if len(candidates) else np.argmax(weights))
    band_low = float(edges[selected_bin])
    band_high = float(edges[selected_bin + 1])
    plane_band = flat_bottom & (face_centers[:, vertical_axis] >= band_low) & (face_centers[:, vertical_axis] <= band_high)
    if not np.any(plane_band):
        return mesh, {"removed": False, "reason": "empty selected plane"}

    plane_y = float(np.average(face_centers[plane_band, vertical_axis], weights=face_areas[plane_band]))
    horizontal_span = np.ptp(face_centers[plane_band][:, horizontal_axes], axis=0)
    horizontal_extent = np.maximum(extents[horizontal_axes], 1e-6)
    coverage = horizontal_span / horizontal_extent
    min_coverage = float(settings.get("min_horizontal_coverage", 0.52))
    if float(np.min(coverage)) < min_coverage:
        return mesh, {
            "removed": False,
            "reason": "flat surface is not wide enough",
            "coverage": [round(float(value), 4) for value in coverage],
            "flat_area_ratio": round(flat_area_ratio, 4),
        }

    cut_padding = float(settings.get("cut_padding_fraction", 0.018))
    cut_value = plane_y + vertical_extent * cut_padding
    vertex_values = np.asarray(working.vertices)[:, vertical_axis]
    face_vertex_values = vertex_values[np.asarray(working.faces)]
    low_centers = face_centers[:, vertical_axis] <= cut_value
    low_vertices = face_vertex_values.max(axis=1) <= cut_value
    outer_center = bounds[:, horizontal_axes].mean(axis=0)
    horizontal_radius = np.linalg.norm(face_centers[:, horizontal_axes] - outer_center, axis=1)
    outer_threshold = float(settings.get("outer_radius_fraction", 0.34)) * max(float(extents[horizontal_axes[0]]), float(extents[horizontal_axes[1]]))
    outer_rim = (face_vertex_values.min(axis=1) <= cut_value) & (horizontal_radius >= outer_threshold)
    remove_faces = low_centers & (
        low_vertices
        | (np.abs(face_normals[:, vertical_axis]) >= normal_threshold * 0.75)
        | outer_rim
    )

    removed_faces = int(remove_faces.sum())
    if removed_faces == 0:
        return mesh, {"removed": False, "reason": "no faces under floor cut"}

    removed_area_ratio = float(face_areas[remove_faces].sum() / total_area)
    max_removed_area_ratio = float(settings.get("max_removed_area_ratio", 0.82))
    if removed_area_ratio > max_removed_area_ratio:
        return mesh, {
            "removed": False,
            "reason": "cut would remove too much of the mesh",
            "removed_area_ratio": round(removed_area_ratio, 4),
        }

    before_faces = int(len(working.faces))
    before_vertices = int(len(working.vertices))
    working.update_faces(~remove_faces)
    working.remove_unreferenced_vertices()

    if settings.get("keep_largest_component", True):
        parts = working.split(only_watertight=False)
        if len(parts) > 1:
            largest = max(parts, key=lambda item: len(item.faces))
            if len(largest.faces) >= len(working.faces) * 0.45:
                working = largest

    return working, {
        "removed": True,
        "axis": ["x", "y", "z"][vertical_axis],
        "cut_value": round(cut_value, 6),
        "plane_value": round(plane_y, 6),
        "flat_area_ratio": round(flat_area_ratio, 4),
        "removed_area_ratio": round(removed_area_ratio, 4),
        "coverage": [round(float(value), 4) for value in coverage],
        "faces_before": before_faces,
        "faces_after": int(len(working.faces)),
        "faces_removed": before_faces - int(len(working.faces)),
        "vertices_before": before_vertices,
        "vertices_after": int(len(working.vertices)),
    }


def _try_texture(config: dict[str, Any], output_dir: Path, mesh, images) -> tuple[Path | None, str | None, dict[str, Any] | None]:
    if not _texture_runtime_ready():
        return None, "Texture skipped: custom_rasterizer is not compiled yet.", None

    _append_log("Texture runtime found. Loading paint pipeline.")
    try:
        _patch_torchvision_functional_tensor()
        from hy3dpaint.convert_utils import create_glb_with_pbr_materials
        import textureGenPipeline as texture_pipeline_module
        from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

        source_dir = Path(config["paths"]["hunyuan_source_dir"])
        paint_root = Path(config["models"]["paint_root"])
        white_obj = output_dir / "white_mesh.obj"
        textured_obj = output_dir / "textured_mesh.obj"
        textured_glb = output_dir / "textured_mesh.glb"
        remesh_glb = output_dir / "textured_mesh.original.glb"
        mesh = _prepare_mesh_for_export(mesh)
        mesh.export(str(white_obj), include_normals=False)

        _patch_snapshot_download(paint_root)
        _patch_texture_remesh_target(texture_pipeline_module, config)
        conf = Hunyuan3DPaintConfig(
            max_num_view=int(config["generation"].get("texture_views", 6)),
            resolution=int(config["generation"].get("texture_resolution", 512)),
        )
        conf.multiview_pretrained_path = str(paint_root)
        conf.realesrgan_ckpt_path = config["models"]["realesrgan"]
        conf.multiview_cfg_path = str(source_dir / "hy3dpaint" / "cfgs" / "hunyuan-paint-pbr.yaml")
        conf.custom_pipeline = str(source_dir / "hy3dpaint" / "hunyuanpaintpbr")
        paint_pipeline = Hunyuan3DPaintPipeline(conf)
        texture_prompt, texture_prompt_report = _prepare_texture_prompt_image(images, config, output_dir)
        paint_pipeline(
            mesh_path=str(white_obj),
            image_path=texture_prompt,
            output_mesh_path=str(textured_obj),
            save_glb=False,
        )
        textures = {
            "albedo": str(textured_obj).replace(".obj", ".jpg"),
            "metallic": str(textured_obj).replace(".obj", "_metallic.jpg"),
            "roughness": str(textured_obj).replace(".obj", "_roughness.jpg"),
        }
        texture_material = _stabilize_pbr_textures(textures, config)
        create_glb_with_pbr_materials(str(textured_obj), textures, str(remesh_glb))
        _append_log("Baking texture colors back onto the white mesh geometry.")
        textured_postprocess = _bake_texture_to_shape_mesh(
            mesh,
            textured_obj,
            Path(textures["albedo"]),
            textured_glb,
            config,
            _primary_image(images),
        )
        warning = None
        if not textured_postprocess.get("applied"):
            warning = "Texture bake to white mesh failed; using the paint remesh geometry."
            shutil.copy2(remesh_glb, textured_glb)
            fallback_report = _refine_textured_glb(textured_glb, config)
            textured_postprocess["fallback_remesh"] = fallback_report
            textured_postprocess["warnings"] = [
                *textured_postprocess.get("warnings", []),
                warning,
            ]
        textured_postprocess["texture_prompt"] = texture_prompt_report
        textured_postprocess["texture_material"] = texture_material
        textured_postprocess["paint_remesh_glb"] = str(remesh_glb)
        _append_log(f"Texture bake method: {textured_postprocess.get('method') or 'fallback_remesh'}.")
        return textured_glb, warning, textured_postprocess
    except Exception as exc:  # noqa: BLE001 - shape output is still useful without texture.
        traceback.print_exc()
        return None, f"Texture failed, kept white mesh: {exc}", None


def _bake_texture_to_shape_mesh(
    shape_mesh,
    textured_obj: Path,
    albedo_path: Path,
    target_glb: Path,
    config: dict[str, Any],
    reference_image: Image.Image | None = None,
    shade_smooth: bool = True,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "enabled": True,
        "applied": False,
        "method": None,
        "target": str(target_glb),
        "source_mesh": str(textured_obj),
        "source_texture": str(albedo_path),
        "warnings": [],
    }
    if not textured_obj.exists():
        report["reason"] = f"textured OBJ was not found: {textured_obj}"
        return report
    if not albedo_path.exists():
        report["reason"] = f"albedo texture was not found: {albedo_path}"
        return report

    try:
        import numpy as np
        import trimesh

        source_mesh = _load_single_mesh(textured_obj)
        source_uv = _mesh_uv(source_mesh)
        source_image = np.asarray(Image.open(albedo_path).convert("RGB"), dtype=np.float32)
        shape = _prepare_mesh_for_export(shape_mesh)
        target_vertices = np.asarray(shape.vertices, dtype=np.float64)

        if target_vertices.size == 0 or len(shape.faces) == 0:
            report["reason"] = "white mesh is empty"
            return report
        if source_uv is None:
            report["warnings"].append("Textured OBJ has no UV coordinates; nearest vertex color fallback will be used.")

        colors = None
        transfer_report: dict[str, Any] = {}
        if source_uv is not None:
            try:
                colors, transfer_report = _transfer_albedo_by_nearest_surface(
                    target_vertices,
                    source_mesh,
                    source_uv,
                    source_image,
                )
            except Exception as exc:  # noqa: BLE001 - fallback keeps the texture pass usable.
                report["warnings"].append(f"Nearest surface texture bake skipped: {exc}")
                try:
                    colors, transfer_report = _transfer_albedo_by_nearest_faces_kdtree(
                        target_vertices,
                        source_mesh,
                        source_uv,
                        source_image,
                    )
                except Exception as fallback_exc:  # noqa: BLE001 - final fallback is nearest vertices.
                    report["warnings"].append(f"Nearest face texture bake skipped: {fallback_exc}")

        if colors is None:
            if source_uv is not None:
                source_colors = _sample_texture_rgb(source_image, source_uv)
            else:
                source_colors = _mesh_vertex_colors(source_mesh)
            colors, transfer_report = _transfer_albedo_by_nearest_vertices(
                target_vertices,
                np.asarray(source_mesh.vertices, dtype=np.float64),
                source_colors,
            )

        colors, color_report = _adjust_baked_vertex_colors(colors, config, reference_image)

        vertex_colors = np.column_stack(
            [
                np.clip(colors, 0, 255).astype(np.uint8),
                np.full((len(target_vertices), 1), 255, dtype=np.uint8),
            ]
        )
        shape.visual = trimesh.visual.ColorVisuals(mesh=shape, vertex_colors=vertex_colors)
        shape.export(str(target_glb))

        report.update(
            {
                "applied": True,
                "method": transfer_report.get("method"),
                "vertices": int(len(shape.vertices)),
                "faces": int(len(shape.faces)),
                "source_vertices": int(len(source_mesh.vertices)),
                "source_faces": int(len(source_mesh.faces)),
                "texture_size": [int(source_image.shape[1]), int(source_image.shape[0])],
                "transfer": transfer_report,
                "color": color_report,
            }
        )
        if shade_smooth:
            report["shade_smooth"] = _shade_smooth_glb(config, target_glb)
        else:
            report["shade_smooth"] = {
                "enabled": False,
                "applied": False,
                "reason": "skipped for texture color re-bake",
            }
        report["glb_material"] = _stabilize_glb_pbr_materials(target_glb, config)
    except Exception as exc:  # noqa: BLE001 - fall back to Hunyuan's remeshed texture output.
        traceback.print_exc()
        report["reason"] = str(exc)
        report["warnings"].append(f"Texture bake to white mesh failed: {exc}")
    return report


def _load_single_mesh(path: Path):
    import trimesh

    loaded = trimesh.load(str(path), force="scene", process=False)
    if not hasattr(loaded, "geometry"):
        return loaded
    geometries = list(loaded.geometry.values())
    if not geometries:
        raise ValueError(f"No geometry found in {path}")
    if len(geometries) == 1:
        return geometries[0]
    combined = trimesh.util.concatenate(tuple(geometries))
    if _mesh_uv(combined) is None:
        raise ValueError(f"Multiple geometries in {path} could not be combined with UVs intact.")
    return combined


def _mesh_uv(mesh):
    visual = getattr(mesh, "visual", None)
    uv = getattr(visual, "uv", None)
    if uv is None:
        return None
    import numpy as np

    uv = np.asarray(uv, dtype=np.float64)
    if uv.ndim != 2 or uv.shape[1] < 2 or len(uv) != len(mesh.vertices):
        return None
    return uv[:, :2]


def _mesh_vertex_colors(mesh):
    visual = getattr(mesh, "visual", None)
    colors = getattr(visual, "vertex_colors", None)
    if colors is None or len(colors) != len(mesh.vertices):
        raise ValueError("Textured mesh has neither UVs nor vertex colors.")
    import numpy as np

    return np.asarray(colors, dtype=np.float32)[:, :3]


def _transfer_albedo_by_nearest_surface(target_vertices, source_mesh, source_uv, source_image):
    import numpy as np
    import trimesh

    colors = np.empty((len(target_vertices), 3), dtype=np.float32)
    faces = np.asarray(source_mesh.faces, dtype=np.int64)
    source_vertices = np.asarray(source_mesh.vertices, dtype=np.float64)
    chunk_size = 50000
    max_distance = 0.0

    for start in range(0, len(target_vertices), chunk_size):
        end = min(start + chunk_size, len(target_vertices))
        points = target_vertices[start:end]
        closest, distances, face_ids = trimesh.proximity.closest_point(source_mesh, points)
        face_ids = np.asarray(face_ids, dtype=np.int64)
        valid = face_ids >= 0
        chunk_colors = np.zeros((len(points), 3), dtype=np.float32)

        if np.any(valid):
            valid_faces = faces[face_ids[valid]]
            triangles = source_vertices[valid_faces]
            barycentric = trimesh.triangles.points_to_barycentric(triangles, closest[valid])
            barycentric = np.nan_to_num(barycentric, nan=0.0, posinf=0.0, neginf=0.0)
            barycentric = np.clip(barycentric, 0.0, 1.0)
            totals = barycentric.sum(axis=1)
            good_totals = totals > 1e-8
            barycentric[good_totals] = barycentric[good_totals] / totals[good_totals, None]
            barycentric[~good_totals] = np.array([1.0, 0.0, 0.0])
            face_uv = source_uv[valid_faces]
            sampled_uv = np.sum(face_uv * barycentric[:, :, None], axis=1)
            chunk_colors[valid] = _sample_texture_rgb(source_image, sampled_uv)
            if len(distances):
                max_distance = max(max_distance, float(np.nanmax(distances[valid])))

        if np.any(~valid):
            fallback_colors, _ = _transfer_albedo_by_nearest_vertices(
                points[~valid],
                source_vertices,
                _sample_texture_rgb(source_image, source_uv),
            )
            chunk_colors[~valid] = fallback_colors

        colors[start:end] = chunk_colors

    return colors, {
        "method": "nearest_surface_uv",
        "chunk_size": chunk_size,
        "max_distance": round(max_distance, 6),
    }


def _transfer_albedo_by_nearest_faces_kdtree(target_vertices, source_mesh, source_uv, source_image):
    import numpy as np
    import trimesh
    from scipy.spatial import cKDTree

    faces = np.asarray(source_mesh.faces, dtype=np.int64)
    source_vertices = np.asarray(source_mesh.vertices, dtype=np.float64)
    if len(faces) == 0 or len(source_vertices) == 0:
        raise ValueError("Textured mesh has no faces.")

    triangles_all = source_vertices[faces]
    centers = triangles_all.mean(axis=1)
    tree = cKDTree(centers)
    candidate_count = min(12, len(faces))
    chunk_size = 30000
    colors = np.empty((len(target_vertices), 3), dtype=np.float32)
    max_distance = 0.0

    for start in range(0, len(target_vertices), chunk_size):
        end = min(start + chunk_size, len(target_vertices))
        points = target_vertices[start:end]
        try:
            _, face_candidates = tree.query(points, k=candidate_count, workers=-1)
        except TypeError:
            _, face_candidates = tree.query(points, k=candidate_count)
        face_candidates = np.asarray(face_candidates, dtype=np.int64)
        if candidate_count == 1:
            face_candidates = face_candidates.reshape(-1, 1)

        best_distance2 = np.full((len(points),), np.inf, dtype=np.float64)
        best_uv = np.zeros((len(points), 2), dtype=np.float64)

        for candidate_index in range(candidate_count):
            candidate_faces = face_candidates[:, candidate_index]
            candidate_triangles = triangles_all[candidate_faces]
            closest = trimesh.triangles.closest_point(candidate_triangles, points)
            distance2 = np.sum((closest - points) ** 2, axis=1)
            improved = distance2 < best_distance2
            if not np.any(improved):
                continue

            barycentric = trimesh.triangles.points_to_barycentric(
                candidate_triangles[improved],
                closest[improved],
            )
            barycentric = np.nan_to_num(barycentric, nan=0.0, posinf=0.0, neginf=0.0)
            barycentric = np.clip(barycentric, 0.0, 1.0)
            totals = barycentric.sum(axis=1)
            good_totals = totals > 1e-8
            barycentric[good_totals] = barycentric[good_totals] / totals[good_totals, None]
            barycentric[~good_totals] = np.array([1.0, 0.0, 0.0])

            candidate_vertex_ids = faces[candidate_faces[improved]]
            candidate_uv = source_uv[candidate_vertex_ids]
            best_uv[improved] = np.sum(candidate_uv * barycentric[:, :, None], axis=1)
            best_distance2[improved] = distance2[improved]

        colors[start:end] = _sample_texture_rgb(source_image, best_uv)
        finite_distances = best_distance2[np.isfinite(best_distance2)]
        if len(finite_distances):
            max_distance = max(max_distance, float(np.sqrt(np.max(finite_distances))))

    return colors, {
        "method": "nearest_face_uv_kdtree",
        "candidate_faces": int(candidate_count),
        "chunk_size": int(chunk_size),
        "max_distance": round(max_distance, 6),
    }


def _transfer_albedo_by_nearest_vertices(target_vertices, source_vertices, source_colors):
    import numpy as np
    from scipy.spatial import cKDTree

    source_count = int(len(source_vertices))
    if source_count == 0:
        raise ValueError("Textured mesh has no vertices.")
    k = min(4, source_count)
    tree = cKDTree(source_vertices)
    try:
        distances, indices = tree.query(target_vertices, k=k, workers=-1)
    except TypeError:
        distances, indices = tree.query(target_vertices, k=k)

    if k == 1:
        colors = np.asarray(source_colors, dtype=np.float32)[indices]
        max_distance = float(np.nanmax(distances)) if len(target_vertices) else 0.0
        return colors, {"method": "nearest_vertex", "neighbors": 1, "max_distance": round(max_distance, 6)}

    distances = np.asarray(distances, dtype=np.float64)
    indices = np.asarray(indices, dtype=np.int64)
    weights = 1.0 / np.maximum(distances, 1e-8)
    weights[~np.isfinite(weights)] = 0.0
    totals = weights.sum(axis=1)
    totals[totals <= 1e-8] = 1.0
    sampled = np.asarray(source_colors, dtype=np.float32)[indices]
    colors = np.sum(sampled * weights[:, :, None], axis=1) / totals[:, None]
    max_distance = float(np.nanmax(distances[:, 0])) if len(target_vertices) else 0.0
    return colors, {"method": "nearest_vertex", "neighbors": k, "max_distance": round(max_distance, 6)}


def _sample_texture_rgb(image_array, uv):
    import numpy as np

    if uv is None or len(uv) == 0:
        return np.zeros((0, 3), dtype=np.float32)

    height, width = image_array.shape[:2]
    coords = np.asarray(uv, dtype=np.float64)
    coords = np.nan_to_num(coords, nan=0.0, posinf=1.0, neginf=0.0)
    u = np.clip(coords[:, 0], 0.0, 1.0)
    v = np.clip(coords[:, 1], 0.0, 1.0)
    x = u * max(width - 1, 0)
    y = (1.0 - v) * max(height - 1, 0)

    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.clip(x0 + 1, 0, max(width - 1, 0))
    y1 = np.clip(y0 + 1, 0, max(height - 1, 0))
    wx = (x - x0)[:, None]
    wy = (y - y0)[:, None]

    top = image_array[y0, x0] * (1.0 - wx) + image_array[y0, x1] * wx
    bottom = image_array[y1, x0] * (1.0 - wx) + image_array[y1, x1] * wx
    return (top * (1.0 - wy) + bottom * wy).astype(np.float32)


def _adjust_baked_vertex_colors(colors, config: dict[str, Any], reference_image: Image.Image | None):
    settings = config.get("postprocess", {}).get("texture_bake", {})
    report: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", True)),
        "applied": False,
        "stats_before": _color_array_stats(colors),
    }
    if not report["enabled"]:
        report["stats_after"] = report["stats_before"]
        return colors, report

    import numpy as np

    adjusted = np.asarray(colors, dtype=np.float32).copy()
    palette_report = _reference_palette_match(adjusted, reference_image, settings)
    if palette_report.get("applied"):
        adjusted = palette_report.pop("colors")
        report["applied"] = True
    report["palette_match"] = palette_report

    saturation = float(settings.get("saturation", 1.0))
    if saturation > 0 and abs(saturation - 1.0) > 0.001:
        luma = _rgb_luma(adjusted)[:, None]
        adjusted = luma + (adjusted - luma) * saturation
        report["applied"] = True
        report["saturation"] = saturation

    contrast = float(settings.get("contrast", 1.0))
    if contrast > 0 and abs(contrast - 1.0) > 0.001:
        adjusted = (adjusted - 127.5) * contrast + 127.5
        report["applied"] = True
        report["contrast"] = contrast

    brightness = float(settings.get("brightness", 1.0))
    if brightness > 0 and abs(brightness - 1.0) > 0.001:
        adjusted *= brightness
        report["applied"] = True
        report["brightness"] = brightness

    gamma = float(settings.get("gamma", 1.0))
    if gamma > 0 and abs(gamma - 1.0) > 0.001:
        normalized = np.clip(adjusted / 255.0, 0.0, 1.0)
        adjusted = np.power(normalized, gamma) * 255.0
        report["applied"] = True
        report["gamma"] = gamma

    target_luma = float(settings.get("target_luma_mean", 0) or 0)
    if target_luma > 0:
        current_luma = float(np.mean(_rgb_luma(adjusted))) if len(adjusted) else 0.0
        if current_luma > 1e-6:
            min_gain = float(settings.get("min_luma_gain", 0.72))
            max_gain = float(settings.get("max_luma_gain", 1.35))
            gain = max(min_gain, min(max_gain, target_luma / current_luma))
            if abs(gain - 1.0) > 0.001:
                adjusted *= gain
                report["applied"] = True
            report["luma_gain"] = round(gain, 4)

    albedo_gain = _rebake_albedo_value(settings.get("albedo_gain", 1.0))
    if abs(albedo_gain - 1.0) > 0.001:
        adjusted *= albedo_gain
        report["applied"] = True
    report["albedo_gain"] = albedo_gain

    adjusted = np.clip(adjusted, 0, 255).astype(np.float32)
    report["stats_after"] = _color_array_stats(adjusted)
    return adjusted, report


def _reference_palette_match(colors, reference_image: Image.Image | None, settings: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {
        "enabled": bool(settings.get("palette_match", True)),
        "applied": False,
    }
    if not report["enabled"] or reference_image is None:
        if reference_image is None:
            report["reason"] = "missing reference image"
        return report

    import numpy as np

    reference_colors = _reference_image_colors(reference_image, settings)
    if reference_colors is None or len(reference_colors) < 64:
        report["reason"] = "not enough reference foreground pixels"
        return report

    source_mean = np.mean(colors, axis=0)
    source_std = np.maximum(np.std(colors, axis=0), 1.0)
    reference_mean = np.mean(reference_colors, axis=0)
    reference_std = np.maximum(np.std(reference_colors, axis=0), 1.0)
    matched = (colors - source_mean) / source_std * reference_std + reference_mean

    blend = max(0.0, min(1.0, float(settings.get("palette_blend", 0.55))))
    report.update(
        {
            "applied": blend > 0,
            "blend": round(blend, 4),
            "source_mean": [round(float(value), 2) for value in source_mean],
            "reference_mean": [round(float(value), 2) for value in reference_mean],
            "source_std": [round(float(value), 2) for value in source_std],
            "reference_std": [round(float(value), 2) for value in reference_std],
            "reference_pixels": int(len(reference_colors)),
        }
    )
    if blend <= 0:
        return report

    report["colors"] = colors * (1.0 - blend) + matched * blend
    return report


def _reference_image_colors(image: Image.Image, settings: dict[str, Any]):
    import numpy as np

    rgba = image.convert("RGBA")
    array = np.asarray(rgba, dtype=np.float32)
    rgb = array[:, :, :3].reshape(-1, 3)
    alpha = array[:, :, 3].reshape(-1)
    luma = _rgb_luma(rgb)

    alpha_threshold = float(settings.get("reference_alpha_threshold", 24))
    min_luma = float(settings.get("reference_min_luma", 8))
    max_luma = float(settings.get("reference_max_luma", 248))
    mask = (alpha > alpha_threshold) & (luma >= min_luma) & (luma <= max_luma)
    if np.count_nonzero(mask) < 64:
        mask = alpha > alpha_threshold
    if np.count_nonzero(mask) < 64:
        return None
    return rgb[mask]


def _color_array_stats(colors) -> dict[str, Any]:
    import numpy as np

    array = np.asarray(colors, dtype=np.float32)
    if array.size == 0:
        return {"count": 0}
    luma = _rgb_luma(array)
    return {
        "count": int(len(array)),
        "mean": [round(float(value), 2) for value in np.mean(array, axis=0)],
        "std": [round(float(value), 2) for value in np.std(array, axis=0)],
        "luma_mean": round(float(np.mean(luma)), 2),
    }


def _rgb_luma(colors):
    import numpy as np

    array = np.asarray(colors, dtype=np.float32)
    return array[..., 0] * 0.2126 + array[..., 1] * 0.7152 + array[..., 2] * 0.0722


def _stabilize_pbr_textures(textures: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("postprocess", {}).get("texture_material", {})
    report: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", True)),
        "applied": False,
    }
    if not report["enabled"]:
        return report

    albedo_report = _stabilize_albedo_map(Path(textures["albedo"]), settings)
    metallic_report = _stabilize_metallic_map(Path(textures["metallic"]), settings)
    roughness_report = _stabilize_roughness_map(Path(textures["roughness"]), settings)
    report.update(
        {
            "albedo": albedo_report,
            "metallic": metallic_report,
            "roughness": roughness_report,
        }
    )
    report["applied"] = any(
        bool(item.get("applied"))
        for item in (albedo_report, metallic_report, roughness_report)
    )
    return report


def _prepare_texture_prompt_image(images, config: dict[str, Any], output_dir: Path) -> tuple[Image.Image, dict[str, Any]]:
    settings = config.get("postprocess", {}).get("texture_prompt", {})
    source = _primary_image(images).convert("RGBA")
    report: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", False)),
        "source_size": source.size,
    }
    if not report["enabled"]:
        return source, report

    image = Image.new("RGB", source.size, (255, 255, 255))
    image.paste(source.convert("RGB"), mask=source.getchannel("A"))
    report["mean_before"] = _image_mean(image)

    gamma = float(settings.get("gamma", 1.0))
    if gamma > 0 and abs(gamma - 1.0) > 0.001:
        image = _apply_gamma(image, gamma)

    contrast = float(settings.get("contrast", 1.0))
    brightness = float(settings.get("brightness", 1.0))
    color = float(settings.get("color", 1.0))
    sharpness = float(settings.get("sharpness", 1.0))
    if contrast > 0 and abs(contrast - 1.0) > 0.001:
        image = ImageEnhance.Contrast(image).enhance(contrast)
    if brightness > 0 and abs(brightness - 1.0) > 0.001:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    if color > 0 and abs(color - 1.0) > 0.001:
        image = ImageEnhance.Color(image).enhance(color)
    if sharpness > 0 and abs(sharpness - 1.0) > 0.001:
        image = ImageEnhance.Sharpness(image).enhance(sharpness)

    report.update(
        {
            "mean_after": _image_mean(image),
            "gamma": gamma,
            "contrast": contrast,
            "brightness": brightness,
            "color": color,
            "sharpness": sharpness,
        }
    )
    if bool(settings.get("save", True)):
        target = output_dir / "texture_prompt.png"
        image.save(target)
        report["path"] = str(target)
    return image, report


def _stabilize_albedo_map(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"enabled": bool(settings.get("albedo_denoise", True)), "applied": False}
    if not report["enabled"]:
        return report
    if not path.exists():
        report["reason"] = "missing albedo map"
        return report

    try:
        image = Image.open(path).convert("RGB")
        report["mean_before"] = _image_mean(image)
        blend = float(settings.get("albedo_median_blend", 0.24))
        if blend > 0:
            median_size = max(3, int(settings.get("albedo_median_size", 3)))
            if median_size % 2 == 0:
                median_size += 1
            filtered = image.filter(ImageFilter.MedianFilter(median_size))
            blur_radius = float(settings.get("albedo_blur_radius", 0.35))
            if blur_radius > 0:
                filtered = filtered.filter(ImageFilter.GaussianBlur(blur_radius))
            image = Image.blend(image, filtered, min(1.0, blend))

        shadow_lift = int(settings.get("albedo_shadow_lift", 0))
        if shadow_lift > 0:
            image = image.point(lambda value: min(255, value + int(shadow_lift * (1.0 - value / 255.0))))

        brightness = float(settings.get("albedo_brightness", 1.0))
        if brightness != 1.0:
            image = ImageEnhance.Brightness(image).enhance(brightness)

        color = float(settings.get("albedo_color", 0.92))
        contrast = float(settings.get("albedo_contrast", 0.94))
        if color != 1.0:
            image = ImageEnhance.Color(image).enhance(color)
        if contrast != 1.0:
            image = ImageEnhance.Contrast(image).enhance(contrast)

        tone_report = _tone_balance_albedo(image, settings)
        if tone_report.get("applied"):
            image = tone_report.pop("image")
        report["tone_balance"] = tone_report

        image.save(path, quality=95)
        report["mean_after"] = _image_mean(image)
        report["applied"] = True
    except Exception as exc:  # noqa: BLE001 - keep texture pass if stabilization fails.
        report["error"] = str(exc)
    return report


def _apply_gamma(image: Image.Image, gamma: float) -> Image.Image:
    import numpy as np

    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    array = np.power(np.clip(array, 0.0, 1.0), gamma)
    return Image.fromarray(np.clip(array * 255.0, 0, 255).astype(np.uint8), mode="RGB")


def _tone_balance_albedo(image: Image.Image, settings: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"enabled": bool(settings.get("albedo_tone_balance", False)), "applied": False}
    if not report["enabled"]:
        return report

    import numpy as np

    array = np.asarray(image.convert("RGB"), dtype=np.float32)
    luma = array[:, :, 0] * 0.2126 + array[:, :, 1] * 0.7152 + array[:, :, 2] * 0.0722
    threshold = float(settings.get("albedo_active_threshold", 8))
    active = luma > threshold
    if int(active.sum()) < max(64, int(active.size * 0.01)):
        active = np.ones_like(luma, dtype=bool)

    mean_before = float(luma[active].mean()) if int(active.sum()) else float(luma.mean())
    min_mean = float(settings.get("albedo_min_luma_mean", 0))
    target_mean = float(settings.get("albedo_target_luma_mean", max(min_mean, mean_before)))
    max_gain = max(1.0, float(settings.get("albedo_max_gain", 2.0)))
    report.update(
        {
            "active_pixels": int(active.sum()),
            "mean_before": round(mean_before, 2),
            "min_luma_mean": min_mean,
            "target_luma_mean": target_mean,
            "max_gain": max_gain,
        }
    )
    if min_mean <= 0 or mean_before >= min_mean:
        report["reason"] = "luma already above minimum"
        return report

    gain = min(max_gain, max(1.0, target_mean / max(mean_before, 1.0)))
    balanced = np.clip(array * gain, 0, 255)
    report["gain"] = round(gain, 3)
    report["mean_after"] = round(float((
        balanced[:, :, 0] * 0.2126 + balanced[:, :, 1] * 0.7152 + balanced[:, :, 2] * 0.0722
    )[active].mean()), 2)
    report["image"] = Image.fromarray(balanced.astype(np.uint8), mode="RGB")
    report["applied"] = True
    return report


def _stabilize_metallic_map(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"enabled": bool(settings.get("metallic_enabled", True)), "applied": False}
    if not report["enabled"]:
        return report
    if not path.exists():
        report["reason"] = "missing metallic map"
        return report

    try:
        image = Image.open(path).convert("L")
        report["mean_before"] = _image_mean(image)
        scale = float(settings.get("metallic_scale", 0.03))
        max_value = int(settings.get("metallic_max", 6))
        image = image.point(lambda value: max(0, min(max_value, int(value * scale))))
        image.save(path, quality=95)
        report["mean_after"] = _image_mean(image)
        report["applied"] = True
    except Exception as exc:  # noqa: BLE001 - keep texture pass if stabilization fails.
        report["error"] = str(exc)
    return report


def _stabilize_roughness_map(path: Path, settings: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"enabled": bool(settings.get("roughness_enabled", True)), "applied": False}
    if not report["enabled"]:
        return report
    if not path.exists():
        report["reason"] = "missing roughness map"
        return report

    try:
        image = Image.open(path).convert("L")
        report["mean_before"] = _image_mean(image)
        min_value = int(settings.get("roughness_min", 220))
        max_value = int(settings.get("roughness_max", 255))
        lift = int(settings.get("roughness_lift", 120))
        image = image.point(lambda value: max(min_value, min(max_value, int(value + lift))))
        image.save(path, quality=95)
        report["mean_after"] = _image_mean(image)
        report["applied"] = True
    except Exception as exc:  # noqa: BLE001 - keep texture pass if stabilization fails.
        report["error"] = str(exc)
    return report


def _image_mean(image: Image.Image) -> float:
    stat = ImageStat.Stat(image)
    return round(float(sum(stat.mean) / len(stat.mean)), 2)


def _refine_textured_glb(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    postprocess = config.get("postprocess", {})
    subdivide_settings = postprocess.get("textured_subdivide")
    if subdivide_settings is None:
        subdivide_settings = postprocess.get("subdivide", {})
    smooth_settings = postprocess.get("textured_smooth")
    if smooth_settings is None:
        smooth_settings = postprocess.get("smooth", {})
    toe_settings = postprocess.get("toe_guard", {})
    report: dict[str, Any] = {
        "enabled": bool(
            subdivide_settings.get("enabled", True)
            or smooth_settings.get("enabled", True)
            or toe_settings.get("enabled", True)
            or postprocess.get("shade_smooth", {}).get("enabled", True)
        ),
        "applied": False,
        "path": str(path),
        "subdivide": {
            "enabled": bool(subdivide_settings.get("enabled", True)),
            "applied": False,
        },
        "smooth": {
            "enabled": bool(smooth_settings.get("enabled", True)),
            "applied": False,
        },
        "toe_guard": {
            "enabled": bool(toe_settings.get("enabled", True)),
            "applied": False,
        },
    }
    if not report["enabled"]:
        return report

    import trimesh

    scene = trimesh.load(path, force="scene")
    if not hasattr(scene, "geometry") or not scene.geometry:
        report["reason"] = "no geometry in textured glb"
        report["shade_smooth"] = _shade_smooth_glb(config, path)
        report["glb_material"] = _stabilize_glb_pbr_materials(path, config)
        report["applied"] = (
            bool(report["shade_smooth"].get("applied"))
            or bool(report["glb_material"].get("applied"))
        )
        return report
    if len(scene.geometry) != 1:
        report["reason"] = "multi-geometry textured glb is not refined yet"
        report["geometries"] = len(scene.geometry)
        report["shade_smooth"] = _shade_smooth_glb(config, path)
        report["glb_material"] = _stabilize_glb_pbr_materials(path, config)
        report["applied"] = (
            bool(report["shade_smooth"].get("applied"))
            or bool(report["glb_material"].get("applied"))
        )
        return report

    name, mesh = next(iter(scene.geometry.items()))
    refined = mesh
    changed = False
    warnings: list[str] = []

    if report["subdivide"]["enabled"]:
        try:
            refined, subdivide_report = _subdivide_mesh(refined, subdivide_settings)
            report["subdivide"].update(subdivide_report)
            changed = changed or bool(subdivide_report.get("applied"))
        except Exception as exc:  # noqa: BLE001 - keep textured output if refinement cannot run.
            warnings.append(f"Textured mesh subdivision skipped: {exc}")
            report["subdivide"]["error"] = str(exc)

    if report["smooth"]["enabled"]:
        try:
            refined, smooth_report = _smooth_mesh(refined, smooth_settings)
            report["smooth"].update(smooth_report)
            changed = changed or bool(smooth_report.get("applied"))
        except Exception as exc:  # noqa: BLE001 - keep textured output if smoothing cannot run.
            warnings.append(f"Textured mesh smoothing skipped: {exc}")
            report["smooth"]["error"] = str(exc)

    if report["toe_guard"]["enabled"]:
        try:
            refined, toe_report = _separate_toe_grooves(refined, toe_settings)
            report["toe_guard"].update(toe_report)
            changed = changed or bool(toe_report.get("applied"))
        except Exception as exc:  # noqa: BLE001 - keep textured output if toe guard cannot run.
            warnings.append(f"Textured toe guard skipped: {exc}")
            report["toe_guard"]["error"] = str(exc)

    if changed:
        refined_scene = trimesh.Scene()
        refined_scene.add_geometry(_prepare_mesh_for_export(refined), geom_name=name)
        refined_scene.export(str(path))

    report["shade_smooth"] = _shade_smooth_glb(config, path)
    report["glb_material"] = _stabilize_glb_pbr_materials(path, config)
    report["warnings"] = warnings
    report["applied"] = (
        changed
        or bool(report["shade_smooth"].get("applied"))
        or bool(report["glb_material"].get("applied"))
    )
    report["path"] = str(path)
    return report


def _stabilize_glb_pbr_materials(path: Path, config: dict[str, Any]) -> dict[str, Any]:
    settings = config.get("postprocess", {}).get("texture_material", {})
    report: dict[str, Any] = {
        "enabled": bool(settings.get("enabled", True)),
        "applied": False,
    }
    if not report["enabled"]:
        return report
    if path.suffix.lower() not in {".glb", ".gltf"} or not path.exists():
        report["reason"] = "missing glb/gltf"
        return report

    try:
        import pygltflib

        gltf = pygltflib.GLTF2().load(str(path))
        changed = False
        materials = gltf.materials or []
        for material in materials:
            pbr = material.pbrMetallicRoughness
            if pbr is None:
                continue
            if "glb_metallic_factor" in settings:
                target = float(settings.get("glb_metallic_factor", 0.0))
                current = 1.0 if pbr.metallicFactor is None else float(pbr.metallicFactor)
                next_value = min(current, target)
                if pbr.metallicFactor != next_value:
                    pbr.metallicFactor = next_value
                    changed = True
            if "glb_roughness_factor" in settings:
                target = float(settings.get("glb_roughness_factor", 1.0))
                current = 1.0 if pbr.roughnessFactor is None else float(pbr.roughnessFactor)
                next_value = max(current, target)
                if pbr.roughnessFactor != next_value:
                    pbr.roughnessFactor = next_value
                    changed = True
            if bool(settings.get("drop_metallic_roughness_texture", False)) and pbr.metallicRoughnessTexture is not None:
                pbr.metallicRoughnessTexture = None
                changed = True

        if changed:
            gltf.save(str(path))
        report.update({"materials": len(materials), "applied": changed})
    except Exception as exc:  # noqa: BLE001 - material cleanup must not fail a completed texture.
        report["error"] = str(exc)
    return report


def _shade_smooth_glb(config: dict[str, Any], path: Path) -> dict[str, Any]:
    settings = config.get("postprocess", {}).get("shade_smooth", {})
    if not bool(settings.get("enabled", True)):
        return {"enabled": False, "applied": False}
    if path.suffix.lower() not in {".glb", ".gltf"}:
        return {"enabled": True, "applied": False, "reason": "not a glb or gltf file"}

    blender = Path(config["paths"]["blender"])
    converter = Path(config["paths"]["hunyuan_source_dir"]).parents[1] / "tools" / "blender_convert.py"
    if not blender.exists():
        return {"enabled": True, "applied": False, "reason": f"Blender was not found: {blender}"}
    if not converter.exists():
        return {"enabled": True, "applied": False, "reason": f"Blender converter was not found: {converter}"}

    temp_path = path.with_name(f"{path.stem}.smooth{path.suffix}")
    try:
        result = subprocess.run(
            [
                str(blender),
                "--background",
                "--python",
                str(converter),
                "--",
                str(path),
                str(temp_path),
            ],
            check=True,
            text=True,
            capture_output=True,
        )
        if result.stdout:
            _append_log(result.stdout.strip())
        temp_path.replace(path)
        return {"enabled": True, "applied": True, "path": str(path)}
    except Exception as exc:  # noqa: BLE001 - smoothing display normals must not fail the job.
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return {"enabled": True, "applied": False, "error": str(exc)}


def _patch_torchvision_functional_tensor() -> None:
    if "torchvision.transforms.functional_tensor" in sys.modules:
        return

    try:
        from torchvision.transforms import functional as functional
    except Exception:
        return

    module = types.ModuleType("torchvision.transforms.functional_tensor")
    for name in dir(functional):
        setattr(module, name, getattr(functional, name))
    sys.modules["torchvision.transforms.functional_tensor"] = module


def _texture_runtime_ready() -> bool:
    try:
        return (
            importlib.util.find_spec("custom_rasterizer") is not None
            and importlib.util.find_spec("custom_rasterizer_kernel") is not None
        )
    except Exception:
        return False


def _patch_snapshot_download(paint_root: Path) -> None:
    import huggingface_hub

    original = huggingface_hub.snapshot_download

    def snapshot_download(repo_id, *args, **kwargs):
        if str(repo_id) == str(paint_root):
            return str(paint_root)
        return original(repo_id, *args, **kwargs)

    huggingface_hub.snapshot_download = snapshot_download


def _patch_texture_remesh_target(texture_pipeline_module, config: dict[str, Any]) -> None:
    target_count = int(config.get("generation", {}).get("texture_remesh_target", 40000))
    if target_count <= 0:
        return

    try:
        from utils.simplify_mesh_utils import mesh_simplify_trimesh
    except Exception:
        from hy3dpaint.utils.simplify_mesh_utils import mesh_simplify_trimesh

    def remesh_mesh(mesh_path, remesh_path):
        _append_log(f"Texture remesh target: {target_count} faces.")
        return mesh_simplify_trimesh(mesh_path, remesh_path, target_count=target_count)

    texture_pipeline_module.remesh_mesh = remesh_mesh


def _primary_image(images):
    if isinstance(images, dict):
        return images.get("front") or next(iter(images.values()))
    return images


def _convert_extra_formats(
    config: dict[str, Any],
    primary_glb: Path,
    formats: list[str],
    output_dir: Path,
) -> list[dict[str, str]]:
    outputs: list[dict[str, str]] = []
    requested = {item.lower() for item in formats}
    requested.discard("glb")
    if not requested:
        return outputs

    blender = Path(config["paths"]["blender"])
    converter = Path(config["paths"]["hunyuan_source_dir"]).parents[1] / "tools" / "blender_convert.py"
    if not blender.exists():
        raise FileNotFoundError(f"Blender was not found: {blender}")
    if not converter.exists():
        raise FileNotFoundError(f"Blender converter was not found: {converter}")

    for fmt in sorted(requested):
        if fmt not in {"obj", "fbx", "ply", "stl"}:
            continue
        target = output_dir / f"{primary_glb.stem}.{fmt}"
        if fmt == "obj" and target.exists():
            target = output_dir / f"{primary_glb.stem}_export.{fmt}"
        subprocess.run(
            [
                str(blender),
                "--background",
                "--python",
                str(converter),
                "--",
                str(primary_glb),
                str(target),
            ],
            check=True,
        )
        outputs.append(_output(fmt, target, f"{fmt.upper()} export"))
    return outputs


def _load_mesh(path: Path):
    import trimesh

    loaded = trimesh.load(path, force="scene")
    if hasattr(loaded, "geometry"):
        return trimesh.util.concatenate(tuple(loaded.geometry.values()))
    return loaded


def _load_existing_images_for_texture(job: dict[str, Any]):
    preprocessing_saved = job.get("preprocessing", {}).get("saved", {})
    payload = job["payload"]
    input_files = payload.get("input_files", {})
    if payload.get("mode") == "multiview":
        return {
            view: Image.open(preprocessing_saved.get(view) or input_files[view]).convert("RGBA")
            for view in VIEW_ORDER
            if view in input_files
        }
    return Image.open(preprocessing_saved.get("single") or input_files["single"]).convert("RGBA")


def _setup_runtime(config: dict[str, Any]) -> None:
    _configure_stdio()
    cache_dir = Path(config["paths"].get("cache_dir", Path(config["service"]["runs_dir"]).parent / ".cache"))
    hf_home = cache_dir / "huggingface"
    source_dir = Path(config["paths"]["hunyuan_source_dir"])

    os.environ["HF_HOME"] = str(hf_home)
    os.environ["HF_HUB_CACHE"] = str(hf_home / "hub")
    os.environ["TRANSFORMERS_CACHE"] = str(hf_home / "transformers")
    os.environ["XDG_CACHE_HOME"] = str(cache_dir)
    os.environ["HY3DGEN_MODELS"] = config["paths"]["models_root"]
    os.environ["PYTHONUTF8"] = "1"

    for path in (cache_dir, hf_home, hf_home / "hub", hf_home / "transformers"):
        path.mkdir(parents=True, exist_ok=True)

    paths = [
        source_dir,
        source_dir / "hy3dshape",
        source_dir / "hy3dpaint",
        source_dir / "hy3dpaint" / "custom_rasterizer",
    ]
    for path in paths:
        sys.path.insert(0, str(path))


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _make_generator(seed: int):
    import torch

    return torch.Generator(device="cpu").manual_seed(seed)


def _cuda_available() -> bool:
    import torch

    return torch.cuda.is_available()


def _output(fmt: str, path: Path, label: str) -> dict[str, str]:
    return {
        "format": fmt,
        "filename": path.name,
        "path": str(path),
        "label": label,
    }


def _read_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    return _expand_config_tokens(config, path.resolve().parents[1])


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _expand_config_tokens(value: Any, project_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _expand_config_tokens(item, project_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_config_tokens(item, project_root) for item in value]
    if isinstance(value, str):
        return value.replace("{project_root}", str(project_root))
    return value


def _update_job(job_path: Path, status: str, message: str, **extra: Any) -> None:
    job = _read_json(job_path)
    job["status"] = status
    job["message"] = message
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    if status != "failed" and "error" not in extra:
        job.pop("error", None)
    job.update(extra)
    with job_path.open("w", encoding="utf-8") as handle:
        json.dump(job, handle, indent=2)


def _append_log(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    main()
