# Runtime integration review (2026-11-17)

This change combines context/token improvements with previously uncommitted
runtime work, retaining main's existing context-selection, request-budget and
stream-diagnostic features. The original operating checkout is preserved;
validation here does not deploy or restart a gateway.

## Included work

- Per-provider-attempt wall-clock limits, bounded recovery after failed mutations,
  repeated-answer protection and explicit answer-replay handling.
- Provider/model-scoped encrypted Grok reasoning replay, retained through session
  history and runtime checkpoints.
- Background shell jobs with status, log tails, exit information and bounded
  termination; foreground timeout reports require verification before retrying.
- Lightweight task cancellation, retained prior effects and no automatic undo or
  goal continuation after withdrawal.
- Scoped memory-search outcome reporting and bounded recovery guidance.
- Separate skill selection/application counters, additive schema migration, and
  corresponding CLI/WebUI fields and trace evidence.
- An optional Genie workspace adapter for its locally installed study-review
  package. The private implementation, learner identifiers and database are not
  shipped. Missing packages do not prevent ordinary tool discovery.
- Main-only and explicit all-persona lifecycle scripts. Ordinary starts use the
  current checkout; pinned runtime profiles remain available by explicit choice.
  All-persona scripts need the local `.local/gateway-repo.sh` helper and check it
  before changing main. They are not general installation scripts.

The earlier dialogue/skill-counter and gateway-process-identity commits are also
included. A generic research-skill attachment is preserved as reference material,
not installed as a runtime skill. The external-AI browser document is a design
proposal, not an implemented tool.

## Integration corrections

Provider call limits are nested with the existing whole-request deadline so the
shorter limit wins. Whole-request send counts, unknown token usage and content-free
stream diagnostics remain intact. Non-finite timeout settings fall back to the
default. The completed-response callback used for reasoning replay is forwarded
through both diagnostic and ordinary parser paths.

Background-job access checks session ownership for both live and persisted jobs.
Persisted metadata cannot redirect a log read outside the job directory, and
symlinks cannot redirect the job directory outside its workspace. Foreground
cancellation/timeout retains existing process-termination evidence. Genie adapter
tests use a synthetic local package and temporary database, not private user data.

## Verification and limits

- Broad Python run: 4,650 passed, 28 failed, 1 error, 36 skipped and 1 xfailed.
  All 28 failures and the setup error reproduce on unmodified origin/main
  (`b9349136a`) when the same failed cases are rerun. No additional failing case
  was found in that comparison. The broad run is not fully green.
- Existing failures include experimental context-selection cases, an unavailable
  fixture, onboarding provider labels, mocked workspace/config handling and an
  MCP logging expectation. This integration does not claim to repair them.
- Focused post-review tests cover call/request deadline precedence, job ownership
  and path containment, optional adapter loading and safe lifecycle wrapper scope.
- WebUI: 42 app-layout tests passed, affected-file ESLint passed, and TypeScript /
  Vite production build passed. Generated assets remain untracked build output.
- Affected Python files pass Ruff except context_selection.py, whose 10 existing
  diagnostics are a subset of the 12 diagnostics on unchanged main. No new Ruff
  diagnostic was added there. No repository-wide formatting was performed.
- Five isolated Grok 4.7 cases passed: arithmetic, one actual save/reread, memory
  recall, exact answer replay, and archive index/original retrieval. File work
  completed in four model calls with exact successful-write references. No MCP
  servers or operating sessions were used for these evaluations.
- Candidate code reopened a read-only backup of the operating skill registry.
  All 76 skill rows and 954 trace rows were unchanged; reopening was idempotent.
  The operating settings parsed with the current model and flags retained.
- Operating process, source/config/memory fingerprints and other gateway process
  identities remained unchanged, and the operating health endpoint remained OK.

These checks support the reviewed configuration and tested paths; they do not
prove every provider, platform or experimental mode is defect-free. In particular,
the current operating configuration has context selection disabled. Review
artifacts, private instruction copies and database backups remain under .local/;
they are not part of this PR. Production replacement/restart is a separate action.
