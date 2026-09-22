# Spec 5, Plan 1 of 4: Kubernetes IR, Collection, Normalization

> Lean plan. Spec: `docs/superpowers/specs/2026-09-22-kubernetes-eks-gke-design.md` §2.2, §2.3, §4.

**Goal:** `scan` + `analyze` produce `kubernetes_cluster` and `node_pool` objects; compute no longer
migrates self-managed Kubernetes nodes. No differences, no generation (the placeholder generator
emits nothing and declares every field in `FIELD_COVERAGE` as `not_generated`).

## Tasks
1. `ir/kubernetes.py`: `KubernetesCluster`, `NodePool` (+ `Taint`, `Addon`, `PodIdentity`,
   `FargateProfile`, `FargateSelector` models). `references()`: cluster → subnets + policies;
   node pool → cluster + subnets + policies. Validators: `min ≤ desired ≤ max`; taint effect in
   `NO_SCHEDULE|NO_EXECUTE|PREFER_NO_SCHEDULE`; CIDRs canonical.
2. `plugins/kubernetes/collect_aws.py` (spec §4.1 table; own pagination helper; each denied call is
   a scan gap and leaves the rest collected). Self-managed groups = ASGs with a
   `kubernetes.io/cluster/<name>` tag; launch-template versions and instance types for those groups
   and for node groups' launch templates / instance types.
3. `plugins/kubernetes/normalize.py`: clusters (non-ACTIVE → info, skipped), managed node groups
   (non-ACTIVE/DEGRADED → info), self-managed groups (cluster from the tag; shape from the launch
   template version the ASG references), Fargate profiles on the cluster. Node pools carry the
   cluster SG plus launch-template SGs. Ids via `allocate_ids`.
4. `KubernetesPlugin`, registry order `(Network, Compute, Kubernetes, Database)`; compute:
   `_standalone_groups` also skips `kubernetes.io/cluster/*` ASGs, and instances with that tag get
   `eks_cluster`.
5. Tests: IR integrity/validators; moto collection (clusters, node groups, Fargate, a denied call);
   normalization from builder snapshots incl. self-managed and non-ACTIVE; compute exclusions;
   `policy_members` counts a node pool; plugin order check passes.
