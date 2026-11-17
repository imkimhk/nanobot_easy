# Task-note cancellation

The opt-in lightweight task-note path previously treated a withdrawn write target
as unfinished work. A prepared write could be correctly skipped, while the final
reply still failed with `execution_required`.

For `intent=withdraw`, targets identify the cancelled work. Completion checks
require an actual cancellation report and a settled status, rather than execution
of those targets. `status=cancelled` is accepted only with `intent=withdraw`;
legacy `intent=withdraw, status=completed` reports also end with the runtime status
`cancelled`. Both normal final replies and the last permitted iteration use this
rule. A sustained goal cannot automatically resume a cancelled request.

Request revision preserves up to 40 prior mutation records separately from current
execution evidence. They are visible in `superseded_mutations`, but do not grant
permission or satisfy the new request's write checks. The runtime cancellation
report retains known success/unknown/error outcomes and makes no rollback claim.
A real read may reconcile an unknown full-file save through its existing hash
verification contract. Cancellation during a mutation records an unknown result
with its target; child cleanup still uses the existing bounded cancellation logic.

Cancellation does not automatically undo completed or partial effects. A separate
request to inspect remaining state runs read-only verification and can complete
normally. A change from writing to review-only analysis remains ordinary work,
rather than being classified as whole-task cancellation.

The full context-selection cancellation path, transport delivery, production
gateway settings, search-claim validation and skill statistics are unchanged.

Validation includes prepared-write supersession, last-iteration reporting, goal
continuation prevention, retained prior effects, unknown-save reconciliation and
actual cancellation after a partial write. Live isolated E17/E18 evaluation uses
real filesystem tools with explicitly injected cancellation.
