# Spec 7, Plan 2 of 4: Load-balancing Rules

> Lean plan. Spec §5.

- Thirteen `LB-*` rules registered after compute, `LB-KUBERNETES-OWNED` first.
- Per-listener differences (certificates, SSL policy) key on `<lb>:<port>`.
- LB-SHAPE allocates one proxy-only /23 per network from `100.126.0.0/16` for internal ALBs.
- LB-BACKENDS writes the named port on the target group and on each targeted managed instance group.
- Listener-level outcomes collect in LB hints: `dropped_rules`, `default_fallbacks`, `skipped_listeners`, `certificates`, `ssl_policies`, `allowlist`, `logging`.
