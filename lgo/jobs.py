from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


PUBLIC_OUTPUT_FORMATS = {"glb", "fbx", "obj", "ply", "stl"}
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

    def summarize(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload", {})
        outputs = job.get("outputs", [])
        primary_output = self.preferred_glb(outputs)
        return {
            "id": job.get("id", ""),
            "display_name": self.display_name(job),
            "created_at": job.get("created_at", ""),
            "updated_at": job.get("updated_at", job.get("created_at", "")),
            "status": job.get("status", "unknown"),
            "message": job.get("message", ""),
            "mode": payload.get("mode", "single"),
            "quality": payload.get("quality", "default"),
            "object_type": payload.get("object_type", "organic"),
            "texture_quality": payload.get("texture_quality", "fast"),
            "rebake_albedo": payload.get("rebake_albedo"),
            "texture_color": payload.get("texture_color"),
            "texture": bool(payload.get("texture")),
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
                if output.get("filename") == filename:
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

    def output_label(self, filename: str, fmt: str) -> str:
        labels = {
            "textured_mesh_stable.glb": "Stable textured mesh GLB",
            "textured_mesh.glb": "Textured mesh GLB",
            "textured_mesh.fbx": "Textured mesh FBX",
            "textured_mesh.obj": "Textured mesh OBJ",
            "textured_mesh_stable.fbx": "Stable textured mesh FBX",
            "textured_mesh_stable.obj": "Stable textured mesh OBJ",
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

    def update(self, job: dict[str, Any], status: str, message: str, **extra: Any) -> dict[str, Any]:
        job["status"] = status
        job["message"] = message
        job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        job.update(extra)
        self.write(job)
        return job
