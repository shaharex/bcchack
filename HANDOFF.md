# Validation handoff

## Project to-do list

- [x] Save the official-input Gemini run as [report_validated.md](reports/report_validated.md) and keep [report.md](report.md) as the historical baseline.
- [x] Review the latest report against the six scenarios in the expert review's critical-defects summary. Record the results and check the cited quotations against the original text.
- [x] Add regression cases for the five missed official scenarios and the explicit response-type control. The [evaluation suite](evals/README.md#official-report-regressions) passes its offline check.
- [x] Run the six new regression cases with pinned Gemini Flash-Lite and save the [results](reports/official_regressions_flash_lite.json). The first two cases completed before a rate limit, and the remaining four completed in a follow-up run.
- [x] Add dedicated data-source review and an exact-quote repair attempt. The [targeted rerun](reports/official_regressions_flash_lite_targeted.json) found the monthly-spend and refund gaps with source evidence. The amount-type control passed.
- [ ] Improve detection and classification of the purchase lifecycle trigger. Flash-Lite produced related findings but the expected ambiguity and evidence did not match in the rerun.
- [x] Verify extraction roles for the API contract and response examples. The [coverage run](reports/api_extraction_coverage_flash_lite.json) classified all five previously excluded blocks as analyzed.
- [x] Repeat the lifecycle fixtures and clean controls with pinned Flash-Lite. Save the [official follow-up](reports/official_regressions_flash_lite_followup.json), [targeted rerun](reports/official_regressions_flash_lite_targeted.json) and [historical controls](reports/lifecycle_controls_lite.json).
- [ ] Complete the same-evidence comparison with a more capable Gemini model. Four attempts stopped before scoring because of provider errors. See the run records below.
- [x] Remove 189 generated pytest artifacts from Git tracking, ignore future test-output directories and commit the retry/resume changes with their tests and documentation.

## Official report review

The latest [official report](reports/report_validated.md) used Gemini Flash-Lite and extracted 42 items. It lists three confirmed findings and three rejected candidates. All eight quotations in the confirmed findings match their cited source offsets. The report labels the agent `v0.2.0`. Record the exact commit in future comparison runs.

The handoff previously referred to six scenarios without listing them. This review uses the six IDs in the [expert review's critical-defects summary](reports/claude/report_requirements_review.md#5-сводка) as the reference set. On that basis, the latest report detected **one scenario and missed five**.

| Reference | Evidence in the official specification | Latest report |
| --- | --- | --- |
| D-20 · Second notification after changing the threshold | L39 caps notifications at one per card per month. L40 calls for a notification on reaching a new threshold regardless of the previous setting's history. After a first notification and a threshold increase, the two rules imply different results. | Missed. The requirements map includes both rules. |
| D-33 · Purchase and posting in different months | L38 assigns spend by purchase date in Almaty. The January 31 purchase posted February 1 in L64 increases the February counter. Specify the purchase timestamp's timezone and settle which date governs. | Missed. The requirements map includes both the rule and the acceptance case. |
| D-45 · Invalid amount returns HTTP 200 or 400 | L163 assigns 400 to invalid parameters. L165–L171 puts an invalid-amount error example under 200. L121 also specifies an `ERROR` result when an amount is rejected. | Found as `F-df4d182b92fade28c2f2c46e`. The example's heading establishes that the quoted error payload is shown under 200. |
| D-29 · Reading settings and monthly spend | L29 shows settings and monthly spend. L52 requires a settings read through Part B. L103 mentions reading, but L108–L120 document the save operation. The read contract and source for monthly spend are unspecified. | Missed. The report recognizes the save operation without flagging the missing read contract. |
| D-15 · Purchase lifecycle trigger | L38 counts posted purchases and L43 names the processing event. L23 sets the goal of informing the customer at purchase time, while L46 starts the dispatch deadline at event receipt. The contract needs to identify the lifecycle stage that triggers accounting and dispatch. | Missed. The report extracts the event and timing statements without resolving their relationship. |
| D-26 · Inputs needed to attribute returns | L38 requires a return to adjust the original purchase's month. L43 and L49 leave the source of that relationship and the original purchase date unspecified. The API already names an `enabled` field at L144 and L157, so the actionable gap is the provenance of required data. | Missed. The report extracts the return rule and stored fields without linking the missing input to the accounting decision. |

### Other findings and coverage

- **GET used to save settings.** Retain `F-be13e7f22f1413ebd9af3eb1` as an API design concern, consistent with expert finding D-36. L104 and the request example at L109 both use GET. L120 explicitly changes state. The declared and example verbs agree.
- **Response amount type.** Reclassify `F-3217baa39fde2081b85422f2` as a false positive for ambiguity. L143 defines an integer request field, L156 explicitly defines a string response field and L167 demonstrates the string. A request and response may specify different types.
- **Rejected candidates.** The 200/201 classification and `200 … Created` heading merit a documentation clarification. The text gives no conflicting 201 outcome for the same request. The 403 heading contains a specific 404 rule for a foreign or missing card at L189–L193. Keep that outcome and clarify when 403 applies, if it is used.
- **Coverage.** The map includes the relevant functional rules, sequence, timing, data, integration and acceptance passages. The coverage ledger labels all of L101–L114 as metadata even though it contains the method and request example. It also labels the 400, 401, 404 and 500 payloads as metadata. A targeted extraction-only run on those five blocks moved all five to analyzed and extracted nine specification facts. This comparison covers the contract block and four response bodies. [Coverage results](reports/api_extraction_coverage_flash_lite.json)

## Official regression suite results

All six synthetic regression cases completed on `gemini-3.5-flash-lite`. The [saved JSON](reports/official_regressions_flash_lite.json) preserves the case results and the rate-limit interruption between attempts.

| Case | Result |
| --- | --- |
| Second notification after a threshold change | Detected with the expected citations. |
| Purchase and posting in different months | Detected with the expected citations. |
| Settings read and monthly spend | Partial. The confirmed finding correctly flags the missing settings read contract. It does not flag the missing source for monthly spend. |
| Purchase lifecycle trigger | Missed during candidate generation. Zero candidates reached grounding. |
| Refund attribution | The model proposed the missing purchase link. Grounding rejected the candidate because the quote did not match the refund-event block. |
| Amount response type | Passed. Individual review covered the response field, and no finding was confirmed. |

The harness scored two of three automatically scored issues and retrieved one of two cases requiring manual review. There were zero unexpected confirmed findings. The manual assessment above records the scope of the settings-read finding and the reason the refund candidate could not be confirmed.

### Follow-up after review changes

The [six-case follow-up](reports/official_regressions_flash_lite_followup.json) detected both existing conflicts, confirmed the refund gap with exact source evidence and passed the amount-type control. Its lifecycle findings used the completeness criterion, so they did not match the expected ambiguity. The [historical lifecycle rerun](reports/lifecycle_controls_lite.json) produced no candidate. Both clean controls in that run had zero confirmed findings.

A subsequent [four-case rerun](reports/official_regressions_flash_lite_targeted.json) used the dedicated data-source pass. Manual review confirmed separate findings for the missing settings read and the missing source of monthly spending. The refund gap cited the actual event fields, and the amount-type control passed. Flash-Lite reported a timing conflict between the purchase goal and the dispatch deadline. That finding did not cite the named transaction stages or distinguish which stage triggers notification, so the lifecycle ambiguity remains open.

The stronger-model attempts completed zero cases. Gemini 3.5 Flash and 3.7 Flash returned HTTP 503 after retries. Gemini 3.1 Pro Preview returned HTTP 429 with a retry interval longer than the configured wait budget. Gemini 2.5 Pro returned HTTP 404. No comparative score is available. [3.5 Flash](reports/lifecycle_controls_flash.json) ? [3.7 Flash](reports/lifecycle_controls_flash_37.json) ? [3.1 Pro Preview](reports/lifecycle_controls_pro.json) ? [2.5 Pro](reports/lifecycle_controls_pro_25.json)

## Baseline and reproducible checks

Keep the root `report.md` as the historical comparison run. Use `reports/claude/report.md` for the earlier Gemini run. The Opus and Sonnet reviews use different unit boundaries.

| Artifact | Units | Confirmed or author-reported findings |
| --- | ---: | ---: |
| Historical Gemini baseline | 24 | 4 |
| Earlier Gemini | 23 | 17 |
| Opus reference | 44 | 57 |
| Sonnet reference | 37 | 61 |

The historical baseline map represents seven section paths. All four findings quote matching source spans. Two findings request a way to detect duplicate deliveries and cite the same sentence. L52 already states that repeat delivery preserves the total. In the older run, two recommendations ask for numeric bounds already given in the document.

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

## Repeating the evaluation

Use `python -m evals.run --mode check` for the offline fixtures and the controlled live procedure in [evals/README.md](evals/README.md) for model comparisons. Examine the two clean controls, proposed omission findings and duplicates manually. An incomplete provider run supplies measurements only for its completed cases.

When repeating the official run, record the model and commit, and use a new output path so the saved reports remain comparable.
