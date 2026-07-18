# benchmarks/ — the eval question set (design §10)

One YAML spec per benchmark question, validated against the `benchmark-spec`
schema (`src/deeper/schemas/evaluation.py`, exported to
`schemas/benchmark-spec.schema.json`). The four seeded specs cover the
design's question shapes — a personal decision with known ground truth
(`probe-space-mapping`), a technical selection (`vector-store-selection`), a
personal decision without ground truth (`faculty-targeting-decision`), and an
open advice question (`cold-email-advice`) — with all content drawn from the
two live runs' triage notes (`docs/m1-live-run-notes.md`,
`docs/m2-live-run-notes.md`, pre-authored in `docs/benchmark-seeds.md`).

## Spec fields

- `question` — the goal string, verbatim; a benchmark rerun starts with
  `deeper new "<question>" --profile <profile>`.
- `type` — the §10 shape tag.
- `reference_angles` — the reference angle union the breadth metric scores
  against. Each angle carries `provenance` (`organic` | `human-added`) and a
  `practitioner_obvious` flag: a **human-added** angle was missed by the
  original run's ensemble and added at Gate A, so a fresh run is scored on
  producing it *without* help, and missing a `practitioner_obvious` angle
  draws the design's explicit penalty flag. Empty until a question's first
  run seeds it (build the union from that run's map plus a manual pass).
- `ground_truth` — what is actually known vs what remains the **user's
  adjudication**; the eval records it, never settles it.
- `option_checks` — option-level ground truth: case-insensitive
  `evidence_terms` scanned over every option card. This exists because the M2
  ground-truth divergence was an *option-level* scouting miss ("compression"
  appeared in zero cards) that the angle-level metric alone cannot catch.
- `baseline_file` — points into `baselines/`, where a plain Deep Research
  answer to the same question is pasted for `deeper eval --compare-baseline`.
  The files ship as commented placeholders; the eval refuses a placeholder
  loudly.

## Using them

```bash
deeper eval <run> --against probe-space-mapping        # score a run
deeper eval <run> --against probe-space-mapping --compare-baseline
deeper eval --compare <runA> <runB>                    # before/after a change
```

Judged matching (breadth, baseline) is one Haiku-class `eval-judge` dispatch
per side, through the same agent runtime as the pipeline — mock runs judge
from fixtures, live runs meter the run's own ledger under stage `EVAL`.
