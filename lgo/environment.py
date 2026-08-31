from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _run(command: list[str], timeout: int = 10, env: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
            env=env,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "not found", "command": command}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "command": command}

    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "command": command,
    }


def _path_info(path: str, kind: str = "any") -> dict[str, Any]:
    value = Path(path)
    exists = value.exists()
    correct_kind = exists
    if exists and kind == "file":
        correct_kind = value.is_file()
    if exists and kind == "dir":
        correct_kind = value.is_dir()

    return {
        "path": str(value),
        "exists": exists,
        "ok": bool(exists and correct_kind),
        "kind": kind,
    }


def _folder_size(path: str) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return total


def _model_folder(path: str, required_files: list[str] | None = None) -> dict[str, Any]:
    info = _path_info(path, "dir")
    missing: list[str] = []
    for relative in required_files or []:
        if not Path(path, relative).exists():
            missing.append(relative)
    info["missing"] = missing
    info["size_bytes"] = _folder_size(path)
    info["ok"] = info["ok"] and not missing
    return info


def _runtime_env(config: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    cache_root = Path(config["paths"]["cache_dir"])
    hf_home = cache_root / "huggingface"
    source_dir = Path(config["paths"]["hunyuan_source_dir"])
    python_paths = [
        str(source_dir),
        str(source_dir / "hy3dshape"),
        str(source_dir / "hy3dpaint"),
        str(source_dir / "hy3dpaint" / "custom_rasterizer"),
        env.get("PYTHONPATH", ""),
    ]
    env.update(
        {
            "HF_HOME": str(hf_home),
            "HF_HUB_CACHE": str(hf_home / "hub"),
            "TRANSFORMERS_CACHE": str(hf_home / "transformers"),
            "XDG_CACHE_HOME": str(cache_root),
            "HY3DGEN_MODELS": config["paths"]["models_root"],
            "PYTHONPATH": os.pathsep.join(path for path in python_paths if path),
            "PYTHONUTF8": "1",
        }
    )
    return env


def _import_check(python_path: str, module: str, env: dict[str, str]) -> dict[str, Any]:
    if not Path(python_path).exists():
        return {"ok": False, "version": None}
    checked = _run(
        [
            python_path,
            "-c",
            (
                "import importlib, sys\n"
                "try:\n"
                f"    {'import torch' if module.startswith('custom_rasterizer') else 'pass'}\n"
                f"    m=importlib.import_module('{module}')\n"
                "    print(getattr(m, '__version__', 'installed'))\n"
                "except Exception as exc:\n"
                "    print(type(exc).__name__ + ': ' + str(exc))\n"
                "    sys.exit(1)\n"
            ),
        ]
        ,
        timeout=60,
        env=env,
    )
    return {
        "ok": checked["ok"],
        "version": checked.get("stdout") or checked.get("stderr"),
    }


def check_environment(config: dict[str, Any]) -> dict[str, Any]:
    paths = config["paths"]
    models = config["models"]

    python_path = paths["python"]
    venv_python_path = paths["venv_python"]
    blender_path = paths["blender"]
    runtime_env = _runtime_env(config)

    python_result = _run([python_path, "--version"], env=runtime_env)
    venv_python_result = (
        _run([venv_python_path, "--version"], env=runtime_env) if Path(venv_python_path).exists() else {"ok": False}
    )
    torch_result = (
        _run(
            [
                venv_python_path,
                "-c",
                "import torch; print(torch.__version__ + ' cuda=' + str(torch.cuda.is_available()))",
            ]
            ,
            env=runtime_env,
        )
        if Path(venv_python_path).exists()
        else {"ok": False}
    )
    blender_result = _run([blender_path, "--version"], timeout=20, env=runtime_env)
    gpu_result = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader",
        ]
    )

    result = {
        "python": {
            **_path_info(python_path, "file"),
            "version": python_result.get("stdout") or python_result.get("stderr"),
        },
        "venv": {
            **_path_info(venv_python_path, "file"),
            "version": venv_python_result.get("stdout") or venv_python_result.get("stderr"),
            "torch": {
                "ok": torch_result["ok"],
                "version": torch_result.get("stdout") or torch_result.get("stderr"),
            },
            "packages": {
                "transformers": _import_check(venv_python_path, "transformers", runtime_env),
                "diffusers": _import_check(venv_python_path, "diffusers", runtime_env),
                "gradio": _import_check(venv_python_path, "gradio", runtime_env),
                "trimesh": _import_check(venv_python_path, "trimesh", runtime_env),
                "rembg": _import_check(venv_python_path, "rembg", runtime_env),
            },
            "texture_native": {
                "custom_rasterizer": _import_check(venv_python_path, "custom_rasterizer", runtime_env),
                "custom_rasterizer_kernel": _import_check(
                    venv_python_path,
                    "custom_rasterizer_kernel",
                    runtime_env,
                ),
            },
        },
        "blender": {
            **_path_info(blender_path, "file"),
            "version": (blender_result.get("stdout") or "").splitlines()[:1],
        },
        "gpu": {
            "ok": gpu_result["ok"],
            "raw": gpu_result.get("stdout") or gpu_result.get("stderr"),
        },
        "models": {
            "single_shape": _model_folder(
                models["single_shape"],
                ["config.yaml", "model.fp16.ckpt"],
            ),
            "multiview_shape": _model_folder(
                models["multiview_shape"],
                ["config.yaml", "model.fp16.safetensors"],
            ),
            "paint_model": _model_folder(
                models["paint_model"],
                [
                    "model_index.json",
                    "unet/diffusion_pytorch_model.bin",
                    "text_encoder/pytorch_model.bin",
                    "image_encoder/model.safetensors",
                    "vae/diffusion_pytorch_model.bin",
                ],
            ),
            "vae": _model_folder(models["vae"], ["config.yaml", "model.fp16.ckpt"]),
            "hy3dpaint": _model_folder(models["hy3dpaint"], ["textureGenPipeline.py"]),
            "realesrgan": _path_info(models["realesrgan"], "file"),
        },
        "runtime": {
            "hunyuan_source": _path_info(paths["hunyuan_source_dir"], "dir"),
        },
    }

    required_sections = [
        result["python"]["ok"],
        result["blender"]["ok"],
        result["models"]["single_shape"]["ok"],
        result["models"]["multiview_shape"]["ok"],
        result["models"]["paint_model"]["ok"],
        result["models"]["vae"]["ok"],
        result["models"]["hy3dpaint"]["ok"],
        result["models"]["realesrgan"]["ok"],
    ]
    result["ready_for_service"] = all(required_sections)
    result["ready_for_generation"] = (
        result["ready_for_service"]
        and result["runtime"]["hunyuan_source"]["ok"]
        and result["venv"]["ok"]
        and result["venv"]["torch"]["ok"]
        and all(package["ok"] for package in result["venv"]["packages"].values())
    )
    result["ready_for_pbr_texture"] = (
        result["ready_for_generation"]
        and result["venv"]["texture_native"]["custom_rasterizer"]["ok"]
        and result["venv"]["texture_native"]["custom_rasterizer_kernel"]["ok"]
    )
    return result


def main() -> None:
    from .settings import load_config

    print(json.dumps(check_environment(load_config()), indent=2))


if __name__ == "__main__":
    main()
