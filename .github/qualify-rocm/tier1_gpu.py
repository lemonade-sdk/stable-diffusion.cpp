#!/usr/bin/env python3
"""
Tier 1 -- GPU functional inference. Requires a real AMD GPU (gfx1151 runners).

Drives sd-server over HTTP rather than sd-cli, because sd-server is the binary
Lemonade actually launches, with the flags it uses (SERVER_ARGS below) and the
model it validates stable-diffusion.cpp with.

T1.2 is the check that carries the weight here. A ROCm build that cannot use
the GPU is not treated as an error by stable-diffusion.cpp: it logs that no
ROCm device was found and carries on with the CPU. sd-server still starts,
still answers, and still returns a perfectly valid image -- it just takes ten
times longer. So a test that only asks "did we get an image?" passes happily on
a build whose GPU support is entirely broken. T1.2 confirms the GPU really did
the work.

Checks:
  T1.1  server boot              -- sd-server starts and serves its web page.
  T1.2  GPU really did the work  -- GATING. A ROCm device was found, every
                                    model component's weights were placed in
                                    VRAM, and compute buffers were allocated in
                                    VRAM on a ROCm device.
  T1.3  GPU target detected      -- GATING. The device sd-server found is the
                                    gfx target this runner is meant to validate.
                                    This is what makes `hardware_validated` true.
  T1.4  image well-formed        -- GATING. A PNG of the requested size that
                                    decodes and is not blank.
  T1.5  deterministic output     -- NON-gating. The same request again gives
                                    identical bytes. See check_determinism().
  T1.6  ROCm loaded from the expected places -- GATING. The running server
                                    loaded the bundled HIP runtime from the
                                    archive and every other ROCm library from
                                    the user's ROCm install -- nothing from a
                                    stale display-driver copy in System32.

Usage:
    python3 tier1_gpu.py --meta qualify-meta-win-rocm-10.1.json \
        --archive-dir ./artifact --gfx-target gfx1151 --output tier1.json
"""

import argparse
import atexit
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archive  # noqa: E402
import report  # noqa: E402

READY_TIMEOUT = 600
REQUEST_TIMEOUT = 600

# ---------------------------------------------------------------------------
# What is run
# ---------------------------------------------------------------------------

# The model Lemonade validates stable-diffusion.cpp with ("SD-Turbo-GGUF" in
# Lemonade's server_models.json): about 2 GB, one file, no licence gate. Pinned
# to one revision and checked against its SHA-256 and size, because a download
# that arrives truncated would otherwise fail T1.1 and withhold a good build.
MODEL_REPO = "Green-Sky/SD-Turbo-GGUF"
MODEL_REVISION = "19a31586d02d64a73b4419bc193b3ecfaf38e1f0"
MODEL_FILE = "sd_turbo-f16-q8_0.gguf"
MODEL_BYTES = 2023745376
MODEL_SHA256 = "d50be7655f0a554cf8041c145d88b210bd5f3c545423119dee62ae08cae51580"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"

# One small, quick image: SD-Turbo is made for 4 steps at 512x512 with
# classifier-free guidance off (cfg 1.0), as Lemonade runs it.
WIDTH = HEIGHT = 512
PROMPT = "a red apple on a wooden table"
# sd-server's OpenAI route ignores top-level seed/steps; they travel inside the
# prompt as <sd_cpp_extra_args>, which is also how Lemonade passes them.
GEN_ARGS = {"seed": 42, "sample_params": {"sample_steps": 4, "guidance": {"txt_cfg": 1.0}}}

# The flags Lemonade launches sd-server with besides the model and port.
# `-v`is not optional: at the default log level sd-server never prints its compute
# buffers, so T1.2 would find no evidence and fail a good build.
SERVER_ARGS = ["-v"]

# ---------------------------------------------------------------------------
# Evidence that the GPU genuinely did the work
#
# All three are required, and each names a GPU or VRAM, so none can appear
# unless a ROCm device was found and used:
#   "  Device 0: AMD Radeon(TM) 8060S Graphics, gfx1151 (0x1151), VMM: no, ..."
#       printed by ggml when it finds ROCm devices (any log level);
#   "total params memory size = 1929.50MB (VRAM 1929.50MB, RAM 0.00MB):
#    text_encoders 358.79MB(VRAM), diffusion_model 1411.02MB(VRAM), vae ..."
#       where each model component's weights were placed (any log level);
#   "unet compute buffer size: 87.86 MB(VRAM) on ROCm0 (peak across 1 segment)"
#       printed when a graph is about to run (verbose only).
#
# Deliberately NOT accepted as evidence, despite how it reads:
#   "Initializing backend: ROCm0"
# stable-diffusion.cpp prints it BEFORE trying to start the device, so it also appears
# when the device then fails to initialise and everything falls back to the CPU.
# ---------------------------------------------------------------------------
DEVICE_RE = re.compile(r"Device (\d+): (.+?), (gfx[0-9a-fA-F]+) \(0x[0-9a-fA-F]+\)")
PARAMS_RE = re.compile(r"total params memory size = ([0-9.]+)MB \(VRAM ([0-9.]+)MB, "
                       r"RAM ([0-9.]+)MB\):([^\n]*)")
COMPONENT_RE = re.compile(r"(\w+) ([0-9.]+)MB\((VRAM|RAM|N/A)\)")
COMPUTE_RE = re.compile(r"(\w+) compute buffer size: ([0-9.]+) MB\(VRAM\) on (ROCm\d+)")
AUTO_FIT_RE = re.compile(r'auto-fit: --backend "([^"]+)"')

# Lines that explain WHY the GPU went unused, so the failure message can say
# something more useful than "no evidence found". Each was seen in a real CPU
# fallback of sd-server with the GPU hidden.
CPU_FALLBACK_HINTS = [
    re.compile(r"failed to initialize ROCm", re.I),
    re.compile(r"no ROCm-capable device", re.I),
    re.compile(r"auto-fit: no GPU devices"),
    re.compile(r"device \S+ failed to init"),
    re.compile(r"loading CPU backend"),
    re.compile(r"Using CPU backend"),
]

# Settings a self-hosted runner may carry that would change which ROCm runtime
# or which device gets used. Removed before launching, and recorded in the
# report's runner_env if they were set.
FOREIGN_ROCM_ENV = ("ROCM_PATH", "HIP_PATH", "HIP_DEVICE_LIB_PATH", "HSA_OVERRIDE_GFX_VERSION",
                    "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
                    "ROCBLAS_TENSILE_LIBPATH", "HIPBLASLT_TENSILE_LIBPATH")

# Library names that belong to ROCm, for T1.6. Same lists as
# lemonade-sdk/llamacpp-rocm, minus ggml-hip, which is this archive's own code.
ROCM_LIB_PREFIXES = (
    "libamdhip64", "libhsa-runtime64", "libamd_comgr", "librocblas", "libhipblas",
    "librocsolver", "librocm", "libroctx", "librocprofiler", "libtensilelite",
    "liborigami", "librocroller", "libhiprtc",
)
ROCM_DLL_PREFIXES = (
    "amdhip64", "amd_comgr", "rocblas", "hipblas", "libhipblas", "rocsolver",
    "rocm_", "origami", "libtensilelite", "hiprtc",
)
HIP_RUNTIME_PREFIX = ("libamdhip64", "amdhip64")


# ---------------------------------------------------------------------------
# Inputs this script fetches for itself
#
# Extraction and downloading live here so the job works on any runner regardless
# of which shell it has. A self-hosted machine is not guaranteed to provide the
# same tools a hosted one does.
# ---------------------------------------------------------------------------

def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_model(cache_dir):
    """Return a verified copy of the test model, downloading it at most once per machine.

    The cache lives under the runner's tool cache, which survives between jobs
    on a self-hosted machine, so each machine downloads the 2 GB once. A cached
    copy is re-hashed every time and replaced if it does not match. A download
    goes to a per-process temporary name and is only moved into place once its
    size and SHA-256 are right, so two jobs on one machine cannot see each
    other's half-written file.
    """
    folder = os.path.join(cache_dir, "sdcpp-qualify", MODEL_REVISION[:12])
    path = os.path.join(folder, MODEL_FILE)
    os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        if os.path.getsize(path) == MODEL_BYTES and file_sha256(path) == MODEL_SHA256:
            print(f"Using cached model {path}")
            return path
        print(f"::warning::cached model {path} is corrupt; downloading it again")
        os.remove(path)

    last = None
    for attempt in range(1, 4):
        part = f"{path}.{os.getpid()}.part"
        try:
            print(f"Downloading model (attempt {attempt}/3): {MODEL_URL}")
            digest = hashlib.sha256()
            with urllib.request.urlopen(MODEL_URL, timeout=600) as resp, open(part, "wb") as out:
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    digest.update(chunk)
                    out.write(chunk)
            size = os.path.getsize(part)
            if size != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
                raise OSError(f"downloaded {size} bytes with sha256 {digest.hexdigest()}, "
                              f"expected {MODEL_BYTES} bytes with sha256 {MODEL_SHA256}")
            os.replace(part, path)
            return path
        except Exception as exc:  # noqa: BLE001 - any failure is retried
            last = exc
            if os.path.exists(part):
                os.remove(part)
            time.sleep(10 * attempt)
    raise SystemExit(f"::error::could not download the test model: {last}")


# ---------------------------------------------------------------------------
# The machine this ran on
# ---------------------------------------------------------------------------

def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _windows_gpu_drivers():
    """AMD display adapters and their driver versions, from the registry."""
    import winreg  # pylint: disable=import-outside-toplevel

    found = []
    base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return found
    with root:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not sub.isdigit():
                continue
            entry = {}
            try:
                with winreg.OpenKey(root, sub) as key:
                    for value in ("DriverDesc", "ProviderName", "DriverVersion",
                                  "DriverDate", "RadeonSoftwareVersion"):
                        try:
                            entry[value] = str(winreg.QueryValueEx(key, value)[0])
                        except OSError:
                            pass
            except OSError:
                continue
            text = " ".join(entry.values())
            if "Advanced Micro Devices" in text or "AMD" in text or "Radeon" in text:
                found.append(entry)
    return found


def runner_environment():
    """What a GPU result depends on besides the archive: driver, kernel, stray ROCm.

    Recorded in every report so a failure that only happens on one machine, or
    after a driver update, can be traced without access to that machine.
    """
    env = {
        "platform": platform.platform(),
        "machine_name": platform.node(),
        "python": platform.python_version(),
        "foreign_rocm_env_removed": {k: os.environ[k] for k in FOREIGN_ROCM_ENV
                                     if k in os.environ},
    }
    if os.name == "nt":
        env["gpu_drivers"] = _windows_gpu_drivers()
        system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        env["driver_hip_runtime_in_system32"] = sorted(
            f for f in os.listdir(system32) if f.lower().startswith("amdhip64"))
    else:
        env["kernel"] = platform.release()
        env["amdgpu_module_version"] = _read("/sys/module/amdgpu/version")
        env["kfd_present"] = os.path.exists("/dev/kfd")
        env["system_rocm"] = {
            "opt_rocm_present": os.path.isdir("/opt/rocm"),
            "opt_rocm_version": _read("/opt/rocm/.info/version"),
        }
    return env


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except (urllib.error.URLError, OSError):
        return None, b""


def generate(base):
    """Ask sd-server for one image the way Lemonade does; return (PNG bytes, seconds)."""
    prompt = f"{PROMPT}<sd_cpp_extra_args>{json.dumps(GEN_ARGS)}</sd_cpp_extra_args>"
    payload = {"prompt": prompt, "n": 1, "size": f"{WIDTH}x{HEIGHT}",
               "response_format": "b64_json"}
    req = urllib.request.Request(f"{base}/v1/images/generations",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    start = time.monotonic()
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed = time.monotonic() - start
    return base64.b64decode(body["data"][0]["b64_json"]), elapsed


def decode_png(data):
    """Return (width, height, channels, raw pixel bytes) for an 8-bit RGB/RGBA/gray PNG.

    A small decoder rather than an image library, because the self-hosted
    runners have nothing installed beyond Python itself. sd-server writes plain
    8-bit, non-interlaced PNGs; anything else is reported as unsupported.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, header = 8, [], None
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat.append(chunk)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if header is None:
        raise ValueError("PNG has no IHDR")
    width, height, depth, color, _compression, _filter_method, interlace = header
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color)
    if depth != 8 or channels is None or interlace:
        raise ValueError(f"unsupported PNG: depth {depth}, color type {color}, interlace {interlace}")
    raw = zlib.decompress(b"".join(idat))
    stride = width * channels
    out = bytearray(height * stride)
    prev = bytearray(stride)
    for y in range(height):
        kind = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if kind == 1:
                line[x] = (line[x] + a) & 0xFF
            elif kind == 2:
                line[x] = (line[x] + b) & 0xFF
            elif kind == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 0xFF
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
            elif kind != 0:
                raise ValueError(f"bad PNG filter type {kind}")
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, channels, bytes(out)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

def build_env(rocm_dirs, root):
    """The environment Lemonade starts sd-server with, minus any foreign ROCm.

    The user-style ROCm goes first on the library search path, then the archive
    folder. On Windows the program's own folder is searched before PATH anyway,
    so the bundled HIP runtime wins over both PATH and System32 -- which is why
    it is bundled -- and hipBLAS/rocBLAS come from the ROCm install.
    """
    env = {k: v for k, v in os.environ.items() if k not in FOREIGN_ROCM_ENV}
    key = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    env[key] = os.pathsep.join(rocm_dirs + [root, env.get(key, "")])
    return env


class Server:
    def __init__(self, exe, model, port, log_path, env, cwd):
        self.exe = exe
        self.model = model
        self.port = port
        self.log_path = log_path
        self.env = env
        self.cwd = cwd
        self.proc = None
        self._log = None

    def start(self):
        cmd = [self.exe, "-m", self.model, "--listen-port", str(self.port), *SERVER_ARGS]
        print("+ " + " ".join(cmd), flush=True)
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT,
                                     env=self.env, cwd=self.cwd, **kwargs)

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def exit_text(self):
        if self.proc is None or self.proc.returncode is None:
            return "sd-server is not running"
        code = self.proc.returncode
        return f"sd-server exited (rc={code}, 0x{code & 0xFFFFFFFF:08X})"

    def wait_ready(self, timeout):
        """Poll sd-server's web page until it answers -- and only from our own process.

        A 200 only counts while the process this script started is still alive,
        so another server on the same port can never stand in for it.
        """
        if self.proc is None:
            return False, "sd-server was never started"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running():
                return False, f"{self.exit_text()} before it was ready"
            status, _ = http_get(f"http://127.0.0.1:{self.port}/")
            if status == 200 and self.running():
                return True, None
            time.sleep(2)
        return False, f"sd-server not ready within {timeout}s"

    def stop(self):
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.terminate()
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=30)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        if self._log:
            self._log.close()

    def log_text(self):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""


# ---------------------------------------------------------------------------
# Which libraries the running server actually loaded
# ---------------------------------------------------------------------------

def _linux_loaded_files(pid):
    paths = []
    with open(f"/proc/{pid}/maps", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split(None, 5)
            if len(parts) == 6 and parts[5].startswith("/"):
                path = parts[5].strip()
                if path.endswith(" (deleted)"):
                    path = path[: -len(" (deleted)")]
                if path not in paths:
                    paths.append(path)
    return paths


def _windows_loaded_files(pid):
    import ctypes  # pylint: disable=import-outside-toplevel
    from ctypes import wintypes  # pylint: disable=import-outside-toplevel

    process_query_information, process_vm_read, list_modules_all = 0x0400, 0x0010, 0x03
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.EnumProcessModulesEx.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE), wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    psapi.GetModuleFileNameExW.argtypes = [
        wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]

    handle = kernel32.OpenProcess(process_query_information | process_vm_read, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        count = 1024
        needed = wintypes.DWORD()
        while True:
            modules = (wintypes.HMODULE * count)()
            if not psapi.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules),
                                              ctypes.byref(needed), list_modules_all):
                raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
            if needed.value <= ctypes.sizeof(modules):
                break
            count = needed.value // ctypes.sizeof(wintypes.HMODULE) + 16
        buffer = ctypes.create_unicode_buffer(32768)
        paths = []
        for i in range(needed.value // ctypes.sizeof(wintypes.HMODULE)):
            if psapi.GetModuleFileNameExW(handle, modules[i], buffer, len(buffer)):
                paths.append(buffer.value)
        return paths
    finally:
        kernel32.CloseHandle(handle)


def _is_rocm(name):
    low = name.lower()
    if os.name == "nt":
        return low.startswith(ROCM_DLL_PREFIXES) and low.endswith(".dll")
    return name.startswith(ROCM_LIB_PREFIXES) and ".so" in name


def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def gpu_evidence(log):
    """Pull the device, weight-placement and compute-buffer lines out of the log."""
    devices = [{"index": int(m.group(1)), "name": m.group(2), "gfx": m.group(3)}
               for m in DEVICE_RE.finditer(log)]
    params = PARAMS_RE.search(log)
    components = {}
    if params:
        for name, size, where in COMPONENT_RE.findall(params.group(4)):
            components[name] = {"mb": float(size), "memory": where}
    compute = [{"graph": g, "mb": float(mb), "device": dev} for g, mb, dev in COMPUTE_RE.findall(log)]
    auto_fit = AUTO_FIT_RE.search(log)
    return {
        "devices": devices,
        "params_vram_mb": float(params.group(2)) if params else None,
        "params_ram_mb": float(params.group(3)) if params else None,
        "components": components,
        "compute_buffers": compute[:10],
        "auto_fit_backend": auto_fit.group(1) if auto_fit else None,
    }


def check_gpu_did_work(server):
    """T1.2: confirm the GPU actually did the work, not merely that it was found.

    This is the check the tier exists for. Every other check here passes
    happily when stable-diffusion.cpp has fallen back to the CPU.

    All of these must hold: a ROCm device was found; every model component
    with weights (text encoders, diffusion model, VAE) had them placed in VRAM;
    and at least one compute buffer was allocated in VRAM on a ROCm device,
    which only happens once a graph really runs there. The device patterns
    match any device index (ROCm0, ROCm1, ...), so a runner with two GPUs passes
    as long as all the work landed on GPUs.
    """
    log = server.log_text()
    details = gpu_evidence(log)
    problems = []
    if not details["devices"]:
        problems.append("no ROCm device was detected")
    if not details["params_vram_mb"]:
        problems.append("no model weights were placed in VRAM")
    in_ram = [n for n, c in details["components"].items() if c["mb"] > 0 and c["memory"] != "VRAM"]
    if in_ram:
        problems.append(f"{', '.join(in_ram)} weights were placed in RAM, not VRAM")
    if not any(c["mb"] > 0 for c in details["compute_buffers"]):
        problems.append("no compute buffer was allocated in VRAM on a ROCm device")
    if not problems:
        print("GPU confirmed by:")
        for device in details["devices"]:
            print(f"    Device {device['index']}: {device['name']}, {device['gfx']}")
        print(f"    weights: VRAM {details['params_vram_mb']} MB, RAM {details['params_ram_mb']} MB")
        for buffer in details["compute_buffers"][:3]:
            print(f"    {buffer['graph']} compute buffer: {buffer['mb']} MB in VRAM on {buffer['device']}")
        return (report.STATUS_PASS, None, details)

    details["cpu_fallback_hints"] = [p.pattern for p in CPU_FALLBACK_HINTS if p.search(log)]
    details["log_tail"] = log[-3000:]
    # The compute-buffer lines are verbose-only: when everything else points at
    # the GPU, the likelier cause is a log level too low to print them.
    if details["devices"] and details["params_vram_mb"] and not in_ram and "[V]" not in log:
        return (report.STATUS_FAIL, "the weights were placed in VRAM but no compute buffer was "
                "logged, and the log has no verbose ([V]) lines at all: sd-server was not "
                f"started with {SERVER_ARGS}, so the evidence was never printed", details)
    return (report.STATUS_FAIL, f"GPU was NOT used -- {'; '.join(problems)}. This is silent "
            "at run time: sd-server still returns an image, just from the CPU.", details)


def check_gpu_target(server, gfx_target):
    """T1.3: the device sd-server found is the gfx target this runner validates.

    `gfx_target` comes from the workflow's Tier 1 leg, so adding a runner with
    another GPU is one more matrix leg, not a change here. Only when this passes
    is the archive recorded as hardware-validated on that target.
    """
    devices = gpu_evidence(server.log_text())["devices"]
    detected = sorted({d["gfx"].lower() for d in devices})
    details = {"expected": gfx_target, "detected": detected,
               "device_names": sorted({d["name"] for d in devices})}
    if not detected:
        return (report.STATUS_FAIL, "sd-server reported no ROCm device", details)
    if gfx_target.lower() not in detected:
        return (report.STATUS_FAIL, f"expected a {gfx_target} device, sd-server found "
                f"{', '.join(detected)}", details)
    return (report.STATUS_PASS, None, details)


def check_image(state):
    """T1.4: the image is a real picture of the requested size.

    A PNG of exactly WIDTHxHEIGHT that decodes completely, and is not flat: a
    GPU kernel that produces NaNs typically yields an all-black or uniform image
    that is still a perfectly valid PNG. Lemonade's validator only reads the PNG
    header, so this also catches what it would not.
    """
    if "error" in state:
        return (report.STATUS_FAIL, state["error"], {})
    data = state["png"]
    width, height, channels, pixels = decode_png(data)
    sample = pixels[::7]
    mean = sum(sample) / len(sample)
    stddev = (sum((v - mean) ** 2 for v in sample) / len(sample)) ** 0.5
    details = {"bytes": len(data), "width": width, "height": height, "channels": channels,
               "pixel_mean": round(mean, 2), "pixel_stddev": round(stddev, 2),
               "distinct_values": len(set(sample)), "request_seconds": round(state["seconds"], 2),
               "sha256": hashlib.sha256(data).hexdigest()}
    if (width, height) != (WIDTH, HEIGHT):
        return (report.STATUS_FAIL, f"image is {width}x{height}, requested {WIDTH}x{HEIGHT}", details)
    if stddev < 5 or details["distinct_values"] < 16:
        return (report.STATUS_FAIL, "image is blank or flat -- the GPU produced no real "
                "picture (often NaNs)", details)
    return (report.STATUS_PASS, None, details)


def check_determinism(base, state):
    """T1.5: the same request with the same seed gives identical bytes.

    Non-gating, and worth being honest about why. On a GPU thousands of threads
    add numbers in whatever order they happen to finish, so results can differ
    in the last decimal place between runs, which can flip a pixel. On the
    gfx1151 runners sd-server is in fact bit-exact from run to run, so a
    difference here is a hint worth a look, not proof of a defect. It is not
    compared across ROCm versions, whose images legitimately differ.
    """
    if "png" not in state:
        return (report.STATUS_SKIP, "the first image was not generated", {})
    second, seconds = generate(base)
    first_hash = hashlib.sha256(state["png"]).hexdigest()
    second_hash = hashlib.sha256(second).hexdigest()
    details = {"first_sha256": first_hash, "second_sha256": second_hash,
               "second_request_seconds": round(seconds, 2)}
    if first_hash != second_hash:
        return (report.STATUS_WARN, "the same request with the same seed produced a "
                "different image", details)
    return (report.STATUS_PASS, None, details)


def check_loaded_from_expected(server, root, rocm_dirs):
    """T1.6: every ROCm library inside the running server came from where it should.

    The HIP runtime the archive bundles (amdhip64_7.dll, amd_comgr.dll,
    rocm_kpack.dll on Windows) must come from the archive folder, and every
    other ROCm library (hipBLAS, hipBLASLt, rocBLAS, ...) from the user's ROCm
    install. Anything else -- a display driver's amdhip64_7.dll in System32, a
    system ROCm under /opt/rocm -- means the binary is running against a ROCm
    it was not built for. That is the mix behind Lemonade #3718, where
    hipGetDeviceCount() returned 0 and every request ran on the CPU.

    Taken after the requests have run, so libraries that are only loaded on
    first use -- the code-object compiler, the maths libraries -- are included.
    """
    proc = server.proc
    if proc is None or proc.poll() is not None:
        return (report.STATUS_FAIL, "sd-server is not running, so its loaded "
                "libraries cannot be inspected", {})
    paths = _windows_loaded_files(proc.pid) if os.name == "nt" else _linux_loaded_files(proc.pid)

    bundled = {f.lower() for f in os.listdir(root) if _is_rocm(f)}
    rocm = [p for p in paths if _is_rocm(os.path.basename(p))]
    misplaced = []
    for path in rocm:
        name, folder = os.path.basename(path).lower(), os.path.dirname(path)
        expected = [root] if name in bundled else rocm_dirs
        if not any(_same_dir(folder, place) for place in expected):
            misplaced.append(path)
    hip_runtime = [p for p in rocm if os.path.basename(p).lower().startswith(HIP_RUNTIME_PREFIX)]
    details = {
        "archive_dir": root,
        "bundled_in_archive": sorted(bundled),
        "modules_loaded": len(paths),
        "rocm_libraries_loaded": sorted(rocm),
        "loaded_from_unexpected_place": misplaced,
    }
    if not hip_runtime:
        return (report.STATUS_FAIL, "the HIP runtime is not loaded in the server at all, so "
                "it cannot be using the GPU", details)
    if misplaced:
        return (report.STATUS_FAIL, f"{len(misplaced)} ROCm library/libraries were loaded "
                f"from neither the archive nor the user's ROCm install, e.g. {misplaced[0]}",
                details)
    return (report.STATUS_PASS, None, details)


CHECKS_AFTER_BOOT = [
    ("T1.2", "GPU really did the work", True),
    ("T1.3", "GPU target detected", True),
    ("T1.4", "image well-formed", True),
    ("T1.5", "deterministic output", False),
    ("T1.6", "ROCm loaded from the expected places", True),
]


def main():
    parser = argparse.ArgumentParser(description="Tier 1 GPU functional inference")
    parser.add_argument("--meta", default=None,
                        help="qualify-meta-*.json written by the build job. Supplies "
                             "--rocm-version/--tag and names the archive to find "
                             "under --archive-dir, so the command line needs no "
                             "other arguments in CI.")
    parser.add_argument("--archive-dir", default=".",
                        help="where to look for the archive named by --meta")
    parser.add_argument("--archive", default=None,
                        help="published archive to test; extracted automatically")
    parser.add_argument("--archive-root", default=None,
                        help="an already-extracted archive, instead of --archive")
    parser.add_argument("--model", default=None,
                        help="path to an already-verified model file; downloaded if absent")
    parser.add_argument("--model-cache",
                        default=os.environ.get("RUNNER_TOOL_CACHE") or tempfile.gettempdir(),
                        help="persistent folder the model is cached in")
    parser.add_argument("--rocm-version", default=None)
    parser.add_argument("--gfx-target", default="gfx1151")
    parser.add_argument("--artifact", default=None, help="release asset filename")
    parser.add_argument("--tag", default=None)
    parser.add_argument("--upstream-sha", default=None)
    parser.add_argument("--rocm-dirs", default=os.environ.get("ROCM_RUNTIME_DIRS", ""),
                        help="os.pathsep-separated ROCm runtime directories")
    parser.add_argument("--port", type=int, default=8233)
    parser.add_argument("--logs-dir", default=".")
    parser.add_argument("--ready-timeout", type=int, default=READY_TIMEOUT)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--output", default="tier1-report.json")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    if args.meta:
        with open(args.meta, encoding="utf-8-sig") as handle:
            info = json.load(handle)
        args.rocm_version = args.rocm_version or info.get("rocm_version")
        args.tag = args.tag or info.get("tag")
        args.upstream_sha = args.upstream_sha or info.get("upstream_sha")
        args.artifact = args.artifact or info.get("artifact")
        if not args.archive and not args.archive_root:
            args.archive = os.path.join(args.archive_dir, info["artifact"])
    if not args.rocm_version:
        parser.error("--rocm-version is required when --meta is not given")

    rocm_dirs = [d for d in args.rocm_dirs.split(os.pathsep) if d]
    if not rocm_dirs:
        raise SystemExit(
            "::error::No ROCm runtime directories supplied. These archives do not "
            "contain ROCm's math libraries, so install_rocm.py must run first -- it "
            "exports ROCM_RUNTIME_DIRS, which this script reads automatically.")

    # Placed inside the runner's own temporary area, which the runner cleans and
    # the teardown step searches. Removed however this run ends.
    workdir = tempfile.mkdtemp(prefix="tier1-", dir=os.environ.get("RUNNER_TEMP") or None)
    atexit.register(lambda: shutil.rmtree(workdir, ignore_errors=True))

    if args.archive_root:
        root = os.path.abspath(args.archive_root)
    elif args.archive:
        if not os.path.exists(args.archive):
            raise SystemExit(f"::error::archive not found: {args.archive}")
        root = archive.extract(args.archive, os.path.join(workdir, "x"))
        print(f"Extracted {args.archive} -> {root}")
    else:
        parser.error("one of --meta, --archive or --archive-root is required")

    exe = os.path.join(root, archive.SERVER["win" if os.name == "nt" else "ubuntu"])
    if not os.path.exists(exe):
        raise SystemExit(f"::error::sd-server not found at {exe}")
    model = args.model or ensure_model(args.model_cache)

    meta = report.build_meta(
        artifact=args.artifact or (os.path.basename(args.archive) if args.archive else None),
        os_name="win" if os.name == "nt" else "ubuntu", rocm_version=args.rocm_version,
        sdcpp_tag=args.tag, upstream_sha=args.upstream_sha, gfx_target=args.gfx_target,
        run_id=args.run_id, run_attempt=args.run_attempt)
    meta["runner_env"] = runner_environment()
    tier = report.TierReport("tier1", meta)

    os.makedirs(args.logs_dir, exist_ok=True)
    log_path = os.path.join(args.logs_dir, f"sd-server-{args.gfx_target}-rocm{args.rocm_version}.log")
    base = f"http://127.0.0.1:{args.port}"
    server = Server(exe, os.path.abspath(model), args.port, log_path,
                    build_env(rocm_dirs, root), root)
    state = {}

    def skip_rest(reason):
        for test_id, name, gating in CHECKS_AFTER_BOOT:
            tier.add(test_id, name, report.STATUS_SKIP, gating=gating, error=reason)

    try:
        # Another server already on this port would answer in place of the one under test.
        if http_get(f"{base}/", timeout=3)[0] is not None:
            tier.add("T1.1", "server boot", report.STATUS_FAIL,
                     error=f"port {args.port} is already in use by another process",
                     details={"port": args.port})
            skip_rest("server not started")
        else:
            server.start()
            ready, err = server.wait_ready(args.ready_timeout)
            if not ready:
                tier.add("T1.1", "server boot", report.STATUS_FAIL, error=err,
                         details={"log_tail": server.log_text()[-3000:]})
                skip_rest("server not ready")
            else:
                tier.add("T1.1", "server boot", report.STATUS_PASS,
                         details={"port": args.port, "args": SERVER_ARGS, "model": MODEL_FILE})
                try:
                    state["png"], state["seconds"] = generate(base)
                except Exception as exc:  # noqa: BLE001 - recorded by T1.4
                    state["error"] = f"image request failed: {type(exc).__name__}: {exc}"
                if not server.running():
                    state = {"error": f"{server.exit_text()} while generating the image"}
                tier.run("T1.2", "GPU really did the work", lambda: check_gpu_did_work(server))
                tier.run("T1.3", "GPU target detected",
                         lambda: check_gpu_target(server, args.gfx_target))
                tier.run("T1.4", "image well-formed", lambda: check_image(state))
                tier.run("T1.5", "deterministic output",
                         lambda: check_determinism(base, state), gating=False)
                tier.run("T1.6", "ROCm loaded from the expected places",
                         lambda: check_loaded_from_expected(server, root, rocm_dirs))
    finally:
        server.stop()

    meta["hardware_validated"] = tier.status_of("T1.3") == report.STATUS_PASS
    tier.write(args.output)
    tier.print_summary()
    print(f"\nWrote {args.output}")
    if args.fail_on_error and tier.rollup_status() == report.STATUS_FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
