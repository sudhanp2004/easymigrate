# Spec 5, Plan 2 of 4: Cluster Rules and Generation

> Lean plan. Spec §2.1, §5 (cluster rows), §6 (cluster), §9.

- `gke_facts.py`: minors offered, image types, log-component and add-on maps, range pools.
- Twelve cluster rules: EMPTY-CLUSTER, VERSION, ENDPOINT, PRIVATE-NODES (moved to cluster level),
  POD-RANGES, LOGGING, SECRETS-ENCRYPTION, ADDONS, IAM-FOR-PODS, CLUSTER-ACCESS, IPV6, FARGATE.
  Rule-id pattern relaxed to allow `K8S`.
- Network generator exports `network_tf_labels` / `subnet_tf_labels` (compute now uses them, fixing
  a latent mismatch when a subnet is excluded) and renders `secondary_ip_range` from the subnet hint.
- `google_container_cluster` generation; coverage for cluster fields.
- Tests: every rule; generator (endpoint variants, channel, add-ons, guards); subnet ranges.
