from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


PUBLIC_OUTPUT_FORMATS = {"glb", "fbx", "obj", "ply", "stl"}
TEXTURE_OUTPUT_FORMATS = PUBLIC_OUTPUT_FORMATS | {"jpg", "jpeg", "png", "mtl"}
PREFERRED_GLB_OUTPUTS = ("textured_mesh.glb", "textured_mesh_stable.glb", "white_mesh.glb")


class JobStore:
    def __init__(self, runs_dir: Path):
        self.runs_dir = runs_dir
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        run_dir = self.runs_dir / job_id
        input_dir = run_dir / "input"
        output_dir = run_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        job = {
            "id": job_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": "created",
            "message": "Job created.",
            "run_dir": str(run_dir),
            "input_dir": str(input_dir),
            "output_dir": str(output_dir),
            "payload": payload,
            "outputs": [],
        }
        self.write(job)
        return job

    def write(self, job: dict[str, Any]) -> None:
        path = Path(job["run_dir"]) / "job.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(job, handle, indent=2)

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self.runs_dir / job_id / "job.json"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as handle:
            return self.hydrate_outputs(json.load(handle))

    def list_jobs(self, limit: int = 40) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        run_dirs = [path for path in self.runs_dir.iterdir() if path.is_dir()]
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for run_dir in run_dirs:
            if len(jobs) >= limit:
                break
            job_path = run_dir / "job.json"
            if not job_path.exists():
                continue
            try:
                with job_path.open("r", encoding="utf-8") as handle:
                    job = self.hydrate_outputs(json.load(handle))
            except (OSError, json.JSONDecodeError):
                continue
            jobs.append(self.summarize(job))
        return jobs

    def iter_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        run_dirs = [path for path in self.runs_dir.iterdir() if path.is_dir()]
        run_dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for run_dir in run_dirs:
            job_path = run_dir / "job.json"
            if not job_path.exists():
                continue
            try:
                with job_path.open("r", encoding="utf-8") as handle:
                    jobs.append(self.hydrate_outputs(json.load(handle)))
            except (OSError, json.JSONDecodeError):
                continue
        return jobs

    def summarize(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload", {})
        outputs = job.get("outputs", [])
        primary_output = self.preferred_glb(outputs)
        texture_versions = self.collect_texture_versions(job, outputs)
        return {
            "id": job.get("id", ""),
            "display_name": self.display_name(job),
            "created_at": job.get("created_at", ""),
            "updated_at": job.get("updated_at", job.get("created_at", "")),
            "status": job.get("status", "unknown"),
            "message": job.get("message", ""),
            "mode": payload.get("mode", "single"),
            "quality": payload.get("quality", "default"),
            "object_type": payload.get("object_type", "character"),
            "scale_preset": payload.get("scale_preset", "character"),
            "target_height_m": payload.get("target_height_m"),
            "texture_quality": payload.get("texture_quality", "fast"),
            "rebake_albedo": payload.get("rebake_albedo"),
            "texture_color": payload.get("texture_color"),
            "texture": bool(payload.get("texture")),
            "texture_versions": texture_versions,
            "rebake_texture_ready": job.get("rebake_texture_ready", False),
            "rebake_texture_missing": job.get("rebake_texture_missing", []),
            "ratings": job.get("ratings", {}),
            "outputs": outputs,
            "primary_output": primary_output,
            "has_model": primary_output is not None,
        }

    def hydrate_outputs(self, job: dict[str, Any]) -> dict[str, Any]:
        outputs = self.collect_outputs(job)
        if outputs:
            job["outputs"] = outputs
        job["texture_versions"] = self.collect_texture_versions(job, outputs)
        missing = self.missing_rebake_texture_inputs(job)
        job["rebake_texture_ready"] = not missing
        job["rebake_texture_missing"] = missing
        return job

    def collect_outputs(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        output_dir_text = str(job.get("output_dir") or "")
        output_dir = Path(output_dir_text) if output_dir_text else None
        outputs: list[dict[str, Any]] = []
        seen: set[str] = set()

        for output in job.get("outputs", []):
            filename = Path(str(output.get("filename", ""))).name
            fmt = str(output.get("format", Path(filename).suffix.lstrip(".").lower())).lower()
            if not self.is_public_output(filename, fmt) or filename in seen:
                continue
            metadata = self.output_metadata(output_dir / filename if output_dir else None)
            outputs.append(
                {
                    "format": fmt,
                    "filename": filename,
                    "path": str(output_dir / filename) if output_dir else filename,
                    "label": self.normalized_output_label(output, filename, fmt),
                    **metadata,
                }
            )
            seen.add(filename)

        if output_dir and output_dir.exists():
            for path in sorted(output_dir.iterdir(), key=lambda item: item.name.lower()):
                fmt = path.suffix.lstrip(".").lower()
                filename = path.name
                if filename in seen or not self.is_public_output(filename, fmt):
                    continue
                outputs.append(
                    {
                        "format": fmt,
                        "filename": filename,
                        "path": str(path),
                        "label": self.output_label(filename, fmt),
                        **self.output_metadata(path),
                    }
                )
                seen.add(filename)
        return outputs

    def preferred_glb(self, outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
        glbs = [output for output in outputs if output.get("format") == "glb"]
        for filename in PREFERRED_GLB_OUTPUTS:
            for output in glbs:
                if self.output_basename(output.get("filename")) == filename:
                    return output
        return glbs[0] if glbs else None

    def collect_texture_versions(
        self,
        job: dict[str, Any],
        outputs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        output_dir_text = str(job.get("output_dir") or "")
        output_dir = Path(output_dir_text) if output_dir_text else None
        versions: list[dict[str, Any]] = []
        seen: set[str] = set()

        for version in job.get("texture_versions", []) or []:
            normalized = self.normalize_texture_version(job, version, output_dir)
            if not normalized:
                continue
            version_id = normalized["id"]
            if version_id in seen:
                continue
            versions.append(normalized)
            seen.add(version_id)

        if not versions:
            legacy = self.legacy_texture_version(job, outputs or self.collect_outputs(job))
            if legacy:
                versions.append(legacy)

        versions.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return versions

    def normalize_texture_version(
        self,
        job: dict[str, Any],
        version: dict[str, Any],
        output_dir: Path | None,
    ) -> dict[str, Any] | None:
        if not isinstance(version, dict):
            return None
        normalized_outputs: list[dict[str, Any]] = []
        for output in version.get("outputs", []) or []:
            filename = str(output.get("filename") or "")
            fmt = str(output.get("format") or Path(filename).suffix.lstrip(".").lower()).lower()
            path = self.safe_output_path(output_dir, filename)
            basename = self.output_basename(filename)
            if not path or not self.is_texture_output(basename, fmt):
                continue
            normalized_outputs.append(
                {
                    "format": fmt,
                    "filename": self.output_relative_name(output_dir, path) if output_dir else filename,
                    "path": str(path),
                    "label": self.normalized_output_label(output, basename, fmt),
                    **self.output_metadata(path),
                }
            )

        primary_output = self.preferred_texture_glb(normalized_outputs)
        if not primary_output:
            return None

        payload = job.get("payload", {})
        created_at = str(version.get("created_at") or primary_output.get("modified_at") or job.get("updated_at") or job.get("created_at") or "")
        version_id = str(version.get("id") or primary_output.get("cache_key") or created_at or "texture")
        kind = str(version.get("kind") or "texture")
        return {
            "id": version_id,
            "job_id": job.get("id", ""),
            "kind": kind,
            "label": str(version.get("label") or self.texture_version_label(kind, created_at)),
            "created_at": created_at,
            "texture_quality": version.get("texture_quality", payload.get("texture_quality")),
            "object_type": version.get("object_type", payload.get("object_type")),
            "rebake_albedo": version.get("rebake_albedo", payload.get("rebake_albedo")),
            "texture_color": version.get("texture_color", payload.get("texture_color")),
            "outputs": normalized_outputs,
            "primary_output": primary_output,
        }

    def legacy_texture_version(self, job: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
        texture_outputs = [
            output for output in outputs
            if self.is_texture_output(self.output_basename(output.get("filename")), str(output.get("format", "")).lower())
        ]
        primary_output = self.preferred_texture_glb(texture_outputs)
        if not primary_output:
            return None
        created_at = str(primary_output.get("modified_at") or job.get("updated_at") or job.get("created_at") or "")
        payload = job.get("payload", {})
        return {
            "id": "current",
            "job_id": job.get("id", ""),
            "kind": "current",
            "label": self.texture_version_label("current", created_at),
            "created_at": created_at,
            "texture_quality": payload.get("texture_quality"),
            "object_type": payload.get("object_type"),
            "rebake_albedo": payload.get("rebake_albedo"),
            "texture_color": payload.get("texture_color"),
            "outputs": texture_outputs,
            "primary_output": primary_output,
        }

    def preferred_texture_glb(self, outputs: list[dict[str, Any]]) -> dict[str, Any] | None:
        glbs = [output for output in outputs if output.get("format") == "glb"]
        for filename in ("textured_mesh.glb", "textured_mesh_stable.glb"):
            for output in glbs:
                if self.output_basename(output.get("filename")) == filename:
                    return output
        return glbs[0] if glbs else None

    def output_metadata(self, path: Path | None) -> dict[str, Any]:
        if path is None or not path.exists():
            return {}
        stat = path.stat()
        return {
            "size": stat.st_size,
            "modified_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)),
            "cache_key": str(stat.st_mtime_ns),
        }

    def missing_rebake_texture_inputs(self, job: dict[str, Any]) -> list[str]:
        output_dir_text = str(job.get("output_dir") or "")
        if not output_dir_text:
            return ["white_mesh.glb", "textured_mesh.obj", "textured_mesh.jpg"]
        output_dir = Path(output_dir_text)
        required = ("white_mesh.glb", "textured_mesh.obj", "textured_mesh.jpg")
        return [filename for filename in required if not (output_dir / filename).exists()]

    def display_name(self, job: dict[str, Any]) -> str:
        created_at = str(job.get("created_at", "")).replace("T", " ")
        payload = job.get("payload", {})
        mode = "4 views" if payload.get("mode") == "multiview" else "1 image"
        if created_at:
            return f"Object {created_at} - {mode}"
        return f"Object {job.get('id', 'Generation')} - {mode}"

    def is_public_output(self, filename: str, fmt: str) -> bool:
        if fmt not in PUBLIC_OUTPUT_FORMATS:
            return False
        if ".original." in filename or ".smooth." in filename:
            return False
        if filename.endswith("_remesh.obj"):
            return False
        if filename == "raw_mesh.glb":
            return False
        return bool(filename)

    def is_texture_output(self, filename: str, fmt: str) -> bool:
        if fmt not in TEXTURE_OUTPUT_FORMATS:
            return False
        return filename.startswith("textured_mesh") and self.is_public_texture_filename(filename)

    def is_public_texture_filename(self, filename: str) -> bool:
        if ".original." in filename or ".smooth." in filename:
            return False
        if filename.endswith("_remesh.obj"):
            return False
        return bool(filename)

    def output_label(self, filename: str, fmt: str) -> str:
        labels = {
            "textured_mesh_stable.glb": "Stable textured mesh GLB",
            "textured_mesh.glb": "Textured mesh GLB",
            "textured_mesh.fbx": "Textured mesh FBX",
            "textured_mesh.obj": "Textured mesh OBJ",
            "textured_mesh_export.obj": "Textured mesh OBJ export",
            "textured_mesh_stable.fbx": "Stable textured mesh FBX",
            "textured_mesh_stable.obj": "Stable textured mesh OBJ",
            "textured_mesh.mtl": "Textured material MTL",
            "textured_mesh.jpg": "Textured albedo JPG",
            "textured_mesh_metallic.jpg": "Textured metallic JPG",
            "textured_mesh_roughness.jpg": "Textured roughness JPG",
            "white_mesh.glb": "White mesh GLB",
            "white_mesh.fbx": "White mesh FBX",
            "white_mesh.obj": "White mesh OBJ",
        }
        return labels.get(filename, f"{fmt.upper()} export")

    def normalized_output_label(self, output: dict[str, Any], filename: str, fmt: str) -> str:
        label = str(output.get("label") or "")
        generic_label = f"{fmt.upper()} export"
        if not label or label == generic_label:
            return self.output_label(filename, fmt)
        return label

    def texture_version_label(self, kind: str, created_at: str) -> str:
        labels = {
            "created": "Created texture",
            "reworked": "Reworked texture",
            "color_rebake": "Color re-bake",
            "current": "Current texture",
        }
        prefix = labels.get(kind, "Texture")
        timestamp = str(created_at or "").replace("T", " ")
        return f"{prefix} {timestamp}".strip()

    def output_basename(self, filename: Any) -> str:
        return Path(str(filename or "").replace("\\", "/")).name

    def output_relative_name(self, output_dir: Path | None, path: Path) -> str:
        if not output_dir:
            return path.name
        try:
            return path.resolve().relative_to(output_dir.resolve()).as_posix()
        except ValueError:
            return path.name

    def safe_output_path(self, output_dir: Path | None, filename: str) -> Path | None:
        if not output_dir or not filename:
            return None
        relative = Path(str(filename).replace("\\", "/"))
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            return None
        output_root = output_dir.resolve()
        path = (output_root / relative).resolve()
        if path != output_root and output_root not in path.parents:
            return None
        if not path.exists():
            return None
        return path

    def update(self, job: dict[str, Any], status: str, message: str, **extra: Any) -> dict[str, Any]:
        job["status"] = status
        job["message"] = message
        job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        job.update(extra)
        self.write(job)
        return job
