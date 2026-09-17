# Spec 3, Plan 3 of 4: Differences, Decisions and the Report

> Lean plan: interfaces, rules and required test cases. There is no pre-written code; implementers write it once, with TDD.
> **For agentic workers:** use `superpowers:subagent-driven-development` to execute this plan task by task.

**Goal:** the HTTP surface for the review step — reading every difference with its
strategies, setting a decision with validated params, and reading the report. This is the
part of the product the UI exists for.

**Spec:** `docs/superpowers/specs/2026-09-17-local-ui-design.md` §5 (binding). Background on
the difference engine: `docs/superpowers/specs/2026-09-15-core-pipeline-networking-design.md`
§4 and `docs/ORIENTATION.md` §3.

## Global Constraints
- **Serve `rule.allowed_strategies(diff)`, never the class-level `rule.strategies`.** Five
  rules vary their strategy set per difference. Offering a strategy a difference disallows
  produces a crash later in `resolve_ir`; that exact bug has been introduced and caught
  twice in this codebase. This is the single most important line in this plan.
- Param validation goes through the owning rule's `validate_params`, and its error message
  is returned verbatim. The rules write errors that name the param and why it was rejected
  — for example naming the VPC a PSA range overlaps. Do not replace them with a generic
  "invalid parameter".
- The API exposes the engine's vocabulary unchanged: difference, strategy, risk, manual,
  decision, params, finding. No second naming scheme.
- Reads are stateless: each request loads from disk (`differences.json`, `decisions.yaml`).
- Commit messages end with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- Use `/home/sudhan/.local/bin/uv run ...`. Every task ends green: `uv run pytest`,
  `uv run ruff format --check .`, `uv run ruff check .`, then commit.

## File Structure
```
src/easymigrate/ui/routes/review.py   # differences, decisions, resolve, analyze, report
src/easymigrate/ui/views.py           # response models assembled from engine types
tests/ui/{test_review_routes.py,test_views.py}
```

## Interfaces

### `views.py`
```python
class StrategyView(StrictModel):
    id: str
    title: str
    effect: str
    risk: Literal["none", "low", "medium", "high"]
    risk_note: str | None
    manual: bool
    is_default: bool             # from rule.default_strategy(diff), not rule.default

class DifferenceView(StrictModel):
    key: str
    rule_id: str
    family: str                  # "network" | "database" — lets the UI group
    title: str
    impact: str
    subjects: list[str]
    context: dict[str, Any]      # the rule's own values: zones, cidrs, dropped params
    strategies: list[StrategyView]      # allowed_strategies(diff) only
    param_docs: dict[str, str]          # param name -> one-line meaning

class DecisionView(StrictModel):
    key: str
    strategy: str
    decided_by: Literal["default", "user"]
    acknowledged: bool
    params: dict[str, Any]
    needs_acknowledgement: bool  # chosen strategy is manual and not yet acknowledged
```
`risk` comes from `rule.risk_for(diff, strategy_id)`, not the strategy's static risk — one
rule escalates to `high` when a database would be open to the world, and a UI showing the
static value would present that as medium.

### `routes/review.py`

| Method | Path | Behaviour |
|---|---|---|
| `POST` | `/api/analyze` | Runs `pipeline.analyze` synchronously (it is fast and local). Returns object counts per kind and the findings. |
| `GET` | `/api/differences` | Every difference as a `DifferenceView`, sorted by key. `StageInputMissing` → 400 with the engine's message telling the user to run analyze. |
| `GET` | `/api/decisions` | Current decisions as `DecisionView`s, plus `blocked: list[str]` from `readiness_problems`. |
| `PUT` | `/api/decisions/{key}` | Body: `strategy`, optional `params`, optional `acknowledged`. Validates and persists. |
| `POST` | `/api/resolve` | Runs `pipeline.resolve(accept_defaults=True)` to normalise and persist, returning the same shape as `GET /api/decisions`. |
| `GET` | `/api/report` | The report as JSON — the same data `report.json` carries, via `core.report.build_report`. |

**`PUT /api/decisions/{key}` rules:**
- An unknown key → 404.
- A strategy not in `allowed_strategies(diff)` → 400 naming the strategies that *are*
  allowed. This is the server-side half of the constraint above: even if a stale page
  offers a bad strategy, the API refuses it.
- Params are validated by the rule; a `ValueError` becomes 400 with its message verbatim.
- Choosing a `manual` strategy without `acknowledged: true` is **allowed** — it persists
  with `needs_acknowledgement: true`, and `readiness_problems` then blocks `plan`. The UI
  needs to represent "chosen but not yet acknowledged"; refusing the write would make that
  impossible.
- Setting a decision marks it `decided_by: "user"`, and switching strategy refills params
  from the rule's defaults, matching `merge_decisions`' behaviour so the API and CLI agree.
- Writes go through the existing `decisions.to_yaml` / `project.write_text`, so a
  hand-edited file and an API-written one are byte-identical in shape.

## Required test cases (minimum)

**`test_views.py`:**
- a difference whose rule varies its strategy set (a Postgres 9.6 instance) exposes **only**
  the upgrade pair, and `is_default` marks the one `default_strategy(diff)` returns — the
  regression guard for the recurring bug
- a `DB-PUBLIC-ACCESS` difference whose authorized networks include `0.0.0.0/0` reports
  `risk: "high"` for `public_ip_authorized_networks`, proving `risk_for` is used rather than
  the static value
- `param_docs` is present for a rule that has params and empty for one that does not
- `needs_acknowledgement` is true for a manual strategy that is not acknowledged, false
  once it is

**`test_review_routes.py`** (build a project fixture by running the real pipeline over an
existing database fixture snapshot, so the differences are real):
- `GET /api/differences` before analyze → 400 carrying the engine's own message
- `GET /api/differences` after analyze → every key present, sorted, each with at least one
  strategy
- `PUT` a valid strategy → persisted, `decided_by` becomes `"user"`, and re-reading through
  `decisions.from_yaml` agrees
- `PUT` a strategy the difference disallows → 400 listing the allowed ones
- `PUT` `DB-PRIVATE-IP` with a range overlapping a scanned VPC → 400 carrying the rule's
  own message naming the overlapping VPC
- `PUT` `DB-PRIVATE-IP` with a valid range → persisted with those params
- switching strategy refills params from the new strategy's defaults
- `PUT` a manual strategy without acknowledgement → 200, `needs_acknowledgement: true`, and
  `GET /api/decisions` reports it in `blocked`
- acknowledging it clears the block
- `GET /api/report` returns the database section's facts for a fixture with an instance
- `GET /api/report` contains no password when one was supplied to a prior plan

## Tasks
1. `views.py` and `test_views.py` — the view models and their derivation from rules,
   including the `allowed_strategies` and `risk_for` guards.
2. `GET /api/differences`, `POST /api/analyze` and their tests.
3. `GET /api/decisions`, `PUT /api/decisions/{key}`, `POST /api/resolve` and their tests,
   including every validation case above.
4. `GET /api/report` and its tests.
