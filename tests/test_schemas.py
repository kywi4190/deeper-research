"""Schema-layer tests: valid/invalid fixtures per artifact, YAML/JSON round-trips,
the LLM-facing error formatter, and JSON-Schema export freshness.

Fixture content follows the design doc's senior-project example: choosing a
senior research project to position for ML PhD admissions.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from deeper.schemas import (
    ARTIFACT_REGISTRY,
    AllocationTable,
    AngleMap,
    Brief,
    CardCritique,
    CartographerReport,
    ContradictionLedger,
    CoverageReport,
    DestinationModel,
    Dossier,
    FrameCheck,
    GateADecision,
    GateBDecision,
    GateCDecision,
    OptionCardSet,
    Preferences,
    Prosecution,
    Rubric,
    RunState,
    ScoreUpdateLog,
    ScreeningResult,
    Shortlist,
    SourceRecord,
    Steelman,
    VerificationReport,
    format_validation_error,
)
from deeper.schemas.export import check as check_exports
from deeper.schemas.export import export_all

# ---------------------------------------------------------------------------
# Valid payload factories (fresh dicts each call, safe to mutate)
# ---------------------------------------------------------------------------

T1_SOURCE = {
    "url": "https://transformer-circuits.pub/2023/monosemantic-features",
    "tier": "T1",
    "title": "Towards Monosemanticity: Decomposing Language Models With Dictionary Learning",
}


def valid_brief() -> dict:
    return {
        "goal": (
            "Choose the senior research project that best positions me for "
            "admission to a top ML PhD program"
        ),
        "answer_type": "decision",
        "scope_in": [
            "projects feasible within two semesters",
            "projects with a faculty supervisor at my university",
        ],
        "scope_out": ["industry internships", "coursework-only options"],
        "constraints": [
            {"statement": "Thesis must be submitted by April 2027", "kind": "deadline"},
            {
                "statement": "Compute limited to the lab's shared 4x A100 node",
                "kind": "budget",
            },
        ],
        "notes": None,
    }


def valid_destination() -> dict:
    return {
        "judges": [
            {
                "description": "PhD admissions committees at top-tier ML programs",
                "rewards": [
                    {
                        "description": (
                            "First-author publications at top venues (NeurIPS, ICML, ICLR)"
                        ),
                        "evidence": [dict(T1_SOURCE)],
                    },
                    {
                        "description": (
                            "Strong, specific recommendation letters from research supervisors"
                        ),
                        "evidence": [],
                    },
                ],
            }
        ],
        "notes": None,
    }


def valid_preferences() -> dict:
    return {
        "items": [
            {
                "statement": "Fascinated by mechanistic interpretability",
                "strength": "strong",
            },
            {
                "statement": "Mild dislike of pure theory without empirical grounding",
                "strength": "mild",
            },
        ],
        "risk_appetite": ("Comfortable with a risky project if a fallback publication path exists"),
        "notes": None,
    }


def _raw_angle(name: str) -> dict:
    return {
        "name": name,
        "definition": f"Projects in the {name.lower()} region of the space.",
        "distinctness_rationale": "Varies a dimension no other candidate angle varies.",
        "example_options": ["a concrete existing option", "another concrete option"],
        "relevance_rationale": (
            "The destination rewards publishable insight under the brief's deadline; "
            "this region decouples contribution from training compute."
        ),
        "notes": None,
    }


def _strategic_note(source_heuristics: list[str] | None = None) -> dict:
    return {
        "insight": (
            "The letter-writer's community standing may outweigh the project topic "
            "in the judge's actual reward function"
        ),
        "kind": "rubric-weight",
        "rationale": (
            "The destination model lists strong supervisor letters as a primary "
            "reward signal; this belongs in the S4 rubric, not on the map."
        ),
        "source_heuristics": source_heuristics or [],
    }


def valid_cartographer_report() -> dict:
    return {
        "heuristic": "first-principles",
        "angles": [
            _raw_angle("Interpretability of existing models"),
            _raw_angle("Empirical benchmarking studies"),
            _raw_angle("Research infrastructure contributions"),
        ],
        "strategic_notes": [_strategic_note()],
        "notes": None,
    }


def valid_angle_map() -> dict:
    return {
        "angles": [
            {
                "id": "mechanistic-interpretability",
                "name": "Mechanistic interpretability",
                "definition": (
                    "Reverse-engineering trained networks' internal circuits into "
                    "human-understandable algorithms"
                ),
                "distinctness_rationale": (
                    "Studies existing models rather than building new capabilities; "
                    "distinct methods (SAEs, activation patching) and venues"
                ),
                "example_options": [
                    "Sparse-autoencoder feature atlas for a small code model",
                    "Replicate and extend induction-head analysis",
                ],
                "relevance_prior": 0.85,
                "prior_justification": (
                    "The destination rewards first-author publications; "
                    "interpretability has active venues and compute demands "
                    "compatible with the lab's A100 budget."
                ),
                "contributing_heuristics": ["first-principles", "practitioner"],
                "sub_angles": [
                    {
                        "name": "SAE-based feature analysis",
                        "definition": "Dictionary-learning decompositions of activations",
                    }
                ],
                "notes": None,
            },
            {
                "id": "ml-systems",
                "name": "Systems for ML",
                "definition": (
                    "Infrastructure that makes training and serving models faster "
                    "or cheaper: schedulers, compilers, serving stacks"
                ),
                "distinctness_rationale": (
                    "Optimizes the machinery around models rather than the models "
                    "themselves; different skill signal (systems building) and venues "
                    "(MLSys, OSDI)"
                ),
                "example_options": [
                    "KV-cache-aware inference scheduler",
                    "Profile-guided kernel autotuner for the lab cluster",
                ],
                "relevance_prior": 0.6,
                "prior_justification": (
                    "Systems work yields strong letters from infrastructure-focused "
                    "faculty and demonstrable artifacts, though top-venue "
                    "first-author publication within two semesters is less certain."
                ),
                "contributing_heuristics": ["practitioner", "taxonomist"],
                "sub_angles": [],
                "notes": None,
            },
        ],
        "dedup_map": [
            {
                "heuristic": "first-principles",
                "raw_name": "Understanding model internals",
                "merged_into": "mechanistic-interpretability",
            },
            {
                "heuristic": "practitioner",
                "raw_name": "Interpretability / SAE work",
                "merged_into": "mechanistic-interpretability",
            },
            {
                "heuristic": "taxonomist",
                "raw_name": "ML systems research",
                "merged_into": "ml-systems",
            },
        ],
        "notes": None,
    }


def valid_coverage_report() -> dict:
    return {
        "contributions": {
            "first-principles": ["mechanistic-interpretability"],
            "practitioner": ["mechanistic-interpretability", "ml-systems"],
            "taxonomist": ["ml-systems"],
        },
        "thin_areas": ["Theory-adjacent angles (learning theory, optimization) are unmapped"],
        "strategic_notes": [_strategic_note(["first-principles", "practitioner"])],
        "notes": None,
    }


def valid_allocation() -> dict:
    return {
        "kind": "initial",
        "total_budget_units": 12,
        "floor": 2,
        "gamma": 1.0,
        "per_angle_cap_pct": 25.0,
        "rows": [
            {"angle_id": "mechanistic-interpretability", "relevance_prior": 0.85, "units": 7},
            {"angle_id": "ml-systems", "relevance_prior": 0.6, "units": 5},
        ],
    }


def valid_card_set() -> dict:
    return {
        "angle_id": "mechanistic-interpretability",
        "cards": [
            {
                "id": "sae-feature-atlas",
                "name": "SAE feature atlas for a 1B-parameter code model",
                "angle_id": "mechanistic-interpretability",
                "description": (
                    "Train sparse autoencoders over residual-stream activations of "
                    "a small open code model and publish a searchable atlas of the "
                    "recovered features with automated labels."
                ),
                "mechanism": (
                    "Dictionary learning decomposes activations into sparse, "
                    "near-monosemantic features; automated interpretability labels "
                    "them at scale."
                ),
                "preliminary_evidence": [
                    {
                        "text": (
                            "Anthropic's monosemanticity work shows SAEs recover "
                            "interpretable features at small model scale"
                        ),
                        "source": dict(T1_SOURCE),
                    }
                ],
                "uncertainties": [
                    "Whether 4x A100 suffices to train SAEs on a 1B model in a semester",
                    "Automated feature-label quality without an annotation budget",
                ],
                "kill_risks": [
                    {
                        "fact": (
                            "Residual-stream activation dumps for the target model "
                            "exceed available lab storage"
                        ),
                        "check_hint": (
                            "Compute activation dataset size for 8B tokens at d_model 2048 in fp16"
                        ),
                    }
                ],
                "misplaced_flag": None,
                "notes": None,
            },
            {
                "id": "induction-head-extension",
                "name": "Extend induction-head analysis to code models",
                "angle_id": "mechanistic-interpretability",
                "description": (
                    "Replicate the induction-head circuit analysis on code models "
                    "and characterize how the circuit differs for syntax completion."
                ),
                "mechanism": (
                    "Activation patching localizes the circuit; ablations quantify "
                    "its contribution to in-context copying on code."
                ),
                "preliminary_evidence": [
                    {
                        "text": "Induction heads replicate robustly across model families",
                        "source": {
                            "url": "https://transformer-circuits.pub/2022/in-context-learning",
                            "tier": "T1",
                            "title": "In-context Learning and Induction Heads",
                        },
                    }
                ],
                "uncertainties": ["Novelty over existing replications may be too thin for a paper"],
                "kill_risks": [],
                "misplaced_flag": None,
                "notes": "Could pair with the SAE atlas as a fallback publication.",
            },
        ],
        "notes": None,
    }


def valid_critique() -> dict:
    return {
        "angle_id": "mechanistic-interpretability",
        "completeness_issues": [
            {
                "card_id": "induction-head-extension",
                "issue": "Preliminary evidence cites replication but not code-model feasibility",
            }
        ],
        "redundancy_pct": 10.0,
        "distinctness_issues": [],
        "missed_options": [
            "Probing benchmark for code-model program state",
            "Circuit analysis of fill-in-the-middle behaviour",
        ],
        "notes": None,
    }


def _levels(what: str) -> dict[int, str]:
    return {
        1: f"No credible path to {what} within two semesters",
        2: f"Weak or speculative path to {what}",
        3: f"Plausible path to {what} with notable execution risk",
        4: f"Strong, evidenced path to {what}",
        5: f"Near-certain {what}, with precedent from comparable projects",
    }


def valid_rubric() -> dict:
    return {
        "criteria": [
            {
                "id": "publication-potential",
                "name": "Publication potential",
                "definition": "Likelihood of a first-author paper at a strong venue",
                "measurement_method": (
                    "Venue fit of comparable published work; supervisor's track "
                    "record shepherding student papers"
                ),
                "levels": _levels("a first-author publication"),
                "weight": 0.30,
                "justification": (
                    "Admissions committees weight first-author publications most "
                    "heavily per the destination model."
                ),
            },
            {
                "id": "letter-strength",
                "name": "Recommendation-letter strength",
                "definition": "How specific and strong the supervisor's letter can be",
                "measurement_method": (
                    "Supervisor seniority, collaboration closeness, and their "
                    "students' admission outcomes"
                ),
                "levels": _levels("a strong, specific letter"),
                "weight": 0.25,
                "justification": "Letters are the second reward signal in the destination model.",
            },
            {
                "id": "feasibility",
                "name": "Two-semester feasibility",
                "definition": "Fit within the thesis deadline and lab compute budget",
                "measurement_method": (
                    "Compute/storage estimates vs the 4x A100 node; timeline of "
                    "comparable student projects"
                ),
                "levels": _levels("on-time completion"),
                "weight": 0.20,
                "justification": "The April 2027 deadline is a hard constraint in the brief.",
            },
            {
                "id": "skill-transfer",
                "name": "PhD-relevant skill transfer",
                "definition": "Research skills built that carry into a PhD",
                "measurement_method": "Overlap of project methods with target-lab methods",
                "levels": _levels("durable research-skill growth"),
                "weight": 0.15,
                "justification": (
                    "Committees read for research maturity beyond the artifact itself."
                ),
            },
            {
                "id": "differentiation",
                "name": "Differentiation",
                "definition": "How much the project distinguishes the applicant",
                "measurement_method": (
                    "Rarity of the topic among applicants; uniqueness of available "
                    "data or infrastructure"
                ),
                "levels": _levels("a distinctive application story"),
                "weight": 0.10,
                "justification": "Distinctiveness moves borderline files per admissions surveys.",
            },
        ],
        "preference_slot": {"weight": 0.20},
        "notes": None,
    }


def _cscore(cid: str, score: float, lo: float, hi: float, pointer: str) -> dict:
    return {
        "criterion_id": cid,
        "score": score,
        "band": {"lo": lo, "hi": hi},
        "evidence_pointer": pointer,
    }


def valid_screening() -> dict:
    return {
        "options": [
            {
                "option_id": "sae-feature-atlas",
                "angle_id": "mechanistic-interpretability",
                "criterion_scores": [
                    _cscore("publication-potential", 4.0, 3.5, 4.5, "cards.yaml evidence[0]"),
                    _cscore("letter-strength", 3.5, 3.0, 4.0, "supervisor track record"),
                    _cscore("feasibility", 3.0, 2.0, 4.5, "compute estimate uncertain"),
                    _cscore("skill-transfer", 4.0, 3.5, 4.5, "SAE methods used in target labs"),
                    _cscore("differentiation", 4.0, 3.5, 4.5, "few applicants have SAE work"),
                ],
                "preference_score": _cscore(
                    "preference-slot", 4.5, 4.0, 5.0, "strong stated interest in interp"
                ),
                "kill_risk_checks": [
                    {
                        "fact": "Activation dumps exceed available lab storage",
                        "outcome": "cleared",
                        "evidence": {
                            "url": "https://example.edu/lab-cluster-docs",
                            "tier": "T2",
                            "title": "Lab cluster storage documentation",
                        },
                    }
                ],
                "weighted_point": 3.7,
                "weighted_ucb": 4.4,
                "notes": None,
            }
        ],
        "notes": None,
    }


def valid_shortlist() -> dict:
    return {
        "threshold": 4.0,
        "decisions": [
            {
                "option_id": "sae-feature-atlas",
                "decision": "advanced",
                "cause": "ucb-above-threshold",
                "reason": (
                    "Point estimate 3.7 sits below the 4.0 threshold, but the wide "
                    "feasibility band lifts the UCB to 4.4 — under-researched, not "
                    "weak, so it advances for deep-dive."
                ),
            },
            {
                "option_id": "induction-head-extension",
                "decision": "cut",
                "cause": "below-threshold",
                "reason": (
                    "UCB 3.6 with narrow bands: well-understood option whose novelty "
                    "ceiling caps publication potential; nothing further research "
                    "would plausibly change."
                ),
            },
        ],
        "finalist_ids": ["sae-feature-atlas"],
        "notes": None,
    }


def _section(content: str, claim_ids: list[str] | None = None) -> dict:
    return {"content": content, "claim_ids": claim_ids or []}


def valid_dossier() -> dict:
    return {
        "option_id": "sae-feature-atlas",
        "criterion_sections": {
            "publication-potential": _section(
                "Comparable SAE atlas papers landed at ICLR and NeurIPS workshops "
                "within a year of the technique's publication.",
                ["sae-venue-precedent"],
            ),
            "feasibility": _section(
                "Training SAEs on a 1B model fits the 4x A100 node in roughly "
                "three weeks of wall-clock time.",
                ["sae-compute-estimate"],
            ),
        },
        "failure_modes": _section(
            "Feature labeling quality collapses without a curation pass; "
            "prerequisite: activation storage provisioning before month two.",
            ["sae-compute-estimate"],
        ),
        "cost_of_adoption": _section(
            "Two semesters of focused work; forecloses a systems-track thesis."
        ),
        "second_order_effects": _section(
            "Builds a public artifact that seeds PhD application writing samples."
        ),
        "strongest_criticism": _section(
            "SAE evaluations lack agreed ground truth, so reviewers may dispute "
            "interpretability claims.",
            ["sae-eval-criticism"],
        ),
        "comparable_cases": _section(
            "Two undergraduates published SAE-adjacent first-author workshop papers "
            "in 2025 from similar-size labs."
        ),
        "claims": [
            {
                "id": "sae-venue-precedent",
                "text": "SAE atlas papers have been accepted at ICLR main track",
                "confidence": "high",
                "source": dict(T1_SOURCE),
                "load_bearing": True,
            },
            {
                "id": "sae-compute-estimate",
                "text": "SAE training on a 1B model needs ~3 A100-weeks",
                "confidence": "med",
                "source": {
                    "url": "https://arxiv.org/abs/2406.04093",
                    "tier": "T1",
                    "title": "Scaling and evaluating sparse autoencoders",
                },
                "load_bearing": True,
            },
            {
                "id": "sae-eval-criticism",
                "text": "SAE interpretability metrics remain contested",
                "confidence": "med",
                "source": {
                    "url": "https://www.alignmentforum.org/sae-eval-debate",
                    "tier": "T3",
                    "title": "Forum discussion of SAE evaluation",
                },
                "load_bearing": False,
            },
        ],
        "rounds_completed": 2,
        "budget_capped": False,
        "open_questions": [],
        "notes": None,
    }


def valid_verification() -> dict:
    return {
        "option_id": "sae-feature-atlas",
        "results": [
            {
                "claim_id": "sae-venue-precedent",
                "verdict": "verified",
                "evidence_quote": "Accepted at ICLR 2025 (main track)",
                "note": None,
            },
            {
                "claim_id": "sae-compute-estimate",
                "verdict": "verified",
                "evidence_quote": "Appendix C reports 21 A100-days for the 1B run",
                "note": None,
            },
            {
                "claim_id": "sae-eval-criticism",
                "verdict": "unsupported",
                "evidence_quote": None,
                "note": "Forum thread no longer accessible; claim not re-derivable.",
            },
        ],
        "sampled_load_bearing_count": 2,
        "sampled_other_count": 1,
        "notes": None,
    }


def valid_prosecution() -> dict:
    return {
        "option_id": "sae-feature-atlas",
        "case": (
            "The atlas is a crowded, fast-moving subfield: by submission time, "
            "larger labs may have published overlapping atlases, reducing the "
            "contribution to a replication."
        ),
        "regret_path": (
            "Month four: a major lab releases a superset atlas; the thesis pivots "
            "to a scooped-replication framing and the venue drops to a workshop, "
            "weakening the publication signal the destination rewards most."
        ),
        "supporting_claim_ids": ["sae-venue-precedent"],
        "new_evidence": [
            {
                "text": "Three overlapping SAE atlas preprints appeared in the last quarter",
                "source": {
                    "url": "https://arxiv.org/list/cs.LG/recent",
                    "tier": "T2",
                    "title": "arXiv cs.LG recent listings",
                },
            }
        ],
        "notes": None,
    }


def valid_steelman() -> dict:
    return {
        "option_id": "induction-head-extension",
        "trigger": "rank-inversion",
        "case": (
            "Destination-only scoring ranks it higher than the preference-adjusted "
            "board does: its narrow bands mean the publication estimate is *reliable*, "
            "and a guaranteed modest paper may serve admissions better than a risky "
            "strong one."
        ),
        "supporting_claim_ids": [],
        "notes": None,
    }


def valid_frame_check_gap() -> dict:
    return {
        "verdict": "gap-found",
        "removals_check": {
            "finding": "No angles were removed at Gate A.",
            "consequential": False,
        },
        "missed_options_check": {
            "finding": (
                "The critic flagged 'probing benchmark for code-model program state' "
                "and it was never scouted; it plausibly dominates on feasibility."
            ),
            "consequential": True,
        },
        "rubric_fragility_check": {
            "finding": ("Winner is stable under +/-0.1 weight shifts on all criteria."),
            "consequential": False,
        },
        "proposal": {
            "kind": "scout-task",
            "description": (
                "Scout the two critic-flagged missed options in the mechanistic "
                "interpretability angle."
            ),
            "target_angle_id": "mechanistic-interpretability",
            "estimated_cost_units": 2,
        },
        "notes": None,
    }


def valid_score_update_log() -> dict:
    return {
        "updates": [
            {
                "option_id": "sae-feature-atlas",
                "criterion_id": "publication-potential",
                "old_score": 4.0,
                "new_score": 3.5,
                "cause": (
                    "Prosecution's new evidence of three overlapping preprints makes "
                    "scooping materially likelier."
                ),
                "source_artifact": "tournament/sae-feature-atlas-prosecution.md",
            }
        ],
        "notes": None,
    }


def valid_gate_a() -> dict:
    return {
        "approved": True,
        "added_angles": [
            {
                "name": "Applied ML for science collaborations",
                "note": "Check campus wet-lab collaborations offering unique datasets",
            }
        ],
        "removed_angles": [],
        "prior_adjustments": [{"angle_id": "ml-systems", "new_prior": 0.5}],
        "rerun_hint": None,
        "notes": None,
    }


def valid_gate_b() -> dict:
    return {
        "approved": True,
        "preference_slot_weight": 0.2,
        "weight_overrides": {"feasibility": 0.25, "differentiation": 0.05},
        "edited_criteria": [],
        "notes": None,
    }


def valid_gate_c() -> dict:
    return {
        "approved": False,
        "preference_feedback": [
            {
                "option_id": "sae-feature-atlas",
                "reaction": "The scooping risk bothers me less than the prosecutor assumes",
                "direction": "positive",
            }
        ],
        "evidence_challenges": [
            {
                "option_id": "sae-feature-atlas",
                "claim_id": "sae-compute-estimate",
                "challenge": "The cited paper used H100s; re-check the A100 conversion",
            }
        ],
        "accept_redivergence": False,
        "notes": None,
    }


def valid_source_record() -> dict:
    return {
        "url": T1_SOURCE["url"],
        "tier": "T1",
        "retrieved_at": "2026-07-01T14:30:00",
        "content_hash": "9f2c1a7e40d13b6f8a55c2e9d0b47a31c6e8f24ab7d90135e2c4a6b8d0f1e3a5",
    }


def valid_ledger() -> dict:
    return {
        "entries": [
            {
                "id": "sae-compute-disagreement",
                "statement_a": {
                    "artifact": "dossiers/sae-feature-atlas.md",
                    "statement": "SAE training fits in 3 A100-weeks",
                },
                "statement_b": {
                    "artifact": "options/mechanistic-interpretability/cards.yaml",
                    "statement": "Compute feasibility on 4x A100 is a key uncertainty",
                },
                "detected_by": "verifier",
                "status": "adjudicated",
                "resolution": (
                    "Dossier estimate stands: the card predated the scaling-paper appendix figures."
                ),
            }
        ]
    }


def valid_run_state() -> dict:
    return {
        "run_id": "2026-07-01-senior-project",
        "profile": "standard",
        "stage": "S4",
        "status": "gate-pending",
        "pending_gate": "gate-b",
        "gates": {
            "gate-a": "approved",
            "gate-b": "pending",
            "gate-c": "not-reached",
        },
        "spend": [
            {
                "stage": "S1",
                "role": "cartographer-practitioner",
                "context": None,
                "usd": 0.42,
                "input_tokens": 18000,
                "output_tokens": 3500,
                "at": "2026-07-01T10:15:00",
            },
            {
                "stage": "S3",
                "role": "scout",
                "context": "mechanistic-interpretability",
                "usd": 1.10,
                "input_tokens": 52000,
                "output_tokens": 9000,
                "at": "2026-07-01T11:40:00",
            },
        ],
        "retry_counts": {"S3/scout/mechanistic-interpretability": 1},
        "updated_at": "2026-07-01T12:00:00",
    }


VALID_CASES = [
    (Brief, valid_brief),
    (DestinationModel, valid_destination),
    (Preferences, valid_preferences),
    (CartographerReport, valid_cartographer_report),
    (AngleMap, valid_angle_map),
    (CoverageReport, valid_coverage_report),
    (AllocationTable, valid_allocation),
    (OptionCardSet, valid_card_set),
    (CardCritique, valid_critique),
    (Rubric, valid_rubric),
    (ScreeningResult, valid_screening),
    (Shortlist, valid_shortlist),
    (Dossier, valid_dossier),
    (VerificationReport, valid_verification),
    (Prosecution, valid_prosecution),
    (Steelman, valid_steelman),
    (FrameCheck, valid_frame_check_gap),
    (ScoreUpdateLog, valid_score_update_log),
    (GateADecision, valid_gate_a),
    (GateBDecision, valid_gate_b),
    (GateCDecision, valid_gate_c),
    (SourceRecord, valid_source_record),
    (ContradictionLedger, valid_ledger),
    (RunState, valid_run_state),
]


def _mutate(factory, fn) -> dict:
    payload = copy.deepcopy(factory())
    fn(payload)
    return payload


def _invalid_cases() -> list[tuple[type, dict, str]]:
    """(model, payload, id-suffix) triples, each violating a designed constraint."""
    cases: list[tuple[type, dict, str]] = []

    def add(model, payload, tag):
        cases.append((model, payload, tag))

    add(
        Brief,
        _mutate(valid_brief, lambda p: p["constraints"][0].update(kind="vibe")),
        "bad-constraint-kind",
    )
    add(Brief, _mutate(valid_brief, lambda p: p.update(mood="ambitious")), "extra-field-forbidden")
    add(DestinationModel, _mutate(valid_destination, lambda p: p.update(judges=[])), "no-judges")
    add(
        Preferences,
        _mutate(valid_preferences, lambda p: p["items"][0].update(strength="overwhelming")),
        "bad-strength",
    )
    add(
        CartographerReport,
        _mutate(
            valid_cartographer_report,
            lambda p: p["angles"][0].update(relevance_prior=0.8),
        ),
        "raw-angle-numeric-prior-forbidden",
    )
    add(
        CartographerReport,
        _mutate(valid_cartographer_report, lambda p: p.update(angles=p["angles"][:2])),
        "too-few-angles",
    )
    add(
        CartographerReport,
        _mutate(
            valid_cartographer_report,
            lambda p: p["strategic_notes"][0].update(kind="vibe"),
        ),
        "strategic-note-bad-kind",
    )
    add(
        CartographerReport,
        _mutate(
            valid_cartographer_report,
            lambda p: p.update(strategic_notes=[_strategic_note() for _ in range(4)]),
        ),
        "too-many-strategic-notes",
    )
    add(
        CoverageReport,
        _mutate(
            valid_coverage_report,
            lambda p: p["strategic_notes"][0].update(source_heuristics=["astrologer"]),
        ),
        "strategic-note-unknown-heuristic",
    )
    add(
        AngleMap,
        _mutate(valid_angle_map, lambda p: p["angles"][0].update(relevance_prior=1.3)),
        "prior-out-of-range",
    )
    add(
        AngleMap,
        _mutate(
            valid_angle_map, lambda p: p["dedup_map"][0].update(merged_into="nonexistent-angle")
        ),
        "dedup-dangling-ref",
    )
    add(
        CoverageReport,
        _mutate(
            valid_coverage_report,
            lambda p: p["contributions"].update({"astrologer": ["ml-systems"]}),
        ),
        "unknown-heuristic",
    )
    add(
        AllocationTable,
        _mutate(valid_allocation, lambda p: p["rows"][0].update(units=9)),
        "budget-not-conserved",
    )
    add(
        OptionCardSet,
        _mutate(valid_card_set, lambda p: p["cards"][0].pop("mechanism")),
        "missing-mechanism",
    )
    add(
        OptionCardSet,
        _mutate(valid_card_set, lambda p: p["cards"][1].update(angle_id="ml-systems")),
        "card-angle-mismatch",
    )
    add(
        CardCritique,
        _mutate(valid_critique, lambda p: p.update(redundancy_pct=140.0)),
        "redundancy-over-100",
    )
    add(
        Rubric,
        _mutate(valid_rubric, lambda p: p["criteria"][0]["levels"].pop(5)),
        "levels-missing-5",
    )
    add(
        Rubric,
        _mutate(valid_rubric, lambda p: p["criteria"][0].update(weight=0.10)),
        "weights-dont-sum",
    )
    add(
        ScreeningResult,
        _mutate(
            valid_screening, lambda p: p["options"][0]["criterion_scores"][0]["band"].update(lo=4.8)
        ),
        "band-lo-above-hi",
    )
    add(
        ScreeningResult,
        _mutate(
            valid_screening, lambda p: p["options"][0]["criterion_scores"][2].update(score=5.0)
        ),
        "score-outside-band",
    )
    add(
        Shortlist,
        _mutate(valid_shortlist, lambda p: p.update(finalist_ids=["induction-head-extension"])),
        "finalists-mismatch",
    )
    add(
        Shortlist,
        _mutate(valid_shortlist, lambda p: p["decisions"][0].update(cause="below-threshold")),
        "advance-with-cut-cause",
    )
    add(
        Dossier,
        _mutate(valid_dossier, lambda p: p.update(budget_capped=True, open_questions=[])),
        "capped-without-open-questions",
    )
    add(
        Dossier,
        _mutate(valid_dossier, lambda p: p["failure_modes"]["claim_ids"].append("ghost-claim")),
        "section-dangling-claim",
    )
    add(
        VerificationReport,
        _mutate(valid_verification, lambda p: p["results"][0].update(verdict="maybe")),
        "bad-verdict",
    )
    add(
        Prosecution,
        _mutate(valid_prosecution, lambda p: p.update(new_evidence=p["new_evidence"] * 4)),
        "too-many-searches",
    )
    add(
        FrameCheck,
        _mutate(valid_frame_check_gap, lambda p: p.update(proposal=None)),
        "gap-without-proposal",
    )
    add(
        GateADecision,
        _mutate(valid_gate_a, lambda p: p.update(rerun_hint="look at theory angles")),
        "approve-and-rerun",
    )
    add(
        GateBDecision,
        _mutate(valid_gate_b, lambda p: p.update(preference_slot_weight=0.6)),
        "slot-weight-over-sweep-range",
    )
    add(
        GateCDecision,
        _mutate(valid_gate_c, lambda p: p.update(approved=True)),
        "approve-with-pending-feedback",
    )
    add(SourceRecord, _mutate(valid_source_record, lambda p: p.update(tier="T4")), "bad-tier")
    add(
        ContradictionLedger,
        _mutate(valid_ledger, lambda p: p["entries"][0].update(resolution=None)),
        "adjudicated-without-resolution",
    )
    add(
        RunState,
        _mutate(valid_run_state, lambda p: p.update(pending_gate=None)),
        "gate-pending-without-gate",
    )
    return cases


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,factory", VALID_CASES, ids=[m.__name__ for m, _ in VALID_CASES])
def test_valid_fixture_validates(model, factory):
    instance = model.model_validate(factory())
    assert isinstance(instance, model)


@pytest.mark.parametrize(
    "model,payload,tag",
    _invalid_cases(),
    ids=[f"{m.__name__}-{tag}" for m, _, tag in _invalid_cases()],
)
def test_invalid_fixture_rejected(model, payload, tag):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model,factory", VALID_CASES, ids=[m.__name__ for m, _ in VALID_CASES])
def test_yaml_round_trip(model, factory):
    original = model.model_validate(factory())
    restored = model.load_yaml(original.dump_yaml())
    assert restored == original


def test_yaml_preserves_field_declaration_order():
    import yaml

    brief = Brief.model_validate(valid_brief())
    dumped_keys = list(yaml.safe_load(brief.dump_yaml()).keys())
    assert dumped_keys == list(Brief.model_fields.keys())


def test_run_state_json_round_trip(tmp_path):
    state = RunState.model_validate(valid_run_state())
    path = tmp_path / "state.json"
    state.to_json_file(path)
    assert RunState.from_json_file(path) == state


def test_yaml_file_round_trip(tmp_path):
    amap = AngleMap.model_validate(valid_angle_map())
    path = tmp_path / "map.yaml"
    amap.to_yaml_file(path)
    assert AngleMap.from_yaml_file(path) == amap


# ---------------------------------------------------------------------------
# Model behaviour
# ---------------------------------------------------------------------------


def test_verification_pass_rate_is_computed():
    report = VerificationReport.model_validate(valid_verification())
    assert report.pass_rate == pytest.approx(2 / 3)


def test_run_state_spend_aggregations():
    state = RunState.model_validate(valid_run_state())
    assert state.spend_by_stage() == {"S1": pytest.approx(0.42), "S3": pytest.approx(1.10)}
    assert state.spend_by_role()["scout"] == pytest.approx(1.10)
    assert state.total_usd() == pytest.approx(1.52)


def test_frame_check_pass_variant_forbids_proposal():
    payload = valid_frame_check_gap()
    payload["verdict"] = "pass"
    with pytest.raises(ValidationError, match="must not carry"):
        FrameCheck.model_validate(payload)
    payload["proposal"] = None
    payload["missed_options_check"]["consequential"] = False
    assert FrameCheck.model_validate(payload).proposal is None


# ---------------------------------------------------------------------------
# Error formatter (fed verbatim to LLM agents on retry)
# ---------------------------------------------------------------------------


def test_error_formatter_is_actionable():
    payload = valid_angle_map()
    payload["angles"][0]["relevance_prior"] = 1.5
    del payload["angles"][0]["definition"]
    payload["angles"][0]["shiny"] = "very"
    try:
        AngleMap.model_validate(payload)
        pytest.fail("expected ValidationError")
    except ValidationError as err:
        message = format_validation_error(err, AngleMap)
    assert "AngleMap" in message
    assert "angles[0].relevance_prior" in message
    assert "a number <= 1" in message
    assert "angles[0].definition" in message
    assert "required field is missing" in message
    assert "angles[0].shiny" in message
    assert "notes" in message  # points the agent at the escape hatch
    assert "ctx" not in message  # no raw pydantic internals


def test_error_formatter_surfaces_custom_validator_messages():
    payload = valid_frame_check_gap()
    payload["proposal"] = None
    try:
        FrameCheck.model_validate(payload)
        pytest.fail("expected ValidationError")
    except ValidationError as err:
        message = format_validation_error(err, FrameCheck)
    assert "re-divergence proposal" in message
    assert "Value error" not in message  # prefix stripped


# ---------------------------------------------------------------------------
# JSON Schema exports
# ---------------------------------------------------------------------------


def test_exported_schemas_are_fresh():
    problems = check_exports()
    assert problems == [], (
        "schemas/ exports do not match the models — run `make schemas` and commit: "
        + ", ".join(problems)
    )


def test_export_all_and_check_round_trip(tmp_path):
    export_all(tmp_path)
    assert check_exports(tmp_path) == []
    assert len(list(tmp_path.glob("*.schema.json"))) == len(ARTIFACT_REGISTRY)
    # Tampering is detected
    victim = tmp_path / "rubric.schema.json"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert check_exports(tmp_path) == ["stale: rubric.schema.json"]
    # Orphans are detected and then cleaned up by export_all
    (tmp_path / "old-artifact.schema.json").write_text("{}", encoding="utf-8")
    assert "orphaned: old-artifact.schema.json" in check_exports(tmp_path)
    export_all(tmp_path)
    assert check_exports(tmp_path) == []


def test_registry_models_are_package_exports():
    import deeper.schemas as pkg

    for name, model in ARTIFACT_REGISTRY.items():
        assert model.__name__ in pkg.__all__, (
            f"registry artifact '{name}' ({model.__name__}) missing from package exports"
        )
