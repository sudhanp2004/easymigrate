"""GKE facts referenced by the AWS→GCP Kubernetes difference rules (spec 5 §5).

Model knowledge dated 2026-09-22, like `gce_facts.py` and `cloudsql_facts.py`: not fetched from
Google, and to be re-verified before customers. A minor version newer than this list is not an
error — the rule says it is unverified, and `tofu plan` has the final word.
"""

from __future__ import annotations

import ipaddress

FACTS_AS_OF = "2026-09-22"

# Kubernetes minors GKE offers for new clusters.
GKE_MINORS: tuple[str, ...] = ("1.31", "1.32", "1.33", "1.34", "1.35")

# EKS AMI type -> GKE node image type. Container-optimized Linux AMIs map to COS; Windows to the
# LTSC image. CUSTOM has no equivalent: the custom image and its bootstrap are not carried.
LINUX_IMAGE = "COS_CONTAINERD"
UBUNTU_IMAGE = "UBUNTU_CONTAINERD"
WINDOWS_IMAGE = "WINDOWS_LTSC_CONTAINERD"

# EKS control-plane log type -> GKE logging component. `audit` becomes Cloud Audit Logs, which
# GKE writes for admin activity regardless; `authenticator` has no equivalent.
LOG_COMPONENTS: dict[str, str | None] = {
    "api": "APISERVER",
    "controllerManager": "CONTROLLER_MANAGER",
    "scheduler": "SCHEDULER",
    "audit": None,
    "authenticator": None,
}

# EKS add-on -> what replaces it on GKE. `None` means GKE has it built in and nothing is
# generated; a string names the GKE add-on EasyMigrate enables.
ADDON_MAP: dict[str, str | None] = {
    "vpc-cni": None,
    "kube-proxy": None,
    "coredns": None,
    "eks-pod-identity-agent": None,
    "aws-ebs-csi-driver": None,  # GKE's Persistent Disk CSI driver is on by default
    "aws-efs-csi-driver": "filestore_csi",
}

# Secondary-range pools. Pods get CGNAT space (RFC 6598), which never collides with RFC 1918 VPC
# ranges; services get a disjoint slice of it.
PODS_POOL = ipaddress.ip_network("100.64.0.0/11")
PODS_PREFIX = 16
SERVICES_POOL = ipaddress.ip_network("100.96.0.0/16")
SERVICES_PREFIX = 20
MIN_PODS_PREFIX = 21  # pods range must be /21 or larger
MIN_SERVICES_PREFIX = 27


def minor(version: str) -> tuple[int, int]:
    major, _, rest = version.partition(".")
    return int(major), int(rest.split(".")[0] or 0)


def nearest_minor(version: str) -> tuple[str, str]:
    """(chosen minor, why) — the source minor when GKE offers it, else the nearest newer one
    (a cluster can be upgraded, never downgraded), else the source minor unverified."""
    if version in GKE_MINORS:
        return version, "offered"
    newer = [m for m in GKE_MINORS if minor(m) > minor(version)]
    if newer and minor(version) < minor(GKE_MINORS[0]):
        return newer[0], "upgraded"
    return version, "unverified"
