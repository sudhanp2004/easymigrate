"""Check EasyMigrate's built-in Google Cloud facts against a live project.

The `*_facts.py` modules and `providers/gcp/zones.py` are model knowledge with a date. Run this
before a customer engagement (and whenever Google changes something) to see what drifted:

    uv run python scripts/verify_gcp_facts.py --project my-gcp-project [--region asia-south1]

It only reads: machine types, images, disk types, zones, the GKE server config, Cloud SQL flags
and predefined IAM roles. Exit status 1 means at least one fact is wrong.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Iterable

from easymigrate.core.regions import REGION_MAP
from easymigrate.differences.rules.aws_gcp import cloudsql_facts, gce_facts, gke_facts, iam_facts
from easymigrate.providers.gcp.zones import ZONE_SUFFIXES

GCLOUD = shutil.which("gcloud") or "gcloud"
problems: list[str] = []
checks = 0


def gcloud(*args: str) -> object:
    result = subprocess.run(
        [GCLOUD, *args, "--format=json"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip().splitlines()[-1] if result.stderr else "failed")
    return json.loads(result.stdout or "null")


def check(ok: bool, message: str) -> None:
    global checks
    checks += 1
    if not ok:
        problems.append(message)


def machine_types(project: str, zones: Iterable[str]) -> None:
    seen: dict[str, tuple[int, int]] = {}
    for zone in zones:
        for item in gcloud(
            "compute", "machine-types", "list", f"--zones={zone}", f"--project={project}"
        ):
            seen[item["name"]] = (item["guestCpus"], item["memoryMb"])
    for mt in (*gce_facts.PREDEFINED, *gce_facts.SHARED_CORE):
        actual = seen.get(mt.name)
        check(actual is not None, f"machine type {mt.name} not offered in the checked zones")
        if actual is None:
            continue
        # Shared-core types report fractional CPUs as whole guestCpus; compare memory only.
        if mt in gce_facts.SHARED_CORE:
            check(
                actual[1] == mt.memory_mib,
                f"{mt.name}: memory {actual[1]} MiB, facts say {mt.memory_mib}",
            )
        else:
            check(
                actual == (mt.vcpus, mt.memory_mib),
                f"{mt.name}: {actual}, facts say {(mt.vcpus, mt.memory_mib)}",
            )


def images() -> None:
    for key, (image_project, family) in gce_facts.IMAGE_FAMILIES.items():
        try:
            image = gcloud(
                "compute", "images", "describe-from-family", family, f"--project={image_project}"
            )
        except RuntimeError as exc:
            check(False, f"image family {image_project}/{family} ({key}): {exc}")
            continue
        check(True, "")
        minimum = gce_facts.min_disk_for_image(image_project, family)
        actual = int(image.get("diskSizeGb", 0))
        check(
            minimum >= actual,
            f"{image_project}/{family}: minimum disk {actual} GB, facts say {minimum}",
        )


def disk_types(project: str, zone: str) -> None:
    available = {
        d["name"]
        for d in gcloud("compute", "disk-types", "list", f"--zones={zone}", f"--project={project}")
    }
    for wanted in {*gce_facts.DISK_TYPE.values(), "hyperdisk-balanced"}:
        check(wanted in available, f"disk type {wanted} not offered in {zone}")


def zones(project: str) -> None:
    actual: dict[str, set[str]] = {}
    for zone in gcloud("compute", "zones", "list", f"--project={project}"):
        region = zone["region"].rsplit("/", 1)[-1]
        actual.setdefault(region, set()).add(zone["name"].rsplit("-", 1)[-1])
    for region in sorted({*ZONE_SUFFIXES, *REGION_MAP.values()}):
        if region not in actual:
            check(False, f"region {region} does not exist (REGION_MAP or ZONE_SUFFIXES)")
            continue
        expected = set(ZONE_SUFFIXES.get(region, ()))
        check(
            expected <= actual[region],
            f"{region}: facts say zones {sorted(expected)}, GCP has {sorted(actual[region])}",
        )
        if expected and expected != actual[region]:
            print(f"  note: {region} also has zones {sorted(actual[region] - expected)}")


def gke(project: str, region: str) -> None:
    config = gcloud("container", "get-server-config", f"--region={region}", f"--project={project}")
    offered = {".".join(v.split(".")[:2]) for v in config.get("validMasterVersions", [])}
    for channel in config.get("channels", []):
        offered |= {".".join(v.split(".")[:2]) for v in channel.get("validVersions", [])}
    for minor in gke_facts.GKE_MINORS:
        check(
            minor in offered, f"GKE {minor} is not offered in {region} (offered: {sorted(offered)})"
        )
    newest = max(offered, key=gke_facts.minor) if offered else None
    if newest and newest not in gke_facts.GKE_MINORS:
        print(f"  note: GKE now offers {newest}, not in GKE_MINORS")
    channels = {c.get("channel") for c in config.get("channels", [])}
    check(
        "EXTENDED" in channels,
        f"the EXTENDED release channel is not offered (channels: {sorted(channels)})",
    )


def cloudsql() -> None:
    # Without --database-version the flags list returns MySQL only, so query each version.
    names_by_engine: dict[str, set[str]] = {"postgres": set(), "mysql": set()}
    for (engine, version), code in cloudsql_facts.DATABASE_VERSION.items():
        flags = gcloud("sql", "flags", "list", f"--database-version={code}")
        check(bool(flags), f"Cloud SQL {code} ({engine} {version}) is not offered")
        names_by_engine[engine] |= {flag["name"] for flag in flags or []}
    for engine, prefix in (("postgres", "POSTGRES"), ("mysql", "MYSQL")):
        known = [int(v.split(".")[0]) for (e, v) in cloudsql_facts.DATABASE_VERSION if e == engine]
        probe = f"{prefix}_{max(known) + 1}" if engine == "postgres" else None
        try:
            offered = bool(probe and gcloud("sql", "flags", "list", f"--database-version={probe}"))
        except RuntimeError:  # gcloud rejects versions it does not know yet
            offered = False
        if offered:
            print(f"  note: Cloud SQL now offers {probe}, not in DATABASE_VERSION")
    for engine, flag in cloudsql_facts.IAM_AUTH_FLAG.items():
        check(flag in names_by_engine[engine], f"Cloud SQL {engine} has no flag {flag}")
    for engine, names in cloudsql_facts.SUPPORTED_FLAGS.items():
        missing = sorted(set(names) - names_by_engine.get(engine, set()))
        check(
            not missing,
            f"Cloud SQL {engine} flags EasyMigrate maps that Cloud SQL lacks: {missing}",
        )
    tiers = {t["tier"] for t in gcloud("sql", "tiers", "list")}
    for tier in cloudsql_facts.SHARED_CORE_TIERS:
        check(tier in tiers, f"Cloud SQL tier {tier} is not offered")


def iam_roles() -> None:
    roles = {
        r for levels in iam_facts.SUGGESTED_ROLES.values() for rs in levels.values() for r in rs
    }
    for role in sorted(roles):
        try:
            gcloud("iam", "roles", "describe", role)
            check(True, "")
        except RuntimeError as exc:
            check(False, f"IAM role {role}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="asia-south1")
    args = parser.parse_args()
    region_zones = [f"{args.region}-{s}" for s in ZONE_SUFFIXES.get(args.region, ("a", "b", "c"))]
    steps = [
        ("machine types", lambda: machine_types(args.project, region_zones)),
        ("stock images", images),
        ("disk types", lambda: disk_types(args.project, region_zones[0])),
        ("regions and zones", lambda: zones(args.project)),
        ("GKE versions and channels", lambda: gke(args.project, args.region)),
        ("Cloud SQL versions, flags and tiers", cloudsql),
        ("IAM predefined roles", iam_roles),
    ]
    for title, step in steps:
        before = len(problems)
        try:
            step()
        except RuntimeError as exc:
            problems.append(f"{title}: could not check ({exc})")
        print(f"{'ok  ' if len(problems) == before else 'FAIL'} {title}")
    print(f"\n{checks} checks, {len(problems)} problem(s)")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
