"""Generate GCP compute resources from the resolved IR (spec 4 §6).

Generation itself is plan 3. This module exists now so the plugin has the same shape as
every other family and so `scripts/gen_differences_doc.py`, which imports
`plugins.<family>.generate_gcp` for every registered plugin, keeps working. Until plan 3,
`generate` returns an empty file and FIELD_COVERAGE declares every field as not yet
generated -- an honest claim rather than a silent gap.
"""

from __future__ import annotations

from easymigrate.core.plugin import GenerateContext
from easymigrate.ir.base import IRDocument
from easymigrate.providers.gcp.hcl import HclFile

_METADATA_FIELDS = {
    "kind": "metadata",
    "id": "metadata",
    "name": "metadata",
    "labels": "metadata",
    "source": "metadata",
    "target_hints": "metadata",
    "resolution": "metadata",
    "excluded": "metadata",
}

FIELD_COVERAGE: dict[str, dict[str, str]] = {
    "compute_instance": {
        **_METADATA_FIELDS,
        "instance_type": "not_generated:generation is spec 4 plan 3",
        "vcpus": "not_generated:generation is spec 4 plan 3",
        "memory_mib": "not_generated:generation is spec 4 plan 3",
        "architecture": "not_generated:generation is spec 4 plan 3",
        "image": "not_generated:generation is spec 4 plan 3",
        "platform": "not_generated:generation is spec 4 plan 3",
        "platform_details": "not_generated:generation is spec 4 plan 3",
        "firewall_policies": "not_generated:generation is spec 4 plan 3",
        "disks": "not_generated:generation is spec 4 plan 3",
        "user_data": "not_generated:generation is spec 4 plan 3",
        "key_pair": "not_generated:generation is spec 4 plan 3",
        "key_material": "not_generated:generation is spec 4 plan 3",
        "iam_profile": "not_generated:generation is spec 4 plan 3",
        "spot": "not_generated:generation is spec 4 plan 3",
        "imds": "not_generated:generation is spec 4 plan 3",
        "state": "not_generated:generation is spec 4 plan 3",
        "zone": "not_generated:generation is spec 4 plan 3",
        "zone_id": "not_generated:generation is spec 4 plan 3",
        "subnet": "not_generated:generation is spec 4 plan 3",
        "external_ip": "not_generated:generation is spec 4 plan 3",
        "private_ip": "not_generated:generation is spec 4 plan 3",
        "tenancy": "not_generated:generation is spec 4 plan 3",
        "placement_group": "not_generated:generation is spec 4 plan 3",
        "termination_protection": "not_generated:generation is spec 4 plan 3",
        "monitoring": "not_generated:generation is spec 4 plan 3",
        "eks_cluster": "not_generated:generation is spec 4 plan 3",
    },
    "disk": {
        **_METADATA_FIELDS,
        "size_gib": "not_generated:generation is spec 4 plan 3",
        "type": "not_generated:generation is spec 4 plan 3",
        "iops": "not_generated:generation is spec 4 plan 3",
        "throughput_mibps": "not_generated:generation is spec 4 plan 3",
        "encrypted": "not_generated:generation is spec 4 plan 3",
        "kms_key": "not_generated:generation is spec 4 plan 3",
        "zone": "not_generated:generation is spec 4 plan 3",
        "boot": "not_generated:generation is spec 4 plan 3",
        "instance_store": "not_generated:generation is spec 4 plan 3",
    },
    "external_address": {
        **_METADATA_FIELDS,
        "address": "not_generated:generation is spec 4 plan 3",
        "attached": "not_generated:generation is spec 4 plan 3",
        "scope": "not_generated:generation is spec 4 plan 3",
    },
    "instance_template": {
        **_METADATA_FIELDS,
        "instance_type": "not_generated:generation is spec 4 plan 3",
        "vcpus": "not_generated:generation is spec 4 plan 3",
        "memory_mib": "not_generated:generation is spec 4 plan 3",
        "architecture": "not_generated:generation is spec 4 plan 3",
        "image": "not_generated:generation is spec 4 plan 3",
        "platform": "not_generated:generation is spec 4 plan 3",
        "platform_details": "not_generated:generation is spec 4 plan 3",
        "firewall_policies": "not_generated:generation is spec 4 plan 3",
        "disks": "not_generated:generation is spec 4 plan 3",
        "user_data": "not_generated:generation is spec 4 plan 3",
        "key_pair": "not_generated:generation is spec 4 plan 3",
        "key_material": "not_generated:generation is spec 4 plan 3",
        "iam_profile": "not_generated:generation is spec 4 plan 3",
        "spot": "not_generated:generation is spec 4 plan 3",
        "imds": "not_generated:generation is spec 4 plan 3",
        "version": "not_generated:generation is spec 4 plan 3",
        "default_version": "not_generated:generation is spec 4 plan 3",
    },
    "instance_group": {
        **_METADATA_FIELDS,
        "template": "not_generated:generation is spec 4 plan 3",
        "min_size": "not_generated:generation is spec 4 plan 3",
        "max_size": "not_generated:generation is spec 4 plan 3",
        "desired_capacity": "not_generated:generation is spec 4 plan 3",
        "zones": "not_generated:generation is spec 4 plan 3",
        "subnets": "not_generated:generation is spec 4 plan 3",
        "health_check_type": "not_generated:generation is spec 4 plan 3",
        "health_check_grace": "not_generated:generation is spec 4 plan 3",
        "target_groups": "not_generated:generation is spec 4 plan 3",
        "scaling_policies": "not_generated:generation is spec 4 plan 3",
        "mixed_instances": "not_generated:generation is spec 4 plan 3",
    },
}


def generate(resolved: IRDocument, ctx: GenerateContext) -> HclFile:
    return HclFile()
