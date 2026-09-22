# Spec 7, Plan 1 of 4: Load-balancing IR, Collection, Normalization

> Lean plan. Spec §2-§4.

- `ir/loadbalancing.py`: `load_balancer` (listeners, rules, actions embedded) and `target_group`.
- Collection of ELBv2 load balancers, attributes, listeners, rules, target groups, attributes, target health, tags (batches of 20), ACM certificate domains, target instances (for Auto Scaling membership) and classic ELB names.
- Normalization: ASG-launched targets resolve to the `instance_group`, standalone targets to `compute_instance`, the rest are unresolved; host/path conditions captured, others named; auth/fixed-response actions typed; k8s-controller LBs marked.
- Compute exports `instance_tf_labels` and `group_tf_refs`, renders MIG `named_port` from a hint; VM-ASG-TARGET-GROUPS now says the attachment moves with the load balancer.
- Plugin registered after compute (`requires = {network, compute}`).
