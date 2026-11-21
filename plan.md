# Harness Benchmark Spec: Fishing Game Task

## Purpose and scope

This benchmark compares coding-agent harnesses on one task, with the model, server, prompt and budget held fixed. Each harness runs as shipped, so its system prompt, tools, context handling and browser support are what is being measured.

Results come in two separate layers: scripted functional checks and blind rating by a single rater, first from the showcase videos and then from live play. They hold only for this model, quantization and prompt, and do not show which harness is best in general.

There are three parts to build, in the order a run passes through them:

1. **Run environment:** a container that starts each harness on an empty workspace.
2. **Evaluator:** plays each finished game with a fixed script and writes results, screenshots and video.
3. **Rating kit:** serves each clip, then each game, to the rater under random IDs and records the tiers and scores.

**First milestone:** one complete run with Pi and a single model provider, taken through the run environment and the evaluator. More runs, more harnesses and human rating come after that works end to end.

## Task prompt

Every harness receives this text, byte for byte, as its only user message.

```text
Build a finished, playable, top-down pixel-art fishing game that runs in the browser, inspired by the warmth and visual detail of cozy farming games like Stardew Valley. All setting, characters, fish and artwork must be original. This is a visual-first task: it should feel like a tiny game someone would want to play, not a tech demo or placeholder scene.

SCENE
One island fishing spot: a small angler on a dock or shoreline, animated water, a readable shoreline, lush vegetation and a few well-chosen environmental details. A rich but controlled palette, clear silhouettes, shadows and depth. Subtle motion in the water, bobber and surroundings. 2D only. The angler stays in one place.

Render the scene on a 320×180 logical canvas scaled up 4× with no smoothing, so every art pixel is the same size. The angler is roughly 16–24 logical pixels tall. The scene fills a 16:9 layout that fits a 1280×720 viewport without scrolling. Controls and catch information stay compact and legible, covering no more than about a fifth of the screen; UI text may be drawn at full resolution.

GAME LOOP
Show a short instruction. Space and a visible on-screen button do the same action:
1. Cast: the line is visibly thrown and the bobber lands in the water.
2. After a short, variable wait, a bite: a clear bobber reaction and an unmistakable cue.
3. Press once during a generous, visibly indicated bite window to reel in. Show a brief catch animation and a satisfying reveal, then allow the next cast.
Missing the window loses the bite and returns to ready. No overlapping casts or duplicate catches from repeated key presses. A successful cycle takes roughly 5–9 seconds. Timers pause while the tab is hidden (document.hidden). No sound or audio of any kind.

RARITIES
Each successful catch picks one rarity with exactly these odds, with no pity rules, rerolls or history-dependent changes:
Common 45%, Uncommon 25%, Rare 15%, Epic 8%, Legendary 5%, Ancient 2%.
Each tier has at least one original named fish with recognizable artwork. The reveal shows the fish, its name and the rarity word, with a visual treatment that differs by tier in more than color. Ancient should feel special but fit the same art style. A compact log shows the last eight catches, newest first; misses are not logged. The new catch is added to the log only once its reveal is on screen; until then nothing on screen gives away which fish was caught. A small legend shows the six odds.

TEST INTERFACE
Expose a global window.fishing object with:
- state: the current phase, one of "ready" (a cast is possible), "waiting" (line cast, no bite yet), "bite" (the bite window is open), "reeling" (catch animation), "reveal" (the caught fish is shown).
- pickRarity(u): a pure function that takes a number in [0, 1) and returns one of the six rarity names exactly as spelled above. For every successful catch the game must call window.fishing.pickRarity(Math.random()) through this object, so that replacing the function changes the outcome.
- log: every successful catch this session (not just the last eight) as an array of { name, rarity }, newest first. The entry is added when the state becomes "reveal", not before.
The interface must not change ordinary play or add visible debug elements.

CONSTRAINTS
- Draw all art in code. No downloaded, generated or embedded image files, emoji, icon sets, fonts or game assets. Small code libraries are fine, installed inside this project folder.
- Node, Python 3 and Playwright (headless Chromium) are already installed globally; use them rather than installing another browser.
- The game is served as static files with `python3 -m http.server 8000 --bind 127.0.0.1` from the current folder and loads at http://127.0.0.1:8000/ with no network access.
- Out of scope: movement, maps, inventory, saving, shops, quests, combat, upgrades.

DONE MEANS
Work autonomously: make reasonable choices and don't ask questions. Before finishing, play the game end-to-end in a real browser (cast, bite, catch, miss, repeat), look at how it actually renders, and fix what you find. Finish with a short summary: how to launch it, what you tested, and any known issues.
```

The prompt deliberately says nothing about compaction, progress files, specific browser tools or other contenders. Those are either harness behaviour being measured or rules enforced by the environment.

The prompt reads as something a user might write, perhaps with help from a stronger model, but every requirement a scored check depends on must have only one sensible reading. The benchmark measures harnesses, not how a model resolves ambiguous wording, so when pilot runs show agents splitting on a reasonable reading, the prompt gets a short clarification in the user's own voice rather than a longer specification. Pilot runs made before such a change are kept as pilots and not counted.

## Run environment

Every run starts from the same container image and an empty workspace, and talks to the model server directly.

**Workspace**

- A fresh folder with `git init` and one empty commit, and nothing else.
- No `AGENTS.md`, `CLAUDE.md` or similar instruction files, since harnesses read different ones.
- The evaluator's scripts never enter the workspace.

**Container**

- One image for all runs: pinned Node, Python 3, git, and Playwright with headless Chromium installed globally.
- Each harness installed at a pinned version with its default configuration.
- A new container per run, run as a non-root user, with a fresh home directory. This clears any harness memory, session history or cache.
- Port 8000 is always free inside the container. Parallel runs map it to different host ports.
- Network limited to package registries, with all outbound traffic logged.
- A wall-clock limit per run, enforced by the run wrapper.

**Model server**

The model server is a black box to this benchmark. Every harness points at the same endpoint, and the server's configuration and logs are outside this spec.

## Evaluator

The evaluator is one Playwright script that plays every finished game the same way, driven by `window.fishing.state`. It runs outside the agent's container, on a read-only copy of the final workspace, using the same pinned Chromium version at 1280×720.

**Session order**

1. Copy the final workspace, start `python3 -m http.server 8000 --bind 127.0.0.1` in it, and launch Chromium with video recording on.
2. Scan the files for bundled assets.
3. Load the page with listeners attached for console messages, page errors and network requests.
4. Check the interface, save the original `pickRarity`, and run the odds sweep.
5. Showcase segment: 5 seconds idle, one natural catch hooked at once (the Playable loop gate), one deliberate miss (which also measures how long the bite window stays open), two more natural catches hooked after a human-like pause (40% of the measured window, at most 0.7 s, so the bite cue is visible), 6 seconds untouched at `ready` (the ambience shot, taken once any start screen has been dismissed), then six forced catches in order from Common to Ancient.
6. Robustness segment: duplicate input, overlapping casts, hidden-tab pause.
7. Write `results.json`, screenshots and video, then shut down.

**How the script plays**

- **Driving a cycle:** press Space at `ready`, then poll `state` every 50 ms.
- **Catching:** press Space once when `state` becomes `bite`. For a miss, press nothing.
- **Timeouts:** 20 s for `waiting` to `bite`, 10 s for `reeling` to `reveal`, 8 s for `reveal` to `ready`.
- **Reveal that needs dismissing:** if `reveal` lasts past 8 s, press Space once and record that the reveal needed dismissal.
- **Forcing a tier:** set `window.fishing.pickRarity = () => "Ancient"` (or another tier), play one catch, then restore the original.
- **Recording input:** during natural catches, wrap the original `pickRarity` to log each `u` it receives.

**Checks**

A gate failure means the game is not playable and later checks are skipped. Scored checks make up the functional pass rate. Reported checks are measured and shown but not scored.

| Check | Method | Type |
| --- | --- | --- |
| Loads | Page loads within 10 s and shows a canvas or game root | Gate |
| Playable loop | One natural catch passes through `ready`, `waiting`, `bite`, `reeling`, `reveal`, `ready` | Gate |
| Interface present | `window.fishing` has `state`, `pickRarity` and `log` | Scored |
| No runtime errors | No uncaught exceptions or console errors during the session | Scored |
| Offline | No request leaves 127.0.0.1 | Scored |
| No audio | The script wraps the browser's audio constructors before the page loads and records any use | Scored |
| Code-drawn art | No image, font or media files in the workspace; no `<img>`, `data:image` or external URLs in source. Hits go to manual review | Scored |
| Layout | At 1280×720, `scrollWidth` ≤ `innerWidth` and `scrollHeight` ≤ `innerHeight` | Scored |
| Odds | Call the original `pickRarity` with 10,000 evenly spaced `u`. Expect 4500, 2500, 1500, 800, 500, 200, each within 1, exactly six names, each tier one continuous range | Scored |
| Log | After each catch, `log` grows by one and `log[0]` is the new catch with a valid rarity | Scored |
| Miss | No press during `bite` returns to `ready` with `log` unchanged | Scored |
| Duplicate input | Five rapid Space presses in the bite window add exactly one catch | Scored |
| Overlapping casts | Rapid presses at `ready` never produce more than one catch per bite | Scored |
| Hook is used | For all six tiers, the forced tier equals `log[0].rarity`. Failure marks the odds as unverifiable | Scored |
| Fresh random input | Wrapped `pickRarity` is called once per catch with distinct `u` in \[0, 1) | Scored |
| Hidden-tab pause | Simulate a hidden tab for 20 s during `waiting`. State stays `waiting` and the next bite is still catchable | Scored |
| Log waits for reveal | The log never grows while the state is `waiting`, `bite` or `reeling`; the log length is sampled with the state every frame | Scored from prompt v4; reported for earlier runs |
| Cycle time | Seconds from cast press to `reveal` over the natural catches (the deliberate hook pause subtracted), against the 5–9 s target | Reported |
| Pixel grid | Canvas is 320×180 scaled 4×, or colour edges in a screenshot align to a 4 px grid | Reported |
| On-screen control | A DOM button is found and clicking it casts. If none is found, flag for a manual check | Reported |

**Known limits**

- **Hidden tab is simulated:** the script overrides `document.hidden` and fires `visibilitychange`. The prompt names `document.hidden` so the check tests what was asked; games that pause only through the browser's own frame throttling will not react and fail.
- **Canvas-drawn UI cannot be read:** the visible log, legend and on-screen button are left to the rater's judgement when they are not DOM elements.
- **Headless video can drop frames:** smooth animation may look worse in the clip than in play. The played pass is the check on animation.

**Fallback**

If `window.fishing.state` is missing, the script cannot play. Record the interface checks as failed and have a person record the showcase by hand in the same order, without the forced tiers.

**Outputs per run**

- `results.json`: each check with pass, fail or unverifiable, plus measured values and the functional pass rate (scored passes over scored checks).
- `session.webm`: the full recording, and `showcase.webm` cut from the showcase segment.
- Screenshots: the idle scene (as first loaded), the ambience scene, a bite, and each of the six reveals.
- `console.log` and the list of network requests.

## Human rating

One person rates every run blind, in two passes: visuals from the showcase videos, then feel from live play. The scripted checks above are kept out of both.

**Blinding**

- A script assigns each run a different random ID for each pass, shuffles the order, and keeps both mappings sealed until all scoring is locked.
- A launcher serves one clip or game at a time by ID, so the rater never sees a folder name, source file or evaluator output.
- The rater does not open any game or clip before the first pass.
- If a clip or game shows a harness name in a title or credits, the rater flags it and it is left out of the analysis.

**Pass 1: visuals from video**

Every clip comes from the same evaluator script, so each run is shown doing the same things in the same order. Anyone can repeat this pass from the published clips.

Pass 1 is split into short phases, each rated across all runs before the next starts, so like is compared with like. Each phase has its own random IDs and order, and about one run in eight is shown twice under a new ID.

| Phase | Clip per run (cut from the session video) | Length | Axes |
| --- | --- | --- | --- |
| 1. Ambience | 6 s untouched at `ready` after the natural catches, so a start screen or instruction banner does not hide the scene; real time | ~6 s | Scene and world, Pixel-art quality, Cohesion and palette |
| 2. Catch | One successful catch hooked after a human-like pause (cast, bite cue, reveal and the log update), then one miss including 2.5 s of its aftermath, real time | ~15 s | Catch presentation, UI and readability |
| 3. Rarities | The six forced reveals, Common to Ancient, cut tight around each reveal | ~15 s | Rarity appeal |
| 4. Overall | Tier board: each run's ambience and Ancient screenshots, with its full showcase clip on demand | — | Tier |

1. Each phase opens with an unlabelled grid of every run's screenshot for that phase (ambience, bite, Ancient reveal), so the first scores are made knowing the range.
2. Watch each clip through at least once (replaying and scrubbing are allowed), then score that phase's axes from 1 to 7.
3. After the three phases, place every run in a tier. If the tier is 2 or more away from the mean of that run's axis scores scaled to 1–5, the board asks for a short reason but does not require one.
4. Before locking, any phase score can be reopened and changed, and any run moved between tiers. Lock the scores and tiers. Both the first-entered and the locked values are kept.

| Visual axis | 1 | 4 | 7 |
| --- | --- | --- | --- |
| Scene and world | Empty background | Water, shore and angler present but static | Layered scene with ambient motion |
| Pixel-art quality | Flat rectangles or placeholder shapes | Recognizable sprites, some inconsistency | Deliberate pixel art with readable silhouettes |
| Cohesion and palette | Clashing colours or mixed styles | Mostly consistent, a few stray elements | Unified palette, UI matches the world |
| Catch presentation | Can't tell when a bite happens or what was caught | Clear but plain | Unmistakable bite cue, a catch with payoff, and a miss that reads clearly |
| UI and readability | Overlapping or unreadable elements | Usable but cluttered | Clear hierarchy, log and rarity readable at a glance |
| Rarity appeal | Tiers look the same | Tiers labelled but not felt | Rarer catches look special, Ancient most of all, in the same style |

Each axis judges one thing, and a flaw counts only on the axis it belongs to; the form shows this under every axis. Scene and world is about depth and motion (a flat view with a few animated details is about 5; 7 needs real depth such as a horizon, mountains or islands), not drawing quality, which is Pixel-art quality. Cohesion is about style and palette (a UI in a clashing style keeps it near 4); UI layout, padding and overlaps go to UI and readability in the Catch phase. Anchors describe qualities, not ranks: several runs can share a score.

Responsiveness, input feel and glitches under repeated input are not judged from video: they belong to pass 2 and the functional checks.

The tier is the overall judgement of the run, not an average of the axes: it can register a single fatal flaw or an overall charm that the axes miss. The report shows how closely tier and axis mean agree.

| Tier | Meaning |
| --- | --- |
| 5 | Would show it to a friend: cohesive, charming and polished |
| 4 | Good: attractive, with minor flaws |
| 3 | Acceptable: complete but plain or uneven |
| 2 | Poor: recognizable but ugly, cluttered or glitchy |
| 1 | Unreadable or broken on screen (runs with a gate failure are placed here automatically) |

**Pass 2: feel from live play**

1. Play two practice games that are not in the study, to settle what the anchors mean.
2. Play each game for about a minute, long enough for a few catches and a miss, then score the three feel axes from 1 to 7.
3. Review the scores in a table, replay and rescore any game, then lock the scores and reveal both ID mappings.

| Feel axis | 1 | 4 | 7 |
| --- | --- | --- | --- |
| Cast, bite and catch feel | Inputs unclear, no feedback | Loop works with minimal feedback | Clear anticipation and payoff at each step |
| Polish | Frequent glitches or softlocks | Minor glitches | No noticeable glitches in normal play |
| Would keep playing | Would close it immediately | Would play a few minutes | Would show it to a friend |

**Failures and repeats**

- A run with a gate failure has no clip and cannot be played. It goes into tier 1 and scores 1 on every axis, so failures count against a harness.
- In each pass, about one run in eight is shown a second time under a new ID. Agreement between the two results is the only check on the rater's consistency.

**Scoring**

- From video, per harness: the mean score on each visual axis and the mean tier, each with an interval from resampling runs, plus the share of runs in tiers 4 and 5.
- From play, per harness: the mean score on each feel axis with an interval.
- Axes are reported separately, not summed.
- Small differences between harnesses stay unresolved, since one person's taste is the whole measurement.

## Run protocol and reporting

Each harness gets at least 5 runs, with 8 to 10 as the target, and every run counts.

**Running**

- **Independence:** each run is an independent sample, with nothing matched across harnesses.
- **Order:** interleaved in blocks, one run of each harness per block in shuffled order.
- **Failures:** a run that times out, crashes or produces nothing is scored as it stands, not rerun.
- **Reruns:** only for documented infrastructure faults such as a server crash, under a rule written before the first run.
- **No selection:** never pick the best of several runs for a harness.

**Reporting, per harness**

- Functional pass rate with an interval, and the count of gate failures.
- From video: mean tier and mean score on each visual axis, with intervals.
- From play: mean score on each feel axis, with intervals.
- The rater's agreement with their own repeats, for each pass.

Report the two layers side by side, not as one composite score. Describe harnesses as tiers where intervals overlap, and state that results apply to this model and task only.

**Publish**

- The prompt, container image definition, harness versions and configurations.
- The name of the model served at the endpoint.
- Every generated workspace, including failed runs.
- Evaluator scripts, `results.json` files, videos and screenshots.
- The raw tier placements and rating sheet, with the ID mappings revealed after scoring.

## Open decisions

These need an answer before the first run, because each one changes what is built or measured.

- [ ] Wall-clock limit per run.
- [x] Logical resolution: 320×180 at 4×.
- [x] Port: 8000 inside each container, mapped on the host for parallel runs.
- [ ] Which harnesses, at which versions.
- [ ] Runs per harness for the first batch.
- [ ] Pass bounds for cycle time, if it should become a scored check.
