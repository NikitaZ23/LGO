from __future__ import annotations

import sys
from pathlib import Path

import bpy


def _args() -> tuple[Path, Path]:
    if "--" not in sys.argv:
        raise SystemExit("Usage: blender --background --python blender_convert.py -- input output")
    index = sys.argv.index("--")
    values = sys.argv[index + 1 :]
    if len(values) != 2:
        raise SystemExit("Expected input and output paths.")
    return Path(values[0]), Path(values[1])


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
        _soften_materials(obj)
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


def _soften_materials(obj) -> None:
    for slot in obj.material_slots:
        material = slot.material
        if material is None:
            continue
        if hasattr(material, "roughness"):
            try:
                material.roughness = max(float(material.roughness), 0.78)
            except (TypeError, ValueError):
                pass
        if hasattr(material, "metallic"):
            try:
                material.metallic = min(float(material.metallic), 0.04)
            except (TypeError, ValueError):
                pass
        if not material.use_nodes or material.node_tree is None:
            continue
        for node in material.node_tree.nodes:
            if node.type == "BSDF_PRINCIPLED":
                _set_input_min(node, "Roughness", 0.78)
                _set_input_max(node, "Metallic", 0.04)
                _set_input_max(node, "Specular IOR Level", 0.35)
                _set_input_max(node, "Specular Tint", 0.2)


def _set_input_min(node, name: str, minimum: float) -> None:
    socket = node.inputs.get(name)
    if socket is not None and hasattr(socket, "default_value"):
        try:
            socket.default_value = max(float(socket.default_value), minimum)
        except (TypeError, ValueError):
            pass


def _set_input_max(node, name: str, maximum: float) -> None:
    socket = node.inputs.get(name)
    if socket is not None and hasattr(socket, "default_value"):
        try:
            socket.default_value = min(float(socket.default_value), maximum)
        except (TypeError, ValueError):
            pass


def _export(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        bpy.ops.export_scene.gltf(filepath=str(path), export_format="GLB" if suffix == ".glb" else "GLTF_SEPARATE")
    elif suffix == ".fbx":
        bpy.ops.export_scene.fbx(filepath=str(path), path_mode="COPY", embed_textures=True)
    elif suffix == ".obj":
        bpy.ops.wm.obj_export(filepath=str(path))
    elif suffix == ".ply":
        bpy.ops.wm.ply_export(filepath=str(path))
    elif suffix == ".stl":
        bpy.ops.wm.stl_export(filepath=str(path))
    else:
        raise ValueError(f"Unsupported output format: {suffix}")


def main() -> None:
    input_path, output_path = _args()
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    _clear_scene()
    _import(input_path)
    _shade_smooth()
    _export(output_path)
    print(f"Converted {input_path} -> {output_path}")


if __name__ == "__main__":
    main()
