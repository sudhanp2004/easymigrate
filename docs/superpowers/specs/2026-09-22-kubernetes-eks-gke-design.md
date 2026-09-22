# EasyMigrate — Kubernetes: EKS → GKE (Spec 5)

Status: draft 2026-09-22
Builds on specs 1 (pipeline, networking), 2 (`Decision.params`) and 4 (compute: the tag seam,
`policy_members`, zone mapping, `gce_facts.py`). Spec 3's UI gains these kinds without change.

## 1. Scope

Fourth family. Depth-first, as every family before it: collection, faithful IR, a complete
difference catalogue, generation, and a live testbed run.

### In scope

- `aws_eks_cluster` → `google_container_cluster`, **GKE Standard, regional**.
- EKS **managed node groups** → `google_container_node_pool`.
- EKS **self-managed node groups** (Auto Scaling groups tagged `kubernetes.io/cluster/<name>`)
  → node pools, shaped from their launch template, at high risk (§5 `K8S-SELF-MANAGED-NODES`).
- **Fargate profiles**: their pods run on the cluster's node pools; a Fargate-only cluster gets a
  default node pool (§5 `K8S-FARGATE`).
- Cluster networking: the node subnet gains pod and service **secondary ranges** (§2.1).
- Version, endpoint access, logging, secrets encryption, add-ons, IAM-for-pods and cluster access
  are each a named difference.

### Out of scope

- **Workloads.** Nothing inside the cluster is read or migrated: Deployments, Services, Ingresses,
  ConfigMaps, Secrets, CRDs, Helm releases, RBAC, PersistentVolumes. The Kubernetes API is never
  called, so scan keeps needing only read-only AWS credentials. The migrated cluster is **empty**,
  and the report says so at the same register as `VM-BOOT-IMAGE`. A workload inventory (reading the
  Kubernetes API to name what will break) is the natural next spec.
- **Autopilot.** Standard only (decision D2 below).
- IAM translation (IRSA / Pod Identity → Workload Identity bindings): reported, never translated.
- EKS Anywhere, Outposts, Karpenter `NodePool` objects (they live in the Kubernetes API),
  IPv6 clusters beyond a manual difference, custom networking (`ENIConfig`), security groups for pods.

## 2. Changes outside the family

### 2.1 Subnet secondary ranges (network family)

GKE VPC-native clusters take pod and service IPs from **secondary ranges on the node subnet**. EKS's
VPC CNI instead gives pods addresses from the subnet itself. So a cluster needs something the
network generator has never emitted: `secondary_ip_range` blocks on a `google_compute_subnetwork`.

Following the rules-decide/generation-renders contract, `K8S-POD-RANGES` (§5) writes a
`secondary_ranges` hint **on the subnet object** (`[{"name": …, "cidr": …}]`), and the network
generator renders one `secondary_ip_range` block per entry, sorted by name. The subnet stays owned
by the network family; only the hint crosses. No new IR field.

### 2.2 Compute stops migrating Kubernetes nodes

Spec 4 already leaves EKS managed node groups alone (`eks:cluster-name`). Self-managed node groups
are plain ASGs and would today become managed instance groups whose VMs never join a cluster.
Compute's `_standalone_groups` also skips ASGs carrying any `kubernetes.io/cluster/<name>` tag
(info `VM-ASG-EKS`), and an instance carrying that tag is captured with `eks_cluster` set.

### 2.3 Plugin order

`KubernetesPlugin.requires = {"network"}`. `PLUGINS` becomes
`(Network, Compute, Kubernetes, Database)`.

## 3. Architecture

```
plugins/kubernetes/
    __init__.py        KubernetesPlugin
    collect_aws.py     eks describe_* calls + the self-managed ASGs and their launch templates
    normalize.py       snapshot -> kubernetes_cluster, node_pool
    generate_gcp.py    resolved IR -> terraform/kubernetes.tf
ir/kubernetes.py
differences/rules/aws_gcp/kubernetes.py   K8S-* rules
differences/rules/aws_gcp/gke_facts.py    GKE versions, image types, add-on map (dated)
```

Machine-type and zone logic is reused from spec 4 (`gce_facts.nearest_predefined`,
`VmZoneMapping`-style positional letters, `providers/gcp/zones.py`), not copied.

## 4. Collection and IR

### 4.1 AWS calls

| Call | Snapshot file |
|---|---|
| `eks:ListClusters`, `DescribeCluster` | `kubernetes.clusters.json` |
| `ListNodegroups`, `DescribeNodegroup` | `kubernetes.nodegroups.json` |
| `ListFargateProfiles`, `DescribeFargateProfile` | `kubernetes.fargate_profiles.json` |
| `ListAddons`, `DescribeAddon` | `kubernetes.addons.json` |
| `ListAccessEntries` | `kubernetes.access_entries.json` |
| `ListPodIdentityAssociations` | `kubernetes.pod_identities.json` |
| `autoscaling:DescribeAutoScalingGroups` (filtered to `kubernetes.io/cluster/*`) | `kubernetes.self_managed_groups.json` |
| `ec2:DescribeLaunchTemplateVersions`, `DescribeInstanceTypes` for those groups and node groups | `kubernetes.launch_templates.json`, `kubernetes.instance_types.json` |

Each denied call becomes a `scan_gap` through `CollectContext.access_denied`. The kubernetes plugin
makes its own ASG, launch-template and instance-type calls rather than reading compute's snapshot
(plugins stay independently runnable; spec 4 §4.1 precedent). `aws_resource_types`: `eks:cluster`,
`eks:nodegroup`, `eks:fargateprofile`, `eks:addon`, `eks:identityproviderconfig`,
`eks:podidentityassociation`, `eks:access-entry`.

### 4.2 IR — two kinds

#### `kubernetes_cluster`

| Field | From |
|---|---|
| `version` | `version`, e.g. `"1.31"` |
| `subnets` | `resourcesVpcConfig.subnetIds` → subnet refs |
| `firewall_policies` | `clusterSecurityGroupId` + `securityGroupIds` → policy refs |
| `endpoint_public`, `endpoint_private` | `endpointPublicAccess`, `endpointPrivateAccess` |
| `public_access_cidrs` | `publicAccessCidrs` (canonicalized) |
| `service_cidr` | `kubernetesNetworkConfig.serviceIpv4Cidr` |
| `ip_family` | `"ipv4"` \| `"ipv6"` |
| `log_types` | enabled `logging.clusterLogging[].types` |
| `secrets_kms_key` | `encryptionConfig[].provider.keyArn` where resources include `secrets` |
| `oidc_issuer` | `identity.oidc.issuer` |
| `addons` | `[{name, version}]` |
| `access_entries` | principal ARNs (names only) |
| `pod_identities` | `[{namespace, service_account, role_arn}]` |
| `fargate_profiles` | `[{name, selectors: [{namespace, labels}]}]` |
| `authentication_mode` | `accessConfig.authenticationMode` |

#### `node_pool`

| Field | From |
|---|---|
| `cluster` | cluster ref |
| `origin` | `"managed"` \| `"self_managed"` |
| `instance_types` | `instanceTypes` / launch template (first is primary) |
| `vcpus`, `memory_mib` | of the primary type; nullable |
| `capacity` | `"on_demand"` \| `"spot"` |
| `min_size`, `max_size`, `desired_size` | `scalingConfig` / ASG |
| `subnets` | subnet refs |
| `disk_size_gib` | `diskSize` / launch template root mapping |
| `ami_type` | `amiType` (`AL2_x86_64`, `AL2023_x86_64_STANDARD`, `BOTTLEROCKET_*`, `WINDOWS_*`, `CUSTOM`, …); `"CUSTOM"` for self-managed |
| `labels`, `taints` | `labels`, `taints[{key, value, effect}]` |
| `firewall_policies` | launch-template SGs + the cluster SG (EKS attaches it to managed nodes) |
| `ssh_key` | `remoteAccess.ec2SshKey` |
| `max_unavailable` | `updateConfig` (count or percent) |
| `version` | node group Kubernetes version |

`node_pool.references()` returns the cluster, subnets and policies, so `policy_members` sees node
pools and `DB-SG-FIREWALL` keeps a policy shared by RDS and a cluster (spec 4 §2.1 applies as-is).

### 4.3 Normalization

- A cluster not `ACTIVE`, or a node group not `ACTIVE`/`DEGRADED`, is an `info` finding and is not
  translated.
- Fargate profiles are captured on the cluster; they are not objects.
- The normalizer makes no GCP judgements.

## 5. Difference catalogue (`K8S-*`)

| Rule | Detects | Strategies (**default** first) → hints |
|---|---|---|
| `K8S-EMPTY-CLUSTER` | every cluster | **`note_only`** (high): the GKE cluster is created with no workloads, configuration or volumes; nothing running on EKS is copied |
| `K8S-VERSION` | every cluster | **`pin_minor`** (low when GKE offers the minor; medium when it must move to the nearest newer offered minor, or when the minor is newer than EasyMigrate's dated list) → `version`, `release_channel = "UNSPECIFIED"`, node auto-upgrade off · `regular_channel` (medium) → `release_channel = "REGULAR"`, no pin, auto-upgrade on |
| `K8S-ENDPOINT` | every cluster | **`mirror`**: public endpoint with authorized networks = `public_access_cidrs` (**high** when that includes `0.0.0.0/0`, low otherwise), or private endpoint when EKS was private-only · `private_endpoint` (medium): reachable only from inside the VPC |
| `K8S-POD-RANGES` | every cluster | **`secondary_ranges`** (medium; params `subnet`, `pods_cidr`, `services_cidr`) → subnet hint `secondary_ranges`, cluster hints `subnet`, `pods_range`, `services_range`. Pods stop having VPC addresses; anything that allow-listed pod IPs must change. Defaults: the subnet hosting the most node groups (ties: sorted first); services keep the EKS `service_cidr` when it overlaps nothing in the network (so `kube-dns`'s address survives), else the next free `/20` in `100.100.0.0/16`; pods get the next free `/16` in `100.64.0.0/10`. Validation: valid CIDRs, no overlap with any subnet or other cluster's ranges, pods ≥ `/21`, services ≥ `/27` |
| `K8S-PRIVATE-NODES` | every node pool | **`private_nodes`** (medium): nodes have no external IPs; pulls from public registries need Cloud NAT on the subnet · `public_nodes` (high) |
| `K8S-NODE-MACHINE-TYPE` | every node pool | as `VM-MACHINE-TYPE` (`nearest_predefined` / `custom_machine_type` / `manual`), on the primary type; risk medium when `instance_types` had more than one type, since a node pool has one |
| `K8S-NODE-IMAGE` | every node pool | **`cos`** (low) → `COS_CONTAINERD` for AL2, AL2023 and Bottlerocket · `ubuntu` (low) → `UBUNTU_CONTAINERD`; Windows → `WINDOWS_LTSC_CONTAINERD` (medium); `CUSTOM` AMIs: medium, the custom image and its bootstrap are not carried |
| `K8S-NODE-SCALING` | every node pool | **`autoscale_totals`** (medium) → `total_min`, `total_max`, `initial_per_zone = ceil(desired / zones)`; GKE's autoscaler is built in and **on**, where EKS scaled only if cluster-autoscaler or Karpenter was installed; offered when `min < max` · `fixed_size` (low) → `node_count_per_zone = ceil(desired / zones)`. Either way a regional pool has at least one node per zone, so the total can exceed `desired`; the effect names the resulting count |
| `K8S-NODE-SPOT` | `capacity = "spot"` | **`spot`** (medium) · `standard` (low) |
| `K8S-SELF-MANAGED-NODES` | `origin = "self_managed"` | **`node_pool`** (high): shape from the launch template; bootstrap user data, custom AMI and any `--kubelet-extra-args` labels or taints are **not** carried, so workloads selecting those labels will not schedule · `omit` → excluded |
| `K8S-FARGATE` | cluster with Fargate profiles | **`schedule_on_nodes`** (medium): the selected pods run on ordinary nodes. When the cluster has no node pools, a default pool is generated (params `machine_type` default `e2-standard-4`, `nodes_per_zone` default 1) → hint `default_pool` |
| `K8S-LOGGING` | every cluster | **`map_components`** (low) → `logging_components`: `api`→`APISERVER`, `controllerManager`→`CONTROLLER_MANAGER`, `scheduler`→`SCHEDULER`, always `SYSTEM_COMPONENTS`; `audit` → Cloud Audit Logs (admin activity is always on; data access logs are not configured); `authenticator` has no equivalent. Workload logging is **not** enabled, matching EKS, which never shipped container logs by default |
| `K8S-SECRETS-ENCRYPTION` | `secrets_kms_key` set | **`google_managed`** (low): secrets encrypted at rest by Google, not by application-layer envelope encryption · `manual` |
| `K8S-ADDONS` | cluster with add-ons | **`map_addons`** (low, medium if any unmapped): `vpc-cni`, `kube-proxy`, `coredns`, `eks-pod-identity-agent` → GKE built-ins; `aws-ebs-csi-driver` → the GKE Persistent Disk CSI driver (on by default; storage classes must change); `aws-efs-csi-driver` → Filestore CSI (enabled → hint `filestore_csi`); anything else named as not migrated |
| `K8S-IAM-FOR-PODS` | `oidc_issuer` or `pod_identities` | **`manual`**, not excluding: Workload Identity is enabled on the cluster; each role binding is listed for the user to recreate as a Google service account binding |
| `K8S-CLUSTER-ACCESS` | every cluster | **`manual`**, not excluding: access entries / `aws-auth` mappings do not carry; only Google IAM principals with `container.*` roles can use the new cluster |
| `K8S-IPV6` | `ip_family = "ipv6"` | **`manual`**: generated IPv4-only |
| `K8S-SSH-KEY` | node pool with `ssh_key` | **`note_only`** (low): GKE nodes are reached with `gcloud compute ssh` / OS Login |

Order: cluster rules first (`K8S-VERSION` before node rules, because node version follows it;
`K8S-POD-RANGES` before `K8S-FARGATE`), then node-pool rules, `K8S-NODE-MACHINE-TYPE` before
`K8S-NODE-IMAGE` (Windows images constrain machine types).

## 6. Generation (`terraform/kubernetes.tf`)

- `google_container_cluster`: `location = var.region`, `network`, `subnetwork` (hint),
  `remove_default_node_pool = true`, `initial_node_count = 1`, `ip_allocation_policy` with the two
  range names, `min_master_version` + `release_channel`, `private_cluster_config` /
  `master_authorized_networks_config`, `workload_identity_config` (`<project>.svc.id.goog`),
  `logging_config`, `addons_config` (Filestore when hinted), and **`deletion_protection = false`**:
  the provider defaults it to true, EKS has no such setting, and a true default blocks teardown.
- `google_container_node_pool` per live pool (plus the `K8S-FARGATE` default pool): `node_locations`
  from positional zones of its subnets' AZs resolved through `providers/gcp/zones.py`,
  `autoscaling { total_min_node_count, total_max_node_count, location_policy = "BALANCED" }` or
  `node_count`, `initial_node_count`, `node_config` (machine type, disk, image type, spot, labels,
  `taint` blocks, `tags` from `policy_network_tags`, `workload_metadata_config = GKE_METADATA`),
  `management { auto_repair = true, auto_upgrade }`, `upgrade_settings` from `max_unavailable`.
- Network generator: `secondary_ip_range` blocks from the subnet hint (§2.1).
- `GenerationError` for missing `version`, `subnet` or range hints, a node pool whose cluster is
  excluded, and a pool with no zones.

## 7. Report

A `## Kubernetes` section per cluster: GKE version and channel, endpoint exposure, pod/service
ranges, each node pool (machine type, image, node counts per zone and total, what was not carried),
IAM-for-pods bindings to recreate, and access to re-grant. Closing paragraph: the cluster is empty;
no workloads, manifests, secrets or volumes were migrated.

## 8. Testing

Unit tests per rule (detection, strategies, params, risk); golden `expected_kubernetes.tf` for
fixtures: managed node group cluster, private endpoint + KMS + logging, self-managed group,
Fargate-only, spot + taints + Windows, add-ons + pod identity; provider-schema (`tofu`) validation;
network golden with secondary ranges; compute tests for the §2.2 exclusions; live testbed run.

## 9. Decisions taken (delegated 2026-09-22)

- **D1 Cluster infra only** (user). Workloads need the Kubernetes API and credentials a read-only
  AWS scan does not have; a workload inventory is the follow-on spec.
- **D2 GKE Standard only** (user). Node groups map to node pools one to one; Autopilot hides every
  node setting inside one difference.
- **D3 Self-managed groups become node pools**, at high risk. Leaving them to compute produced VMs
  that never join a cluster; omitting them silently loses capacity. The risk text names what does
  not carry.
- **D4 Fargate pods run on node pools.** GKE Standard has no serverless pods; scheduling them on
  nodes is what happens anyway once Fargate is gone, and a Fargate-only cluster needs a pool.
- **D5 Regional clusters.** EKS control planes are multi-AZ; a zonal GKE cluster would reduce
  control-plane availability. Costs the GKE management fee, which the report can name.
- **D6 Pin the minor, no release channel, by default.** EKS never auto-upgrades except at end of
  support; `UNSPECIFIED` matches that most closely. The channel is one strategy away.
- **D7 Private nodes by default**, consistent with `VM-PUBLIC-IP` and `DB-PUBLIC-ACCESS`.
- **D8 Endpoint mirrors the source** rather than defaulting private: a private-only default would
  lock the user's kubectl out of a cluster that was reachable before. A world-open endpoint is
  high risk, not silently narrowed.
- **D9 Autoscaling on by default when `min < max`**: the bounds were set for a reason, and GKE's
  autoscaler is the native equivalent; the uncertainty (EKS needed an installed autoscaler) is in
  the effect text.
- **D10 Keep the EKS service CIDR when possible**, because in-cluster DNS addresses are sometimes
  hard-coded; pods move to a CGNAT (`100.64.0.0/10`) secondary range, which never collides with
  RFC 1918 VPC ranges.
- **D11 `deletion_protection = false`** on the cluster, matching EKS and keeping teardown possible.

## 10. Plans

1. **IR, collection, normalization** — `ir/kubernetes.py`, `plugins/kubernetes/{collect_aws,
   normalize}.py`, plugin registration and order, compute's §2.2 exclusions, `policy_members` check.
2. **Cluster rules and generation** — `gke_facts.py`, the cluster-level `K8S-*` rules, the subnet
   secondary-range seam in the network generator, `google_container_cluster`.
3. **Node pools and report** — node-pool rules, `google_container_node_pool`, the Kubernetes report
   section, goldens and provider-schema tests.
4. **Live run** — testbed additions (add-ons, logging, a Fargate profile), apply, verify the GKE
   cluster and node pools come up and nodes register, record findings here.
