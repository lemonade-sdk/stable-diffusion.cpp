#!/usr/bin/env python3
"""
Tier 0 -- static verification of a published ROCm archive. No GPU required.

Runs on a free hosted runner against the ARCHIVE WE ARE ABOUT TO PUBLISH, not
against the build tree. That distinction matters because every bug this tier
exists to catch -- a dropped gfx target, a missing library, a leaked build
path, a missing licence -- is invisible in the build tree and only appears in
the packaged bytes.

The two operating systems are packaged differently, and the checks follow suit:
  - Linux:   a zip of build/bin/ (versioned .so names stored as symlinks).
             ggml loads its HIP backend, libggml-hip.so, as a plug-in.
  - Windows: a flat zip. ggml's HIP backend is linked into
             stable-diffusion.dll, and the HIP runtime itself (amdhip64_7.dll,
             amd_comgr.dll, rocm_kpack.dll) ships next to sd-server.exe.
Neither archive contains ROCm's math libraries (hipBLAS, hipBLASLt, rocBLAS);
the user installs those, and Lemonade does it automatically.

Checks:
  T0.1  archive contents           -- every file a user needs is present:
                                      sd-cli, sd-server, the stable-diffusion
                                      and ggml libraries, the licences, and on
                                      Windows the bundled HIP runtime.
  T0.2  HIP code present           -- the file holding the GPU code exists and
                                      is large enough to contain it.
  T0.3  gfx target audit           -- that file really contains code for every
                                      gfx target the build asked for.
                                      See binfmt.py for how and why.
  T0.4  CPU backend variants       -- Linux only: ggml-cpu-* present. Windows
                                      links the CPU backend in.
  T0.5  runtime dependency survey  -- every non-ROCm dependency resolves from
                                      the archive or the base system; the ROCm
                                      ones are recorded as expected-external.
  T0.6  Linux version support      -- Linux only. The archive must not require
                                      a newer C library than the oldest
                                      distribution we support.
  T0.7  library search path        -- Linux only. No absolute build-machine
                                      folder in the binaries' library search
                                      path (RUNPATH/RPATH); only $ORIGIN.
  T0.8  sd-server flag surface     -- the flags Lemonade launches sd-server
                                      with, and the one Tier 1 relies on, are
                                      all still accepted (needs --run-binaries).
  T0.9  upstream provenance        -- sd-server reports the upstream commit this
                                      run pinned (needs --run-binaries).
  T0.10 ROCm dependency resolution -- the ROCm libraries this archive needs
                                      exist in the ROCm a user will install
                                      (needs --rocm-dirs).
  T0.11 bundled HIP runtime        -- the HIP runtime shipped in the archive is
                                      byte-identical to that ROCm version's own
                                      copy (needs --rocm-dirs).

T0.12 (final archive integrity) is emitted separately by check_final.py, which
runs in the release job on the exact files about to be uploaded.

Test ids are this repository's own: they do not share meanings with lemonade-sdk/llama.cpp's,
and an id is never reused for a different check.

Usage:
    python3 tier0_static.py --meta meta/qualify-meta-win-rocm-10.1.json \
        --archive-dir ./artifact --run-binaries --output tier0-win-rocm-10.1.json

    python3 tier0_static.py --archive sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip \
        --os win --rocm-version 10.1.0 --gpu-targets "gfx1010;...;gfx1201" \
        --upstream-sha <sha> --run-binaries --rocm-dirs "<dirs>" --output tier0.json
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

# Files a consumer needs, by exact name at the top of the archive's binary folder.
# The unversioned Linux .so names are symlinks in the zip; they still
# count, because that is the name the binaries ask the loader for.
REQUIRED_FILES = {
    "ubuntu": ["sd-cli", "sd-server", "libstable-diffusion.so", "libggml.so",
               "libggml-base.so", "libggml-hip.so", "ggml.txt", "stable-diffusion.cpp.txt"],
    "win": ["sd-cli.exe", "sd-server.exe", "stable-diffusion.dll", "ggml.txt",
            "stable-diffusion.cpp.txt", "amdhip64_7.dll", "amd_comgr.dll", "rocm_kpack.dll"],
}

# The file that holds the GPU code. Linux builds ggml with GGML_BACKEND_DL, so
# its HIP backend is a separate plug-in; the Windows ROCm build does not, so the
# HIP backend is linked into stable-diffusion.dll.
HIP_CODE = {"ubuntu": "libggml-hip.so", "win": "stable-diffusion.dll"}

# Other binaries that carry HIP kernels of their own. libstable-diffusion.so
# compiles some of stable-diffusion.cpp's own GPU code, so it is audited too --
# but only when it carries any, so an upstream change that moves that code
# elsewhere does not read as a broken audit.
OTHER_HIP_CODE = {"ubuntu": ["libstable-diffusion.so"], "win": []}

# A multi-target HIP fat binary is around a gigabyte. Anything below this holds
# little or no GPU code.
MIN_HIP_CODE_BYTES = 64 << 20

CPU_VARIANT_GLOB = "libggml-cpu-*.so"

# The HIP runtime shipped next to sd-server.exe since TheRock#7691: Windows
# looks for DLLs in the program's folder first, so this keeps a stale
# amdhip64_7.dll from the display driver in System32 from being picked instead.
# Linux ships none.
BUNDLED_HIP_RUNTIME = {"ubuntu": [], "win": ["amdhip64_7.dll", "amd_comgr.dll", "rocm_kpack.dll"]}

# Libraries that come with any Linux installation, so the archive does not and
# should not carry them.
BASE_LIBS_LINUX = {
    "libc.so.6", "libm.so.6", "libdl.so.2", "librt.so.1", "libpthread.so.0",
    "libstdc++.so.6", "libgcc_s.so.1", "ld-linux-x86-64.so.2", "libutil.so.1",
    "libresolv.so.2", "libgomp.so.1",
}
# Windows DLLs supplied by the OS or the MSVC redistributable. gdi32 and ntdll
# are needed by the bundled HIP runtime, which llama.cpp's archives do not ship.
BASE_DLLS_WIN = ("kernel32", "user32", "gdi32", "advapi32", "ws2_32", "shell32",
                 "ole32", "oleaut32", "bcrypt", "crypt32", "dbghelp", "ntdll",
                 "msvcp140", "vcruntime140", "vcruntime140_1", "msvcrt",
                 "api-ms-win-", "ucrtbase", "concrt140", "powrprof",
                 "psapi", "setupapi", "cfgmgr32", "version", "winmm")

# ROCm libraries. Apart from the bundled HIP runtime these are deliberately NOT
# in the archive: the user installs ROCm, and stable-diffusion.cpp loads it from
# there at run time. T0.5 records which ones an archive needs; T0.10 confirms
# they exist in the ROCm that will actually be installed. hipBLASLt is named
# libhipblaslt.dll on Windows, hence both spellings.
ROCM_LIB_PREFIXES = (
    "libamdhip64", "libhipblas", "librocblas", "libhipblaslt", "librocsolver",
    "libamd_comgr", "libhsa-runtime64", "librocm", "libroctx", "librocprofiler",
)
ROCM_DLL_PREFIXES = (
    "amdhip64", "hipblas", "libhipblas", "rocblas", "hipblaslt", "libhipblaslt",
    "rocsolver", "amd_comgr", "amdhsa", "rocm",
)

# --- oldest Linux we support ------------------------------------------------
# The Linux ROCm artifact is built on Ubuntu 24.04, which is also the oldest
# release Lemonade supports. Ubuntu 24.04 ships glibc 2.39, so an archive that
# needs anything newer than that would fail to start even on a supported machine.
#
# Raise this only together with the runner image in the build job, and only
# after deciding that dropping the older distributions is acceptable. See
# binfmt.elf_glibc_floor() for what this number means.
SUPPORTED_GLIBC = (2, 39)
SUPPORTED_DISTRO = "Ubuntu 24.04"

# Flags Lemonade passes to a ROCm sd-server
# (lemonade-sdk/lemonade src/cpp/server/backends/sdcpp/sdcpp_server.cpp).
# This repo rebuilds upstream every night, so a rename should fail here within
# a day rather than surfacing as a Lemonade bug.
LEMONADE_SERVER_FLAGS = [
    "--listen-port", "-m", "--model", "--diffusion-model", "--llm", "--vae",
    # Raises the log level to verbose. Lemonade passes it only in debug mode, but
    # it is listed because Tier 1 depends on it: without it sd-server never
    # prints where it put the weights or its compute buffers, and the GPU check
    # has nothing to inspect. If upstream renames it, failing here names the
    # cause instead of leaving Tier 1 to report a confusing absence of evidence.
    "-v",
]

# The first line of `sd-server --help`:
#   "stable-diffusion.cpp version master-1106-532d2c1, commit 532d2c1"
VERSION_RE = re.compile(r"stable-diffusion\.cpp version (\S+), commit ([0-9a-f]+)")


# ---------------------------------------------------------------------------
# Archive handling
# ---------------------------------------------------------------------------

def exe(name, os_name):
    return f"{name}.exe" if os_name == "win" else name


def listing(root):
    return sorted(os.listdir(root))


def binaries(root):
    """Every regular file at the top of the archive's binary folder, symlinks excluded.
    """
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
    """The environment to launch an archive binary with: the user-style ROCm
    first, then the archive folder, exactly the search order Lemonade sets up.
    """
    env = dict(os.environ)
    joined = os.pathsep.join(rocm_dirs + [root])
    key = "PATH" if os_name == "win" else "LD_LIBRARY_PATH"
    env[key] = joined + os.pathsep + env.get(key, "")
    return env


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_contents(root, os_name):
    """T0.1: every file a consumer needs is present.

    The tools Lemonade launches, the libraries they load, the two licence files
    every other zip this repository publishes carries, and on Windows the HIP
    runtime shipped next to sd-server.exe. Any `test-*` file means upstream's
    unit tests leaked into the archive, which is worth a warning but harmless.
    """
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
    """T0.2: the file holding the GPU code exists and is big enough to hold it.

    A HIP compile that silently produced nothing still leaves a file behind; on
    Windows stable-diffusion.dll would even exist without any GPU code at all, so
    the size is what tells the two apart.
    """
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
    """T0.3: the GPU code covers every gfx target the build asked for.

    `gpu_targets` is the build job's own matrix entry, carried in the
    qualify-meta JSON, so each OS is compared against the list it was compiled
    with. A missing target is silent at run time: users with that GPU get the CPU.

    An audit that finds nothing at all means the METHOD is broken (the toolchain
    changed the bundle entry format), and that must fail loudly rather than pass.
    Uneven per-target counts mean a target reached only some translation units,
    which is a warning.
    """
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
    """T0.4: the CPU backend variants are present (Linux).

    The Linux build uses GGML_CPU_ALL_VARIANTS, which yields one library per CPU
    generation; a single one means the variant build collapsed. The Windows ROCm
    build links its CPU backend into stable-diffusion.dll, so there is nothing to
    count there.
    """
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
    """The libraries a binary imports, or None when the file is not a binary.
    """
    if os_name == "ubuntu":
        return binfmt.elf_dynamic(path)[0] if binfmt.is_elf(path) else None
    return binfmt.pe_imports(path) if binfmt.is_pe(path) else None


def _is_rocm(name, os_name):
    """Whether a library name belongs to ROCm.
    """
    prefixes = ROCM_LIB_PREFIXES if os_name == "ubuntu" else ROCM_DLL_PREFIXES
    return name.lower().startswith(prefixes)


def check_dependencies(root, os_name):
    """T0.5: survey every binary's dynamic dependencies.

    ROCm libraries are recorded as expected-external: the archive needs a user
    installed ROCm for hipBLAS, hipBLASLt and rocBLAS, and the set a consumer must
    provide is written down per release. Anything else that is neither in the
    archive nor part of the OS is a packaging defect and fails -- the binary would
    not start on a user's machine.
    """
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
    """T0.6: the archive still runs on the oldest Linux we support.

    Each binary records the newest version of the system C library it needs.
    Whichever binary needs the newest one decides the oldest Linux release the
    whole archive can run on. If that goes past what SUPPORTED_GLIBC allows,
    users on a supported distribution get a startup error instead of a program.

    This normally only changes when the build job's runner image changes, which
    is exactly the moment it should be noticed.
    """
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
    """T0.7: no absolute build-machine paths in RUNPATH.

    The HIP link adds the pip-installed ROCm devel tree's lib folder
    (.../_rocm_sdk_devel/lib) to RUNPATH even with CMAKE_INSTALL_RPATH='$ORIGIN',
    so the Linux ROCm job resets every binary to '$ORIGIN' with patchelf before
    packaging ("Normalize RPATH for portable distribution"). This catches a
    build-machine path that survives it, e.g. if that step is removed or stops
    reaching a file. Every published Linux ROCm zip before that step carried one.
    """
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
    """Runs `sd-server --help` once and shares the result between T0.8 and T0.9.

    Needs --run-binaries (the runner's OS matches the archive) and a user-style
    ROCm, without which sd-server cannot load at all. Without either, both
    checks are skipped rather than failed.
    """

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
    """T0.8: the sd-server flags Lemonade launches with are all still accepted.

    Checks `sd-server --help`. Windows needs the user-style ROCm on PATH even for
    this, because stable-diffusion.dll imports hipBLAS at load time.
    """
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
    """T0.9: the binary was built from the upstream commit this run pinned.

    sd-server prints the commit it was built from at the top of --help. Every
    job in a run builds the commit resolve-upstream pinned, so a mismatch means
    the archive came from somewhere else -- a stale cache, a different checkout --
    and nothing else in the record describes it.
    """
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
    """Every file in the user-style ROCm runtime folders, by lower-case name.
    """
    available = {}
    for directory in rocm_dirs:
        if os.path.isdir(directory):
            for entry in os.listdir(directory):
                available.setdefault(entry.lower(), os.path.join(directory, entry))
    return available


def check_rocm_dependencies(root, os_name, rocm_dirs):
    """T0.10: the ROCm libraries this archive needs will actually be there.

    The archive says "I need hipblas.dll", and whether that is true depends
    entirely on what the user installed -- something the build machine can tell
    us nothing about. Checking against the ROCm the archive was COMPILED with
    would be circular: of course the libraries it linked against are present
    there. The useful question is whether they are present in the ROCm a user
    ends up with, which is installed from the same release but with a different
    set of components: the build needs compilers, the user needs per-GPU kernels.
    So `rocm_dirs` must point at a user-style ROCm install (install_rocm.py).
    A library the archive bundles itself counts as present.
    """
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
    """T0.11: the HIP runtime shipped in the archive matches its ROCm version.

    The Windows zip carries amdhip64_7.dll, amd_comgr.dll and rocm_kpack.dll, and
    Windows loads those ahead of anything on PATH. A copy from another ROCm
    version would pair a mismatched HIP runtime with the user's hipBLAS -- the
    kind of mix that made hipGetDeviceCount() return 0 in Lemonade #3718 and sent
    every request to the CPU. Each bundled file must be byte-identical to the
    same file in that ROCm version's user install. Linux bundles no ROCm, so
    there is nothing to compare.
    """
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


# Every check this script runs, in order. T0.12 comes from check_final.py.
TIER0_CHECKS = [
    ("T0.1", "archive contents"),
    ("T0.2", "HIP code present"),
    ("T0.3", "gfx target audit"),
    ("T0.4", "CPU backend variants"),
    ("T0.5", "runtime dependency survey"),
    ("T0.6", "Linux version support"),
    ("T0.7", "library search path"),
    ("T0.8", "sd-server flag surface"),
    ("T0.9", "upstream provenance"),
    ("T0.10", "ROCm dependency resolution"),
    ("T0.11", "bundled HIP runtime matches ROCm"),
]


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

    # Absolute, because --run-binaries starts sd-server from the archive folder.
    rocm_dirs = [os.path.abspath(d) for d in args.rocm_dirs.split(os.pathsep) if d]
    tier = report.TierReport("tier0", report.build_meta(
        artifact=os.path.basename(args.archive), os_name=args.os_name,
        rocm_version=args.rocm_version, sdcpp_tag=args.tag, upstream_sha=args.upstream_sha,
        run_id=args.run_id, run_attempt=args.run_attempt))

    workdir = tempfile.mkdtemp(prefix="tier0-", dir=os.environ.get("RUNNER_TEMP") or None)
    try:
        try:
            root = archive.extract(args.archive, workdir)
        except Exception as exc:  # noqa: BLE001 - any failure here is the finding
            # Still write a report, so the record says why instead of "no report".
            tier.add("T0.1", "archive contents", report.STATUS_FAIL,
                     error=f"the archive cannot be extracted: {exc}")
            root = None
        if root is None:
            for test_id, name in TIER0_CHECKS[1:]:
                tier.add(test_id, name, report.STATUS_SKIP, error="archive could not be extracted")
        else:
            print(f"Extracted {args.archive} -> {root} ({len(listing(root))} entries)")
            help_run = ServerHelp(root, args.os_name, args.run_binaries, rocm_dirs)
            checks = {
                "T0.1": lambda: check_contents(root, args.os_name),
                "T0.2": lambda: check_hip_code(root, args.os_name),
                "T0.3": lambda: check_gfx_targets(root, args.os_name, args.gpu_targets),
                "T0.4": lambda: check_cpu_variants(root, args.os_name),
                "T0.5": lambda: check_dependencies(root, args.os_name),
                "T0.6": lambda: check_linux_version_support(root, args.os_name),
                "T0.7": lambda: check_rpath(root, args.os_name),
                "T0.8": lambda: check_server_flags(help_run),
                "T0.9": lambda: check_provenance(help_run, args.upstream_sha),
                "T0.10": lambda: check_rocm_dependencies(root, args.os_name, rocm_dirs),
                "T0.11": lambda: check_bundled_runtime(root, args.os_name, rocm_dirs),
            }
            for test_id, name in TIER0_CHECKS:
                tier.run(test_id, name, checks[test_id])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    tier.write(args.output)
    tier.print_summary()
    print(f"\nWrote {args.output}")
    if args.fail_on_error and tier.rollup_status() == report.STATUS_FAIL:
        sys.exit(1)


if __name__ == "__main__":
    main()
