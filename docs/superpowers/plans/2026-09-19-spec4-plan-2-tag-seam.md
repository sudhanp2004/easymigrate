# Spec 4, Plan 2 of 5: The Tag Seam

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** `FirewallPolicy.applies_to` is deleted. Security-group membership is read from forward
references instead, which fixes a latent bug in `DB-SG-FIREWALL` and lets `NET-EGRESS-TAG` narrow
from whole-network CIDRs to the subnets that actually host the peer's instances.

**Spec:** `docs/superpowers/specs/2026-09-18-compute-ec2-design.md` §2.1, §2.1.1, §10 (binding).

## Why this plan is second and separate

It is the only plan in spec 4 that changes two already-shipped, live-tested families. Everything
else in the spec adds a new family alongside them. Doing it early means plans 3-5 build on a
settled seam; doing it in its own plan means a regression here is unambiguous about what caused it.

## Global Constraints
- `easymigrate.ir.*` imports only the stdlib and pydantic. `easymigrate.differences` imports only
  `easymigrate.ir`. `tests/test_dependency_rules.py` enforces this; it needs no change.
- **No behaviour change for any case that has no compute instances.** The existing network and
  database golden fixtures contain none, so every one of their `expected_ir.json` and
  `expected_*.tf` files must be byte-identical after this plan. If a golden file changes, the
  change is a bug until proven otherwise — do not regenerate goldens to make tests pass.
- Deterministic output: ranges sorted, no dependence on object order.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/ir/network.py                          # delete FirewallPolicy.applies_to
src/easymigrate/plugins/database/normalize.py          # stop mutating it
src/easymigrate/plugins/network/generate_gcp.py        # drop its FIELD_COVERAGE entry
src/easymigrate/differences/membership.py              # NEW: who references a policy
src/easymigrate/differences/rules/aws_gcp/database.py  # DB-SG-FIREWALL detection
src/easymigrate/differences/rules/aws_gcp/network.py   # NET-EGRESS-TAG apply + effect text
tests/differences/test_membership.py                   # NEW
```

## Interfaces

### `differences/membership.py` (new)
One helper, used by both rules, so "who is behind this policy" has a single definition.

```python
def policy_members(ir: IRDocument) -> dict[str, set[str]]:
    """policy IR id -> ids of the objects whose forward references name it.

    Replaces FirewallPolicy.applies_to (spec 4 §2.1). Derived from references, so it cannot
    drift from the objects it describes, and a family that forgets to maintain a back-reference
    cannot silently under-report membership.
    """
```

Built by walking every object's `references()` and keeping those that land on a `firewall_policy`.
Returns an entry for every `firewall_policy` in the document, empty set included, so callers can
distinguish "guards nothing" from "not a policy".

### `ir/network.py`
Delete `FirewallPolicy.applies_to`. Nothing replaces it. `FirewallPolicy.references()` is unchanged.

### `plugins/database/normalize.py`
Delete the two lines that append to `policy.applies_to`. `DatabaseInstance.network.firewall_policies`
already carries the same information and is already returned by `references()`, so nothing else
changes and no database golden file moves.

### `plugins/network/generate_gcp.py`
Remove the `"applies_to"` entry from the `firewall_policy` block of `FIELD_COVERAGE`. The field no
longer exists, so declaring coverage for it would be a lie.

### `differences/rules/aws_gcp/database.py` — `DB-SG-FIREWALL`
Detection today: `policy.applies_to` is non-empty and every entry starts with `database_instance:`.

Detection after this plan: using `policy_members(ir)`, a policy is RDS-only when its member set is
non-empty, at least one member is a `database_instance`, and **no** member is a `compute_instance`
or an `instance_template`.

This fixes the latent bug in spec §2.1: a security group guarding both an RDS instance and an EC2
instance used to read as RDS-only, because only the database plugin wrote `applies_to`. Dropping
that policy would remove the firewall rules protecting a migrated instance.

`strategies`, `risk`, `default`, `apply` and the target hint it sets are unchanged. The
`applies_to=` context value the difference carries keeps its name and now comes from the member
set, sorted.

### `differences/rules/aws_gcp/network.py` — `NET-EGRESS-TAG`
`detect` is unchanged. Only `apply`'s `peer_attached_ranges` branch changes, and the strategy's
`effect` text with it.

For each peer policy, the destination ranges become, in order:

1. The sorted, deduplicated `subnet.cidr` of every subnet named by a `compute_instance` whose
   `firewall_policies` include that peer — the subnets that actually host the peer's instances.
2. The peer policy's network `cidr_blocks`, exactly as today, when the peer has no compute
   instances behind it.

Instance IPs are not usable: GCP assigns internal addresses at apply time, so no CIDR exists to
write at plan time. Hosting subnets are the tightest statically-known answer (spec §10).

`effect` text becomes:

```
"destination becomes the CIDRs of the subnets hosting the peer's instances, or the peer's "
"network CIDR blocks when no migrated instance is behind it"
```

The `"applies_to is not yet known"` admission goes away. Risk stays `medium`: narrower than before,
still broader than a security group.

`InstanceTemplate` is deliberately **not** consulted here. A template names no subnet — the group
that uses it does — so resolving a template's hosting subnets would mean walking
`InstanceGroup.subnets`, which describes where instances *may* launch rather than where they are.
Case 2's fallback covers a peer whose only members are templates, which is the correct
conservative answer. Record this as a comment on the branch, not just in the plan.

## Required test cases (minimum)

**`tests/differences/test_membership.py`** (new)
- a policy referenced by one database instance → that instance's id
- a policy referenced by a compute instance and a database instance → both ids
- a policy referenced by nobody → present in the mapping with an empty set
- an object that references a non-policy id does not appear
- an instance template referencing a policy is included

**`tests/differences/rules/…` — `DB-SG-FIREWALL`**
- existing RDS-only cases still detect, with identical output (no golden churn)
- **a policy guarding both an RDS instance and a compute instance is NOT detected** — the bug fix
- a policy guarding only a compute instance is not detected
- a policy guarding nothing is not detected
- the difference's `applies_to` context equals the sorted member ids

**`tests/differences/rules/…` — `NET-EGRESS-TAG`**
- peer with compute instances in two subnets → destination is both subnet CIDRs, sorted
- peer with compute instances all in one subnet → that one CIDR, and it is narrower than the
  network CIDR (assert both, so a regression that widens it fails loudly)
- peer with no compute instances → unchanged network `cidr_blocks`, byte-identical to today
- peer whose only member is an `instance_template` → falls back to network CIDRs
- two peers, one with instances and one without → each resolved independently
- `manual` strategy unchanged

**Regression guard**
- every existing network and database fixture's `expected_ir.json` and `expected_*.tf` is
  unchanged; run the full suite without `--update-golden` and expect zero diffs
- `grep -r applies_to src/` returns nothing

## Tasks
1. `differences/membership.py` and `tests/differences/test_membership.py`.
2. `DB-SG-FIREWALL` detection rewritten onto `policy_members`, with the shared-policy bug-fix test.
3. Delete `FirewallPolicy.applies_to`, the database normalizer's mutation and the `FIELD_COVERAGE`
   entry. Full suite green with no golden regeneration.
4. `NET-EGRESS-TAG` narrowing, its `effect` text, and the branch comment explaining why templates
   are excluded.
