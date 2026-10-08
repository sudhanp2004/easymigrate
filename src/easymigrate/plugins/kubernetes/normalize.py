"""Kubernetes normalizer: snapshot → `kubernetes_cluster` and `node_pool` (spec 5 §4.2, §4.3).

Faithful capture only. Subnet and security-group native ids resolve through `ctx.objects`, which
the network plugin fills first (`KubernetesPlugin.requires`).
"""

from __future__ import annotations

from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import IntentObject, SourceRef
from easymigrate.ir.kubernetes import (
    Addon,
    FargateProfile,
    FargateSelector,
    KubernetesCluster,
    NodePool,
    PodIdentity,
    Taint,
)
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.plugins.compute.aws_values import FALLBACK_INSTANCE_TYPES
from easymigrate.plugins.compute.normalize import group_template_spec
from easymigrate.plugins.kubernetes.collect_aws import SELF_MANAGED_TAG_PREFIX

_LIVE_NODEGROUP_STATES = {"ACTIVE", "DEGRADED", "UPDATING"}


def _source(ctx: NormalizeContext, type_: str, native_id: str) -> SourceRef:
    return SourceRef(cloud="aws", type=type_, id=native_id, region=ctx.region)


def _lookups(ctx: NormalizeContext) -> tuple[dict[str, str], dict[str, str]]:
    subnets: dict[str, str] = {}
    policies: dict[str, str] = {}
    for obj in ctx.objects.values():
        if obj.kind == "subnet":
            subnets[obj.source.id] = obj.id
        elif obj.kind == "firewall_policy":
            policies[obj.source.id] = obj.id
    return subnets, policies


def _sizes(types_raw: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    sizes = dict(FALLBACK_INSTANCE_TYPES)
    for entry in types_raw:
        vcpus = (entry.get("VCpuInfo") or {}).get("DefaultVCpus")
        memory = (entry.get("MemoryInfo") or {}).get("SizeInMiB")
        if vcpus is not None and memory is not None:
            sizes[entry["InstanceType"]] = (vcpus, memory)
    return sizes


def _refs(natives: list[str], lookup: dict[str, str], ctx: NormalizeContext, what: str, owner: str):
    refs = []
    for native in natives:
        if native in lookup:
            refs.append(lookup[native])
        else:
            ctx.info(
                "K8S-REF-UNRESOLVED",
                f"{owner} references {what} {native}, which is not in this scan; it is omitted.",
                subject=owner,
            )
    return sorted(set(refs))


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    clusters_raw: dict[str, Any] = snapshot.get("kubernetes.clusters") or {}
    nodegroups_raw: dict[str, list[Any]] = snapshot.get("kubernetes.nodegroups") or {}
    fargate_raw: dict[str, list[Any]] = snapshot.get("kubernetes.fargate_profiles") or {}
    addons_raw: dict[str, list[Any]] = snapshot.get("kubernetes.addons") or {}
    access_raw: dict[str, list[Any]] = snapshot.get("kubernetes.access_entries") or {}
    identities_raw: dict[str, list[Any]] = snapshot.get("kubernetes.pod_identities") or {}
    groups_raw = (snapshot.get("kubernetes.self_managed_groups") or {}).get("AutoScalingGroups", [])
    templates_raw: dict[str, dict[str, Any]] = snapshot.get("kubernetes.launch_templates") or {}
    sizes = _sizes((snapshot.get("kubernetes.instance_types") or {}).get("InstanceTypes", []))
    subnet_ids, policy_ids = _lookups(ctx)

    live = {}
    for name, cluster in sorted(clusters_raw.items()):
        if cluster.get("status", "ACTIVE") != "ACTIVE":
            ctx.info(
                "K8S-CLUSTER-NOT-ACTIVE",
                f"EKS cluster {name} is {cluster.get('status')}; it is reported but not migrated.",
                subject=name,
            )
            continue
        live[name] = cluster
    cluster_ids = allocate_ids("kubernetes_cluster", [(name, name) for name in live])

    objects: list[IntentObject] = []
    for name, cluster in live.items():
        objects.append(
            _cluster(
                cluster_ids[name],
                cluster,
                ctx,
                subnet_ids=subnet_ids,
                policy_ids=policy_ids,
                addons=addons_raw.get(name, []),
                access=access_raw.get(name, []),
                identities=identities_raw.get(name, []),
                fargate=fargate_raw.get(name, []),
            )
        )

    pools: list[tuple[str, str, dict[str, Any]]] = []  # (native id, kind, payload)
    for name in live:
        for nodegroup in nodegroups_raw.get(name, []):
            native = f"{name}/{nodegroup['nodegroupName']}"
            if nodegroup.get("status", "ACTIVE") not in _LIVE_NODEGROUP_STATES:
                ctx.info(
                    "K8S-NODEGROUP-NOT-ACTIVE",
                    f"node group {native} is {nodegroup.get('status')}; it is reported but "
                    "not migrated.",
                    subject=native,
                )
                continue
            pools.append((native, "managed", nodegroup))
    for group in groups_raw:
        cluster_name = _self_managed_cluster(group)
        if cluster_name not in live:
            ctx.info(
                "K8S-SELF-MANAGED-ORPHAN",
                f"Auto Scaling group {group['AutoScalingGroupName']} is tagged for cluster "
                f"{cluster_name}, which is not in this scan; it is not migrated.",
                subject=group["AutoScalingGroupName"],
            )
            continue
        pools.append((f"{cluster_name}/{group['AutoScalingGroupName']}", "self_managed", group))

    pool_ids = allocate_ids(
        "node_pool", [(native.split("/", 1)[1], native) for native, _, _ in pools]
    )
    for native, origin, payload in pools:
        cluster_name = native.split("/", 1)[0]
        cluster = live[cluster_name]
        common = dict(
            id=pool_ids[native],
            cluster=cluster_ids[cluster_name],
            ctx=ctx,
            subnet_ids=subnet_ids,
            policy_ids=policy_ids,
            templates=templates_raw,
            sizes=sizes,
            cluster_sg=(cluster.get("resourcesVpcConfig") or {}).get("clusterSecurityGroupId"),
        )
        if origin == "managed":
            objects.append(_managed_pool(native, payload, **common))
        else:
            objects.append(_self_managed_pool(native, payload, **common))
    return objects


def _self_managed_cluster(group: dict[str, Any]) -> str:
    for tag in group.get("Tags") or []:
        key = tag.get("Key", "")
        if key.startswith(SELF_MANAGED_TAG_PREFIX):
            return key[len(SELF_MANAGED_TAG_PREFIX) :]
    return ""


def _cluster(
    object_id: str,
    cluster: dict[str, Any],
    ctx: NormalizeContext,
    *,
    subnet_ids: dict[str, str],
    policy_ids: dict[str, str],
    addons: list[dict[str, Any]],
    access: list[str],
    identities: list[dict[str, Any]],
    fargate: list[dict[str, Any]],
) -> KubernetesCluster:
    vpc = cluster.get("resourcesVpcConfig") or {}
    network = cluster.get("kubernetesNetworkConfig") or {}
    name = cluster["name"]
    policies = [vpc["clusterSecurityGroupId"]] if vpc.get("clusterSecurityGroupId") else []
    policies += vpc.get("securityGroupIds") or []
    log_types = sorted(
        {
            log_type
            for entry in (cluster.get("logging") or {}).get("clusterLogging") or []
            if entry.get("enabled")
            for log_type in entry.get("types") or []
        }
    )
    kms = next(
        (
            (entry.get("provider") or {}).get("keyArn")
            for entry in cluster.get("encryptionConfig") or []
            if "secrets" in (entry.get("resources") or [])
        ),
        None,
    )
    return KubernetesCluster(
        id=object_id,
        name=name,
        labels=clean_labels(cluster.get("tags") or {}),
        source=_source(ctx, "eks-cluster", name),
        version=str(cluster.get("version", "")),
        subnets=_refs(vpc.get("subnetIds") or [], subnet_ids, ctx, "subnet", name),
        firewall_policies=_refs(policies, policy_ids, ctx, "security group", name),
        endpoint_public=bool(vpc.get("endpointPublicAccess", True)),
        endpoint_private=bool(vpc.get("endpointPrivateAccess", False)),
        public_access_cidrs=sorted(vpc.get("publicAccessCidrs") or []),
        service_cidr=network.get("serviceIpv4Cidr"),
        ip_family="ipv6" if network.get("ipFamily") == "ipv6" else "ipv4",
        log_types=log_types,
        secrets_kms_key=kms,
        oidc_issuer=((cluster.get("identity") or {}).get("oidc") or {}).get("issuer"),
        addons=sorted(
            (Addon(name=a["addonName"], version=a.get("addonVersion")) for a in addons),
            key=lambda a: a.name,
        ),
        access_entries=sorted(access),
        pod_identities=sorted(
            (
                PodIdentity(
                    namespace=i.get("namespace", ""),
                    service_account=i.get("serviceAccount", ""),
                    role_arn=i.get("roleArn"),
                )
                for i in identities
            ),
            key=lambda i: (i.namespace, i.service_account),
        ),
        fargate_profiles=sorted(
            (
                FargateProfile(
                    name=p["fargateProfileName"],
                    selectors=[
                        FargateSelector(
                            namespace=s.get("namespace", ""), labels=s.get("labels") or {}
                        )
                        for s in p.get("selectors") or []
                    ],
                )
                for p in fargate
            ),
            key=lambda p: p.name,
        ),
        authentication_mode=(cluster.get("accessConfig") or {}).get("authenticationMode"),
        role_arn=cluster.get("roleArn"),
        auto_mode=bool((cluster.get("computeConfig") or {}).get("enabled")),
    )


def _template_data(templates: dict[str, dict[str, Any]], template_id: str | None, version) -> dict:
    if not template_id:
        return {}
    versions = templates.get(template_id) or {}
    return versions.get(str(version or "$Default")) or next(iter(versions.values()), {})


def _template_sgs(data: dict[str, Any]) -> list[str]:
    groups = list(data.get("SecurityGroupIds") or [])
    for interface in data.get("NetworkInterfaces") or []:
        groups += interface.get("Groups") or []
    return groups


_ROOT_DEVICES = ("/dev/xvda", "/dev/sda1", "/dev/xvda1", "/dev/nvme0n1")


def _root_size(data: dict[str, Any]) -> int | None:
    """The root volume's size: a template may list a data disk first (review, 2026-10-08)."""
    sized = {
        m.get("DeviceName"): (m.get("Ebs") or {}).get("VolumeSize")
        for m in data.get("BlockDeviceMappings") or []
        if (m.get("Ebs") or {}).get("VolumeSize")
    }
    for device in _ROOT_DEVICES:
        if device in sized:
            return sized[device]
    return next(iter(sized.values()), None)


def _managed_pool(
    native: str,
    nodegroup: dict[str, Any],
    *,
    id: str,  # noqa: A002 - mirrors the IR field
    cluster: str,
    ctx: NormalizeContext,
    subnet_ids: dict[str, str],
    policy_ids: dict[str, str],
    templates: dict[str, dict[str, Any]],
    sizes: dict[str, tuple[int, int]],
    cluster_sg: str | None,
) -> NodePool:
    spec = nodegroup.get("launchTemplate") or {}
    data = _template_data(templates, spec.get("id"), spec.get("version"))
    instance_types = list(nodegroup.get("instanceTypes") or [])
    if not instance_types and data.get("InstanceType"):
        instance_types = [data["InstanceType"]]
    # A launch template that names security groups replaces the cluster group EKS would
    # otherwise attach; without one, nodes get the cluster group plus any remote-access group.
    groups = _template_sgs(data)
    if not groups:
        groups = [cluster_sg] if cluster_sg else []
        remote = (nodegroup.get("resources") or {}).get("remoteAccessSecurityGroup")
        if remote:
            groups.append(remote)
    scaling = nodegroup.get("scalingConfig") or {}
    update = nodegroup.get("updateConfig") or {}
    vcpus, memory = sizes.get(instance_types[0], (None, None)) if instance_types else (None, None)
    return NodePool(
        id=id,
        name=nodegroup["nodegroupName"],
        labels=clean_labels(nodegroup.get("tags") or {}),
        source=_source(ctx, "eks-nodegroup", native),
        cluster=cluster,
        origin="managed",
        instance_types=instance_types,
        vcpus=vcpus,
        memory_mib=memory,
        capacity="spot" if nodegroup.get("capacityType") == "SPOT" else "on_demand",
        min_size=scaling.get("minSize", 0),
        max_size=scaling.get("maxSize", 0),
        desired_size=scaling.get("desiredSize", 0),
        subnets=_refs(nodegroup.get("subnets") or [], subnet_ids, ctx, "subnet", native),
        disk_size_gib=nodegroup.get("diskSize") or _root_size(data),
        ami_type=nodegroup.get("amiType") or ("CUSTOM" if data.get("ImageId") else "AL2_x86_64"),
        node_labels=dict(sorted((nodegroup.get("labels") or {}).items())),
        taints=[
            Taint(key=t["key"], value=t.get("value"), effect=t["effect"])
            for t in sorted(nodegroup.get("taints") or [], key=lambda t: t["key"])
        ],
        firewall_policies=_refs(groups, policy_ids, ctx, "security group", native),
        ssh_key=(nodegroup.get("remoteAccess") or {}).get("ec2SshKey"),
        max_unavailable=update.get("maxUnavailable"),
        max_unavailable_percent=update.get("maxUnavailablePercentage"),
        version=nodegroup.get("version"),
        role_arn=nodegroup.get("nodeRole"),
    )


def _self_managed_pool(
    native: str,
    group: dict[str, Any],
    *,
    id: str,  # noqa: A002 - mirrors the IR field
    cluster: str,
    ctx: NormalizeContext,
    subnet_ids: dict[str, str],
    policy_ids: dict[str, str],
    templates: dict[str, dict[str, Any]],
    sizes: dict[str, tuple[int, int]],
    cluster_sg: str | None,
) -> NodePool:
    spec = group_template_spec(group)
    data = _template_data(
        templates,
        spec.get("LaunchTemplateId") or spec.get("LaunchTemplateName"),
        spec.get("Version"),
    )
    instance_types = [data["InstanceType"]] if data.get("InstanceType") else []
    overrides = ((group.get("MixedInstancesPolicy") or {}).get("LaunchTemplate") or {}).get(
        "Overrides"
    ) or []
    instance_types += [o["InstanceType"] for o in overrides if o.get("InstanceType")]
    instance_types = list(dict.fromkeys(instance_types))
    subnets = [s for s in (group.get("VPCZoneIdentifier") or "").split(",") if s]
    vcpus, memory = sizes.get(instance_types[0], (None, None)) if instance_types else (None, None)
    return NodePool(
        id=id,
        name=group["AutoScalingGroupName"],
        labels=clean_labels({t["Key"]: t.get("Value", "") for t in group.get("Tags") or []}),
        source=_source(ctx, "autoScalingGroup", native),
        cluster=cluster,
        origin="self_managed",
        instance_types=instance_types,
        vcpus=vcpus,
        memory_mib=memory,
        capacity="on_demand",
        min_size=group.get("MinSize", 0),
        max_size=group.get("MaxSize", 0),
        desired_size=group.get("DesiredCapacity", 0),
        subnets=_refs(subnets, subnet_ids, ctx, "subnet", native),
        disk_size_gib=_root_size(data),
        ami_type="CUSTOM",
        firewall_policies=_refs(_template_sgs(data), policy_ids, ctx, "security group", native),
        ssh_key=data.get("KeyName"),
    )
