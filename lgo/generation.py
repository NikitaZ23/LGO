from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from lgo.settings import PROJECT_ROOT


class GenerationService:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    def prepare(self, job: dict[str, Any]) -> dict[str, Any]:
        run_dir = Path(job["run_dir"])
        manifest = {
            "job_id": job["id"],
            "mode": job["payload"]["mode"],
            "quality": job["payload"].get("quality", self.config.get("generation", {}).get("default_quality", "balanced")),
            "object_type": job["payload"].get(
                "object_type",
                self.config.get("generation", {}).get("default_object_type", "character"),
            ),
            "scale_preset": job["payload"].get(
                "scale_preset",
                self.config.get("generation", {}).get("default_scale_preset", "character"),
            ),
            "target_height_m": job["payload"].get(
                "target_height_m",
                self.config.get("generation", {}).get("default_target_height_m", 1.8),
            ),
            "texture_quality": job["payload"].get(
                "texture_quality",
                self.config.get("generation", {}).get("default_texture_quality", "fast"),
            ),
            "rebake_albedo": job["payload"].get("rebake_albedo"),
            "texture": job["payload"]["texture"],
            "formats": job["payload"]["formats"],
            "input_files": job["payload"]["input_files"],
            "models": self._models_for(job["payload"]["mode"], job["payload"]["texture"]),
            "paths": {
                "blender": self.config["paths"]["blender"],
                "hunyuan_source_dir": self.config["paths"]["hunyuan_source_dir"],
            },
            "low_vram": self.config["generation"].get("low_vram", True),
        }

        with (run_dir / "manifest.json").open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)

        return manifest

    def start(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._start_runner(job)

    def start_texture(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._start_runner(job, ["--texture-only"])

    def start_texture_rebake(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._start_runner(job, ["--rebake-texture"])

    def _start_runner(self, job: dict[str, Any], extra_args: list[str] | None = None) -> dict[str, Any]:
        run_dir = Path(job["run_dir"])
        log_path = run_dir / "run.log"
        python_path = Path(self.config["paths"].get("venv_python") or self.config["paths"]["python"])
        runner_path = PROJECT_ROOT / "scripts" / "run_hunyuan_job.py"
        config_path = PROJECT_ROOT / "config" / "lgo_config.json"

        if not python_path.exists():
            raise FileNotFoundError(f"LGO Python runtime was not found: {python_path}")
        if not runner_path.exists():
            raise FileNotFoundError(f"LGO generation runner was not found: {runner_path}")

        env = self._runtime_env()
        command = [
            str(python_path),
            str(runner_path),
            "--job",
            str(run_dir / "job.json"),
            "--config",
            str(config_path),
        ]
        if extra_args:
            command.extend(extra_args)

        popen_kwargs: dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                **popen_kwargs,
            )

        return {
            "process_id": process.pid,
            "log": str(log_path),
        }

    def _models_for(self, mode: str, texture: bool) -> dict[str, str]:
        models = self.config["models"]
        selected = {
            "shape": models["single_shape"] if mode == "single" else models["multiview_shape"],
        }
        if texture:
            selected.update(
                {
                    "paint_model": models["paint_model"],
                    "vae": models["vae"],
                    "hy3dpaint": models["hy3dpaint"],
                    "realesrgan": models["realesrgan"],
                }
            )
        return selected

    def can_run_real_generation(self) -> bool:
        return (
            Path(self.config["paths"]["hunyuan_source_dir"]).exists()
            and Path(self.config["paths"].get("venv_python", self.config["paths"]["python"])).exists()
        )

    def _runtime_env(self) -> dict[str, str]:
        env = os.environ.copy()
        cache_dir = Path(self.config["paths"].get("cache_dir", PROJECT_ROOT / ".cache"))
        hf_home = cache_dir / "huggingface"
        source_dir = Path(self.config["paths"]["hunyuan_source_dir"])
        python_paths = [
            str(source_dir),
            str(source_dir / "hy3dshape"),
            str(source_dir / "hy3dpaint"),
            env.get("PYTHONPATH", ""),
        ]

        env.update(
            {
                "HF_HOME": str(hf_home),
                "HF_HUB_CACHE": str(hf_home / "hub"),
                "TRANSFORMERS_CACHE": str(hf_home / "transformers"),
                "XDG_CACHE_HOME": str(cache_dir),
                "HY3DGEN_MODELS": self.config["paths"]["models_root"],
                "PYTHONPATH": os.pathsep.join(path for path in python_paths if path),
                "PYTHONUTF8": "1",
            }
        )
        return env

    def stop_process_tree(self, process_id: Any) -> dict[str, Any]:
        try:
            pid = int(process_id)
        except (TypeError, ValueError):
            return {"pid": process_id, "ok": False, "reason": "missing process id"}
        if pid <= 0 or pid == os.getpid():
            return {"pid": pid, "ok": False, "reason": "invalid process id"}

        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            output = (result.stdout or result.stderr or "").strip()
            return {
                "pid": pid,
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "output": output[-1000:],
            }

        try:
            os.killpg(pid, signal.SIGTERM)
            return {"pid": pid, "ok": True, "signal": "SIGTERM"}
        except ProcessLookupError:
            return {"pid": pid, "ok": True, "reason": "process not found"}
        except Exception as exc:  # noqa: BLE001 - shutdown should report any local process issue.
            return {"pid": pid, "ok": False, "reason": str(exc)}
