### PVC growth forecast

5 volume(s): 1 warning, 1 ok, 1 low-confidence, 2 stable

| | Namespace | Claim | Used | Capacity | Growth | Days to full | R² |
|---|---|---|---|---|---|---|---|
| 🟠 | `prod` | `postgres-data` | 90.0GiB (90%) | 100.0GiB | +1.0GiB/d | 10 | 1.00 |
| 🔵 | `dev` | `scratch` | 24.6GiB (49%) | 50.0GiB | +332.2MiB/d | 78 | 0.06 |
| 🟢 | `prod` | `metrics-data` | 39.0GiB (20%) | 200.0GiB | +1.0GiB/d | 161 | 1.00 |
| ⚪ | `staging` | `log-archive` | 46.0GiB (58%) | 80.0GiB | -2.0GiB/d | - | 1.00 |
| ⚪ | `staging` | `redis-cache` | 12.0GiB (60%) | 20.0GiB | -1.5MiB/d | - | 0.01 |

- `dev/scratch`: R2 0.06 below --min-r2 0.50; treat the forecast as a hint
- `prod/metrics-data`: refit on the 84 samples after the resize at 2025-10-05 21:53 UTC
- `staging/log-archive`: flat or shrinking
- `staging/redis-cache`: flat or shrinking
