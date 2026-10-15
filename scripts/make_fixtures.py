#!/usr/bin/env python3
"""Regenerate the committed range-query fixtures.

Everything here is deterministic: no RNG, so the fixtures are byte-stable across Python
versions and a diff in testdata/ always means a deliberate change.

    python3 scripts/make_fixtures.py
"""

from __future__ import annotations

import json
import math
import os

GiB = 1024 ** 3
STEP = 3600
WINDOW_HOURS = 7 * 24  # 169 samples, matching --lookback 7d --step 1h
END = 1_760_000_000  # fixed epoch so forecasts in tests never move
START = END - WINDOW_HOURS * STEP

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")


def series(namespace, claim, used_fn, capacity_fn):
    used = []
    capacity = []
    for index in range(WINDOW_HOURS + 1):
        ts = START + index * STEP
        hours = index
        used.append([ts, "{:.0f}".format(used_fn(hours))])
        capacity.append([ts, "{:.0f}".format(capacity_fn(hours))])
    labels = {"namespace": namespace, "persistentvolumeclaim": claim}
    return (
        {"metric": dict(labels), "values": used},
        {"metric": dict(labels), "values": capacity},
    )


def matrix(entries):
    return {"status": "success", "data": {"resultType": "matrix", "result": entries}}


def build():
    used_entries = []
    capacity_entries = []

    def add(namespace, claim, used_fn, capacity_fn):
        used, capacity = series(namespace, claim, used_fn, capacity_fn)
        used_entries.append(used)
        capacity_entries.append(capacity)

    # 1. Steady 1 GiB/day growth, ends at 90 GiB of 100 GiB -> 10 days to full.
    add(
        "prod",
        "postgres-data",
        lambda h: (83 + h / 24.0) * GiB,
        lambda h: 100 * GiB,
    )

    # 2. Resized mid-window. Before the expand it climbed 5 GiB/day into a 50 GiB volume;
    #    after the expand to 200 GiB it grows 1 GiB/day. A naive whole-window fit reads the
    #    step and panics; the post-resize fit is the honest one.
    def metrics_used(h):
        if h <= 84:
            return (18 + 5 * h / 24.0) * GiB
        return (35.5 + 1.0 * (h - 84) / 24.0) * GiB

    add("prod", "metrics-data", metrics_used, lambda h: (50 if h <= 84 else 200) * GiB)

    # 3. Flat with a little jitter: a cache that evicts as fast as it fills.
    add(
        "staging",
        "redis-cache",
        lambda h: (12 + 0.05 * math.sin(h / 3.0)) * GiB,
        lambda h: 20 * GiB,
    )

    # 4. Shrinking: log rotation finally works.
    add(
        "staging",
        "log-archive",
        lambda h: (60 - 2.0 * h / 24.0) * GiB,
        lambda h: 80 * GiB,
    )

    # 5. Noisy sawtooth with a weak upward trend: R² lands far below --min-r2, so the row is
    #    reported as low-confidence instead of being used to fail a pipeline.
    def scratch_used(h):
        base = 20 + 0.3 * h / 24.0
        saw = 9.0 * ((h % 11) / 11.0)
        return (base + saw) * GiB

    add("dev", "scratch", scratch_used, lambda h: 50 * GiB)

    return matrix(used_entries), matrix(capacity_entries)


def main():
    used, capacity = build()
    os.makedirs(OUT_DIR, exist_ok=True)

    with open(os.path.join(OUT_DIR, "range.json"), "w", encoding="utf-8") as handle:
        json.dump({"used": used, "capacity": capacity}, handle, indent=1)
        handle.write("\n")

    with open(os.path.join(OUT_DIR, "empty.json"), "w", encoding="utf-8") as handle:
        json.dump({"used": matrix([]), "capacity": matrix([])}, handle, indent=1)
        handle.write("\n")

    with open(os.path.join(OUT_DIR, "prometheus-error.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "used": {
                    "status": "error",
                    "errorType": "bad_data",
                    "error": 'parse error at char 1: unknown function "kubelet_volume_stats"',
                },
                "capacity": matrix([]),
            },
            handle,
            indent=1,
        )
        handle.write("\n")

    print("wrote fixtures to {}".format(OUT_DIR))


if __name__ == "__main__":
    main()
