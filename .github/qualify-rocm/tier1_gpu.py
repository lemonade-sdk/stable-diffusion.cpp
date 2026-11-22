#!/usr/bin/env python3
"""
Tier 1 -- GPU image generation on a real AMD GPU (gfx1151 runners).

Drives sd-server over HTTP the way Lemonade does. A ROCm build that cannot use
the GPU still starts, still answers and still returns a valid image -- it just
runs on the CPU -- so T1.2 checks the log for proof the GPU did the work.

Usage:
    python3 tier1_gpu.py --meta meta/qualify-meta-win-rocm-10.1.json \
        --archive-dir ./artifact --gfx-target gfx1151 --output tier1.json
"""

import argparse
import atexit
import base64
import hashlib
import json
import os
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

# The model Lemonade validates sd.cpp with (SD-Turbo-GGUF), pinned to one revision.
MODEL_REPO = "Green-Sky/SD-Turbo-GGUF"
MODEL_REVISION = "19a31586d02d64a73b4419bc193b3ecfaf38e1f0"
MODEL_FILE = "sd_turbo-f16-q8_0.gguf"
MODEL_BYTES = 2023745376
MODEL_SHA256 = "d50be7655f0a554cf8041c145d88b210bd5f3c545423119dee62ae08cae51580"
MODEL_URL = f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_FILE}"

WIDTH = HEIGHT = 512
PROMPT = "a red apple on a wooden table"
# sd-server's OpenAI route ignores top-level seed/steps; they travel in the prompt.
GEN_ARGS = {"seed": 42, "sample_params": {"sample_steps": 4, "guidance": {"txt_cfg": 1.0}}}

# -v is required: the backend and compute-buffer lines are verbose-only.
SERVER_ARGS = ["-v"]

# Evidence, all from one sd-server log:
#   "  Device 0: AMD Radeon(TM) 8060S Graphics, gfx1151 (0x1151), VMM: no, ..."
#   "total params memory size = 1929.50MB (VRAM 1929.50MB, RAM 0.00MB): text_encoders 358.79MB(VRAM), ..."
#   "unet compute buffer size: 87.86 MB(VRAM) on ROCm0 (peak across 1 segment)"
DEVICE_RE = re.compile(r"Device (\d+): (.+?), (gfx[0-9a-fA-F]+) \(0x[0-9a-fA-F]+\)")
PARAMS_RE = re.compile(r"total params memory size = ([0-9.]+)MB \(VRAM ([0-9.]+)MB, "
                       r"RAM ([0-9.]+)MB\):([^\n]*)")
COMPONENT_RE = re.compile(r"(\w+) ([0-9.]+)MB\((VRAM|RAM|N/A)\)")
COMPUTE_RE = re.compile(r"(\w+) compute buffer size: ([0-9.]+) MB\(VRAM\) on (ROCm\d+)")
AUTO_FIT_RE = re.compile(r'auto-fit: --backend "([^"]+)"')

# Lines a CPU fallback prints instead; used to explain a failure.
CPU_FALLBACK_HINTS = [
    re.compile(r"failed to initialize ROCm", re.I),
    re.compile(r"no ROCm-capable device", re.I),
    re.compile(r"auto-fit: no GPU devices"),
    re.compile(r"device \S+ failed to init"),
    re.compile(r"loading CPU backend"),
    re.compile(r"Using CPU backend"),
]

# Inherited ROCm settings that would change which runtime or device gets used.
FOREIGN_ROCM_ENV = ("ROCM_PATH", "HIP_PATH", "HIP_DEVICE_LIB_PATH", "HSA_OVERRIDE_GFX_VERSION",
                    "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES",
                    "ROCBLAS_TENSILE_LIBPATH", "HIPBLASLT_TENSILE_LIBPATH")


def ensure_model(cache_dir):
    """Return a verified copy of the test model, downloading it at most once per machine."""
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
        except (urllib.error.URLError, OSError) as exc:
            last = exc
            if os.path.exists(part):
                os.remove(part)
            time.sleep(10 * attempt)
    raise SystemExit(f"::error::could not download the test model: {last}")


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read()
    except (urllib.error.URLError, OSError):
        return None, b""


def generate(base):
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
    """Return (width, height, channels, raw pixel bytes) for an 8-bit RGB/RGBA/gray PNG."""
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
    width, height, depth, color, _comp, _filt, interlace = header
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


class Server:
    def __init__(self, exe, model, port, log_path, env, cwd):
        self.exe, self.model, self.port = exe, model, port
        self.log_path, self.env, self.cwd = log_path, env, cwd
        self.proc = None
        self._log = None

    def start(self):
        cmd = [self.exe, "-m", self.model, "--listen-port", str(self.port), *SERVER_ARGS]
        print("+ " + " ".join(cmd), flush=True)
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        kwargs = {"start_new_session": True} if os.name != "nt" else {}
        self.proc = subprocess.Popen(cmd, stdout=self._log, stderr=subprocess.STDOUT,
                                     env=self.env, cwd=self.cwd, **kwargs)

    def wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False, (f"sd-server exited early (rc={self.proc.returncode}, "
                               f"0x{self.proc.returncode & 0xFFFFFFFF:08X})")
            status, _ = http_get(f"http://127.0.0.1:{self.port}/")
            if status == 200 and self.proc.poll() is None:
                return True, None
            time.sleep(2)
        return False, f"sd-server not ready within {timeout}s"

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                if os.name == "nt":
                    self.proc.terminate()
                else:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                self.proc.wait(timeout=30)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
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


def gpu_evidence(log):
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
        return (report.STATUS_PASS, None, details)
    details["cpu_fallback_hints"] = [p.pattern for p in CPU_FALLBACK_HINTS if p.search(log)]
    details["log_tail"] = log[-3000:]
    return (report.STATUS_FAIL, f"GPU was NOT used -- {'; '.join(problems)}. sd-server "
            "still returns an image this way, just from the CPU.", details)


def check_gpu_target(server, gfx_target):
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


def build_env(rocm_dirs, root):
    env = {k: v for k, v in os.environ.items() if k not in FOREIGN_ROCM_ENV}
    key = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    env[key] = os.pathsep.join(rocm_dirs + [root, env.get(key, "")])
    return env


def main():
    parser = argparse.ArgumentParser(description="Tier 1 GPU image generation")
    parser.add_argument("--meta", help="qualify-meta-*.json written by the build job")
    parser.add_argument("--archive-dir", default=".")
    parser.add_argument("--archive")
    parser.add_argument("--archive-root", help="an already-extracted archive")
    parser.add_argument("--model", help="path to an already-verified model file")
    parser.add_argument("--model-cache", default=os.environ.get("RUNNER_TOOL_CACHE") or tempfile.gettempdir(),
                        help="persistent folder the model is cached in")
    parser.add_argument("--rocm-version")
    parser.add_argument("--gfx-target", default="gfx1151")
    parser.add_argument("--artifact")
    parser.add_argument("--tag")
    parser.add_argument("--upstream-sha")
    parser.add_argument("--rocm-dirs", default=os.environ.get("ROCM_RUNTIME_DIRS", ""))
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
        raise SystemExit("::error::No ROCm runtime directories; run install_rocm.py first.")

    workdir = tempfile.mkdtemp(prefix="tier1-", dir=os.environ.get("RUNNER_TEMP") or None)
    atexit.register(lambda: shutil.rmtree(workdir, ignore_errors=True))

    if args.archive_root:
        root = os.path.abspath(args.archive_root)
    elif args.archive:
        root = archive.extract(args.archive, os.path.join(workdir, "x"))
        print(f"Extracted {args.archive} -> {root}")
    else:
        parser.error("one of --meta, --archive or --archive-root is required")
    model = args.model or ensure_model(args.model_cache)

    exe = os.path.join(root, archive.SERVER["win" if os.name == "nt" else "ubuntu"])
    if not os.path.exists(exe):
        raise SystemExit(f"::error::sd-server not found at {exe}")

    meta = report.build_meta(
        artifact=args.artifact or (os.path.basename(args.archive) if args.archive else None),
        os_name="win" if os.name == "nt" else "ubuntu", rocm_version=args.rocm_version,
        sdcpp_tag=args.tag, upstream_sha=args.upstream_sha, gfx_target=args.gfx_target,
        run_id=args.run_id, run_attempt=args.run_attempt)
    tier = report.TierReport("tier1", meta)

    os.makedirs(args.logs_dir, exist_ok=True)
    log_path = os.path.join(args.logs_dir, f"sd-server-{args.gfx_target}-rocm{args.rocm_version}.log")
    base = f"http://127.0.0.1:{args.port}"
    server = Server(exe, os.path.abspath(model), args.port, log_path, build_env(rocm_dirs, root), root)
    state = {}
    try:
        # Another sd-server on this port would answer for the one under test.
        if http_get(f"{base}/", timeout=3)[0] is not None:
            raise SystemExit(f"::error::port {args.port} is already in use; refusing to test another server")
        server.start()
        ready, err = server.wait_ready(args.ready_timeout)
        if ready:
            tier.add("T1.1", "server boot", report.STATUS_PASS,
                     details={"port": args.port, "args": SERVER_ARGS, "model": MODEL_FILE})
            try:
                state["png"], state["seconds"] = generate(base)
            except Exception as exc:  # noqa: BLE001
                state["error"] = f"image request failed: {type(exc).__name__}: {exc}"
            if server.proc.poll() is not None:
                state = {"error": f"sd-server under test exited (rc={server.proc.returncode}); "
                                  "the response came from another process"}
            tier.run("T1.2", "GPU really did the work", lambda: check_gpu_did_work(server))
            tier.run("T1.3", "GPU target detected", lambda: check_gpu_target(server, args.gfx_target))
            tier.run("T1.4", "image well-formed", lambda: check_image(state))
            tier.run("T1.5", "deterministic output", lambda: check_determinism(base, state),
                     gating=False)
        else:
            tier.add("T1.1", "server boot", report.STATUS_FAIL, error=err,
                     details={"log_tail": server.log_text()[-3000:]})
            for test_id, name, gating in [("T1.2", "GPU really did the work", True),
                                          ("T1.3", "GPU target detected", True),
                                          ("T1.4", "image well-formed", True),
                                          ("T1.5", "deterministic output", False)]:
                tier.add(test_id, name, report.STATUS_SKIP, gating=gating, error="server not ready")
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
