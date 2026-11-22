# ROCm build qualification

Verification of the ROCm archives this repository publishes. Driven from
`.github/workflows/build.yml`, but every script here can be run by hand
against a downloaded release asset, which is the easiest way to understand what
any of them does.

The design follows [lemonade-sdk/llama.cpp](https://github.com/lemonade-sdk/llama.cpp/tree/lemonade/.github/qualify-rocm)
so both repositories behave the same way and feed the same dashboard. Test ids
are this repository's own, numbered from T0.1 and T1.1; they do not share
meanings with llama.cpp's.

## Why this exists

A broken ROCm build does not look broken.

stable-diffusion.cpp treats a GPU it cannot use as a non-event: it writes a line
to the log saying no ROCm device was found, and carries on with the CPU. So a
build whose GPU support is completely broken still starts, still answers every
request, and still returns a perfectly good image. It is simply ten times
slower, with no error anywhere. On a Strix Halo the same 512x512 image takes
about 4 seconds on the GPU and about 35 on the CPU.

The same quietness applies further up. If the compiler produces GPU code for
only some of the gfx targets it was asked to cover, nothing reports an error —
the output file is still created, with the expected name and a plausible size.
And packaging mistakes — a leaked build-machine path, a missing licence, a HIP
runtime from the wrong ROCm version — are invisible until a user's machine
trips over them.

All of those ship green unless something deliberately looks inside the
archive. That is what this suite is for.

## What the archives contain

| | Windows (`sd-<…>-bin-win-rocm-<ver>-x64.zip`) | Linux (`sd-<…>-bin-Linux-Ubuntu-24.04-x86_64-rocm-<ver>.zip`) |
|---|---|---|
| Layout | flat | `build/bin/`, versioned `.so` names stored as symlinks |
| GPU code | linked into `stable-diffusion.dll` | `libggml-hip.so`, a plug-in ggml loads at run time |
| CPU code | linked into `stable-diffusion.dll` | `libggml-cpu-*.so`, one per CPU generation |
| HIP runtime | bundled: `amdhip64_7.dll`, `amd_comgr.dll`, `rocm_kpack.dll` | not bundled |
| ROCm math libraries (hipBLAS, hipBLASLt, rocBLAS) | not bundled | not bundled |

The math libraries come from the user's ROCm install, which Lemonade sets up
automatically (`rocm[libraries,device-<gfx>]`), exactly as for
lemonade-sdk/llama.cpp. Without them sd-server cannot start at all: on Windows
it exits with `0xC0000135` ("DLL not found") before printing a line.

The Windows HIP runtime is bundled because Windows searches a program's own
folder for DLLs before anything else, so the bundled copy wins over the older
`amdhip64_7.dll` the AMD display driver puts in System32 (TheRock#7691). Running
sd-server against that driver copy instead crashes it with an access violation
(`0xC0000005`) before it serves anything.

## Tiers

| Tier | Runs on | Blocks a release? |
|---|---|---|
| Tier 0 — static checks | standard GitHub runners (`ubuntu-24.04`, `windows-2025`) | yes, affected ROCm version only |
| Tier 1 — GPU inference | self-hosted Strix Halo (gfx1151) machines, Windows and Linux | yes, affected ROCm version only |

Both tiers run on every trigger — the nightly schedule, manual runs and pull
requests. The one exception is that Tier 1 is skipped for a pull request opened
from a fork, so code from outside this repository never runs on the self-hosted
machines.

Each tier runs one job per published archive — two operating systems by three
ROCm versions — so a failure is reported against the exact archive it affects.

Each ROCm version is treated as an independent product. A consumer downloads
exactly one of them, so a failure in one version removes only that version's
Windows and Linux pair; the other versions, and every non-ROCm backend (CPU,
Vulkan, CUDA, Metal), publish as normal. The release job then exits non-zero so
the gap is noticed.

A version ships only when, for both of its archives, Tier 0 and Tier 1 both
reported and neither failed, and the final-archive check (T0.12) passed. Unlike
lemonade-sdk/llama.cpp, a Tier 1 that never reported — the GPU runner was
offline, or ROCm failed to install — blocks the version rather than letting it
ship untested. A `warn` never blocks.

On a pull request nothing is published at all, so there is no aggregate step to
pass judgement. Each job therefore fails on its own as soon as a check fails —
otherwise a pull request would report success no matter what the checks found.

### Tier 0 — `tier0_static.py`, `check_final.py`

Runs against the archive as it will be published, not against the build
directory, because most of what it looks for only exists once the files have
been packaged.

| ID | Check | Blocks release |
|---|---|---|
| T0.1 | every file a user needs is present: sd-cli, sd-server, the stable-diffusion and ggml libraries, both licence files, and on Windows the bundled HIP runtime | yes |
| T0.2 | the file holding the GPU code exists and is big enough to hold it | yes |
| T0.3 | that file really contains code for every gfx target the build asked for (on Linux, `libstable-diffusion.so`'s own GPU code too) | yes |
| T0.4 | the CPU backend variants are present (Linux; Windows links the CPU backend in) | yes |
| T0.5 | every library the archive needs is either inside it, part of the OS, or a ROCm library | yes |
| T0.6 | the archive still runs on the oldest Linux release we support | yes |
| T0.7 | no build-machine paths left inside the binaries | yes |
| T0.8 | `sd-server` still accepts the flags Lemonade launches it with | yes |
| T0.9 | `sd-server` reports the upstream commit this run pinned | yes |
| T0.10 | the ROCm libraries the archive needs exist in the ROCm a user installs | yes |
| T0.11 | the bundled HIP runtime is byte-identical to that ROCm version's own copy | yes |
| T0.12 | the finished archive reads back intact and is the one the build job produced and the qualify jobs tested | yes |

T0.12 comes from `check_final.py`, which runs in the release job on the archives
about to be uploaded. It compares each archive's SHA-256 with the one its build
job recorded, so the bytes that ship are the bytes every other check and Tier 1
ran on. The release job never repacks or merges an archive; it uploads it under
the name the build job gave it, which is also the name Lemonade downloads.

T0.7 exists because the HIP link writes the build machine's pip-installed ROCm
folder (`…/_rocm_sdk_devel/lib`) into every Linux binary's library search path.
The build job now resets it to `$ORIGIN` with `patchelf`; every Linux ROCm zip
published before that carried the leaked path.

### Tier 1 — `tier1_gpu.py`

Starts `sd-server` from the archive with the flags Lemonade uses, loads the
model Lemonade validates stable-diffusion.cpp with (SD-Turbo-GGUF), and asks it
for one 512x512 image over the same OpenAI-style endpoint Lemonade calls.

| ID | Check | Blocks release |
|---|---|---|
| T1.1 | the server starts and serves its web page | yes |
| T1.2 | the GPU actually did the work | yes |
| T1.3 | the GPU sd-server found is the gfx target this runner validates | yes |
| T1.4 | the image is a PNG of the requested size that decodes and is not blank | yes |
| T1.5 | the same request again gives identical bytes | no |
| T1.6 | the running server loaded the bundled HIP runtime from the archive and every other ROCm library from the user's ROCm install | yes |

**T1.2 is the one that matters.** Every other check in this tier passes
perfectly well when the GPU was never used at all — the image is simply slower
to arrive. How it avoids being fooled is described below.

T1.3 is what makes a record say `hardware_validated`. Only gfx1151 has test
hardware today; the target comes from the workflow, so adding a runner with
another GPU is one more Tier 1 leg, not a change to the scripts.

T1.5 is recorded but never blocks a release. On the gfx1151 runners sd-server
is in fact bit-for-bit repeatable, so a difference is worth a look, but GPUs do
not promise it and treating it as a release gate would block good builds.

T1.6 catches the case behind Lemonade #3718: a HIP runtime from somewhere else —
the display driver's copy in System32, a system ROCm under `/opt/rocm` — mixed
with this archive's ROCm, which made the GPU invisible and sent every request to
the CPU.

## How some of this works

### Counting the gfx targets inside the GPU code

`binfmt.py` searches the file holding the GPU code for the short text labels the
compiler writes next to each gfx target's code, rather than asking an AMD tool to
list them.

The reason is that the GPU code lives in a named block inside the file, and
Windows shortens those names to eight characters once a program is linked — so
a block Linux calls `.hip_fatbin` is called `.hip_fat` there. Any tool asked to
find the block by name comes back empty on Windows. Searching the file's bytes
does not care what the block is called, so one implementation covers both
operating systems and needs no AMD tooling installed, which in turn means the
check can run on an ordinary runner against the archive we actually ship.

Two deliberate behaviours:

- Finding **no** gfx targets at all is a failure, not a pass. A check that can
  silently come up empty is worse than no check.
- The number of entries per target is reported, not just which targets are
  present. The compiler writes one entry per source file, so in a healthy build
  every target has the same count (141 in `libggml-hip.so`, 150 in the Windows
  `stable-diffusion.dll`); a target with a lower count was built into only part
  of the project, which a present/absent check would miss.

The expected list is each build job's own `gpu_targets`, carried in the
`qualify-meta-<os>-rocm-<ver>.json` it uploads next to the archive, so each
operating system is audited against exactly what it was compiled for.

### Proving the GPU was used

Two things make this harder than it looks, and both have to be handled or T1.2
reports the wrong answer.

**The server leaves out half the evidence by default.** At its default log
level sd-server reports which device it found and where it put the weights, but
not where it ran anything. That is why Tier 1 launches it with `-v`, as Lemonade
does in CI. Without that flag T1.2 finds no compute buffers and fails a
perfectly good build — and says so, naming the missing flag. T0.8 checks every
night that `-v` still exists.

**The obvious evidence is not evidence.** stable-diffusion.cpp prints:

```
[V] Initializing backend: ROCm0
```

It is the line Lemonade's own validator looks for, but it is printed *before*
the device is started, so it also appears when the device then fails to
initialise and everything falls back to the CPU.

So T1.2 requires all three of these, each naming a GPU or VRAM, which cannot
appear unless a ROCm device was found and used:

```
  Device 0: AMD Radeon(TM) 8060S Graphics, gfx1151 (0x1151), VMM: no, ...
[I] total params memory size = 1929.50MB (VRAM 1929.50MB, RAM 0.00MB): text_encoders 358.79MB(VRAM), diffusion_model 1411.02MB(VRAM), vae 159.68MB(VRAM), ...
[V] unet compute buffer size: 87.86 MB(VRAM) on ROCm0 (peak across 1 segment)
```

Every model component with weights must be in VRAM, not just some, and the
compute buffer must be larger than zero. The patterns match any device index
(`ROCm0`, `ROCm1`, …), so a machine with two GPUs passes as long as all the work
landed on GPUs.

The same sd-server with the GPU hidden prints instead:

```
ggml_cuda_init: failed to initialize ROCm: no ROCm-capable device is detected
[W] auto-fit: no GPU devices; using the default backend
[I] total params memory size = 1929.50MB (VRAM 0.00MB, RAM 1929.50MB): text_encoders 358.79MB(RAM), ...
[V] unet compute buffer size: 123.14 MB(RAM) on CPU (peak across 1 segment)
```

and still returns a valid image. T1.2 fails it on every count.

### Which ROCm the checks verify against

The archives do not contain ROCm's math libraries. A user installs them
separately, and stable-diffusion.cpp loads them at run time. So "are this
archive's libraries available?" depends entirely on what the user installed.

Checking against the ROCm used to *build* the archive would always pass — those
are the exact files it was compiled against. `install_rocm.py` therefore
installs the components an end user gets (the kernels for their GPU) rather
than the components a build needs (compilers), and T0.10, T0.11 and Tier 1 all
verify against that. Everything goes into a throwaway virtual environment under
the runner's temporary directory; nothing is installed system-wide.

### Which Linux versions the archive runs on

Every Linux program records the newest version of the system C library it
relied on, and refuses to start on a machine with an older one. Newer Linux
releases ship newer versions of that library, so building on a newer Linux
silently narrows who can run the result — nothing warns about it at build time.

T0.6 reads that number out of the archive and compares it against
`SUPPORTED_GLIBC` in `tier0_static.py`, which is set to what the oldest Linux
release we support provides (Ubuntu 24.04, glibc 2.39). The check passes today
and would fail if the build job's runner image moved to a newer Linux without
that decision being made deliberately.

### Nightlies with nothing new

The release tag is `master-<upstream commit count>-<fork commit>`, so it only
changes when upstream stable-diffusion.cpp or this repository changes. A nightly
first checks whether the release for its tag already holds every backend family
and `checksums.txt`; if it does, the whole run is skipped, and only a
`last_check` of `"unchanged"` is written to the feed. If the release is missing
anything — say ROCm 10.2 was withheld the night before — the nightly rebuilds,
re-qualifies, and uploads only what is missing. An asset already in a release is
never replaced, because Lemonade may already be pinned to it.

## Running by hand

Everything here works against a downloaded release asset on a normal machine,
which is the quickest way to check a CI result yourself.

```bash
# Static checks on a downloaded release asset
python3 .github/qualify-rocm/install_rocm.py --version 10.1.0 --arch gfx1151
python3 .github/qualify-rocm/tier0_static.py \
    --archive sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip \
    --os win --rocm-version 10.1.0 \
    --gpu-targets "gfx1010;gfx1011;...;gfx1201" \
    --upstream-sha <upstream commit> --run-binaries \
    --rocm-dirs "<dirs printed by install_rocm.py>" \
    --output tier0.json

# Combine reports into one record per archive and decide what may ship
python3 .github/qualify-rocm/aggregate.py \
    --fragments-dir ./fragments --tag master-1106-abc1234 \
    --records-dir ./qualification --fail-on-no-promote
```

To run the GPU checks on a machine with a supported AMD GPU, install a
user-style ROCm first — it goes into a throwaway folder and changes nothing
system-wide — then point the inference check at the archive. The model is
downloaded and verified automatically if it is not already cached.

```bash
python3 .github/qualify-rocm/install_rocm.py --version 10.1.0 --arch gfx1151
# install_rocm.py prints the runtime directories; pass them through --rocm-dirs,
# or let the workflow's exported ROCM_RUNTIME_DIRS be picked up automatically.

python3 .github/qualify-rocm/tier1_gpu.py \
    --archive sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip \
    --rocm-version 10.1.0 --rocm-dirs "<dirs printed above>" \
    --output tier1.json
```

Running the GPU check with `HIP_VISIBLE_DEVICES=-1` set on sd-server is a
useful sanity check on T1.2 itself: sd-server still returns an image, and T1.2
must fail.

Each run writes a `qualification-<archive>.json` record that is attached to the
release. It lists which gfx targets the archive contains code for, which of them
were actually tested on hardware, and the result of every check — so questions
like "was this gfx target ever verified in that build?" stay answerable long after
the CI logs have expired.

## Published feed

The release job's records are also published, pass or fail, to the
`qualification-data` branch by `publish_qualification.py`, so a dashboard can
follow the history of every run without reading each release. An archive whose
build failed gets a placeholder row, so a missing archive shows up as a failure
rather than a gap, and a nightly skipped for lack of changes is recorded too.
The layout and every field are described in [FEED.md](FEED.md).

## Files

| File | Role |
|---|---|
| `tier0_static.py` | Tier 0 checks T0.1–T0.11 |
| `check_final.py` | T0.12, in the release job |
| `tier1_gpu.py` | Tier 1 checks T1.1–T1.6 |
| `install_rocm.py` | installs a user-style ROCm runtime for Tier 0 and Tier 1 |
| `binfmt.py` | reads gfx targets, imports, RUNPATH and glibc out of ELF and PE files |
| `archive.py` | unpacks an archive the way a user's unzip would |
| `report.py` | the fragment and record format every tier writes |
| `aggregate.py` | merges fragments per archive and decides which ROCm versions ship |
| `release_assets.py` | which assets make up each backend family; used to drop incomplete families and to skip unchanged nightlies |
| `release_notes.py` | renders the release notes from the attached assets |
| `publish_qualification.py` | writes the `qualification-data` feed |

## Out of scope

- **Performance.** Tracked downstream against these same artifacts; a second
  set of numbers here would split the history.
- **Image quality.** T1.4 covers "the output is a real picture", which is what
  catches a backend producing NaNs or a blank image. Judging whether an image
  is *good* is a separate problem needing a different kind of test.
- **Other backends.** CPU, Vulkan, CUDA and Metal archives get the release
  job's family completeness check only.
- **Other gfx targets on hardware.** Every compiled target is checked for
  presence (T0.3), but only gfx1151 is run. Records say so explicitly rather
  than implying the rest were tested too.
