# Linking omitted write result references

An otherwise complete TaskNote could require another model call solely because
its report omitted a successful write's opaque tool-call ID. The file had already
been saved and read back; retrying the mutation was neither necessary nor safe.

TaskNote.accept now links omitted IDs from its own bounded write_results ledger
only when every remaining defect is missing_result_reference, the update has
status completed, all selected write targets have a latest successful event in
the current request, and every supplied reference belongs to those events.
It copies exact execution IDs rather than deriving IDs from tool names or reads.
The submitted update is copied; original model tool arguments are not mutated.

This does not infer analysis, read coverage, permission or write success. Active,
partial, blocked, missing-answer and incomplete-analysis reports still follow
their existing checks. A latest failure, denial or unknown result prevents
linking. Incorrect supplied references are not replaced by this repair. A timed
out write can qualify only after the existing read-after-unknown-write check has
verified its expected content hash. Earlier request writes cannot qualify.
Cancellation and mutation guards are unchanged.

The metadata and update_task response record runtime_linked_result_refs so an
audit can distinguish runtime linking from references supplied by the model.
That list is bounded to 40 IDs and cleared when the request changes. Linking
performs no tool action or additional model call.

The focused regression suite covers omitted and partially supplied references,
wrong IDs, unsuccessful latest attempts, incomplete reports, missing read
coverage, request changes and timeout reconciliation without a second write.

One paired isolated Grok 4.7 evaluation, using the same frozen operating policy
and memory, changed save-and-read input usage from 114,647 to 89,076 tokens and
model calls from five to four. Each side performed one real write and one real
read. This is a single observation rather than an average savings claim.

The subsequent main-only deployment on 2026-11-17 verified four model calls,
one actual write/read, exact execution references and runtime-link audit records
through the operating WebUI WebSocket service. Other gateway processes and
configurations were preserved. Deployment evidence is referenced by
.local/deployments/latest-context-report.txt.
