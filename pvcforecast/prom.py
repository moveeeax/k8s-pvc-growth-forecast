"""Prometheus range queries for kubelet volume stats, plus offline fixtures.

Two metrics carry everything we need, and kubelet exports both for every mounted PVC:

    kubelet_volume_stats_used_bytes{namespace="...",persistentvolumeclaim="..."}
    kubelet_volume_stats_capacity_bytes{namespace="...",persistentvolumeclaim="..."}

A fixture file is a single JSON object holding one ``/api/v1/query_range`` response per
metric, so a recorded cluster can be replayed offline:

    {"used": {...range response...}, "capacity": {...range response...}}

Live queries can carry credentials for managed or multi-tenant endpoints (Grafana Cloud,
Mimir, Thanos behind an auth proxy, OpenShift): a bearer token or basic auth goes into the
``Authorization`` header, extra headers such as ``X-Scope-OrgID`` ride along as given, and
``ca_file`` pins the CA bundle used to verify an HTTPS endpoint.
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, NamedTuple, Optional, Tuple

USED_METRIC = "kubelet_volume_stats_used_bytes"
CAPACITY_METRIC = "kubelet_volume_stats_capacity_bytes"

NAMESPACE_LABEL = "namespace"
CLAIM_LABEL = "persistentvolumeclaim"

# Projected service-account token mounted into every pod that does not opt out.
SERVICE_ACCOUNT_TOKEN = "/var/run/secrets/kubernetes.io/serviceaccount/token"

# RFC 9110 field-name: a token of visible ASCII without separators.
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

Sample = Tuple[float, float]


class QueryError(Exception):
    """Prometheus was unreachable, unhappy, or answered with something unexpected."""


class SeriesKey(NamedTuple):
    namespace: str
    claim: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "{}/{}".format(self.namespace, self.claim)


class VolumeSeries:
    """Used-bytes and capacity-bytes samples for one PVC, oldest first."""

    def __init__(
        self,
        key: SeriesKey,
        used: Optional[List[Sample]] = None,
        capacity: Optional[List[Sample]] = None,
    ) -> None:
        self.key = key
        self.used = used or []
        self.capacity = capacity or []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "VolumeSeries({}, used={}, capacity={})".format(
            self.key, len(self.used), len(self.capacity)
        )


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> int:
    """Parse a Prometheus-style duration (``90s``, ``30m``, ``6h``, ``7d``, ``2w``)."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty duration")
    unit = raw[-1]
    if unit not in _UNITS:
        raise ValueError("unknown duration unit in {!r} (use s, m, h, d or w)".format(text))
    body = raw[:-1]
    if not body:
        raise ValueError("missing duration value in {!r}".format(text))
    try:
        value = float(body)
    except ValueError:
        raise ValueError("bad duration value in {!r}".format(text))
    if value <= 0:
        raise ValueError("duration must be positive, got {!r}".format(text))
    return int(value * _UNITS[unit])


def build_query(metric: str, namespace: Optional[str] = None, selector: Optional[str] = None) -> str:
    """Build ``metric{matchers}`` from a namespace shortcut and a raw matcher string."""
    matchers = []
    if namespace:
        matchers.append('{}="{}"'.format(NAMESPACE_LABEL, namespace))
    if selector:
        matchers.append(selector.strip().strip("{}").strip())
    matchers = [m for m in matchers if m]
    if not matchers:
        return metric
    return "{}{{{}}}".format(metric, ",".join(matchers))


def parse_matrix(payload: object, what: str = "response") -> Dict[SeriesKey, List[Sample]]:
    """Turn a range-query matrix response into ``{SeriesKey: [(ts, value), ...]}``.

    Series missing the namespace or PVC label are skipped: they cannot be attributed to a
    claim, and guessing a name is worse than leaving the volume out of the report.
    """
    if not isinstance(payload, dict):
        raise QueryError("{}: expected a JSON object".format(what))
    status = payload.get("status")
    if status == "error":
        raise QueryError(
            "{}: prometheus error ({}): {}".format(
                what, payload.get("errorType", "unknown"), payload.get("error", "no detail")
            )
        )
    if status != "success":
        raise QueryError("{}: unexpected status {!r}".format(what, status))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise QueryError("{}: missing data object".format(what))
    result_type = data.get("resultType")
    if result_type != "matrix":
        raise QueryError(
            "{}: expected resultType matrix (a range query), got {!r}".format(what, result_type)
        )
    result = data.get("result")
    if not isinstance(result, list):
        raise QueryError("{}: missing result list".format(what))

    out: Dict[SeriesKey, List[Sample]] = {}
    for entry in result:
        if not isinstance(entry, dict):
            raise QueryError("{}: malformed series entry".format(what))
        labels = entry.get("metric") or {}
        namespace = labels.get(NAMESPACE_LABEL)
        claim = labels.get(CLAIM_LABEL)
        if not namespace or not claim:
            continue
        samples: List[Sample] = []
        for pair in entry.get("values") or []:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise QueryError("{}: malformed sample in series {}/{}".format(what, namespace, claim))
            try:
                ts = float(pair[0])
                value = float(pair[1])
            except (TypeError, ValueError):
                # NaN and friends arrive as strings; drop the sample rather than the series.
                continue
            if value != value:  # NaN
                continue
            samples.append((ts, value))
        if not samples:
            continue
        samples.sort(key=lambda s: s[0])
        key = SeriesKey(namespace, claim)
        out.setdefault(key, []).extend(samples)
    return out


def join_series(
    used: Dict[SeriesKey, List[Sample]], capacity: Dict[SeriesKey, List[Sample]]
) -> List[VolumeSeries]:
    """Join used and capacity samples per PVC, sorted by namespace then claim."""
    series = []
    for key in sorted(set(used) | set(capacity)):
        series.append(VolumeSeries(key, list(used.get(key, [])), list(capacity.get(key, []))))
    return series


def load_fixture(path: str) -> List[VolumeSeries]:
    """Load a recorded ``{"used": ..., "capacity": ...}`` fixture from disk."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise QueryError("cannot read fixture {}: {}".format(path, exc))
    except ValueError as exc:
        raise QueryError("fixture {} is not valid JSON: {}".format(path, exc))
    if not isinstance(payload, dict) or "used" not in payload or "capacity" not in payload:
        raise QueryError(
            "fixture {} must be a JSON object with 'used' and 'capacity' range responses".format(path)
        )
    return join_series(
        parse_matrix(payload["used"], "fixture used"),
        parse_matrix(payload["capacity"], "fixture capacity"),
    )


def parse_header(text: str) -> Tuple[str, str]:
    """Split ``'Name: value'`` into a validated ``(name, value)`` pair."""
    name, sep, value = (text or "").partition(":")
    name = name.strip()
    value = value.strip()
    if not sep or not name:
        raise ValueError("header {!r} must look like 'Name: value'".format(text))
    if not _HEADER_NAME.match(name):
        raise ValueError("invalid header name {!r}".format(name))
    if any(ch in value for ch in "\r\n\0"):
        raise ValueError("header {} must not contain line breaks or NUL".format(name))
    return name, value


def read_token_file(path: str) -> str:
    """Read a bearer token from disk, trimming the trailing newline most tools leave."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        raise QueryError("cannot read bearer token file {}: {}".format(path, exc))
    if not token:
        raise QueryError("bearer token file {} is empty".format(path))
    if any(ch in token for ch in "\r\n\0"):
        raise QueryError("bearer token file {} must hold a single line".format(path))
    return token


def resolve_bearer_token(spec: Optional[str], service_account_path: str = SERVICE_ACCOUNT_TOKEN) -> Optional[str]:
    """Return the token for ``--bearer-token-file``.

    ``auto`` uses the pod's service-account token when it is mounted and sends no token
    otherwise, so the same command line works in cluster and from a laptop.
    """
    if not spec:
        return None
    if spec == "auto":
        if not os.path.isfile(service_account_path):
            return None
        return read_token_file(service_account_path)
    return read_token_file(spec)


def bearer_authorization(token: str) -> str:
    return "Bearer {}".format(token)


def basic_authorization(user: str, password: str) -> str:
    """Build an RFC 7617 ``Basic`` credential; the user-id may not contain a colon."""
    if ":" in user:
        raise ValueError("basic auth user must not contain ':'")
    raw = "{}:{}".format(user, password).encode("utf-8")
    return "Basic {}".format(base64.b64encode(raw).decode("ascii"))


def ssl_context(ca_file: Optional[str]) -> Optional[ssl.SSLContext]:
    """A verifying TLS context that trusts ``ca_file``, or ``None`` for the system default."""
    if not ca_file:
        return None
    try:
        return ssl.create_default_context(cafile=ca_file)
    except (OSError, ssl.SSLError) as exc:
        raise QueryError("cannot load CA file {}: {}".format(ca_file, exc))


def _range_query(
    base_url: str,
    query: str,
    start: float,
    end: float,
    step: int,
    timeout: float,
    headers: Optional[Dict[str, str]] = None,
    authorization: Optional[str] = None,
    context: Optional[ssl.SSLContext] = None,
) -> object:
    params = urllib.parse.urlencode(
        {"query": query, "start": repr(start), "end": repr(end), "step": "{}s".format(step)}
    )
    url = "{}/api/v1/query_range?{}".format(base_url.rstrip("/"), params)
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    if authorization:
        # Unredirected: urllib copies ordinary headers onto a redirect, possibly to another
        # host, and a credential must never follow it there.
        request.add_unredirected_header("Authorization", authorization)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # pragma: no cover - best effort
            pass
        raise QueryError("prometheus HTTP {} for {}: {}".format(exc.code, query, detail or exc.reason))
    except urllib.error.URLError as exc:
        raise QueryError("cannot reach prometheus at {}: {}".format(base_url, exc.reason))
    except OSError as exc:  # pragma: no cover - socket level
        raise QueryError("cannot reach prometheus at {}: {}".format(base_url, exc))
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        raise QueryError("prometheus returned invalid JSON: {}".format(exc))


def fetch_series(
    base_url: str,
    end: float,
    lookback_seconds: int,
    step_seconds: int,
    namespace: Optional[str] = None,
    selector: Optional[str] = None,
    timeout: float = 30.0,
    headers: Optional[Dict[str, str]] = None,
    authorization: Optional[str] = None,
    ca_file: Optional[str] = None,
) -> List[VolumeSeries]:
    """Range-query both kubelet metrics over the lookback window and join them per PVC.

    ``authorization`` is the full ``Authorization`` header value (``Bearer ...`` or
    ``Basic ...``); ``headers`` are sent as given, for example ``X-Scope-OrgID``.
    """
    start = end - lookback_seconds
    context = ssl_context(ca_file)

    def query(metric: str) -> object:
        return _range_query(
            base_url,
            build_query(metric, namespace, selector),
            start,
            end,
            step_seconds,
            timeout,
            headers=headers,
            authorization=authorization,
            context=context,
        )

    used = parse_matrix(query(USED_METRIC), "used-bytes query")
    capacity = parse_matrix(query(CAPACITY_METRIC), "capacity-bytes query")
    return join_series(used, capacity)
