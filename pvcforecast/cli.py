"""Command line entry point.

Exit codes: 0 clean, 1 findings at or above ``--fail-on``, 2 query or parse error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

from . import __version__
from .fit import DEFAULT_HORIZON_DAYS, forecast_all, should_fail, worst_status
from .prom import (
    QueryError,
    VolumeSeries,
    basic_authorization,
    bearer_authorization,
    fetch_series,
    load_fixture,
    parse_duration,
    parse_header,
    resolve_bearer_token,
)
from .report import render

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

PASSWORD_ENV = "PROM_PASSWORD"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="k8s-pvc-growth-forecast",
        description=(
            "Forecast when every PersistentVolumeClaim runs out of space, from kubelet volume "
            "stats in Prometheus."
        ),
    )
    parser.add_argument(
        "--prometheus",
        metavar="URL",
        default="http://localhost:9090",
        help="Prometheus base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--fixture",
        metavar="FILE",
        help="read recorded range-query JSON instead of querying Prometheus (offline runs, tests)",
    )
    parser.add_argument("--lookback", default="7d", help="fit window (default: %(default)s)")
    parser.add_argument("--step", default="1h", help="range-query resolution (default: %(default)s)")
    parser.add_argument(
        "--warning-days", type=float, default=30.0, help="days-to-full warning threshold (default: %(default)s)"
    )
    parser.add_argument(
        "--critical-days", type=float, default=7.0, help="days-to-full critical threshold (default: %(default)s)"
    )
    parser.add_argument(
        "--min-r2",
        type=float,
        default=0.5,
        help="minimum fit quality before a forecast counts as confident (default: %(default)s)",
    )
    parser.add_argument(
        "--horizon-days",
        type=float,
        default=DEFAULT_HORIZON_DAYS,
        help="forecasts beyond this many days are reported as stable (default: %(default)s)",
    )
    parser.add_argument("--namespace", help="restrict to one namespace")
    parser.add_argument(
        "--selector",
        metavar="MATCHERS",
        help='extra PromQL label matchers, e.g. \'persistentvolumeclaim=~"data-.*"\' (live queries only)',
    )
    parser.add_argument(
        "--format", choices=("table", "json", "markdown"), default="table", help="output format (default: %(default)s)"
    )
    parser.add_argument(
        "--fail-on",
        choices=("warning", "critical", "never"),
        default="warning",
        help="exit 1 when a volume reaches this status (default: %(default)s)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds (default: %(default)s)")
    auth = parser.add_argument_group("authentication (live queries only)")
    auth.add_argument(
        "--bearer-token-file",
        metavar="PATH",
        help="send the token in PATH as 'Authorization: Bearer'; 'auto' uses the pod's "
        "service-account token when it is mounted",
    )
    auth.add_argument(
        "--basic-auth-user",
        metavar="USER",
        help="HTTP basic auth user; the password is read from ${}".format(PASSWORD_ENV),
    )
    auth.add_argument(
        "--header",
        metavar="'NAME: VALUE'",
        action="append",
        default=[],
        help="extra request header, repeatable, e.g. 'X-Scope-OrgID: team-a'",
    )
    auth.add_argument("--ca-file", metavar="PATH", help="CA bundle (PEM) to verify an HTTPS Prometheus")
    parser.add_argument("--version", action="version", version="k8s-pvc-growth-forecast {}".format(__version__))
    return parser


def _connection(args: argparse.Namespace) -> Tuple[Dict[str, str], Optional[str]]:
    """Turn the authentication flags into extra headers and an ``Authorization`` value."""
    headers: Dict[str, str] = {}
    authorization: Optional[str] = None
    for raw in args.header:
        name, value = parse_header(raw)
        if name.lower() == "authorization":
            if authorization is not None:
                raise ValueError("--header Authorization given more than once")
            authorization = value
            continue
        if any(existing.lower() == name.lower() for existing in headers):
            raise ValueError("--header {} given more than once".format(name))
        headers[name] = value

    if args.bearer_token_file and args.basic_auth_user:
        raise ValueError("use either --bearer-token-file or --basic-auth-user, not both")
    if (args.bearer_token_file or args.basic_auth_user) and authorization is not None:
        raise ValueError("--header Authorization conflicts with --bearer-token-file/--basic-auth-user")

    if args.basic_auth_user:
        password = os.environ.get(PASSWORD_ENV)
        if password is None:
            raise ValueError("--basic-auth-user needs the password in ${}".format(PASSWORD_ENV))
        authorization = basic_authorization(args.basic_auth_user, password)
    token = resolve_bearer_token(args.bearer_token_file)
    if token is not None:
        authorization = bearer_authorization(token)

    if authorization is not None and urllib.parse.urlsplit(args.prometheus).scheme != "https":
        print(
            "k8s-pvc-growth-forecast: warning: sending credentials to {} over plain HTTP".format(
                args.prometheus
            ),
            file=sys.stderr,
        )
    return headers, authorization


def _collect(args: argparse.Namespace) -> List[VolumeSeries]:
    if args.fixture:
        series = load_fixture(args.fixture)
    else:
        lookback = parse_duration(args.lookback)
        step = parse_duration(args.step)
        if step >= lookback:
            raise QueryError("--step must be shorter than --lookback")
        headers, authorization = _connection(args)
        series = fetch_series(
            args.prometheus,
            end=time.time(),
            lookback_seconds=lookback,
            step_seconds=step,
            namespace=args.namespace,
            selector=args.selector,
            timeout=args.timeout,
            headers=headers,
            authorization=authorization,
            ca_file=args.ca_file,
        )
    if args.namespace:
        series = [item for item in series if item.key.namespace == args.namespace]
    return series


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.critical_days > args.warning_days:
        print(
            "k8s-pvc-growth-forecast: --critical-days must not exceed --warning-days",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        if not args.fixture:
            parse_duration(args.lookback)
            parse_duration(args.step)
        series = _collect(args)
    except ValueError as exc:
        print("k8s-pvc-growth-forecast: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR
    except QueryError as exc:
        print("k8s-pvc-growth-forecast: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR

    forecasts = forecast_all(
        series,
        warning_days=args.warning_days,
        critical_days=args.critical_days,
        min_r2=args.min_r2,
        horizon_days=args.horizon_days,
    )
    print(render(forecasts, args.format))
    sys.stdout.flush()

    if should_fail(forecasts, args.fail_on):
        print(
            "k8s-pvc-growth-forecast: {} volume(s) at or above --fail-on {} (worst: {})".format(
                sum(1 for f in forecasts if f.status in ("critical", "warning")),
                args.fail_on,
                worst_status(forecasts),
            ),
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
