# RewindAI — Watch any app. Pick the best version. Fix with free local AI.

A developer ships **v2**. It looks fine in CI, then buyers hit bugs. RewindAI **watches the release without code changes**, rolls back to the **best** scored version (or lets you pick among many), shows **where the bugs are**, and can ask a **free local Ollama model** for a patch that ships as **v3** through the same checks.

**GatePass** (flash-sale tickets) is the first registered app. **Notes** is a tiny second app with no metrics code, to prove the watcher is zero-instrumentation.

```text
Register app → publish version → watch (docker stats + HTTP probes)
  → healthy: score headroom, maybe become BEST
  → anomaly: quarantine, bug report, roll back to BEST
  → optional: Fix with AI → review diff → ship next version → verify again
```

## Quick demo

Prerequisites: Docker Desktop, Python 3.9+, `curl`. For the AI chapter: Homebrew + ~5 GB disk for Ollama.

```bash
chmod +x scripts/*.sh

# One-time AI setup (Mac GPU, not Docker)
./scripts/ai-setup.sh

# Build stack, register GatePass + notes, publish healthy v1
./scripts/scenario.sh prepare

# Ship buggy GatePass v2, auto-rollback, AI fix → v3
PAUSE=1 ./scripts/scenario.sh live
```

Open:

| URL | What |
|-----|------|
| http://127.0.0.1:8090/ | RewindAI apps console |
| http://127.0.0.1:8090/apps/gatepass | GatePass versions, bugs, AI fix |
| http://127.0.0.1:8080/ | GatePass buyers |
| http://127.0.0.1:3000/d/rewindai-overview | Grafana (`admin` / `admin`) |

GatePass `/console` redirects to RewindAI.

## CLI

```bash
./scripts/rewind.sh register --id myapp --name MyApp --host-port 8082 --probes /,/health
./scripts/rewind.sh publish myapp --version 1.0.0 --dir ./path/to/source
./scripts/rewind.sh publish myapp --version 2.0.0 --base 1.0.0 --edits ./bugs.json
./scripts/rewind.sh status myapp
./scripts/rewind.sh pick myapp 1.0.0
./scripts/rewind.sh fix myapp
./scripts/rewind.sh ship myapp --version 3.0.0
```

## Architecture

| Piece | Role |
|-------|------|
| `rewind/` service (`rewindai` on `:8090`) | Registry, publish, verify, rank, bug report, Ollama fix, console |
| Docker volume `rewind-data` | Apps, releases, source trees, models (not on the USB) |
| Watcher | `docker stats` + probes on registered paths — no app SDK |
| Isolation Forest / thresholds | Per-app verify window; majority vote → anomaly |
| Ollama on the Mac | `qwen2.5-coder:7b` via `host.docker.internal:11434` |
| Prometheus + Grafana | Scrapes `rewind_*` and GatePass `/metrics` |
| Legacy `detector` + `scripts/deploy.sh` | Still available for the single-app GHCR CI path (`--profile legacy`) |

## Ranking and rollback

Each healthy verify gets a **headroom score** (0–100) from how far peaks sit under the limits (CPU 80%, memory 80%, p95 1000 ms, errors 5%). **BEST** is the highest-scoring healthy non-quarantined release; within 5 points the newer version wins. Automatic rollback and **Revert to best** both use that choice.

## AI fixes

Planted bugs and AI patches use the same edit format:

```json
[{"file": "catalog.py", "search": "exact text", "replace": "fixed text"}]
```

Nothing ships until you review the diff. Ship publishes the next version from the bad release plus those edits, then verifies again.

## Tests

```bash
cd app && PYTHONPATH=. pytest -q
cd ../rewind && PYTHONPATH=. pytest -q
```

## Limitations

RewindAI rolls back the **application release**, not irreversible data. Out of scope: canary splitting, traffic replay, real payments, DB migration rollback. Image-only publishes get signals and logs but not diff/AI fix (need source).
