# Semantic regression evals

This directory is intentionally outside `tests/`. Pytest checks deterministic program
behavior; these evals measure the non-deterministic semantic behavior of the real model.
They do not add fixture-specific rules to production code.

The default baseline contains eight synthetic cases covering conflicting notification limits, purchase versus
processing dates, HTTP 200 error envelopes versus HTTP 400, a missing read operation,
missing data dependencies, numeric endpoint inclusion resolved in another section,
an observable event-stage ambiguity, and a clean valid requirement. Each oracle names the
source passage set that must ground a finding; it does not prescribe finding prose.
Case descriptions and oracle passage names never enter the model payload: documents use
a neutral title and opaque stable document/block IDs, which are mapped back only while
scoring.

## Official report regressions

The separate `official-regressions` suite uses six synthetic cases based on the
[official report review](../HANDOFF.md#official-report-review). Its five positive
cases cover a second notification after a threshold change, purchase attribution
across months, missing read and monthly-spend contracts, the purchase lifecycle
trigger, and the data needed to attribute a refund. The clean control specifies
an integer request amount and a string response amount with a matching JSON example.

```powershell
python -m evals.run --mode check --suite official-regressions
python -m evals.run --mode live --suite official-regressions --output official-regressions.json
```

The live control also runs extraction and individual review. Its `control_passed`
field requires the response field to have been reviewed and zero confirmed
findings. `individual_review_passage_reviewed` distinguishes a clean result from an
extraction miss. The remaining cases exercise cross-specification review.

## Offline check

From the repository root:

```powershell
python -m evals.run --mode check
```

This validates case IDs, passage references, and exact source resolvability. It makes no
network call and explicitly reports that it is not a model-quality result.

## Controlled live run

Place `GEMINI_API_KEY` and optional settings in the repository-root `.env`, or export
them in the process environment, then deliberately run from the repository root:

```powershell
python -m evals.run --mode live --output eval-result.json
```

For a repeatable controlled subset, repeat `--case` in the desired order:

```powershell
python -m evals.run --mode live --case conflicting_limits --case clean_valid_requirement --output eval-result.json
```

The live mode uses `GeminiClient`, `CrossRequirementReviewer`, and `FindingsAggregator`,
so it exercises the same broad review, dependency tracing, focused scenarios
and source-passage comparisons as the application. It also exercises exact source
grounding, one repair attempt for invalid cross-review quotes, final evidence
verification, duplicate handling, and rejection. The
amount-type control also uses `RequirementsExtractor` and `IndividualReviewer`.
Configuration precedence matches
the application CLI: process variables override values in `cwd/.env`. A live run makes
multiple paid/external calls and is never triggered by pytest or offline mode.

Interpret the JSON as follows:

- `detected_with_expected_passages` requires the right criterion and all passages from
  one acceptable oracle set. Exact finding wording is not scored.
- `unexpected_findings` are verifier-approved findings that matched no oracle. On the
  resolved-boundary and clean cases, every approved finding is unexpected.
- `rejected_candidates` exposes grounding and final-verifier rejections, including
  rejected false positives on clean cases.
- `candidate_metrics` reports raw, grounded, and ungrounded candidates. Its
  `overlapping_evidence_candidate_count` only means that extra proposals share a
  criterion and passage set; they may still be distinct claims. `merged_candidate_count`
  is the number actually consolidated by aggregation. `verified_finding_count` reports
  what survived.
- `accepted_findings` contains every verifier-approved finding, including exact evidence
  text, so manual adjudication does not depend on the abbreviated summary.
- The omission cases are marked `manual_review`. Passage retrieval is measured, but
  a person must confirm that the claimed missing operation/dependency is genuinely
  necessary and not merely a preferred design. Record that decision alongside the JSON
  before using the run as a release gate.

For regression tracking, pin `GEMINI_MODEL`, retain the raw JSON artifact, repeat a
candidate model/configuration enough times to expose variance, and compare automated
detection, evidence-overlap signals, actual merges, verifier rejections, unexpected findings, and the
completed manual decisions.
Do not compare offline `check` output to live scores.

If the provider fails during a case, the JSON has `status: incomplete`, preserves every
previously completed case without null placeholder metrics, names the interrupted case,
and includes a sanitized provider error. The command exits with status 2. Re-run that
case with `--case`; do not treat an incomplete artifact as a completed regression run.
