"""View models for the review step, derived from rule hooks rather than the
class-level `strategies`/`default`/static `Strategy.risk` (spec §5, plan 3 task 1).

Differences are built by running the real database normalizer + detect() over an
existing fixture snapshot (`tests/plugins/database/fixtures/`), so the cases exercised
here are real differences, not hand-built approximations.
"""

from __future__ import annotations

from pathlib import Path

from easymigrate.core.plugin import NormalizeContext, Snapshot
from easymigrate.differences.engine import catalogue, detect
from easymigrate.differences.model import Decision, Difference
from easymigrate.ir import IRDocument
from easymigrate.plugins.database.normalize import normalize as normalize_database
from easymigrate.plugins.network.normalize import normalize as normalize_network
from easymigrate.ui.views import decision_view, difference_view

FIXTURES = Path(__file__).parent.parent / "plugins" / "database" / "fixtures"


def _ir(fixture: str) -> IRDocument:
    snapshot = Snapshot.from_dir(FIXTURES / fixture / "snapshot")
    ctx = NormalizeContext(account_id="123456789012", region="ap-south-1")
    objects = []
    for normalize in (normalize_network, normalize_database):
        produced = normalize(snapshot, ctx)
        objects.extend(produced)
        ctx.objects.update({obj.id: obj for obj in produced})
    return IRDocument(
        source={"cloud": "aws", "account_id": "123456789012", "region": "ap-south-1"},
        objects=objects,
        findings=ctx.findings,
    )


def _diff(fixture: str, rule_id: str, subject_suffix: str = "") -> Difference:
    doc = _ir(fixture)
    differences = detect(doc)
    matches = [d for d in differences if d.rule_id == rule_id]
    if subject_suffix:
        matches = [d for d in matches if d.key.endswith(subject_suffix)]
    return matches[0]


def _rule(rule_id: str):
    return next(r for r in catalogue() if r.id == rule_id)


def test_engine_version_rule_exposes_only_the_upgrade_pair_for_a_96_instance():
    """The regression guard: `old_engine_version`'s legacy-db is Postgres 9.6, which
    Cloud SQL cannot run as-is, so only the upgrade pair may be offered — never the
    full class-level `strategies` tuple, which also carries `nearest_major`."""
    diff = _diff("old_engine_version", "DB-ENGINE-VERSION")
    rule = _rule("DB-ENGINE-VERSION")
    view = difference_view(diff, rule)

    ids = {s.id for s in view.strategies}
    assert ids == {"upgrade_to_oldest_supported", "manual"}
    assert "nearest_major" not in ids

    default_ids = [s.id for s in view.strategies if s.is_default]
    assert default_ids == [rule.default_strategy(diff)] == ["upgrade_to_oldest_supported"]


def test_public_access_risk_escalates_to_high_when_open_to_the_world():
    """`public_postgres`'s security group allows 0.0.0.0/0; `risk_for` must be consulted
    instead of the strategy's static (medium) risk."""
    diff = _diff("public_postgres", "DB-PUBLIC-ACCESS")
    assert diff.context["open_to_world"] is True
    rule = _rule("DB-PUBLIC-ACCESS")
    view = difference_view(diff, rule)

    strategy = next(s for s in view.strategies if s.id == "public_ip_authorized_networks")
    assert strategy.risk == "high"


def test_param_docs_present_for_a_rule_with_params_and_empty_for_one_without():
    diff_with_params = _diff("psa_range_conflict", "DB-PRIVATE-IP")
    rule_with_params = _rule("DB-PRIVATE-IP")
    view = difference_view(diff_with_params, rule_with_params)
    assert view.param_docs == {
        "range": "Network address of the allocated Private Services Access range.",
        "prefix_length": "Prefix length of the allocated range (16-24).",
    }

    diff_without_params = _diff("old_engine_version", "DB-TIER")
    rule_without_params = _rule("DB-TIER")
    view = difference_view(diff_without_params, rule_without_params)
    assert view.param_docs == {}


def test_needs_acknowledgement_true_until_acknowledged():
    diff = _diff("old_engine_version", "DB-PRIVATE-IP")
    rule = _rule("DB-PRIVATE-IP")

    unacknowledged = Decision(key=diff.key, strategy="manual", decided_by="user")
    assert decision_view(unacknowledged, rule).needs_acknowledgement is True

    acknowledged = Decision(key=diff.key, strategy="manual", decided_by="user", acknowledged=True)
    assert decision_view(acknowledged, rule).needs_acknowledgement is False
