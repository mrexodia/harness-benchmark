# Model providers

A provider is where the model is served: the model name, the hosts the egress
proxy must allow, the secrets to pass through, and optional per-harness setup.
Harnesses stay as shipped; everything specific to a backend lives here.

```
providers/<name>/provider.env    HC_MODEL, MODEL_HOSTS, SECRET_ENV (overrides config/benchmark.env)
providers/<name>/<harness>.sh    optional, sourced in the container before that harness starts
providers/<name>/*               any other files the hooks need, mounted read-only at /opt/provider/
```

Private providers go in `local/providers/<name>/`, which is gitignored and
searched first. Pick one with `PROVIDER=<name>` in `local/benchmark.env`
(also gitignored), or per run with `./hc run --provider <name>`. The provider
folder is mounted at run time and never built into the agent image. Each run
records its provider and model in `meta.json`.

Start from `providers/example/`.
