#!/usr/bin/env python3
"""Per-harness report: functional layer from results.json, rating layers from
the locked rating session. The two layers are reported side by side, never
combined.

  python3 rating/report.py [--runs runs] [--session rating/session] [--functional-only]

Manual review outcomes (e.g. a Code-drawn art hit judged harmless) go in
runs/<id>/overrides.json as {"checks": {"code_art": "pass"}, "note": "..."}.
Intervals are 95% percentile bootstrap intervals over runs.
"""
import argparse
import fnmatch
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

VISUAL = ["scene", "pixel_art", "cohesion", "catch", "ui", "rarity"]
FEEL = ["feel", "polish", "keep_playing"]


def boot(values, stat=statistics.mean, n=5000, seed=1):
    if not values:
        return None, None, None
    if len(values) == 1:
        return stat(values), None, None
    rng = random.Random(seed)
    sims = sorted(stat([rng.choice(values) for _ in values]) for _ in range(n))
    return stat(values), sims[int(0.025 * n)], sims[int(0.975 * n) - 1]


def fmt(t, digits=2):
    m, lo, hi = t
    if m is None:
        return "–"
    return f"{m:.{digits}f}" + (f" [{lo:.{digits}f}, {hi:.{digits}f}]" if lo is not None else "")


def load_runs(runs_dir, include):
    out = []
    for meta_p in sorted(Path(runs_dir).glob("*/meta.json")):
        rid = meta_p.parent.name
        if not fnmatch.fnmatch(rid, include):
            continue
        meta = json.loads(meta_p.read_text())
        res_p = meta_p.parent / "eval/results.json"
        res = json.loads(res_p.read_text()) if res_p.exists() else None
        ov_p = meta_p.parent / "overrides.json"
        if res and ov_p.exists():
            for k, v in json.loads(ov_p.read_text()).get("checks", {}).items():
                res["checks"][k]["status"] = v
                res["checks"][k]["overridden"] = True
        out.append({"id": rid, "meta": meta, "res": res})
    return out


def functional(run):
    res = run["res"]
    if not res:  # never evaluated (e.g. produced nothing): every check fails
        return {"rate": 0.0, "gate_failed": True, "pending_review": False}
    scored = [c for c in res["checks"].values() if c["type"] == "scored"]
    passed = sum(c["status"] == "pass" for c in scored)
    gate = any(c["type"] == "gate" and c["status"] != "pass" for c in res["checks"].values())
    return {"rate": passed / len(scored), "gate_failed": gate, "pending_review": any(c["status"] == "review" for c in scored)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--session", default="rating/session")
    ap.add_argument("--include", default="*")
    ap.add_argument("--functional-only", action="store_true")
    ap.add_argument("--ratings", help="a ratings file exported from the static site (default: the session's ratings.json)")
    ap.add_argument("--out", default="report")
    a = ap.parse_args()

    runs = load_runs(a.runs, a.include)
    if not runs:
        sys.exit("no runs")
    by_h = defaultdict(list)
    for r in runs:
        by_h[r["meta"]["harness"]].append(r)

    ratings = mapping = None
    if not a.functional_only:
        sess = Path(a.session)
        rp = Path(a.ratings) if a.ratings else sess / "ratings.json"
        if not rp.exists():
            sys.exit(f"no ratings at {rp}; use --functional-only")
        ratings = json.loads(rp.read_text())
        if not (ratings["locked"]["pass1"] and ratings["locked"]["pass2"]):
            sys.exit("both passes must be locked before the mapping is revealed; use --functional-only for now")
        mapping = json.loads((sess / "sealed/mapping.json").read_text())
        if ratings.get("rater"):
            print(f"ratings by {ratings['rater']} (exported {ratings.get('exported', '?')})")

    # Per-run rating scores (first showing only; repeats feed the agreement check).
    # Pass 1 is rated in phases (each phase scores some of the visual axes); the
    # tier comes from the overall board.
    vis, feel, flagged, agree = {}, {}, set(), {}
    if ratings:
        for p, store in (("pass1", vis), ("pass2", feel)):
            for i, m in mapping[p].items():
                e = ratings[p][i]
                if e.get("flag_harness_name"):
                    flagged.add(m["run"])
                if m.get("repeat"):
                    agree.setdefault(m.get("phase", p), []).append((ratings[p][m["repeat_of"]], e))
                else:
                    store.setdefault(m["run"], {}).update(e["scores"])
        for i, m in mapping["overall"].items():
            vis.setdefault(m["run"], {})["tier"] = ratings["overall"][i]["tier"]
        for rid in mapping["auto_tier1"]:
            vis[rid] = {**{k: 1 for k in VISUAL}, "tier": 1}
            feel[rid] = {k: 1 for k in FEEL}

    lines = [f"# Harness benchmark report", "",
             f"Runs: {len(runs)} across {len(by_h)} harness(es). Model: {', '.join(sorted({r['meta']['model'] for r in runs}))}. "
             "Results hold for this model, task and prompt only. Intervals are 95% bootstrap over runs; "
             "where intervals overlap, treat harnesses as one tier.", "",
             "## Functional checks", "",
             "| Harness | Runs | Gate failures | Functional pass rate | Pending review |", "|---|---|---|---|---|"]
    summary = {}
    for h, rs in sorted(by_h.items()):
        f = [functional(r) for r in rs]
        rate = boot([x["rate"] for x in f])
        summary[h] = {"runs": len(rs), "gate_failures": sum(x["gate_failed"] for x in f), "pass_rate": rate}
        lines.append(f"| {h} | {len(rs)} | {summary[h]['gate_failures']} | {fmt(rate)} | {sum(x['pending_review'] for x in f)} |")

    if ratings:
        lines += ["", "## Visuals (pass 1, from video)", "",
                  "| Harness | Mean tier | Tier 4–5 share | " + " | ".join(VISUAL) + " |", "|---" * (3 + len(VISUAL)) + "|"]
        for h, rs in sorted(by_h.items()):
            ids = [r["id"] for r in rs if r["id"] in vis and r["id"] not in flagged]
            tiers = [vis[i]["tier"] for i in ids]
            summary[h]["visual"] = {k: boot([vis[i][k] for i in ids]) for k in ["tier", *VISUAL]}
            summary[h]["tier45"] = boot([1.0 if t >= 4 else 0.0 for t in tiers])
            lines.append(f"| {h} | {fmt(summary[h]['visual']['tier'])} | {fmt(summary[h]['tier45'])} | "
                         + " | ".join(fmt(summary[h]["visual"][k]) for k in VISUAL) + " |")
        lines += ["", "## Feel (pass 2, from play)", "", "| Harness | " + " | ".join(FEEL) + " |", "|---" * (1 + len(FEEL)) + "|"]
        for h, rs in sorted(by_h.items()):
            ids = [r["id"] for r in rs if r["id"] in feel and r["id"] not in flagged]
            summary[h]["feel"] = {k: boot([feel[i][k] for i in ids]) for k in FEEL}
            lines.append(f"| {h} | " + " | ".join(fmt(summary[h]["feel"][k]) for k in FEEL) + " |")
        lines += ["", "## Rater consistency (repeats)", ""]
        if not agree:
            lines.append("- no repeats")
        for k, pairs in agree.items():
            mad = statistics.mean(abs(x["scores"][a] - y["scores"][a]) for x, y in pairs for a in x["scores"])
            lines.append(f"- {k}: {len(pairs)} repeats, mean absolute axis difference {mad:.2f}")
        lines += ["", "## Tier against axes (pass 1)", ""]
        rated = [rid for rid in vis if rid not in mapping["auto_tier1"] and "tier" in vis[rid] and all(a in vis[rid] for a in VISUAL)]
        if len(rated) >= 3:
            xs = [statistics.mean(vis[rid][a] for a in VISUAL) for rid in rated]
            ys = [vis[rid]["tier"] for rid in rated]
            try:
                r = statistics.correlation(xs, ys)
                lines.append(f"- Correlation between tier and axis mean over {len(rated)} runs: r = {r:.2f}. "
                             "Near 1 means the tier adds little beyond the axes; low means the scales are used inconsistently.")
            except statistics.StatisticsError:
                lines.append("- Correlation undefined (no variation in tier or axis mean).")
        notes = [(i, e) for i, e in ratings["overall"].items() if e.get("tier_mismatch")]
        if notes:
            lines += ["", "| Run | Tier | Axis mean | Note |", "|---|---|---|---|"]
            for i, e in notes:
                rid = mapping["overall"][i]["run"]
                note = (e.get("note") or "–").replace("|", "/")
                lines.append(f"| {rid} | {e['tier']} | {statistics.mean(vis[rid][a] for a in VISUAL):.1f} | {note} |")
        else:
            lines.append("- No tier/axis mismatches were flagged.")
        lines += ["", "## Calibration drift (first entry against locked score)", ""]
        for p in ("pass1", "pass2"):
            es = [e for e in ratings[p].values() if "first" in e]
            if not es:
                lines.append(f"- {p}: no first-entry data")
                continue
            changed = sum(e["scores"] != e["first"]["scores"] for e in es)
            per = {}
            for e in es:
                for ax, v in e["scores"].items():
                    per.setdefault(ax, []).append(abs(v - e["first"]["scores"][ax]))
            diffs = ", ".join(f"{ax} {statistics.mean(v):.2f}" for ax, v in per.items())
            lines.append(f"- {p}: {changed}/{len(es)} changed after first entry; mean absolute change: {diffs}")
        ov = list(ratings["overall"].values())
        if ov:
            moved = sum(e["tier"] != e["first_tier"] for e in ov)
            lines.append(f"- overall tier: {moved}/{len(ov)} moved after first placement; mean absolute change "
                         f"{statistics.mean(abs(e['tier'] - e['first_tier']) for e in ov):.2f}")
        if flagged:
            lines += ["", f"Excluded for showing a harness name: {', '.join(sorted(flagged))}"]
        lines += ["", "## ID mappings (revealed after locking)", "", "```json", json.dumps(mapping, indent=1), "```"]

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    print("\n".join(lines[: 8 + len(by_h)]))
    print(f"\nwrote {out / 'report.md'}")


if __name__ == "__main__":
    main()
