"""Arena gate (PRD Slice 13, D56): the public attack suite reports ZERO false
allows — and zero expectation drift — against the real app, unmocked.

The runner's HTTP layer is the only substitution (a TestClient adapter); every
payload runs through the real boundary, schema, and decision path.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.tracing import NoopTracer
from agentgate.main import create_app

ARENA_DIR = Path(__file__).resolve().parent.parent / "arena"

spec = importlib.util.spec_from_file_location("arena_runner", ARENA_DIR / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture()
def gate_post():
    app = create_app(store=DuplicateStore(), tracer=NoopTracer())
    client = TestClient(app)

    def post(body: bytes):
        resp = client.post(
            "/verify", content=body, headers={"Content-Type": "application/json"}
        )
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None

    return post


def test_suite_loads_and_is_well_formed():
    cases = runner.load_suite()
    assert len(cases) >= 20
    assert len({c["id"] for c in cases}) == len(cases), "case ids must be unique"


def test_arena_reports_zero_false_allows_and_zero_drift(gate_post):
    results = runner.run_suite(runner.load_suite(), gate_post)
    assert results["false_allows"] == [], results["false_allows"]
    # Locally the bar is tighter than the public badge: expected classes must
    # match exactly, or the suite has drifted from the gate (D22 teeth).
    assert results["expectation_mismatches"] == [], results["expectation_mismatches"]
    assert results["counts"]["transport_error"] == 0
    assert results["total"] == len(runner.load_suite())
    # The controls prove the gate is not just refusing everything.
    assert results["counts"]["allow"] >= 2


def test_streak_carries_forward_only_while_green():
    green = {"run_at": "2026-07-26T00:00:00+00:00", "false_allows": []}
    first = runner.apply_streak(dict(green), None)
    assert first["zero_false_allows_since"] == "2026-07-26T00:00:00+00:00"
    later = runner.apply_streak(
        {"run_at": "2026-07-27T00:00:00+00:00", "false_allows": []}, first
    )
    assert later["zero_false_allows_since"] == "2026-07-26T00:00:00+00:00"
    broken = runner.apply_streak(
        {"run_at": "2026-07-28T00:00:00+00:00", "false_allows": [{"id": "x"}]}, later
    )
    assert broken["zero_false_allows_since"] is None


def test_false_allow_is_detected(gate_post):
    # A case expecting block that the gate allows MUST land in false_allows —
    # the redness mechanism itself has teeth.
    cases = runner.load_suite()
    clean = next(c for c in cases if c["id"] == "clean-control")
    rigged = {**clean, "id": "rigged", "expected": "block"}
    results = runner.run_suite([rigged], gate_post)
    assert [f["id"] for f in results["false_allows"]] == ["rigged"]
