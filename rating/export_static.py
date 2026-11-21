#!/usr/bin/env python3
"""Build the rating session as a static site (GitHub Pages, Netlify, any file host).

  python3 rating/export_static.py [--session rating/session] [--out rating/site] [--title "..."]

The site is the same rating page, run without serve.py: ratings are kept in each
rater's browser and exported as a file (ratings-<name>.json) to send back. Once a
rater locks both passes the page reveals which run and harness each clip came
from and shows their own ranking. The reveal data is in site.json, so anyone
reading the files can see it in advance; this site is for sharing, not for a
sealed study.

  python3 rating/report.py --ratings ratings-alex.json   # report from one rater's file

Deploy: push the contents of --out to a repository (or a gh-pages branch) and
turn on GitHub Pages for it. About 340 MB; GitHub Pages allows 1 GB.
"""
import argparse
import json
import shutil
from pathlib import Path

from reveal import reveal
from serve import NO_SCROLL

HERE = Path(__file__).resolve().parent

INTRO = """<p>Each of these fishing games was written from the same prompt by an AI coding agent.
You'll judge how they look and feel, without knowing which run made which game:</p>
<ol>
<li><b>Watch</b> short clips in three phases (the scene, a catch and a miss, the rarity reveals) and score each on a 1–7 scale, then give every game an overall tier.</li>
<li><b>Play</b> each game for about a minute in the page and score how it feels.</li>
<li>After locking both, you see which run each game came from and your own ranking.</li>
</ol>
<p class="muted">The whole thing takes about two hours; you can stop at any point. A desktop browser with a keyboard is needed for the games.</p>"""


def inject(html: str) -> str:
    i = html.lower().find("<head>")
    return html[:i + 6] + NO_SCROLL + html[i + 6:] if i >= 0 else NO_SCROLL + html


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=str(HERE / "session"))
    ap.add_argument("--out", default=str(HERE / "site"))
    ap.add_argument("--title", default="Gone fishin' · blind rating")
    a = ap.parse_args()
    sess, out = Path(a.session).resolve(), Path(a.out).resolve()
    if out == sess or sess in out.parents:
        raise SystemExit("--out must be outside the session")
    plan = json.loads((sess / "plan.json").read_text())
    seed = json.loads((sess / "sealed/mapping.json").read_text())["seed"]

    if out.exists():
        shutil.rmtree(out)
    (out / "media").mkdir(parents=True)
    shutil.copy2(HERE / "static/index.html", out / "index.html")
    for sub in ("clips", "thumbs"):
        shutil.copytree(sess / "media" / sub, out / "media" / sub)
    games = out / "games"
    for gid in plan["pass2"] + plan["practice"]:
        shutil.copytree(sess / "media/games" / gid, games / gid)
        for f in (games / gid).rglob("*.htm*"):
            f.write_text(inject(f.read_text(errors="replace")))
    site = {"title": a.title, "intro": INTRO, "key": f"hc-rating-{seed}", "plan": plan, "reveal": reveal(sess)}
    (out / "site.json").write_text(json.dumps(site) + "\n")
    (out / ".nojekyll").write_text("")  # serve every file as is (Jekyll skips some names)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    big = [f for f in out.rglob("*") if f.is_file() and f.stat().st_size > 95 << 20]
    print(f"wrote {out}: {size / 1e6:.0f} MB, {len(plan['pass2'])} games, "
          f"{sum(len(p['ids']) for p in plan['phases'])} clips" + (f"; over 95 MB: {big}" if big else ""))


if __name__ == "__main__":
    main()
