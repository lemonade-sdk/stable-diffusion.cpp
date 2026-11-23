# Lemonade Stable diffusion fork

This repo is a "Github actions only" fork of [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp) that is built with more ROCm releases than upstream stable diffusion supports.

These are built for the purpose of [Lemonade](https://github.com/lemonade-sdk/lemonade) use, but they can be utilized by any project. Releases just need to be used with matching AMD ROCm releases — see [Using the ROCm builds](#using-the-rocm-builds).

# Cadence

The builds are triggered nightly, or can optionally be triggered on demand as well. A nightly publishes when upstream stable-diffusion.cpp or this repository changed, or to fill in a build an earlier run could not publish.

Every ROCm build is checked before it is published — statically, and by generating an image on an AMD Strix Halo (gfx1151) GPU on Windows and Linux. A ROCm version is published only when both its Windows and Linux builds pass. See [.github/qualify-rocm](.github/qualify-rocm/README.md).

# Using the ROCm builds

[Lemonade](https://github.com/lemonade-sdk/lemonade) does all of this for you.

If you instead want to use a build on its own, follow these steps:

### 1. Find your GPU's gfx target

Every AMD GPU has a gfx target, for example:

| GPU | gfx target |
|---|---|
| Ryzen AI Max (Radeon 8060S, Strix Halo) | `gfx1151` |
| Ryzen AI 300 (Radeon 890M) | `gfx1150` |
| Ryzen 7040/8040 (Radeon 780M) | `gfx1103` |
| Radeon RX 7900 XT/XTX | `gfx1100` |
| Radeon RX 9070/9070 XT | `gfx1201` |

`sd-server` also prints it when it starts, in a line like `Device 0: AMD Radeon(TM) 8060S Graphics, gfx1151 (0x1151)`.

The builds include code for gfx1010, gfx1011, gfx1012, gfx1030–gfx1036, gfx1100–gfx1103, gfx1150–gfx1153, gfx1200 and gfx1201, and on Linux also gfx908, gfx90a, gfx942 and gfx950.

### 2. Download a build

From the [latest release](https://github.com/lemonade-sdk/stable-diffusion.cpp/releases/latest), pick one ROCm version and download your OS's zip:

- Windows: `sd-master-<commit>-bin-win-rocm-<version>-x64.zip`
- Linux: `sd-master-<commit>-bin-Linux-Ubuntu-24.04-x86_64-rocm-<version>.zip` (Ubuntu 24.04 or newer)

You also need a model, for example [SD-Turbo](https://huggingface.co/Green-Sky/SD-Turbo-GGUF/resolve/main/sd_turbo-f16-q8_0.gguf) (~2 GB).

### 3. Install the matching ROCm runtime

The ROCm zips (in the [Releases](https://github.com/lemonade-sdk/stable-diffusion.cpp/releases) page of this repo) contain stable-diffusion.cpp (`sd-server`, `sd-cli` and their libraries) but not ROCm's math libraries (hipBLAS, hipBLASLt, rocBLAS). Those come from AMD's ROCm runtime, installed with `pip`, and it must be the **same ROCm version** as the zip.

Use Python 3.12 or 3.13, and replace `10.2.0` with the zip's ROCm version and `gfx1151` with your gfx target.

Windows (PowerShell):

```powershell
python -m venv rocm-10.2
.\rocm-10.2\Scripts\python.exe -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ "rocm[libraries,device-gfx1151]==10.2.0"
```

Linux:

```bash
python3 -m venv rocm-10.2
./rocm-10.2/bin/python -m pip install --index-url https://stable.repo.amd.com/rocm/whl-next/ "rocm[libraries,device-gfx1151]==10.2.0"
```

### 4. Unpack and start sd-server

Windows (PowerShell, from the folder holding `rocm-10.2`):

```powershell
Expand-Archive sd-master-<commit>-bin-win-rocm-10.2.0-x64.zip -DestinationPath sd
$env:PATH = "$PWD\rocm-10.2\Lib\site-packages\_rocm_sdk_libraries\bin;$env:PATH"
.\sd\sd-server.exe -m sd_turbo-f16-q8_0.gguf --listen-port 8080
```

> Keep the `amdhip64_7.dll`, `amd_comgr.dll` and `rocm_kpack.dll` that came in the zip next to `sd-server.exe`: they stop Windows from loading the older copy the AMD display driver installs.

Linux (from the folder holding `rocm-10.2`):

```bash
unzip sd-master-<commit>-bin-Linux-Ubuntu-24.04-x86_64-rocm-10.2.0.zip
SITE=$(./rocm-10.2/bin/python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")
export LD_LIBRARY_PATH="$SITE/_rocm_sdk_core/lib:$SITE/_rocm_sdk_libraries/lib:$LD_LIBRARY_PATH"
./build/bin/sd-server -m sd_turbo-f16-q8_0.gguf --listen-port 8080
```

### 5. Generate an image

Open <http://127.0.0.1:8080> in a browser, or call the OpenAI-style API. Generation settings such as the seed and step count go inside the prompt, in `<sd_cpp_extra_args>`; these are the ones SD-Turbo is made for.

Windows (PowerShell):

```powershell
$body = '{"prompt":"a glass of lemonade on a wooden table<sd_cpp_extra_args>{\"seed\":42,\"sample_params\":{\"sample_steps\":4,\"guidance\":{\"txt_cfg\":1.0}}}</sd_cpp_extra_args>","size":"512x512","response_format":"b64_json"}'
$r = Invoke-RestMethod -Uri http://127.0.0.1:8080/v1/images/generations -Method Post -ContentType "application/json" -Body $body
[IO.File]::WriteAllBytes("$PWD\image.png", [Convert]::FromBase64String($r.data[0].b64_json))
```

Linux:

```bash
curl -s http://127.0.0.1:8080/v1/images/generations -H "Content-Type: application/json" \
  -d '{"prompt":"a glass of lemonade on a wooden table<sd_cpp_extra_args>{\"seed\":42,\"sample_params\":{\"sample_steps\":4,\"guidance\":{\"txt_cfg\":1.0}}}</sd_cpp_extra_args>","size":"512x512","response_format":"b64_json"}' \
  | python3 -c "import base64, json, sys; open('image.png', 'wb').write(base64.b64decode(json.load(sys.stdin)['data'][0]['b64_json']))"
```

### Is it using the GPU?

A build that cannot use the GPU still works — on the CPU, several times slower, without an error.

Start `sd-server` with `-v` and check its output for `Device 0: … gfx…` and lines ending in `(VRAM) on ROCm0`.

`failed to initialize ROCm: no ROCm-capable device is detected` or `… RAM) on CPU` means it is running on the CPU.

### Troubleshooting

| Symptom | Cause |
|---|---|
| Windows: `sd-server.exe` exits at once with code `-1073741515` (`0xC0000135`) | The ROCm runtime folder is not on `PATH` (step 4) |
| Windows: exits with code `-1073741819` (`0xC0000005`) | The `amdhip64_7.dll` / `amd_comgr.dll` from the zip are missing or replaced |
| Linux: `error while loading shared libraries: libhipblas.so.3: cannot open shared object file` | `LD_LIBRARY_PATH` is not set (step 4) |
