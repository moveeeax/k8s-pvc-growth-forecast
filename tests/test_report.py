"""Rendering helpers."""

from __future__ import annotations

import os

import pytest

from pvcforecast.fit import forecast_all
from pvcforecast.prom import load_fixture
from pvcforecast.report import human_bytes, human_days, render, render_markdown, render_table

GiB = 1024 ** 3
RANGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata", "range.json")


@pytest.mark.parametrize(
    "value,text",
    [
        (None, "-"),
        (0, "0B"),
        (512, "512B"),
        (1024, "1.0KiB"),
        (10 * GiB, "10.0GiB"),
        (1536 * GiB, "1.5TiB"),
        (-2 * GiB, "-2.0GiB"),
    ],
)
def test_human_bytes(value, text):
    assert human_bytes(value) == text


@pytest.mark.parametrize("value,text", [(None, "-"), (3.25, "3.2"), (10.4, "10"), (4000.0, ">1000")])
def test_human_days(value, text):
    assert human_days(value) == text


@pytest.fixture(scope="module")
def forecasts():
    return forecast_all(load_fixture(RANGE), 30.0, 7.0, 0.5)


def test_table_is_aligned_and_carries_notes(forecasts):
    text = render_table(forecasts)
    lines = text.splitlines()
    assert lines[0].startswith("STATUS")
    header_claim_column = lines[0].index("CLAIM")
    assert lines[1].index("postgres-data") == header_claim_column
    assert any(line.startswith("note  prod/metrics-data") for line in lines)


def test_stable_rows_show_no_days(forecasts):
    row = [line for line in render_table(forecasts).splitlines() if "log-archive" in line][0]
    assert row.split()[0] == "stable"
    assert "-2.0GiB/d" in row


def test_markdown_lists_every_volume(forecasts):
    text = render_markdown(forecasts)
    for claim in ("postgres-data", "metrics-data", "redis-cache", "log-archive", "scratch"):
        assert "`{}`".format(claim) in text
    assert "5 volume(s)" in text


def test_empty_renderings():
    assert "no PersistentVolumeClaims matched" in render([], "table")
    assert "no PersistentVolumeClaims matched" in render([], "markdown")
    assert render([], "json").strip().startswith("{")


def test_render_dispatch(forecasts):
    assert render(forecasts, "json").startswith("{")
    assert render(forecasts, "markdown").startswith("###")
    assert render(forecasts, "table").startswith("STATUS")
