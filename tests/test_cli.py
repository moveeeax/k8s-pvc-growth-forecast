"""End-to-end CLI behaviour: output formats and exit codes."""

from __future__ import annotations

import json
import os

import pytest

from pvcforecast.cli import main

TESTDATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")
RANGE = os.path.join(TESTDATA, "range.json")


def run(capsys, *args):
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_table_output_and_findings_exit_code(capsys):
    code, out, err = run(capsys, "--fixture", RANGE)
    assert code == 1
    assert "postgres-data" in out
    assert "warning" in out
    assert "--fail-on warning" in err


def test_fail_on_never_is_always_clean(capsys):
    code, out, _ = run(capsys, "--fixture", RANGE, "--fail-on", "never")
    assert code == 0
    assert "STATUS" in out


def test_fail_on_critical_ignores_warnings(capsys):
    code, _, _ = run(capsys, "--fixture", RANGE, "--fail-on", "critical")
    assert code == 0
    code, _, _ = run(capsys, "--fixture", RANGE, "--fail-on", "critical", "--critical-days", "12")
    assert code == 1


def test_json_output_is_machine_readable(capsys):
    code, out, _ = run(capsys, "--fixture", RANGE, "--format", "json", "--fail-on", "never")
    assert code == 0
    payload = json.loads(out)
    assert payload["summary"]["warning"] == 1
    assert payload["summary"]["stable"] == 2
    first = payload["volumes"][0]
    assert first["persistentvolumeclaim"] == "postgres-data"
    assert first["days_to_full"] == pytest.approx(10.0, abs=0.5)
    assert first["capacity_bytes"] == 100 * 1024 ** 3
    assert 0.99 < first["r2"] <= 1.0


def test_markdown_output_is_a_table(capsys):
    code, out, _ = run(capsys, "--fixture", RANGE, "--format", "markdown", "--fail-on", "never")
    assert code == 0
    assert out.startswith("### PVC growth forecast")
    assert "| `prod` | `postgres-data` |" in out
    assert out.count("\n|") >= 6


def test_namespace_filter(capsys):
    code, out, _ = run(capsys, "--fixture", RANGE, "--namespace", "staging", "--format", "json")
    assert code == 0
    payload = json.loads(out)
    claims = {volume["persistentvolumeclaim"] for volume in payload["volumes"]}
    assert claims == {"redis-cache", "log-archive"}


def test_no_matching_volumes_is_clean(capsys):
    code, out, _ = run(capsys, "--fixture", RANGE, "--namespace", "nope")
    assert code == 0
    assert "no PersistentVolumeClaims matched" in out


def test_empty_fixture_is_clean(capsys):
    code, _, _ = run(capsys, "--fixture", os.path.join(TESTDATA, "empty.json"))
    assert code == 0


def test_prometheus_error_exits_two(capsys):
    code, _, err = run(capsys, "--fixture", os.path.join(TESTDATA, "prometheus-error.json"))
    assert code == 2
    assert "prometheus error" in err


def test_missing_fixture_exits_two(capsys):
    code, _, err = run(capsys, "--fixture", "/nonexistent/range.json")
    assert code == 2
    assert "cannot read fixture" in err


def test_bad_duration_exits_two(capsys):
    code, _, err = run(capsys, "--lookback", "7years")
    assert code == 2
    assert "duration" in err


def test_step_longer_than_lookback_exits_two(capsys):
    code, _, err = run(capsys, "--lookback", "1h", "--step", "6h")
    assert code == 2
    assert "--step" in err


def test_inverted_thresholds_exit_two(capsys):
    code, _, err = run(capsys, "--fixture", RANGE, "--warning-days", "5", "--critical-days", "30")
    assert code == 2
    assert "--critical-days" in err


def test_unreachable_prometheus_exits_two(capsys):
    # Port 1 on localhost refuses connections immediately, so this stays a fast offline test.
    code, _, err = run(capsys, "--prometheus", "http://127.0.0.1:1", "--lookback", "2h", "--step", "1h")
    assert code == 2
    assert "prometheus" in err.lower()


def test_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "k8s-pvc-growth-forecast" in capsys.readouterr().out
