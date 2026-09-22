# Spec 7, Plan 3 of 4: Generation and Report

> Lean plan. Spec §6-§8.

- Generator: unmanaged instance groups per (network, zone) shared across target groups; global external ALB (address, health checks, backend services, URL maps with host/path matchers or redirect, HTTP/HTTPS proxies, managed certificates, SSL policies, forwarding rules, Cloud Armor allow-list); regional internal ALB (proxy-only subnet, regional equivalents); passthrough NLBs (regional backend services with CONNECTION balancing, forwarding rules per listener); firewall rules for proxy/health ranges to the backends tags.
- LB-CERTIFICATES: internal ALB HTTPS is manual-only (managed certificates are global-only).
- `## Load balancers` report section; two golden fixtures built on the compute fixtures; provider-schema validation alongside network and compute.
