"""Authentication to Prometheus: header building and what actually goes over the wire."""

from __future__ import annotations

import base64
import http.server
import json
import os
import threading

import pytest

from pvcforecast.cli import main
from pvcforecast.prom import (
    QueryError,
    basic_authorization,
    fetch_series,
    parse_header,
    read_token_file,
    resolve_bearer_token,
    ssl_context,
)

TESTDATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testdata")
EMPTY_MATRIX = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": []}}).encode()


class _Recorder(http.server.BaseHTTPRequestHandler):
    """Answers every range query with an empty matrix and records the request headers.

    ``/redirect/...`` answers 302 to the same query on ``/``, so a test can check which
    headers survive a redirect.
    """

    requests = None  # type: list

    def do_GET(self):  # noqa: N802 - http.server naming
        self.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/redirect"):])
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(EMPTY_MATRIX)))
        self.end_headers()
        self.wfile.write(EMPTY_MATRIX)

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def prometheus():
    """A loopback HTTP server standing in for Prometheus; yields (base_url, requests)."""
    requests = []
    handler = type("Handler", (_Recorder,), {"requests": requests})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:{}".format(server.server_address[1]), requests
    finally:
        server.shutdown()
        server.server_close()


def test_parse_header():
    assert parse_header("X-Scope-OrgID: team-a") == ("X-Scope-OrgID", "team-a")
    assert parse_header("X-Token:a:b") == ("X-Token", "a:b")  # only the first colon splits
    assert parse_header("X-Empty:") == ("X-Empty", "")


@pytest.mark.parametrize("text", ["", "no-colon", ": value", "Bad Name: x", "X-A: one\r\nX-B: two"])
def test_parse_header_rejects_junk(text):
    with pytest.raises(ValueError):
        parse_header(text)


def test_basic_authorization():
    value = basic_authorization("grafana", "s3cret:with:colons")
    assert value.startswith("Basic ")
    assert base64.b64decode(value[len("Basic "):]).decode() == "grafana:s3cret:with:colons"
    with pytest.raises(ValueError):
        basic_authorization("user:name", "pw")


def test_read_token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("abc.def\n", encoding="utf-8")
    assert read_token_file(str(path)) == "abc.def"

    path.write_text("\n", encoding="utf-8")
    with pytest.raises(QueryError) as excinfo:
        read_token_file(str(path))
    assert "empty" in str(excinfo.value)

    path.write_text("one\ntwo\n", encoding="utf-8")
    with pytest.raises(QueryError):
        read_token_file(str(path))

    with pytest.raises(QueryError):
        read_token_file(str(tmp_path / "missing"))


def test_resolve_bearer_token_auto(tmp_path):
    mounted = tmp_path / "token"
    mounted.write_text("sa-token\n", encoding="utf-8")
    assert resolve_bearer_token("auto", service_account_path=str(mounted)) == "sa-token"
    # Outside a pod there is no mounted token, and auto quietly sends none.
    assert resolve_bearer_token("auto", service_account_path=str(tmp_path / "absent")) is None
    assert resolve_bearer_token(None) is None
    assert resolve_bearer_token(str(mounted)) == "sa-token"


def test_ssl_context(tmp_path):
    assert ssl_context(None) is None
    with pytest.raises(QueryError) as excinfo:
        ssl_context(str(tmp_path / "missing.pem"))
    assert "CA file" in str(excinfo.value)
    junk = tmp_path / "junk.pem"
    junk.write_text("not a certificate", encoding="utf-8")
    with pytest.raises(QueryError):
        ssl_context(str(junk))


def test_fetch_series_sends_headers_on_both_queries(prometheus):
    base_url, requests = prometheus
    fetch_series(
        base_url,
        end=7200.0,
        lookback_seconds=3600,
        step_seconds=60,
        headers={"X-Scope-OrgID": "team-a"},
        authorization="Bearer tok",
    )
    assert len(requests) == 2
    for _, headers in requests:
        assert headers["authorization"] == "Bearer tok"
        assert headers["x-scope-orgid"] == "team-a"
        assert headers["accept"] == "application/json"


def test_authorization_does_not_follow_a_redirect(prometheus):
    base_url, requests = prometheus
    fetch_series(
        base_url + "/redirect",
        end=7200.0,
        lookback_seconds=3600,
        step_seconds=60,
        headers={"X-Scope-OrgID": "team-a"},
        authorization="Bearer tok",
    )
    redirected = [headers for path, headers in requests if not path.startswith("/redirect/")]
    assert len(redirected) == 2
    for headers in redirected:
        assert "authorization" not in headers
        assert headers["x-scope-orgid"] == "team-a"


def run(capsys, *args):
    code = main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def live(base_url, *extra):
    return ("--prometheus", base_url, "--lookback", "2h", "--step", "1h") + extra


def test_cli_bearer_token_file(prometheus, capsys, tmp_path):
    base_url, requests = prometheus
    token = tmp_path / "token"
    token.write_text("glc_abc\n", encoding="utf-8")
    code, _, err = run(capsys, *live(base_url, "--bearer-token-file", str(token)))
    assert code == 0
    assert [h["authorization"] for _, h in requests] == ["Bearer glc_abc"] * 2
    assert "plain HTTP" in err
    assert "glc_abc" not in err


def test_cli_basic_auth_reads_password_from_env(prometheus, capsys, monkeypatch):
    base_url, requests = prometheus
    monkeypatch.setenv("PROM_PASSWORD", "pw")
    code, _, _ = run(capsys, *live(base_url, "--basic-auth-user", "123456"))
    assert code == 0
    expected = "Basic " + base64.b64encode(b"123456:pw").decode()
    assert [h["authorization"] for _, h in requests] == [expected] * 2


def test_cli_basic_auth_without_password_exits_two(prometheus, capsys, monkeypatch):
    base_url, requests = prometheus
    monkeypatch.delenv("PROM_PASSWORD", raising=False)
    code, _, err = run(capsys, *live(base_url, "--basic-auth-user", "123456"))
    assert code == 2
    assert "PROM_PASSWORD" in err
    assert requests == []


def test_cli_repeatable_headers(prometheus, capsys):
    base_url, requests = prometheus
    code, _, err = run(
        capsys, *live(base_url, "--header", "X-Scope-OrgID: team-a", "--header", "X-Extra: 1")
    )
    assert code == 0
    for _, headers in requests:
        assert headers["x-scope-orgid"] == "team-a"
        assert headers["x-extra"] == "1"
        assert "authorization" not in headers
    assert "plain HTTP" not in err


def test_cli_authorization_header_is_treated_as_a_credential(prometheus, capsys):
    base_url, requests = prometheus
    code, _, err = run(capsys, *live(base_url, "--header", "Authorization: Bearer raw"))
    assert code == 0
    assert [h["authorization"] for _, h in requests] == ["Bearer raw"] * 2
    assert "plain HTTP" in err


@pytest.mark.parametrize(
    "extra,message",
    [
        (("--bearer-token-file", "auto", "--basic-auth-user", "u"), "not both"),
        (("--basic-auth-user", "u", "--header", "authorization: Basic x"), "conflicts"),
        (("--header", "X-A: 1", "--header", "x-a: 2"), "more than once"),
        (("--header", "nonsense"), "Name: value"),
        (("--bearer-token-file", "/nonexistent/token"), "bearer token file"),
        (("--ca-file", "/nonexistent/ca.pem"), "CA file"),
    ],
)
def test_cli_bad_auth_flags_exit_two(prometheus, capsys, monkeypatch, extra, message):
    base_url, requests = prometheus
    monkeypatch.setenv("PROM_PASSWORD", "pw")
    code, _, err = run(capsys, *live(base_url, *extra))
    assert code == 2
    assert message in err
    assert requests == []


def test_cli_auth_flags_are_ignored_with_a_fixture(capsys, tmp_path):
    fixture = os.path.join(TESTDATA, "empty.json")
    code, _, err = run(capsys, "--fixture", fixture, "--bearer-token-file", str(tmp_path / "missing"))
    assert code == 0
    assert err == ""
