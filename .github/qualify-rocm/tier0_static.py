#!/usr/bin/env python3
"""
Tier 0 -- static checks on a ROCm archive exactly as the build job packed it.
No GPU needed. T0.12 (final archive integrity) runs separately in the release
job, see check_final.py.

Usage:
    python3 tier0_static.py --meta meta/qualify-meta-win-rocm-10.1.json \
        --archive-dir ./artifact --run-binaries --output tier0.json
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archive  # noqa: E402
import binfmt  # noqa: E402
import report  # noqa: E402

REQUIRED_FILES = {
    "ubuntu": ["sd-cli", "sd-server", "libstable-diffusion.so", "libggml.so",
               "libggml-base.so", "libggml-hip.so", "ggml.txt", "stable-diffusion.cpp.txt"],
    "win": ["sd-cli.exe", "sd-server.exe", "stable-diffusion.dll", "ggml.txt",
            "stable-diffusion.cpp.txt", "amdhip64_7.dll", "amd_comgr.dll", "rocm_kpack.dll"],
}

# Linux loads ggml-hip as a backend library; Windows links it into stable-diffusion.dll.
HIP_CODE = {"ubuntu": "libggml-hip.so", "win": "stable-diffusion.dll"}
# Other binaries with HIP kernels of their own; audited when they carry any.
OTHER_HIP_CODE = {"ubuntu": ["libstable-diffusion.so"], "win": []}
MIN_HIP_CODE_BYTES = 64 << 20

CPU_VARIANT_GLOB = "libggml-cpu-*.so"

# Shipped next to sd-server.exe since TheRock#7691: a stale driver amdhip64 in
# System32 would otherwise be loaded instead.
BUNDLED_HIP_RUNTIME = {"ubuntu": [], "win": ["amdhip64_7.dll", "amd_comgr.dll", "rocm_kpack.dll"]}

BASE_LIBS_LINUX = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "librt.so.1", "libpthread.so.0",
    "libstdc++.so.6", "libgcc_s.so.1", "ld-linux-x86-64.so.2", "libutil.so.1",
    "libresolv.so.2", "libgomp.so.1",
}
BASE_DLLS_WIN = ("kernel32", "user32", "gdi32", "advapi32", "ws2_32", "shell32",
                 "ole32", "oleaut32", "bcrypt", "crypt32", "dbghelp", "ntdll",
                 "msvcp140", "vcruntime140", "vcruntime140_1", "msvcrt",
                 "api-ms-win-", "ucrtbase", "concrt140", "powrprof",
                 "psapi", "setupapi", "cfgmgr32", "version", "winmm")

# ROCm libraries the user installs; T0.5 lists them, T0.10 resolves them.
ROCM_LIB_PREFIXES = (
    "libamdhip64", "libhipblas", "librocblas", "libhipblaslt", "librocsolver",
    "libamd_comgr", "libhsa-runtime64", "librocm", "libroctx", "librocprofiler",
)
ROCM_DLL_PREFIXES = (
    "amdhip64", "hipblas", "libhipblas", "rocblas", "hipblaslt", "libhipblaslt",
    "rocsolver", "amd_comgr", "amdhsa", "rocm",
)

# The Linux archive is built on Ubuntu 24.04 (glibc 2.39), the oldest Linux Lemonade supports.
SUPPORTED_GLIBC = (2, 39)
SUPPORTED_DISTRO = "Ubuntu 24.04"

# What Lemonade passes to a ROCm sd-server (src/cpp/server/backends/sdcpp/sdcpp_server.cpp).
# -v is also what makes sd-server print the evidence Tier 1 reads.
LEMONADE_SERVER_FLAGS = ["--listen-port", "-m", "--model", "--diffusion-model",
                         "--llm", "--vae", "-v"]

VERSION_RE = re.compile(r"stable-diffusion\.cpp version (\S+), commit ([0-9a-f]+)")


def exe(name, os_name):
    return f"{name}.exe" if os_name == "win" else name


def listing(root):
    return sorted(os.listdir(root))


def binaries(root):
    for name in listing(root):
        path = os.path.join(root, name)
        if os.path.isfile(path) and not os.path.islink(path):
            yield name, path


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rocm_env(rocm_dirs, root, os_name):
    env = dict(os.environ)
    joined = os.pathsep.join(rocm_dirs + [root])
    key = "PATH" if os_name == "win" else "LD_LIBRARY_PATH"
    env[key] = joined + os.pathsep + env.get(key, "")
    return env


def check_contents(root, os_name):
    files = set(listing(root))
    missing = [f for f in REQUIRED_FILES[os_name] if f not in files]
    tests = sorted(f for f in files if f.startswith("test-"))
    details = {"entries": len(files), "missing": missing,
               "test_binaries_shipped": len(tests), "test_binaries": tests[:10]}
    if missing:
        return (report.STATUS_FAIL, f"missing from archive: {', '.join(missing)}", details)
    if tests:
        return (report.STATUS_WARN,
                f"{len(tests)} upstream test binary/binaries shipped to users", details)
    return (report.STATUS_PASS, None, details)


def check_hip_code(root, os_name):
    name = HIP_CODE[os_name]
    path = os.path.join(root, name)
    details = {"binary": name, "exists": os.path.exists(path)}
    if not details["exists"]:
        return (report.STATUS_FAIL, f"{name} is missing -- the HIP build failed or was "
                "dropped at packaging", details)
    details["bytes"] = os.path.getsize(path)
    if details["bytes"] < MIN_HIP_CODE_BYTES:
        return (report.STATUS_FAIL, f"{name} is only {details['bytes'] / 1e6:.1f} MB; a "
                "multi-target HIP fat binary is ~1 GB, so it holds little or no GPU code",
                details)
    return (report.STATUS_PASS, None, details)


def check_gfx_targets(root, os_name, gpu_targets):
    path = os.path.join(root, HIP_CODE[os_name])
    if not os.path.exists(path):
        return (report.STATUS_FAIL, f"{HIP_CODE[os_name]} missing (see T0.2)", {})
    expected = [t for t in re.split(r"[;,\s]+", gpu_targets or "") if t]
    if not expected:
        return (report.STATUS_FAIL, "no expected gfx target list was supplied", {})

    result = binfmt.audit_gfx_targets(path, expected)
    if result["present_count"] == 0:
        return (report.STATUS_FAIL, "found no gfx code objects at all -- the audit "
                "method is broken, not necessarily the build (see binfmt.BUNDLE_ENTRY_RE)",
                result)

    problems = []
    if result["missing"]:
        problems.append(f"{HIP_CODE[os_name]} lacks {', '.join(result['missing'])}")
    others = {}
    for name in OTHER_HIP_CODE[os_name]:
        other = os.path.join(root, name)
        if not os.path.exists(other):
            continue
        audit = binfmt.audit_gfx_targets(other, expected)
        others[name] = {k: audit[k] for k in ("present_count", "missing", "extra",
                                               "entry_count_uniform", "bundle_magic_count")}
        if audit["present_count"] and audit["missing"]:
            problems.append(f"{name} lacks {', '.join(audit['missing'])}")
    result["other_binaries"] = others

    if problems:
        return (report.STATUS_FAIL, "requested gfx target(s) missing from the fat binary: "
                f"{'; '.join(problems)}. Users with those GPUs fall back to the CPU "
                "with no error.", result)
    uneven = [n for n, a in others.items() if not a["entry_count_uniform"]]
    if not result["entry_count_uniform"] or uneven:
        return (report.STATUS_WARN, "all targets present but entry counts differ -- a "
                "target may be compiled into only some translation units", result)
    return (report.STATUS_PASS, None, result)


def check_cpu_variants(root, os_name):
    if os_name != "ubuntu":
        return (report.STATUS_SKIP, "the Windows ROCm build links the CPU backend into "
                "stable-diffusion.dll", {})
    variants = sorted(fnmatch.filter(listing(root), CPU_VARIANT_GLOB))
    details = {"count": len(variants), "variants": variants}
    if not variants:
        return (report.STATUS_FAIL, f"no CPU backend matching {CPU_VARIANT_GLOB}", details)
    if len(variants) < 5:
        return (report.STATUS_WARN, f"only {len(variants)} CPU backend variant(s); "
                "GGML_CPU_ALL_VARIANTS=ON normally yields 14+", details)
    return (report.STATUS_PASS, None, details)


def _deps(path, os_name):
    if os_name == "ubuntu":
        return binfmt.elf_dynamic(path)[0] if binfmt.is_elf(path) else None
    return binfmt.pe_imports(path) if binfmt.is_pe(path) else None


def _is_rocm(name, os_name):
    prefixes = ROCM_LIB_PREFIXES if os_name == "ubuntu" else ROCM_DLL_PREFIXES
    return name.lower().startswith(prefixes)


def check_dependencies(root, os_name):
    present = set(listing(root))
    present_lower = {f.lower() for f in present}
    rocm_needed, unresolved, scanned = set(), {}, 0
    for name, path in binaries(root):
        try:
            deps = _deps(path, os_name)
        except (ValueError, OSError, struct.error) as exc:
            unresolved[name] = [f"<unreadable: {exc}>"]
            continue
        if not deps:
            continue
        scanned += 1
        missing = []
        for dep in deps:
            if _is_rocm(dep, os_name):
                rocm_needed.add(dep)
            elif os_name == "ubuntu" and (dep in present or dep in BASE_LIBS_LINUX):
                continue
            elif os_name == "win" and (dep.lower() in present_lower
                                       or dep.lower().startswith(BASE_DLLS_WIN)):
                continue
            else:
                missing.append(dep)
        if missing:
            unresolved[name] = sorted(missing)

    details = {"binaries_scanned": scanned, "rocm_runtime_required": sorted(rocm_needed),
               "unresolved": unresolved}
    if unresolved:
        flat = sorted({d for deps in unresolved.values() for d in deps})
        return (report.STATUS_FAIL, f"{len(unresolved)} binary/binaries need libraries "
                f"that are neither in the archive nor part of the OS: {', '.join(flat[:6])}",
                details)
    if not rocm_needed:
        return (report.STATUS_WARN, "no ROCm runtime dependency found at all", details)
    return (report.STATUS_PASS, None, details)


def check_linux_version_support(root, os_name):
    if os_name != "ubuntu":
        return (report.STATUS_SKIP, "Linux-only check", {})
    needs, needs_file, per_file = (0, 0), None, {}
    for name, path in binaries(root):
        if not binfmt.is_elf(path):
            continue
        floor = binfmt.elf_glibc_floor(path)
        if not floor:
            continue
        per_file[name] = ".".join(map(str, floor))
        if floor > needs:
            needs, needs_file = floor, name

    details = {"glibc_required": ".".join(map(str, needs)) if needs_file else None,
               "glibc_allowed": ".".join(map(str, SUPPORTED_GLIBC)),
               "oldest_supported_distro": SUPPORTED_DISTRO,
               "required_by": needs_file, "per_file": per_file}
    if not needs_file:
        return (report.STATUS_WARN, "could not determine the glibc this archive needs", details)
    if needs > SUPPORTED_GLIBC:
        return (report.STATUS_FAIL, f"{needs_file} needs glibc {details['glibc_required']}, "
                f"newer than {SUPPORTED_DISTRO} provides ({details['glibc_allowed']}); "
                "it will not start there", details)
    return (report.STATUS_PASS, None, details)


def check_rpath(root, os_name):
    if os_name != "ubuntu":
        return (report.STATUS_SKIP, "Linux-only check", {})
    leaks = {}
    for name, path in binaries(root):
        if not binfmt.is_elf(path):
            continue
        bad = [part for entry in binfmt.elf_dynamic(path)[1] for part in entry.split(":")
               if part and not part.startswith("$ORIGIN")]
        if bad:
            leaks[name] = bad
    details = {"leaked": leaks}
    if leaks:
        flat = sorted({p for parts in leaks.values() for p in parts})
        return (report.STATUS_FAIL, f"{len(leaks)} binary/binaries carry a build-machine "
                f"path in RUNPATH: {flat[0]}", details)
    return (report.STATUS_PASS, None, details)


class ServerHelp:
    """Runs `sd-server --help` once for T0.8 and T0.9."""

    def __init__(self, root, os_name, run_binaries, rocm_dirs):
        self.root, self.os_name = root, os_name
        self.run_binaries, self.rocm_dirs = run_binaries, rocm_dirs
        self._result = None

    def get(self):
        if self._result is None:
            self._result = self._run()
        return self._result

    def _run(self):
        if not self.run_binaries:
            return None, "not running binaries on this runner"
        if not self.rocm_dirs:
            return None, "no ROCm runtime supplied; sd-server cannot load without one"
        server = os.path.join(self.root, exe("sd-server", self.os_name))
        if not os.path.exists(server):
            return None, "sd-server missing (see T0.1)"
        proc = subprocess.run([server, "--help"], capture_output=True, text=True,
                              errors="replace", check=False, timeout=180, cwd=self.root,
                              env=rocm_env(self.rocm_dirs, self.root, self.os_name))
        return proc, None


def check_server_flags(help_run):
    proc, why = help_run.get()
    if proc is None:
        status = report.STATUS_FAIL if "missing" in why else report.STATUS_SKIP
        return (status, why, {})
    text = (proc.stdout or "") + (proc.stderr or "")
    details = {"returncode": proc.returncode, "help_bytes": len(text)}
    if proc.returncode != 0 or not text.strip():
        details["output_tail"] = text[-600:]
        return (report.STATUS_FAIL, f"sd-server --help exited {proc.returncode} "
                f"(0x{proc.returncode & 0xFFFFFFFF:08X}) with {len(text)} bytes of output",
                details)
    missing = [f for f in LEMONADE_SERVER_FLAGS
               if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", text)]
    details.update(flags_checked=len(LEMONADE_SERVER_FLAGS), flags_missing=missing)
    if missing:
        return (report.STATUS_FAIL, f"sd-server no longer accepts {', '.join(missing)}; "
                "Lemonade's launch command will break", details)
    return (report.STATUS_PASS, None, details)


def check_provenance(help_run, upstream_sha):
    proc, why = help_run.get()
    if proc is None:
        status = report.STATUS_FAIL if "missing" in why else report.STATUS_SKIP
        return (status, why, {})
    text = (proc.stdout or "") + (proc.stderr or "")
    match = VERSION_RE.search(text)
    details = {"expected_upstream_sha": upstream_sha,
               "binary_version": match.group(1) if match else None,
               "binary_commit": match.group(2) if match else None}
    if not upstream_sha:
        return (report.STATUS_FAIL, "no pinned upstream commit was supplied", details)
    if not match:
        details["output_head"] = text[:300]
        return (report.STATUS_FAIL, "sd-server does not report its version and commit", details)
    if not upstream_sha.lower().startswith(match.group(2)):
        return (report.STATUS_FAIL, f"sd-server was built from {match.group(2)}, not the "
                f"pinned upstream commit {upstream_sha[:12]}", details)
    return (report.STATUS_PASS, None, details)


def _rocm_files(rocm_dirs):
    available = {}
    for directory in rocm_dirs:
        if os.path.isdir(directory):
            for entry in os.listdir(directory):
                available.setdefault(entry.lower(), os.path.join(directory, entry))
    return available


def check_rocm_dependencies(root, os_name, rocm_dirs):
    if not rocm_dirs:
        return (report.STATUS_SKIP, "no ROCm install supplied via --rocm-dirs", {})
    available = _rocm_files(rocm_dirs)
    bundled = {f.lower() for f in listing(root)}
    required, unresolved = set(), {}
    for name, path in binaries(root):
        try:
            deps = [d for d in (_deps(path, os_name) or []) if _is_rocm(d, os_name)]
        except (ValueError, OSError, struct.error):
            continue
        required.update(deps)
        missing = [d for d in deps if d.lower() not in available and d.lower() not in bundled]
        if missing:
            unresolved[name] = sorted(missing)

    details = {"rocm_dirs_searched": len(rocm_dirs), "files_available": len(available),
               "rocm_libraries_required": sorted(required), "unresolved": unresolved}
    if not required:
        return (report.STATUS_WARN, "the archive references no ROCm library", details)
    if unresolved:
        flat = sorted({d for deps in unresolved.values() for d in deps})
        return (report.STATUS_FAIL, f"the ROCm a user installs does not provide "
                f"{', '.join(flat)}; sd-server will not start", details)
    return (report.STATUS_PASS, None, details)


def check_bundled_runtime(root, os_name, rocm_dirs):
    bundled = sorted(set(BUNDLED_HIP_RUNTIME[os_name])
                     | {n for n, _ in binaries(root) if _is_rocm(n, os_name)})
    if not bundled:
        return (report.STATUS_SKIP, "this archive bundles no ROCm library", {})
    if not rocm_dirs:
        return (report.STATUS_SKIP, "no ROCm install supplied via --rocm-dirs", {})
    available = _rocm_files(rocm_dirs)
    files, problems = {}, []
    for name in bundled:
        path = os.path.join(root, name)
        if not os.path.exists(path):
            problems.append(f"{name} is not in the archive")
            continue
        reference = available.get(name.lower())
        if not reference:
            problems.append(f"{name} is not part of this ROCm version")
            continue
        files[name] = {"archive": sha256(path), "rocm": sha256(reference)}
        if files[name]["archive"] != files[name]["rocm"]:
            problems.append(f"{name} differs from this ROCm version's copy")
    details = {"files": files, "problems": problems}
    if problems:
        return (report.STATUS_FAIL, f"bundled HIP runtime does not match the ROCm version "
                f"the archive is for: {'; '.join(problems)}", details)
    return (report.STATUS_PASS, None, details)


def main():
    parser = argparse.ArgumentParser(description="Tier 0 static archive verification")
    parser.add_argument("--meta", help="qualify-meta-*.json written by the build job")
    parser.add_argument("--archive-dir", default=".")
    parser.add_argument("--archive")
    parser.add_argument("--os", dest="os_name", choices=["ubuntu", "win"])
    parser.add_argument("--rocm-version")
    parser.add_argument("--gpu-targets")
    parser.add_argument("--tag")
    parser.add_argument("--upstream-sha")
    parser.add_argument("--run-binaries", action="store_true",
                        help="the runner's OS matches the archive (enables T0.8, T0.9)")
    parser.add_argument("--rocm-dirs", default=os.environ.get("ROCM_RUNTIME_DIRS", ""),
                        help="user-style ROCm runtime dirs, os.pathsep-separated")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--output", default="tier0-report.json")
    parser.add_argument("--fail-on-error", action="store_true")
    args = parser.parse_args()

    if args.meta:
        with open(args.meta, encoding="utf-8-sig") as handle:
            info = json.load(handle)
        args.os_name = args.os_name or info["os"]
        args.rocm_version = args.rocm_version or info["rocm_version"]
        args.gpu_targets = args.gpu_targets or info["gpu_targets"]
        args.tag = args.tag or info.get("tag")
        args.upstream_sha = args.upstream_sha or info.get("upstream_sha")
        args.archive = args.archive or os.path.join(args.archive_dir, info["artifact"])
    for required in ("archive", "os_name", "rocm_version", "gpu_targets"):
        if not getattr(args, required):
            parser.error(f"--{required.replace('_name', '').replace('_', '-')} is required "
                         "when --meta is not given")
    if not os.path.exists(args.archive):
        raise SystemExit(f"::error::archive not found: {args.archive}")

    rocm_dirs = [d for d in args.rocm_dirs.split(os.pathsep) if d]
    tier = report.TierReport("tier0", report.build_meta(
        artifact=os.path.basename(args.archive), os_name=args.os_name,
        rocm_version=args.rocm_version, sdcpp_tag=args.tag, upstream_sha=args.upstream_sha,
        run_id=args.run_id, run_attempt=args.run_attempt))

    workdir = tempfile.mkdtemp(prefix="tier0-", dir=os.environ.get("RUNNER_TEMP") or None)
    try:
        root = archive.extract(args.archive, workdir)
        print(f"Extracted {args.archive} -> {root} ({len(listing(root))} entries)")
        help_run = ServerHelp(root, args.os_name, args.run_binaries, rocm_dirs)

        tier.run("T0.1", "archive contents", lambda: check_contents(root, args.os_name))
        tier.run("T0.2", "HIP code present", lambda: check_hip_code(root, args.os_name))
        tier.run("T0.3", "gfx target audit",
                 lambda: check_gfx_targets(root, args.os_name, args.gpu_targets))
        tier.run("T0.4", "CPU backend variants", lambda: check_cpu_variants(root, args.os_name))
        tier.run("T0.5", "runtime dependency survey",
                 lambda: check_dependencies(root, args.os_name))
        tier.run("T0.6", "Linux version support",
                 lambda: check_linux_version_support(root, args.os_name))
        tier.run("T0.7", "library search path", lambda: check_rpath(root, args.os_name))
        tier.run("T0.8", "sd-server flag surface", lambda: check_server_flags(help_run))
        tier.run("T0.9", "upstream provenance",
                 lambda: check_provenance(help_run, args.upstream_sha))
        tier.run("T0.10", "ROCm dependency resolution",
                 lambda: check_rocm_dependencies(root, args.os_name, rocm_dirs))
        tier.run("T0.11", "bundled HIP runtime matches ROCm",
                 lambda: check_bundled_runtime(root, args.os_name, rocm_dirs))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    tier.write(args.output)
    tier.print_summary()
    print(f"\nWrote {args.output}")
    if args.fail_on_error and tier.rollup_status() == report.STATUS_FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
