#!/usr/bin/env python3
"""Prepare a blind rating session from finished, evaluated runs.

  python3 rating/prepare.py [--runs runs] [--session rating/session] [--include 'pi-*']
                            [--practice path/to/gameA path/to/gameB] [--seed N]

Pass 1 (visuals from video) is split into phases, each rated across all runs
before the next starts:

  ambience  6 s at rest after the natural catches      scene, pixel_art, cohesion
  catch     one successful catch (hooked after a short
            human-like pause), then one miss            catch, ui
  rarity    the six forced reveals, Common to Ancient   rarity
  overall   tier board: every run, full clip on demand  tier

Each phase opens with an unlabelled grid of every run's screenshot for that
phase, has its own random IDs and order, and shows about one run in eight a
second time under a new ID. Clips are cut from each run's eval/session.mp4
using the evaluator's timeline marks. Pass 2 (feel from play) serves each
game under another set of IDs.

Media are copied under their IDs so the rater never sees a folder name. The
ID -> run mapping goes to <session>/sealed/mapping.json, which nobody opens
until both passes are locked (rating/report.py refuses to read it before then).

Runs with a gate failure get no clip and no game: they are recorded as tier 1
and 1 on every axis without being shown.
"""
import argparse
import fnmatch
import json
import random
import secrets
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ALPHABET = "ACDEFGHJKLMNPQRTUVWXY3479"  # no look-alikes
TIERS = ["Common", "Uncommon", "Rare", "Epic", "Legendary", "Ancient"]
PHASES = [
    {"key": "ambience", "title": "Ambience", "axes": ["scene", "pixel_art", "cohesion"], "shot": "ambience.png",
     "hint": "The scene at rest, filmed after the first catches so any start screen is gone. No input is given."},
    {"key": "catch", "title": "Catch", "axes": ["catch", "ui"], "shot": "bite.png",
     "hint": "One successful catch, then one miss."},
    {"key": "rarity", "title": "Rarities", "axes": ["rarity"], "shot": "reveal-ancient.png",
     "hint": "The six reveals in order: Common, Uncommon, Rare, Epic, Legendary, Ancient."},
]


def new_id(rng, taken):
    while True:
        i = "".join(rng.choice(ALPHABET) for _ in range(5))
        if i not in taken:
            taken.add(i)
            return i


def spread_shuffle(rng, items, key, tries=500):
    """Shuffle so repeats of the same run are not close together, when possible."""
    best, best_gap = None, -1
    for _ in range(tries):
        order = items[:]
        rng.shuffle(order)
        pos = {}
        gap = len(order)
        for n, it in enumerate(order):
            k = key(it)
            if k in pos:
                gap = min(gap, n - pos[k])
            pos[k] = n
        if gap > best_gap:
            best, best_gap = order, gap
        if gap >= max(3, len(order) // 3):
            break
    return best


def phase_cuts(rdir, res):
    """({phase: [(file, start, duration)]}, marked) for one run, from the timeline marks.
    Without marks (or for a hand-recorded showcase) every phase gets the whole clip."""
    manual = res.get("manual_showcase_required")
    whole = rdir / ("eval/showcase-manual.mp4" if manual else "eval/showcase.mp4")
    t = {e["event"]: e["t"] for e in res.get("timeline", [])}
    src = rdir / "eval/session.mp4"
    start = (res.get("showcase") or {}).get("start")
    need = ["ambience-start", "ambience-end", "cycle-start:natural-2", "reveal:natural-2", "cycle-end:natural-2", "cycle-start:miss", "bite:miss", "cycle-end:miss",
            *[f"reveal:forced-{x}" for x in TIERS]]
    if manual or start is None or any(k not in t for k in need) or not src.exists():
        return {p["key"]: [(whole, 0.0, None)] for p in PHASES}, False
    # natural-2 is hooked after a human-like pause, so the bite cue is visible.
    nat_end = min(t["cycle-end:natural-2"], t["reveal:natural-2"] + 3.5)
    # The miss: a few seconds of waiting, the whole bite window, then the 2.5 s
    # the evaluator leaves after the game returns to ready (miss aftermath).
    miss_start = max(t["cycle-start:miss"], t["bite:miss"] - 4)
    miss_end = min(t["cycle-end:miss"], t["bite:miss"] + 9)
    return {
        "ambience": [(src, t["ambience-start"], t["ambience-end"] - t["ambience-start"])],
        "catch": [(src, t["cycle-start:natural-2"], nat_end - t["cycle-start:natural-2"]),
                  (src, miss_start, miss_end - miss_start)],
        "rarity": [(src, max(0, t[f"reveal:forced-{x}"] - 0.5), 2.5) for x in TIERS],
    }, True


def cut(segments, out):
    """Concatenate segments into one H.264 MP4 that plays on iOS."""
    norm = "fps=30,scale=1280:720,setsar=1,format=yuv420p"
    args, filters, labels = ["ffmpeg", "-y", "-loglevel", "error"], [], []
    for k, (f, a, d) in enumerate(segments):
        args += ["-ss", f"{a:.2f}"] + (["-t", f"{d:.2f}"] if d else []) + ["-i", str(f)]
        filters.append(f"[{k}:v]{norm},setpts=PTS-STARTPTS[s{k}]")
        labels.append(f"[s{k}]")
    filters.append(f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0[out]")
    args += ["-filter_complex", ";".join(filters), "-map", "[out]", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    subprocess.run(args, check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--session", default="rating/session")
    ap.add_argument("--include", default="*", help="glob on run IDs to include")
    ap.add_argument("--exclude", default="", help="comma-separated globs on run IDs to leave out")
    ap.add_argument("--practice", nargs="*", default=[], help="game folders for the two pass-2 practice games")
    ap.add_argument("--repeat-rate", type=float, default=1 / 8)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--refresh", default="", help="comma-separated phases (and/or 'overall') to re-cut in the existing "
                    "session from the current evaluations; IDs, order and every rating are kept")
    ap.add_argument("--append", action="store_true", help="add runs that are not in the existing session yet; "
                    "every rating is kept (see append())")
    a = ap.parse_args()
    if a.append:
        excludes = [g for g in a.exclude.split(",") if g]
        return append(Path(a.runs), Path(a.session), a.include, excludes, a.repeat_rate)
    if a.refresh:
        return refresh(Path(a.runs), Path(a.session), [x for x in a.refresh.split(",") if x])

    runs_dir, sess = Path(a.runs), Path(a.session)
    if sess.exists():
        if not a.force:
            sys.exit(f"{sess} exists; pass --force to replace it (this discards its ratings)")
        shutil.rmtree(sess)
    seed = a.seed if a.seed is not None else secrets.randbits(32)
    rng = random.Random(seed)
    excludes = [g for g in a.exclude.split(",") if g]

    runs, auto, problems, no_marks = [], [], [], []
    for meta_p in sorted(runs_dir.glob("*/meta.json")):
        rdir = meta_p.parent
        rid = rdir.name
        if not fnmatch.fnmatch(rid, a.include) or any(fnmatch.fnmatch(rid, g) for g in excludes):
            continue
        res_p = rdir / "eval/results.json"
        if not res_p.exists():
            problems.append(f"{rid}: not evaluated")
            continue
        res = json.loads(res_p.read_text())
        if res["gate_failed"] and not res.get("manual_showcase_required"):
            auto.append(rid)
            continue
        clip = rdir / "eval/showcase-manual.mp4" if res.get("manual_showcase_required") else rdir / "eval/showcase.mp4"
        if not clip.exists():
            problems.append(f"{rid}: missing {clip.relative_to(rdir)}")
            continue
        cuts, marked = phase_cuts(rdir, res)
        if not marked:
            no_marks.append(rid)
        runs.append({"run": rid, "dir": rdir, "clip": clip, "cuts": cuts, "game": rdir / "workspace"})
    if problems:
        sys.exit("cannot prepare:\n  " + "\n  ".join(problems))
    if not runs:
        sys.exit("no runs to rate")

    taken = set()
    mapping = {"seed": seed, "pass1": {}, "overall": {}, "pass2": {}, "auto_tier1": auto}
    plan = {"phases": [], "overall": [], "pass2": [], "practice": []}
    media = sess / "media"
    for d in ("clips", "thumbs", "games"):
        (media / d).mkdir(parents=True)
    n_rep = round(len(runs) * a.repeat_rate) if len(runs) >= 4 else 0

    def with_repeats():
        items = [{"src": r} for r in runs] + [{"src": r} for r in rng.sample(runs, n_rep)]
        return spread_shuffle(rng, items, key=lambda it: it["src"]["run"])

    cuts, copies = [], []  # cut in parallel at the end; repeats copy the first cut
    first_id = {}  # (phase, run) -> ID of its first showing
    for ph in PHASES:
        entry = {k: ph[k] for k in ("key", "title", "axes", "hint")}
        entry["ids"], entry["grid"] = [], []
        cut_once = {}
        for it in with_repeats():
            r, i = it["src"], new_id(rng, taken)
            k = (ph["key"], r["run"])
            m = {"run": r["run"], "phase": ph["key"], "repeat": k in first_id}
            if m["repeat"]:
                m["repeat_of"] = first_id[k]
            else:
                first_id[k] = i
            mapping["pass1"][i] = m
            entry["ids"].append(i)
            out = media / "clips" / f"{i}.mp4"
            if r["run"] in cut_once:
                copies.append((cut_once[r["run"]], out))
            else:
                cut_once[r["run"]] = out
                cuts.append((r["cuts"][ph["key"]], out))
        # Overview grid: one screenshot per run, unlabelled, in its own order.
        for n, r in enumerate(rng.sample(runs, len(runs))):
            src = r["dir"] / "eval/screenshots" / ph["shot"]
            if not src.exists() and ph["shot"] == "ambience.png":
                src = r["dir"] / "eval/screenshots/idle.png"
            if src.exists():
                name = f"grid-{ph['key']}-{n:02d}.png"
                shutil.copy2(src, media / "thumbs" / name)
                entry["grid"].append(name)
        plan["phases"].append(entry)

    # Overall tier board: fresh IDs, the full showcase clip, idle and Ancient
    # thumbnails, and the IDs of that run's phase ratings (for the mismatch prompt).
    for r in rng.sample(runs, len(runs)):
        i = new_id(rng, taken)
        mapping["overall"][i] = {"run": r["run"]}
        shutil.copy2(r["clip"], media / "clips" / f"{i}.mp4")
        thumbs = []
        for shot in ("ambience.png", "reveal-ancient.png"):
            src = r["dir"] / "eval/screenshots" / shot
            if not src.exists() and shot == "ambience.png":
                src = r["dir"] / "eval/screenshots/idle.png"
            if src.exists():
                shutil.copy2(src, media / "thumbs" / f"{i}-{shot}")
                thumbs.append(f"{i}-{shot}")
        plan["overall"].append({"id": i, "thumbs": thumbs, "links": [first_id[(ph["key"], r["run"])] for ph in PHASES]})

    p2_first = {}
    for it in with_repeats():
        r, i = it["src"], new_id(rng, taken)
        m = {"run": r["run"], "repeat": r["run"] in p2_first}
        if m["repeat"]:
            m["repeat_of"] = p2_first[r["run"]]
        else:
            p2_first[r["run"]] = i
        mapping["pass2"][i] = m
        plan["pass2"].append(i)
        shutil.copytree(r["game"], media / "games" / i, ignore=shutil.ignore_patterns(".git"))

    for n, src in enumerate(a.practice[:2], 1):
        pid = f"PRACTICE{n}"
        shutil.copytree(src, media / "games" / pid, ignore=shutil.ignore_patterns(".git"))
        plan["practice"].append(pid)

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda j: cut(*j), cuts))
    for src, out in copies:
        shutil.copy2(src, out)

    (sess / "sealed").mkdir()
    (sess / "sealed/mapping.json").write_text(json.dumps(mapping, indent=2) + "\n")
    (sess / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    (sess / "ratings.json").write_text(json.dumps(
        {"pass1": {}, "overall": {}, "pass2": {}, "intro_done": {}, "locked": {"pass1": False, "pass2": False}}, indent=2) + "\n")
    print(f"session ready in {sess}: {len(runs)} runs, {n_rep} repeats per phase, {len(auto)} gate failures scored automatically")
    if no_marks:
        print(f"no timeline marks (whole clip used in every phase): {', '.join(no_marks)}")
    print("start the launcher with: python3 rating/serve.py")


def refresh(runs_dir, sess, which):
    """Re-cut media for some phases of an existing session after re-evaluating
    the runs. Keeps IDs, order, ratings and the media of every other phase."""
    mapping = json.loads((sess / "sealed/mapping.json").read_text())
    plan = json.loads((sess / "plan.json").read_text())
    ratings = json.loads((sess / "ratings.json").read_text())
    bad = [w for w in which if w not in [p["key"] for p in PHASES] + ["overall"]]
    if bad:
        sys.exit(f"unknown phase(s): {', '.join(bad)}")
    rated = [i for i, m in mapping["pass1"].items() if m["phase"] in which and i in ratings["pass1"]]
    if rated or ("overall" in which and ratings["overall"]):
        sys.exit("refusing: some of these clips are already rated")
    info = {}
    for rid in {m["run"] for m in mapping["pass1"].values()}:
        rdir = runs_dir / rid
        res = json.loads((rdir / "eval/results.json").read_text())
        info[rid] = {"dir": rdir, "cuts": phase_cuts(rdir, res)[0],
                     "clip": rdir / ("eval/showcase-manual.mp4" if res.get("manual_showcase_required") else "eval/showcase.mp4")}
    media = sess / "media"
    cuts, copies, rng = [], [], random.Random()
    for ph_def, ph in zip(PHASES, plan["phases"]):
        if ph["key"] not in which:
            continue
        for i in ph["ids"]:
            m = mapping["pass1"][i]
            if m["repeat"]:
                copies.append((media / "clips" / f"{m['repeat_of']}.mp4", media / "clips" / f"{i}.mp4"))
            else:
                cuts.append((info[m["run"]]["cuts"][ph["key"]], media / "clips" / f"{i}.mp4"))
        for n, (g, rid) in enumerate(zip(ph["grid"], rng.sample(sorted(info), len(info)))):
            src = info[rid]["dir"] / "eval/screenshots" / ph_def["shot"]
            if src.exists():
                shutil.copy2(src, media / "thumbs" / g)
    if "overall" in which:
        for o in plan["overall"]:
            rid = mapping["overall"][o["id"]]["run"]
            shutil.copy2(info[rid]["clip"], media / "clips" / f"{o['id']}.mp4")
            src = info[rid]["dir"] / "eval/screenshots/reveal-ancient.png"
            if src.exists():
                shutil.copy2(src, media / "thumbs" / f"{o['id']}-reveal-ancient.png")
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda j: cut(*j), cuts))
    for src, out in copies:
        shutil.copy2(src, out)
    print(f"refreshed {', '.join(which)}: {len(cuts)} clips cut, {len(copies)} repeats copied; ratings untouched")


def append(runs_dir, sess, include, excludes, repeat_rate):
    """Add runs that are not in an existing session, keeping every rating.

    Per phase, clips already rated keep their place; the unrated ones and the
    new runs' clips are shuffled together after them. New repeats (drawn from
    the new runs) keep the phase at about one repeat per eight runs. A phase
    whose overview was already seen shows its overview again, now with every
    run, so the new games can be placed against the old. Overall board and
    pass 2 get the new runs too (refused if either has ratings or is locked).
    """
    mapping = json.loads((sess / "sealed/mapping.json").read_text())
    plan = json.loads((sess / "plan.json").read_text())
    ratings = json.loads((sess / "ratings.json").read_text())
    if ratings["locked"]["pass1"] or ratings["overall"] or ratings["pass2"]:
        sys.exit("refusing: pass 1 is locked, or the overall board / pass 2 already has ratings")
    known = {m["run"] for m in mapping["pass1"].values()} | set(mapping["auto_tier1"])
    new, problems = [], []
    for meta_p in sorted(runs_dir.glob("*/meta.json")):
        rdir = meta_p.parent
        rid = rdir.name
        if rid in known or not fnmatch.fnmatch(rid, include) or any(fnmatch.fnmatch(rid, g) for g in excludes):
            continue
        res_p = rdir / "eval/results.json"
        if not res_p.exists():
            problems.append(f"{rid}: not evaluated")
            continue
        res = json.loads(res_p.read_text())
        if res["gate_failed"] and not res.get("manual_showcase_required"):
            mapping["auto_tier1"].append(rid)
            continue
        clip = rdir / ("eval/showcase-manual.mp4" if res.get("manual_showcase_required") else "eval/showcase.mp4")
        if not clip.exists():
            problems.append(f"{rid}: missing {clip.relative_to(rdir)}")
            continue
        new.append({"run": rid, "dir": rdir, "clip": clip, "cuts": phase_cuts(rdir, res)[0], "game": rdir / "workspace"})
    if problems:
        sys.exit("cannot append:\n  " + "\n  ".join(problems))
    if not new:
        sys.exit("no new runs to append")

    rng = random.Random()
    taken = set(mapping["pass1"]) | set(mapping["overall"]) | set(mapping["pass2"])
    media = sess / "media"
    total = len(known - set(mapping["auto_tier1"])) + len(new)
    cuts, copies = [], []
    first_id = {}
    for ph_def, ph in zip(PHASES, plan["phases"]):
        key = ph["key"]
        old_reps = sum(1 for i in ph["ids"] if mapping["pass1"][i]["repeat"])
        n_rep = max(0, min(len(new), round(total * repeat_rate) - old_reps))
        items = [{"src": r} for r in new] + [{"src": r} for r in rng.sample(new, n_rep)]
        added, cut_once = [], {}
        for it in spread_shuffle(rng, items, key=lambda it: it["src"]["run"]):
            r, i = it["src"], new_id(rng, taken)
            k = (key, r["run"])
            m = {"run": r["run"], "phase": key, "repeat": k in first_id}
            if m["repeat"]:
                m["repeat_of"] = first_id[k]
            else:
                first_id[k] = i
            mapping["pass1"][i] = m
            added.append(i)
            out = media / "clips" / f"{i}.mp4"
            if r["run"] in cut_once:
                copies.append((cut_once[r["run"]], out))
            else:
                cut_once[r["run"]] = out
                cuts.append((r["cuts"][key], out))
        rated = [i for i in ph["ids"] if i in ratings["pass1"]]
        unrated = [i for i in ph["ids"] if i not in ratings["pass1"]]
        rest = spread_shuffle(rng, unrated + added, key=lambda i: mapping["pass1"][i]["run"])
        ph["ids"] = rated + rest
        # Overview grid: add the new runs' screenshots and reshuffle.
        n0 = len(ph["grid"])
        for n, r in enumerate(new):
            src = r["dir"] / "eval/screenshots" / ph_def["shot"]
            if not src.exists() and ph_def["shot"] == "ambience.png":
                src = r["dir"] / "eval/screenshots/idle.png"
            if src.exists():
                name = f"grid-{key}-{n0 + n:02d}.png"
                shutil.copy2(src, media / "thumbs" / name)
                ph["grid"].append(name)
        rng.shuffle(ph["grid"])
        if ratings.get("intro_done", {}).get(key) and any(i not in ratings["pass1"] for i in added):
            ratings["intro_done"][key] = False
        print(f"{key}: +{len(added)} clips ({n_rep} repeats); {len(rated)} rated clips kept in place")

    for r in new:
        i = new_id(rng, taken)
        mapping["overall"][i] = {"run": r["run"]}
        shutil.copy2(r["clip"], media / "clips" / f"{i}.mp4")
        thumbs = []
        for shot in ("ambience.png", "reveal-ancient.png"):
            src = r["dir"] / "eval/screenshots" / shot
            if not src.exists() and shot == "ambience.png":
                src = r["dir"] / "eval/screenshots/idle.png"
            if src.exists():
                shutil.copy2(src, media / "thumbs" / f"{i}-{shot}")
                thumbs.append(f"{i}-{shot}")
        plan["overall"].append({"id": i, "thumbs": thumbs, "links": [first_id[(ph["key"], r["run"])] for ph in PHASES]})
    rng.shuffle(plan["overall"])

    old_reps = sum(1 for m in mapping["pass2"].values() if m["repeat"])
    n_rep = max(0, min(len(new), round(total * repeat_rate) - old_reps))
    p2_first = {}
    for r in new + rng.sample(new, n_rep):
        i = new_id(rng, taken)
        m = {"run": r["run"], "repeat": r["run"] in p2_first}
        if m["repeat"]:
            m["repeat_of"] = p2_first[r["run"]]
        else:
            p2_first[r["run"]] = i
            shutil.copytree(r["game"], media / "games" / i, ignore=shutil.ignore_patterns(".git"))
        mapping["pass2"][i] = m
        plan["pass2"].append(i)
    for i, m in mapping["pass2"].items():  # repeats serve a copy of the game under their own ID
        if not (media / "games" / i).exists():
            shutil.copytree(media / "games" / m["repeat_of"], media / "games" / i)
    plan["pass2"] = spread_shuffle(rng, plan["pass2"], key=lambda i: mapping["pass2"][i]["run"])

    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(lambda j: cut(*j), cuts))
    for src, out in copies:
        shutil.copy2(src, out)
    mapping.setdefault("appended", []).append({"runs": [r["run"] for r in new]})
    (sess / "sealed/mapping.json").write_text(json.dumps(mapping, indent=2) + "\n")
    (sess / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
    # Write ratings last and only touch intro_done, re-reading so a rating
    # saved while clips were cutting is not lost.
    live = json.loads((sess / "ratings.json").read_text())
    live.setdefault("intro_done", {}).update({k: v for k, v in ratings["intro_done"].items() if v is False})
    (sess / "ratings.json").write_text(json.dumps(live, indent=2) + "\n")
    print(f"appended {len(new)} runs: {', '.join(r['run'] for r in new)}")


if __name__ == "__main__":
    main()
