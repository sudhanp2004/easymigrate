"""AWS→GCP compute difference rules (spec 4 §5).

Rules decide the GCP shape and record it in `target_hints`; `plugins/compute/generate_gcp.py`
renders those hints and never re-derives a choice. GCP facts live in `gce_facts.py` beside this
module, because `easymigrate.differences` may import only `easymigrate.ir`.
"""

from __future__ import annotations

from typing import Any

from easymigrate.differences.model import Difference, DifferenceRule, Risk, Strategy
from easymigrate.differences.rules.aws_gcp.gce_facts import (
    IMAGE_FAMILIES,
    image_for,
    nearest_predefined,
)
from easymigrate.ir.base import IntentObject, IRDocument
from easymigrate.ir.compute import ComputeInstance, InstanceTemplate

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
