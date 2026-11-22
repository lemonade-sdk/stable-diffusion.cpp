#!/usr/bin/env python3
"""
Merge the tier fragments into one qualification record per ROCm archive and
decide which ROCm versions may be published.

A version is publishable only when both of its archives (Windows and Linux)
qualify. An archive qualifies when Tier 0 and Tier 1 both reported and neither
failed; a missing tier blocks it, so an offline GPU runner withholds ROCm
rather than shipping it untested. Versions are judged independently.

Usage:
    python3 aggregate.py --fragments-dir ./fragments --tag master-1106-abc1234 \
        --records-dir ./qualification --output-summary summary.json \
        --operating-systems ubuntu,win --rocm-versions 10.0,10.1,10.2 --fail-on-no-promote
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import report  # noqa: E402


def load_fragments(fragments_dir):
    fragments = []
    for path in sorted(glob.glob(os.path.join(fragments_dir, "*.json"))):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            print(f"::warning::skipping unreadable fragment {path}: {exc}")
            continue
        if data.get("kind") == "tier_fragment":
            fragments.append(data)
    return fragments


def split_list(text):
    return [name for part in (text or "").split(",") for name in part.split() if name]


def published_name(tag, os_name, rocm_version):
    """Release asset name; 'master-1106-abc1234' names its assets 'sd-master-abc1234-...'."""
    parts = (tag or "").split("-")
    if len(parts) != 3:
        return None
    prefix = f"sd-{parts[0]}-{parts[2]}"
    if os_name == "win":
        return f"{prefix}-bin-win-rocm-{rocm_version}.0-x64.zip"
    if os_name == "ubuntu":
        return f"{prefix}-bin-Linux-Ubuntu-24.04-x86_64-rocm-{rocm_version}.0.zip"
    return None


def decide(record, require_final=False):
    reasons = []
    tiers = record.get("tiers", {})
    tier0_ids = {t.get("id") for t in tiers.get("tier0", {}).get("tests", [])}
    for tier in ("tier0", "tier1"):
        # The release job's T0.12 fragment alone does not mean the Tier 0 job reported.
        if tier not in tiers or (tier == "tier0" and not tier0_ids - {"T0.12"}):
            reasons.append(f"{tier} missing")
        elif tiers[tier]["status"] == report.STATUS_FAIL:
            reasons.append(f"{tier}=fail")
    if require_final and "T0.12" not in tier0_ids:
        reasons.append("T0.12 missing")
    return not reasons, reasons


def gfx_summary(record):
    tests = record.get("tiers", {}).get("tier0", {}).get("tests", [])
    audit = next((t for t in tests if t.get("id") == "T0.3"), None)
    details = (audit or {}).get("details", {})
    build = record.get("build", {})
    validated = [build["gfx_target"]] if build.get("hardware_validated") and build.get("gfx_target") else []
    return {
        "requested": details.get("expected", []),
        "present_in_binary": details.get("present", []),
        "missing_from_binary": details.get("missing", []),
        "hardware_validated": validated,
    }


def write_step_summary(records, promote_all, missing=()):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## ROCm build qualification", "",
             f"**Every ROCm version publishable: {'YES' if promote_all else 'NO'}**", "",
             "| Artifact | Tier 0 | Tier 1 | gfx in binary | HW validated | Promote |",
             "|---|---|---|---|---|---|"]
    for record in records:
        tiers = record.get("tiers", {})
        gfx = record["gfx"]
        lines.append(
            f"| `{record['build'].get('artifact')}` "
            f"| {tiers.get('tier0', {}).get('status', 'missing').upper()} "
            f"| {tiers.get('tier1', {}).get('status', 'missing').upper()} "
            f"| {len(gfx['present_in_binary'])}/{len(gfx['requested'])} "
            f"| {', '.join(gfx['hardware_validated']) or 'none'} "
            f"| {'yes' if record['promoted'] else 'NO'} |")
    for os_name, rocm_version in missing:
        lines.append(f"| {os_name} ROCm {rocm_version} | no report | | | | NO |")
    problems = []
    for record in records:
        for tier in sorted(record.get("tiers", {})):
            for test in record["tiers"][tier].get("tests", []):
                if test["status"] in (report.STATUS_FAIL, report.STATUS_WARN):
                    gate = "" if test.get("gating") else " _(non-gating)_"
                    problems.append(f"- `{record['build'].get('artifact')}` `{test['id']}` "
                                    f"**{test['status']}**{gate} — {test['name']}: "
                                    f"{test.get('error') or ''}")
    if problems:
        lines += ["", "### Findings", *problems]
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description="Aggregate ROCm tier fragments")
    parser.add_argument("--fragments-dir", required=True)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--records-dir", default=None)
    parser.add_argument("--output-summary", default=None)
    parser.add_argument("--operating-systems", default=None)
    parser.add_argument("--rocm-versions", default=None, help="X.Y, e.g. 10.0,10.1,10.2")
    parser.add_argument("--require-final", action="store_true",
                        help="block an archive the release job's T0.12 check did not run on")
    parser.add_argument("--fail-on-no-promote", action="store_true")
    args = parser.parse_args()

    expected = [(o, v) for v in split_list(args.rocm_versions)
                for o in split_list(args.operating_systems)]
    fragments = load_fragments(args.fragments_dir)
    if not fragments:
        print(f"::error::No tier fragments in {args.fragments_dir}; nothing can be vouched for.")
        if not expected:
            sys.exit(1 if args.fail_on_no_promote else 0)

    grouped = {}
    for fragment in fragments:
        build = fragment.get("build", {})
        grouped.setdefault((build.get("os") or "?", build.get("rocm_version") or "?"), []).append(fragment)

    records = []
    for (os_name, rocm_version), group in sorted(grouped.items()):
        record = report.merge_fragments(group, extra_meta={
            "sdcpp_tag": args.tag,
            "artifact": published_name(args.tag, os_name, rocm_version),
        })
        promote, reasons = decide(record, args.require_final)
        record["promoted"] = promote
        record["promotion"] = {"blocked_by": reasons}
        if not promote:
            record["overall"] = report.STATUS_FAIL
        record["gfx"] = gfx_summary(record)
        records.append(record)

        artifact = record["build"].get("artifact") or f"{os_name}-rocm-{rocm_version}"
        if args.records_dir:
            os.makedirs(args.records_dir, exist_ok=True)
            out = os.path.join(args.records_dir, f"qualification-{artifact.removesuffix('.zip')}.json")
            with open(out, "w", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
            print(f"Wrote {out}")
        print(f"{artifact}: overall={record['overall']} promote={promote} blocked_by={reasons} "
              f"gfx={len(record['gfx']['present_in_binary'])}/{len(record['gfx']['requested'])} "
              f"hw_validated={record['gfx']['hardware_validated'] or 'none'}")

    by_version = {}
    for record in records:
        by_version.setdefault(record["build"].get("rocm_version") or "?", []).append(record)
    blocked = {v for v, group in by_version.items() if not all(r["promoted"] for r in group)}
    missing = [(o, v) for o, v in expected if (o, v) not in grouped]
    for os_name, rocm_version in missing:
        print(f"::error::No qualification report at all for {os_name} ROCm {rocm_version}.")
        blocked.add(rocm_version)
    blocked = sorted(blocked)
    promote_all = bool(records) and not blocked
    write_step_summary(records, promote_all, missing)

    if args.output_summary:
        os.makedirs(os.path.dirname(os.path.abspath(args.output_summary)), exist_ok=True)
        with open(args.output_summary, "w", encoding="utf-8") as handle:
            json.dump({
                "tag": args.tag,
                "all_versions_publishable": promote_all,
                "blocked_rocm_versions": blocked,
                "missing_archives": [f"{o}-rocm-{v}" for o, v in missing],
                "artifacts": [{"artifact": r["build"].get("artifact"),
                               "rocm_version": r["build"].get("rocm_version"),
                               "publishable": r["promoted"], "overall": r["overall"],
                               "blocked_by": r["promotion"]["blocked_by"]} for r in records],
            }, handle, indent=2)
            handle.write("\n")

    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"promote_rocm={'true' if promote_all else 'false'}\n")
            handle.write(f"blocked_rocm_versions={' '.join(blocked)}\n")
    if blocked:
        print(f"::error::ROCm {', '.join(blocked)} did not qualify and will be withheld. "
              "Other ROCm versions and every other backend are unaffected.")
    if args.fail_on_no_promote and blocked:
        sys.exit(1)


if __name__ == "__main__":
    main()
