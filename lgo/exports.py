from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from lgo.settings import PROJECT_ROOT


def export_source(job: dict[str, Any], filename: str) -> Path:
    root = Path(job["output_dir"]).resolve()
    relative = Path(filename.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts or relative.suffix.lower() != ".glb":
        raise ValueError("Select a GLB result from this job.")
    registered = list(job.get("outputs", []))
    for version in job.get("texture_versions", []):
        registered.extend(version.get("outputs", []))
    if relative.as_posix() not in {str(item.get("filename", "")).replace("\\", "/") for item in registered}:
        raise ValueError("Selected result is not registered in this job.")
    source = (root / relative).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError("Selected GLB result was not found inside the job output directory.")
    return source


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_export(job: dict[str, Any], config: dict[str, Any], filename: str, fmt: str) -> dict[str, Any]:
    if fmt not in {"obj", "fbx"}:
        raise ValueError("Export format must be OBJ or FBX.")
    if fmt == "fbx" and not config.get("generation", {}).get("allow_fbx", True):
        raise ValueError("FBX export is disabled in the configuration.")
    source = export_source(job, filename)
    converter = PROJECT_ROOT / "tools" / "blender_convert.py"
    if not Path(config["paths"]["blender"]).is_file() or not converter.is_file():
        raise FileNotFoundError("Blender or the LGO converter was not found.")
    digest = file_hash(source)
    key = hashlib.sha256(f"1:{fmt}:{digest}:{file_hash(converter)}".encode()).hexdigest()
    return {"id": uuid.uuid4().hex, "source": source.relative_to(Path(job["output_dir"]).resolve()).as_posix(),
            "source_hash": digest, "source_cache_key": str(source.stat().st_mtime_ns),
            "format": fmt, "cache_key": key, "status": "running", "previous_status": job["status"]}


def cached_export(job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any] | None:
    root = Path(job["output_dir"]).resolve()
    for item in reversed(job.get("exports", [])):
        path = (root / item.get("filename", "")).resolve()
        if (item.get("cache_key") == request["cache_key"] and item.get("source") == request["source"]
                and root in path.parents and path.is_file() and path.stat().st_size):
            return item
    return None


def convert_export(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    request = job["export_request"]
    fmt = request["format"]
    if fmt not in {"obj", "fbx"}:
        raise ValueError("Unsupported export format.")
    source = export_source(job, request["source"])
    if file_hash(source) != request["source_hash"]:
        raise ValueError("The selected result changed. Start export again.")
    root = Path(job["output_dir"]).resolve()
    destination = root / "exports"
    if root not in destination.resolve().parents:
        raise ValueError("Invalid export directory.")
    destination.mkdir(exist_ok=True)
    converter = PROJECT_ROOT / "tools" / "blender_convert.py"
    artifact_id = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix=".export-", dir=destination) as directory:
        work = Path(directory)
        source_copy = work / "source.glb"
        shutil.copy2(source, source_copy)
        if file_hash(source_copy) != request["source_hash"]:
            raise ValueError("The selected result changed while copying.")
        target = work / f"{source.stem}.{fmt}"
        subprocess.run([str(config["paths"]["blender"]), "--background", "--factory-startup",
                        "--python-exit-code", "1", "--python", str(converter), "--",
                        str(source_copy), str(target), "--no-postprocess"],
                       cwd=work, check=True, timeout=900, stdin=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"Blender did not produce a valid {fmt.upper()} file.")
        download_name = target.name
        if fmt == "obj":
            archive = work / f"{source.stem}_obj.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
                for path in sorted(work.rglob("*")):
                    if path.is_file() and path not in {source_copy, archive}:
                        package.write(path, path.relative_to(work).as_posix())
            target = archive
            download_name = archive.name
        final = destination / f"{artifact_id}{target.suffix}"
        target.replace(final)
    return {"id": artifact_id, "source": request["source"], "format": fmt,
            "source_cache_key": request["source_cache_key"], "cache_key": request["cache_key"],
            "filename": final.relative_to(root).as_posix(), "download_name": download_name,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
