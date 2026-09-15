# EasyMigrate Plan 3 of 4: Difference Engine and `resolve`

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.

**Goal:**
- `analyze` also detects every AWS→GCP difference and writes `differences.json`.
- `easymigrate resolve` lets the user choose a strategy per difference (safe defaults preselected) and saves `decisions.yaml`.
- `easymigrate.differences.engine.resolve_ir()` turns IR plus decisions into resolved IR, for plan 4's generator.

**Spec:** `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md` §4 (binding), §2.2, §6.2.

## Global Constraints
- `easymigrate.differences.*` imports only `easymigrate.ir.*`, the stdlib, `pydantic` and `yaml`. It never imports core, plugins or providers. `tests/test_dependency_rules.py` already enforces this.
- Rules are pure. `detect` and `apply` never mutate their input: they use `model_copy(deep=True)` and build new objects. After every `apply`, the engine re-validates the document with `IRDocument.model_validate_json(doc.model_dump_json())`.
- A rule that raises inside `detect` or `apply` → `DifferenceRuleError(rule_id, cause)`. It must never be swallowed (spec §6.2).
- Output order is deterministic. Differences are sorted by `key`, and rules apply in catalogue order.
- Decision keys are `"<RULE_ID>@<primary subject id>"`.
- The existing CLI conventions stay: exit codes 0/1/2; `EasyMigrateError`, `StageInputMissing`, `ValidationError` and JSON/YAML errors give a one-line error and exit 1.
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...` (uv may not be on PATH).

## Rulings (deviations from and additions to spec §4.4)
1. **`NET-ZONE-REGIONAL` is detected once per network**, not once per subnet. Its key is `NET-ZONE-REGIONAL@network:<id>`, and its subjects are the network plus its subnets. The reason: `merge_per_network` is a per-network choice, and per-subnet decisions could contradict each other. Update the spec §4.4 table and the §4.2 YAML example to match.
2. **New rule `NET-ICMP-TYPE`.** GCP firewall rules can't filter by ICMP type or code. Strategies: **`allow_all_icmp`** (risk medium: broader than the source) and `manual`.
3. **New rule `NET-PREFIX-LIST`.** GCP firewalls have no AWS managed prefix lists. The only strategy is **`manual`**, and those rules are omitted.
4. **`manual` strategies don't always mean `excluded`.** Each rule defines what `manual` generates, as specified in the catalogue below. Every manual strategy needs an acknowledgement.
5. **`NET-IPV6`'s impact text must mention** that a subnet public only through IPv6 is recorded as `isolated` (a plan 2 follow-up).

Add rulings 1–3 to spec §4.4 in the same commit as the catalogue.

## File Structure
```
src/easymigrate/differences/
├── __init__.py
├── model.py            # Strategy, DifferenceRule (ABC), Difference, Decision, DecisionSet, DifferenceRuleError
├── engine.py           # detect(), resolve_ir(), catalogue()
├── decisions.py        # merge_decisions(), readiness_problems(), to_yaml(), from_yaml(), fingerprint()
└── rules/
    ├── __init__.py
    └── aws_gcp/
        ├── __init__.py  # AWS_GCP_RULES: tuple[DifferenceRule, ...] in catalogue order
        └── network.py   # the 11 networking rules
src/easymigrate/core/pipeline.py   # analyze writes differences.json; new resolve()
src/easymigrate/core/project.py    # + write_text(path, text)
src/easymigrate/cli.py             # + resolve command
scripts/gen_differences_doc.py     # renders docs/differences.md from rule metadata
docs/differences.md                # generated
tests/differences/{test_model.py,test_engine.py,test_decisions.py,test_network_rules.py,test_docs.py}
tests/core/test_pipeline_resolve.py
tests/test_cli_resolve.py
```

## Interfaces

### `differences/model.py`
```python
Risk = Literal["none", "low", "medium", "high"]

class Strategy(StrictModel):          # StrictModel from easymigrate.ir.base
    id: str; title: str; effect: str; risk: Risk; risk_note: str | None = None; manual: bool = False

class Difference(StrictModel):
    key: str                 # f"{rule_id}@{subjects[0]}"
    rule_id: str
    subjects: list[str]      # IR ids; subjects[0] is primary
    context: dict[str, Any]  # JSON-safe values shown to the user

class DifferenceRule(ABC):
    id: ClassVar[str]; family: ClassVar[str]; title: ClassVar[str]; impact: ClassVar[str]
    strategies: ClassVar[tuple[Strategy, ...]]; default: ClassVar[str]
    source: ClassVar[str] = "aws"; target: ClassVar[str] = "gcp"
    def strategy(self, strategy_id: str) -> Strategy        # KeyError if unknown
    def difference(self, subjects: list[str], **context) -> Difference   # builds the key
    @abstractmethod def detect(self, ir: IRDocument) -> list[Difference]
    @abstractmethod def apply(self, ir: IRDocument, diff: Difference, strategy_id: str) -> IRDocument

class Decision(StrictModel):
    key: str; strategy: str; decided_by: Literal["default", "user"]; acknowledged: bool = False

class DecisionSet(StrictModel):
    schema_version: Literal["1"] = "1"
    differences_fingerprint: str
    decisions: list[Decision]          # sorted by key
    acknowledged_in_bulk: bool = False # set by --acknowledge-manual

class DifferenceRuleError(Exception):
    def __init__(self, rule_id: str, cause: BaseException)  # message: f"difference rule {rule_id} failed: {cause}"
```
`__init_subclass__` validation on concrete rules: `default` is one of the strategies, strategy ids are unique, and `id` matches `^[A-Z]+(-[A-Z0-9]+)+$`.

### `differences/engine.py`
- `catalogue() -> tuple[DifferenceRule, ...]`: returns `AWS_GCP_RULES`.
- `detect(ir, rules=None) -> list[Difference]`: sorted by key. Raises `ValueError` on a duplicate key and `DifferenceRuleError` when a rule fails.
- `resolve_ir(ir, differences, decisions: DecisionSet, rules=None) -> IRDocument`: applies rules in catalogue order, and each rule's differences in key order, using the decided strategy. After each apply it appends `ResolutionRef(key, strategy)` to every **surviving** subject object and re-validates. A decision missing for a difference → `ValueError`.

### `differences/decisions.py`
- `fingerprint(differences) -> str`: sha256 hex of the sorted keys joined by `\n`.
- `merge_decisions(existing: DecisionSet | None, differences, rules) -> tuple[DecisionSet, list[str]]`:
  - keeps existing decisions whose key still exists and whose strategy is still valid
  - adds defaults (`decided_by="default"`) for new keys
  - returns the dropped keys: orphaned decisions and invalid strategies
  - sets the fingerprint to that of the current differences
- `readiness_problems(decisions, differences, rules) -> list[str]`: plain sentences for:
  - a missing file or fingerprint mismatch ("differences changed since resolve; run `easymigrate resolve`")
  - an unknown strategy
  - an unacknowledged manual decision ("<key> uses a manual strategy and is not acknowledged")

  An empty list means the decisions are ready. Plan 4's `plan` calls this.
- `to_yaml(DecisionSet) -> str` and `from_yaml(text) -> DecisionSet`: `from_yaml` raises `ValueError` for invalid YAML and `ValidationError` for the wrong shape.

## Networking rule catalogue (`rules/aws_gcp/network.py`), in this order

The strategy marked **bold** is the default. Contexts use JSON-safe values only.

| # | Rule | Detects (one difference per…) | Strategies and what `apply` does |
|---|---|---|---|
| 1 | `NET-ZONE-REGIONAL` | network with ≥1 subnet; subjects = network, then its subnets sorted; context `{network, zones, subnet_count}` | **`keep_1to1`** (low): no change. `merge_per_network` (high): replace the network's subnets with one `subnet:<network-slug>-regional`, where `cidr` = the smallest IPv4 supernet containing all of them, `zone="regional"`, `internet` = most exposed of the originals (public > private_nat > isolated), `map_public_ip` = any. Rewrite every `subnet:` reference (Nat.serves_subnets, NetworkAcl.subnets, CustomRoute.subnets) to the new id, de-duplicated. |
| 2 | `NET-ISOLATION-VM-LEVEL` | `isolated` subnet in a network that also has a `public` subnet; subject = subnet | **`no_external_ip`** (medium): `target_hints["no_external_ip"]=True` on the subnet. `manual` (manual): no change. |
| 3 | `NET-EGRESS-TAG` | firewall_policy with ≥1 egress rule whose peer is `PolicyPeer`; subject = policy, then the peer policies | **`peer_attached_ranges`** (medium): `target_hints["egress_ranges"] = {peer_policy_id: peer network's cidr_blocks}`, because `applies_to` is empty in this spec. `manual` (manual): remove those egress rules; `target_hints["omitted_rules"] += n`. |
| 4 | `NET-SG-XVPC-REF` | policy with ≥1 `ExternalPolicyPeer` rule; subject = policy | **`manual`** (manual): remove those rules; `omitted_rules += n`; context lists group ids. |
| 5 | `NET-PREFIX-LIST` | policy with ≥1 `PrefixListPeer` rule; subject = policy | **`manual`** (manual): remove those rules; `omitted_rules += n`. |
| 6 | `NET-ICMP-TYPE` | policy with ≥1 rule having `icmp_type` set; subject = policy | **`allow_all_icmp`** (medium): clear `icmp_type`/`icmp_code` on those rules, then de-duplicate identical rules. `manual` (manual): remove those rules; `omitted_rules += n`. |
| 7 | `NET-NACL` | each `network_acl`; subject = acl | **`manual`** (manual): `excluded = "NET-NACL: stateless subnet ACLs have no GCP equivalent"`. |
| 8 | `NET-PEERING` | `custom_route` with `target_type == "peering"`; subject = route | **`manual`** (manual): `excluded = "NET-PEERING: recreate VPC peering after migrating"`. |
| 9 | `NET-CUSTOM-ROUTE` | any other `custom_route`; subject = route | **`manual`** (manual): `excluded = "NET-CUSTOM-ROUTE: <target_type> target <target_id> is not migrated"`. |
| 10 | `NET-IPV6` | network with IPv6 CIDRs; subjects = network, then subnets with `ipv6_cidr` | **`ipv4_only`** (medium): clear `ipv6_cidr_blocks` and `ipv6_cidr`. `manual` (manual): the same change, plus acknowledgement required. |
| 11 | `NET-CIDR-OVERLAP` | each pair of networks with overlapping IPv4 CIDRs; subjects = the two network ids sorted | **`keep`** (none): no change. |

Omitted rules must never leave a policy referencing removed peers: removing rules only shrinks the list. Rules 4–6 can hit the same policy; each rule counts its own `omitted_rules`, adding to any existing value.

## Pipeline and CLI
- **`Project.write_text(path, text)`:** a public atomic write with private mode.
- **`pipeline.analyze`:** after writing `ir.json`, run `detect(doc)` and write `differences.json` as `{"differences": [...dumps], "rules": {rule_id: {"title", "impact", "strategies": [...], "default"}}}`, holding only the rules that fired. `analyze` returns the doc unchanged; add `pipeline.load_differences(project)`.
- **`pipeline.resolve(project, *, choose=None, accept_defaults=False, acknowledge_manual=False) -> ResolveSummary`:**
  - Reads `ir.json` (a missing file raises `StageInputMissing(..., "analyze")`) and `differences.json`, plus existing `decisions.yaml` if present.
  - Merges decisions.
  - If `choose` is given and `accept_defaults` is False, it calls `choose(view) -> tuple[str, bool]` once per difference in key order and records `decided_by="user"` when the choice differs from the stored one. `view` is `{difference, rule metadata, current strategy}`, and the returned pair is `(strategy_id, acknowledged)`.
  - With `acknowledge_manual`, it acknowledges every manual decision and sets `acknowledged_in_bulk=True`.
  - It **always saves** `decisions.yaml`.
  - Returns `ResolveSummary(decisions: DecisionSet, dropped: list[str], problems: list[str])`, where `problems = readiness_problems(...)`.
- **`easymigrate resolve [--accept-defaults [--acknowledge-manual]]`:**
  - `--acknowledge-manual` without `--accept-defaults` → exit 1.
  - **Interactive** mode prints, per difference:
    - the rule title and key
    - its subjects, the impact and the context
    - numbered strategies with effect, risk and a `(default)` marker

    Enter keeps the current choice. Choosing a manual strategy asks `I will handle this myself [y/N]`; `N` re-asks the choice.
  - It prints any dropped keys, then a summary table (key, strategy, decided_by, acknowledged).
  - If `problems` is non-empty, it prints them and exits **2**. Otherwise it prints `Next: run `easymigrate plan`.` and exits 0.
  - The interactive prompt must go through an injectable function, so tests can drive it with `CliRunner(input=...)`.

## Docs
- `scripts/gen_differences_doc.py` renders `docs/differences.md` from `catalogue()`: for each rule, its title, id, impact and a strategy table (id, title, effect, risk, manual, default).
- `tests/differences/test_docs.py` fails if the committed file differs from the rendered output, with a message telling you to run the script. CI already runs pytest, so no workflow change is needed.

## Required test cases (minimum)
- **model:**
  - an invalid default, duplicate strategy ids or a bad rule id raise at class definition
  - `difference()` builds `RULE@subject`
- **Per rule (11×):**
  - detect positive, and detect negative (nothing fires)
  - apply for every strategy, checking the exact resulting objects and hints
  - the input IR is unchanged after `apply`
  - use small hand-built IR, with the builders local to the test file
- **zone merge:** 10.0.1.0/24 + 10.0.2.0/24 → 10.0.0.0/22, a public+isolated mix → public, and references rewritten in a NAT, an ACL and a custom route
- **engine:**
  - detect sorted, and a duplicate key raises
  - a failing rule becomes `DifferenceRuleError` with its id, in both detect and apply
  - `resolve_ir` adds `ResolutionRef`s, skips excluded or removed subjects without error, a missing decision raises, and the result validates
- **decisions:**
  - defaults are filled in
  - user decisions are kept across re-merge
  - orphaned and invalid-strategy keys are dropped and reported
  - fingerprint mismatch, unacknowledged manual and unknown strategy each yield a readiness problem
  - YAML round-trip, and invalid YAML raises `ValueError`
- **pipeline:**
  - analyze writes `differences.json` containing only the rules that fired
  - resolve before analyze → `StageInputMissing`
  - a choose callback records user decisions
  - `acknowledge_manual` sets bulk acknowledgement
  - re-running resolve keeps choices
- **CLI:**
  - `--accept-defaults` with a manual difference → exit 2 listing it
  - adding `--acknowledge-manual` → exit 0
  - interactive: choose strategy 2 through input, then Enter for the rest → decisions saved with `decided_by=user`
  - picking manual and answering `n` re-asks
  - `--acknowledge-manual` alone → exit 1
- **golden:** for every plan 2 fixture case, detected difference keys are asserted as exact lists in `test_network_rules.py`, using `Snapshot.from_dir` plus `NetworkPlugin().normalize`.

## Tasks (each ends green: pytest, ruff format --check, ruff check, commit)
1. `model.py`, `engine.py` (with an empty catalogue), `decisions.py`, and `Project.write_text`, plus their tests.
2. Rules 1–6 (topology and firewall) with tests, and the spec §4.4 update.
3. Rules 7–11, the fixture-based detection tests, the docs generator, `docs/differences.md` and `test_docs.py`.
4. The pipeline (`analyze` writes differences, `resolve`), the CLI `resolve` command and their tests, plus a README usage line for `resolve`.
