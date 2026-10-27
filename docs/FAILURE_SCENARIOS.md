# RewindAI Failure Scenarios

These scenarios show how RewindAI behaves under different post-deploy failures on the GatePass flash-sale checkout.

Run after `./scripts/scenario.sh prepare` so a healthy baseline model exists.

```bash
./scripts/failure-scenarios.sh list
./scripts/failure-scenarios.sh run <id>
./scripts/failure-scenarios.sh run suite
```

| ID | Fault injected | What buyers see | Expected RewindAI action |
|----|----------------|-----------------|--------------------------|
| `cpu` | Background CPU burn | Checkout slows / stalls | Anomaly → rollback to v1 + quarantine |
| `memory` | Large buffers held | Pressure + instability | Anomaly → rollback to v1 + quarantine |
| `latency` | ~2.5s sleep per request | Checkout feels stuck | Anomaly → rollback to v1 + quarantine |
| `errors` | ~15–20% HTTP 500 | Some checkouts fail | Anomaly → rollback to v1 + quarantine |
| `all` | Combined | Sale effectively down | Anomaly → rollback to v1 + quarantine |
| `healthy` | `FAULT_MODE=none` | Normal checkout | Promote; **no** quarantine |
| `spike` | Brief blip already fixed | Transient noise | No rollback (majority not met) |

Story beat:

1. **Flash sale, v1.0 stable** — Isolation Forest learned normal CPU, memory, latency, error rate.
2. **Rushed v2 ships** — `/health` stays green; checkout path is where faults show.
3. **RewindAI watches** — majority vote on anomalous samples triggers rollback and quarantine.
4. **Console** — incident card shows time-to-detect, time-to-rollback, peaks, and most-affected endpoint.
