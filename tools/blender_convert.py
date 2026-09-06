from __future__ import annotations

import sys
from pathlib import Path

import bpy


def _args() -> tuple[Path, Path, bool]:
    if "--" not in sys.argv:
        raise SystemExit("Usage: blender --background --python blender_convert.py -- input output")
    index = sys.argv.index("--")
    values = sys.argv[index + 1 :]
    no_postprocess = "--no-postprocess" in values
    values = [value for value in values if value != "--no-postprocess"]
    if len(values) != 2:
        raise SystemExit("Expected input and output paths.")
    return Path(values[0]), Path(values[1]), no_postprocess


def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()


def _import(path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif suffix == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(path))
    elif suffix == ".ply":
        bpy.ops.wm.ply_import(filepath=str(path))
    elif suffix == ".stl":
        bpy.ops.wm.stl_import(filepath=str(path))
    else:
        raise ValueError(f"Unsupported input format: {suffix}")


def _shade_smooth() -> None:
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH":
            continue
        for polygon in obj.data.polygons:
            polygon.use_smooth = True
        _apply_weighted_normals(obj)
        obj.data.update()


def _apply_weighted_normals(obj) -> None:
    try:
        modifier = obj.modifiers.new(name="LGO weighted normals", type="WEIGHTED_NORMAL")
        if hasattr(modifier, "keep_sharp"):
            modifier.keep_sharp = False
        if hasattr(modifier, "weight"):
            modifier.weight = 50
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    except Exception:
        obj.data.update()


def _export(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB" if suffix == ".glb" else "GLTF_SEPARATE")
    elif suffix == ".fbx":
        _write_textures(path.parent)
        bpy.ops.export_scene.fbx(filepath=str(path), path_mode="COPY", embed_textures=True)
    elif suffix == ".obj":
        _write_textures(path.parent)
        bpy.ops.wm.obj_export(filepath=str(path), path_mode="RELATIVE", export_materials=True,
                              export_pbr_extensions=True, export_colors=True)
    elif suffix == ".ply":
        bpy.ops.wm.ply_export(filepath=str(path))
    elif suffix == ".stl":
        bpy.ops.wm.stl_export(filepath=str(path))
    else:
        raise ValueError(f"Unsupported output format: {suffix}")


def _write_textures(directory: Path) -> None:
    for index, image in enumerate(bpy.data.images):
        if image.type != "IMAGE" or (not image.packed_file and not image.has_data):
            continue
        folder = directory / "textures"
        folder.mkdir(exist_ok=True)
        extension = {"JPEG": ".jpg", "PNG": ".png"}.get(image.file_format, ".png")
        target = folder / f"texture_{index}{extension}"
        if image.packed_file:
            target.write_bytes(bytes(image.packed_file.data))
            # The OBJ exporter skips images still marked as packed.
            image.unpack(method="REMOVE")
        else:
            image.file_format = "PNG"
            target = target.with_suffix(".png")
            image.filepath_raw = str(target)
            image.save()
        image.filepath = str(target)


def main() -> None:
    input_path, output_path, no_postprocess = _args()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    _clear_scene()
    _import(input_path)
    if not no_postprocess:
        _shade_smooth()
    _export(output_path)
    print(f"Converted {input_path} -> {output_path}")


if __name__ == "__main__":
    main()
