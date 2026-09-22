"""Compute plugin normalizer: snapshot → compute IR objects (spec 4 §4.2, §4.3).

Faithful capture only: nothing here decides whether GCP can reproduce what it records. Subnet
and security-group native ids resolve to IR ids through `ctx.objects`, which the network plugin
has already populated — `ComputePlugin.requires` declares that ordering and
`pipeline.check_plugin_order` enforces it (spec 4 §2.2).

This module never writes `FirewallPolicy.applies_to`. Membership is expressed as forward
references from the instance, which `IRDocument` validates (spec 4 §2.1).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.ir.base import Finding, IntentObject, SourceRef
from easymigrate.ir.compute import (
    ComputeInstance,
    Disk,
    ExternalAddress,
    ImdsOptions,
    InstanceGroup,
    InstanceTemplate,
    ScalingPolicy,
)
from easymigrate.ir.naming import allocate_ids, clean_labels
from easymigrate.plugins.compute.aws_values import (
    ASG_MEMBER_TAG,
    EKS_CLUSTER_TAG,
    FALLBACK_INSTANCE_TYPES,
    TRANSIENT_STATES,
    UNSUPPORTED_REASONS,
    name_tag,
    tags,
)

_WINDOWS = "windows"

# (snapshot file, response key, id field, the `UNSUPPORTED_REASONS` key) — spec 4 §4.4.
_UNSUPPORTED = (
    ("compute.hosts", "Hosts", "HostId", "dedicated-host"),
    (
        "compute.spot_fleet_requests",
        "SpotFleetRequestConfigs",
        "SpotFleetRequestId",
        "spot-fleet-request",
    ),
    ("compute.fleets", "Fleets", "FleetId", "fleet"),
    (
        "compute.capacity_reservations",
        "CapacityReservations",
        "CapacityReservationId",
        "capacity-reservation",
    ),
)


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


def normalize(snapshot: Snapshot, ctx: NormalizeContext) -> list[IntentObject]:
    instances_raw = (snapshot.get("compute.instances") or {}).get("Instances", [])
    volumes_raw = (snapshot.get("compute.volumes") or {}).get("Volumes", [])
    addresses_raw = (snapshot.get("compute.addresses") or {}).get("Addresses", [])
    images_raw = (snapshot.get("compute.images") or {}).get("Images", [])
    key_pairs_raw = (snapshot.get("compute.key_pairs") or {}).get("KeyPairs", [])
    types_raw = (snapshot.get("compute.instance_types") or {}).get("InstanceTypes", [])
    templates_raw = snapshot.get("compute.launch_templates") or {}
    groups_raw = (snapshot.get("compute.autoscaling_groups") or {}).get("AutoScalingGroups", [])
    policies_raw = (snapshot.get("compute.scaling_policies") or {}).get("ScalingPolicies", [])
    attributes = snapshot.get("compute.instance_attributes") or {}
    hooks_raw = snapshot.get("compute.lifecycle_hooks") or {}

    subnet_ids, policy_ids = _lookups(ctx)
    images = {image["ImageId"]: image for image in images_raw}
    key_pairs = {pair["KeyName"]: pair for pair in key_pairs_raw if pair.get("KeyName")}
    sizes = _sizes(types_raw)
    volumes = {volume["VolumeId"]: volume for volume in volumes_raw}

    _report_unsupported(snapshot, ctx)
    groups_raw = _standalone_groups(groups_raw, ctx)

    translatable = _translatable_instances(instances_raw, ctx)

    addresses, address_by_instance = _addresses(addresses_raw, ctx)
    disks, disk_ids_by_instance = _disks(translatable, volumes, ctx)

    instances = _instances(
        translatable,
        ctx,
        subnet_ids=subnet_ids,
        policy_ids=policy_ids,
        images=images,
        key_pairs=key_pairs,
        sizes=sizes,
        attributes=attributes,
        disk_ids_by_instance=disk_ids_by_instance,
        address_by_instance=address_by_instance,
    )

    templates, template_disks, template_ids = _templates(
        templates_raw,
        groups_raw,
        ctx,
        policy_ids=policy_ids,
        images=images,
        sizes=sizes,
        key_pairs=key_pairs,
    )
    groups = _groups(
        groups_raw,
        policies_raw,
        hooks_raw,
        ctx,
        subnet_ids=subnet_ids,
        template_ids=template_ids,
    )

    return [
        *sorted([*disks, *template_disks], key=lambda o: o.id),
        *sorted(addresses, key=lambda o: o.id),
        *sorted(instances, key=lambda o: o.id),
        *sorted(templates, key=lambda o: o.id),
        *sorted(groups, key=lambda o: o.id),
    ]


def _report_unsupported(snapshot: Snapshot, ctx: NormalizeContext) -> None:
    """Resources this family deliberately does not migrate (spec 4 §4.4).

    They become no IR object; the point is that the user sees them named rather than
    assuming the instances they govern came across.
    """
    for file_name, key, id_field, reason_key in _UNSUPPORTED:
        for item in (snapshot.get(file_name) or {}).get(key, []):
            native = item.get(id_field, "")
            ctx.findings.append(
                Finding(
                    level="unsupported",
                    code="VM-RESOURCE-UNSUPPORTED",
                    message=f"{native} is not migrated: {UNSUPPORTED_REASONS[reason_key]}.",
                    subject=native,
                    suggestion=(
                        "Plan to migrate this by hand, or wait for a release that supports it."
                    ),
                )
            )


def _translatable_instances(
    instances_raw: list[dict[str, Any]], ctx: NormalizeContext
) -> list[dict[str, Any]]:
    keep = []
    for instance in instances_raw:
        state = (instance.get("State") or {}).get("Name", "")
        if state in TRANSIENT_STATES:
            ctx.info(
                "VM-STATE-TRANSIENT",
                f"{instance['InstanceId']} is {state}; it is reported but not migrated, because a "
                "half-built or half-destroyed instance is not a migration target.",
                subject=instance["InstanceId"],
            )
            continue
        instance_tags = tags(instance)
        group = instance_tags.get(ASG_MEMBER_TAG)
        if group and EKS_CLUSTER_TAG not in instance_tags:
            ctx.info(
                "VM-ASG-MEMBER",
                f"{instance['InstanceId']} was launched by Auto Scaling group {group}; the group "
                "migrates as a managed instance group that launches its own instances, so this "
                "instance is not migrated on its own.",
                subject=instance["InstanceId"],
            )
            continue
        keep.append(instance)
    return keep


def _standalone_groups(
    groups_raw: list[dict[str, Any]], ctx: NormalizeContext
) -> list[dict[str, Any]]:
    """Groups an EKS managed node group created belong to the EKS family (spec 4 §1)."""
    keep = []
    for group in groups_raw:
        group_tags = {tag["Key"]: tag.get("Value", "") for tag in group.get("Tags") or []}
        cluster = group_tags.get(EKS_CLUSTER_TAG)
        if cluster:
            ctx.info(
                "VM-ASG-EKS",
                f"Auto Scaling group {group['AutoScalingGroupName']} belongs to EKS cluster "
                f"{cluster}; it is left to the EKS family and not migrated here.",
                subject=group["AutoScalingGroupName"],
            )
            continue
        keep.append(group)
    return keep


def _sizes(types_raw: list[dict[str, Any]]) -> dict[str, tuple[int, int]]:
    sizes: dict[str, tuple[int, int]] = {}
    for entry in types_raw:
        vcpus = (entry.get("VCpuInfo") or {}).get("DefaultVCpus")
        memory = (entry.get("MemoryInfo") or {}).get("SizeInMiB")
        if vcpus is not None and memory is not None:
            sizes[entry["InstanceType"]] = (vcpus, memory)
    return sizes


def _addresses(
    addresses_raw: list[dict[str, Any]], ctx: NormalizeContext
) -> tuple[list[ExternalAddress], dict[str, str]]:
    ids = allocate_ids(
        "external_address",
        [(name_tag(item), item.get("AllocationId") or item["PublicIp"]) for item in addresses_raw],
    )
    objects: list[ExternalAddress] = []
    by_instance: dict[str, str] = {}
    for item in addresses_raw:
        native = item.get("AllocationId") or item["PublicIp"]
        object_id = ids[native]
        objects.append(
            ExternalAddress(
                id=object_id,
                name=name_tag(item) or native,
                labels=clean_labels(tags(item)),
                source=_source(ctx, "elastic-ip", native),
                address=item["PublicIp"],
                attached=bool(item.get("InstanceId") or item.get("NetworkInterfaceId")),
                scope="vpc" if item.get("Domain", "vpc") == "vpc" else "standard",
            )
        )
        if item.get("InstanceId"):
            by_instance[item["InstanceId"]] = object_id
    return objects, by_instance


def _disks(
    instances: list[dict[str, Any]], volumes: dict[str, dict[str, Any]], ctx: NormalizeContext
) -> tuple[list[Disk], dict[str, list[str]]]:
    """Disks are built per instance so the boot disk can be identified and ordered first."""
    wanted: list[tuple[str | None, str]] = []
    plan: dict[str, list[tuple[str, bool, bool]]] = {}

    for instance in instances:
        instance_id = instance["InstanceId"]
        root = instance.get("RootDeviceName")
        ebs: list[tuple[str, str]] = []
        scratch: list[str] = []
        for mapping in instance.get("BlockDeviceMappings") or []:
            volume_id = (mapping.get("Ebs") or {}).get("VolumeId")
            if volume_id:
                ebs.append((mapping["DeviceName"], volume_id))
            else:
                scratch.append(mapping["DeviceName"])

        boot = [pair for pair in ebs if pair[0] == root]
        rest = sorted(pair for pair in ebs if pair[0] != root)
        ordered: list[tuple[str, bool, bool]] = []
        for device, volume_id in [*boot, *rest]:
            ordered.append((volume_id, device == root, False))
        for device in sorted(scratch):
            ordered.append((f"{instance_id}{device}", False, True))
        plan[instance_id] = ordered

        for native, _, is_scratch in ordered:
            label = None if is_scratch else name_tag(volumes.get(native, {}))
            wanted.append((label, native))

    ids = allocate_ids("disk", wanted)
    objects: dict[str, Disk] = {}
    by_instance: dict[str, list[str]] = {}

    for instance in instances:
        instance_id = instance["InstanceId"]
        refs: list[str] = []
        for native, is_boot, is_scratch in plan[instance_id]:
            object_id = ids[native]
            refs.append(object_id)
            if object_id in objects:
                ctx.info(
                    "VM-DISK-MULTI-ATTACH",
                    f"volume {native} is attached to more than one instance; it is recorded once "
                    "and referenced by each.",
                    subject=native,
                )
                continue
            objects[object_id] = _disk(
                object_id, native, is_boot, is_scratch, volumes.get(native, {}), instance, ctx
            )
        by_instance[instance_id] = refs

    return list(objects.values()), by_instance


def _disk(
    object_id: str,
    native: str,
    is_boot: bool,
    is_scratch: bool,
    volume: dict[str, Any],
    instance: dict[str, Any],
    ctx: NormalizeContext,
) -> Disk:
    zone = volume.get("AvailabilityZone") or (instance.get("Placement") or {}).get(
        "AvailabilityZone", ""
    )
    if is_scratch:
        return Disk(
            id=object_id,
            name=native,
            source=_source(ctx, "instance-store", native),
            zone=zone,
            boot=False,
            instance_store=True,
        )
    return Disk(
        id=object_id,
        name=name_tag(volume) or native,
        labels=clean_labels(tags(volume)),
        source=_source(ctx, "volume", native),
        size_gib=volume.get("Size"),
        type=volume.get("VolumeType"),
        iops=volume.get("Iops") if volume.get("VolumeType") in {"gp3", "io1", "io2"} else None,
        throughput_mibps=volume.get("Throughput") if volume.get("VolumeType") == "gp3" else None,
        encrypted=bool(volume.get("Encrypted")),
        kms_key=volume.get("KmsKeyId"),
        zone=zone,
        boot=is_boot,
    )


def _instances(
    instances_raw: list[dict[str, Any]],
    ctx: NormalizeContext,
    *,
    subnet_ids: dict[str, str],
    policy_ids: dict[str, str],
    images: dict[str, dict[str, Any]],
    key_pairs: dict[str, dict[str, Any]],
    sizes: dict[str, tuple[int, int]],
    attributes: dict[str, Any],
    disk_ids_by_instance: dict[str, list[str]],
    address_by_instance: dict[str, str],
) -> list[ComputeInstance]:
    ids = allocate_ids(
        "compute_instance",
        [(name_tag(item), item["InstanceId"]) for item in instances_raw],
    )
    objects: list[ComputeInstance] = []
    for item in instances_raw:
        native = item["InstanceId"]
        subnet_native = item.get("SubnetId")
        subnet = subnet_ids.get(subnet_native or "")
        if subnet is None:
            ctx.findings.append(
                _scan_gap(
                    "SCAN-VM-SUBNET-UNRESOLVED",
                    f"{native} is in subnet {subnet_native!r}, which is not in this scan; the "
                    "instance is not migrated.",
                    native,
                )
            )
            continue

        policies = []
        for group in item.get("SecurityGroups") or []:
            resolved = policy_ids.get(group.get("GroupId", ""))
            if resolved is None:
                ctx.findings.append(
                    _scan_gap(
                        "SCAN-VM-SG-UNRESOLVED",
                        f"{native} references security group {group.get('GroupId')!r}, which is "
                        "not in this scan; it is omitted from the instance's firewall policies.",
                        native,
                    )
                )
                continue
            policies.append(resolved)

        interfaces = item.get("NetworkInterfaces") or []
        if len(interfaces) > 1:
            others = ", ".join(sorted(i["NetworkInterfaceId"] for i in interfaces[1:]))
            ctx.info(
                "VM-MULTI-NIC",
                f"{native} has more than one network interface; the primary interface's subnet is "
                f"used and these are not migrated: {others}.",
                subject=native,
            )

        instance_tags = tags(item)
        eks_cluster = instance_tags.get(EKS_CLUSTER_TAG)
        if eks_cluster:
            ctx.info(
                "VM-EKS-MEMBER",
                f"{native} belongs to EKS cluster {eks_cluster!r}; it is recorded in the inventory "
                "and left to the EKS family.",
                subject=native,
            )

        image = images.get(item.get("ImageId", ""), {})
        vcpus, memory = _size_for(item.get("InstanceType", ""), sizes, native, ctx)

        objects.append(
            ComputeInstance(
                id=ids[native],
                name=name_tag(item) or native,
                labels=clean_labels(instance_tags),
                source=_source(ctx, "instance", native),
                instance_type=item.get("InstanceType", ""),
                vcpus=vcpus,
                memory_mib=memory,
                architecture=item.get("Architecture", "x86_64"),
                state=(item.get("State") or {}).get("Name", "running"),
                zone=(item.get("Placement") or {}).get("AvailabilityZone", ""),
                zone_id=(item.get("Placement") or {}).get("AvailabilityZoneId"),
                subnet=subnet,
                firewall_policies=policies,
                disks=disk_ids_by_instance.get(native, []),
                image=item.get("ImageId", ""),
                platform=_platform(item, image),
                platform_details=image.get("PlatformDetails"),
                external_ip=_external_ip(native, item, address_by_instance),
                private_ip=item.get("PrivateIpAddress"),
                user_data=_user_data(native, attributes, ctx),
                key_pair=item.get("KeyName"),
                key_material=key_pairs.get(item.get("KeyName", ""), {}).get("PublicKey"),
                iam_profile=(item.get("IamInstanceProfile") or {}).get("Arn"),
                tenancy=(item.get("Placement") or {}).get("Tenancy", "default"),
                placement_group=(item.get("Placement") or {}).get("GroupName"),
                spot=item.get("InstanceLifecycle") == "spot",
                termination_protection=bool(
                    (attributes.get(native) or {}).get("disableApiTermination")
                ),
                monitoring=(item.get("Monitoring") or {}).get("State") == "enabled",
                imds=_imds(item),
                eks_cluster=eks_cluster,
            )
        )
    return objects


def _scan_gap(code: str, message: str, subject: str) -> Finding:
    return Finding(level="scan_gap", code=code, message=message, subject=subject)


def _size_for(
    instance_type: str,
    sizes: dict[str, tuple[int, int]],
    native: str,
    ctx: NormalizeContext,
) -> tuple[int | None, int | None]:
    if instance_type in sizes:
        return sizes[instance_type]
    if instance_type in FALLBACK_INSTANCE_TYPES:
        return FALLBACK_INSTANCE_TYPES[instance_type]
    ctx.findings.append(
        _scan_gap(
            "SCAN-VM-TYPE-UNKNOWN",
            f"{native} is a {instance_type!r}, whose vCPU and memory this scan could not "
            "determine; machine-type mapping will need a manual decision.",
            native,
        )
    )
    return None, None


def _platform(item: dict[str, Any], image: dict[str, Any]) -> str:
    reported = (item.get("Platform") or image.get("Platform") or "").lower()
    details = (image.get("PlatformDetails") or "").lower()
    return _WINDOWS if _WINDOWS in reported or _WINDOWS in details else "linux"


def _external_ip(native: str, item: dict[str, Any], address_by_instance: dict[str, str]) -> str:
    if native in address_by_instance:
        return address_by_instance[native]
    return "ephemeral" if item.get("PublicIpAddress") else "none"


def _user_data(native: str, attributes: dict[str, Any], ctx: NormalizeContext) -> str | None:
    raw = (attributes.get(native) or {}).get("userData")
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        ctx.info(
            "VM-USER-DATA-BINARY",
            f"{native} has user data that is not UTF-8 text; it is reported but not carried.",
            subject=native,
        )
        return None


def _imds(item: dict[str, Any]) -> ImdsOptions | None:
    options = item.get("MetadataOptions")
    if not options or not options.get("HttpTokens"):
        return None
    return ImdsOptions(
        http_tokens=options["HttpTokens"],
        hop_limit=options.get("HttpPutResponseHopLimit", 1),
    )


def _templates(
    templates_raw: dict[str, Any],
    groups_raw: list[dict[str, Any]],
    ctx: NormalizeContext,
    *,
    policy_ids: dict[str, str],
    images: dict[str, dict[str, Any]],
    sizes: dict[str, tuple[int, int]],
    key_pairs: dict[str, dict[str, Any]],
) -> tuple[list[InstanceTemplate], list[Disk], dict[tuple[str, str], str]]:
    """Only versions an ASG references become objects (spec 4 §4.3)."""
    referenced: set[tuple[str, str]] = set()
    for group in groups_raw:
        spec = group.get("LaunchTemplate") or {}
        template_id = spec.get("LaunchTemplateId")
        if template_id:
            referenced.add((template_id, str(spec.get("Version") or "")))

    wanted: list[tuple[str | None, str]] = []
    for template_id, version in sorted(referenced):
        entry = templates_raw.get(template_id) or {}
        name = (entry.get("template") or {}).get("LaunchTemplateName")
        wanted.append((name, f"{template_id}:{version}"))
    ids = allocate_ids("instance_template", wanted)

    objects: list[InstanceTemplate] = []
    disks: list[Disk] = []
    by_reference: dict[tuple[str, str], str] = {}
    for template_id, entry in sorted(templates_raw.items()):
        template = entry.get("template") or {}
        default_version = str(template.get("DefaultVersionNumber") or "") or None
        for version in entry.get("versions") or []:
            number = str(version.get("VersionNumber"))
            if (template_id, number) not in referenced:
                ctx.info(
                    "VM-LT-VERSION-UNUSED",
                    f"launch template {template_id} version {number} is referenced by no Auto "
                    "Scaling group; it is recorded but not migrated.",
                    subject=f"{template_id}:{number}",
                )
                continue
            native = f"{template_id}:{number}"
            object_id = ids[native]
            data = version.get("LaunchTemplateData") or {}
            image = images.get(data.get("ImageId", ""), {})
            vcpus, memory = sizes.get(data.get("InstanceType", ""), (None, None))
            template_disks = _template_disks(native, data, image, ctx)
            disks.extend(template_disks)
            profile = data.get("IamInstanceProfile") or {}
            objects.append(
                InstanceTemplate(
                    id=object_id,
                    name=template.get("LaunchTemplateName") or native,
                    source=_source(ctx, "launch-template", native),
                    instance_type=data.get("InstanceType", ""),
                    vcpus=vcpus,
                    memory_mib=memory,
                    architecture=data.get("Architecture", "x86_64"),
                    image=data.get("ImageId", ""),
                    platform=_platform(data, image),
                    platform_details=image.get("PlatformDetails"),
                    firewall_policies=[
                        policy_ids[group]
                        for group in data.get("SecurityGroupIds") or []
                        if group in policy_ids
                    ],
                    disks=[disk.id for disk in template_disks],
                    user_data=_template_user_data(data),
                    key_pair=data.get("KeyName"),
                    key_material=key_pairs.get(data.get("KeyName", ""), {}).get("PublicKey"),
                    iam_profile=profile.get("Arn") or profile.get("Name"),
                    spot=(data.get("InstanceMarketOptions") or {}).get("MarketType") == "spot",
                    imds=_imds(data),
                    version=number,
                    default_version=default_version,
                )
            )
            by_reference[(template_id, number)] = object_id
    return objects, disks, by_reference


def _template_disks(
    native: str, data: dict[str, Any], image: dict[str, Any], ctx: NormalizeContext
) -> list[Disk]:
    """A template's EBS mappings, boot disk first. The boot disk comes from the image's root
    mapping when the template does not override it, so every template has one.

    Template disks have `zone = ""`: they belong to no zone until a group places an instance.
    """
    root = image.get("RootDeviceName")
    image_root = next(
        (
            m.get("Ebs") or {}
            for m in image.get("BlockDeviceMappings") or []
            if m.get("DeviceName") == root
        ),
        {},
    )
    mappings: dict[str, dict[str, Any]] = {}
    for mapping in data.get("BlockDeviceMappings") or []:
        device = mapping.get("DeviceName")
        if not device:
            continue
        if "Ebs" not in mapping:
            ctx.info(
                "VM-LT-INSTANCE-STORE",
                f"launch template {native} maps instance-store device {device}; it is recorded "
                "here and not migrated.",
                subject=native,
            )
            continue
        mappings[device] = mapping["Ebs"] or {}
    boot_device = root or "/dev/xvda"
    boot = {**image_root, **mappings.pop(boot_device, {})}

    ordered = [(boot_device, boot, True), *((d, e, False) for d, e in sorted(mappings.items()))]
    ids = allocate_ids("disk", [(None, f"{native}{device}") for device, _, _ in ordered])
    disks = []
    for device, ebs, is_boot in ordered:
        volume_type = ebs.get("VolumeType") or "gp2"
        disks.append(
            Disk(
                id=ids[f"{native}{device}"],
                name=f"{native}{device}",
                source=_source(ctx, "launch-template-disk", f"{native}{device}"),
                size_gib=ebs.get("VolumeSize"),
                type=volume_type,
                iops=ebs.get("Iops") if volume_type in {"gp3", "io1", "io2"} else None,
                throughput_mibps=ebs.get("Throughput") if volume_type == "gp3" else None,
                encrypted=bool(ebs.get("Encrypted")),
                kms_key=ebs.get("KmsKeyId"),
                zone="",
                boot=is_boot,
            )
        )
    return disks


def _template_user_data(data: dict[str, Any]) -> str | None:
    raw = data.get("UserData")
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _groups(
    groups_raw: list[dict[str, Any]],
    policies_raw: list[dict[str, Any]],
    hooks_raw: dict[str, list[dict[str, Any]]],
    ctx: NormalizeContext,
    *,
    subnet_ids: dict[str, str],
    template_ids: dict[tuple[str, str], str],
) -> list[InstanceGroup]:
    ids = allocate_ids(
        "instance_group",
        [(name_tag(item), item["AutoScalingGroupName"]) for item in groups_raw],
    )
    by_group: dict[str, list[ScalingPolicy]] = {}
    for policy in policies_raw:
        by_group.setdefault(policy["AutoScalingGroupName"], []).append(_scaling_policy(policy))

    objects: list[InstanceGroup] = []
    for item in groups_raw:
        native = item["AutoScalingGroupName"]
        spec = item.get("LaunchTemplate") or {}
        key = (spec.get("LaunchTemplateId"), str(spec.get("Version") or ""))
        template = template_ids.get(key)
        if template is None:
            ctx.findings.append(
                _scan_gap(
                    "SCAN-ASG-TEMPLATE-UNRESOLVED",
                    f"Auto Scaling group {native} references launch template {key[0]!r} version "
                    f"{key[1]!r}, which is not in this scan; the group is not migrated.",
                    native,
                )
            )
            continue
        subnets = [
            subnet_ids[part]
            for part in (item.get("VPCZoneIdentifier") or "").split(",")
            if part and part in subnet_ids
        ]
        objects.append(
            InstanceGroup(
                id=ids[native],
                name=native,
                labels=clean_labels({tag["Key"]: tag["Value"] for tag in item.get("Tags") or []}),
                source=_source(ctx, "autoScalingGroup", native),
                template=template,
                min_size=item.get("MinSize", 0),
                max_size=item.get("MaxSize", 0),
                desired_capacity=item.get("DesiredCapacity", 0),
                zones=sorted(item.get("AvailabilityZones") or []),
                subnets=subnets,
                health_check_type=item.get("HealthCheckType", "EC2"),
                health_check_grace=item.get("HealthCheckGracePeriod"),
                target_groups=sorted(item.get("TargetGroupARNs") or []),
                scaling_policies=sorted(by_group.get(native, []), key=lambda p: p.name),
                mixed_instances=bool(item.get("MixedInstancesPolicy")),
                lifecycle_hooks=sorted(
                    hook["LifecycleHookName"] for hook in hooks_raw.get(native, [])
                ),
            )
        )
    return objects


def _scaling_policy(policy: dict[str, Any]) -> ScalingPolicy:
    tracking = policy.get("TargetTrackingConfiguration") or {}
    predefined = tracking.get("PredefinedMetricSpecification") or {}
    return ScalingPolicy(
        name=policy["PolicyName"],
        policy_type=policy.get("PolicyType", ""),
        metric=predefined.get("PredefinedMetricType"),
        target_value=tracking.get("TargetValue"),
    )
