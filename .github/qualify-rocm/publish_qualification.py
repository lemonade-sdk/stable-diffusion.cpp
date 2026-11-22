#!/usr/bin/env python3
"""
Publish one run's qualification records to the `qualification-data` branch,
the feed the ROCm readiness dashboard reads. FEED.md is the contract.

  index.json                    one row per ROCm archive per run, newest run first
  reports/<build_id>.json       the full record
  latest/<os>-rocm-<ver>.json   the newest full record for that archive

build_id is "<run_id>-<os>-rocm-<rocm_version>"; re-publishing a run replaces
its rows. An expected archive with no record gets a placeholder row.

Usage:
    python3 publish_qualification.py --records-dir ./records --data-dir ./data/qualification \
        --operating-systems ubuntu,win --rocm-versions 10.0,10.1,10.2 --tag master-1106-abc1234 \
        --jobs-file jobs.jsonl --published-artifacts "<names>" --final-checked "<names>"
"""

import argparse
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aggregate  # noqa: E402
import report  # noqa: E402

SCHEMA_VERSION = 2

TIER_TESTS = {
    "tier0": ["T0.1", "T0.2", "T0.3", "T0.4", "T0.5", "T0.6", "T0.7", "T0.8",
              "T0.9", "T0.10", "T0.11", "T0.12"],
    "tier1": ["T1.1", "T1.2", "T1.3", "T1.4", "T1.5"],
}
# Run by the release job only on archives still in the release after its completeness check.
FINAL_TESTS = {"T0.12"}

STATUS_MISSING = "missing"
STATUS_NOT_APPLICABLE = "not_applicable"
SUMMARY_FILE = "qualification-summary.json"

BUILD_PASS = "pass"
BUILD_FAIL = "fail"
BUILD_UNKNOWN = "unknown"
BUILD_RESULTS = {"success": BUILD_PASS, "cancelled": "cancelled", "skipped": "skipped"}
PLACEHOLDER_REASONS = {BUILD_FAIL: "build failed", "cancelled": "build cancelled",
                       "skipped": "build skipped"}
PLACEHOLDER_NO_REPORT = "no report"

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# GitHub names a matrix job after its matrix values, ROCM_VERSION first:
# "windows-latest-rocm (10.1.0, gfx1010;...)".
_BUILD_JOBS = {"ubuntu-latest-rocm": "ubuntu", "windows-latest-rocm": "win"}
_BUILD_JOB = re.compile(r"^(?P<job>[a-z0-9-]+) \((?P<first>[^,)]+)")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_order(run_id):
    number = _int(run_id)
    return number if number is not None else -1


def load_records(records_dir):
    records, summary = [], None
    for path in sorted(glob.glob(os.path.join(records_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"::warning::skipping unreadable record {path}: {exc}")
            continue
        if os.path.basename(path) == SUMMARY_FILE:
            summary = data
        elif data.get("kind") == "qualification_record":
            records.append(data)
    return records, summary


def load_build_results(jobs_file):
    """Map (os, X.Y) -> result of its build job, from `gh api .../jobs` lines."""
    results = {}
    if not jobs_file:
        return results
    try:
        with open(jobs_file, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        print(f"::warning::could not read {jobs_file}: {exc}")
        return results
    for line in filter(None, (l.strip() for l in lines)):
        try:
            job = json.loads(line)
        except ValueError:
            continue
        match = _BUILD_JOB.match(job.get("name") or "")
        if not match or match.group("job") not in _BUILD_JOBS:
            continue
        version = report.short_version(match.group("first").strip())
        conclusion = job.get("conclusion")
        results[(_BUILD_JOBS[match.group("job")], version)] = (
            BUILD_RESULTS.get(conclusion, BUILD_FAIL) if conclusion else BUILD_UNKNOWN)
    return results


def make_build_id(run_id, os_name, rocm_version):
    if not (run_id and os_name and rocm_version):
        raise SystemExit(f"::error::cannot form a build_id from run_id={run_id!r} "
                         f"os={os_name!r} rocm_version={rocm_version!r}")
    build_id = f"{run_id}-{os_name}-rocm-{rocm_version}"
    if not _SAFE_ID.match(build_id):
        raise SystemExit(f"::error::refusing unsafe build_id {build_id!r}")
    return build_id


def collect_results(record, final_checked):
    by_id = {}
    for tier in record.get("tiers", {}).values():
        for test in tier.get("tests", []):
            by_id.setdefault(test.get("id"), test)

    def absent(test_id):
        if test_id in FINAL_TESTS and record.get("build", {}).get("artifact") not in final_checked:
            return STATUS_NOT_APPLICABLE
        return STATUS_MISSING

    results = {test_id: by_id[test_id]["status"] if test_id in by_id else absent(test_id)
               for ids in TIER_TESTS.values() for test_id in ids}
    for test_id in sorted((t for t in by_id if t not in results), key=report.test_order):
        results[test_id] = by_id[test_id]["status"]
    blocking = sorted((t for t, test in by_id.items()
                       if test.get("gating") and test.get("status") == report.STATUS_FAIL),
                      key=report.test_order)
    return results, blocking


def make_placeholder(os_name, rocm_version, build_result, tag, upstream_sha,
                     run_id, run_attempt, generated_at):
    build = report.build_meta(
        artifact=aggregate.published_name(tag, os_name, rocm_version), os_name=os_name,
        rocm_version=rocm_version, sdcpp_tag=tag, upstream_sha=upstream_sha, run_id=run_id,
        run_attempt=str(run_attempt) if run_attempt is not None else None)
    return {
        "schema_version": report.SCHEMA_VERSION,
        "kind": "qualification_placeholder",
        "generated_at": generated_at,
        "build": build,
        "overall": report.STATUS_FAIL,
        "promoted": False,
        "counts": {report.STATUS_PASS: 0, report.STATUS_FAIL: 0, report.STATUS_WARN: 0,
                   report.STATUS_SKIP: 0, "total": 0},
        "tiers": {},
        "promotion": {"blocked_by": [PLACEHOLDER_REASONS.get(build_result, PLACEHOLDER_NO_REPORT)]},
        "build_result": build_result,
        "gfx": {"requested": [], "present_in_binary": [], "missing_from_binary": [],
                "hardware_validated": []},
    }


def tier_status(record, tier):
    reported = record.get("tiers", {}).get(tier)
    return reported.get("status") if reported else STATUS_MISSING


def build_entry(record, build_id, build_result, version_qualified, published,
                final_checked, run_url, event, run_attempt):
    build = record.get("build", {})
    results, blocking = collect_results(record, final_checked)
    statuses = list(results.values())
    gfx = record.get("gfx", {})
    return {
        "build_id": build_id,
        "tag": build.get("sdcpp_tag"),
        "upstream_sha": build.get("upstream_sha"),
        "artifact": build.get("artifact"),
        "os": build.get("os"),
        "rocm_version": build.get("rocm_version"),
        "gfx_target": build.get("gfx_target"),
        "hardware_validated": bool(build.get("hardware_validated")),
        "gfx": {"requested": len(gfx.get("requested", [])),
                "present": len(gfx.get("present_in_binary", [])),
                "missing": gfx.get("missing_from_binary", [])},
        "generated_at": record.get("generated_at"),
        "run_id": build.get("run_id"),
        "run_attempt": run_attempt if run_attempt is not None else _int(build.get("run_attempt")),
        "run_url": run_url,
        "event": event,
        "build_result": build_result,
        "tests": {
            "passed": statuses.count(report.STATUS_PASS),
            "failed": statuses.count(report.STATUS_FAIL),
            "warned": statuses.count(report.STATUS_WARN),
            "skipped": statuses.count(report.STATUS_SKIP),
            "missing": statuses.count(STATUS_MISSING),
            "not_applicable": statuses.count(STATUS_NOT_APPLICABLE),
            "total": len(statuses),
        },
        "qualified": bool(record.get("promoted")),
        "version_qualified": version_qualified,
        "published": published,
        "overall": record.get("overall"),
        "tier0": tier_status(record, "tier0"),
        "tier1": tier_status(record, "tier1"),
        "blocked_by": record.get("promotion", {}).get("blocked_by", []),
        "blocking_tests": blocking,
        "results": results,
        "report_url": f"reports/{build_id}.json",
    }


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def start_fresh_if_older(data_dir):
    index_path = os.path.join(data_dir, "index.json")
    if not os.path.exists(index_path):
        return
    with open(index_path, encoding="utf-8") as handle:
        version = json.load(handle).get("schema_version")
    if (_int(version) or 0) >= SCHEMA_VERSION:
        return
    print(f"The feed holds schema_version {version}; starting a fresh one.")
    os.remove(index_path)
    for folder in ("reports", "latest"):
        shutil.rmtree(os.path.join(data_dir, folder), ignore_errors=True)


def update_latest(path, record):
    """Never roll latest/ back to an older run."""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                current = json.load(handle).get("build", {}).get("run_id")
        except (OSError, ValueError, AttributeError):
            current = None
        if _run_order(current) > _run_order(record.get("build", {}).get("run_id")):
            return False
    write_json(path, record)
    return True


def upsert_index(index_path, repo, entries):
    index = {}
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as handle:
            index = json.load(handle)
    replaced = {entry["build_id"] for entry in entries}
    builds = [b for b in index.get("builds", []) if b.get("build_id") not in replaced]
    builds.extend(entries)
    builds.sort(key=lambda b: b.get("build_id") or "")
    builds.sort(key=lambda b: _run_order(b.get("run_id")), reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo or index.get("repo"),
        "updated_at": max((b.get("generated_at") or "" for b in builds), default="") or None,
        "builds": builds,
    }


def main():
    parser = argparse.ArgumentParser(description="Publish qualification records to the feed")
    parser.add_argument("--records-dir", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--operating-systems", required=True)
    parser.add_argument("--rocm-versions", required=True, help="X.Y, e.g. 10.0,10.1,10.2")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--upstream-sha")
    parser.add_argument("--jobs-file")
    parser.add_argument("--published-artifacts", default="",
                        help="ROCm archives this run uploaded, space separated")
    parser.add_argument("--final-checked", default="",
                        help="ROCm archives T0.12 ran on, space separated")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--server-url", default=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    args = parser.parse_args()

    expected = [(o, v) for v in aggregate.split_list(args.rocm_versions)
                for o in aggregate.split_list(args.operating_systems)]
    published_artifacts = set(args.published_artifacts.split())
    final_checked = set(args.final_checked.split())
    run_attempt = _int(args.run_attempt)

    records, summary = load_records(args.records_dir)
    if summary is None:
        print(f"::warning::no {SUMMARY_FILE} in {args.records_dir}; version_qualified will be null")
    blocked_versions = set((summary or {}).get("blocked_rocm_versions") or [])
    build_results = load_build_results(args.jobs_file)
    run_time = max((r.get("generated_at") or "" for r in records), default="") or report.now_iso()

    covered, items = set(), []
    for record in records:
        build = record.get("build", {})
        key = (build.get("os"), build.get("rocm_version"))
        covered.add(key)
        items.append((record, make_build_id(build.get("run_id") or args.run_id, *key), False))
    for os_name, rocm_version in expected:
        if (os_name, rocm_version) in covered:
            continue
        build_result = build_results.get((os_name, rocm_version), BUILD_UNKNOWN)
        record = make_placeholder(os_name, rocm_version, build_result, args.tag, args.upstream_sha,
                                  args.run_id, run_attempt, run_time)
        items.append((record, make_build_id(args.run_id, os_name, rocm_version), True))
    if not items:
        raise SystemExit("::error::no qualification records and no expected archives; nothing to publish")

    start_fresh_if_older(args.data_dir)
    os.makedirs(os.path.join(args.data_dir, "reports"), exist_ok=True)
    os.makedirs(os.path.join(args.data_dir, "latest"), exist_ok=True)

    # A re-run of the release job finds its archives already uploaded; keep what the earlier attempt published.
    index_path = os.path.join(args.data_dir, "index.json")
    prior_published = set()
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as handle:
            prior_published = {b.get("build_id") for b in json.load(handle).get("builds", []) if b.get("published")}

    entries = []
    for record, build_id, is_placeholder in items:
        build = record["build"]
        run_url = (f"{args.server_url}/{args.repo}/actions/runs/{build.get('run_id') or args.run_id}"
                   if args.repo else None)
        build_result = build_results.get((build.get("os"), build.get("rocm_version")), BUILD_UNKNOWN)
        if is_placeholder:
            version_qualified = False
        elif summary is None:
            version_qualified = None
        else:
            version_qualified = build.get("rocm_version") not in blocked_versions
        published = not is_placeholder and (build.get("artifact") in published_artifacts
                                            or build_id in prior_published)
        entry = build_entry(record, build_id, build_result, version_qualified, published,
                            final_checked, run_url, args.event, run_attempt)
        entries.append(entry)
        write_json(os.path.join(args.data_dir, "reports", f"{build_id}.json"), record)
        moved = update_latest(os.path.join(args.data_dir, "latest",
                                           f"{build['os']}-rocm-{build['rocm_version']}.json"), record)
        tests = entry["tests"]
        print(f"{build_id}: {tests['passed']}/{tests['total']} passed, {tests['failed']} failed, "
              f"{tests['missing']} missing, build={build_result} qualified={entry['qualified']} "
              f"version_qualified={entry['version_qualified']} published={entry['published']}"
              f"{'' if moved else ' (latest/ kept: it holds a newer run)'}")

    index = upsert_index(index_path, args.repo, entries)
    write_json(index_path, index)
    print(f"Published {len(entries)} row(s); {len(index['builds'])} build(s) in the index")


if __name__ == "__main__":
    main()
