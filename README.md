# RewindAI — Watch any app. Pick the best version. Fix with free local AI.

A developer ships **v2**. It looks fine in CI, then buyers hit bugs. RewindAI **watches the release without code changes**, rolls back to the **best** scored version (or lets you pick among many), shows **where the bugs are**, and can ask a **free local Ollama model** for a patch that ships as **v3** through the same checks.

**GatePass** (flash-sale tickets) is the first registered app. **Notes** is a tiny second app with no metrics code, to prove the watcher is zero-instrumentation.

```text
Register app → publish version (CLI or GitHub push) → watch (docker stats + HTTP probes)
  → healthy: score headroom, maybe become BEST
  → anomaly: quarantine, bug report, roll back to BEST
  → optional: Fix with AI → review diff → ship next version → verify again
```

## Start and stop

Prerequisites: Docker Desktop, Python 3.9+, `curl`. For AI fixes: Homebrew and about 5 GB of disk for Ollama.

```bash
chmod +x scripts/*.sh

./scripts/ai-setup.sh        # once: native Ollama on the Mac GPU + qwen2.5-coder:7b
./scripts/start.sh           # wakes Docker, builds on first run, starts Ollama, seeds GatePass and Notes 1.0.0
./scripts/stop.sh            # stops the stack; releases, recordings and Ollama stay
```

`start.sh --build` rebuilds the images after code changes. `start.sh --fresh` wipes the registry and seeds 1.0.0 again.

Open:

| URL | What |
|-----|------|
| http://127.0.0.1:8090/ | RewindAI apps console |
| http://127.0.0.1:8090/apps/gatepass | GatePass versions, bugs, compare, replay, AI fix |
| http://127.0.0.1:8080/ | GatePass buyers |
| http://127.0.0.1:3000/d/rewindai-overview | Grafana (`admin` / `admin`) |

## The demo

```bash
./scripts/scenario.sh prepare      # start.sh --fresh --build
PAUSE=1 ./scripts/scenario.sh live # ship buggy v2, watch the rollback, Fix with AI, ship v3
```

GatePass 2.0.0 is a believable feature commit: surge pricing, a seat-hold audit and receipt caching. Under live traffic it crosses three verify limits at once: CPU from the audit, p95 latency on GA checkout, and errors on FRT checkout from a missing surge tier. RewindAI rolls back to 1.0.0 in about 15 seconds, points at the slow and failing routes, and the AI fix ships as 3.0.0.

## The console

- **Hero and status band.** The headline says what is true right now: verifying, failing, rolled back, healthy. Live health and Last incident are separate cells, so a rollback stays visible after 1.0.0 is healthy again.
- **Verify banner.** While a release is in its window, a live bar shows seconds elapsed, majority votes and the latest sample. The page reloads with the verdict.
- **Where the bugs are.** Each crossed signal names its location: CPU at the app process, latency at the slowest probe path, errors at the failing one. Suspects come from tracebacks and the diff against BEST. The panel shows "Fixed in 3.0.0" once a newer release verifies healthy.
- **Compare versions.** Overlays every recorded verify window on seconds since deploy, with the limits as red dashes. 1.0.0 is ink, the failed release is red, later releases are dashed. Click a legend entry to hide a version.
- **Replay the incident.** Play at 4x, Step through events, or drag the scrubber. The charts follow the playhead and each step lights up: deployed, first bad vote, majority, logs, report, rollback, restored.

## CLI

```bash
./scripts/rewind.sh status [ID]
./scripts/rewind.sh register --id myapp --name MyApp --host-port 8082 --probes /,/health
./scripts/rewind.sh publish myapp --version 1.0.0 --dir ./path/to/source --wait
./scripts/rewind.sh publish myapp --version 2.0.0 --base 1.0.0 --edits ./bugs.json --wait
./scripts/rewind.sh watch myapp 2.0.0
./scripts/rewind.sh pick myapp 1.0.0
./scripts/rewind.sh fix myapp                 # waits for the proposal and prints it
./scripts/rewind.sh ship myapp --wait         # next major version by default
./scripts/rewind.sh connect-github myapp --repo owner/name --path app
./scripts/rewind.sh disconnect-github myapp
```

Every error says what to do next, for example "RewindAI is not running. Start it with ./scripts/start.sh".

## Deploy from GitHub

```text
push to main → CI runs both test suites → green moves the release branch
  → RewindAI polls release every 30 s → the app folder changed → build, deploy, verify
```

1. Push this repo. The CI workflow creates and moves `release` whenever main is green.
2. Connect the app: `./scripts/rewind.sh connect-github gatepass --repo Soham11k/Rollback --path app`. The branch head at connect time is the baseline, so nothing redeploys.
3. Bump `app/VERSION` (for example to `1.1.0`), commit and push. RewindAI publishes 1.1.0 from that commit and verifies it. The releases table links the commit.

Commits that leave the folder unchanged are skipped. A reused VERSION gets the short commit hash appended. RewindAI waits while another verify runs. Branch heads come from git smart HTTP and code from codeload tarballs, so polling stays clear of the GitHub API rate limit. Set `GITHUB_TOKEN` before `start.sh` for private repos.

## CI

`.github/workflows/ci.yml` runs on every push and pull request:

- **GatePass tests** and **RewindAI tests** on Python 3.11.
- **Promote to release** on pushes to main after both pass: `git push origin HEAD:release --force`.

`.github/workflows/deploy.yml` is the legacy single-app GHCR path and runs only by hand (it needs a self-hosted runner).

## Architecture

| Piece | Role |
|-------|------|
| `rewind/` service (`rewindai` on `:8090`) | Registry, publish, verify, rank, bug report, Ollama fix, GitHub poller, console |
| Docker volume `rewind-data` | Apps, releases, source trees, recordings, models (not on the USB) |
| Watcher | `docker stats` + probes on registered paths every 2 s — no app SDK |
| Isolation Forest / thresholds | Per-app verify window; majority vote → anomaly |
| Recordings | `recording.json` per release: verify samples and timeline events for compare and replay |
| Ollama on the Mac | `qwen2.5-coder:7b` via `host.docker.internal:11434`, 16k context |
| Prometheus + Grafana | Scrapes `rewind_*` and GatePass `/metrics` |
| Legacy `detector` + `scripts/deploy.sh` | Single-app path (`--profile legacy`, manual workflow) |

## Ranking and rollback

Each healthy verify gets a **headroom score** (0–100) from how far peaks sit under the limits (CPU 80%, memory 80%, p95 1000 ms, errors 5%). **BEST** is the highest-scoring healthy non-quarantined release; within 5 points the newer version wins. Automatic rollback and **Revert to best** both use that choice.

## AI fixes

Planted bugs and AI patches use the same edit format:

```json
[{"file": "catalog.py", "search": "exact text", "replace": "fixed text"}]
```

The prompt carries the crossed signals, tracebacks, suspects, the diff against BEST and the full changed files. When an edit does not apply, the model gets one retry with the error and the exact lines of the enclosing function. Search blocks that only lost their indentation still apply. Nothing ships until you review the diff; Ship publishes the bad release plus those edits as the next version and verifies it again.

## Tests

```bash
cd app && python seed.py && PYTHONPATH=. pytest -q
cd ../rewind && PYTHONPATH=. pytest -q
```

The RewindAI suite covers the verify loop with a fake watcher (rollback to BEST, recordings, busy guard), the AI fix with Ollama mocked (repair retry, ship), and the GitHub poller with HTTP mocked (baseline, folder changes, versions, safe extraction).

## Limitations

RewindAI rolls back the **application release**, not irreversible data. Out of scope: canary splitting, traffic replay, real payments, DB migration rollback. Image-only publishes get signals and logs but not diff/AI fix (need source).
