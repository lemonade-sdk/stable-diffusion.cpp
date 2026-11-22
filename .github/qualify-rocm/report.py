#!/usr/bin/env python3
"""
Shared reporting for the ROCm qualification suite.

Each tier writes a JSON fragment; aggregate.py merges the fragments for one
archive into the qualification record attached to the release. A test id keeps
its meaning forever and is never reused.
"""

import datetime
import json
import os
import re
import sys
import time
import traceback

SCHEMA_VERSION = 1

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_SKIP = "skip"

_SEVERITY = {STATUS_SKIP: 0, STATUS_PASS: 1, STATUS_WARN: 2, STATUS_FAIL: 3}

TIER_NAMES = {
    "tier0": "Static artifact verification",
    "tier1": "GPU functional inference",
}


def now_iso():
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def short_version(version):
    """'10.1.0' -> '10.1', the form records and the feed use."""
    return ".".join(str(version).split(".")[:2]) if version else version


def test_order(test_id):
    """Sort "T0.10" after "T0.9", and unknown ids last."""
    match = re.match(r"^T(\d+)\.(\d+)$", test_id or "")
    if match:
        return (int(match.group(1)), int(match.group(2)), "")
    return (sys.maxsize, sys.maxsize, test_id or "")


def build_meta(
    artifact,
    os_name=None,
    rocm_version=None,
    sdcpp_tag=None,
    upstream_sha=None,
    gfx_target=None,
    hardware_validated=False,
    run_id=None,
    run_attempt=None,
):
    build_id = artifact or "local"
    if run_id:
        build_id = f"{build_id}@{run_id}"
    return {
        "build_id": build_id,
        "artifact": artifact,
        "os": os_name,
        "rocm_version": short_version(rocm_version),
        "sdcpp_tag": sdcpp_tag,
        "upstream_sha": upstream_sha,
        "gfx_target": gfx_target,
        "hardware_validated": bool(hardware_validated),
        "run_id": run_id,
        "run_attempt": run_attempt,
    }


class TierReport:
    def __init__(self, tier, meta):
        if tier not in TIER_NAMES:
            raise ValueError(f"unknown tier {tier!r}")
        self.tier = tier
        self.meta = meta
        self.tests = []
        self._started = time.monotonic()
        self._generated_at = now_iso()

    def add(self, test_id, name, status, gating=True, duration_ms=None,
            error=None, details=None):
        if status not in _SEVERITY:
            raise ValueError(f"invalid status {status!r}")
        self.tests.append({
            "id": test_id,
            "name": name,
            "tier": self.tier,
            "status": status,
            "gating": bool(gating),
            "duration_ms": duration_ms,
            "error": error,
            "details": details or {},
        })
        return status

    def run(self, test_id, name, fn, gating=True):
        """Run a check; `fn` returns status, (status, error) or (status, error, details).

        An exception is recorded as FAIL so one broken check never hides the others.
        """
        start = time.monotonic()
        try:
            result = fn()
            if isinstance(result, str):
                status, error, details = result, None, None
            elif len(result) == 2:
                (status, error), details = result, None
            else:
                status, error, details = result
        except Exception as exc:  # noqa: BLE001
            status = STATUS_FAIL
            error = f"{type(exc).__name__}: {exc}"
            details = {"traceback": traceback.format_exc()}
        duration_ms = int((time.monotonic() - start) * 1000)
        return self.add(test_id, name, status, gating=gating,
                        duration_ms=duration_ms, error=error, details=details)

    def status_of(self, test_id):
        return next((t["status"] for t in self.tests if t["id"] == test_id), None)

    def counts(self):
        out = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_WARN: 0, STATUS_SKIP: 0,
               "total": len(self.tests)}
        for test in self.tests:
            out[test["status"]] += 1
        return out

    def rollup_status(self):
        """Worst status among gating tests."""
        worst = STATUS_PASS
        for test in self.tests:
            if test["gating"] and _SEVERITY[test["status"]] > _SEVERITY[worst]:
                worst = test["status"]
        return worst

    def to_dict(self):
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "tier_fragment",
            "tier": self.tier,
            "tier_name": TIER_NAMES[self.tier],
            "status": self.rollup_status(),
            "generated_at": self._generated_at,
            "duration_ms": int((time.monotonic() - self._started) * 1000),
            "build": self.meta,
            "counts": self.counts(),
            "tests": sorted(self.tests, key=lambda t: test_order(t["id"])),
        }

    def write(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2)
            handle.write("\n")

    def print_summary(self):
        print(f"\n=== {self.tier} ({TIER_NAMES[self.tier]}): {self.rollup_status().upper()} ===")
        for test in sorted(self.tests, key=lambda t: test_order(t["id"])):
            gate = "" if test["gating"] else " (non-gating)"
            line = f"  [{test['status'].upper()}] {test['id']} {test['name']}{gate}"
            if test["error"]:
                line += f" -- {test['error']}"
            print(line)
        counts = self.counts()
        print(f"  totals: {counts[STATUS_PASS]} pass, {counts[STATUS_FAIL]} fail, "
              f"{counts[STATUS_WARN]} warn, {counts[STATUS_SKIP]} skip")


def merge_fragments(fragments, extra_meta=None):
    """Merge the per-tier fragments of one archive into one record."""
    fragments = sorted(fragments, key=lambda frag: frag.get("tier", ""))
    meta = {}
    for frag in fragments:
        for key, value in frag.get("build", {}).items():
            if key == "hardware_validated":
                meta[key] = bool(meta.get(key)) or bool(value)
            elif value is not None or key not in meta:
                meta[key] = value
    if extra_meta:
        meta.update({k: v for k, v in extra_meta.items() if v is not None})

    tiers = {}
    for frag in fragments:
        bucket = tiers.setdefault(frag["tier"], {
            "status": STATUS_SKIP,
            "tier_name": frag.get("tier_name"),
            "counts": {},
            "tests": [],
        })
        seen = {t.get("id") for t in bucket["tests"]}
        for test in frag.get("tests", []):
            if test.get("id") in seen:
                continue
            bucket["tests"].append(test)
            seen.add(test.get("id"))
    for bucket in tiers.values():
        bucket["tests"].sort(key=lambda test: test_order(test.get("id")))

    overall = STATUS_PASS
    totals = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_WARN: 0, STATUS_SKIP: 0, "total": 0}
    for bucket in tiers.values():
        counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_WARN: 0, STATUS_SKIP: 0,
                  "total": len(bucket["tests"])}
        status = STATUS_PASS
        for test in bucket["tests"]:
            counts[test["status"]] += 1
            if test.get("gating") and _SEVERITY[test["status"]] > _SEVERITY[status]:
                status = test["status"]
        bucket["counts"] = counts
        bucket["status"] = status
        if _SEVERITY[status] > _SEVERITY[overall]:
            overall = status
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "qualification_record",
        "generated_at": now_iso(),
        "build": meta,
        "overall": overall,
        "promoted": False,
        "counts": totals,
        "tiers": tiers,
    }
