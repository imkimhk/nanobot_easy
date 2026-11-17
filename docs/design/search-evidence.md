# Search outcome reporting

An audit of the resumed E12 evaluation found a real earlier `search_memory`
assistant/tool pair in the persisted session. The resumed call reused that result.
Counting only new tool calls incorrectly classified this as an unsupported search
claim. E12's baseline verdict was corrected from partial to pass, with the initial
grading and source evidence preserved in the local evaluation artifacts. Recorded
usage totals remain unchanged; interrupted-attempt usage is still unknown.

Search reporting now makes the following distinctions:

- A completed query carries its query, runtime scope, candidate count and returned
  window count, including when nothing is returned.
- `no_matches` means no candidates within that query/scope. It does not establish
  global absence.
- `no_evidence_returned` means candidates existed but no evidence window was
  returned; it must not be presented as no candidates.
- `candidates_returned` means evidence candidates were returned. They may contain
  unrelated fallback records or the current question, rather than the requested fact.
- Exceptions remain tool errors. They do not become a successful empty search.
- An applicable earlier real result can be reused and identified as earlier
  evidence. A new tool call is not required solely for bookkeeping.

Memory guidance, the search tool description and the general tool contract explain
these rules. This is an outcome-format and instruction improvement, rather than a
language-independent semantic verifier of every final sentence. Search ranking,
permission scopes, tool error propagation and the runner's execution path are
unchanged. Ordinary compact answers do not receive the deferred memory tutorials.

Validation uses real scoped memory stores, preserved assistant/tool protocol pairs,
an injected search error, an unavailable registry, an unrelated-record fixture,
an empty DB and the compact activation path. The isolated live evaluator is:

```bash
python -m scripts.improvement_eval.live_search_evidence --out .local/evaluations/search-new
```

It uses the existing Codex OAuth account and incurs model usage. Use a fresh output
directory; `--cases empty-db` runs just the empty-DB case. `reuse-earlier` requires
`fresh-empty`, whose name means the requested fact is absent; its DB intentionally
contains an unrelated deployment record.

## Bounded recovery from memory backend failures

The Grok 4.7 integration run exposed an instruction conflict: the general tool
contract encouraged trying a different approach after errors, while memory
guidance explained honest reporting without saying when recovery should stop.
A persistent memory outage led to two failed searches and repeated reads of
missing files/directories, exhausting nine iterations plus a finalizer call
(102,864 input tokens).

The memory instructions and tool description now allow at most one retry for a
potentially transient backend/index error. Changing search words does not repair
an unavailable backend. After persistent failure, only a known relevant source
may be used as an alternative; guessed memory paths, unrelated workspace browsing
and forced rereads of known missing files are not recovery strategies. Without a
relevant alternative, the agent should report that retrieval failed and the fact
remains unverified. Explicit memory diagnosis/repair requests remain separate.
The general error-recovery instruction now requires a concrete reason to expect
progress and points to these memory-specific rules.

This is a prompt-level correction, not a hard runtime call cap. It leaves normal
multi-step work, search permissions and error propagation unchanged. Model
compliance must be checked with live evaluation; scripted responses cannot prove
that the model follows these instructions.

The operating-provider evaluator supports persistent outage, successful search
with no prior evidence, and a transient outage followed by a real scoped search:

```bash
python -m scripts.improvement_eval.live_integration --config .local/config.json \
  --out .local/evaluations/search-recovery-new \
  --cases search-error search-empty search-transient
```

Use a fresh output directory. The transient fixture fails only the first search;
subsequent calls execute the actual scoped backend. Inspect tool events, final
answers and provider usage rather than treating a normal stop reason alone as a
pass. This evaluator uses the selected operating provider and incurs model usage.
