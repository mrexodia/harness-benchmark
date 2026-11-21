#!/usr/bin/env python3
"""Harness benchmark runner.

  ./hc build                         build the base, agent and evaluator images
  ./hc run --harness pi              one run: fresh container, empty workspace, then evaluate
  ./hc eval runs/<run-id>            (re)run the evaluator on a finished run
  ./hc run --harness pi --provider x one run against another provider
  ./hc schedule --harnesses pi,x --blocks 8   write runs/queue.json, interleaved in shuffled blocks
                                     (an entry may be harness@provider)
  ./hc batch                         work through runs/queue.json in order (resumable)

Config: config/benchmark.env, overridden by local/benchmark.env (gitignored),
then by the provider's provider.env (providers/README.md). Secrets named in
SECRET_ENV are read from the host environment and passed to the agent container
by name only.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import random
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"


def read_env(path, cfg):
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.split("#", 1)[0].strip()
    return cfg


def provider_dir(name):
    for base in (ROOT / "local/providers", ROOT / "providers"):
        if (base / name / "provider.env").exists():
            return base / name
    raise SystemExit(f"unknown provider: {name} (looked in local/providers/ and providers/)")


def load_config(provider=None):
    """Study config, local overrides, then the provider's settings."""
    cfg = read_env(ROOT / "config/benchmark.env", {})
    if (ROOT / "local/benchmark.env").exists():
        read_env(ROOT / "local/benchmark.env", cfg)
    cfg["PROVIDER"] = provider or cfg.get("PROVIDER", "")
    if not cfg["PROVIDER"]:
        raise SystemExit("no model provider: set PROVIDER in local/benchmark.env or pass --provider (see providers/README.md)")
    cfg["PROVIDER_DIR"] = provider_dir(cfg["PROVIDER"])
    read_env(cfg["PROVIDER_DIR"] / "provider.env", cfg)
    if not cfg.get("HC_MODEL"):
        raise SystemExit(f"provider {cfg['PROVIDER']} does not set HC_MODEL")
    return cfg


def sh(*args, check=True, capture=True, **kw):
    r = subprocess.run(list(args), text=True, capture_output=capture, **kw)
    if check and r.returncode != 0:
        raise SystemExit(f"command failed ({r.returncode}): {' '.join(args)}\n{r.stderr}")
    return r.stdout.strip() if capture else ""


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def image_id(tag):
    return sh("docker", "image", "inspect", tag, "--format", "{{.Id}}", check=False) or None


# ---------------------------------------------------------------- build

def cmd_build(args):
    for name, df in [("hc-base", "base"), ("hc-agent", "agent"), ("hc-eval", "eval")]:
        print(f"== building {name}")
        # Derived images skip the layer cache so they always pick up the current base.
        extra = [] if df == "base" else ["--no-cache"]
        sh("docker", "build", *extra, "-t", f"{name}:latest", "-f", f"docker/{df}.Dockerfile", ".", cwd=ROOT, capture=False)
    cfg = read_env(ROOT / "config/benchmark.env", {})
    sh("docker", "pull", "-q", cfg["EGRESS_IMAGE"], capture=False)
    lock = {n: image_id(f"{n}:latest") for n in ("hc-base", "hc-agent", "hc-eval")}
    lock["harnesses"] = {d.name: (d / "VERSION").read_text().strip() for d in sorted((ROOT / "harnesses").iterdir()) if (d / "VERSION").exists()}
    (ROOT / "config/images.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    print(json.dumps(lock, indent=2))


# ---------------------------------------------------------------- run

def cmd_run(args):
    cfg = load_config(args.provider)
    harness = args.harness
    hdir = ROOT / "harnesses" / harness
    if not (hdir / "run.sh").exists():
        raise SystemExit(f"unknown harness: {harness}")
    missing = [n for n in filter(None, cfg.get("SECRET_ENV", "").split(",")) if not os.environ.get(n.strip())]
    if missing:
        raise SystemExit(f"missing secret env vars on the host: {', '.join(missing)}")

    run_id = args.run_id or f"{harness}-{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
    rdir = RUNS / run_id
    if rdir.exists():
        raise SystemExit(f"run dir exists: {rdir}")
    rdir.mkdir(parents=True)
    limit_s = int(float(args.minutes or cfg["WALL_CLOCK_MINUTES"]) * 60)
    prompt = (ROOT / "task/prompt.txt").read_bytes()

    meta = {
        "run_id": run_id,
        "harness": harness,
        "harness_version": (hdir / "VERSION").read_text().strip(),
        "provider": cfg["PROVIDER"],
        "model": cfg["HC_MODEL"],
        "thinking": cfg.get("HC_THINKING") or None,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(),
        "agent_image": image_id(cfg["AGENT_IMAGE"]),
        "eval_image": image_id(cfg["EVAL_IMAGE"]),
        "wall_clock_limit_s": limit_s,
        "started": now(),
    }
    write_meta(rdir, meta)

    net, egress, agent = f"hc-net-{run_id}", f"hc-egress-{run_id}", f"hc-agent-{run_id}"
    cleanup = lambda: [sh("docker", "rm", "-f", c, check=False) for c in (agent, egress)] + [sh("docker", "network", "rm", net, check=False)]
    try:
        sh("docker", "network", "create", "--internal", net)
        sh("docker", "run", "-d", "--name", egress, "--network", net, "--network-alias", "egress",
           "-v", f"{ROOT / 'egress'}:/egress:ro", "-e", f"EXTRA_ALLOW={cfg.get('MODEL_HOSTS', '')}",
           cfg["EGRESS_IMAGE"], "node", "/egress/proxy.mjs")
        sh("docker", "network", "connect", "bridge", egress)

        proxy = "http://egress:3128"
        env = {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
               "NODE_USE_ENV_PROXY": "1", "HC_MODEL": cfg["HC_MODEL"], "HC_THINKING": cfg.get("HC_THINKING", "")}
        create = ["docker", "create", "--name", agent, "--network", net, "--init", "--shm-size", "1g",
                  "-v", f"{cfg['PROVIDER_DIR']}:/opt/provider:ro"]
        for k, v in env.items():
            create += ["-e", f"{k}={v}"]
        for name in filter(None, (n.strip() for n in cfg.get("SECRET_ENV", "").split(","))):
            create += ["-e", name]  # value taken from this process's environment
        create += [cfg["AGENT_IMAGE"], harness]
        sh(*create)

        print(f"[{run_id}] started, limit {limit_s // 60} min")
        t0 = time.monotonic()
        with open(rdir / "transcript.jsonl", "w") as out, open(rdir / "agent.stderr.log", "w") as err:
            p = subprocess.Popen(["docker", "start", "-a", agent], stdout=out, stderr=err)
            try:
                code = p.wait(timeout=limit_s)
                status = "completed" if code == 0 else "error"
            except subprocess.TimeoutExpired:
                sh("docker", "kill", agent, check=False)
                p.wait(timeout=60)
                code, status = None, "timeout"
        meta.update(ended=now(), duration_s=round(time.monotonic() - t0, 1), exit_code=code, status=status)
        print(f"[{run_id}] {status} after {meta['duration_s']:.0f}s")

        sh("docker", "cp", f"{agent}:/home/agent/work", str(rdir / "workspace"), check=False)
        if not (rdir / "workspace").exists():
            (rdir / "workspace").mkdir()
        trace = rdir / "trace"
        trace.mkdir()
        collect = hdir / "collect.sh"
        for path in (collect.read_text().splitlines() if collect.exists() else []):
            path = path.strip()
            if path and not path.startswith("#"):
                sh("docker", "cp", f"{agent}:{path}", str(trace / Path(path).name), check=False)
        (rdir / "egress.jsonl").write_text(sh("docker", "logs", egress, check=False) + "\n")
        meta["egress_blocked"] = sorted({json.loads(l)["host"] for l in (rdir / "egress.jsonl").read_text().splitlines()
                                         if l.startswith("{") and json.loads(l).get("allowed") is False})
    except BaseException as e:
        meta.update(status=meta.get("status") or "infra-error", infra_error=repr(e), ended=now())
        raise
    finally:
        write_meta(rdir, meta)
        cleanup()

    if not args.no_eval:
        evaluate(rdir, cfg)
    return rdir


def write_meta(rdir, meta):
    (rdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")


# ---------------------------------------------------------------- eval

def evaluate(rdir, cfg=None):
    cfg = cfg or read_env(ROOT / "config/benchmark.env", {})  # the evaluator needs no provider
    rdir = Path(rdir).resolve()
    out = rdir / "eval"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir()
    print(f"[{rdir.name}] evaluating")
    # Checks that only some prompt versions ask for are scored per run.
    log_timing = "reported"
    try:
        sha = json.loads((rdir / "meta.json").read_text())["prompt_sha256"]
        versions = json.loads((ROOT / "task/prompt-versions.json").read_text())
        log_timing = versions.get(sha[:8], {}).get("log_timing", "reported")
    except (OSError, KeyError, ValueError):
        pass
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--shm-size", "1g",
                        "-e", f"HC_LOG_TIMING={log_timing}",
                        "-v", f"{rdir / 'workspace'}:/in:ro", "-v", f"{out}:/out",
                        cfg["EVAL_IMAGE"]], text=True, capture_output=True)
    (out / "evaluator.log").write_text(r.stdout + r.stderr)
    res = out / "results.json"
    if r.returncode != 0 or not res.exists():
        print(f"[{rdir.name}] evaluator failed, see {out / 'evaluator.log'}")
        return None
    results = json.loads(res.read_text())
    pr = results["pass_rate"]
    print(f"[{rdir.name}] gate {'FAILED' if results['gate_failed'] else 'ok'}, "
          f"functional pass rate {pr['passed']}/{pr['total']}")
    return results


def cmd_eval(args):
    for d in args.run_dirs:
        evaluate(d)


# ---------------------------------------------------------------- schedule / batch

def cmd_schedule(args):
    harnesses = [h.strip() for h in args.harnesses.split(",") if h.strip()]
    rng = random.Random(args.seed)
    queue = []
    for b in range(args.blocks):
        order = harnesses[:]
        rng.shuffle(order)
        queue += [{"block": b + 1, "harness": h.partition("@")[0], "provider": h.partition("@")[2] or None,
                   "run_id": None, "done": False} for h in order]
    q = RUNS / "queue.json"
    RUNS.mkdir(exist_ok=True)
    if q.exists() and not args.force:
        raise SystemExit(f"{q} exists; pass --force to replace it")
    q.write_text(json.dumps({"seed": args.seed, "created": now(), "queue": queue}, indent=2) + "\n")
    print(f"wrote {len(queue)} runs in {args.blocks} blocks to {q}")


def cmd_batch(args):
    q = RUNS / "queue.json"
    data = json.loads(q.read_text())
    for item in data["queue"]:
        if item["done"]:
            continue
        rdir = cmd_run(argparse.Namespace(harness=item["harness"], provider=item.get("provider"), run_id=None, minutes=None, no_eval=False))
        item.update(run_id=rdir.name, done=True)
        q.write_text(json.dumps(data, indent=2) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build").set_defaults(fn=cmd_build)
    p = sub.add_parser("run")
    p.add_argument("--harness", required=True)
    p.add_argument("--provider", help="override PROVIDER (a folder in local/providers/ or providers/)")
    p.add_argument("--run-id")
    p.add_argument("--minutes", help="override WALL_CLOCK_MINUTES (smoke tests only)")
    p.add_argument("--no-eval", action="store_true")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("eval")
    p.add_argument("run_dirs", nargs="+")
    p.set_defaults(fn=cmd_eval)
    p = sub.add_parser("schedule")
    p.add_argument("--harnesses", required=True, help="comma-separated; harness@provider pins a provider for that entry")
    p.add_argument("--blocks", type=int, required=True)
    p.add_argument("--seed", type=int, default=lambda: secrets.randbits(32))
    p.add_argument("--force", action="store_true")
    p.set_defaults(fn=cmd_schedule)
    sub.add_parser("batch").set_defaults(fn=cmd_batch)
    args = ap.parse_args()
    if callable(getattr(args, "seed", None)):
        args.seed = args.seed()
    args.fn(args)


if __name__ == "__main__":
    main()
