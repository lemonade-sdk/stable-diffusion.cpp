#!/usr/bin/env python3
"""
Install the ROCm runtime a user gets into a throwaway venv.

The archives need ROCm's math libraries at run time and do not ship them, so
the checks install `rocm[libraries,device-<arch>]` -- the user's component set,
not the build's `devel` set, which would make the checks circular.

Exports ROCM_RUNTIME_DIRS and ROCM_VENV to later workflow steps.

Usage:
    python3 install_rocm.py --version 10.1.0 --arch gfx1151
"""

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

# Keep in step with the index the build jobs install from.
INDEX_BY_MAJOR = {
    "10": "https://stable.repo.amd.com/rocm/whl-next/",
}


def venv_python(venv_dir):
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def minimal_env(python):
    """PATH cut down to the interpreter and the OS, so a broken user PATH entry cannot abort pip."""
    env = dict(os.environ)
    parts = [os.path.dirname(os.path.abspath(python))]
    if os.name == "nt":
        scripts = os.path.join(os.path.dirname(os.path.abspath(python)), "Scripts")
        if os.path.isdir(scripts):
            parts.append(scripts)
        windir = env.get("SystemRoot") or r"C:\Windows"
        parts += [os.path.join(windir, "system32"), windir,
                  os.path.join(windir, "system32", "Wbem"),
                  os.path.join(windir, "system32", "WindowsPowerShell", "v1.0")]
    else:
        parts += ["/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin",
                  "/usr/sbin", "/sbin"]
    env["PATH"] = os.pathsep.join(p for p in parts if os.path.isdir(p))
    return env


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=False, **kwargs)


def site_packages(venv_dir):
    if os.name == "nt":
        matches = [os.path.join(venv_dir, "Lib", "site-packages")]
    else:
        matches = sorted(glob.glob(os.path.join(venv_dir, "lib", "python3*", "site-packages")))
    for path in matches:
        if os.path.isdir(path):
            return path
    raise SystemExit(f"no site-packages under {venv_dir}")


def runtime_dirs(venv_dir):
    """Every runtime directory: HIP and the math libraries live in separate packages."""
    site = site_packages(venv_dir)
    dirs = []
    for pkg in sorted(glob.glob(os.path.join(site, "_rocm_sdk_*"))):
        for sub in ("bin", "lib", os.path.join("lib", "llvm", "bin"),
                    os.path.join("lib", "llvm", "lib")):
            candidate = os.path.join(pkg, sub)
            if os.path.isdir(candidate) and candidate not in dirs:
                dirs.append(candidate)
    return dirs


def main():
    parser = argparse.ArgumentParser(description="Install a user-style ROCm runtime into a venv")
    parser.add_argument("--version", required=True, help="e.g. 10.1.0")
    parser.add_argument("--arch", required=True, help="e.g. gfx1151")
    parser.add_argument("--prefix", default=None,
                        help="venv parent folder; defaults to <RUNNER_TEMP>/rocm-qual")
    parser.add_argument("--index-url", default=None)
    parser.add_argument("--output-json", default=None)
    args = parser.parse_args()

    prefix = args.prefix or os.path.join(
        os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "rocm-qual")
    major = args.version.split(".")[0]
    index = args.index_url or INDEX_BY_MAJOR.get(major)
    if not index:
        raise SystemExit(f"No package index known for ROCm {args.version}. Add it to INDEX_BY_MAJOR.")

    venv_dir = os.path.join(prefix, f"rocm-{args.version}-{args.arch}")
    os.makedirs(prefix, exist_ok=True)
    if not os.path.exists(venv_python(venv_dir)):
        if run([sys.executable, "-m", "venv", venv_dir],
               env=minimal_env(sys.executable)).returncode != 0:
            raise SystemExit(f"could not create venv at {venv_dir}")

    python = venv_python(venv_dir)
    env = minimal_env(python)
    run([python, "-m", "pip", "install", "--upgrade", "pip", "--quiet"], env=env)

    spec = f"rocm[libraries,device-{args.arch}]=={args.version}"
    if run([python, "-m", "pip", "install", "--index-url", index, spec], env=env).returncode != 0:
        raise SystemExit(
            f"::error::Could not install '{spec}' from {index}. "
            f"`pip index versions --index-url {index} rocm-sdk-device-{args.arch}` "
            "lists what is available there.")

    # pip only warns on an unknown extra, so confirm the device package really landed.
    if run([python, "-m", "pip", "show", "--quiet", f"rocm-sdk-device-{args.arch}"],
           env=env, capture_output=True).returncode != 0:
        raise SystemExit(f"::error::rocm-sdk-device-{args.arch} was not installed by '{spec}'.")

    dirs = runtime_dirs(venv_dir)
    if not dirs:
        raise SystemExit(f"::error::no _rocm_sdk_*/{{bin,lib}} directories under {site_packages(venv_dir)}")

    print(f"\nROCm {args.version} ({args.arch}) runtime directories:")
    for directory in dirs:
        print(f"  {directory}")

    if args.output_json:
        with open(args.output_json, "w", encoding="utf-8") as handle:
            json.dump({"version": args.version, "arch": args.arch, "index": index,
                       "venv": venv_dir, "runtime_dirs": dirs}, handle, indent=2)

    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as handle:
            handle.write(f"ROCM_RUNTIME_DIRS={os.pathsep.join(dirs)}\n")
            handle.write(f"ROCM_VENV={venv_dir}\n")


if __name__ == "__main__":
    main()
