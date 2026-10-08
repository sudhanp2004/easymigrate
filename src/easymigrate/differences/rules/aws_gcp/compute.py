"""AWS→GCP compute difference rules (spec 4 §5).

Rules decide the GCP shape and record it in `target_hints`; `plugins/compute/generate_gcp.py`
renders those hints and never re-derives a choice. GCP facts live in `gce_facts.py` beside this
module, because `easymigrate.differences` may import only `easymigrate.ir`.
"""

from __future__ import annotations

import re
from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.gce_facts import (
    DISK_TYPE,
    GP3_BASELINE_IOPS,
    GP3_BASELINE_THROUGHPUT_MIBPS,
    IMAGE_FAMILIES,
    image_for,
    local_ssd_units,
    min_disk_for_image,
    nearest_predefined,
)
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.ir.compute import (
    ComputeInstance,
    Disk,
    ExternalAddress,
    InstanceGroup,
    InstanceTemplate,
)

# Above this, the memory a machine gains in translation is large enough to matter for cost
# and for sizing assumptions, so VM-MACHINE-TYPE raises its risk (spec 4 §5).
_MEMORY_DELTA_MEDIUM = 0.25


def _set_hint(obj: IntentObject, key: str, value: Any) -> IntentObject:
    return obj.model_copy(update={"target_hints": {**obj.target_hints, key: value}})


def _replace_object(doc: IRDocument, updated: IntentObject) -> IRDocument:
    objects = [updated if obj.id == updated.id else obj for obj in doc.objects]
    return doc.model_copy(update={"objects": objects})


def _shapes(ir: IRDocument) -> list[ComputeInstance | InstanceTemplate]:
    """Instances and templates, which share every field these rules read."""
    shapes: list[ComputeInstance | InstanceTemplate] = [
        *ir.of_kind(ComputeInstance),
        *ir.of_kind(InstanceTemplate),
    ]
    return sorted(shapes, key=lambda obj: obj.id)


class VmBootImage(DifferenceRule):
    id = "VM-BOOT-IMAGE"
    family = "compute"
    title = "An AMI cannot be moved to GCP, and a disk's contents do not migrate"
    impact = (
        "EasyMigrate does not import, convert or rebuild AMIs, and does not copy disk "
        "contents. A migrated instance boots a current stock image of the nearest "
        "distribution: the machine runs, on the right network, at the right size, with "
        "nothing that was installed or stored on the source disk."
    )
    strategies = (
        Strategy(
            id="stock_image",
            title="Boot a stock GCP image",
            effect=(
                "the instance boots a current stock image chosen from the source platform; "
                "everything that was on the source disk is absent"
            ),
            risk="high",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="nothing is generated for this instance",
            risk="low",
            manual=True,
        ),
    )
    default = "stock_image"
    param_docs = {
        "image_project": "GCP image project, e.g. debian-cloud",
        "image_family": "GCP image family, e.g. debian-12",
    }

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for obj in _shapes(ir):
            project, family = image_for(obj.platform, obj.platform_details)
            diffs.append(
                self.difference(
                    [obj.id],
                    ami=obj.image,
                    platform=obj.platform,
                    platform_details=obj.platform_details,
                    image_project=project,
                    image_family=family,
                    # Stated in the report rather than implied: there is no Amazon Linux on GCP.
                    no_equivalent=family == IMAGE_FAMILIES["amazon"][1] and obj.platform == "linux",
                )
            )
        return diffs

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if strategy_id != "stock_image":
            return {}
        return {
            "image_project": diff.context["image_project"],
            "image_family": diff.context["image_family"],
        }

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id != "stock_image":
            return
        for key in ("image_project", "image_family"):
            value = params.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        obj = doc.get(obj_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, obj.model_copy(update={"excluded": f"{self.id}: handled manually"})
            )
        if strategy_id != "stock_image":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        params = params or self.default_params(diff, strategy_id)
        return _replace_object(
            doc,
            _set_hint(
                obj,
                "boot_image",
                {
                    "project": params["image_project"],
                    "family": params["image_family"],
                },
            ),
        )


class VmMachineType(DifferenceRule):
    id = "VM-MACHINE-TYPE"
    family = "compute"
    title = "EC2 instance types and GCP machine types do not line up"
    impact = (
        "GCP predefined machine types come in fixed memory-per-vCPU ratios, and AWS "
        "families do not share them. EasyMigrate never reduces memory, so a source whose "
        "ratio GCP has no type for gains memory, which costs more."
    )
    strategies = (
        Strategy(
            id="nearest_predefined",
            title="Nearest predefined machine type",
            effect=(
                "same vCPU count, the smallest predefined type whose memory is at least the "
                "source's; the report names the exact delta"
            ),
            risk="low",
        ),
        Strategy(
            id="custom_machine_type",
            title="Custom machine type",
            effect=(
                "exact vCPU and memory via a custom type, at a higher per-unit price and "
                "without committed-use discounts"
            ),
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="nothing is generated for this instance",
            risk="low",
            manual=True,
        ),
    )
    default = "nearest_predefined"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for obj in _shapes(ir):
            chosen = nearest_predefined(obj.vcpus, obj.memory_mib, obj.instance_type)
            delta = None
            if chosen is not None and obj.memory_mib:
                delta = chosen.memory_mib - obj.memory_mib
            diffs.append(
                self.difference(
                    [obj.id],
                    instance_type=obj.instance_type,
                    vcpus=obj.vcpus,
                    memory_mib=obj.memory_mib,
                    machine_type=chosen.name if chosen else None,
                    machine_type_memory_mib=chosen.memory_mib if chosen else None,
                    memory_delta_mib=delta,
                )
            )
        return diffs

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("machine_type") is None:
            # Sizing is unknown, so neither automatic strategy can be computed (spec 4 §5).
            return (self.strategy("manual"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return "manual" if diff.context.get("machine_type") is None else self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if strategy_id != "nearest_predefined":
            return self.strategy(strategy_id).risk
        source = diff.context.get("memory_mib")
        delta = diff.context.get("memory_delta_mib")
        if not source or delta is None:
            return "medium"
        return "medium" if delta / source >= _MEMORY_DELTA_MEDIUM else "low"

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        obj = doc.get(obj_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, obj.model_copy(update={"excluded": f"{self.id}: handled manually"})
            )
        if strategy_id == "custom_machine_type":
            if obj.vcpus is None or obj.memory_mib is None:
                raise ValueError("custom_machine_type needs vCPU and memory")
            name = f"custom-{obj.vcpus}-{obj.memory_mib}"
            return _replace_object(doc, _set_hint(obj, "machine_type", name))
        if strategy_id != "nearest_predefined":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        machine_type = diff.context.get("machine_type")
        if machine_type is None:
            raise ValueError("nearest_predefined has no machine type for this instance")
        updated = _set_hint(obj, "machine_type", machine_type)
        updated = _set_hint(updated, "memory_delta_mib", diff.context.get("memory_delta_mib"))
        return _replace_object(doc, updated)


class VmZoneMapping(DifferenceRule):
    id = "VM-ZONE-MAPPING"
    family = "compute"
    title = "AWS availability-zone names carry no meaning a GCP zone preserves"
    impact = (
        "An AWS AZ letter is a per-account alias over a stable AZ id, so `ap-south-1a` is a "
        "different physical zone for different accounts. Zones are therefore assigned by "
        "position: instances that shared an AZ stay together, and instances that were spread "
        "stay spread, but no letter is preserved."
    )
    strategies = (
        Strategy(
            id="positional",
            title="Assign zones by position",
            effect=(
                "distinct source zones are sorted and mapped to the destination region's "
                "zones in order; co-location and spread are preserved, letters are not"
            ),
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="nothing is generated for this instance",
            risk="low",
            manual=True,
        ),
    )
    default = "positional"

    @staticmethod
    def _letters(ir: IRDocument) -> dict[str, str]:
        """Source zone → destination zone letter, assigned positionally.

        Sorted by the stable zone id, the only account-independent identifier AWS offers;
        instances carry theirs and groups record theirs from `describe_availability_zones`. A
        zone with no known id sorts last by name so the mapping stays deterministic.

        Only objects that will be generated take a letter: an instance a decision excluded, or
        an EKS member, must not push the zones of the ones that remain (live run, 2026-09-22).
        `apply` therefore recomputes this from the document as resolution has left it, and a
        letter is an index into the destination region's zones, which generation resolves.
        """
        zone_ids: dict[str, str] = {}
        zones: set[str] = set()
        for instance in ir.of_kind(ComputeInstance):
            # An instance VM-ZONE-MAPPING itself excluded still holds its position: letters
            # already handed out in this pass must not shift (review, 2026-10-08).
            excluded_here = (instance.excluded or "").startswith("VM-ZONE-MAPPING")
            if (instance.excluded is not None and not excluded_here) or instance.eks_cluster:
                continue
            if not instance.zone:
                continue
            zones.add(instance.zone)
            if instance.zone_id:
                zone_ids[instance.zone] = instance.zone_id
        for group in ir.of_kind(InstanceGroup):
            if group.excluded is not None:
                continue
            zones.update(group.zones)
            zone_ids.update(group.zone_ids)
        keyed = {(zone_ids.get(zone) or "~", zone) for zone in zones}
        letters: dict[str, str] = {}
        for index, (_, zone) in enumerate(sorted(keyed)):
            letters[zone] = chr(ord("a") + index)
        return letters

    def detect(self, ir: IRDocument) -> list[Difference]:
        letters = self._letters(ir)
        diffs = []
        for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id):
            # EKS members are never generated here, so they take no zone (spec 4 §1).
            if not instance.zone or instance.eks_cluster:
                continue
            diffs.append(
                self.difference(
                    [instance.id],
                    source_zone=instance.zone,
                    source_zone_id=instance.zone_id,
                    zone_letter=letters[instance.zone],
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        obj = doc.get(obj_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, obj.model_copy(update={"excluded": f"{self.id}: handled manually"})
            )
        if strategy_id != "positional":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        # Recomputed rather than read from the detected context: an instance an earlier
        # decision excluded no longer takes a zone.
        letter = self._letters(doc).get(obj.zone, diff.context["zone_letter"])
        return _replace_object(doc, _set_hint(obj, "zone_letter", letter))


class VmPublicIp(DifferenceRule):
    id = "VM-PUBLIC-IP"
    family = "compute"
    title = "A reachable-from-the-internet instance stays reachable unless you say otherwise"
    impact = (
        "An instance with a public address is reachable from the internet, governed only by "
        "its firewall rules. Recreating that exposure without asking would change the "
        "security posture of a machine the user may not have reviewed, so the default is to "
        "give the instance no external address."
    )
    strategies = (
        Strategy(
            id="private_only",
            title="No external IP",
            effect=(
                "the instance has no external address; every client must reach it from inside "
                "the VPC or through a bastion. This matches nothing in the source"
            ),
            risk="medium",
        ),
        Strategy(
            id="ephemeral_external",
            title="Keep an ephemeral external IP",
            effect=(
                "the instance stays reachable from the internet on a new, non-reserved address, "
                "governed only by the migrated firewall rules"
            ),
            risk="high",
        ),
        Strategy(
            id="static_external",
            title="Reserve a static external IP",
            effect=(
                "as above, with a reserved address, so the instance keeps a stable public "
                "endpoint; it is billed whether or not it is in use"
            ),
            risk="high",
        ),
    )
    default = "private_only"

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        # A reserved address needs an Elastic IP to stand for; an ephemeral source has none.
        if diff.context.get("reserved"):
            return self.strategies
        return tuple(s for s in self.strategies if s.id != "static_external")

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id):
            if instance.external_ip == "none":
                continue
            diffs.append(
                self.difference(
                    [instance.id],
                    source=instance.external_ip,
                    reserved=instance.external_ip.startswith("external_address:"),
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        instance = doc.get(obj_id)
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {obj_id}")
        external = {
            "private_only": "none",
            "ephemeral_external": "ephemeral",
            "static_external": "static",
        }.get(strategy_id)
        if external is None:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(instance, "external", external))


class VmStopped(DifferenceRule):
    id = "VM-STOPPED"
    family = "compute"
    title = "A stopped instance should not start billing on arrival"
    impact = (
        "An instance the customer deliberately stopped costs nothing in AWS. Creating it "
        "running in GCP would start charging for it without anyone asking."
    )
    strategies = (
        Strategy(
            id="create_stopped",
            title="Create it stopped",
            effect="the instance is created and immediately left in TERMINATED state",
            risk="none",
        ),
        Strategy(
            id="create_running",
            title="Create it running",
            effect="the instance is created running, and billing starts at apply time",
            risk="medium",
        ),
    )
    default = "create_stopped"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id])
            for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id)
            if instance.state == "stopped"
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id == "create_running":
            return doc
        if strategy_id != "create_stopped":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(doc.get(obj_id), "desired_status", "TERMINATED"))


def _is_provisioned(disk) -> bool:
    """Whether this volume actually bought performance beyond what its type includes free.

    gp3 ships 3000 IOPS and 125 MiB/s at no cost, and AWS reports those figures on every gp3
    volume. Reading them as provisioned made VM-DISK-TYPE fire for every gp3 disk and tell the
    user performance was being lost when none had been bought (live run, 2026-09-19).
    """
    if disk.type != "gp3":
        return disk.iops is not None or disk.throughput_mibps is not None
    return (disk.iops or 0) > GP3_BASELINE_IOPS or (
        disk.throughput_mibps or 0
    ) > GP3_BASELINE_THROUGHPUT_MIBPS


def _disks(ir: IRDocument):
    return sorted(ir.of_kind(Disk), key=lambda d: d.id)


class VmDiskType(DifferenceRule):
    id = "VM-DISK-TYPE"
    family = "compute"
    title = "Persistent disk performance scales with size; EBS provisions it directly"
    impact = (
        "An EBS volume can be given IOPS and throughput independently of its size. A "
        "persistent disk cannot: its performance follows its size and type, so provisioned "
        "figures do not carry and a small disk may be slower than the source."
    )
    strategies = (
        Strategy(
            id="nearest_pd",
            title="Nearest persistent disk type",
            effect=(
                "io1/io2 become pd-ssd, st1/sc1 become pd-standard, gp2/gp3 become "
                "pd-balanced; provisioned IOPS and throughput are lost"
            ),
            risk="medium",
        ),
        Strategy(
            id="hyperdisk",
            title="Hyperdisk",
            effect=(
                "provisioned IOPS and throughput are preserved, at a different price and with "
                "machine-type and zone availability limits"
            ),
            risk="medium",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="nothing is generated for this disk",
            risk="low",
            manual=True,
        ),
    )
    default = "nearest_pd"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for disk in _disks(ir):
            if disk.instance_store or disk.type is None:
                continue
            if disk.type not in {"io1", "io2", "st1", "sc1"} and not _is_provisioned(disk):
                continue
            diffs.append(
                self.difference(
                    [disk.id],
                    source_type=disk.type,
                    iops=disk.iops,
                    throughput_mibps=disk.throughput_mibps,
                    disk_type=DISK_TYPE[disk.type],
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        disk = doc.get(obj_id)
        if strategy_id == "manual":
            return _replace_object(
                doc, disk.model_copy(update={"excluded": f"{self.id}: handled manually"})
            )
        if strategy_id == "hyperdisk":
            return _replace_object(doc, _set_hint(disk, "disk_type", "hyperdisk-balanced"))
        if strategy_id != "nearest_pd":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(disk, "disk_type", diff.context["disk_type"]))


class VmDiskEmpty(DifferenceRule):
    id = "VM-DISK-EMPTY"
    family = "compute"
    title = "A data volume is recreated empty"
    impact = (
        "EasyMigrate does not copy disk contents. A non-boot volume is recreated at the same "
        "size and type with nothing on it. A mounted empty disk is a subtler failure than a "
        "missing one, which is why this carries the same risk as the boot image."
    )
    strategies = (
        Strategy(
            id="create_empty",
            title="Create an empty disk",
            effect=(
                "a persistent disk of matching size and type is created and attached, "
                "containing nothing from the source volume"
            ),
            risk="high",
        ),
        Strategy(
            id="omit",
            title="Do not create it",
            effect="the disk is reported and not created; the instance comes up without it",
            risk="medium",
        ),
    )
    default = "create_empty"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([disk.id], size_gib=disk.size_gib, source_type=disk.type)
            for disk in _disks(ir)
            if not disk.boot and not disk.instance_store
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        disk = doc.get(obj_id)
        if strategy_id == "omit":
            return _replace_object(
                doc, disk.model_copy(update={"excluded": f"{self.id}: not created"})
            )
        if strategy_id != "create_empty":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(disk, "create_empty", True))


class VmDiskEncryption(DifferenceRule):
    id = "VM-DISK-ENCRYPTION"
    family = "compute"
    title = "A customer-managed KMS key does not carry to GCP"
    impact = (
        "An EBS volume encrypted with a customer-managed AWS KMS key cannot use that key in "
        "GCP. The disk stays encrypted at rest either way; what changes is who holds the key."
    )
    strategies = (
        Strategy(
            id="google_managed",
            title="Google-managed encryption",
            effect="the disk is encrypted at rest with Google-managed keys",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "create a Cloud KMS key, grant the Compute Engine service agent, and set "
                "disk_encryption_key yourself"
            ),
            risk="low",
            manual=True,
        ),
    )
    default = "google_managed"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([disk.id], kms_key=disk.kms_key)
            for disk in _disks(ir)
            if disk.encrypted and disk.kms_key
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        if strategy_id not in {"google_managed", "manual"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        # Neither strategy changes generation: a disk with no encryption hint is created with
        # Google-managed keys, which is what both outcomes look like in Terraform.
        return ir.model_copy(deep=True)


class VmInstanceStore(DifferenceRule):
    id = "VM-INSTANCE-STORE"
    family = "compute"
    title = "Instance-store volumes become fixed-size local SSDs"
    impact = (
        "Both clouds lose the data when the machine stops. GCP local SSD comes in fixed "
        "375 GiB devices and requires a machine type that supports it, so the size rarely "
        "matches the source exactly."
    )
    strategies = (
        Strategy(
            id="local_ssd",
            title="Attach local SSD",
            effect=(
                "whole 375 GiB local SSD devices are attached, rounded up; the machine type "
                "must support local SSD"
            ),
            risk="medium",
        ),
        Strategy(
            id="omit",
            title="Do not attach it",
            effect="the ephemeral volume is reported and not recreated",
            risk="low",
        ),
    )
    default = "local_ssd"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([disk.id], size_gib=disk.size_gib, units=local_ssd_units(disk.size_gib))
            for disk in _disks(ir)
            if disk.instance_store
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        disk = doc.get(obj_id)
        if strategy_id == "omit":
            return _replace_object(
                doc, disk.model_copy(update={"excluded": f"{self.id}: not attached"})
            )
        if strategy_id != "local_ssd":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(disk, "local_ssd_count", diff.context["units"]))


class VmBootDiskMinimum(DifferenceRule):
    id = "VM-BOOT-DISK-MINIMUM"
    family = "compute"
    title = "A GCP stock image needs a bigger boot disk than the source root volume"
    impact = (
        "Every Compute Engine image carries a minimum boot-disk size and the API refuses "
        "anything smaller. An AWS AMI of the same distribution routinely fits in less, so a "
        "root volume that was legal in AWS can be too small for the image replacing it."
    )
    strategies = (
        Strategy(
            id="grow_to_minimum",
            title="Grow the boot disk to the image's minimum",
            effect="the boot disk is created at the image's minimum size instead of the source's",
            risk="none",
        ),
    )
    default = "grow_to_minimum"

    @staticmethod
    def _boot_disk(obj, disks: dict[str, object]):
        for disk_id in obj.disks:
            disk = disks.get(disk_id)
            if disk is not None and disk.boot:
                return disk
        return None

    def detect(self, ir: IRDocument) -> list[Difference]:
        disks = {disk.id: disk for disk in ir.of_kind(Disk)}
        diffs = []
        for obj in _shapes(ir):
            # Detection runs against the unresolved IR, before any rule's `apply`, so the
            # `boot_image` hint does not exist yet. Recompute VM-BOOT-IMAGE's default choice
            # here; `apply` re-reads the hint, so an image the user overrode still wins.
            project, family = image_for(obj.platform, obj.platform_details)
            boot = self._boot_disk(obj, disks)
            if boot is None or not boot.size_gib:
                continue
            minimum = min_disk_for_image(project, family)
            if boot.size_gib >= minimum:
                continue
            diffs.append(
                self.difference(
                    [obj.id],
                    source_gib=boot.size_gib,
                    minimum_gib=minimum,
                    image_family=family,
                )
            )
        return diffs

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc = ir.model_copy(deep=True)
        obj_id = diff.subjects[0]
        if obj_id not in {o.id for o in doc.objects}:
            return doc
        if strategy_id != "grow_to_minimum":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        obj = doc.get(obj_id)
        # VM-BOOT-IMAGE runs first, so by now the hint holds the image actually chosen —
        # which may not be the default this rule detected against.
        chosen = obj.target_hints.get("boot_image")
        minimum = (
            min_disk_for_image(chosen["project"], chosen["family"])
            if chosen
            else diff.context["minimum_gib"]
        )
        if minimum <= diff.context["source_gib"]:
            return doc
        return _replace_object(doc, _set_hint(obj, "boot_disk_size_gib", minimum))


# --- Access and identity (spec 4 plan 4) -----------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PUBLIC_KEY_RE = re.compile(
    r"^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-\S+) [A-Za-z0-9+/=]+( .*)?$"
)


def _merge_metadata(obj: IntentObject, entries: dict[str, str]) -> IntentObject:
    """Metadata is one GCP attribute written by several rules, so each merges its keys in."""
    return _set_hint(obj, "metadata", {**obj.target_hints.get("metadata", {}), **entries})


def _subject(ir: IRDocument, diff: Difference) -> tuple[IRDocument, IntentObject | None]:
    doc = ir.model_copy(deep=True)
    obj_id = diff.subjects[0]
    if obj_id not in {o.id for o in doc.objects}:
        return doc, None
    return doc, doc.get(obj_id)


def _default_username(platform_details: str | None) -> str:
    """The login the AMI's distribution shipped, so the user keeps the name they already use."""
    details = (platform_details or "").lower()
    if "ubuntu" in details:
        return "ubuntu"
    if "debian" in details:
        return "admin"
    return "ec2-user"


def _check_username(params: dict[str, Any]) -> None:
    username = params.get("username")
    if not isinstance(username, str) or not _USERNAME_RE.match(username):
        raise ValueError("username must be a lowercase POSIX login name, e.g. ec2-user")


class VmSshKeys(DifferenceRule):
    id = "VM-SSH-KEYS"
    family = "compute"
    title = "An EC2 key pair becomes a GCP SSH key, or GCP's own access model"
    impact = (
        "AWS injects one key pair into an instance at launch. GCP reads SSH keys from instance "
        "metadata, or delegates access to IAM through OS Login. EasyMigrate never generates a "
        "key pair; it carries the public key AWS still holds, or lets you choose another model."
    )
    strategies = (
        Strategy(
            id="import_public_key",
            title="Import the key pair's public key",
            effect="the public key AWS holds becomes an ssh-keys metadata entry for the same login",
            risk="low",
        ),
        Strategy(
            id="os_login",
            title="Use OS Login",
            effect=(
                "the key pair is ignored and GCP IAM governs who may log in; this changes the "
                "access model, so every current user needs an IAM grant"
            ),
            risk="medium",
        ),
        Strategy(
            id="user_supplied_key",
            title="Use a public key you supply",
            effect="the public key you give becomes an ssh-keys metadata entry",
            risk="low",
        ),
        Strategy(
            id="no_ssh_access",
            title="No SSH access",
            effect=(
                "no key is added and project-wide SSH keys are blocked; nobody can log in "
                "over SSH until access is configured"
            ),
            risk="medium",
        ),
    )
    default = "import_public_key"
    param_docs = {
        "username": "login name the key is installed for, e.g. ec2-user",
        "public_key": "an OpenSSH public key, e.g. 'ssh-ed25519 AAAA… you@host'",
    }

    _WINDOWS = Strategy(
        id="no_ssh_access",
        title="No SSH access",
        effect=(
            "a Windows key pair only decrypted the administrator password in AWS; in GCP, set "
            "one with `gcloud compute reset-windows-password` after the instance starts"
        ),
        risk="low",
    )

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [obj.id],
                key_pair=obj.key_pair,
                has_public_key=bool(obj.key_material),
                platform=obj.platform,
                username=_default_username(obj.platform_details),
            )
            for obj in _shapes(ir)
            if obj.key_pair
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("platform") == "windows":
            return (self._WINDOWS,)
        if not diff.context.get("has_public_key"):
            return tuple(s for s in self.strategies if s.id != "import_public_key")
        return self.strategies

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        if diff.context.get("platform") == "windows":
            return self._WINDOWS.risk
        return self.strategy(strategy_id).risk

    def default_strategy(self, diff: Difference) -> str:
        if diff.context.get("platform") == "windows":
            return "no_ssh_access"
        return self.default if diff.context.get("has_public_key") else "os_login"

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if strategy_id == "import_public_key":
            return {"username": diff.context["username"]}
        if strategy_id == "user_supplied_key":
            # Declared empty so the key set survives `merge_decisions`; validation refuses it
            # until the user fills it in.
            return {"username": diff.context["username"], "public_key": ""}
        return {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id not in {"import_public_key", "user_supplied_key"}:
            return
        _check_username(params)
        if strategy_id == "user_supplied_key":
            key = params.get("public_key")
            if not isinstance(key, str) or not _PUBLIC_KEY_RE.match(key.strip()):
                raise ValueError("public_key must be an OpenSSH public key line")

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, obj = _subject(ir, diff)
        if obj is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {obj.id}")
        params = {**self.default_params(diff, strategy_id), **(params or {})}
        if strategy_id == "os_login":
            return _replace_object(doc, _merge_metadata(obj, {"enable-oslogin": "TRUE"}))
        if strategy_id == "no_ssh_access":
            return _replace_object(doc, _merge_metadata(obj, {"block-project-ssh-keys": "TRUE"}))
        self.validate_params(diff, strategy_id, params)
        if strategy_id == "import_public_key":
            if not obj.key_material:
                raise ValueError(f"{obj.id} has no public key material to import")
            key = obj.key_material.strip()
        else:
            key = params["public_key"].strip()
        return _replace_object(
            doc, _merge_metadata(obj, {"ssh-keys": f"{params['username']}:{key}"})
        )


_WINDOWS_WRAPPERS = (
    ("<powershell>", "</powershell>", "windows-startup-script-ps1"),
    ("<script>", "</script>", "windows-startup-script-cmd"),
)


def _windows_script(user_data: str) -> tuple[str, str]:
    """AWS wraps Windows user data in <powershell> or <script>; GCP keys it by language."""
    stripped = user_data.strip()
    for opening, closing, key in _WINDOWS_WRAPPERS:
        start = stripped.find(opening)
        end = stripped.find(closing)
        if start != -1 and end > start:
            return key, stripped[start + len(opening) : end].strip()
    return "windows-startup-script-ps1", stripped


class VmUserData(DifferenceRule):
    id = "VM-USER-DATA"
    family = "compute"
    title = "EC2 user data becomes a GCP startup script, which runs on every boot"
    impact = (
        "AWS runs user data once, on first boot, through cloud-init. GCP's guest agent runs a "
        "startup script on every boot and does not interpret #cloud-config. A script that is "
        "not safe to run twice needs a guard before it migrates."
    )
    strategies = (
        Strategy(
            id="startup_script",
            title="Run it as a startup script",
            effect=(
                "copied verbatim into the startup-script metadata key; it runs on every boot, "
                "not first boot only, and #cloud-config is not interpreted"
            ),
            risk="medium",
        ),
        Strategy(
            id="cloud_init",
            title="Pass it to cloud-init",
            effect=(
                "written to the user-data metadata key, which only images that ship cloud-init "
                "honour; GCP's Debian, Rocky and RHEL images do not"
            ),
            risk="medium",
        ),
        Strategy(
            id="omit",
            title="Do not carry it",
            effect="the script is reported in full and not run on the new instance",
            risk="low",
        ),
    )
    default = "startup_script"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [obj.id],
                platform=obj.platform,
                cloud_config=obj.user_data.lstrip().startswith("#cloud-config"),
                length=len(obj.user_data),
            )
            for obj in _shapes(ir)
            if obj.user_data and obj.user_data.strip()
        ]

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("platform") == "windows":
            return tuple(s for s in self.strategies if s.id != "cloud_init")
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        # Running a #cloud-config document as a shell script fails on its first line.
        if diff.context.get("cloud_config") and diff.context.get("platform") != "windows":
            return "cloud_init"
        return self.default

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, obj = _subject(ir, diff)
        if obj is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {obj.id}")
        if strategy_id == "omit":
            return doc
        if strategy_id == "cloud_init":
            return _replace_object(doc, _merge_metadata(obj, {"user-data": obj.user_data}))
        if obj.platform == "windows":
            key, script = _windows_script(obj.user_data)
        else:
            key, script = "startup-script", obj.user_data
        return _replace_object(doc, _merge_metadata(obj, {key: script}))


class _AcknowledgeOnly:
    """A gap EasyMigrate names but does not bridge. The subject is still generated; the
    `manual` strategy exists so the user must acknowledge the gap before `plan`. A mixin, since
    `DifferenceRule.__init_subclass__` requires a concrete rule id."""

    strategies: tuple[Strategy, ...]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        if strategy_id not in {s.id for s in self.strategies}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return ir.model_copy(deep=True)


class VmIamProfile(_AcknowledgeOnly, DifferenceRule):
    id = "VM-IAM-PROFILE"
    family = "compute"
    title = "An instance profile's IAM role is not translated"
    impact = (
        "An EC2 instance profile gives software on the machine AWS credentials. GCP service "
        "accounts have different semantics, and translating permissions is out of scope, so "
        "whatever the software did with those credentials stops working."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "the instance is created with no service account; the AWS role is named in the "
                "report so you can create and attach a service account with equivalent access"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([obj.id], iam_profile=obj.iam_profile)
            for obj in _shapes(ir)
            if obj.iam_profile
        ]


class VmSpot(DifferenceRule):
    id = "VM-SPOT"
    family = "compute"
    title = "AWS Spot and GCP Spot VMs are preempted differently"
    impact = (
        "Both clouds reclaim spot capacity at short notice, but AWS gives a two-minute "
        "interruption notice through instance metadata while GCP gives thirty seconds through "
        "an ACPI shutdown. Handlers written for one do not fire on the other."
    )
    strategies = (
        Strategy(
            id="spot_vm",
            title="Create a Spot VM",
            effect=(
                "provisioning model SPOT, stopped rather than deleted on preemption, and never "
                "restarted automatically"
            ),
            risk="medium",
        ),
        Strategy(
            id="standard",
            title="Create a standard VM",
            effect="the instance costs more and is never preempted",
            risk="low",
        ),
    )
    default = "spot_vm"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([obj.id]) for obj in _shapes(ir) if obj.spot]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, obj = _subject(ir, diff)
        if obj is None:
            return doc
        if strategy_id == "standard":
            return doc
        if strategy_id != "spot_vm":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(obj, "spot", True))


class VmTenancy(_AcknowledgeOnly, DifferenceRule):
    id = "VM-TENANCY"
    family = "compute"
    title = "Dedicated tenancy is not reproduced"
    impact = (
        "GCP's equivalent is sole-tenant nodes, which need a node template, a node group and "
        "affinity labels. This family does not generate them."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "the instance is created on shared hardware; create a sole-tenant node group "
                "and set node affinity yourself if isolation or licensing requires it"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], tenancy=instance.tenancy)
            for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id)
            if instance.tenancy in {"dedicated", "host"}
        ]


class VmPlacementGroup(_AcknowledgeOnly, DifferenceRule):
    id = "VM-PLACEMENT-GROUP"
    family = "compute"
    title = "A placement group is not reproduced"
    impact = (
        "Cluster, spread and partition placement groups correspond to GCP resource policies "
        "with different guarantees. This family does not generate them."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "the instance is created without placement constraints; attach a GCP placement "
                "policy yourself if latency or failure isolation depended on the group"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id], placement_group=instance.placement_group)
            for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id)
            if instance.placement_group
        ]


class VmImds(_AcknowledgeOnly, DifferenceRule):
    id = "VM-IMDS"
    family = "compute"
    title = "GCP's metadata server has no IMDSv2 token protocol"
    impact = (
        "The source requires IMDSv2 session tokens. GCP's metadata server instead requires a "
        "`Metadata-Flavor: Google` header on every request and has no v1/v2 distinction. "
        "Software that calls the AWS metadata endpoint needs changing regardless."
    )
    strategies = (
        Strategy(
            id="note_only",
            title="Note it",
            effect=(
                "nothing to configure: GCP's header requirement already blocks the "
                "SSRF-style requests IMDSv2 was introduced to stop"
            ),
            risk="none",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([obj.id], hop_limit=obj.imds.hop_limit)
            for obj in _shapes(ir)
            if obj.imds is not None and obj.imds.http_tokens == "required"
        ]


class VmTerminationProtection(DifferenceRule):
    id = "VM-TERMINATION-PROTECTION"
    family = "compute"
    title = "Termination protection becomes deletion protection"
    impact = (
        "GCP's deletion protection refuses a delete exactly as AWS termination protection does."
    )
    strategies = (
        Strategy(
            id="deletion_protection",
            title="Enable deletion protection",
            effect="the instance cannot be deleted until deletion protection is turned off",
            risk="none",
        ),
    )
    default = "deletion_protection"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([instance.id])
            for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id)
            if instance.termination_protection
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, obj = _subject(ir, diff)
        if obj is None:
            return doc
        if strategy_id != "deletion_protection":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(obj, "deletion_protection", True))


class VmElasticIpUnattached(DifferenceRule):
    id = "VM-ELASTIC-IP-UNATTACHED"
    family = "compute"
    title = "An unattached Elastic IP becomes a reserved address, with a different IP"
    impact = (
        "An Elastic IP nobody uses is usually kept for a reason — an allow-list entry or a DNS "
        "record. GCP can reserve an address, but not the same one, so whatever depended on "
        "the old IP needs updating either way."
    )
    strategies = (
        Strategy(
            id="reserve_static",
            title="Reserve a static address",
            effect="a new external address is reserved and billed while unused, as in AWS",
            risk="low",
        ),
        Strategy(
            id="omit",
            title="Do not reserve it",
            effect="the address is reported and not created",
            risk="none",
        ),
    )
    default = "reserve_static"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([address.id], address=address.address)
            for address in sorted(ir.of_kind(ExternalAddress), key=lambda o: o.id)
            if not address.attached
        ]

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, obj = _subject(ir, diff)
        if obj is None:
            return doc
        if strategy_id == "omit":
            return _replace_object(
                doc, obj.model_copy(update={"excluded": f"{self.id}: not reserved"})
            )
        if strategy_id != "reserve_static":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        return _replace_object(doc, _set_hint(obj, "reserve", True))


# --- Scaling (spec 4 plan 5) -----------------------------------------------------------------

_CPU_METRIC = "ASGAverageCPUUtilization"
_DEFAULT_GRACE_SEC = 300


def _groups(ir: IRDocument) -> list[InstanceGroup]:
    return sorted(ir.of_kind(InstanceGroup), key=lambda o: o.id)


class VmAsgShape(DifferenceRule):
    id = "VM-ASG-SHAPE"
    family = "compute"
    title = "An Auto Scaling group becomes a managed instance group"
    impact = (
        "An ASG spanning availability zones maps to a regional managed instance group, which "
        "spreads instances across the mapped GCP zones. A GCP subnet is regional, so one subnet "
        "serves every zone where AWS used one per AZ. Instances are created with no external IP."
    )
    strategies = (
        Strategy(
            id="regional_mig",
            title="Regional managed instance group",
            effect=(
                "instances are distributed across the mapped zones, the closest match to an ASG "
                "spanning AZs; they use one regional subnet and have no external IP"
            ),
            risk="low",
        ),
        Strategy(
            id="zonal_mig",
            title="Zonal managed instance group",
            effect=(
                "every instance runs in a single zone: cheaper, with no cross-zone redundancy; "
                "instances have no external IP"
            ),
            risk="medium",
        ),
    )
    default = "regional_mig"

    def detect(self, ir: IRDocument) -> list[Difference]:
        letters = VmZoneMapping._letters(ir)
        return [
            self.difference(
                [group.id],
                zones=group.zones,
                zone_letters=sorted({letters[zone] for zone in group.zones if zone in letters}),
                subnet=sorted(group.subnets)[0] if group.subnets else None,
                subnets=sorted(group.subnets),
            )
            for group in _groups(ir)
        ]

    def default_strategy(self, diff: Difference) -> str:
        return "zonal_mig" if len(diff.context.get("zones") or []) <= 1 else self.default

    def risk_for(self, diff: Difference, strategy_id: str) -> Risk:
        # With one source AZ a zonal group loses nothing the source had.
        if strategy_id == "zonal_mig" and len(diff.context.get("zones") or []) <= 1:
            return "low"
        return self.strategy(strategy_id).risk

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, group = _subject(ir, diff)
        if group is None:
            return doc
        if strategy_id not in {"regional_mig", "zonal_mig"}:
            raise ValueError(f"unknown strategy {strategy_id!r}")
        if diff.context.get("subnet") is None:
            return _replace_object(
                doc,
                group.model_copy(
                    update={
                        "excluded": f"{self.id}: none of its subnets is in the scan, so there "
                        "is nowhere to place its instances"
                    }
                ),
            )
        template = next((o for o in doc.objects if o.id == group.template), None)
        if template is None or template.excluded is not None:
            reason = template.excluded if template is not None else "missing"
            return _replace_object(
                doc,
                group.model_copy(
                    update={"excluded": f"{self.id}: its template is not migrated ({reason})"}
                ),
            )
        current = VmZoneMapping._letters(doc)
        letters = sorted({current[zone] for zone in group.zones if zone in current}) or ["a"]
        if strategy_id == "zonal_mig":
            letters = letters[:1]
        updated = _set_hint(group, "mig", "regional" if strategy_id == "regional_mig" else "zonal")
        updated = _set_hint(updated, "zone_letters", letters)
        updated = _set_hint(updated, "subnet", diff.context.get("subnet"))
        return _replace_object(doc, updated)


class VmAsgHealthcheck(DifferenceRule):
    id = "VM-ASG-HEALTHCHECK"
    family = "compute"
    title = "Autohealing needs a health check EasyMigrate must be told about"
    impact = (
        "An ASG with ELB health checks relied on a target group's health check, which this "
        "family does not migrate. A managed instance group heals from a standalone health "
        "check instead, whose port and path are yours to confirm. An ASG with EC2 health "
        "checks only replaced instances that failed at the VM level."
    )
    strategies = (
        Strategy(
            id="autohealing_http",
            title="Autoheal on an HTTP health check",
            effect=(
                "a standalone HTTP health check drives autohealing; an instance that fails it "
                "is recreated. A wrong port or path recreates healthy instances in a loop"
            ),
            risk="medium",
        ),
        Strategy(
            id="no_autohealing",
            title="No autohealing",
            effect=(
                "instances are recreated only when the VM itself fails, not when the "
                "application does"
            ),
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect="no health check is generated; configure autohealing yourself",
            risk="low",
            manual=True,
        ),
    )
    default = "autohealing_http"
    param_docs = {
        "port": "TCP port the health check requests, e.g. 80",
        "request_path": "HTTP path the health check requests, e.g. /healthz",
    }

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference(
                [group.id],
                health_check_type=group.health_check_type,
                grace_sec=group.health_check_grace,
            )
            for group in _groups(ir)
        ]

    def default_strategy(self, diff: Difference) -> str:
        # EC2 checks never looked at the application; inventing an HTTP port for them would
        # recreate healthy instances whenever the guess is wrong.
        return self.default if diff.context.get("health_check_type") == "ELB" else "no_autohealing"

    def default_params(self, diff: Difference, strategy_id: str) -> dict[str, Any]:
        if strategy_id == "autohealing_http":
            return {"port": 80, "request_path": "/"}
        return {}

    def validate_params(self, diff: Difference, strategy_id: str, params: dict[str, Any]) -> None:
        if strategy_id != "autohealing_http":
            return
        port = params.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        path = params.get("request_path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("request_path must start with '/'")

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, group = _subject(ir, diff)
        if group is None:
            return doc
        if strategy_id in {"no_autohealing", "manual"}:
            return doc
        if strategy_id != "autohealing_http":
            raise ValueError(f"unknown strategy {strategy_id!r}")
        params = {**self.default_params(diff, strategy_id), **(params or {})}
        self.validate_params(diff, strategy_id, params)
        updated = _set_hint(
            group,
            "health_check",
            {"port": params["port"], "request_path": params["request_path"]},
        )
        updated = _set_hint(
            updated, "initial_delay_sec", group.health_check_grace or _DEFAULT_GRACE_SEC
        )
        return _replace_object(doc, updated)


class VmAsgScaling(DifferenceRule):
    id = "VM-ASG-SCALING"
    family = "compute"
    title = "Only CPU target tracking has a direct autoscaler equivalent"
    impact = (
        "A GCP autoscaler can track CPU utilisation the way an ASG target-tracking policy does. "
        "Step scaling, scheduled actions and other metrics have no single equivalent."
    )
    strategies = (
        Strategy(
            id="cpu_target",
            title="Autoscale on CPU utilisation",
            effect=(
                "an autoscaler holds average CPU at the source's target between the group's "
                "minimum and maximum size; its scale-in timing differs from AWS"
            ),
            risk="medium",
        ),
        Strategy(
            id="fixed_size",
            title="Fixed size",
            effect="no autoscaler; the group stays at its desired capacity",
            risk="low",
        ),
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "no autoscaler is generated; the group stays at its desired capacity until you "
                "add scaling yourself"
            ),
            risk="low",
            manual=True,
        ),
    )
    default = "cpu_target"

    def detect(self, ir: IRDocument) -> list[Difference]:
        diffs = []
        for group in _groups(ir):
            if not group.scaling_policies:
                continue
            policies = group.scaling_policies
            cpu = (
                len(policies) == 1
                and policies[0].policy_type == "TargetTrackingScaling"
                and policies[0].metric == _CPU_METRIC
                and policies[0].target_value is not None
                and group.max_size >= 1
            )
            diffs.append(
                self.difference(
                    [group.id],
                    policies=[p.model_dump() for p in policies],
                    cpu_target=policies[0].target_value if cpu else None,
                )
            )
        return diffs

    def allowed_strategies(self, diff: Difference) -> tuple[Strategy, ...]:
        if diff.context.get("cpu_target") is None:
            return (self.strategy("manual"),)
        return self.strategies

    def default_strategy(self, diff: Difference) -> str:
        return self.default if diff.context.get("cpu_target") is not None else "manual"

    def apply(
        self,
        ir: IRDocument,
        diff: Difference,
        strategy_id: str,
        params: dict[str, Any] | None = None,
    ) -> IRDocument:
        doc, group = _subject(ir, diff)
        if group is None:
            return doc
        if strategy_id not in {s.id for s in self.allowed_strategies(diff)}:
            raise ValueError(f"strategy {strategy_id!r} is not offered for {group.id}")
        if strategy_id != "cpu_target":
            return doc
        target = diff.context["cpu_target"] / 100
        return _replace_object(doc, _set_hint(group, "autoscaler", {"cpu_target": target}))


class VmAsgMixedInstances(_AcknowledgeOnly, DifferenceRule):
    id = "VM-ASG-MIXED-INSTANCES"
    family = "compute"
    title = "A mixed-instances policy cannot be expressed in one instance template"
    impact = (
        "An instance template declares one machine type. A weighted mix of types and purchase "
        "options needs choices EasyMigrate cannot infer."
    )
    strategies = (
        Strategy(
            id="manual",
            title="Handle manually",
            effect=(
                "the group is created with the launch template's single machine type; "
                "reproduce the mix yourself if it mattered for cost or capacity"
            ),
            risk="medium",
            manual=True,
        ),
    )
    default = "manual"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [self.difference([group.id]) for group in _groups(ir) if group.mixed_instances]


class VmAsgTargetGroups(_AcknowledgeOnly, DifferenceRule):
    id = "VM-ASG-TARGET-GROUPS"
    family = "compute"
    title = "The group's load-balancer attachment moves with its load balancer"
    impact = (
        "An Auto Scaling group registers its instances with target groups. On GCP the load "
        "balancer names the managed instance group as a backend instead, so the attachment exists "
        "only if the target group's load balancer is migrated too (spec 7)."
    )
    strategies = (
        Strategy(
            id="drop_with_note",
            title="Attach through the migrated load balancer",
            effect=(
                "the group becomes a backend of each migrated load balancer that targeted it; a "
                "target group whose load balancer is not migrated sends it no traffic"
            ),
            risk="medium",
        ),
    )
    default = "drop_with_note"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([group.id], target_groups=group.target_groups)
            for group in _groups(ir)
            if group.target_groups
        ]


class VmAsgLifecycle(_AcknowledgeOnly, DifferenceRule):
    id = "VM-ASG-LIFECYCLE"
    family = "compute"
    title = "Lifecycle hooks have no managed-instance-group equivalent"
    impact = (
        "An ASG lifecycle hook pauses a launch or termination until something calls back. MIG "
        "update policies have no pause-and-callback step, so whatever the hook drove — "
        "draining, registration, log shipping — no longer runs."
    )
    strategies = (
        Strategy(
            id="note_only",
            title="Note it",
            effect="the hooks are named in the report; nothing pauses instance changes",
            risk="medium",
        ),
    )
    default = "note_only"

    def detect(self, ir: IRDocument) -> list[Difference]:
        return [
            self.difference([group.id], hooks=group.lifecycle_hooks)
            for group in _groups(ir)
            if group.lifecycle_hooks
        ]
