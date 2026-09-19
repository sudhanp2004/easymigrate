"""Who is behind a firewall policy, derived from forward references (spec 4 §2.1).

This replaces `FirewallPolicy.applies_to`. A back-reference has to be written by every family
that attaches to a security group, is invisible to `IntentObject.references()` and so is never
validated, and silently under-reports when a family forgets. Deriving membership from the
references the objects already carry cannot drift from what it describes.
"""

from __future__ import annotations

from easymigrate.ir.base import IRDocument

_POLICY_PREFIX = "firewall_policy:"


def policy_members(ir: IRDocument) -> dict[str, set[str]]:
    """policy IR id -> ids of the objects whose forward references name it.

    Every `firewall_policy` in the document gets an entry, empty set included, so a caller can
    tell "guards nothing" apart from "not a policy in this document".
    """
    members: dict[str, set[str]] = {
        obj.id: set() for obj in ir.objects if obj.kind == "firewall_policy"
    }
    for obj in ir.objects:
        # A policy referencing a policy is a rule *peer*, not a resource attached to it.
        # `FirewallPolicy.references()` returns peers, so counting them would make an RDS-only
        # policy look as though something else were behind it.
        if obj.kind == "firewall_policy":
            continue
        for ref in obj.references():
            if ref.startswith(_POLICY_PREFIX) and ref in members:
                members[ref].add(obj.id)
    return members
