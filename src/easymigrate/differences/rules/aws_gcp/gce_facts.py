"""Compute Engine facts referenced by the AWS→GCP compute difference rules (spec 4 §5).

All tables below were checked 2026-09-19 against Compute Engine machine-type, image and
persistent-disk documentation. Refreshing them later is one edit to this module.

Like `cloudsql_facts.py`, this is model knowledge with a date, not something fetched from
Google. Re-verify before customers.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

FACTS_AS_OF = "2026-09-19"


@dataclass(frozen=True)
class MachineType:
    name: str
    family: str
    vcpus: int
    memory_mib: int


def _series(family: str, class_: str, per_vcpu_mib: int, sizes: tuple[int, ...]) -> tuple:
    return tuple(MachineType(f"{family}-{class_}-{n}", family, n, n * per_vcpu_mib) for n in sizes)


_SIZES = (2, 4, 8, 16, 32, 48, 64, 80)

# Predefined types, by series. Ratios are Compute Engine's: standard 4 GiB/vCPU,
# highmem 8 GiB/vCPU, highcpu 1 GiB/vCPU. Checked 2026-09-19.
PREDEFINED: tuple[MachineType, ...] = (
    *_series("e2", "standard", 4096, (2, 4, 8, 16, 32)),
    *_series("e2", "highmem", 8192, (2, 4, 8, 16)),
    *_series("e2", "highcpu", 1024, (2, 4, 8, 16, 32)),
    *_series("n2", "standard", 4096, _SIZES),
    *_series("n2", "highmem", 8192, (2, 4, 8, 16, 32, 48, 64, 80)),
    *_series("n2", "highcpu", 1024, _SIZES),
    *_series("c2", "standard", 4096, (4, 8, 16, 30, 60)),
)

# Burstable, shared-vCPU types. Deliberately absent from PREDEFINED: a burstable source
# reaches these through the shared-core branch, not the normal vCPU match.
SHARED_CORE: tuple[MachineType, ...] = (
    MachineType("e2-micro", "e2", 2, 1024),
    MachineType("e2-small", "e2", 2, 2048),
    MachineType("e2-medium", "e2", 2, 4096),
)

# AWS family letter → the GCP series whose purpose matches. Checked 2026-09-19.
AWS_FAMILY_TO_GCP: dict[str, str] = {
    "t": "e2",  # burstable → shared-core / cost-optimised
    "m": "n2",  # general purpose
    "a": "n2",
    "c": "c2",  # compute optimised
    "r": "n2",  # memory optimised → n2-highmem, picked by ratio below
    "x": "n2",
    "z": "n2",
    "i": "n2",
    "d": "n2",
    "g": "n2",
    "p": "n2",
}

# Burstable families whose smallest sizes have no full-size equivalent.
BURSTABLE_FAMILIES = frozenset({"t"})

# Image project and family per source platform. Checked 2026-09-19.
# Amazon Linux has no Compute Engine equivalent; Rocky Linux is the nearest
# RHEL-compatible distribution, and VM-BOOT-IMAGE says so outright.
IMAGE_FAMILIES: dict[str, tuple[str, str]] = {
    "amazon": ("rocky-linux-cloud", "rocky-linux-9"),
    "ubuntu": ("ubuntu-os-cloud", "ubuntu-2204-lts"),
    "debian": ("debian-cloud", "debian-12"),
    "rhel": ("rhel-cloud", "rhel-9"),
    "suse": ("suse-cloud", "sles-15"),
    "rocky": ("rocky-linux-cloud", "rocky-linux-9"),
    "windows": ("windows-cloud", "windows-2022"),
}

# Substrings of an AMI's reported platform details, in match order.
_PLATFORM_MARKERS: tuple[tuple[str, str], ...] = (
    ("windows", "windows"),
    ("ubuntu", "ubuntu"),
    ("debian", "debian"),
    ("red hat", "rhel"),
    ("rhel", "rhel"),
    ("suse", "suse"),
    ("rocky", "rocky"),
)

# EBS volume type → persistent disk type. Provisioned IOPS and throughput do not carry;
# VM-DISK-TYPE reports that. Checked 2026-09-19.
DISK_TYPE: dict[str, str] = {
    "gp2": "pd-balanced",
    "gp3": "pd-balanced",
    "io1": "pd-ssd",
    "io2": "pd-ssd",
    "st1": "pd-standard",
    "sc1": "pd-standard",
    "standard": "pd-standard",
}

# gp3's free baseline. AWS reports these figures on every gp3 volume whether or not anything
# was provisioned, so a volume at exactly these numbers has provisioned nothing. Found in the
# 2026-09-19 live run, where VM-DISK-TYPE fired for every gp3 disk and claimed performance was
# lost when none had been bought. Checked 2026-09-19.
GP3_BASELINE_IOPS = 3000
GP3_BASELINE_THROUGHPUT_MIBPS = 125

# Local SSD is attached in fixed-size devices; a partial device cannot be requested.
LOCAL_SSD_GIB = 375

# Compute Engine's smallest persistent disk.
MIN_DISK_GIB = 10

_FAMILY_LETTER = re.compile(r"^([a-z])")


def family_for(instance_type: str) -> str:
    match = _FAMILY_LETTER.match(instance_type)
    letter = match.group(1) if match else "m"
    return AWS_FAMILY_TO_GCP.get(letter, "n2")


def is_burstable(instance_type: str) -> bool:
    match = _FAMILY_LETTER.match(instance_type)
    return bool(match) and match.group(1) in BURSTABLE_FAMILIES


def nearest_predefined(
    vcpus: int | None, memory_mib: int | None, instance_type: str
) -> MachineType | None:
    """The smallest type that gives at least the source's vCPUs and memory.

    Memory is never reduced: a machine that boots with less RAM than it had is a silent
    behaviour change, which principle 1 forbids. The memory it gains instead is reported by
    VM-MACHINE-TYPE as the delta.
    """
    if vcpus is None or memory_mib is None:
        return None

    if is_burstable(instance_type):
        shared = [m for m in SHARED_CORE if m.memory_mib >= memory_mib]
        if shared:
            return min(shared, key=lambda m: m.memory_mib)

    preferred = family_for(instance_type)
    for candidates in (
        [m for m in PREDEFINED if m.family == preferred and m.vcpus == vcpus],
        [m for m in PREDEFINED if m.vcpus == vcpus],
        [m for m in PREDEFINED if m.family == preferred and m.vcpus >= vcpus],
        list(PREDEFINED),
    ):
        fits = [m for m in candidates if m.memory_mib >= memory_mib and m.vcpus >= vcpus]
        if fits:
            return min(fits, key=lambda m: (m.vcpus, m.memory_mib, m.name))
    return None


def image_for(platform: str, platform_details: str | None) -> tuple[str, str]:
    """(image project, image family) for a source platform. Defaults to Amazon Linux's
    stand-in, because an unrecognised Linux AMI is most often Amazon Linux."""
    if platform == "windows":
        return IMAGE_FAMILIES["windows"]
    haystack = (platform_details or "").lower()
    for marker, key in _PLATFORM_MARKERS:
        if marker in haystack:
            return IMAGE_FAMILIES[key]
    return IMAGE_FAMILIES["amazon"]


def local_ssd_units(size_gib: int | None) -> int:
    """Whole 375 GiB local SSD devices needed to cover `size_gib`, at least one."""
    if not size_gib or size_gib <= 0:
        return 1
    return max(1, math.ceil(size_gib / LOCAL_SSD_GIB))
