# LGO - Local Generation Object

LGO is a local Windows web service for generating 3D objects from one reference image or four views with Hunyuan3D-2.1.

The app wraps a local Hunyuan3D runtime and provides:

- single-image and four-view input modes;
- geometry quality presets: Fast, Balanced, High;
- object type presets: Organic and Hard surface;
- optional PBR texture generation with independent texture speed presets;
- post-processing for background cleanup, floor-plate removal, smoothing, weighted normals, finger/toe cleanup, and format conversion;
- GLB/OBJ/FBX export;
- generation history with loadable results;
- separate 1-5 star ratings for white mesh and textured mesh results.

## What is not included

This repository intentionally does not include generated jobs, model weights, virtual environments, CUDA wheels, or the Hunyuan3D source checkout.

Ignored local folders include:

- `.venv/`
- `.cache/`
- `vendor/`
- `wheelhouse/`
- `runs/`
- `logs/`

## Requirements

- Windows 10/11
- Python 3.10
- NVIDIA GPU with CUDA support
- CUDA Toolkit if you need to build the native texture rasterizer
- Blender installed locally
- Hunyuan3D-2.1 model weights downloaded from Hugging Face

Default local paths are configured in `config/lgo_config.json`.

The repo uses `{project_root}` inside the config for paths that should follow the cloned folder.

## Setup

Clone the project:

```powershell
git clone https://github.com/NikitaZ23/LGO.git
cd LGO
```

Create the Python virtual environment:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1
```

Install the Hunyuan3D source checkout:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_hunyuan_source.ps1
```

Install the AI runtime:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_ai_runtime.ps1
```

If your Python is not discoverable as `py -3.10`, pass it explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_venv.ps1 -Python C:\Path\To\Python310\python.exe
```

## Models

Download the Hunyuan3D-2.1 model folders and update `config/lgo_config.json` if your model root is different.

Expected default paths:

```text
E:\AI\Models\Hunyuan3D-DiT-v2-1
E:\AI\Models\Hunyuan3D-DiT-v2-mv
E:\AI\Models\Hunyuan3D-Paint-v2-1\hunyuan3d-paintpbr-v2-1
E:\AI\Models\Hunyuan3D-Paint-v2-1\hunyuan3d-vae-v2-1
E:\AI\Models\Hunyuan3D-Paint-v2-1\hy3dpaint
E:\AI\Models\Hunyuan3D-Paint-v2-1\hy3dpaint\ckpt\RealESRGAN_x4plus.pth
```

## Run

Foreground:

```powershell
.\start-lgo.bat
```

Background:

```powershell
.\start-lgo-background.bat
```

Stop:

```powershell
.\stop-lgo.bat
```

Open:

```text
http://127.0.0.1:7865
```

## Check Environment

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\check_environment.ps1
```

## Notes

Texture generation is much slower than geometry generation. Use Fast texture first, then re-run texture on a good white mesh with Balanced or High only when the shape is worth it.

For GitHub, keep large generated files and model weights out of commits.
