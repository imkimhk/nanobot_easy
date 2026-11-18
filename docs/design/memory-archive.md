# Keeping historical records outside baseline context

Long-term MEMORY.md is loaded repeatedly. Dated implementation stories, old
versions, resolved incidents and superseded designs can make this baseline
large even after explicit reference selection. Historical records can instead
be preserved in ordinary files reached through a short, always-loaded index
pointer. No new retrieval framework or automatic sentence classifier is needed.

The reviewed layout is:

```text
memory/MEMORY.md                    current rules, context and archive pointer
memory/archive/INDEX.md             dated descriptions and links
memory/archive/record-NNN.md        complete original affected records
memory/archive/MEMORY.before-*.md   exact complete original memory
```

The reorganization is manual. Preserve current permissions, scope boundaries,
preferences, safety rules, completion criteria and unresolved state directly in
MEMORY.md or its existing canonical always-loaded policy files. Retain any
context needed to interpret them; removing the antecedent of "those files" can
change a restriction even if its sentence survives. Keep mixed historical/rule
records intact whenever separation is uncertain. An unresolved incident or a
current runtime requirement is not merely historical because it has a date.

Save the complete source first. Review every removed fragment, record original
and retained hashes, preserve each affected original record in full, and keep a
manifest linking each change to its archive file. Existing explicit Reference
Notes groups and corrections must remain intact. Do not use keyword matching as
proof that a fragment contains no rule.

The active pointer directs the agent to read the index and relevant original
before answering historical questions or acting on an old incident. Archives
are historical data, not current approval, proof of current versions or proof
of delivery. Current rules remain in force. Reads use the existing read_file
workspace boundary; archives are not injected into full, compact or followup
system prompts. A previously read record can of course appear in dialogue
history. Dream's conditional instructions preserve an existing archive layout
and prevent automatic migration of active restrictions; Dream is not run by
this reorganization.

## Reviewed local candidate

The 2026-11-17 candidate reviews 20 of 68 always-loaded records, separates 48
historical fragments, and fully archives two completed or explicitly superseded
records. Thirteen optional reference entries remain byte-for-byte unchanged.
The complete original retains all 81 entries. Active memory decreases from
7,718 to 6,484 locally estimated tokens, about 16%. This is a memory-only local
estimate; provider request totals also include policy, tools and dialogue.

The candidate and manifest are in the private evaluation workspace referenced
by .local/evaluations/latest-context-report.txt. After separate approval, the
candidate was applied to main on 2026-11-17 after rechecking the original hash
and backing up the stopped workspace. Main alone was restarted; all gateway
configurations and the other five gateway processes were preserved. Deployment
evidence is referenced by .local/deployments/latest-context-report.txt.
Reapplying this snapshot elsewhere requires checking the original memory hash
again and preserving any newer memory; it is not a generic automatic migration.

Regression tests verify that rules and the archive pointer stay in full, compact
and followup context while historical content stays on demand. A scripted loop
retrieves the index and original through real guarded file reads, without
changing active memory. The live evaluator also accepts --history-prompt-file
to verify an archived fact against the copied original.

The final isolated Grok 4.7 run passed arithmetic, save-and-reread, reference
recall, exact answer replay and historical retrieval. Historical retrieval read
the actual index and applicable record before reporting the archived identifier.
With the result-reference patch held constant, arithmetic input decreased from
15,047 to 13,778 tokens and file work from 89,076 to 82,440 tokens, still with one
write and one read. These are single-run observations with frozen policy copies;
they do not establish average savings or production behavior.
