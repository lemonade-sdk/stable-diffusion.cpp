# ROCm build qualification

Every ROCm archive this repository publishes (Windows and Linux, for each ROCm
version) is checked before it can ship. The checks run on pull requests, manual
runs and nightly runs; only nightly and `create_release` runs publish.

| Tier | Runs on | Blocks the release? |
|---|---|---|
| Tier 0, static checks | hosted `ubuntu-24.04` / `windows-2025` | yes, on any gating FAIL or if it never reported |
| Tier 1, GPU inference | self-hosted Strix Halo (gfx1151), Windows and Linux | yes, on any gating FAIL or if it never reported |

A ROCm version ships only when both of its archives qualify. Versions are judged
independently, and other backends (CPU, Vulkan, CUDA, Metal) are never affected.
WARN never blocks.

## Tier 0 (`tier0_static.py`, `check_final.py`)

| ID | Check |
|---|---|
| T0.1 | archive contents: sd-cli, sd-server, the sd/ggml libraries, licence files, and on Windows the bundled HIP runtime; WARN if upstream test binaries ship |
| T0.2 | HIP code present: `libggml-hip.so` (Linux) / `stable-diffusion.dll` (Windows) exists and is large enough to hold GPU code |
| T0.3 | gfx target audit: every target in the build's `gpu_targets` is in the fat binary (also `libstable-diffusion.so`'s own kernels on Linux) |
| T0.4 | CPU backend variants (Linux; Windows links the CPU backend in) |
| T0.5 | runtime dependency survey: every import resolves to the archive, the OS, or the ROCm runtime |
| T0.6 | Linux version support: needs no newer glibc than Ubuntu 24.04 |
| T0.7 | library search path: no build-machine path in RUNPATH |
| T0.8 | sd-server flag surface: the flags Lemonade launches a ROCm sd-server with still exist |
| T0.9 | upstream provenance: sd-server reports the upstream commit this run pinned |
| T0.10 | ROCm dependency resolution: the ROCm a user installs provides every ROCm library the archive imports |
| T0.11 | bundled HIP runtime matches ROCm: the bundled `amdhip64_7.dll`, `amd_comgr.dll` and `rocm_kpack.dll` are byte-identical to that ROCm version's copies |
| T0.12 | final archive integrity (release job): the uploaded archive reads back intact and its SHA-256 equals the one the build job recorded, so what ships is what was tested |

## Tier 1 (`tier1_gpu.py`)

Starts `sd-server -m sd_turbo-f16-q8_0.gguf --listen-port 8233 -v` from the
archive with a user-style ROCm install (`install_rocm.py`), then generates a
512x512 image (seed 42, 4 steps) through `/v1/images/generations`.

| ID | Check | Gating |
|---|---|---|
| T1.1 | server boot | yes |
| T1.2 | GPU really did the work: a ROCm device was found, every model component's weights are in VRAM, and compute buffers were allocated in VRAM on a ROCm device | yes |
| T1.3 | GPU target detected: the device sd-server found is the runner's gfx target | yes |
| T1.4 | image well-formed: a 512x512 PNG that decodes and is not blank | yes |
| T1.5 | deterministic output: the same request again gives identical bytes | no |

T1.2 matters because a ROCm build that cannot use the GPU still starts and
still returns a valid image, from the CPU. The test model (SD-Turbo-GGUF, the
model Lemonade validates sd.cpp with) is pinned to one revision and SHA-256
and cached per runner in `RUNNER_TOOL_CACHE`.

## Results

Each archive's merged record is attached to the release as
`qualification-<archive>.json`, and every run's records are published to the
`qualification-data` branch for the ROCm readiness dashboard (see `FEED.md`).
