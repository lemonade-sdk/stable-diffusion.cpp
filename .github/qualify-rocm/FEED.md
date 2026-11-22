# Qualification feed

The ROCm qualification results of every nightly and `create_release` run of
lemonade-sdk/stable-diffusion.cpp. Written by `publish_qualification.py`; this
file is copied to the branch as `SCHEMA.md`. Same shape as
lemonade-sdk/llama.cpp's feed, so one dashboard reads both.

## Layout

```
qualification/
  index.json                         one row per ROCm archive per run, newest run first
  reports/<build_id>.json            the full qualification record of that row
  latest/<os>-rocm-<rocm_version>.json   the newest full record for that archive
  SCHEMA.md                          this file
```

`build_id` is `<run_id>-<os>-rocm-<rocm_version>`, e.g. `44999-win-rocm-10.1`.
Re-publishing a run (a re-run) replaces its rows. Every archive the run was
meant to build gets a row; one with no record (its build failed, or no check
reported) gets a placeholder row whose `blocked_by` says why.

## `index.json`

```json
{
  "schema_version": 2,
  "repo": "lemonade-sdk/stable-diffusion.cpp",
  "updated_at": "2026-11-22T12:00:00Z",
  "builds": [ { "...": "one row, see below" } ]
}
```

| Field | Meaning |
|---|---|
| `build_id` | `<run_id>-<os>-rocm-<rocm_version>` |
| `tag` | release tag, `master-<upstream commit count>-<fork commit>` |
| `upstream_sha` | leejet/stable-diffusion.cpp commit the run built |
| `artifact` | release asset name, e.g. `sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip` |
| `os` | `win` \| `ubuntu` |
| `rocm_version` | `10.0` \| `10.1` \| `10.2` |
| `gfx_target` | gfx target Tier 1 ran on, or null |
| `hardware_validated` | Tier 1 confirmed it ran on `gfx_target` |
| `gfx` | `{requested, present}` target counts from T0.3 and the `missing` targets |
| `generated_at` | ISO-8601 UTC time the record was made |
| `run_id`, `run_attempt`, `run_url`, `event` | the workflow run (`run_id` is a string, `run_attempt` an int) |
| `build_result` | `pass` \| `fail` \| `cancelled` \| `skipped` \| `unknown` for the archive's build job |
| `tests` | counts of `passed`, `failed`, `warned`, `skipped`, `missing`, `not_applicable`, `total` over `results` |
| `qualified` | this archive passed both tiers |
| `version_qualified` | both archives of this ROCm version qualified (null if unknown) |
| `published` | this run uploaded the archive to the release |
| `overall` | `pass` \| `warn` \| `fail`; `fail` whenever not qualified |
| `tier0`, `tier1` | `pass` \| `warn` \| `fail` \| `missing` |
| `blocked_by` | e.g. `tier0=fail`, `tier1 missing`, `build failed`, `no report` |
| `blocking_tests` | gating test ids that failed |
| `results` | status of every test id: `pass` \| `fail` \| `warn` \| `skip` \| `missing` \| `not_applicable` |
| `report_url` | `reports/<build_id>.json`, relative to this folder |

Test ids are this repository's own (T0.1-T0.12, T1.1-T1.5, see README.md) and
do not share meaning with lemonade-sdk/llama.cpp's ids. `not_applicable` is
used for T0.12 on an archive dropped before the release job checked it.

## `reports/<build_id>.json` and `latest/`

The full record: `build` (archive, os, `rocm_version`, `sdcpp_tag`,
`upstream_sha`, `gfx_target`, `hardware_validated`, run), `overall`,
`promoted`, `promotion.blocked_by`, `counts`, `gfx`, and `tiers.tier0` /
`tiers.tier1` with every test's `id`, `name`, `status`, `gating`, `error` and
`details`. Placeholders have `kind: "qualification_placeholder"` and empty
`tiers`. `latest/` is never rolled back to an older run.
