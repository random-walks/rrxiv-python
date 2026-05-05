"""Tests for `rrxiv doctor`."""

from __future__ import annotations

from rrxiv.doctor import (
    CheckResult,
    overall_status,
    run_doctor,
)


def test_run_doctor_returns_results() -> None:
    results = run_doctor()
    assert len(results) > 0
    for r in results:
        assert isinstance(r, CheckResult)
        assert r.status in ("pass", "fail", "warn")


def test_overall_pass_when_all_pass() -> None:
    results = [
        CheckResult(name="x", status="pass"),
        CheckResult(name="y", status="pass"),
    ]
    assert overall_status(results) == "pass"


def test_overall_warn_when_any_warn_no_fail() -> None:
    results = [
        CheckResult(name="x", status="pass"),
        CheckResult(name="y", status="warn"),
    ]
    assert overall_status(results) == "warn"


def test_overall_fail_when_any_fail() -> None:
    results = [
        CheckResult(name="x", status="pass"),
        CheckResult(name="y", status="warn"),
        CheckResult(name="z", status="fail"),
    ]
    assert overall_status(results) == "fail"


def test_smoke_doctor_runs_clean() -> None:
    """Running doctor in the dev environment shouldn't FAIL.

    LaTeX engine may be PASS or WARN depending on the dev machine; we
    accept both. The other checks must PASS.
    """
    results = run_doctor()
    by_name = {r.name: r for r in results}
    assert by_name["rrxiv package importable"].status == "pass"
    assert by_name["vendored schemas present"].status == "pass"
    assert by_name["schemas are parseable JSON"].status == "pass"
    assert by_name["generated models importable"].status == "pass"
    assert by_name["CIR schema version matches package"].status in ("pass", "warn")
    assert by_name["LaTeX engine on PATH"].status in ("pass", "warn")


def test_check_result_render_format() -> None:
    r = CheckResult(name="thing", status="pass", detail="all good")
    assert "[OK]" in r.render()
    assert "thing" in r.render()
    assert "all good" in r.render()

    r2 = CheckResult(name="thing", status="fail", detail="broken")
    assert "[FAIL]" in r2.render()


def test_overall_empty_results() -> None:
    """Edge case: no checks at all → overall pass."""
    assert overall_status([]) == "pass"
