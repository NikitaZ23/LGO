from __future__ import annotations

import argparse
import cgi
import json
import mimetypes
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from lgo.environment import check_environment
from lgo.generation import GenerationService
from lgo.jobs import JobStore
from lgo.settings import PROJECT_ROOT, load_config


CONFIG = load_config()
RUNS_DIR = Path(CONFIG["service"]["runs_dir"])
STORE = JobStore(RUNS_DIR)
GENERATOR = GenerationService(CONFIG)
WEB_ROOT = PROJECT_ROOT / "web"


class LGOHandler(SimpleHTTPRequestHandler):
    server_version = "LGO/0.1"

    def do_GET(self) -> None:
        request_url = urlparse(self.path)
        request_path = request_url.path
        if request_path == "/" or request_path == "/index.html":
            return self._serve_file(WEB_ROOT / "index.html")
        if request_path == "/styles.css":
            return self._serve_file(WEB_ROOT / "styles.css")
        if request_path == "/app.js":
            return self._serve_file(WEB_ROOT / "app.js")
        if request_path in {
            "/favicon.svg",
            "/favicon.ico",
            "/favicon-16.png",
            "/favicon-32.png",
            "/favicon-48.png",
            "/favicon-64.png",
            "/apple-touch-icon.png",
        }:
            return self._serve_file(WEB_ROOT / request_path.lstrip("/"))
        if request_path == "/api/health":
            return self._json(check_environment(CONFIG))
        if request_path == "/api/jobs":
            query = parse_qs(request_url.query)
            limit = _int_query(query, "limit", 40)
            return self._json({"jobs": STORE.list_jobs(limit)})
        if request_path.startswith("/api/jobs/"):
            parts = request_path.strip("/").split("/")
            if len(parts) < 3:
                return self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
            job_id = parts[2]
            job = STORE.get(job_id)
            if job is None:
                return self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
            if len(parts) == 4 and parts[3] == "log":
                return self._serve_job_log(job)
            if len(parts) == 5 and parts[3] == "outputs":
                return self._serve_job_output(job, unquote(parts[4]))
            return self._json(job)
        return self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_url = urlparse(self.path)
        request_path = request_url.path
        query = parse_qs(request_url.query)
        if request_path == "/api/shutdown":
            self._json({"ok": True, "message": "LGO shutdown command sent."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if request_path == "/api/restart":
            try:
                restart_info = _spawn_restart_helper(_server_host(self.server), _server_port(self.server))
            except Exception as exc:  # noqa: BLE001 - surface restart setup errors to the UI.
                return self._json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            self._json({"ok": True, "message": "LGO restart command sent.", **restart_info})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if request_path.startswith("/api/jobs/"):
            parts = request_path.strip("/").split("/")
            if len(parts) == 4 and parts[3] == "texture":
                return self._add_texture(parts[2], _texture_quality_query(query), _object_type_query(query))
            if len(parts) == 4 and parts[3] == "rebake-texture":
                return self._rebake_texture(
                    parts[2],
                    _texture_quality_query(query),
                    _object_type_query(query),
                    _rebake_albedo_query(query),
                )
            if len(parts) == 4 and parts[3] == "rating":
                return self._rate_job(parts[2])
            return self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

        if request_path != "/api/jobs":
            return self._json({"error": "Not found."}, HTTPStatus.NOT_FOUND)

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )

        mode = _field(form, "mode", "single")
        quality = _quality_field(form)
        object_type = _object_type_field(form)
        texture_quality = _texture_quality_field(form)
        texture = _field(form, "texture", "false") == "true"
        formats = _list_field(form, "formats") or CONFIG["generation"]["default_formats"]

        payload = {
            "mode": mode,
            "quality": quality,
            "object_type": object_type,
            "texture_quality": texture_quality,
            "texture": texture,
            "formats": formats,
            "input_files": {},
        }
        job = STORE.create(payload)

        try:
            self._save_inputs(form, job, payload)
            manifest = GENERATOR.prepare(job)
            if not GENERATOR.can_run_real_generation():
                message = (
                    "Inputs saved. Hunyuan3D source runtime is not installed yet, "
                    "so real generation is not connected."
                )
                STORE.update(job, "needs_runtime", message, manifest=str(Path(job["run_dir"]) / "manifest.json"))
            else:
                STORE.update(
                    job,
                    "queued",
                    "Inputs saved. Hunyuan3D generation queued.",
                    manifest=str(Path(job["run_dir"]) / "manifest.json"),
                )
                process_info = GENERATOR.start(job)
                STORE.update(job, "running", "Hunyuan3D generation started.", **process_info)
        except Exception as exc:  # noqa: BLE001 - surface local service errors as JSON.
            STORE.update(job, "failed", str(exc))

        return self._json(job, HTTPStatus.CREATED)

    def _add_texture(self, job_id: str, texture_quality: str, object_type: str | None) -> None:
        job = STORE.get(job_id)
        if job is None:
            return self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
        if job.get("status") not in {"completed", "completed_with_warnings"}:
            return self._json({"error": "The mesh must finish generating before texture can be added."}, HTTPStatus.CONFLICT)

        white_mesh = Path(job["output_dir"]) / "white_mesh.glb"
        if not white_mesh.exists():
            return self._json({"error": "white_mesh.glb was not found for this job."}, HTTPStatus.CONFLICT)

        payload = job.get("payload", {})
        payload["texture"] = True
        payload["texture_quality"] = texture_quality
        if object_type:
            payload["object_type"] = object_type
        else:
            payload.setdefault("object_type", _default_object_type())
        job["payload"] = payload
        STORE.write(job)

        try:
            manifest = GENERATOR.prepare(job)
            if not GENERATOR.can_run_real_generation():
                message = (
                    "Texture request saved. Hunyuan3D source runtime is not installed yet, "
                    "so texture generation is not connected."
                )
                job = STORE.update(job, "needs_runtime", message, manifest=str(Path(job["run_dir"]) / "manifest.json"))
            else:
                job = STORE.update(
                    job,
                    "queued_texture",
                    "Texture pass queued.",
                    manifest=str(Path(job["run_dir"]) / "manifest.json"),
                )
                process_info = GENERATOR.start_texture(job)
                job = STORE.update(job, "running_texture", "Texture pass started.", **process_info)
        except Exception as exc:  # noqa: BLE001 - surface local service errors as JSON.
            job = STORE.update(job, "failed", str(exc))

        return self._json(job)

    def _rebake_texture(
        self,
        job_id: str,
        texture_quality: str,
        object_type: str | None,
        rebake_albedo: float,
    ) -> None:
        job = STORE.get(job_id)
        if job is None:
            return self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
        if job.get("status") not in {"completed", "completed_with_warnings"}:
            return self._json({"error": "The texture must finish before color re-bake can run."}, HTTPStatus.CONFLICT)

        output_dir = Path(job["output_dir"])
        required = [output_dir / "white_mesh.glb", output_dir / "textured_mesh.obj", output_dir / "textured_mesh.jpg"]
        missing = [path.name for path in required if not path.exists()]
        if missing:
            return self._json(
                {
                    "error": (
                        "Texture re-bake needs white_mesh.glb, textured_mesh.obj, and textured_mesh.jpg. "
                        f"Missing: {', '.join(missing)}"
                    )
                },
                HTTPStatus.CONFLICT,
            )

        payload = job.get("payload", {})
        payload["texture"] = True
        payload["texture_quality"] = texture_quality
        payload["rebake_albedo"] = rebake_albedo
        if object_type:
            payload["object_type"] = object_type
        else:
            payload.setdefault("object_type", _default_object_type())
        job["payload"] = payload
        STORE.write(job)

        try:
            manifest = GENERATOR.prepare(job)
            if not GENERATOR.can_run_real_generation():
                message = (
                    "Texture color re-bake saved. Hunyuan3D source runtime is not installed yet, "
                    "so the runner is not connected."
                )
                job = STORE.update(job, "needs_runtime", message, manifest=str(Path(job["run_dir"]) / "manifest.json"))
            else:
                job = STORE.update(
                    job,
                    "queued_texture",
                    "Texture color re-bake queued.",
                    manifest=str(Path(job["run_dir"]) / "manifest.json"),
                )
                process_info = GENERATOR.start_texture_rebake(job)
                job = STORE.update(job, "running_texture", "Texture color re-bake started.", **process_info)
        except Exception as exc:  # noqa: BLE001 - surface local service errors as JSON.
            job = STORE.update(job, "failed", str(exc))

        return self._json(job)

    def _rate_job(self, job_id: str) -> None:
        job = STORE.get(job_id)
        if job is None:
            return self._json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)

        try:
            payload = self._read_json_payload()
            target = str(payload.get("target", "")).lower()
            rating = int(payload.get("rating", 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._json({"error": "Rating payload must contain target and rating 1-5."}, HTTPStatus.BAD_REQUEST)

        if target not in {"white", "texture"}:
            return self._json({"error": "Rating target must be white or texture."}, HTTPStatus.BAD_REQUEST)
        if rating < 1 or rating > 5:
            return self._json({"error": "Rating must be between 1 and 5."}, HTTPStatus.BAD_REQUEST)
        if not _has_rating_target(job, target):
            return self._json({"error": f"No {target} model output is available for rating."}, HTTPStatus.CONFLICT)

        ratings = job.setdefault("ratings", {})
        ratings[target] = rating
        STORE.write(job)
        return self._json(job)

    def _read_json_payload(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("JSON payload must be an object.")
        return payload

    def _save_inputs(self, form: cgi.FieldStorage, job: dict[str, Any], payload: dict[str, Any]) -> None:
        mode = payload["mode"]
        fields = ["single"] if mode == "single" else ["front", "back", "left", "right"]
        input_dir = Path(job["input_dir"])

        for field in fields:
            item = form[field] if field in form else None
            if item is None or not getattr(item, "filename", ""):
                raise ValueError(f"Missing required image field: {field}")

            filename = Path(item.filename).name
            suffix = Path(filename).suffix.lower() or ".png"
            target = input_dir / f"{field}{suffix}"
            with target.open("wb") as handle:
                handle.write(item.file.read())
            payload["input_files"][field] = str(target)

    def _serve_file(self, path: Path, cache_control: str | None = None) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        elif path.name == "index.html" or path.name.startswith("favicon") or path.name == "apple-touch-icon.png":
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(content)

    def _serve_job_output(self, job: dict[str, Any], filename: str) -> None:
        output_dir = Path(job["output_dir"]).resolve()
        path = (output_dir / Path(filename).name).resolve()
        if output_dir not in path.parents and path != output_dir:
            return self._json({"error": "Invalid output path."}, HTTPStatus.BAD_REQUEST)
        return self._serve_file(path, cache_control="no-store, max-age=0")

    def _serve_job_log(self, job: dict[str, Any]) -> None:
        log_path = Path(job.get("log") or Path(job["run_dir"]) / "run.log").resolve()
        run_dir = Path(job["run_dir"]).resolve()
        if run_dir not in log_path.parents and log_path != run_dir:
            return self._json({"error": "Invalid log path."}, HTTPStatus.BAD_REQUEST)
        if not log_path.exists():
            return self._text("")

        max_bytes = 120_000
        size = log_path.stat().st_size
        with log_path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            content = handle.read().decode("utf-8", errors="replace")
        return self._text(content)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = content.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args)


def _field(form: cgi.FieldStorage, name: str, default: str = "") -> str:
    if name not in form:
        return default
    value = form[name]
    if isinstance(value, list):
        value = value[0]
    return str(value.value)


def _list_field(form: cgi.FieldStorage, name: str) -> list[str]:
    if name not in form:
        return []
    values = form[name]
    if not isinstance(values, list):
        values = [values]
    return [str(item.value) for item in values]


def _quality_field(form: cgi.FieldStorage) -> str:
    default_quality = str(CONFIG.get("generation", {}).get("default_quality", "balanced")).lower()
    quality = _field(form, "quality", default_quality).lower()
    presets = CONFIG.get("quality_presets", {})
    if quality in presets:
        return quality
    if default_quality in presets:
        return default_quality
    if presets:
        return next(iter(presets))
    return "balanced"


def _default_object_type() -> str:
    default_type = str(CONFIG.get("generation", {}).get("default_object_type", "organic")).lower()
    presets = CONFIG.get("object_type_presets", {})
    if default_type in presets:
        return default_type
    if "organic" in presets:
        return "organic"
    if presets:
        return next(iter(presets))
    return "organic"


def _object_type_value(value: str) -> str:
    object_type = str(value or _default_object_type()).lower()
    presets = CONFIG.get("object_type_presets", {})
    if object_type in presets:
        return object_type
    return _default_object_type()


def _object_type_field(form: cgi.FieldStorage) -> str:
    return _object_type_value(_field(form, "object_type", _default_object_type()))


def _object_type_query(query: dict[str, list[str]]) -> str | None:
    if "object_type" not in query:
        return None
    return _object_type_value(query.get("object_type", [""])[0])


def _default_texture_quality() -> str:
    default_quality = str(CONFIG.get("generation", {}).get("default_texture_quality", "fast")).lower()
    presets = CONFIG.get("texture_quality_presets", {})
    if default_quality in presets:
        return default_quality
    if "fast" in presets:
        return "fast"
    if presets:
        return next(iter(presets))
    return "fast"


def _texture_quality_value(value: str) -> str:
    texture_quality = str(value or _default_texture_quality()).lower()
    presets = CONFIG.get("texture_quality_presets", {})
    if texture_quality in presets:
        return texture_quality
    return _default_texture_quality()


def _texture_quality_field(form: cgi.FieldStorage) -> str:
    return _texture_quality_value(_field(form, "texture_quality", _default_texture_quality()))


def _texture_quality_query(query: dict[str, list[str]]) -> str:
    value = query.get("texture_quality", query.get("texture_speed", [""]))[0]
    return _texture_quality_value(value)


def _rebake_albedo_query(query: dict[str, list[str]]) -> float:
    value = query.get("albedo", query.get("rebake_albedo", ["1.0"]))[0]
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        parsed = 1.0
    return round(max(0.5, min(1.8, parsed)), 2)


def _has_rating_target(job: dict[str, Any], target: str) -> bool:
    outputs = job.get("outputs", [])
    if target == "white":
        return any(output.get("format") == "glb" and output.get("filename") == "white_mesh.glb" for output in outputs)
    return any(
        output.get("format") == "glb" and str(output.get("filename", "")).startswith("textured_mesh")
        for output in outputs
    )


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        value = int(query.get(name, [str(default)])[0])
    except (TypeError, ValueError):
        return default
    return max(1, min(100, value))


def _server_host(server) -> str:
    return str(getattr(server, "lgo_host", CONFIG["service"]["host"]))


def _server_port(server) -> int:
    return int(getattr(server, "lgo_port", CONFIG["service"]["port"]))


def _spawn_restart_helper(host: str, port: int) -> dict[str, Any]:
    python_path = Path(sys.executable)
    server_path = PROJECT_ROOT / "lgo_server.py"
    if not python_path.exists():
        raise FileNotFoundError(f"LGO Python runtime was not found: {python_path}")
    if not server_path.exists():
        raise FileNotFoundError(f"LGO server script was not found: {server_path}")

    helper_code = r"""
import os
import socket
import subprocess
import sys
import time

server_path = sys.argv[1]
host = sys.argv[2]
port = int(sys.argv[3])
project_root = sys.argv[4]

time.sleep(1.2)
deadline = time.time() + 30
while time.time() < deadline:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.25)
    try:
        sock.connect((host, port))
    except OSError:
        break
    finally:
        sock.close()
    time.sleep(0.35)

log_dir = os.path.join(project_root, "logs")
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "service-restart.log")
log = open(log_path, "ab", buffering=0)
kwargs = {
    "cwd": project_root,
    "stdin": subprocess.DEVNULL,
    "stdout": log,
    "stderr": log,
}
if os.name == "nt":
    kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
else:
    kwargs["start_new_session"] = True

subprocess.Popen([sys.executable, server_path, "--host", host, "--port", str(port)], **kwargs)
"""

    kwargs: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(
        [str(python_path), "-c", helper_code, str(server_path), host, str(port), str(PROJECT_ROOT)],
        **kwargs,
    )
    return {"restart_helper_pid": process.pid}


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the LGO local service.")
    parser.add_argument("--host", default=CONFIG["service"]["host"])
    parser.add_argument("--port", default=int(CONFIG["service"]["port"]), type=int)
    args = parser.parse_args()

    address = (args.host, args.port)
    httpd = ThreadingHTTPServer(address, LGOHandler)
    httpd.lgo_host = args.host
    httpd.lgo_port = args.port
    print(f"LGO service running at http://{args.host}:{args.port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
