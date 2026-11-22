# Harness benchmark: fishing game task

The idea of comparing coding agents on a pixel-art fishing game comes from
Otis' AI Lab, in [this video](https://www.youtube.com/watch?v=V8M9uXxAsP0). The
task prompt is derived from the one in the video, changed substantially to
remove ambiguity that made results vary between runs and wording aimed at
particular harnesses.

Implementation of [plan.md](plan.md): run environment, evaluator and rating kit.

```
hc                      run wrapper: build, run, eval, schedule, batch
task/prompt.txt         the task prompt, byte for byte from plan.md (sha256 recorded in each run)
task/history/           earlier prompt versions; task/prompt-versions.json maps each hash to a version
config/benchmark.env    provider, thinking level, wall-clock limit, images (local/benchmark.env overrides it)
config/rerun-policy.md  rerun rule (draft, confirm before the first counted run)
docker/                 base image (Node 24.22.3, Python 3.11, git, Playwright 1.65.1 + Chromium),
                        agent image (base + harnesses), evaluator image (base + ffmpeg)
egress/                 per-run forward proxy: allowlist + JSON log of every outbound request
harnesses/<name>/       install.sh, run.sh, VERSION, collect.sh (paths to copy out as the trace)
providers/<name>/       model backend: provider.env (model, hosts, secrets) + optional <harness>.sh setup hook
local/                  gitignored: private providers (local/providers/<name>/) and local/benchmark.env
evaluator/              Playwright session script (evaluate.mjs) and container entry
rating/                 prepare.py (blinding), serve.py + static/ (launcher UI), report.py
fixtures/               reference game and a deliberately broken copy, for testing the evaluator
runs/<run-id>/          one folder per run (see below)
```

## Quick start

```sh
./hc build                                   # base, agent and evaluator images; writes config/images.lock.json
cp -r providers/example local/providers/mine # then edit provider.env (see providers/README.md)
echo PROVIDER=mine > local/benchmark.env
export ANTHROPIC_API_KEY=...                 # every name in the provider's SECRET_ENV must be set on the host
./hc run --harness pi                        # one run, then the evaluator
./hc schedule --harnesses pi,other --blocks 8 && ./hc batch   # interleaved blocks, resumable
python3 rating/report.py --functional-only   # functional layer only, before any rating
```

Blind rating, after all runs:

```sh
python3 rating/prepare.py --practice path/to/practiceA path/to/practiceB
python3 rating/serve.py                      # open http://127.0.0.1:8100/
python3 rating/report.py                     # only works once both passes are locked
```

The step bar under the header shows where the rater is. Finished steps of a
pass that is still open can be reopened to review and edit scores; steps not
reached yet stay closed, and pass 2 opens only after pass 1 is locked. Once
both passes are locked, the rating page shows the results: which run and
harness every clip came from, the rater's own ranking of the runs, and a page
per run with its three clips, its game and the scores given.

Sharing the rating as a static site (GitHub Pages or any file host):

```sh
python3 rating/export_static.py              # writes rating/site/ (~360 MB)
python3 rating/report.py --ratings ratings-alex.json   # report from a rater's exported file
```

The static site is the same page without `serve.py`: each rater's ratings are
kept in their browser (Export / Import buttons in the header), and the file
they export is what they send back. The reveal data is bundled in
`site.json`, so the static site is not sealed; it is for sharing, not for a
blind study. To publish, push the contents of `rating/site/` to a repository
and turn on GitHub Pages for it (`.nojekyll` is included).

## What a run does

1. Creates an internal Docker network for the run and an egress proxy container
   on it. The proxy is the only way out. It allows the package registries in
   `egress/allowlist.txt` plus `MODEL_HOSTS`, and logs every request,
   allowed or blocked.
2. Creates a new agent container from `hc-agent` (non-root `agent` user, fresh
   home), mounts the provider folder read-only at `/opt/provider`, passes the
   secrets by name, and starts it. The entrypoint makes `/home/agent/work` with
   `git init` and one empty commit, sources the provider's `<harness>.sh` hook
   if there is one, then execs `harnesses/<name>/run.sh`.
3. Waits up to `WALL_CLOCK_MINUTES`, then kills the container.
4. Copies out the workspace, the harness trace and the egress log, removes the
   containers and network, and runs the evaluator in a separate container with
   `--network none` on a read-only mount of the workspace.

Port 8000 is always free because each agent has its own network namespace.
Internal Docker networks cannot publish ports, and nothing on the host needs to
reach a running agent, so nothing is mapped. The evaluator serves its own copy.

`runs/<run-id>/` holds:

| File | Contents |
| --- | --- |
| `meta.json` | harness, version, model, prompt hash, image IDs, start/end, status (`completed`, `error`, `timeout`, `infra-error`), blocked hosts |
| `transcript.jsonl` | harness stdout (Pi: its JSON event stream) |
| `agent.stderr.log` | harness stderr |
| `trace/` | harness session files (Pi: `~/.pi/agent/sessions`) |
| `egress.jsonl` | every outbound request with `allowed` true or false |
| `workspace/` | the final workspace, including `.git` |
| `eval/results.json` | every check with status and detail, pass rate, showcase timestamps, timeline |
| `eval/session.mp4`, `eval/showcase.mp4` | full recording and the showcase cut (H.264, plays on iOS) |
| `eval/screenshots/` | `idle.png`, `ambience.png`, `bite.png`, `reveal-<tier>.png` for all six tiers |
| `eval/console.log`, `eval/requests.json` | browser console and every request the page made |
| `overrides.json` | optional, hand-written: manual review outcomes, e.g. `{"checks": {"code_art": "pass"}, "note": "..."}` |

## Evaluator notes

The session runs in the order in plan.md. Some details the plan leaves open:

- **State sampling:** besides the 50 ms driver poll, an in-page 5 ms sampler
  records every `state` transition, so a short `reeling` phase is not missed by
  the Playable loop gate.
- **Space at `ready` that does nothing:** the script focuses the canvas and
  presses once more (for games that need focus or dismiss an instruction
  first), and notes it in `results.json`.
- **Statuses:** `pass`, `fail`, `unverifiable`, `skipped` (after a gate
  failure), `review` (Code-drawn art hits waiting for manual review). The
  functional pass rate counts only `pass`; `review` becomes a pass only through
  `overrides.json`.
- **No audio:** an init script wraps `AudioContext` (and its webkit and
  offline variants), `Audio`, `HTMLMediaElement.play`, `createElement('audio')`
  and `speechSynthesis.speak` before the page loads, then reads what was used
  at the end. The wrappers behave like the originals. Any use fails the check.
  Audio files are caught by Code-drawn art.
- **Code-drawn art:** decided after the session from what the page actually
  loaded. An asset file triggers review only if the page requested it or a
  file the page loaded names it; a source hit only if it is in a file the page
  loaded. The agent's own test screenshots and scripts are listed under
  `unused_asset_files` and `hits_in_files_not_loaded` but do not count. Assets
  under `node_modules/` never trigger review. URLs to `127.0.0.1`, `localhost`
  and `www.w3.org` (SVG namespaces) are ignored.
- **Interface:** checked at load, and again at the end against every state
  value seen during play; a value outside the five (e.g. an extra `"cast"`
  phase) fails it.
- **Log timing (prompt v4+):** the page tracker samples `log.length` with the
  state; `log_timing` fails if a catch enters the log during `waiting`, `bite`
  or `reeling`. It is scored only for runs whose prompt hash is marked
  `"log_timing": "scored"` in `task/prompt-versions.json` (hc passes
  `HC_LOG_TIMING`); for older runs it is reported but not counted.
- **Hidden-tab pause:** after the cast it waits up to 3 s for `waiting` (some
  games play a short cast animation first), then hides the tab for 20 s.
- **Layout:** overflow past 1280×720 fails only on an axis the viewer can
  scroll; content clipped by `overflow: hidden` on the root or body is
  counted in `clipped_overflow_seen` but passes.
- **Game in a subfolder:** if the workspace has no `index.html` but exactly
  one top-level folder has one (e.g. the agent built everything in `fishing/`),
  the evaluator serves that folder as if it were the root, and the rating kit
  uses it too. The reported `game_folder` check and `served_from` in
  `results.json` record it; it costs no points. Evaluator version 9.
- **Showcase order and hook timing:** the first natural catch is hooked at
  once (the gate), then comes the miss, which measures the bite window; after a miss the
  evaluator waits 2.5 s before casting again so the game's miss feedback is
  recorded. The
  second and third catches wait 40% of that window (at most 0.7 s) before
  pressing, so the bite cue is on screen; cycle time subtracts that pause.
- **Adding runs to a session:** `prepare.py --append` adds runs that are not in
  the session yet. Rated clips keep their place; unrated and new clips are
  shuffled together after them, with new repeats to keep about one in eight.
  Phases with new clips show their overview grid again (now with every run).
  Refused once the overall board or pass 2 has ratings. Restart `serve.py`
  afterwards; it reads the plan at startup.
- **Refreshing a session:** after re-evaluating runs, `prepare.py --refresh
  catch,rarity,overall` re-cuts those phases' clips in the existing session
  and keeps every ID, order and rating. It refuses if any of those clips are
  already rated.
- **Overlapping casts:** passes when the log grows by at most one and by no
  more than the number of bite windows seen.
- **Pixel grid (reported):** passes if a 320×180 canvas is shown at exactly
  1280×720. Otherwise it scores how well colour changes line up with a 4 px
  grid (1 = perfect, about 0.3 for smooth-scaled art), using the canvas's own
  pixels when the canvas is drawn at full size, or the idle screenshot when not.
  It passes at 0.6 or higher.
- **Fallback:** without a usable `window.fishing.state`, the play checks are
  marked `unverifiable` and `manual_showcase_required` is set. Put the
  hand-recorded clip at `eval/showcase-manual.mp4` and `prepare.py` uses it.

Tested against `fixtures/reference` (13/14; the one non-pass is a correct
review hit on the URL in its disabled debug fetch) and `fixtures/broken` (3/14,
with each injected fault caught by the check meant to catch it).

## Rating kit notes

- `prepare.py` gives each run a different random 5-letter ID in each pass,
  shuffles, adds `round(n/8)` repeats per pass under new IDs (spread apart),
  copies clips, thumbnails and games under their IDs, and writes the mapping
  to `rating/session/sealed/mapping.json`. Runs with a gate failure are not
  shown; they are scored tier 1 and 1 on every axis.
- Pass 1 runs in phases, each across all runs before the next: ambience
  (6 s untouched at `ready` after the natural catches, so start screens are
  gone, ~6 s), catch (one catch hooked after a human-like pause, then one miss with its aftermath, ~15 s), rarities (the
  six forced reveals, ~15 s), then an overall tier board. `prepare.py` cuts
  the phase clips from each run's `session.mp4` with the evaluator's timeline
  marks (`bite:*`, `reveal:*`, `cycle-*`); runs without marks get the whole
  showcase in every phase. Each phase has fresh IDs, its own order and its own
  repeats, and opens with an unlabelled grid of that phase's screenshots.
- Clips have normal video controls; the form unlocks the first time the clip
  reaches the end.
- The tier board shows each run's ambience and Ancient screenshots with its full
  clip on demand. A tier 2+ away from the run's scaled axis mean asks for an
  optional note. Phase scores can be reviewed and edited from the board
  before pass 1 is locked.
- Pass 2 is blocked until pass 1 is locked, starts with the practice games,
  and serves each game at `http://127.0.0.1:8010/` (`--game-port` to change)
  in a 1280×720 frame with a timer. It ends with a review table where any
  game can be replayed and rescored before locking.
- Every rating keeps its first-entered scores (`first`) and an edit count, so
  the report can show calibration drift.
- Both forms have a "shows a harness name" flag; flagged runs are left out of
  the analysis.
- `report.py` refuses to read the mapping until both passes are locked. It
  reports per-harness means with 95% bootstrap intervals over runs, the share
  of tiers 4–5, gate failures, rater agreement on the repeats (per phase), the
  correlation between tier and axis mean (with every flagged mismatch and its
  note), and how far scores moved between first entry and lock.
