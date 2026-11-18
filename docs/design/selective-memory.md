# Explicit reference selection for long-term memory

Long-term memory can contain both project facts and user rules. Guessing which
sentences are optional could hide permission gates or preferences. This feature
therefore selects only reference groups that the memory author explicitly marked
optional. All unclassified memory stays loaded.

```json
{"agents": {"defaults": {"selectiveMemory": true}}}
```

Default is false. This is separate from the older contextSelection feature and
works with or without compactContext. It makes no extra model call.

## Memory format

```markdown
# Long-term Memory
## Rules
- Ask for separate approval before changing production settings.
## Reference Notes
### Aurora | 오로라
- Current project code: A7Q-314.
- The older OLD-19 value is revoked.
### Borealis
- A separate project's reference material.
## Permanent Constraints
- Retain existing schedules.
```

Only exact level-two headings `## Reference Notes` and `## 참고 메모` opt a
section into selection. All other sections, including unknown ones, are retained.
Reference content must be top-level bullet records; wrapped indented lines stay
with their record. A level-three heading groups a complete project. All records
under that project are selected together, so an old fact cannot be selected
without its correction merely because they were separate bullets.

The implementation uses local lexical matching. URLs and filesystem paths,
including quoted paths with spaces, are excluded from matching so incidental
directory names such as local/workspace do not load unrelated project notes.
Recognized filenames such as local.txt are excluded from body matching too;
dotted technical identifiers are not automatically treated as files. This
cleaning affects only selection; the actual user request and tool paths are
unchanged.

An explicit heading name or pipe-separated alias takes priority over incidental
body words. Name matching respects boundaries, Korean particles and sentence
punctuation; Aurora does not identify Aurora-East or Aurora.One. Dotted names and
explicitly named filename headings remain valid identities. All aliases for the
identified project also match its direct cross-references in other groups. Those
complete groups are kept even over budget, including corrections and every
explicitly requested project in a comparison.

Without an explicit identity, local lexical overlap, including Korean substrings,
remains available for topic queries. Common query words shared by every reference
cannot identify a narrower topic, so that case retains everything. A soft budget
of 1,200 estimated tokens limits body-topic matches; the first matching group
remains complete even if larger. No selected record is cut or rewritten. This is
not semantic search: synonyms and references expressed only through paths may
need full-source retrieval.

An explicit full-memory request, an ambiguous followup, unsupported reference
formatting or a code fence preserves the whole memory. Cross-project filesystem
scopes and disabled file access also keep whole memory. Calls without a current
task query retain full memory. Turns with previous dialogue also retain full
memory: a reliable new-task boundary is not inferred from wording alone. This
means the current optimization primarily benefits new-session requests, not
ongoing conversations. Existing dialogue, summaries, bootstrap rules,
custom always-on skills and configured hot skills are unchanged.

Here "full memory" means the current MEMORY.md file. A separately reviewed
[historical archive](memory-archive.md) can shorten that file while retaining
all current rules and a pointer to complete source records. Such archives remain
on demand even for ongoing conversations; this selector does not classify or
move their contents automatically.

Selected output identifies how many groups were included and points to the full
MEMORY.md source. Omission is not proof of absence. If the requested fact is not
present, the agent can activate read_file and read the full original through its
normal filesystem guards. Do not put globally applicable rules or preferences
inside optional references. No automatic classification or source migration was
implemented.

Both compact and full prompt construction use the same selection. Activating
execution tools does not silently restore omitted reference groups. Dream's
instructions preserve an existing explicit organization and keep global rules
outside optional references; Dream was not executed as part of this change.

## Operating-memory finding and reviewable proposal

The operating MEMORY.md had 81 bullet entries, about 7,660 locally estimated
tokens, all under Project Context. Many entries contained permission, scope,
workflow or persona constraints. Enabling the feature on that original format
does not shorten it. Six offline comparisons verified identical system prompts
with the option on and off for arithmetic, file work and a persona question.

A separate proposal groups 13 named persona-specific reference entries into five
projects. Every original bullet remains verbatim exactly once. Shared-gateway,
security and cross-project instructions remain always loaded. Its manifest
records source/proposal hashes and each moved line's hash. Following scope and
hash review, the user-approved main deployment on 2026-11-17 adopted this
organization and enabled selectiveMemory. Other gateway configurations were not
changed or restarted. This was a reviewed local migration, not an automatic one.

Do not claim synthetic-reference benchmarks as production savings. The initial
implementation and isolated evaluation left production unchanged. The subsequent
main deployment backed up the stopped workspace and original settings/memory
before applying the reviewed proposal and restarting only main.

## Verification

Unit and integration tests cover policy retention, default-off behavior, Korean
queries, generic and deictic queries, complete corrections over budget, malformed
reference fallback, cross-project fallback and full-guidance activation.

```bash
pytest tests/agent/test_memory_selection.py tests/agent/test_compact_context.py \
  tests/agent/test_context_builder.py -q
python -m scripts.improvement_eval.live_memory_selection --config .local/config.json \
  --out .local/evaluations/memory-selection-new
```

The live evaluator uses the operating provider with synthetic memory and isolated
real files. It measures math, save-and-reread, Korean fact recall, followups,
missing-fact full-source reads and a pinned approval gate. It incurs model usage.

The initial evaluation exposed a pre-existing stall guard false positive on an
exact repeat-answer request, both before and after selection. A separate runner
fix now exempts explicit answer-only replay requests from cross-turn comparisons.
It does not disable comparisons within the current run, failed-tool recovery,
task evidence checks or execution-request guards. It uses the current visible
user text, excluding runtime metadata and background memory; ambiguous requests
and requests combining a replay with more work retain the guard.

Six subsequent live checks passed with selection both off and on: factual recall,
the repeated answer and a followup asking for only the last three digits. One
repeated live answer met the old rejection threshold, confirming the correction
was exercised. The subsequent operating WebUI check also delivered an identical
long answer on request. Arithmetic, persona-memory recall and a real file
write/reread passed; 44 deployment checks verified source/config fingerprints,
backups, preserved memory, exact write evidence and delivery. The initial
evaluation report retains its original failures.

The operating file check used five model calls and 116,663 input tokens, compared
with 118,836 in the previous deployment's separate observation (1.83% less).
This is not a controlled estimate: lexical selection retained extra reference
groups for that wording, and a missing result reference needed one report-only
correction without repeating the successful write/read. Do not extrapolate the
larger isolated draft benchmark reduction to production or ongoing dialogue.

The subsequent precision patch described above was tested in isolation and then
deployed to main on 2026-11-17 with result-reference linking and the reviewed
historical archive. It removes path-induced
matches and prioritizes explicit identities while preserving policies, complete
cross-references and followup fallback. In one paired isolated Grok run with
frozen operating policy copies, file input tokens were 117,940 before and 114,647
after (2.79% less), with five model calls and one real write/read on each side.
All eight live cases and 517 regression tests passed. This is an isolated
observation, not a production or average savings claim. The evaluator accepts a
private memory-question file through --memory-prompt-file; project-specific
questions do not need to be embedded in the reusable script.

The subsequent [result-reference patch](task-result-references.md) removes an
extra report correction when all requirements except omitted successful write
references are satisfied. It is separate from reference-group selection.
