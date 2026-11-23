#!/usr/bin/env python3
"""
Which release assets make up each backend family, and whether a family is complete.

A family is a set of archives that only makes sense together: the eight CUDA
architectures on Linux x64 and Windows x64 plus the sm_121 (GB10) Linux arm64
build, the CPU builds for Linux and Windows, and so on. Windows Vulkan and the
all-architecture Windows CUDA 12 build (with its runtime zip) are families of
their own, so a failure there never withholds the Linux builds. Each family ships as a complete set or not at all, so a consumer never
finds half of one. Families are independent of each other, and each ROCm
version is a family of its own -- a consumer downloads exactly one ROCm
version, so a gap in 10.2 is no reason to withhold a good 10.0. What must stay
together is the Windows + Linux pair within a version.

The rules live here, in one place, because two jobs need them:

  check-dir      the release job: drop every incomplete family from the folder
                 of built artifacts before anything is uploaded. Exits non-zero
                 only when nothing at all is left to publish.
  check-release  the resolve-upstream job, on a nightly: does the release for
                 this tag already hold every family (and checksums.txt)? If so,
                 the nightly has nothing to add and skips the whole build.

Keep the lists in step with the build matrices in build.yml.

Usage:
    python3 release_assets.py check-dir --dir artifact --branch master \
        --short abc1234 --rocm-versions 10.0,10.1,10.2
    python3 release_assets.py check-release --names-file assets.txt --branch master \
        --short abc1234 --rocm-versions 10.0,10.1,10.2
"""

import argparse
import fnmatch
import os
import sys

CUDA_ARCHS = ["sm_75", "sm_80", "sm_86", "sm_89", "sm_90", "sm_100", "sm_120", "sm_121"]


def families(branch, short, rocm_versions):
    p = f"sd-{branch}-{short}"
    cuda = []
    for sm in CUDA_ARCHS:
        cuda += [f"{p}-ubuntu-cuda-{sm}-x64.tar.xz", f"{p}-windows-cuda-{sm}-x64.zip"]
    cuda.append(f"{p}-ubuntu-cuda-sm_121-arm64.tar.xz")
    out = {"cuda": cuda}
    for version in rocm_versions:
        out[f"rocm-{version}"] = [f"{p}-bin-win-rocm-{version}.0-x64.zip",
                                  f"{p}-bin-Linux-Ubuntu-24.04-x86_64-rocm-{version}.0.zip"]
    out["cpu"] = [f"{p}-bin-Linux-*-x86_64.zip", f"{p}-bin-win-cpu-x64.zip"]
    out["vulkan"] = [f"{p}-bin-Linux-*-x86_64-vulkan.zip"]
    # Separate from Linux Vulkan, so a failed Windows build does not withhold Linux.
    out["vulkan-win"] = [f"{p}-bin-win-vulkan-x64.zip"]
    # The all-architecture Windows CUDA 12 build and the CUDA runtime it needs.
    out["cuda12-win"] = [f"{p}-bin-win-cuda12-x64.zip", "cudart-sd-bin-win-cu12-x64.zip"]
    out["metal"] = [f"{p}-bin-Darwin-macOS-*-arm64.zip"]
    return out


def missing_by_family(names, fams):
    return {family: [pat for pat in patterns if not fnmatch.filter(names, pat)]
            for family, patterns in fams.items()}


def write_outputs(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def check_dir(args, fams):
    names = sorted(os.listdir(args.dir))
    missing, summary = 0, []
    for family, patterns in missing_by_family(names, fams).items():
        for pattern in patterns:
            print(f"::error::[{family}] Missing artifact: {pattern}")
        if not patterns:
            continue
        print(f"::warning::{family} family is incomplete ({len(patterns)} artifact(s) missing) "
              f"- dropping all {family} artifacts from this release.")
        for pattern in fams[family]:
            for name in fnmatch.filter(names, pattern):
                os.remove(os.path.join(args.dir, name))
                print(f"removed {name}")
        missing += len(patterns)
        summary.append(f"{family}: {len(patterns)} missing, family dropped")

    write_outputs(missing=missing, summary=", ".join(summary))
    left = [n for n in os.listdir(args.dir) if n.endswith((".zip", ".tar.xz"))]
    if not left:
        print(f"::error::No release artifacts left in {args.dir}; refusing to publish nothing.")
        sys.exit(1)
    print(f"{len(left)} artifact(s) ready to publish" + (f"; {', '.join(summary)}" if summary else ""))


def check_release(args, fams):
    with open(args.names_file, encoding="utf-8") as handle:
        names = [line.strip() for line in handle if line.strip()]
    gaps = {f: m for f, m in missing_by_family(names, fams).items() if m}
    if "checksums.txt" not in names:
        gaps["checksums"] = ["checksums.txt"]
    for family, patterns in gaps.items():
        print(f"[{family}] not yet published: {', '.join(patterns)}")
    write_outputs(complete="false" if gaps else "true")
    print("The release already holds every family." if not gaps else "The release is incomplete.")


def main():
    parser = argparse.ArgumentParser(description="Check which backend families are complete")
    parser.add_argument("mode", choices=["check-dir", "check-release"])
    parser.add_argument("--branch", required=True)
    parser.add_argument("--short", required=True, help="fork commit in the asset names")
    parser.add_argument("--rocm-versions", required=True, help="X.Y, e.g. 10.0,10.1,10.2")
    parser.add_argument("--dir", help="check-dir: folder of built artifacts")
    parser.add_argument("--names-file", help="check-release: asset names, one per line")
    args = parser.parse_args()
    fams = families(args.branch, args.short,
                    [v for v in args.rocm_versions.replace(" ", "").split(",") if v])
    (check_dir if args.mode == "check-dir" else check_release)(args, fams)


if __name__ == "__main__":
    main()
