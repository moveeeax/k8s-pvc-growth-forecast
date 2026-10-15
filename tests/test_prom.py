"""Query building, matrix parsing and fixture loading."""

from __future__ import annotations

import json
import os

import pytest

from pvcforecast.prom import (
    CAPACITY_METRIC,
    USED_METRIC,
    QueryError,
    build_query,
    load_fixture,
    parse_duration,
    parse_matrix,
)

TESTDATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")


def matrix(result):
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


@pytest.mark.parametrize(
    "text,seconds",
    [("30s", 30), ("5m", 300), ("6h", 21600), ("7d", 604800), ("2w", 1209600), ("1.5h", 5400)],
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "7", "7y", "d", "-5m", "0h", "abc"])
def test_parse_duration_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_build_query():
    assert build_query(USED_METRIC) == USED_METRIC
    assert build_query(USED_METRIC, namespace="prod") == USED_METRIC + '{namespace="prod"}'
    assert (
        build_query(CAPACITY_METRIC, namespace="prod", selector='persistentvolumeclaim=~"data-.*"')
        == CAPACITY_METRIC + '{namespace="prod",persistentvolumeclaim=~"data-.*"}'
    )
    # A selector pasted with its braces still works.
    assert build_query(USED_METRIC, selector='{app="db"}') == USED_METRIC + '{app="db"}'


def test_parse_matrix_groups_by_namespace_and_claim():
    payload = matrix(
        [
            {
                "metric": {"namespace": "prod", "persistentvolumeclaim": "data", "node": "n1"},
                "values": [[2.0, "20"], [1.0, "10"]],
            }
        ]
    )
    parsed = parse_matrix(payload)
    assert list(parsed) == [("prod", "data")]
    assert parsed[("prod", "data")] == [(1.0, 10.0), (2.0, 20.0)]  # sorted oldest first


def test_parse_matrix_skips_unattributable_and_nan_samples():
    payload = matrix(
        [
            {"metric": {"node": "n1"}, "values": [[1.0, "5"]]},
            {"metric": {"namespace": "prod", "persistentvolumeclaim": "d"}, "values": [[1.0, "NaN"], [2.0, "7"]]},
            {"metric": {"namespace": "prod", "persistentvolumeclaim": "empty"}, "values": []},
        ]
    )
    parsed = parse_matrix(payload)
    assert parsed == {("prod", "d"): [(2.0, 7.0)]}


def test_parse_matrix_rejects_instant_vectors_and_errors():
    with pytest.raises(QueryError) as excinfo:
        parse_matrix({"status": "success", "data": {"resultType": "vector", "result": []}})
    assert "matrix" in str(excinfo.value)

    with pytest.raises(QueryError) as excinfo:
        parse_matrix({"status": "error", "errorType": "bad_data", "error": "parse error"})
    assert "parse error" in str(excinfo.value)

    for bad in [[], "nope", {"status": "success"}, {"status": "weird"}]:
        with pytest.raises(QueryError):
            parse_matrix(bad)


def test_load_fixture_joins_both_metrics():
    series = load_fixture(os.path.join(TESTDATA, "range.json"))
    assert [str(item.key) for item in series] == [
        "dev/scratch",
        "prod/metrics-data",
        "prod/postgres-data",
        "staging/log-archive",
        "staging/redis-cache",
    ]
    for item in series:
        assert len(item.used) == len(item.capacity) == 169


def test_load_fixture_rejects_the_wrong_shape(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(matrix([])), encoding="utf-8")
    with pytest.raises(QueryError) as excinfo:
        load_fixture(str(path))
    assert "'used' and 'capacity'" in str(excinfo.value)

    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(QueryError):
        load_fixture(str(broken))

    with pytest.raises(QueryError):
        load_fixture(str(tmp_path / "missing.json"))


def test_prometheus_error_fixture_is_surfaced():
    with pytest.raises(QueryError):
        load_fixture(os.path.join(TESTDATA, "prometheus-error.json"))


def test_empty_fixture_is_valid_and_empty():
    assert load_fixture(os.path.join(TESTDATA, "empty.json")) == []
