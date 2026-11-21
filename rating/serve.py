#!/usr/bin/env python3
"""Blind rating launcher.

  python3 rating/serve.py [--session rating/session]

Serves the rating UI at http://127.0.0.1:8100/ and the game being played at
http://127.0.0.1:8010/ by default (each game served at the root, as it expects). Only IDs reach the
browser until both passes are locked; then /api/reveal sends the sealed mapping for the results page.
"""
import argparse
import io
import json
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

# Browsers allow only ~6 connections per host. Range replies are capped so a
# video that stops reading (paused, or removed from the page) cannot keep a
# connection busy and starve the rating API.
CHUNK = 1 << 20
from pathlib import Path

from reveal import reveal

HERE = Path(__file__).resolve().parent
VISUAL = ["scene", "pixel_art", "cohesion", "catch", "ui", "rarity"]
FEEL = ["feel", "polish", "keep_playing"]
lock = threading.Lock()
GAME_PORT = 8010


# Keys a game uses also scroll the page by default, and when the game page
# itself can't scroll Chrome passes the scroll up to the rating page around
# the iframe. Cancel only that default; the game still receives every key.
# Buttons and form fields keep their own behaviour (Space clicks a button).
# It also accepts keys forwarded by the rating page (pressed while the page,
# not the game, had focus) and replays them as key events inside the game.
NO_SCROLL = """<script>addEventListener("keydown",function(e){var k=e.key;
if(k===" "||k==="Spacebar"||k==="ArrowUp"||k==="ArrowDown"||k==="PageUp"||k==="PageDown"){
var t=e.target;if(!(t&&t.closest&&t.closest("button,input,textarea,select,a,[contenteditable]")))e.preventDefault();}},false);
addEventListener("message",function(m){var d=m.data;if(!d||!d.hcKey)return;
var o={key:d.hcKey,code:d.code,keyCode:d.keyCode,which:d.keyCode,bubbles:true,cancelable:true};
var t=document.activeElement||document.body;t.dispatchEvent(new KeyboardEvent("keydown",o));t.dispatchEvent(new KeyboardEvent("keyup",o));});</script>"""


class GameHandler(SimpleHTTPRequestHandler):
    root = None  # set per game

    def translate_path(self, path):
        self.directory = str(GameHandler.root or HERE / "static/empty")
        return super().translate_path(path)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        # Inject NO_SCROLL into the game's HTML pages; everything else is served as is.
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        if path.suffix.lower() not in (".html", ".htm") or not path.is_file():
            return super().send_head()
        html = path.read_text(errors="replace")
        low = html.lower()
        i = low.find("<head>")
        html = html[:i + 6] + NO_SCROLL + html[i + 6:] if i >= 0 else NO_SCROLL + html
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        return io.BytesIO(data)

    def log_message(self, *a):
        pass


def make_ui_handler(sess: Path):
    plan = json.loads((sess / "plan.json").read_text())
    ratings_p = sess / "ratings.json"
    phase_axes = {i: ph["axes"] for ph in plan["phases"] for i in ph["ids"]}
    overall_ids = {o["id"] for o in plan["overall"]}

    def load():
        return json.loads(ratings_p.read_text())

    def save(r):
        tmp = ratings_p.with_suffix(".tmp")
        tmp.write_text(json.dumps(r, indent=2) + "\n")
        tmp.replace(ratings_p)

    class UI(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(HERE / "static"), **kw)

        def log_message(self, *a):
            pass

        def send_json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file_range(self, f, ctype):
            # Range support so the browser can seek (scrubbing; Safari needs it to play at all).
            data = f.read_bytes()
            size = len(data)
            start, end, code = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            if rng.startswith("bytes="):
                a, _, b = rng[6:].split(",")[0].strip().partition("-")
                try:
                    if a:
                        start, end = int(a), (int(b) if b else size - 1)
                    else:
                        start = max(0, size - int(b))
                    end = min(end, size - 1, start + CHUNK - 1)  # short replies: a paused video can't hold a connection
                    if start > end:
                        raise ValueError
                    code = 206
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            self.wfile.write(data[start:end + 1])

        def do_GET(self):
            if self.path == "/api/state":
                return self.send_json({"plan": plan, "ratings": load(), "axes": {"pass2": FEEL}})
            if self.path == "/api/reveal":
                r = load()
                if not (r["locked"]["pass1"] and r["locked"]["pass2"]):
                    return self.send_json({"error": "lock both passes first"}, 409)
                return self.send_json(reveal(sess))
            if self.path.startswith("/media/"):
                parts = self.path.split("?")[0].split("/")
                if len(parts) == 4 and parts[2] in ("clips", "thumbs") and ".." not in parts[3]:
                    f = sess / "media" / parts[2] / parts[3]
                    if f.exists():
                        return self.send_file_range(f, "video/mp4" if f.suffix == ".mp4" else "image/png")
                return self.send_error(404)
            return super().do_GET()

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            with lock:
                r = load()
                if self.path == "/api/rate":
                    p, i = body["pass"], body["id"]
                    if r["locked"].get(p):
                        return self.send_json({"error": "pass is locked"}, 409)
                    if p == "pass2" and not r["locked"]["pass1"]:
                        return self.send_json({"error": "lock pass 1 first"}, 409)
                    if p == "pass1" and i not in phase_axes or p == "pass2" and i not in plan["pass2"]:
                        return self.send_json({"error": "unknown id"}, 404)
                    axes = phase_axes[i] if p == "pass1" else FEEL
                    scores = {k: int(body["scores"][k]) for k in axes}
                    if not all(1 <= v <= 7 for v in scores.values()):
                        return self.send_json({"error": "scores must be 1-7"}, 400)
                    entry = {"scores": scores, "flag_harness_name": bool(body.get("flag")), "note": body.get("note", "")}
                    # Keep what was entered first, so the report can show how much calibration moved things.
                    prev = r[p].get(i)
                    entry["first"] = prev["first"] if prev else {"scores": scores}
                    entry["edits"] = (prev.get("edits", 0) + 1) if prev else 0
                    r[p][i] = entry
                    save(r)
                    return self.send_json({"ok": True})
                if self.path == "/api/intro-done":
                    r.setdefault("intro_done", {})[str(body["phase"])] = True
                    save(r)
                    return self.send_json({"ok": True})
                if self.path == "/api/tier":
                    i = body["id"]
                    if r["locked"]["pass1"]:
                        return self.send_json({"error": "pass is locked"}, 409)
                    if i not in overall_ids:
                        return self.send_json({"error": "unknown id"}, 404)
                    tier = int(body["tier"])
                    if not 1 <= tier <= 5:
                        return self.send_json({"error": "tier must be 1-5"}, 400)
                    prev = r["overall"].get(i)
                    r["overall"][i] = {"tier": tier, "note": body.get("note", prev["note"] if prev else ""),
                                       "tier_mismatch": bool(body.get("mismatch")),
                                       "first_tier": prev["first_tier"] if prev else tier,
                                       "edits": (prev["edits"] + 1) if prev else 0}
                    save(r)
                    return self.send_json({"ok": True})
                if self.path == "/api/lock":
                    p = body["pass"]
                    if p == "pass1":
                        missing = [i for i in phase_axes if i not in r["pass1"]] + [i for i in overall_ids if i not in r["overall"]]
                    else:
                        missing = [i for i in plan["pass2"] if i not in r["pass2"]]
                    if missing:
                        return self.send_json({"error": f"{len(missing)} items not rated"}, 409)
                    r["locked"][p] = True
                    save(r)
                    return self.send_json({"ok": True})
                if self.path == "/api/game":
                    i = body["id"]
                    if i not in plan["pass2"] and i not in plan["practice"]:
                        return self.send_json({"error": "unknown id"}, 404)
                    GameHandler.root = sess / "media/games" / i
                    return self.send_json({"url": f"http://127.0.0.1:{GAME_PORT}/"})
            self.send_error(404)

    return UI


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=str(HERE / "session"))
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--game-port", type=int, default=8010)
    a = ap.parse_args()
    sess = Path(a.session).resolve()
    (HERE / "static/empty").mkdir(exist_ok=True)
    global GAME_PORT
    GAME_PORT = a.game_port
    games = ThreadingHTTPServer(("127.0.0.1", GAME_PORT), GameHandler)
    threading.Thread(target=games.serve_forever, daemon=True).start()
    ui = ThreadingHTTPServer(("127.0.0.1", a.port), make_ui_handler(sess))
    print(f"rating UI: http://127.0.0.1:{a.port}/   (games are served on :{GAME_PORT})")
    try:
        ui.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
