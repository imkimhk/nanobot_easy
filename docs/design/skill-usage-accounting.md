# Skill selection and application accounting

`skill_decision` used to increment `usage_count` for every choice. Choosing a wrong
skill and then a correct one therefore counted both as usage, even when the first
method was never applied.

## Accounting contract

Each skill trace now has a `phase`:

- `selected`: a routing choice, counted in `selection_count` only.
- `applied`: a recorded application with non-empty `application_evidence`, counted
  once in selection and usage. The evidence identifies an actual result, artifact
  or tool-result references. It is a usage observation, not a verified quality gate.
- `legacy`: an older/unclassified execution record. Its existing usage/outcome
  contribution is preserved rather than guessing whether it represented application.

`skill_decision` defaults to `selected`. It returns a `trace_id`; promote that same
skill/application with `phase=applied`, `application_evidence`, and the returned ID.
Promotion validates session, skill and composite wave inside the counter transaction.
An applied record can also be made directly alongside the actual answer, without
an extra selection-only model turn. A different selection/application gets a new ID.

Replaying the same ID replaces its contribution atomically, rather than adding
another use. A delayed selection replay cannot downgrade an applied observation.
Application replay preserves separately recorded gate/feedback outcomes. Distinct
IDs are retained as distinct observations; the store does not infer that two tasks
or subitems are duplicates from similar names, text, session or wave.

Only the existing gate/feedback mechanism changes success/failure counts. The model
cannot declare a verified success through the new tool parameters. `decision=none`
keeps a trace but has no per-skill contribution. System skills retain their existing
policy of excluding outcome counter updates.

CLI `skill stats`, registry payloads and WebUI detail expose selection counts next
to usage. The WebUI also shows the trace phase and application evidence. Selection
counts do not boost usage-based search ranking.

## Existing databases

The schema migration is additive, transactional and once-only. Old
`skill_decision:*` traces with neither gate result nor feedback are identifiable as
routing-only observations: they become `selected`, add to selection counts and have
their former usage contribution subtracted (clamped at zero). Their trace IDs and
history remain intact. Other traces and independent outcomes retain legacy counts.
This does not reconstruct unknown historical counter drift or claim that every
legacy use was a verified application. Reindexing preserves both counters.

The DB migration takes effect when the updated SkillStore first opens that
database. Verification uses isolated test/evaluation databases. Integration review
also opened a read-only backup of the operating database with the candidate code
and checked preservation and idempotent reopening; the operating database was not
migrated by that review.

## Verification

Tests cover choice correction, same-ID application promotion/replay, independent
applications, session/wave mismatch rejection, preserved graded outcomes, reindex,
one-time migration, and rollback of schema/phase/counters on injected DB failure.
The WebUI test checks distinct selection/usage values, phase and evidence display.

Live E21–E24 used real local skill files and SQLite traces. E23 ended with translate
selection=1/use=0 and table-sum selection=1/use=1, with success=0 because no business
gate had run. E24 preserved a prior choice and a new applied record as distinct IDs
(selection=2/use=1); it did not infer an identity absent from the supplied history.

The live observations are individual cases, not a semantic guarantee for every
model claim. Evidence is recorded from the tool caller; final outcome quality still
requires separate verification.
