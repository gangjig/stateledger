"""End-to-end smoke demo for stateledger.

Scenario: an agent discovers an API's base path, believes it, gets
contradicted by a failing request, reconciles the outcome, and exports
its state. Run it with:

    python examples/smoke_demo.py
"""

from pathlib import Path

from stateledger import Ledger, export_markdown, render_context, rewrite_from_outcomes

OUT_DIR = Path(__file__).parent / "output"


def main() -> None:
    ledger = Ledger()

    print("1. The agent observes the API and forms a belief")
    ev1 = ledger.add_evidence(
        "tool-output", content="GET https://api.example.com/v1/users -> 200",
        source="smoke-test",
    )
    belief = ledger.add_conclusion(
        "The API base path is /v1", [ev1.id], confidence=0.8
    )
    print(f"   {ev1.id}: {ev1.content}")
    print(f"   {belief.id}: {belief.statement} (confidence {belief.confidence})")

    print("\n2. A retried observation dedupes instead of double-counting")
    again = ledger.add_evidence(
        "tool-output", content="GET https://api.example.com/v1/users -> 200",
        source="smoke-test-retry",
    )
    print(f"   re-adding the same output returned {again.id}; "
          f"evidence count is still {len(ledger.all_evidence())}")

    print("\n3. Reality contradicts the belief")
    ev2 = ledger.add_evidence(
        "tool-output", content="GET https://api.example.com/v1/users -> 404",
        source="smoke-test", contradicts=[belief.id],
    )
    stale = ledger.find_stale()
    print(f"   {ev2.id}: {ev2.content}")
    for finding in stale:
        print(f"   stale: {finding.conclusion.id} — {'; '.join(finding.reasons)}")

    print("\n4. The reconciler rewrites the belief from the outcome")
    report = rewrite_from_outcomes(
        ledger,
        [
            {
                "conclusion_id": belief.id,
                "verdict": "wrong",
                "reason": "v1 was decommissioned; the service moved to /v2",
                "evidence": {
                    "kind": "tool-output",
                    "content": "GET https://api.example.com/v2/users -> 200",
                    "source": "smoke-test",
                },
                "replacement": {
                    "statement": "The API base path is /v2",
                    "confidence": 0.95,
                },
            }
        ],
    )
    result = report.results[0]
    print(f"   outcome: {result.action}; {belief.id} -> {result.new_conclusion_id}")

    print("\n5. Provenance trail for the current belief")
    trail = ledger.evidence_trail(result.new_conclusion_id)
    for entry in trail.entries:
        c = entry.conclusion
        ev_ids = ", ".join(e.id for e in entry.evidence)
        print(f"   {c.id} [{c.status.value}] {c.statement}  <-  {ev_ids}")

    print("\n6. Prompt-ready context within a 400-char budget")
    rendered = render_context(ledger, max_chars=400)
    print(rendered.text)
    print(f"   included conclusions: {rendered.included_conclusion_ids}")
    print(f"   omitted conclusions: "
          f"{[(o.id, o.reason) for o in rendered.omitted_conclusions]}")
    print(f"   omitted evidence: {rendered.omitted_evidence_ids}")

    OUT_DIR.mkdir(exist_ok=True)
    md_path = OUT_DIR / "ledger.md"
    export_markdown(ledger, md_path)
    json_path = ledger.save(OUT_DIR / "ledger.json")
    print(f"\n7. Exported {md_path} and {json_path}")


if __name__ == "__main__":
    main()
