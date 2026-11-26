# Validation handoff

## Baseline and reproducible checks

The latest artifact is the root `report.md`. Keep it as the comparison run. Use `reports/claude/report.md` for the earlier Gemini run. The Opus and Sonnet reviews use different unit boundaries.

| Artifact | Units | Confirmed or author-reported findings |
| --- | ---: | ---: |
| Latest Gemini | 24 | 4 |
| Earlier Gemini | 23 | 17 |
| Opus reference | 44 | 57 |
| Sonnet reference | 37 | 61 |

The latest map represents seven section paths. All four finding quotations match the cited local source spans. Two findings request a way to detect duplicate deliveries and cite the same sentence. The specification already states the observable idempotency outcome. The six requested reference scenarios are absent from its confirmed list. In the older run, two recommendations ask for numeric bounds already given in the document. These are manual comparisons. They do not establish a population false-positive rate.

## Live synthetic evaluation

Two component runs used `gemini-3.5-flash-lite`, the same eight fixtures, and the same passage-level scoring. Each fixture was presented without its oracle labels. The first run used broad and focused review. The second also compared selected passages and derived two observations for each test case.

| Check | First run | With source-passage comparisons |
| --- | ---: | ---: |
| Six expected issues matched on criterion and cited passages | 2 | 5 |
| Confirmed findings on the two clean controls | 0 | 0 |
| Raw proposals / merged duplicates / final findings | 6 / 3 / 3 | 11 / 6 / 5 |

The second run found the conflicting limits, accounting period, HTTP outcome, missing read operation and missing calculation input. It missed the ambiguous lifecycle trigger. In the first run two confirmed findings described the same month-boundary decision. Each detected issue appeared once in the second run. These single-run observations include model variance. They do not measure accuracy on the official input.

Manual review of both omission cases supports the underlying gaps. A write-only contract does not explain how the UI obtains a previously saved value. The proposed GET endpoint is one possible correction. An identifier and user threshold alone do not determine monthly purchases. Either a documented event source or a source of aggregate spending would supply the missing information.

The raw measurements are in [the first run](reports/synthetic_eval.json) and [the source-comparison run](reports/synthetic_eval_relations.json). The component harness exercises cross-review and verification. A separate [synthetic CLI run](reports/synthetic_pipeline.md) also exercises extraction, individual review, severity, questions and rendering.

A [targeted follow-up](reports/synthetic_eval_lifecycle.json) explicitly tested competing event milestones against a deadline. Flash-Lite still produced no candidate for that fixture, and the clean control remained clear. Review whether “completion” is an adequate definition of settlement for this domain, then compare the cross-review stage with a stronger pinned model using the same passages and verification policy. The production model setting was preserved.

## Remaining evaluation

1. Run the official input through the CLI once Gemini access for this file is approved. Pin the baseline model ID for comparison. Retain its new output separately.

   ```powershell
   $env:GEMINI_MODEL = "gemini-3.5-flash-lite"
   python -m requirements_validator --input mdfile/requirements.md --output reports/report_validated.md
   ```

2. Check each of the six reference scenarios against the original text and the new finding's exact citations. Record missed scenarios, credible new issues, repeated decisions and rejected candidates resolved by an explicit passage. Data provenance can be unspecified even when a viable implementation exists. The validator's coverage ledger accounts for parser blocks. Check that the role assigned to each substantive section is appropriate.
3. Use `python -m evals.run --mode check` for the offline fixtures. Use the controlled live procedure in [evals/README.md](evals/README.md) for model comparisons. Examine the two clean controls, all proposed omission findings and duplicates manually. An incomplete provider run supplies measurements only for its completed cases.
4. Consider a more capable model for cross-requirement reasoning only if the same evidence-backed omissions persist in repeated pinned runs. Retain the existing extraction, verification, severity and reporting stages for a controlled comparison.
