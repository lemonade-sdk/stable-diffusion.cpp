# Qualification feed — published contract

Every release run publishes its ROCm qualification records to the
**`qualification-data`** branch of this repository, in a fixed layout an
external dashboard can poll. This file is that contract. A copy is kept on the
branch as `qualification/SCHEMA.md`, refreshed on every publish, so the data and
its description never drift apart. Treat it as versioned (`schema_version`).

The layout and field names match the
[lemonade-sdk/llama.cpp feed](https://github.com/lemonade-sdk/llama.cpp/blob/qualification-data/qualification/SCHEMA.md),
so one dashboard can read both. Where the two repositories' archives really
differ, the values differ too;
[Differences from the llama.cpp feeds](#differences-from-the-llamacpp-feeds)
lists every one.

## Why a branch and not the releases

The records are already attached to each release, but that copy cannot serve a
dashboard:

- A release only holds what shipped. The dashboard needs every run, including
  the ones whose ROCm archives were withheld.
- Reading history out of releases means one API call per release, and the
  Releases API allows only 60 unauthenticated requests an hour.

A file on a branch is one plain HTTP GET with no authentication and no rate
limit. The branch is only the transport; the contract below would hold
unchanged if the files moved to GitHub Pages, a bucket or a database.

The branch is created by the first run that publishes; nothing has to be set up
by hand.

## Layout

```
qualification/
  index.json                    # the feed: one compact row per archive per run
  reports/<build_id>.json       # the full record for that row (drill-down)
  latest/<os>-rocm-<ver>.json   # the newest full record for each archive
  SCHEMA.md                     # a copy of this file
```

Public raw base URL (no authentication, about 5 minutes of CDN caching):

```
https://raw.githubusercontent.com/lemonade-sdk/stable-diffusion.cpp/qualification-data/qualification/
```

The dashboard's one regular fetch is `.../qualification/index.json`.

## One row per archive

A release run builds six ROCm archives — Windows and Linux for each of ROCm
10.0, 10.1 and 10.2 (`ROCM_VERSIONS` in `build.yml`) — and each is qualified on
its own. Each one gets its own row, because an archive is what a user downloads
and what a bug report names. Rows from the same run share `run_id`; group on it
to show a run as a whole.

Every archive the run was meant to build gets a row, including one whose build
failed (see [Placeholder rows](#placeholder-rows)). A run that built anything
therefore always has six rows.

## `build_id` and re-runs

```
build_id = "<run_id>-<os>-rocm-<rocm_version>"     e.g. 45012345678-win-rocm-10.1
build_id = "<run_id>.<attempt>-<os>-rocm-<rocm_version>"   e.g. 45012345678.2-win-rocm-10.1
```

The second form is used only for a re-run that built a different tag than the
rows already in the feed for that run (see below).

Unique per archive per run. Publishing is an upsert keyed on `build_id`. A
re-run of a run keeps its `run_id`, so it replaces that run's rows and its
`reports/` files instead of adding new ones; the latest attempt's values win,
except one: when an earlier attempt of the run already shipped an archive for
the same tag and the re-run did not upload it again (it cannot replace a
published asset), the earlier attempt's row and report are kept, because they
describe the bytes users actually download. This needs the earlier attempt's
own feed publish to have succeeded; if it did not, the re-run's row is
published with `published: false`.

"Re-run all jobs" resolves upstream again, so if upstream moved in the
meantime the re-run builds a different tag (and, if it passes, a new release).
Its rows then use `<run_id>.<attempt>-…` instead of replacing the rows of the
tag the earlier attempt built, so both stay in `index.json`. "Re-run failed
jobs" keeps the earlier attempt's resolved commit, so it never changes the tag.
`run_attempt` records which attempt published the rows. A new run has a new
`run_id` and adds new rows.

## Which runs appear

Runs of `.github/workflows/build.yml` whose `release` job runs: the scheduled
nightly runs, and manual runs with `create_release` ticked. Pass or fail, every
one of them is published. The records normally come from the `release` job; if
it stopped before writing them, `publish-qualification` builds them from the
Tier 0 and Tier 1 reports instead.

A nightly whose release tag is already fully published builds nothing (see
[`last_check`](#last_check)). It adds no rows, but it does update `last_check`,
so "nothing changed upstream" can be told apart from "the nightly did not run".

Not in the feed:

- **Pull requests**, and manual runs without `create_release`.
- **A run that was cancelled.**
- **A run whose `resolve-upstream` job failed.** No upstream commit was
  pinned, so nothing was built.

## Example `index.json`

```jsonc
{
  "schema_version": 2,
  "repo": "lemonade-sdk/stable-diffusion.cpp",
  "updated_at": "2026-11-23T14:52:10Z",       // the most recent generated_at of any row
  "last_check": {                               // what the most recent run decided
    "status": "unchanged",                      // built | unchanged
    "tag": "master-1106-abc1234",
    "upstream_sha": "532d2c15c430a785a712b2d94281f04e733008a4",
    "run_id": "45099999999",
    "run_attempt": 1,
    "run_url": "https://github.com/lemonade-sdk/stable-diffusion.cpp/actions/runs/45099999999",
    "event": "schedule",
    "checked_at": "2026-11-24T12:18:40Z"
  },
  "builds": [                                   // newest run (highest run_id) first
    // ... the other rows of run 45012345678 ...

    // A placeholder: this archive's build failed, so there was nothing to check.
    {
      "build_id": "45012345678-win-rocm-10.2",
      "tag": "master-1106-abc1234",             // the release tag; set even though nothing shipped
      "upstream_sha": "532d2c15c430a785a712b2d94281f04e733008a4",
      "artifact": "sd-master-abc1234-bin-win-rocm-10.2.0-x64.zip",   // the name it would have had
      "os": "win",
      "rocm_version": "10.2",
      "gfx_target": null,
      "hardware_validated": false,
      "gfx": { "requested": 0, "present": 0, "missing": [] },  // no archive, so nothing was audited
      "generated_at": "2026-11-23T14:52:10Z",
      "run_id": "45012345678",
      "run_attempt": 1,
      "run_url": "https://github.com/lemonade-sdk/stable-diffusion.cpp/actions/runs/45012345678",
      "event": "schedule",
      "build_result": "fail",                   // the windows-latest-rocm (10.2.0) job failed
      "tests": { "passed": 0, "failed": 0, "warned": 0, "skipped": 0, "missing": 17, "not_applicable": 1, "total": 18 },
      "qualified": false,
      "version_qualified": false,
      "published": false,
      "overall": "fail",
      "tier0": "missing",                       // should have run, but there was no archive
      "tier1": "missing",
      "blocked_by": ["build failed"],
      "blocking_tests": [],
      "results": {
        "T0.1": "missing", "T0.2": "missing", "T0.3": "missing", "T0.4": "missing",
        "T0.5": "missing", "T0.6": "missing", "T0.7": "missing", "T0.8": "missing",
        "T0.9": "missing", "T0.10": "missing", "T0.11": "missing", "T0.12": "not_applicable",
        "T1.1": "missing", "T1.2": "missing", "T1.3": "missing", "T1.4": "missing",
        "T1.5": "missing", "T1.6": "missing"
      },
      "report_url": "reports/45012345678-win-rocm-10.2.json"
    },

    // The Linux archive of the same version, from the same run: it passed every
    // check, but did not ship, because its Windows counterpart above never built.
    {
      "build_id": "45012345678-ubuntu-rocm-10.2",
      "tag": "master-1106-abc1234",
      "upstream_sha": "532d2c15c430a785a712b2d94281f04e733008a4",
      "artifact": "sd-master-abc1234-bin-Linux-Ubuntu-24.04-x86_64-rocm-10.2.0.zip",
      "os": "ubuntu",
      "rocm_version": "10.2",
      "gfx_target": "gfx1151",
      "hardware_validated": true,
      "gfx": { "requested": 24, "present": 24, "missing": [] },
      "generated_at": "2026-11-23T14:52:10Z",
      "run_id": "45012345678",
      "run_attempt": 1,
      "run_url": "https://github.com/lemonade-sdk/stable-diffusion.cpp/actions/runs/45012345678",
      "event": "schedule",
      "build_result": "pass",
      "tests": { "passed": 16, "failed": 0, "warned": 0, "skipped": 1, "missing": 0, "not_applicable": 1, "total": 18 },
      "qualified": false,                       // dropped before the final-archive check, which the
                                                // release job requires, so it was never cleared to ship
      "version_qualified": false,               // its Windows counterpart has no record
      "published": false,                       // so ROCm 10.2 was withheld from master-1106-abc1234
      "overall": "fail",
      "tier0": "pass",
      "tier1": "pass",
      "blocked_by": ["T0.12 missing"],
      "blocking_tests": [],
      "results": {
        "T0.1": "pass", "T0.2": "pass", "T0.3": "pass", "T0.4": "pass",
        "T0.5": "pass", "T0.6": "pass", "T0.7": "pass", "T0.8": "pass",
        "T0.9": "pass", "T0.10": "pass", "T0.11": "skip",
        "T0.12": "not_applicable",              // dropped before the final-archive check
        "T1.1": "pass", "T1.2": "pass", "T1.3": "pass", "T1.4": "pass",
        "T1.5": "pass", "T1.6": "pass"
      },
      "report_url": "reports/45012345678-ubuntu-rocm-10.2.json"
    },

    // ... the rows of earlier runs ...

    // An archive that passed and shipped.
    {
      "build_id": "44999999999-win-rocm-10.1",
      "tag": "master-1106-abc1234",             // the release it was attached to
      "upstream_sha": "532d2c15c430a785a712b2d94281f04e733008a4",
      "artifact": "sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip",
      "os": "win",                              // win | ubuntu
      "rocm_version": "10.1",                   // currently 10.0 | 10.1 | 10.2
      "gfx_target": "gfx1151",                  // the one gfx target Tier 1 ran on; null when Tier 1 did not run
      "hardware_validated": true,               // Tier 1 confirmed it ran on a gfx_target GPU
      "gfx": { "requested": 20, "present": 20, "missing": [] },
      "generated_at": "2026-11-22T14:47:31Z",   // ISO-8601 UTC, when this row's record was written
      "run_id": "44999999999",
      "run_attempt": 1,                         // the attempt that published this row
      "run_url": "https://github.com/lemonade-sdk/stable-diffusion.cpp/actions/runs/44999999999",
      "event": "schedule",                      // schedule | workflow_dispatch
      "build_result": "pass",                   // pass | fail | cancelled | skipped | unknown
      "tests": { "passed": 15, "failed": 0, "warned": 0, "skipped": 3, "missing": 0, "not_applicable": 0, "total": 18 },
      "qualified": true,                        // passed every gating check required of it: Tier 0,
                                                // Tier 1 and T0.12. Not whether it shipped.
      "version_qualified": true,                // both archives of ROCm 10.1 exist and qualified
      "published": true,                        // this run attached it to the release
      "overall": "pass",                        // pass | warn | fail; fail exactly when qualified is false
      "tier0": "pass",                          // pass | warn | fail | missing
      "tier1": "pass",                          // pass | warn | fail | missing
      "blocked_by": [],                         // why it did not qualify, e.g. ["tier1 missing"]
      "blocking_tests": [],                     // the gating tests that failed, e.g. ["T1.2"]
      "results": {
        "T0.1": "pass", "T0.2": "pass", "T0.3": "pass", "T0.4": "skip",
        "T0.5": "pass", "T0.6": "skip", "T0.7": "skip", "T0.8": "pass",
        "T0.9": "pass", "T0.10": "pass", "T0.11": "pass", "T0.12": "pass",
        "T1.1": "pass", "T1.2": "pass", "T1.3": "pass", "T1.4": "pass",
        "T1.5": "pass", "T1.6": "pass"
      },
      "report_url": "reports/44999999999-win-rocm-10.1.json"
    }
  ]
}
```

### Field notes

- **`results`** has one entry per test: T0.1–T0.12 and T1.1–T1.6, as listed in
  [README.md](README.md). A test id added to the suite later appears here as
  soon as it runs, so the failure-frequency and heatmap views can be built from
  `index.json` alone. A retired test stops appearing in new rows and keeps its
  value in older ones; its id is never reused. The values:
  - `pass`: the check ran and passed.
  - `fail`: the check ran and found a problem. It blocks the archive only if
    the test is gating; T1.5 never blocks.
  - `warn`: the check ran and found something worth recording that does not
    block, e.g. upstream test programs shipped in the archive (T0.1).
  - `skip`: the check ran but could not apply, e.g. T0.4, T0.6 and T0.7 on
    Windows, and T0.11 on Linux, which bundles no ROCm.
  - `missing`: the check should have run for this archive and did not. It
    blocks the archive (see `qualified`).
  - `not_applicable`: the check was never going to run for this archive.
    T0.12 checks the final archive inside the release job, and only runs on
    archives still in the release at that point. An archive dropped before
    then — because the other archive of its ROCm version was missing — or
    never built has it `not_applicable`.
- **`tests`** counts the values in `results`, so
  `passed + failed + warned + skipped + missing + not_applicable == total`
  always holds. Use `qualified`, not the counts, to decide pass or fail.
- **`qualified`** is the verdict for this one archive: Tier 0 ran and no gating
  Tier 0 check failed, Tier 1 ran and no gating Tier 1 check failed, and the
  release job's final-archive check (T0.12) passed. A `warn` does not block,
  and non-gating tests never affect it. It does not say whether the archive
  shipped (`published`).
- **`version_qualified`** is `true` when both archives of this ROCm version —
  Windows **and** Linux — exist and qualified. A version ships only as a pair,
  so an archive can have `qualified: true` and `version_qualified: false`
  because its counterpart failed a check or never built. `null` means the run
  recorded no decision.
- **`published`** says whether this run attached this archive to the release
  `tag`. It can be `false` even when `version_qualified` is `true`: an upload
  can fail, and a nightly filling the gaps of an incomplete release never
  replaces an archive that release already holds, so it reports `false` for
  those. `published` describes what this run shipped, not what the release
  holds today.
- **`overall`** is `fail` exactly when `qualified` is `false`. Otherwise it is
  the worst result of the gating tests that ran: `warn` if any of them warned,
  else `pass`.
- **`tier0` / `tier1`** are each tier's result: `pass`, `warn` or `fail` when
  the tier ran, and `missing` when it did not.
- **`tag`** is the release tag this run pinned, `master-<upstream commit
  count>-<fork commit>`. Every row of a run has it, whether or not anything
  shipped. The asset names drop the count: `sd-master-<fork commit>-…`.
- **`build_result`** is how the archive's build job (`ubuntu-latest-rocm` or
  `windows-latest-rocm`) ended: `pass`, `fail` (including a timeout),
  `cancelled`, `skipped`, or `unknown` when the run's job list could not be
  read. Each build ends on its own: one ROCm version failing does not cancel
  the others.
- **`run_attempt`** is the attempt of the run that published the row. Rows of
  one run normally share it; a row kept from an earlier attempt that shipped
  its archive keeps that attempt's number. For the run's latest attempt, read
  `last_check.run_attempt`.
- **`generated_at`** is when the row's record was written. A re-run writes it
  again, later than any run that started after it, so order runs by `run_id`
  (rows are already newest-run-first) rather than by `generated_at`.
- **`blocked_by`** gives the reasons the archive did not qualify. It is empty
  when it qualified. The values:
  - `tier0=fail`: a gating Tier 0 check failed;
  - `tier0 missing`: no Tier 0 report;
  - `tier1=fail`: a gating Tier 1 check failed;
  - `tier1 missing`: no Tier 1 report, e.g. the GPU runner was offline;
  - `T0.12 missing`: the release job's final-archive check did not run on it;
  - on placeholder rows only: `build failed`, `build cancelled`, `build skipped`
    or `no report`.
- **`blocking_tests`** lists the gating tests that failed; empty when none did.
- **`gfx`** summarises the check that every gfx target was compiled into the
  GPU code (T0.3): how many were requested, how many are present, and the
  names of any that are missing.
- **`gfx_target`** is the gfx target Tier 1 ran on, and `null` when Tier 1 did
  not run. **`hardware_validated`** is `true` only when Tier 1 confirmed the GPU
  it ran on is that target (T1.3). Every other target in `gfx.requested` was
  only checked for presence, never run. Don't present those as tested.
- **`report_url`** is relative to the base URL; it holds every check's
  `status`, `gating`, `error` and `details`.

### Placeholder rows

An archive with no record at all gets a placeholder row. This happens when its
build failed, so there was nothing to check, or when no check reported on it.
In a placeholder row:

- every test is `missing` except T0.12, which is `not_applicable` (or
  `missing` in the rare case the archive reached the release job's final-archive
  check but the check itself could not run, e.g. its metadata was lost);
  `tier0` and `tier1` are `missing`; `qualified`, `version_qualified` and
  `published` are `false`; and `overall` is `fail`;
- `blocked_by` follows `build_result`: `build failed`, `build cancelled`,
  `build skipped`, or `no report` when the build passed or its result is
  `unknown`;
- `gfx` is all zero and empty: there is no binary, and the target list lives in
  the build job;
- `gfx_target` is `null` and `hardware_validated` is `false`; `tag` and
  `artifact` are the ones the archive would have had.

The first row of the [`index.json`](#example-indexjson) example is a placeholder.

### `last_check`

A top-level object, not a row: what the most recent nightly or release run
decided.

- **`status`** is `built` when the run built and qualified archives (its rows
  are in `builds`), and `unchanged` when a nightly found the release for its tag
  already holding every backend family and built nothing. The tag only changes
  when upstream stable-diffusion.cpp or this repository changes, so a run of
  `unchanged` nights means nothing new has landed upstream.
- **`tag`** and **`upstream_sha`** are what that run resolved — for an
  `unchanged` night, the release that was already complete.
- **`run_id`**, **`run_attempt`**, **`run_url`**, **`event`** identify the run;
  **`checked_at`** is when it published.

Like `latest/`, `last_check` never moves back: re-running an older run leaves a
newer run's `last_check` in place.

The freshest row in `builds` can therefore be days old while `last_check` is
from this morning; that is the normal state when upstream is quiet.

## `reports/<build_id>.json`

The `qualification-<archive>.json` record exactly as `aggregate.py` wrote it (`kind: "qualification_record"`):
- the `build` metadata (archive, `os`, `rocm_version`, `sdcpp_tag`,
  `upstream_sha`, `gfx_target`, `hardware_validated`, the run, and for Tier 1
  `runner_env`: the machine's driver versions, kernel, and any HIP runtime
  found in System32 or system ROCm),
- `overall`,
- `promoted`,
- `promotion.blocked_by` (why this archive did not qualify) and
  `promotion.version_blocked_by` (why its ROCm version was withheld because of
  the other archive, e.g. `["win: tier1=fail"]` or `["win: no report"]`, so an
  archive that passed its own checks but did not ship still says why),
- raw `counts`,
- the `gfx` audit, and
- `tiers` with every test.

The same content is attached to the release. Its own `build.build_id` (`<artifact>@<run_id>`) differs from the feed's `build_id`; use the feed's.

For a placeholder row it is a placeholder (`kind: "qualification_placeholder"`) with the same top-level fields:
- `build` metadata,
- `overall: "fail"`,
- `promoted: false`,
- `promotion.blocked_by`,
- `build_result` (in the feed's vocabulary),
- the empty `gfx` block,
- zero `counts` and
- empty `tiers`.

## `latest/<os>-rocm-<ver>.json`

The newest full record for one archive (for example `latest/win-rocm-10.1.json`),
for status tiles that want the current state without scanning the index. A
placeholder is written here too, so a tile shows a failing build instead of the
last good one. It is replaced only by a record from the same run or a newer one
(a higher `run_id`), so re-running an old run cannot move it backwards.

## Differences from the llama.cpp feeds

This feed has one archive per ROCm version, like lemonade-sdk/llama.cpp's, with
ROCm's math libraries installed separately by the user. Everything else in a row
means the same as there, except:

| Field | this repository | lemonade-sdk/llama.cpp | lemonade-sdk/llamacpp-rocm |
|---|---|---|---|
| `schema_version` | `2` | `2` | `1` (same row shape) |
| `build_id` | `<run_id>-<os>-rocm-<rocm_version>` | same | `<run_id>-<os>-rocm-<gfx_family>` |
| rows per run | 6 | 8 | 20 |
| `tag` | `master-<count>-<fork commit>`; set on every row | llama.cpp build number; set on every row | its own release tag; `null` unless `published` |
| `artifact` | the release asset name (`sd-master-<fork commit>-…`) | the release name | the untagged CI name unless `published` |
| `os` | `win` \| `ubuntu` | `win` \| `ubuntu` | `windows` \| `ubuntu` |
| `rocm_version` | `10.0` \| `10.1` \| `10.2` | `7.14` \| `10.0` \| `10.1` \| `10.2` | full nightly, e.g. `10.3.0a20261102` |
| `results` keys | T0.1–T0.12, T1.1–T1.6 — this repository's own ids | T0.1–T0.8, T0.11, T0.12, T1.1–T1.5 | T0.1–T0.3, T0.5–T0.9, T0.11, T0.13, T1.1–T1.6 |
| missing Tier 1 | blocks (`tier1 missing`) | does not block | blocks |
| final-archive check | T0.12; missing blocks | T0.11; missing does not block | T0.11; missing blocks |
| `hardware_validated` | `true` only when T1.3 confirmed the GPU | `true` whenever Tier 1 ran | `true` whenever Tier 1 ran |
| `last_check` | yes | — | — |
| placeholder `gfx` | all zero | all zero | requested targets, all in `missing` |

Test ids are per repository: the same id names different checks in different
feeds (T0.11 is the final-archive check in llama.cpp and the bundled HIP runtime
check here). Compare checks across repositories by name, not by id.

## Versioning and growth

Bump `schema_version` on any breaking change to `index.json` or the row shape.
Adding a field is not breaking (`last_check` was added without a bump). The
dashboard should read `schema_version` and show a notice, rather than break, on
a version it does not know.

The `schema_version` inside each record under `reports/` and `latest/` is a
separate number. It versions the record written by `aggregate.py` (`report.py`
sets it), not `index.json`, so the two do not have to match and change
independently.

`index.json` grows by six rows per run that builds, and by nothing on a night
when upstream did not change. That is comfortable to poll for a long time,
especially compressed. If it ever is not, older rows will move into per-month
files under a new `schema_version`.
