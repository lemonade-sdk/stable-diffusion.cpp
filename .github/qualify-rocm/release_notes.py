#!/usr/bin/env python3
"""
Write the release notes from the assets actually attached to the release, so a
family that was withheld gets no link and is listed under "Not in this release".

Usage:
    python3 release_notes.py --tag master-1106-abc1234 --repo lemonade-sdk/stable-diffusion.cpp \
        --assets-file assets.txt --rocm-versions 10.0,10.1,10.2 --summary "..." --output notes.md
"""

import argparse
import re

# Oldest OS each package starts on, set by the runner image its build job uses.
LINUX_X64_RUNS_ON = "Ubuntu 24.04 or newer"
CUDA_X64_RUNS_ON = "Ubuntu 22.04 or newer"
CUDA_ARM64_RUNS_ON = "Ubuntu 24.04 or newer"


def render(tag, repo, names, rocm_versions, summary):
    branch, _count, short = tag.split("-")
    p = re.escape(f"sd-{branch}-{short}")
    attached = set(names)
    url = lambda name: f"https://github.com/{repo}/releases/download/{tag}/{name}"  # noqa: E731
    link = lambda label, name: f"[{label}]({url(name)})"  # noqa: E731

    def first(pattern):
        return next((n for n in sorted(names) if re.fullmatch(pattern, n)), None)

    def cuda(platform, suffix):
        found = sorted(((int(m.group(1)), m.group(0)) for m in
                        (re.fullmatch(rf"{p}-{platform}-cuda-sm_(\d+)-{suffix}", n) for n in names) if m))
        return [link(f"sm_{sm}", name) for sm, name in found]

    rocm = {v: {"ubuntu": f"sd-{branch}-{short}-bin-Linux-Ubuntu-24.04-x86_64-rocm-{v}.0.zip",
                "win": f"sd-{branch}-{short}-bin-win-rocm-{v}.0-x64.zip"} for v in rocm_versions}
    cpu_linux = first(rf"{p}-bin-Linux-[^/]+-x86_64\.zip")
    vulkan_linux = first(rf"{p}-bin-Linux-[^/]+-x86_64-vulkan\.zip")
    metal = first(rf"{p}-bin-Darwin-macOS-[^/]+-arm64\.zip")
    win = {key: name for key, name in {
        "cpu": f"sd-{branch}-{short}-bin-win-cpu-x64.zip",
        "vulkan": f"sd-{branch}-{short}-bin-win-vulkan-x64.zip",
        "cuda12": f"sd-{branch}-{short}-bin-win-cuda12-x64.zip",
        "cudart": "cudart-sd-bin-win-cu12-x64.zip"}.items() if name in attached}

    linux_x64 = []
    if cpu_linux:
        linux_x64.append(["CPU", LINUX_X64_RUNS_ON, link("download", cpu_linux)])
    if vulkan_linux:
        linux_x64.append(["Vulkan", LINUX_X64_RUNS_ON, link("download", vulkan_linux)])
    # A ROCm version is linked only as a complete Windows + Linux pair.
    paired = {v: n for v, n in rocm.items() if n["ubuntu"] in attached and n["win"] in attached}
    linux_x64 += [[f"ROCm {v}.0", LINUX_X64_RUNS_ON, link("download", names_["ubuntu"])]
                  for v, names_ in paired.items()]
    if cuda("ubuntu", r"x64\.tar\.xz"):
        linux_x64.append(["CUDA", CUDA_X64_RUNS_ON, " ".join(cuda("ubuntu", r"x64\.tar\.xz"))])
    linux_arm64 = ([["CUDA", CUDA_ARM64_RUNS_ON, " ".join(cuda("ubuntu", r"arm64\.tar\.xz"))]]
                   if cuda("ubuntu", r"arm64\.tar\.xz") else [])

    def table(title, rows):
        if not rows:
            return []
        return [f"**{title}**", "", "| Package | Runs on | Download |", "|---|---|---|",
                *[f"| {' | '.join(r)} |" for r in rows], ""]

    win_rocm = [link(f"{v}.0", n["win"]) for v, n in paired.items()]
    windows = []
    if "cpu" in win:
        windows.append(f"- CPU: {link('download', win['cpu'])}")
    if "vulkan" in win:
        windows.append(f"- Vulkan: {link('download', win['vulkan'])}")
    if win_rocm:
        windows.append(f"- ROCm: {' | '.join(win_rocm)}")
    if "cuda12" in win:
        runtime = f" (runtime: {link('cudart', win['cudart'])})" if "cudart" in win else ""
        windows.append(f"- CUDA 12, all architectures: {link('download', win['cuda12'])}{runtime}")
    win_cuda = cuda("windows", r"x64\.zip")
    if win_cuda:
        windows.append(f"- CUDA: {' | '.join(win_cuda)}")

    missing = [f"- ROCm {v}.0: did not build, or did not pass the release checks. The "
               "attached `qualification-*.json` records list the failing checks."
               for v in rocm if v not in paired]
    if not any("-cuda-sm_" in n for n in names):
        missing.append("- CUDA: at least one CUDA build failed, so no CUDA builds are included.")
    if not cpu_linux or "cpu" not in win:
        missing.append("- CPU: did not build.")
    if not vulkan_linux:
        missing.append("- Vulkan (Linux): did not build.")
    if not metal:
        missing.append("- Metal: did not build.")

    body = ["<details open>", "", summary, "", "</details>", "",
            *table("Linux x64", linux_x64), *table("Linux arm64", linux_arm64),
            *(["**Windows x64**", *windows, ""] if windows else []),
            *(["**macOS arm64**", f"- Metal: {link('download', metal)}", ""] if metal else [])]
    if paired:
        body += ["ROCm packages need the ROCm runtime of the same version "
                 "(`rocm[libraries]` from TheRock); Lemonade installs it automatically.", ""]
    if missing:
        body += ["**Not in this release**", *missing]
    return "\n".join(body).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(description="Render release notes")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--assets-file", required=True)
    parser.add_argument("--rocm-versions", required=True, help="X.Y, e.g. 10.0,10.1,10.2")
    parser.add_argument("--summary", default="")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.assets_file, encoding="utf-8") as handle:
        names = [line.strip() for line in handle if line.strip()]
    versions = [v for v in args.rocm_versions.replace(" ", "").split(",") if v]
    body = render(args.tag, args.repo, names, versions, args.summary)
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write(body)
    print(body)


if __name__ == "__main__":
    main()
