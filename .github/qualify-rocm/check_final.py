#!/usr/bin/env python3
"""
Final-archive check on the exact bytes about to be uploaded. No GPU required.

Runs inside the release job, after the family completeness check, which is the
only place this question can be answered:

  T0.12  final archive integrity -- the final archive opens, reads back intact
         (every entry's CRC-32 is checked), still contains sd-server and the
         file holding the GPU code, and is the archive the build job produced
         and the qualify jobs tested: its SHA-256 must match the one the build
         job recorded in qualify-meta-<os>-rocm-<ver>.json.

This is what makes "what is built is what is tested" hold. Tier 0 and Tier 1
download the build job's artifact by name; the release job downloads it again
and uploads it under the same name, never repacking or merging anything into
it. If any step in between ever changed the bytes -- a re-pack, a merge, a
corrupted transfer -- the SHA-256 would no longer match and the archive's ROCm
version would be withheld. (lemonade-sdk/llama.cpp calls the same check T0.11,
in check_post_merge.py.)

The file is opened inside the check, so an archive that cannot be read is
recorded as a FAIL instead of crashing the script and leaving no result.

Usage:
    python3 check_final.py --archive artifact/sd-master-abc1234-bin-win-rocm-10.1.0-x64.zip \
        --meta meta/qualify-meta-win-rocm-10.1.json --output frag.json
"""

import argparse
import hashlib
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import archive  # noqa: E402
import report  # noqa: E402
import tier0_static  # noqa: E402


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_integrity(path, os_name, expected_sha):
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"{bad} fails its CRC check")
        names = [os.path.basename(i.filename) for i in zf.infolist() if not i.is_dir()]
    required = [archive.SERVER[os_name], tier0_static.HIP_CODE[os_name]]
    missing = [r for r in required if r not in names]
    actual = sha256(path)
    details = {"entries": len(names), "missing": missing, "bytes": os.path.getsize(path),
               "sha256": actual, "expected_sha256": expected_sha}
    if missing:
        return (report.STATUS_FAIL, f"final archive is missing {', '.join(missing)}", details)
    if not expected_sha:
        return (report.STATUS_FAIL, "the build job recorded no SHA-256 for this archive", details)
    if actual != expected_sha.lower():
        return (report.STATUS_FAIL, "this is not the archive the build job produced and "
                "Tier 0/Tier 1 tested (SHA-256 mismatch)", details)
    return (report.STATUS_PASS, None, details)


def main():
    parser = argparse.ArgumentParser(description="Final release-archive check (T0.12)")
    parser.add_argument("--archive", required=True)
    parser.add_argument("--meta", required=True)
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    parser.add_argument("--run-attempt", default=os.environ.get("GITHUB_RUN_ATTEMPT"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.meta, encoding="utf-8-sig") as handle:
        info = json.load(handle)
    tier = report.TierReport("tier0", report.build_meta(
        artifact=os.path.basename(args.archive), os_name=info["os"],
        rocm_version=info["rocm_version"], sdcpp_tag=info.get("tag"),
        upstream_sha=info.get("upstream_sha"), run_id=args.run_id, run_attempt=args.run_attempt))
    tier.run("T0.12", "final archive integrity",
             lambda: check_integrity(args.archive, info["os"], info.get("sha256")))
    tier.write(args.output)
    tier.print_summary()


if __name__ == "__main__":
    main()
