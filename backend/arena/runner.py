#!/usr/bin/env python3
"""Adversarial arena runner (PRD Slice 13, D56).

Runs every payload in ``suite.jsonl`` against a live AgentGate ``POST /verify``
and asserts the expected decision CLASS. The output JSON is the public claim:
the scheduled workflow goes RED if ``false_allows`` is non-empty — a false
allow is a decision of ``allow`` on a case whose expected class is not allow.

Deliberately stdlib-only (urllib), so the cron job needs no installs. The
attack suite being public and in-repo is the mechanism, not a leak: bring your
own payloads — the win condition and its threat-model scope live on the /arena
page.

Usage:
    python backend/arena/runner.py --base-url https://agentgate-api.example.com \
        --output results.json [--previous prev-results.json] [--wait-health 300]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SUITE_PATH = Path(__file__).resolve().parent / "suite.jsonl"

PostFn = Callable[[bytes], tuple[int, Optional[dict]]]


def load_suite(path: Path = SUITE_PATH) -> list[dict]:
    cases = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        for field in ("id", "expected"):
            if field not in case:
                raise ValueError(f"suite line {line_number} is missing {field!r}")
        if case["expected"] not in {"allow", "block", "escalate"}:
            raise ValueError(f"case {case['id']!r} has invalid expected class")
        if ("request" in case) == ("raw_body" in case):
            raise ValueError(f"case {case['id']!r} needs exactly one of request | raw_body")
        cases.append(case)
    return cases


def http_post(base_url: str, timeout: float = 60.0) -> PostFn:
    url = f"{base_url.rstrip('/')}/verify"

    def post(body: bytes) -> tuple[int, Optional[dict]]:
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, None
        except (urllib.error.URLError, TimeoutError, ValueError):
            return 0, None

    return post


def wait_for_health(base_url: str, max_seconds: float) -> bool:
    """Poll /health until the deploy wakes (free-tier cold starts)."""
    deadline = time.monotonic() + max_seconds
    url = f"{base_url.rstrip('/')}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5)
    return False


def case_body(case: dict) -> bytes:
    if "raw_body" in case:
        return str(case["raw_body"]).encode("utf-8")
    return json.dumps(case["request"]).encode("utf-8")


def run_suite(cases: list[dict], post: PostFn) -> dict[str, Any]:
    counts = {"allow": 0, "block": 0, "escalate": 0, "transport_error": 0}
    false_allows: list[dict] = []
    mismatches: list[dict] = []
    rows: list[dict] = []

    for case in cases:
        status, payload = post(case_body(case))
        got = payload.get("decision") if isinstance(payload, dict) else None
        if got in counts:
            counts[got] += 1
        else:
            counts["transport_error"] += 1
            got = f"transport_error(status={status})"
        row = {"id": case["id"], "expected": case["expected"], "got": got}
        rows.append(row)
        if got == "allow" and case["expected"] != "allow":
            false_allows.append({**row, "note": case.get("note", "")})
        elif got != case["expected"]:
            mismatches.append(row)

    return {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "total": len(cases),
        "counts": counts,
        "false_allows": false_allows,
        "expectation_mismatches": mismatches,
        "cases": rows,
    }


def apply_streak(results: dict, previous: Optional[dict]) -> dict:
    """`zero_false_allows_since` carries forward across green runs and resets
    to None the moment a run has a false allow — the /arena page's since-date."""
    if results["false_allows"]:
        results["zero_false_allows_since"] = None
    elif previous and previous.get("zero_false_allows_since"):
        results["zero_false_allows_since"] = previous["zero_false_allows_since"]
    else:
        results["zero_false_allows_since"] = results["run_at"]
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--suite", type=Path, default=SUITE_PATH)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--wait-health", type=float, default=0)
    args = parser.parse_args()

    if args.wait_health and not wait_for_health(args.base_url, args.wait_health):
        print(f"ERROR: {args.base_url}/health did not come up in {args.wait_health}s", file=sys.stderr)
        return 2

    previous = None
    if args.previous and args.previous.is_file():
        try:
            previous = json.loads(args.previous.read_text(encoding="utf-8"))
        except ValueError:
            previous = None

    results = apply_streak(run_suite(load_suite(args.suite), http_post(args.base_url)), previous)

    output = json.dumps(results, indent=2, ensure_ascii=False)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)

    if results["counts"]["transport_error"]:
        print(f"ERROR: {results['counts']['transport_error']} transport errors", file=sys.stderr)
        return 2
    if results["false_allows"]:
        print(f"FALSE ALLOWS: {[f['id'] for f in results['false_allows']]}", file=sys.stderr)
        return 1
    if results["expectation_mismatches"]:
        # Suite drift is visible but does not redden the public badge — the
        # badge's claim is exactly "zero false allows" (D56).
        print(
            f"note: expectation mismatches (suite drift): "
            f"{[m['id'] for m in results['expectation_mismatches']]}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
