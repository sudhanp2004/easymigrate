"""Report tests (plan 4 B1, spec §6.1): section presence/ordering, md and json shape."""

from __future__ import annotations

from pathlib import Path

from moto import mock_aws

from easymigrate.core.pipeline import analyze as run_analyze
from easymigrate.core.pipeline import plan as run_plan
from easymigrate.core.pipeline import resolve as run_resolve
from easymigrate.core.pipeline import scan
from easymigrate.core.project import Project
from easymigrate.core.registry import source_for
from easymigrate.core.report import Report, _database_rows, build_report, write_report
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.database import (
    DatabaseInstance,
    DbBackup,
    DbCompute,
    DbNetwork,
    DbStorage,
)


class _FakeGcp:
    def credentials_ok(self) -> bool:
        return True

    def ensure_bucket(self, gcp_project, name, region, confirm) -> bool:
        return True


class _FakeTofu:
    def init(self, backend_config=None, *, backend=True) -> None:
        pass

    def plan(self, out: Path) -> str:
        out.write_bytes(b"fake-plan-bytes")
        return "Plan: 1 to add, 0 to change, 0 to destroy."

    def show_json(self, plan: Path) -> dict:
        return {"resource_changes": []}


_SECTIONS_IN_ORDER = [
    "Scan gaps",
    "Needs your action",
    "Decisions",
    "Not yet supported",
    "Translated",
]


def _small_project(tmp_path) -> Project:
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        vpc = ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[{"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "main"}]}],
        )["Vpc"]["VpcId"]
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24", AvailabilityZone="ap-south-1a")
        ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.2.0/24", AvailabilityZone="ap-south-1b")
        scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)
    return project


def test_report_after_analyze_has_header_and_coverage(tmp_path):
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_vpc(CidrBlock="10.0.0.0/16")
        scan(project, region="ap-south-1")
    run_analyze(project)

    report = build_report(project)

    assert report.source == "aws"
    assert report.target is None
    assert report.coverage["found"] >= 1
    assert report.plan is None


def test_report_sections_present_and_in_order(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)
    md = report.to_markdown()

    positions = [md.index(f"## {title}") for title in _SECTIONS_IN_ORDER]
    assert positions == sorted(positions), "sections must appear in the documented order"


def test_decisions_table_lists_every_decision_with_risk(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)

    assert report.decisions, "expected at least one decision (NET-ZONE-REGIONAL)"
    for row in report.decisions:
        assert row.key
        assert row.strategy
        assert row.decided_by in ("default", "user")
        assert row.risk in ("none", "low", "medium", "high")


def test_translated_counts_per_kind(tmp_path):
    project = _small_project(tmp_path)

    report = build_report(project)

    assert report.translated.get("network", 0) >= 1
    assert report.translated.get("subnet", 0) >= 2


def test_write_report_writes_md_and_json(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    write_report(project, report)

    assert project.report_md_path.exists()
    assert project.report_json_path.exists()
    assert "Scan gaps" in project.report_md_path.read_text(encoding="utf-8")


def test_json_schema_keys(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    data = report.to_dict()

    for key in (
        "source",
        "target",
        "coverage",
        "scan_gaps",
        "needs_action",
        "decisions",
        "not_supported",
        "translated",
        "info",
        "plan",
    ):
        assert key in data


def test_generation_finding_from_plan_summary_appears_in_report(tmp_path):
    """GEN-RENAMED (and other generation findings) reach plan_summary.json but were never
    folded into the report; they must show up in report.md's Notes section (info-level,
    spec §6.1: info findings appear only in the md/json files)."""
    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_vpc(
            CidrBlock="10.0.0.0/16",
            TagSpecifications=[
                {"ResourceType": "vpc", "Tags": [{"Key": "Name", "Value": "My_VPC!!"}]}
            ],
        )
        scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)
    run_plan(
        project,
        gcp_project="my-proj",
        gcp_region=None,
        confirm_bucket=lambda name: True,
        gcp=_FakeGcp(),
        tofu=_FakeTofu(),
    )

    report = build_report(project)

    renamed = [f for f in report.info if f.code == "GEN-RENAMED"]
    assert renamed, "expected a GEN-RENAMED finding from the plan summary in report.info"
    md = report.to_markdown()
    assert renamed[0].message in md


def test_plan_fingerprint_matches_plan_lock_hash(tmp_path):
    """The UI's Apply page reads `report.plan.fingerprint` and sends it back as
    `plan_fingerprint` on `POST /api/apply`; it must match the server's own hash of
    `plan.lock.json` (`easymigrate.ui.routes.stages.plan_fingerprint`) exactly."""
    import hashlib

    project = Project(tmp_path)
    with mock_aws():
        ec2 = source_for("aws").connect(region="ap-south-1").client("ec2")
        ec2.create_vpc(CidrBlock="10.0.0.0/16")
        scan(project, region="ap-south-1")
    run_analyze(project)
    run_resolve(project, accept_defaults=True)
    run_plan(
        project,
        gcp_project="my-proj",
        gcp_region=None,
        confirm_bucket=lambda name: True,
        gcp=_FakeGcp(),
        tofu=_FakeTofu(),
    )

    report = build_report(project)

    expected = hashlib.sha256(project.plan_lock_path.read_bytes()).hexdigest()
    assert report.plan.fingerprint == expected
    assert report.to_dict()["plan"]["fingerprint"] == expected


def test_plan_fingerprint_is_none_before_plan(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)
    assert report.plan is None


def test_info_findings_only_in_md_and_json_not_terminal(tmp_path):
    project = _small_project(tmp_path)
    report = build_report(project)

    # info findings are collected for md/json...
    assert isinstance(report.info, list)
    # ...but the terminal renderer (cli.render_report) is responsible for omitting them;
    # the Report object itself just carries them separately from the listed sections.
    md = report.to_markdown()
    for finding in report.info:
        assert finding.message in md


# --- Databases section (spec 2 §7) --------------------------------------------------


def _instance(name: str = "db", **overrides) -> DatabaseInstance:
    defaults = dict(
        id=f"database_instance:{name}",
        name=name,
        engine="postgres",
        engine_version="15.5",
        compute=DbCompute(class_name="db.t3.micro", vcpus=2, memory_mib=1024),
        storage=DbStorage(size_gib=20, type="gp2"),
        network=DbNetwork(port=5432),
        source=SourceRef(cloud="aws", type="rds:db", id=name, region="ap-south-1"),
    )
    defaults.update(overrides)
    return DatabaseInstance(**defaults)


def _doc(*instances: DatabaseInstance) -> IRDocument:
    return IRDocument(source={"cloud": "aws"}, objects=list(instances))


def test_database_row_reports_tier_and_its_source():
    row = _database_rows(_doc(_instance(target_hints={"tier": "db-f1-micro"})))[0]
    assert row.tier == "db-f1-micro"
    assert row.tier_source == "db.t3.micro, 2 vCPU, 1024 MiB"


def test_database_row_reports_unknown_sizing_rather_than_inventing_it():
    unsized = DbCompute(class_name="db.serverless")
    row = _database_rows(_doc(_instance(compute=unsized)))[0]
    assert row.tier_source == "db.serverless, size unknown"
    assert row.tier == "not decided"


def test_private_connectivity_names_the_psa_range_and_network():
    row = _database_rows(
        _doc(
            _instance(
                target_hints={
                    "private_network": "network:prod-vpc",
                    "psa": {"range": "10.64.0.0", "prefix_length": 24},
                }
            )
        )
    )[0]
    assert "private IP only" in row.connectivity
    assert "10.64.0.0/24" in row.connectivity
    assert "prod-vpc" in row.connectivity
    assert row.open_to_world is False


def test_public_connectivity_lists_the_authorized_networks():
    row = _database_rows(
        _doc(_instance(target_hints={"public_ip": True, "authorized_networks": ["10.0.0.0/16"]}))
    )[0]
    assert row.connectivity == "public IP, reachable from 10.0.0.0/16"
    assert row.open_to_world is False


def test_world_open_database_is_flagged_and_shouted_about_in_markdown():
    report = Report(
        source="aws",
        target="gcp",
        coverage={},
        databases=_database_rows(
            _doc(_instance(target_hints={"public_ip": True, "authorized_networks": ["0.0.0.0/0"]}))
        ),
    )
    assert report.databases[0].open_to_world is True
    markdown = report.to_markdown()
    assert "reachable from the whole internet" in markdown


def test_ipv6_world_open_database_is_flagged_and_shouted_about_in_markdown():
    """I4: `::/0` in authorized_networks is exactly as open to the world as
    `0.0.0.0/0`; an exact string match on the IPv4 form alone would silently under-flag
    it (open_to_world False, no bold warning) instead of raising the alarm."""
    report = Report(
        source="aws",
        target="gcp",
        coverage={},
        databases=_database_rows(
            _doc(_instance(target_hints={"public_ip": True, "authorized_networks": ["::/0"]}))
        ),
    )
    assert report.databases[0].open_to_world is True
    markdown = report.to_markdown()
    assert "reachable from the whole internet" in markdown


def test_excluded_instance_says_it_is_not_migrated_and_why():
    row = _database_rows(_doc(_instance(excluded="DB-TIER: sizing unknown")))[0]
    assert row.connectivity == "not migrated — DB-TIER: sizing unknown"


def test_backup_clamp_is_stated_not_hidden():
    row = _database_rows(
        _doc(
            _instance(
                backup=DbBackup(retention_days=400, window="03:00-04:00"),
                target_hints={
                    "backup": {"enabled": True, "retained_backups": 365, "start_time": "03:00"}
                },
            )
        )
    )[0]
    assert "365 backups retained" in row.backups
    assert "clamped from the source's 400 days" in row.backups


def test_backups_off_matches_the_source_wording():
    row = _database_rows(_doc(_instance(target_hints={"backup": {"enabled": False}})))[0]
    assert row.backups == "no automated backups, matching the source"


def test_pitr_reports_the_shorter_cloud_sql_log_retention():
    row = _database_rows(
        _doc(
            _instance(
                backup=DbBackup(retention_days=35),
                target_hints={"pitr": {"enabled": True, "log_retention_days": 7}},
            )
        )
    )[0]
    assert "keeping 7 days" in row.pitr
    assert "the source kept 35 days" in row.pitr


def test_replica_backups_do_not_overstate_protection_it_does_not_have():
    # DB-BACKUP-WINDOW/DB-PITR detect replicas too (I3), but generate_gcp never emits a
    # backup_configuration block for a replica: the report must not claim retained
    # backups a replica's generated HCL will not have.
    rows = _database_rows(
        _doc(
            _instance("primary"),
            _instance(
                "replica",
                role="read_replica",
                replica_of="database_instance:primary",
                backup=DbBackup(retention_days=7),
                target_hints={
                    "backup": {"enabled": True, "retained_backups": 7, "start_time": "03:00"}
                },
            ),
        )
    )
    row = next(r for r in rows if r.id == "database_instance:replica")
    assert "backups retained" not in row.backups
    assert "inherited from the primary" in row.backups


def test_replica_pitr_does_not_overstate_protection_it_does_not_have():
    rows = _database_rows(
        _doc(
            _instance("primary"),
            _instance(
                "replica",
                role="read_replica",
                replica_of="database_instance:primary",
                backup=DbBackup(retention_days=7),
                target_hints={"pitr": {"enabled": True, "log_retention_days": 7}},
            ),
        )
    )
    row = next(r for r in rows if r.id == "database_instance:replica")
    assert "keeping 7 days" not in row.pitr
    assert "inherited from the primary" in row.pitr


def test_dropped_parameters_are_listed_with_their_values_in_markdown():
    report = Report(
        source="aws",
        target="gcp",
        coverage={},
        databases=_database_rows(
            _doc(
                _instance(
                    target_hints={
                        "dropped_parameters": {"shared_buffers": "{DBInstanceClassMemory/32768}"}
                    }
                )
            )
        ),
    )
    markdown = report.to_markdown()
    assert "shared_buffers = {DBInstanceClassMemory/32768}" in markdown


def test_databases_section_carries_the_password_and_users_warnings():
    report = Report(
        source="aws", target="gcp", coverage={}, databases=_database_rows(_doc(_instance()))
    )
    markdown = report.to_markdown()
    assert "Rotate it after cutover" in markdown
    assert "Only the master user was migrated" in markdown


def test_no_databases_means_no_databases_section():
    report = Report(source="aws", target="gcp", coverage={})
    assert "## Databases" not in report.to_markdown()


def test_report_json_carries_the_database_facts():
    report = Report(
        source="aws",
        target="gcp",
        coverage={},
        databases=_database_rows(_doc(_instance(target_hints={"tier": "db-f1-micro"}))),
    )
    data = report.to_dict()["databases"][0]
    assert data["id"] == "database_instance:db"
    assert data["tier"] == "db-f1-micro"
    assert data["open_to_world"] is False


# --- Compute section (spec 4 §7) ---------------------------------------------------


def _vm(**overrides):
    from easymigrate.ir.compute import ComputeInstance

    defaults = dict(
        id="compute_instance:web",
        name="web",
        source=SourceRef(cloud="aws", type="instance", id="i-1", region="ap-south-1"),
        instance_type="m5.large",
        vcpus=2,
        memory_mib=8192,
        architecture="x86_64",
        state="running",
        zone="ap-south-1a",
        subnet="subnet:a",
        image="ami-123",
        platform="linux",
        disks=["disk:boot", "disk:data"],
        firewall_policies=["firewall_policy:web-sg", "firewall_policy:db-only"],
        iam_profile="arn:aws:iam::1:instance-profile/app",
        target_hints={
            "machine_type": "n2-standard-2",
            "boot_image": {"project": "rocky-linux-cloud", "family": "rocky-linux-9"},
            "external": "none",
        },
    )
    defaults.update(overrides)
    return ComputeInstance(**defaults)


def _compute_doc(*extra):
    from easymigrate.ir.compute import Disk
    from easymigrate.ir.network import FirewallPolicy, Network, Subnet

    def src(t, i):
        return SourceRef(cloud="aws", type=t, id=i, region="ap-south-1")

    return IRDocument(
        source={"cloud": "aws"},
        objects=[
            Network(
                id="network:main",
                name="main",
                source=src("vpc", "vpc-1"),
                cidr_blocks=["10.0.0.0/16"],
                dns_hostnames=True,
                dns_resolution=True,
            ),
            Subnet(
                id="subnet:a",
                name="a",
                source=src("subnet", "s-1"),
                network="network:main",
                cidr="10.0.1.0/24",
                zone="ap-south-1a",
                internet="public",
                map_public_ip=False,
            ),
            FirewallPolicy(
                id="firewall_policy:web-sg",
                name="web-sg",
                source=src("sg", "sg-1"),
                network="network:main",
            ),
            FirewallPolicy(
                id="firewall_policy:db-only",
                name="db-only",
                source=src("sg", "sg-2"),
                network="network:main",
                excluded="DB-SG-FIREWALL: dropped",
            ),
            Disk(
                id="disk:boot",
                name="boot",
                source=src("volume", "v-1"),
                size_gib=20,
                type="gp3",
                zone="ap-south-1a",
                boot=True,
            ),
            Disk(
                id="disk:data",
                name="data",
                source=src("volume", "v-2"),
                size_gib=50,
                type="gp3",
                zone="ap-south-1a",
            ),
            *extra,
        ],
    )


def test_compute_row_states_what_plan_decided():
    from easymigrate.core.report import _compute_rows

    row = _compute_rows(_compute_doc(_vm()))[0]
    assert row.machine_type == "n2-standard-2"
    assert row.machine_source == "m5.large, 2 vCPU, 8192 MiB"
    assert row.image == "rocky-linux-cloud/rocky-linux-9"
    assert row.ami == "ami-123"
    assert row.firewall_policies == ["web-sg"]
    assert "only from inside the VPC" in row.reachability
    assert row.empty_disks == ["disk:data"]
    assert "was not translated" in row.service_account_note


def test_excluded_and_eks_instances_have_no_compute_row():
    from easymigrate.core.report import _compute_rows

    doc = _compute_doc(
        _vm(excluded="VM-BOOT-IMAGE: manual"),
    )
    assert _compute_rows(doc) == []
    assert _compute_rows(_compute_doc(_vm(eks_cluster="prod"))) == []


def test_compute_section_states_the_blank_machine_limitation():
    from easymigrate.core.report import _compute_rows

    report = Report(
        source="aws", target="gcp", coverage={}, compute=_compute_rows(_compute_doc(_vm()))
    )
    markdown = report.to_markdown()
    assert "## Compute" in markdown
    assert "AMI ami-123 was not migrated" in markdown
    assert "Disk disk:data is created empty" in markdown
    assert "No disk contents, installed software or running workload were migrated" in markdown
    assert report.to_dict()["compute"][0]["machine_type"] == "n2-standard-2"


def test_no_instances_means_no_compute_section():
    assert "## Compute" not in Report(source="aws", target="gcp", coverage={}).to_markdown()


# --- Kubernetes section (spec 5 §7) -------------------------------------------------


def _k8s_doc():
    from easymigrate.ir.kubernetes import KubernetesCluster, NodePool, PodIdentity
    from tests.plugins.kubernetes.builders import network_objects

    network = [
        o.model_copy(
            update={
                "target_hints": {
                    "secondary_ranges": [
                        {"name": "prod-pods", "cidr": "100.64.0.0/16"},
                        {"name": "prod-services", "cidr": "172.20.0.0/16"},
                    ]
                }
            }
        )
        if o.id == "subnet:private-1"
        else o
        for o in network_objects()
    ]
    cluster = KubernetesCluster(
        id="kubernetes_cluster:prod",
        name="prod",
        source=SourceRef(cloud="aws", type="eks-cluster", id="prod", region="ap-south-1"),
        version="1.31",
        pod_identities=[PodIdentity(namespace="app", service_account="api")],
        access_entries=["arn:aws:iam::1:role/admin"],
        target_hints={
            "version": "1.31",
            "release_channel": "EXTENDED",
            "endpoint": {"private_endpoint": False, "authorized_networks": None},
            "subnet": "subnet:private-1",
            "pods_range": "prod-pods",
            "services_range": "prod-services",
        },
    )
    pool = NodePool(
        id="node_pool:workers",
        name="workers",
        source=SourceRef(cloud="aws", type="eks-nodegroup", id="prod/workers", region="x"),
        cluster=cluster.id,
        origin="self_managed",
        min_size=1,
        max_size=4,
        desired_size=2,
        ami_type="CUSTOM",
        target_hints={
            "machine_type": "n2-standard-2",
            "image_type": "COS_CONTAINERD",
            "zone_letters": ["a", "b"],
            "autoscaling": {"total_min": 1, "total_max": 4, "initial_per_zone": 1},
        },
    )
    return IRDocument(source={"cloud": "aws"}, objects=[*network, cluster, pool])


def test_kubernetes_row_states_what_plan_decided():
    from easymigrate.core.report import _kubernetes_rows

    row = _kubernetes_rows(_k8s_doc())[0]
    assert row.version == "1.31, pinned (EKS ran 1.31)"
    assert "open to every address" in row.endpoint
    assert row.ranges == "pods 100.64.0.0/16, services 172.20.0.0/16 on subnet:private-1"
    assert "autoscaling 1–4 nodes in total" in row.node_pools[0]
    assert "custom AMI and bootstrap not carried" in row.node_pools[0]
    assert row.iam_bindings == ["app/api"]


def test_kubernetes_section_says_the_cluster_is_empty():
    from easymigrate.core.report import _kubernetes_rows

    report = Report(
        source="aws", target="gcp", coverage={}, kubernetes=_kubernetes_rows(_k8s_doc())
    )
    markdown = report.to_markdown()
    assert "## Kubernetes" in markdown
    assert "Every cluster is created empty" in markdown
    assert "arn:aws:iam::1:role/admin" in markdown
    assert report.to_dict()["kubernetes"][0]["id"] == "kubernetes_cluster:prod"


# --- Storage section (spec 6 §7) ----------------------------------------------------


def test_storage_rows_state_access_versioning_and_what_did_not_carry():
    from easymigrate.core.report import _storage_rows
    from tests.plugins.storage.test_generate_gcp_golden import resolved_case

    document, _ = resolved_case("versioned_lifecycle_lock")
    row = _storage_rows(document)[0]
    assert row.access.startswith("private")
    assert row.versioning == "on"
    assert any("retention policy of 30 days" in note for note in row.notes)
    assert "replication not reproduced" in row.notes
    report = Report(source="aws", target="gcp", coverage={}, storage=[row])
    markdown = report.to_markdown()
    assert "## Storage" in markdown
    assert "Every bucket is created empty" in markdown


def test_renamed_bucket_names_its_origin():
    from easymigrate.core.report import _storage_rows
    from tests.plugins.storage.test_generate_gcp_golden import resolved_case

    document, _ = resolved_case("plain_and_dotted")
    names = [row.name for row in _storage_rows(document)]
    assert "www-example-com (renamed from www.example.com)" in names


# --- Load balancers section (spec 7 §7) ---------------------------------------------


def test_load_balancer_rows_name_the_type_listeners_and_what_was_dropped():
    from easymigrate.core.report import _load_balancer_rows
    from tests.plugins.loadbalancing.test_generate_gcp_golden import resolved_case

    document, _ = resolved_case("alb_https_asg")
    row = _load_balancer_rows(document)[0]
    assert row.shape == "global external Application Load Balancer"
    assert "HTTP 80 → redirects to HTTPS (created)" in row.listeners
    assert "listener 443 rule priority 20" in row.dropped
    markdown = Report(source="aws", target="gcp", coverage={}, load_balancers=[row]).to_markdown()
    assert "## Load balancers" in markdown
    assert "repointed at cutover" in markdown
