"""Per-rule detect/apply tests for the AWS→GCP database catalogue, rules 1-10
(spec §5, plan 2 Part C). Small hand-built IR objects, local to this file.
"""

from __future__ import annotations

import pytest

from easymigrate.differences.decisions import merge_decisions
from easymigrate.differences.engine import detect, resolve_ir
from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES
from easymigrate.differences.rules.aws_gcp.database import (
    DbBackupWindow,
    DbEngineVersion,
    DbIamAuth,
    DbKmsCmek,
    DbLogExports,
    DbMaintenanceWindow,
    DbMultiAz,
    DbNameReuse,
    DbParamFlags,
    DbPitr,
    DbPort,
    DbPrivateIp,
    DbPublicAccess,
    DbReplica,
    DbSgFirewall,
    DbStorageAutoresize,
    DbStorageMinimum,
    DbStorageType,
    DbTier,
    DbUsers,
)
from easymigrate.ir.base import IRDocument, SourceRef
from easymigrate.ir.database import (
    DatabaseInstance,
    DbBackup,
    DbCompute,
    DbMaintenance,
    DbNetwork,
    DbStorage,
)
from easymigrate.ir.network import CidrPeer, FirewallPolicy, FirewallRule, Network

# ---- builders ---------------------------------------------------------------


def _source(type_="db-instance", id_="native-1"):
    return SourceRef(cloud="aws", type=type_, id=id_, region="ap-south-1")


def _network(id_="network:main", cidr="10.0.0.0/16"):
    return Network(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("vpc", id_),
        cidr_blocks=[cidr],
        dns_hostnames=True,
        dns_resolution=True,
    )


def _policy(id_, network="network:main", rules=None, applies_to=None):
    return FirewallPolicy(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source("security-group", id_),
        network=network,
        rules=rules or [],
        applies_to=applies_to or [],
    )


def _instance(
    id_="database_instance:db1",
    engine="postgres",
    engine_version="15.4",
    class_name="db.m5.large",
    vcpus=2,
    memory_mib=8192,
    size_gib=20,
    max_size_gib=None,
    storage_type="gp2",
    iops=None,
    throughput_mibps=None,
    network="network:main",
    publicly_accessible=False,
    port=5432,
    firewall_policies=None,
    high_availability=False,
    multi_az_cluster=False,
    parameters=None,
    encrypted=False,
    kms_key=None,
    backup_retention_days=0,
    backup_window=None,
    maintenance_window=None,
    iam_auth=False,
    log_exports=None,
    role="primary",
    replica_of=None,
    master_username=None,
):
    return DatabaseInstance(
        id=id_,
        name=id_.split(":", 1)[1],
        source=_source(),
        engine=engine,
        engine_version=engine_version,
        role=role,
        replica_of=replica_of,
        compute=DbCompute(class_name=class_name, vcpus=vcpus, memory_mib=memory_mib),
        storage=DbStorage(
            size_gib=size_gib,
            max_size_gib=max_size_gib,
            type=storage_type,
            iops=iops,
            throughput_mibps=throughput_mibps,
            encrypted=encrypted,
            kms_key=kms_key,
        ),
        network=DbNetwork(
            network=network,
            publicly_accessible=publicly_accessible,
            port=port,
            firewall_policies=firewall_policies or [],
        ),
        high_availability=high_availability,
        multi_az_cluster=multi_az_cluster,
        parameters=parameters or {},
        backup=DbBackup(retention_days=backup_retention_days, window=backup_window),
        maintenance=DbMaintenance(window=maintenance_window),
        iam_auth=iam_auth,
        log_exports=log_exports or [],
        master_username=master_username,
    )


def _doc(*objects) -> IRDocument:
    """Auto-injects the default `network:main` referenced by `_instance()` unless the
    caller already supplied some `Network` object of its own."""
    objs = list(objects)
    if not any(isinstance(o, Network) for o in objs):
        objs.insert(0, _network())
    return IRDocument(source={"cloud": "aws"}, objects=objs)


# ---- DB-ENGINE-VERSION -------------------------------------------------------


def test_engine_version_detects_every_instance():
    doc = _doc(_instance())
    diffs = DbEngineVersion().detect(doc)
    assert len(diffs) == 1
    assert diffs[0].key == "DB-ENGINE-VERSION@database_instance:db1"
    assert diffs[0].context == {
        "engine": "postgres",
        "version": "15.4",
        "major": "15",
        "target": "15",
    }


def test_engine_version_detects_nothing_without_instances():
    assert DbEngineVersion().detect(_doc(_network())) == []


def test_engine_version_postgres_15_maps_directly():
    instance = _instance(engine="postgres", engine_version="15.4")
    doc = _doc(instance)
    rule = DbEngineVersion()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "nearest_major")

    assert result.get(instance.id).target_hints == {"database_version": "POSTGRES_15"}
    assert doc.get(instance.id).target_hints == {}


def test_engine_version_mysql_8_0_maps_directly():
    instance = _instance(engine="mysql", engine_version="8.0.32")
    doc = _doc(instance)
    rule = DbEngineVersion()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "nearest_major")

    assert result.get(instance.id).target_hints == {"database_version": "MYSQL_8_0"}


def test_engine_version_postgres_9_6_is_the_upgrade_variant_with_only_two_strategies():
    instance = _instance(engine="postgres", engine_version="9.6.24")
    doc = _doc(instance)
    rule = DbEngineVersion()
    diff = rule.detect(doc)[0]
    assert diff.context["major"] == "9.6"
    assert diff.context["target"] == "13"

    upgraded = rule.apply(doc, diff, "upgrade_to_oldest_supported")
    assert upgraded.get(instance.id).target_hints == {"database_version": "POSTGRES_13"}

    excluded = rule.apply(doc, diff, "manual")
    assert (
        excluded.get(instance.id).excluded
        == "DB-ENGINE-VERSION: engine version requires manual review"
    )

    with pytest.raises(ValueError):
        rule.apply(doc, diff, "nearest_major")


def test_engine_version_manual_excludes_and_leaves_input_unchanged():
    instance = _instance()
    doc = _doc(instance)
    rule = DbEngineVersion()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded
        == "DB-ENGINE-VERSION: engine version requires manual review"
    )
    assert doc.get(instance.id).excluded is None


# ---- DB-TIER ------------------------------------------------------------------


def test_tier_runs_after_engine_version_in_catalogue():
    from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES

    ids = [rule.id for rule in AWS_GCP_RULES]
    assert ids.index("DB-ENGINE-VERSION") < ids.index("DB-TIER")


def test_tier_detects_every_instance():
    doc = _doc(_instance())
    diffs = DbTier().detect(doc)
    assert len(diffs) == 1
    assert diffs[0].key == "DB-TIER@database_instance:db1"


def test_tier_burstable_micro_defaults_to_shared_core_f1_micro():
    instance = _instance(class_name="db.t3.micro", vcpus=2, memory_mib=1024)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]
    assert diff.context["shared_core_tier"] == "db-f1-micro"

    result = rule.apply(doc, diff, "shared_core")

    assert result.get(instance.id).target_hints == {"tier": "db-f1-micro"}
    assert doc.get(instance.id).target_hints == {}


def test_tier_custom_tier_for_m5_large():
    instance = _instance(class_name="db.m5.large", vcpus=2, memory_mib=8192)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "nearest_custom_tier")

    assert result.get(instance.id).target_hints == {"tier": "db-custom-2-8192"}


def test_tier_memory_below_band_rounds_up_to_floor():
    instance = _instance(class_name="db.m5.large", vcpus=2, memory_mib=1024)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "nearest_custom_tier")

    # floor = 2 * 922 = 1844, rounded up to a 256 multiple = 2048
    assert result.get(instance.id).target_hints == {"tier": "db-custom-2-2048"}


def test_tier_unknown_sizing_is_manual_only():
    instance = _instance(class_name="db.unknown.type", vcpus=None, memory_mib=None)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]
    assert diff.context["sizing_known"] is False

    result = rule.apply(doc, diff, "manual")
    assert result.get(instance.id).excluded == "DB-TIER: instance sizing requires manual review"

    with pytest.raises(ValueError):
        rule.apply(doc, diff, "nearest_custom_tier")
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "shared_core")


# ---- DB-PRIVATE-IP -------------------------------------------------------------


def test_private_ip_detects_only_non_public_instances():
    private = _instance(id_="database_instance:priv", publicly_accessible=False)
    public = _instance(id_="database_instance:pub", publicly_accessible=True)
    doc = _doc(_network(), private, public)

    diffs = DbPrivateIp().detect(doc)

    assert [d.subjects[0] for d in diffs] == ["database_instance:priv"]


def test_private_ip_detects_nothing_when_all_public():
    doc = _doc(_network(), _instance(publicly_accessible=True))
    assert DbPrivateIp().detect(doc) == []


def test_private_ip_default_range_avoids_scanned_vpc_cidr():
    net = _network(cidr="10.64.0.0/16")
    instance = _instance(network=net.id, publicly_accessible=False)
    doc = _doc(net, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]

    params = rule.default_params(diff, "psa_auto_range")

    import ipaddress

    candidate = ipaddress.ip_network(f"{params['range']}/{params['prefix_length']}")
    assert not candidate.overlaps(ipaddress.ip_network("10.64.0.0/16"))


def test_private_ip_two_instances_in_one_network_share_a_range():
    net = _network()
    i1 = _instance(id_="database_instance:a", network=net.id, publicly_accessible=False)
    i2 = _instance(id_="database_instance:b", network=net.id, publicly_accessible=False)
    doc = _doc(net, i1, i2)
    rule = DbPrivateIp()
    diffs = {d.subjects[0]: d for d in rule.detect(doc)}

    params_a = rule.default_params(diffs["database_instance:a"], "psa_auto_range")
    params_b = rule.default_params(diffs["database_instance:b"], "psa_auto_range")

    assert params_a == params_b


def test_private_ip_two_networks_get_different_ranges():
    net_a = _network(id_="network:a", cidr="10.1.0.0/16")
    net_b = _network(id_="network:b", cidr="10.2.0.0/16")
    i1 = _instance(id_="database_instance:a", network=net_a.id, publicly_accessible=False)
    i2 = _instance(id_="database_instance:b", network=net_b.id, publicly_accessible=False)
    doc = _doc(net_a, net_b, i1, i2)
    rule = DbPrivateIp()
    diffs = {d.subjects[0]: d for d in rule.detect(doc)}

    params_a = rule.default_params(diffs["database_instance:a"], "psa_auto_range")
    params_b = rule.default_params(diffs["database_instance:b"], "psa_auto_range")

    assert params_a["range"] != params_b["range"]


def test_private_ip_validate_params_rejects_overlapping_range_naming_vpc():
    net = _network(id_="network:main", cidr="10.80.0.0/16")
    instance = _instance(network=net.id, publicly_accessible=False)
    doc = _doc(net, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]

    with pytest.raises(ValueError, match="network:main"):
        rule.validate_params(diff, "psa_auto_range", {"range": "10.80.5.0", "prefix_length": 24})


def test_private_ip_validate_params_rejects_bad_prefix_length():
    net = _network()
    instance = _instance(network=net.id, publicly_accessible=False)
    doc = _doc(net, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]

    with pytest.raises(ValueError):
        rule.validate_params(diff, "psa_auto_range", {"range": "10.90.0.0", "prefix_length": 25})


def test_private_ip_psa_auto_range_apply_sets_hints():
    net = _network()
    instance = _instance(network=net.id, publicly_accessible=False)
    doc = _doc(net, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]
    params = rule.default_params(diff, "psa_auto_range")

    result = rule.apply(doc, diff, "psa_auto_range", params)

    hints = result.get(instance.id).target_hints
    assert hints["private_network"] == net.id
    assert hints["psa"] == params
    assert doc.get(instance.id).target_hints == {}


def test_private_ip_public_ip_authorized_networks_apply():
    net = _network()
    policy = _policy(
        "firewall_policy:sg1",
        network=net.id,
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["5432"],
                peer=CidrPeer(cidr="1.2.3.0/24"),
            ),
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["22"], peer=CidrPeer(cidr="9.9.9.0/24")
            ),
        ],
    )
    instance = _instance(
        network=net.id,
        publicly_accessible=False,
        port=5432,
        firewall_policies=[policy.id],
    )
    doc = _doc(net, policy, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "public_ip_authorized_networks")

    hints = result.get(instance.id).target_hints
    assert hints["public_ip"] is True
    assert hints["authorized_networks"] == ["1.2.3.0/24"]


def test_private_ip_manual_excludes():
    net = _network()
    instance = _instance(network=net.id, publicly_accessible=False)
    doc = _doc(net, instance)
    rule = DbPrivateIp()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded
        == "DB-PRIVATE-IP: private connectivity requires manual review"
    )


# ---- DB-PUBLIC-ACCESS -----------------------------------------------------------


def test_public_access_detects_only_public_instances():
    private = _instance(id_="database_instance:priv", publicly_accessible=False)
    public = _instance(id_="database_instance:pub", publicly_accessible=True)
    doc = _doc(_network(), private, public)

    diffs = DbPublicAccess().detect(doc)

    assert [d.subjects[0] for d in diffs] == ["database_instance:pub"]


def test_public_access_detects_nothing_when_all_private():
    doc = _doc(_network(), _instance(publicly_accessible=False))
    assert DbPublicAccess().detect(doc) == []


def test_public_access_authorized_networks_from_matching_port_only():
    net = _network()
    policy = _policy(
        "firewall_policy:sg1",
        network=net.id,
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["5432"],
                peer=CidrPeer(cidr="1.2.3.0/24"),
            ),
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["22"], peer=CidrPeer(cidr="9.9.9.0/24")
            ),
        ],
    )
    instance = _instance(
        network=net.id,
        publicly_accessible=True,
        port=5432,
        firewall_policies=[policy.id],
    )
    doc = _doc(net, policy, instance)
    rule = DbPublicAccess()
    diff = rule.detect(doc)[0]
    assert diff.context["authorized_networks"] == ["1.2.3.0/24"]
    assert diff.context["open_to_world"] is False

    result = rule.apply(doc, diff, "public_ip_authorized_networks")

    hints = result.get(instance.id).target_hints
    assert hints["public_ip"] is True
    assert hints["authorized_networks"] == ["1.2.3.0/24"]


def test_public_access_open_to_world_sets_context_flag():
    net = _network()
    policy = _policy(
        "firewall_policy:sg1",
        network=net.id,
        rules=[
            FirewallRule(
                direction="ingress", protocol="tcp", ports=["5432"], peer=CidrPeer(cidr="0.0.0.0/0")
            ),
        ],
    )
    instance = _instance(
        network=net.id, publicly_accessible=True, port=5432, firewall_policies=[policy.id]
    )
    doc = _doc(net, policy, instance)
    rule = DbPublicAccess()
    diff = rule.detect(doc)[0]

    assert diff.context["open_to_world"] is True


def test_public_access_private_only_apply_uses_psa_params():
    net = _network()
    instance = _instance(network=net.id, publicly_accessible=True)
    doc = _doc(net, instance)
    rule = DbPublicAccess()
    diff = rule.detect(doc)[0]
    params = rule.default_params(diff, "private_only")

    result = rule.apply(doc, diff, "private_only", params)

    hints = result.get(instance.id).target_hints
    assert hints["private_network"] == net.id
    assert hints["psa"] == params
    assert "public_ip" not in hints
    assert doc.get(instance.id).target_hints == {}


def test_public_access_manual_excludes_and_input_unchanged():
    net = _network()
    instance = _instance(network=net.id, publicly_accessible=True)
    doc = _doc(net, instance)
    rule = DbPublicAccess()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded == "DB-PUBLIC-ACCESS: public access requires manual review"
    )
    assert doc.get(instance.id).excluded is None


# ---- DB-SG-FIREWALL --------------------------------------------------------------


def test_sg_firewall_fires_for_db_only_policy():
    net = _network()
    policy = _policy("firewall_policy:sg1", network=net.id, applies_to=["database_instance:db1"])
    doc = _doc(net, policy)

    diffs = DbSgFirewall().detect(doc)

    assert len(diffs) == 1
    assert diffs[0].subjects == [policy.id]


def test_sg_firewall_does_not_fire_when_attached_to_something_else():
    net = _network()
    policy = _policy(
        "firewall_policy:sg1",
        network=net.id,
        applies_to=["database_instance:db1", "instance:ec2-1"],
    )
    doc = _doc(net, policy)

    assert DbSgFirewall().detect(doc) == []


def test_sg_firewall_does_not_fire_when_applies_to_empty():
    net = _network()
    policy = _policy("firewall_policy:sg1", network=net.id, applies_to=[])
    doc = _doc(net, policy)

    assert DbSgFirewall().detect(doc) == []


def test_sg_firewall_runs_after_public_access_so_derivation_still_worked():
    from easymigrate.differences.rules.aws_gcp import AWS_GCP_RULES

    ids = [rule.id for rule in AWS_GCP_RULES]
    assert ids.index("DB-PRIVATE-IP") < ids.index("DB-SG-FIREWALL")
    assert ids.index("DB-PUBLIC-ACCESS") < ids.index("DB-SG-FIREWALL")

    net = _network()
    policy = _policy(
        "firewall_policy:sg1",
        network=net.id,
        rules=[
            FirewallRule(
                direction="ingress",
                protocol="tcp",
                ports=["5432"],
                peer=CidrPeer(cidr="1.2.3.0/24"),
            )
        ],
        applies_to=["database_instance:db1"],
    )
    instance = _instance(
        id_="database_instance:db1",
        network=net.id,
        publicly_accessible=True,
        port=5432,
        firewall_policies=[policy.id],
    )
    doc = _doc(net, policy, instance)

    public_rule = DbPublicAccess()
    diff = public_rule.detect(doc)[0]
    after_public = public_rule.apply(doc, diff, "public_ip_authorized_networks")
    assert after_public.get(instance.id).target_hints["authorized_networks"] == ["1.2.3.0/24"]

    sg_rule = DbSgFirewall()
    sg_diff = sg_rule.detect(after_public)[0]
    after_sg = sg_rule.apply(after_public, sg_diff, "drop_with_note")
    assert (
        after_sg.get(policy.id).excluded
        == "DB-SG-FIREWALL: VPC firewall rules do not govern Cloud SQL"
    )


def test_sg_firewall_apply_excludes_with_reason():
    net = _network()
    policy = _policy("firewall_policy:sg1", network=net.id, applies_to=["database_instance:db1"])
    doc = _doc(net, policy)
    rule = DbSgFirewall()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "drop_with_note")

    assert (
        result.get(policy.id).excluded
        == "DB-SG-FIREWALL: VPC firewall rules do not govern Cloud SQL"
    )
    assert doc.get(policy.id).excluded is None


# ---- DB-MULTI-AZ ------------------------------------------------------------------


def test_multi_az_detects_high_availability():
    instance = _instance(high_availability=True)
    doc = _doc(instance)
    assert len(DbMultiAz().detect(doc)) == 1


def test_multi_az_detects_nothing_for_single_az():
    doc = _doc(_instance(high_availability=False, multi_az_cluster=False))
    assert DbMultiAz().detect(doc) == []


def test_multi_az_regional_ha_and_zonal():
    instance = _instance(high_availability=True)
    doc = _doc(instance)
    rule = DbMultiAz()
    diff = rule.detect(doc)[0]

    regional = rule.apply(doc, diff, "regional_ha")
    assert regional.get(instance.id).target_hints == {"availability_type": "REGIONAL"}

    zonal = rule.apply(doc, diff, "zonal")
    assert zonal.get(instance.id).target_hints == {"availability_type": "ZONAL"}
    assert doc.get(instance.id).target_hints == {}


def test_multi_az_cluster_is_manual_only():
    instance = _instance(high_availability=True, multi_az_cluster=True)
    doc = _doc(instance)
    rule = DbMultiAz()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")
    assert (
        result.get(instance.id).excluded
        == "DB-MULTI-AZ: multi-AZ DB clusters have no Cloud SQL equivalent"
    )

    with pytest.raises(ValueError):
        rule.apply(doc, diff, "regional_ha")
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "zonal")


# ---- DB-STORAGE-AUTORESIZE ---------------------------------------------------------


def test_storage_autoresize_detects_every_instance():
    doc = _doc(_instance())
    assert len(DbStorageAutoresize().detect(doc)) == 1


def test_storage_autoresize_match_source_on_when_max_size_set():
    instance = _instance(max_size_gib=100)
    doc = _doc(instance)
    rule = DbStorageAutoresize()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "match_source")

    assert result.get(instance.id).target_hints == {
        "disk_autoresize": True,
        "disk_autoresize_limit": 100,
    }


def test_storage_autoresize_match_source_off_when_max_size_unset():
    instance = _instance(max_size_gib=None)
    doc = _doc(instance)
    rule = DbStorageAutoresize()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "match_source")

    assert result.get(instance.id).target_hints == {"disk_autoresize": False}
    assert doc.get(instance.id).target_hints == {}


def test_storage_autoresize_enable_with_limit_uses_zero_when_unset():
    instance = _instance(max_size_gib=None)
    doc = _doc(instance)
    rule = DbStorageAutoresize()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "enable_with_limit")

    assert result.get(instance.id).target_hints == {
        "disk_autoresize": True,
        "disk_autoresize_limit": 0,
    }


# ---- DB-STORAGE-TYPE ------------------------------------------------------------


def test_storage_type_detects_io1():
    instance = _instance(storage_type="io1", iops=3000)
    doc = _doc(instance)
    assert len(DbStorageType().detect(doc)) == 1


def test_storage_type_detects_gp3_only_with_iops_or_throughput():
    plain_gp3 = _instance(storage_type="gp3")
    doc = _doc(plain_gp3)
    assert DbStorageType().detect(doc) == []

    provisioned_gp3 = _instance(storage_type="gp3", iops=4000)
    doc2 = _doc(provisioned_gp3)
    assert len(DbStorageType().detect(doc2)) == 1


def test_storage_type_detects_nothing_for_gp2():
    doc = _doc(_instance(storage_type="gp2"))
    assert DbStorageType().detect(doc) == []


def test_storage_type_pd_ssd_and_manual():
    instance = _instance(storage_type="io2", iops=5000, throughput_mibps=250)
    doc = _doc(instance)
    rule = DbStorageType()
    diff = rule.detect(doc)[0]
    assert diff.context == {"type": "io2", "iops": 5000, "throughput_mibps": 250}

    result = rule.apply(doc, diff, "pd_ssd")
    assert result.get(instance.id).target_hints == {"disk_type": "PD_SSD"}
    assert doc.get(instance.id).target_hints == {}

    excluded = rule.apply(doc, diff, "manual")
    assert (
        excluded.get(instance.id).excluded
        == "DB-STORAGE-TYPE: provisioned IOPS/throughput have no Cloud SQL equivalent"
    )


# ---- DB-STORAGE-MINIMUM ----------------------------------------------------------


def test_storage_minimum_detects_undersized_disk():
    doc = _doc(_instance(size_gib=5))
    diffs = DbStorageMinimum().detect(doc)
    assert len(diffs) == 1
    assert diffs[0].context == {"size_gib": 5}


def test_storage_minimum_detects_nothing_at_or_above_minimum():
    assert DbStorageMinimum().detect(_doc(_instance(size_gib=10))) == []
    assert DbStorageMinimum().detect(_doc(_instance(size_gib=20))) == []


def test_storage_minimum_grows_to_minimum():
    instance = _instance(size_gib=5)
    doc = _doc(instance)
    rule = DbStorageMinimum()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "grow_to_minimum")

    assert result.get(instance.id).target_hints == {"disk_size": 10}
    assert doc.get(instance.id).target_hints == {}


# ---- DB-PARAM-FLAGS --------------------------------------------------------------


def test_param_flags_detects_instance_with_parameters():
    doc = _doc(_instance(parameters={"max_connections": "100"}))
    assert len(DbParamFlags().detect(doc)) == 1


def test_param_flags_detects_nothing_without_parameters():
    assert DbParamFlags().detect(_doc(_instance(parameters={}))) == []


def test_param_flags_keeps_supported_drops_unsupported_and_formula():
    instance = _instance(
        engine="postgres",
        parameters={
            "max_connections": "100",
            "shared_buffers": "{DBInstanceClassMemory/4}",
            "some_unsupported_flag": "value",
        },
    )
    doc = _doc(instance)
    rule = DbParamFlags()
    diff = rule.detect(doc)[0]
    assert diff.context["database_flags"] == {"max_connections": "100"}
    assert diff.context["dropped_parameters"] == {
        "shared_buffers": "{DBInstanceClassMemory/4}",
        "some_unsupported_flag": "value",
    }

    result = rule.apply(doc, diff, "supported_flags_only")

    hints = result.get(instance.id).target_hints
    assert hints["database_flags"] == {"max_connections": "100"}
    assert hints["dropped_parameters"] == {
        "shared_buffers": "{DBInstanceClassMemory/4}",
        "some_unsupported_flag": "value",
    }
    assert doc.get(instance.id).target_hints == {}


def test_param_flags_merges_into_existing_database_flags_hint():
    instance = _instance(engine="mysql", parameters={"max_connections": "50"})
    doc = _doc(instance)
    rule = DbParamFlags()
    diff = rule.detect(doc)[0]

    # Simulate a hint already written by another rule (e.g. DB-IAM-AUTH) before this
    # rule applies; the merge must not lose it.
    pre_seeded = instance.model_copy(
        update={"target_hints": {"database_flags": {"cloudsql_iam_authentication": "on"}}}
    )
    seeded_doc = doc.model_copy(update={"objects": [pre_seeded]})

    result = rule.apply(seeded_doc, diff, "supported_flags_only")

    assert result.get(instance.id).target_hints["database_flags"] == {
        "cloudsql_iam_authentication": "on",
        "max_connections": "50",
    }


def test_param_flags_manual_excludes():
    instance = _instance(parameters={"max_connections": "100"})
    doc = _doc(instance)
    rule = DbParamFlags()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded == "DB-PARAM-FLAGS: parameter group requires manual review"
    )


# ---- defect regression: per-difference default (DB-ENGINE-VERSION / DB-TIER) -----


def test_postgres_9_6_resolves_end_to_end_with_accept_defaults():
    """`DifferenceRule.default` used to be a fixed class attribute, so a 9.6 instance
    (whose only valid strategies are `upgrade_to_oldest_supported`/`manual`) was handed
    the class default `nearest_major` by `merge_decisions`, and `resolve_ir` then raised.
    `default_strategy` must be consulted instead so `--accept-defaults` never crashes on
    a legitimate input."""
    instance = _instance(engine="postgres", engine_version="9.6.24")
    doc = _doc(instance)

    differences = detect(doc, AWS_GCP_RULES)
    decisions, dropped = merge_decisions(None, differences, AWS_GCP_RULES)
    assert dropped == []
    engine_decision = next(
        d for d in decisions.decisions if d.key == f"DB-ENGINE-VERSION@{instance.id}"
    )
    assert engine_decision.strategy == "upgrade_to_oldest_supported"

    resolved = resolve_ir(doc, differences, decisions, AWS_GCP_RULES)

    assert resolved.superseded == []
    assert resolved.document.get(instance.id).target_hints["database_version"] == "POSTGRES_13"


def test_tier_unknown_sizing_defaults_to_manual():
    instance = _instance(class_name="db.unknown.type", vcpus=None, memory_mib=None)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]

    assert rule.default_strategy(diff) == "manual"


def test_tier_burstable_defaults_to_shared_core():
    instance = _instance(class_name="db.t3.micro", vcpus=2, memory_mib=1024)
    doc = _doc(instance)
    rule = DbTier()
    diff = rule.detect(doc)[0]

    assert rule.default_strategy(diff) == "shared_core"


# ---- DB-BACKUP-WINDOW ---------------------------------------------------------


def test_backup_window_detects_every_instance():
    instance = _instance()
    doc = _doc(instance)
    diffs = DbBackupWindow().detect(doc)
    assert len(diffs) == 1
    assert diffs[0].key == f"DB-BACKUP-WINDOW@{instance.id}"


def test_backup_window_nearest_equivalent_maps_start_time_and_retention():
    instance = _instance(backup_retention_days=35, backup_window="03:00-04:00")
    doc = _doc(instance)
    rule = DbBackupWindow()
    diff = rule.detect(doc)[0]
    assert diff.context["backups_clamped"] is False

    result = rule.apply(doc, diff, "nearest_equivalent")

    assert result.get(instance.id).target_hints == {
        "backup": {"enabled": True, "start_time": "03:00", "retained_backups": 35}
    }
    assert doc.get(instance.id).target_hints == {}


def test_backup_window_retention_above_365_is_clamped():
    instance = _instance(backup_retention_days=400, backup_window="03:00-04:00")
    doc = _doc(instance)
    rule = DbBackupWindow()
    diff = rule.detect(doc)[0]
    assert diff.context["backups_clamped"] is True

    result = rule.apply(doc, diff, "nearest_equivalent")

    assert result.get(instance.id).target_hints["backup"]["retained_backups"] == 365


def test_backup_window_retention_zero_offers_the_other_two_strategies():
    instance = _instance(backup_retention_days=0, backup_window="03:00-04:00")
    doc = _doc(instance)
    rule = DbBackupWindow()
    diff = rule.detect(doc)[0]
    assert rule.default_strategy(diff) == "no_backups"

    no_backups = rule.apply(doc, diff, "no_backups")
    assert no_backups.get(instance.id).target_hints == {"backup": {"enabled": False}}

    default_backups = rule.apply(doc, diff, "enable_default_backups")
    assert default_backups.get(instance.id).target_hints == {
        "backup": {"enabled": True, "start_time": "03:00", "retained_backups": 7}
    }

    with pytest.raises(ValueError):
        rule.apply(doc, diff, "nearest_equivalent")
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "manual")


def test_backup_window_manual_excludes():
    instance = _instance(backup_retention_days=10, backup_window="03:00-04:00")
    doc = _doc(instance)
    rule = DbBackupWindow()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded
        == "DB-BACKUP-WINDOW: backup schedule requires manual review"
    )
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "no_backups")


# ---- DB-PITR --------------------------------------------------------------------


def test_pitr_detects_positive_retention_only():
    positive = _instance(id_="database_instance:with-retention", backup_retention_days=10)
    negative = _instance(id_="database_instance:no-retention", backup_retention_days=0)
    doc = _doc(positive, negative)

    diffs = DbPitr().detect(doc)

    assert [d.subjects[0] for d in diffs] == [positive.id]


def test_pitr_retention_35_clamps_log_retention_to_7():
    instance = _instance(backup_retention_days=35)
    doc = _doc(instance)
    rule = DbPitr()
    diff = rule.detect(doc)[0]
    assert diff.context["log_retention_clamped"] is True

    result = rule.apply(doc, diff, "enable")

    assert result.get(instance.id).target_hints == {
        "pitr": {"enabled": True, "log_retention_days": 7}
    }
    assert doc.get(instance.id).target_hints == {}


def test_pitr_disable():
    instance = _instance(backup_retention_days=10)
    doc = _doc(instance)
    rule = DbPitr()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "disable")

    assert result.get(instance.id).target_hints == {"pitr": {"enabled": False}}


# ---- DB-MAINTENANCE-WINDOW -------------------------------------------------------


def test_maintenance_window_detects_positive_and_negative():
    with_window = _instance(
        id_="database_instance:with-window", maintenance_window="sun:05:30-sun:06:30"
    )
    without_window = _instance(id_="database_instance:no-window", maintenance_window=None)
    doc = _doc(with_window, without_window)

    diffs = DbMaintenanceWindow().detect(doc)

    assert [d.subjects[0] for d in diffs] == [with_window.id]


def test_maintenance_window_sun_0530_maps_to_day_7_hour_5():
    instance = _instance(maintenance_window="sun:05:30-sun:06:30")
    doc = _doc(instance)
    rule = DbMaintenanceWindow()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "nearest_hour")

    assert result.get(instance.id).target_hints == {"maintenance_window": {"day": 7, "hour": 5}}
    assert doc.get(instance.id).target_hints == {}


def test_maintenance_window_manual_makes_no_change():
    instance = _instance(maintenance_window="mon:00:00-mon:01:00")
    doc = _doc(instance)
    rule = DbMaintenanceWindow()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None


# ---- DB-PORT --------------------------------------------------------------------


def test_port_detects_positive_and_negative():
    custom = _instance(id_="database_instance:custom-port", port=5433)
    default = _instance(id_="database_instance:default-port", port=5432)
    doc = _doc(custom, default)

    diffs = DbPort().detect(doc)

    assert [d.subjects[0] for d in diffs] == [custom.id]
    assert diffs[0].context == {"source_port": 5433, "default_port": 5432}


def test_port_default_port_sets_no_hints_and_leaves_object_otherwise_unchanged():
    instance = _instance(port=5433)
    doc = _doc(instance)
    rule = DbPort()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "default_port")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None


def test_port_manual_excludes():
    instance = _instance(port=5433)
    doc = _doc(instance)
    rule = DbPort()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get(instance.id).excluded == "DB-PORT: custom port requires manual review"
    assert doc.get(instance.id).excluded is None


# ---- DB-KMS-CMEK ------------------------------------------------------------------


def test_kms_cmek_detects_positive_and_negative():
    encrypted_cmek = _instance(
        id_="database_instance:cmek", encrypted=True, kms_key="arn:aws:kms:key/1"
    )
    encrypted_default = _instance(id_="database_instance:default-key", encrypted=True, kms_key=None)
    unencrypted = _instance(id_="database_instance:plain", encrypted=False)
    doc = _doc(encrypted_cmek, encrypted_default, unencrypted)

    diffs = DbKmsCmek().detect(doc)

    assert [d.subjects[0] for d in diffs] == [encrypted_cmek.id]
    assert diffs[0].context == {"kms_key": "arn:aws:kms:key/1"}


def test_kms_cmek_google_managed_makes_no_change():
    instance = _instance(encrypted=True, kms_key="arn:aws:kms:key/1")
    doc = _doc(instance)
    rule = DbKmsCmek()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "google_managed")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None


def test_kms_cmek_manual_excludes():
    instance = _instance(encrypted=True, kms_key="arn:aws:kms:key/1")
    doc = _doc(instance)
    rule = DbKmsCmek()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert (
        result.get(instance.id).excluded
        == "DB-KMS-CMEK: customer-managed key requires manual review"
    )


# ---- DB-IAM-AUTH ------------------------------------------------------------------


def test_iam_auth_detects_positive_and_negative():
    enabled = _instance(id_="database_instance:iam", iam_auth=True)
    disabled = _instance(id_="database_instance:no-iam", iam_auth=False)
    doc = _doc(enabled, disabled)

    diffs = DbIamAuth().detect(doc)

    assert [d.subjects[0] for d in diffs] == [enabled.id]


def test_iam_auth_manual_makes_no_change():
    instance = _instance(iam_auth=True)
    doc = _doc(instance)
    rule = DbIamAuth()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "manual")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None


def test_iam_auth_merges_into_database_flags_written_before_it():
    """DB-PARAM-FLAGS (rule 10) runs before DB-IAM-AUTH in the catalogue and writes the
    same `database_flags` hint; DB-IAM-AUTH must merge into it, not replace it."""
    instance = _instance(engine="mysql", iam_auth=True)
    doc = _doc(instance)

    param_flags_first = instance.model_copy(
        update={"target_hints": {"database_flags": {"max_connections": "50"}}}
    )
    seeded_doc = doc.model_copy(update={"objects": [param_flags_first]})
    diff = DbIamAuth().detect(seeded_doc)[0]

    result = DbIamAuth().apply(seeded_doc, diff, "enable_flag")

    assert result.get(instance.id).target_hints["database_flags"] == {
        "max_connections": "50",
        "cloudsql_iam_authentication": "on",
    }


def test_iam_auth_flag_survives_param_flags_running_after_it():
    """The reverse arrival order: DB-IAM-AUTH writes its flag first, then DB-PARAM-FLAGS
    (rule 10) merges its own supported flags in; the IAM flag must not be lost."""
    instance = _instance(engine="mysql", iam_auth=True, parameters={"max_connections": "50"})
    doc = _doc(instance)

    iam_auth_first = instance.model_copy(
        update={"target_hints": {"database_flags": {"cloudsql_iam_authentication": "on"}}}
    )
    seeded_doc = doc.model_copy(update={"objects": [iam_auth_first]})
    diff = DbParamFlags().detect(seeded_doc)[0]

    result = DbParamFlags().apply(seeded_doc, diff, "supported_flags_only")

    assert result.get(instance.id).target_hints["database_flags"] == {
        "cloudsql_iam_authentication": "on",
        "max_connections": "50",
    }


# ---- DB-LOG-EXPORTS ---------------------------------------------------------------


def test_log_exports_detects_positive_and_negative():
    with_exports = _instance(id_="database_instance:logs", log_exports=["postgresql", "upgrade"])
    without_exports = _instance(id_="database_instance:no-logs", log_exports=[])
    doc = _doc(with_exports, without_exports)

    diffs = DbLogExports().detect(doc)

    assert [d.subjects[0] for d in diffs] == [with_exports.id]
    assert diffs[0].context == {"log_exports": ["postgresql", "upgrade"]}


def test_log_exports_note_only_makes_no_change():
    instance = _instance(log_exports=["postgresql"])
    doc = _doc(instance)
    rule = DbLogExports()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "note_only")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None
    assert doc.get(instance.id).target_hints == {}


# ---- DB-REPLICA -------------------------------------------------------------------


def test_replica_ir_id_primary_sets_master_instance_hint():
    primary = _instance(id_="database_instance:primary")
    replica = _instance(id_="database_instance:replica", role="read_replica", replica_of=primary.id)
    doc = _doc(primary, replica)
    rule = DbReplica()
    diffs = rule.detect(doc)
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.subjects == [replica.id, primary.id]
    assert rule.default_strategy(diff) == "cloud_sql_replica"

    result = rule.apply(doc, diff, "cloud_sql_replica")

    assert result.get(replica.id).target_hints == {"master_instance": primary.id}
    assert doc.get(replica.id).target_hints == {}


def test_replica_external_primary_is_manual_only():
    replica = _instance(
        id_="database_instance:replica", role="read_replica", replica_of="external:on-prem-db"
    )
    doc = _doc(replica)
    rule = DbReplica()
    diff = rule.detect(doc)[0]
    assert diff.subjects == [replica.id]
    assert rule.default_strategy(diff) == "manual"

    result = rule.apply(doc, diff, "manual")
    assert (
        result.get(replica.id).excluded
        == "DB-REPLICA: replica of an external primary requires manual review"
    )

    with pytest.raises(ValueError):
        rule.apply(doc, diff, "cloud_sql_replica")


def test_replica_not_detected_for_a_primary_instance():
    primary = _instance()
    doc = _doc(primary)
    assert DbReplica().detect(doc) == []


# ---- DB-USERS ---------------------------------------------------------------------


def test_users_detects_every_instance_and_records_master_username():
    instance = _instance(master_username="admin")
    doc = _doc(instance)

    diffs = DbUsers().detect(doc)

    assert len(diffs) == 1
    assert diffs[0].context == {"master_username": "admin"}


def test_users_root_user_only_makes_no_change():
    instance = _instance(master_username="admin")
    doc = _doc(instance)
    rule = DbUsers()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "root_user_only")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "manual")


# ---- DB-NAME-REUSE ------------------------------------------------------------------


def test_name_reuse_detects_every_instance():
    instance = _instance()
    doc = _doc(instance)

    diffs = DbNameReuse().detect(doc)

    assert len(diffs) == 1
    assert diffs[0].context == {"name": instance.name}


def test_name_reuse_keep_makes_no_change():
    instance = _instance()
    doc = _doc(instance)
    rule = DbNameReuse()
    diff = rule.detect(doc)[0]

    result = rule.apply(doc, diff, "keep")

    assert result.get(instance.id).target_hints == {}
    assert result.get(instance.id).excluded is None
    with pytest.raises(ValueError):
        rule.apply(doc, diff, "manual")
