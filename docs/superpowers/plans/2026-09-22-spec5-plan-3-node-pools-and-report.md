# Spec 5, Plan 3 of 4: Node Pools and Report

> Lean plan. Spec §5 (node rows), §6 (node pools), §7, §8.

- Six node-pool rules: SELF-MANAGED-NODES, NODE-MACHINE-TYPE, NODE-IMAGE, NODE-SPOT, NODE-SCALING,
  SSH-KEY. Node zones are positional over the node pools' subnet AZs (`node_zone_letters`) and
  resolved against the region's real zones at generation.
- `google_container_node_pool` (totals-based autoscaling or per-zone fixed count, management,
  upgrade settings, node_config with image, spot, labels, taints, network tags, Workload Identity
  metadata) and the K8S-FARGATE default pool; orphaned-pool and no-zone `GenerationError`s.
- Report `## Kubernetes` section with the empty-cluster paragraph.
- Four golden fixtures (managed, private+KMS+spot, self-managed, Fargate-only), a network check that
  subnets carry the ranges, and provider-schema validation for all four.
- Fix found by the goldens: a private endpoint renders no authorized-networks block.
