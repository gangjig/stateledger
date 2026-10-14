# stateledger

**Explicit-state memory for long-running agents: evidence and conclusions, kept separate, with provenance.**

Most agent "memory" is one growing blob — a chat history, or a free-form
notes file the model rewrites as it goes. That fails in a specific,
predictable way: **silent drift**. A belief formed on Monday ("the API
base is `/v1`") sits in the text forever. On Tuesday the endpoint starts
returning 404, but nothing connects the new fact to the old belief, and
nothing records why the agent believed anything in the first place. When
the memory file *is* updated, the old belief is edited in place — the
history of what the agent believed, and why, is gone.

stateledger takes the LongHarness approach instead: the agent's state
is a structured **ledger** with two strictly separate layers.

- **Evidence** — raw observations: tool outputs, file references, test
  results. Append-only, deduped by content hash, never rewritten.
- **Conclusions** — derived beliefs. Each carries the ids of the
  evidence that supports it (provenance), a confidence, and a lifecycle
  status: `active` → `superseded` or `retracted`. A conclusion is never
  edited in place; when it stops being true, it is *closed* through the
  lifecycle and linked to what replaced it.

A **Reconciler** closes the loop: real-world outcomes ("that deploy
target was wrong, here's the log") are replayed into the ledger as
supersede/retract bookkeeping — deterministic, auditable, and safe to
run in batch.

Pure Python, zero dependencies.

## Quickstart

```python
from stateledger import Ledger, rewrite_from_outcomes

ledger = Ledger()

# 1. The agent observes something and forms a belief.
ev = ledger.add_evidence(
    "tool-output",
    content="GET https://api.example.com/v1/users -> 200",
    source="smoke-test",
)
belief = ledger.add_conclusion(
    "The API base path is /v1", [ev.id], confidence=0.8
)

# 2. Reality contradicts it. New evidence is filed...
ledger.add_evidence(
    "tool-output",
    content="GET https://api.example.com/v1/users -> 404",
    source="smoke-test",
    contradicts=[belief.id],
)
print([f.conclusion.id for f in ledger.find_stale()])   # ['c_0001']

# 3. ...and an outcome record rewrites the belief: the old conclusion
#    is superseded (not edited), linked to its replacement.
report = rewrite_from_outcomes(ledger, [{
    "conclusion_id": belief.id,
    "verdict": "wrong",
    "reason": "v1 was decommissioned; the service moved to /v2",
    "evidence": {"kind": "tool-output",
                 "content": "GET https://api.example.com/v2/users -> 200",
                 "source": "smoke-test"},
    "replacement": {"statement": "The API base path is /v2",
                    "confidence": 0.95},
}])

print(ledger.active_conclusions()[0].statement)          # The API base path is /v2
print(ledger.get_conclusion(belief.id).status)           # superseded

# 4. The full "why do we believe this — and what did we believe
#    before?" chain stays walkable.
trail = ledger.evidence_trail(ledger.active_conclusions()[0].id)
for entry in trail.entries:  # newest first
    print(entry.conclusion.id, entry.conclusion.statement,
          "supported by", [e.id for e in entry.evidence])
```

Run the full scenario (including Markdown export and prompt rendering)
with:

```bash
python examples/smoke_demo.py
```

## Data model

```
Evidence                              Conclusion
--------                              ----------
id            ev_0001                 id             c_0001
kind          "tool-output"           statement      "The API base is /v2"
content       "GET /v2 -> 200"        evidence_ids   (ev_0003,)   <- provenance
  (or ref)                            confidence     0.95
source        "smoke-test"            status         active | superseded | retracted
timestamp                             supersedes     c_0000  (what it replaced)
hash          sha256(kind+payload)    superseded_by  c_0007  (what replaced it)
contradicts   (c_0000,)               retraction_reason / retraction_evidence_ids
      |                                        |
      +--- evidence is append-only             +--- conclusions are immutable:
           and deduped by hash                      status transitions only,
                                                    recorded in an audit log
```

## Design rules

1. **Evidence is append-only.** There is no API to edit or delete an
   observation. If the world changed, that is a *new* observation.
2. **Conclusions are never mutated in place.** Statement, evidence list
   and confidence are frozen at creation. The store performs status
   transitions by swapping in a copy — a `Conclusion` object you
   already hold still shows the state it had when you fetched it.
3. **Nothing closes silently.** Superseding links both directions.
   Retracting requires a non-empty reason and at least one piece of
   supporting evidence; the store refuses otherwise.
4. **Omission is reported, never silent.** `render_context` fits
   conclusions into a character budget greedily and returns exactly
   which conclusions and evidence were left out, and why.

## Main APIs

| Area | Call |
| --- | --- |
| Evidence | `add_evidence(kind, content=… \| ref=…, source=…)`, `all_evidence()` |
| Conclusions | `add_conclusion(statement, evidence_ids, confidence)` |
| Lifecycle | `supersede_conclusion(old_id, statement, evidence_ids, confidence)`, `retract_conclusion(id, reason, evidence_ids)` |
| Queries | `active_conclusions()`, `conclusions_for_evidence(id)`, `evidence_trail(id)`, `find_stale(max_evidence_age_seconds=…)` |
| Reconciler | `Reconciler(ledger).apply_outcome(record)`, `rewrite_from_outcomes(ledger, records)` |
| Persistence | `ledger.save(path)` (atomic), `Ledger.load(path)`, `to_dict()` / `from_dict()` |
| Rendering | `export_markdown(ledger, path=None)`, `render_context(ledger, max_chars=4000)` |

**Stale rule** (deliberately simple, deterministic): an active
conclusion is stale if some evidence lists it in its `contradicts`
field, or — when `max_evidence_age_seconds` is given — its newest
supporting evidence is older than that.

**Reconciler verdicts**: `confirmed` leaves the conclusion active and
logs the outcome; `wrong` / `outdated` supersede it when the record
carries a `replacement`, otherwise retract it with the record's reason.

## Persistence

The store serializes to a single JSON file (schema-versioned), written
atomically — temp file + rename — so a crash mid-save never leaves a
torn ledger. Counters, dedupe hashes, statuses and the audit log all
survive a save/load round-trip.

## Limitations (honest ones)

- **Confidence is the agent's own number.** stateledger stores and
  sorts by it; it does not calibrate it. A confidently wrong conclusion
  is still wrong — the ledger only guarantees you'll be able to see
  *why* it was believed and *when* it stopped being believed.
- **Contradiction is declared, not inferred.** The stale finder only
  knows about contradictions recorded in an evidence item's
  `contradicts` field (or the age rule). It will not notice that two
  statements disagree in natural language.
- **Single-process, single-file.** The JSON store is for one agent
  process; there is no locking against concurrent writers and no
  query engine. State bigger than memory needs a different backend.
- **Character budgets are approximate.** `render_context` budgets in
  characters, not tokens; treat chars ≈ 4× tokens as a rough guide.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e . pytest
python -m pytest
```

Requires Python ≥ 3.10. No runtime dependencies.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 Gang Ji.

