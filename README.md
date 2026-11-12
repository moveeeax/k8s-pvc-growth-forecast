# k8s-pvc-growth-forecast

[![ci](https://github.com/moveeeax/k8s-pvc-growth-forecast/actions/workflows/ci.yml/badge.svg)](https://github.com/moveeeax/k8s-pvc-growth-forecast/actions/workflows/ci.yml)
[![python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A PVC that is 71% full tells you nothing. A PVC that gained 4 GiB a day for the last week and
has 9 GiB left tells you exactly when you get paged.

`k8s-pvc-growth-forecast` reads the two metrics kubelet already exports for every mounted
claim, fits the growth rate per volume, and prints days-to-full ranked soonest first. No
operator, no CRDs, no dependencies: stdlib-only Python, so it runs in any `python:3-alpine`
pod or straight off a laptop against a port-forward.

```
STATUS          NAMESPACE  CLAIM          USED     CAPACITY  USED%  GROWTH       DAYS  R2
warning         prod       postgres-data  90.0GiB  100.0GiB  90%    +1.0GiB/d    10    1.00
low-confidence  dev        scratch        24.6GiB  50.0GiB   49%    +332.2MiB/d  78    0.06
ok              prod       metrics-data   39.0GiB  200.0GiB  20%    +1.0GiB/d    161   1.00
stable          staging    log-archive    46.0GiB  80.0GiB   58%    -2.0GiB/d    -     1.00
stable          staging    redis-cache    12.0GiB  20.0GiB   60%    -1.5MiB/d    -     0.01
```

## Install

```sh
git clone https://github.com/moveeeax/k8s-pvc-growth-forecast
cd k8s-pvc-growth-forecast
./k8s-pvc-growth-forecast --help
```

Or install the console script:

```sh
pip install git+https://github.com/moveeeax/k8s-pvc-growth-forecast
k8s-pvc-growth-forecast --version
```

## Usage

```sh
# Against an in-cluster Prometheus
k8s-pvc-growth-forecast --prometheus http://prometheus-operated.monitoring.svc:9090

# Through a port-forward, two-week window, Markdown for a PR comment or job summary
kubectl -n monitoring port-forward svc/prometheus-operated 9090:9090 &
k8s-pvc-growth-forecast --lookback 14d --format markdown --fail-on critical

# One namespace, tighter thresholds
k8s-pvc-growth-forecast --namespace prod --warning-days 45 --critical-days 14

# Grafana Cloud: basic auth with the metrics instance ID as user and an access policy
# token (scope metrics:read) as password. Copy the query endpoint URL and the instance
# ID from the Prometheus card's Details page in the Cloud Portal.
PROM_PASSWORD="$GRAFANA_CLOUD_TOKEN" k8s-pvc-growth-forecast \
  --prometheus '<query endpoint URL from the Details page>' \
  --basic-auth-user '<metrics instance ID>'

# Mimir or Cortex: pick the tenant
k8s-pvc-growth-forecast --prometheus http://mimir-gateway.mimir.svc/prometheus \
  --header 'X-Scope-OrgID: team-a'

# OpenShift Thanos querier from inside a pod, with the pod's service-account token.
# Port 9091 serves all namespaces and needs the cluster-monitoring-view cluster role
# bound to the service account. The serving cert is signed by the OpenShift service CA;
# --ca-file points at a CA bundle holding it (see the note below the examples).
k8s-pvc-growth-forecast \
  --prometheus https://thanos-querier.openshift-monitoring.svc:9091 \
  --bearer-token-file auto \
  --ca-file /var/run/secrets/kubernetes.io/serviceaccount/service-ca.crt

# Offline, against recorded data
k8s-pvc-growth-forecast --fixture testdata/range.json --format json
```

Notes on the hosted examples:

- Grafana Cloud: the hostname differs per stack and region, so it is not spelled out here.
  The Prometheus HTTP API sits under the query endpoint URL, which is the value to pass to
  `--prometheus` ([Grafana Cloud: query metrics using HTTP APIs](https://grafana.com/docs/grafana-cloud/send-data/metrics/metrics-prometheus/query-http-api/)).
- OpenShift: the `thanos-querier` service in `openshift-monitoring` exposes port 9091 for
  all Thanos Querier endpoints (`cluster-monitoring-view` or `cluster-monitoring-metrics-api`
  role) and port 9092 for queries restricted to one project (`view` role in that project)
  ([service definition in cluster-monitoring-operator](https://github.com/openshift/cluster-monitoring-operator/blob/main/assets/thanos-querier/service.yaml)).
  The OpenShift docs describe `service-ca.crt` in the service-account directory as mounted
  automatically into pods
  ([OpenShift 3.11: secrets](https://docs.redhat.com/en/documentation/openshift_container_platform/3.11/html/developer_guide/dev-guide-secrets)).
  If it is missing in yours, mount a ConfigMap annotated with
  `service.beta.openshift.io/inject-cabundle=true` and point `--ca-file` at its
  `service-ca.crt` key ([OpenShift: configuring certificates](https://docs.redhat.com/en/documentation/openshift_container_platform/4.8/html/security_and_compliance/configuring-certificates)).

Real run against the committed fixture:

```sh
$ ./k8s-pvc-growth-forecast --fixture testdata/range.json --warning-days 30 --critical-days 7
STATUS    NAMESPACE  CLAIM          USED     CAPACITY  USED%  GROWTH     DAYS  R2
warning   prod       postgres-data  90.0GiB  100.0GiB  90%    +1.0GiB/d  10    1.00
...
note  prod/metrics-data: refit on the 84 samples after the resize at 2025-10-05 21:53 UTC
$ echo $?
1
```

### Flags

| Flag | Default | What it does |
|---|---|---|
| `--prometheus URL` | `http://localhost:9090` | Prometheus base URL |
| `--fixture FILE` | - | Read recorded range-query JSON instead of querying Prometheus |
| `--lookback` | `7d` | Fit window |
| `--step` | `1h` | Range-query resolution |
| `--warning-days` | `30` | Days-to-full warning threshold |
| `--critical-days` | `7` | Days-to-full critical threshold |
| `--min-r2` | `0.5` | Fit quality below which a forecast is only a hint |
| `--horizon-days` | `3650` | Forecasts further out than this are reported as `stable` |
| `--namespace` | - | Restrict to one namespace |
| `--selector` | - | Extra PromQL matchers, e.g. `persistentvolumeclaim=~"data-.*"` |
| `--format` | `table` | `table`, `json` or `markdown` |
| `--fail-on` | `warning` | `warning`, `critical` or `never` |
| `--timeout` | `30` | HTTP timeout for each Prometheus query, in seconds |
| `--bearer-token-file PATH` | - | Send the token in `PATH` as `Authorization: Bearer`; `auto` uses the pod's service-account token when mounted and sends nothing otherwise |
| `--basic-auth-user USER` | - | HTTP basic auth; the password comes from `$PROM_PASSWORD`, never from the command line |
| `--header 'NAME: VALUE'` | - | Extra request header, repeatable, e.g. `X-Scope-OrgID: team-a` |
| `--ca-file PATH` | system trust store | PEM CA bundle used to verify an HTTPS Prometheus |

Exit codes: `0` clean, `1` findings at or above `--fail-on`, `2` query or parse error.

### Authentication

The authentication flags apply to live queries only; `--fixture` ignores them. Bearer token and
basic auth are mutually exclusive, and `--header 'Authorization: ...'` counts as a credential
too. The `Authorization` header is never forwarded when Prometheus answers with a redirect,
so a token cannot leak to whatever host the redirect points at. Other `--header` values are
forwarded. Credentials sent to an `http://` URL produce a warning on stderr. TLS verification
is always on: `--ca-file` adds trust, nothing turns verification off.

## How it works

1. Two range queries over the lookback window, grouped by `namespace` and
   `persistentvolumeclaim`:
   `kubelet_volume_stats_used_bytes` and `kubelet_volume_stats_capacity_bytes`.
2. Per claim, the capacity series is scanned for a step. A PVC expand makes used-bytes history
   from before the expand belong to a different volume, so the fit restarts after the last
   capacity change.
3. Ordinary least squares of used-bytes against time, in days. The slope is growth per day,
   and R² travels with it as a fit-quality signal.
4. `days_to_full = free_bytes / growth_per_day`, then classification:

| Status | Meaning |
|---|---|
| `critical` | fills within `--critical-days` |
| `warning` | fills within `--warning-days` |
| `ok` | growing, but past the warning horizon |
| `stable` | flat or shrinking, or further out than `--horizon-days`; no forecast is published |
| `low-confidence` | R² below `--min-r2`; the number is shown but never fails the run |
| `no-data` | no capacity series, or too few samples to fit |

Only `critical` and `warning` can trip `--fail-on`. A sawtooth cache volume should not be able
to break a pipeline.

## Fixtures and tests

`testdata/range.json` holds five recorded volumes: steady growth, a volume resized mid-window,
a flat cache, a shrinking log archive, and a noisy series below the R² floor. They are
generated deterministically by `scripts/make_fixtures.py`, and CI regenerates them to check
the committed copies still match.

```sh
pip install pytest
python -m pytest -q
```

See `examples/` for a weekly-report Markdown sample, a `CronJob` that runs the review in
cluster, and a GitHub Actions gate.

## One thing it does not do

It will not tell you *why* a volume grows. The fit is linear on purpose: a straight line over a
one-to-two week window is honest about a disk that fills at a steady rate, and the R² floor is
there to admit when the data does not support a straight line at all. If you need seasonality,
feed the slopes into Prometheus as recording rules and alert on those instead.

## License

MIT, see [LICENSE](LICENSE).
