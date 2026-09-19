"""AWS→GCP compute difference rules (spec 4 §5).

Rules decide the GCP shape and record it in `target_hints`; `plugins/compute/generate_gcp.py`
renders those hints and never re-derives a choice. GCP facts live in `gce_facts.py` beside this
module, because `easymigrate.differences` may import only `easymigrate.ir`.
"""

from __future__ import annotations

from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.gce_facts import (
    DISK_TYPE,
    IMAGE_FAMILIES,
    image_for,
    local_ssd_units,
    nearest_predefined,
)
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.ir.compute import ComputeInstance, Disk, InstanceTemplate

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

        Sorted by the stable `zone_id` where the scan captured one, since that is the only
        account-independent identifier AWS offers; instances without one sort last by zone
        name so the mapping stays deterministic.
        """
        keyed = {
            (instance.zone_id or "~", instance.zone)
            for instance in ir.of_kind(ComputeInstance)
            if instance.zone
        }
        letters: dict[str, str] = {}
        for index, (_, zone) in enumerate(sorted(keyed)):
            letters[zone] = chr(ord("a") + index)
        return letters

    def detect(self, ir: IRDocument) -> list[Difference]:
        letters = self._letters(ir)
        diffs = []
        for instance in sorted(ir.of_kind(ComputeInstance), key=lambda o: o.id):
            if not instance.zone:
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
        return _replace_object(doc, _set_hint(obj, "zone_letter", diff.context["zone_letter"]))


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
            provisioned = disk.iops is not None or disk.throughput_mibps is not None
            if disk.type not in {"io1", "io2", "st1", "sc1"} and not provisioned:
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
