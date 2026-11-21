# Rerun policy (draft — confirm before the first counted run)

A run is rerun only when it hit a documented infrastructure fault. Everything
else (timeouts, crashes, empty workspaces, bad games) is scored as it stands.

Counts as an infrastructure fault, rerun allowed:

1. The model endpoint returned 5xx or connection errors for 10 or more minutes
   in total during the run, as shown by the harness transcript or egress log.
2. `./hc run` recorded `status: infra-error` (Docker, network or disk failure
   in the wrapper itself, not in the agent).
3. The host lost power, slept or rebooted during the run.

Does not count: the agent breaking its own environment, rate limits that the
harness retried through, model refusals, a harness's own bugs, or a slow model.

Every rerun is logged here with the run ID, the fault and the evidence. The
faulty run stays in `runs/` and is excluded with `--exclude` when preparing
the rating session.

| Date | Run ID | Fault | Evidence |
| --- | --- | --- | --- |
