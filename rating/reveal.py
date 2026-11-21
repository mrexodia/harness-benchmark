"""What the rating page shows once both passes are locked: which run (and harness)
each clip and game came from, plus each run's functional result, so the page can
draw the rater's final ranking. Used by serve.py (/api/reveal, after locking) and
export_static.py (bundled with the static site)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def functional(run_dir: Path):
    p = run_dir / "eval/results.json"
    if not p.exists():
        return None
    res = json.loads(p.read_text())
    ov_p = run_dir / "overrides.json"
    if ov_p.exists():
        for k, v in json.loads(ov_p.read_text()).get("checks", {}).items():
            res["checks"][k]["status"] = v
    scored = [c for c in res["checks"].values() if c["type"] == "scored"]
    return {"passed": sum(c["status"] == "pass" for c in scored), "total": len(scored),
            "gate_ok": all(c["status"] == "pass" for c in res["checks"].values() if c["type"] == "gate")}


def reveal(sess: Path, runs_dir: Path = ROOT / "runs"):
    mapping = json.loads((sess / "sealed/mapping.json").read_text())
    try:
        versions = json.loads((ROOT / "task/prompt-versions.json").read_text())
    except FileNotFoundError:
        versions = {}
    run_ids = {m["run"] for p in ("pass1", "overall", "pass2") for m in mapping[p].values()} | set(mapping.get("auto_tier1", []))
    runs = {}
    for rid in sorted(run_ids):
        meta_p = runs_dir / rid / "meta.json"
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
        pv = versions.get((meta.get("prompt_sha256") or "")[:8], {})
        runs[rid] = {"harness": meta.get("harness", "?"), "harness_version": meta.get("harness_version"),
                     "model": meta.get("model"), "prompt": pv.get("version"),
                     "status": meta.get("status"), "minutes": round(meta["duration_s"] / 60) if meta.get("duration_s") else None,
                     "functional": functional(runs_dir / rid)}
    return {"mapping": {p: mapping[p] for p in ("pass1", "overall", "pass2")},
            "auto_tier1": mapping.get("auto_tier1", []), "runs": runs}
