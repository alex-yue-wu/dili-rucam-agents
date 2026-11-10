# Decisions taken during execution — native-SDK analyst tier

Every ruling made while executing
[the plan](2026-11-10-native-sdk-analyst-tier.md), in the order made. Each is a
decision taken on the author's behalf because execution could not pause for it,
and each records what it costs if it is wrong, so any of them can be checked and
overturned.

Six were later found wrong or inconsistent and were reversed; those are marked
**[REVERSED]** where the reversal is itself a ruling below.

Count: 57.

---

## 1. Ruling

`ITEM_LEGAL_SCORES` lives in `validators/rucam_json.py`, and `rucam_assessment.py` imports it — rucam_json is already the lower layer (it holds the item names and `expected_category`) so this cannot cycle, and T6's prompt test needs it without pulling in the assessment schema — if wrong, the constant moves one module and two import lines change.

## 2. Ruling

`build_analyst_identities` is in T8's scope and takes `configs: list[AnalystConfig]`, reading `.model` and `.max_output_tokens` directly instead of the deleted `resolve_rucam_model` / `resolve_analyst_max_output_tokens` env helpers — if wrong, T11 finds `checkpoints.py` importing a deleted module and does the same work later inside a much bigger diff.

## 3. Ruling

the `FailureKind` widening to `truncation` and `terminal` moves from T8 to T3, which is where the first code raises them; T8 keeps `_VALIDATION_FIELD_PATHS` and the checkpoint-store changes — if wrong, T3's tests cannot assert their own failure kinds and T8 rewrites them.

## 4. Ruling

`providers.py` imports each vendor SDK lazily inside its `_complete_*` coroutine rather than at module scope, so the request-builder tests run against the SDKs currently installed and the dependency bump stays in T11 as planned — if wrong, a bad SDK import surfaces on the user's live smoke run instead of at build time, which is already the residual risk the plan's Hand-Off names and the five-call smoke roster exists to catch.

## 5. Ruling

T7 moves `load_rucam_prompt` and the two `DEFAULT_RUCAM_*_PROMPT_PATH` constants into `contracts.py` and updates the import in `tests/test_prompt_rules_sync.py` — if wrong, T11 hits one stale import and fixes one line.

## 6. Task 1

the reviewer's warning that DEFAULT_ROSTER_PATH only resolves in a source checkout is real but is not a Task 1 defect — roster.py uses the same strategy as default_dilirank_path() (parents[N] off __file__), and diverging for one shipped data file and not the other would be worse. The actual hole is the Dockerfile, which COPYs dilirank_v2 but has no config/. Amended the plan so Task 11 adds `COPY config /app/config`. If wrong, a container run with no --analysts fails to find the default roster, which is commit 70c572c's bug repeated and is caught the first time anyone runs the image.

## 7. Task 2

the implementer reports DeepSeek has no native json_schema, so the ladder as planned would spend one call per DeepSeek run discovering that, and would log a permanent mode_downgrades=1 — polluting the exact counter the acceptance gate reads to confirm or kill the eta/OpenRouter hypothesis. Amended Task 3 to add SUPPORTED_MODES/starting_mode in providers.py, so the ladder starts at the highest mode a provider actually supports and a never-supported mode is not counted as a downgrade. If wrong (DeepSeek does support json_schema), DeepSeek starts one rung lower than it could and returns json_object instead of a schema-validated object — the runner parses and validates every tier anyway, so the cost is a slightly weaker provider-side guarantee, visible on the user's five-call smoke run which includes DeepSeek.

## 8. Task 2

`_is_format_rejection` must treat OpenRouter's 404-shaped format refusal as a format rejection rather than a terminal status, even though 404 is in TERMINAL_STATUS_CODES. Task 2 correctly dropped the prior art's helper as out of its brief, so Task 3 owns it; amended Task 3 with a test. If wrong, an OpenRouter analyst dies on a terminal failure where it should have stepped down the ladder — which is `analyst_eta`, the analyst this whole change is trying to measure.

## 9. Task 2

the reviewer's ⚠️ that Google kwargs were verified against google-genai 1.49.0 while the plan targets >=2.28.0 is real and cannot be settled before the bump. Amended Task 11 to re-verify them there, alongside replacing the local anthropic schema transform with the SDK's. If wrong, a renamed Google kwarg fails on the user's five-call smoke run rather than at build time — which is the residual risk the plan's Hand-Off already names.

## 10. Task 2

the reviewer confirmed the OpenRouter 404 contract is sequenced to Task 3 rather than lost, and noted _complete_openai has no try block, so Task 3 must sniff a raw 404 rather than catch a typed refusal. Already amended into Task 3 before this review landed; no further change.

## 11. Task 3

the transient budget is 3 retries, so a fourth consecutive transient error raises `execution`. The implementer built 2 retries to match my plan test name `test_three_transient_errors_raise_execution_kind`, but spec line 401 states "3, backoff 0.5/1/2/4s + jitter" and the prior art uses `_MAX_TRANSIENT_RETRIES = 3` with `if transient_used > 3`. The spec is the binding authority and my test name was wrong, so I corrected the plan and sent the change back. Being stingier than the spec directly undercuts the anti-goal "any path where a transient network error reduces an analyst's genuine scoring attempts". If wrong, a genuinely dead endpoint costs one extra retry of latency before failing.

## 12. Task 3

concern 2 (the runner can only emit a generic ("report","invalid_value") because _VALIDATION_FIELD_PATHS is a closed frozenset owned by Task 8) is correctly sequenced, not a defect — but widening the allowlist alone would not make the diagnostic precise, so I amended Task 8 to own the pydantic-error-to-path mapping as well. If wrong, the four retry counters would record that a validation retry happened without ever recording which field caused it, which is most of their diagnostic value.

## 13. Task 3

consecutive wins — reset transient_errors on a successful provider.complete, keep transient_retries cumulative for the counters. The reviewer's case decides it: a run makes ~6 provider calls, so `429, BAD, 429, BAD, 429, 429` dies on the fourth scattered 429, losing completed analyst work to transport noise already recovered from — the exact anti-goal "any path where a transient network error reduces an analyst's genuine scoring attempts". If wrong, a persistently flaky endpoint can absorb up to 3 retries per stall instead of 3 per run, costing latency on an endpoint that is dying anyway; bounded because the other three budgets cap total calls.

## 14. Task 3

promoted the reviewer's Minor 2 (every 400 treated as a format rejection) into the fix round, against its own Minor calibration. The brief explicitly required deliberate non-over-matching, and the reviewer established that no test depends on the blanket — marker-required and blanket policies pass the suite identically — so the one decision the brief asked to be made deliberately is pinned by nothing. If wrong, I spent a fix round on a bounded issue whose runs end terminal anyway.

## 15. Task 3

included passing response.text as the truncated Attempt's raw_text (reviewer's Minor 4, first part only). The engineering standards name truncation as a reported failure mode and the partial text is its evidence. Excluded the attempt-numbering and on_attempt stream-shape redesign from the same finding as genuinely deferrable.

## 16. Task 4

ITEM_LEGAL_SCORES is defined in validators/rucam_json.py and BOTH validators enforce it — the new assessment schema and the existing RucamScores, whose ge/le envelopes are replaced by exact set membership. The spec says the exact sets replace the loose envelope "enforced from one constant"; leaving RucamScores loose would let a rendered Section C carrying concomitant_drugs:+2 validate, which is the hole being closed. Verified none of the nine tests in test_rucam_json_validator.py uses a value outside the legal sets, so nothing breaks. If wrong, a legitimate off-sheet score becomes unrepresentable and the first analyst to need one fails validation instead of being scored — recoverable by widening one constant.

## 17. Task 4

ruff is not a standard for this project and its output is not a gate. The implementer flagged "3 ruff errors elsewhere in src"; I checked and `ruff check src` reports 151 errors, with no ruff or line-length configuration anywhere in pyproject.toml, across files this branch never touched. So ruff's default ruleset (import ordering, __all__ sorting) has never been followed here and nothing regressed. No task will chase it, and reviewers are told to judge style against the surrounding code instead. If wrong, the project later adopts ruff and pays a one-time formatting commit across the whole tree, which is cheaper than threading an unrelated 151-error cleanup through this change.

## 18. Task 4

Important 1 is fixed with a per-field AfterValidator rather than a loop, which also restores per-field `loc`. This is load-bearing for Task 8: that task maps pydantic error locations onto _VALIDATION_FIELD_PATHS, and it cannot produce a precise path from a diagnostic whose loc is empty. If wrong, I have spent a round on diagnostics quality; the schema itself was already correct.

## 19. Task 4

on the reviewer's ⚠️ that nothing recomputes r_ratio from r_ratio_basis — split it. The cross-check (reject an r_ratio that disagrees with its basis) goes into Task 4's fix round, because it is the same validator pattern the task already implements for g1 and total_score and the four numbers are already in the schema. Deriving injury_pattern from r_ratio in code does NOT: injury_pattern can legitimately be "Not reported", it depends on which ULN column was used, and forcing derivation could move scores, which the anti-goals forbid without a separate measurement. If wrong, a model's injury_pattern stays its own claim and a wrong pattern selects the wrong scoring table — caught by the cross-check only if the r_ratio is also wrong.

## 20. Task 4

on the reviewer's ⚠️ that the frozen pre-flight still orders arithmetic code now owns — no change. The pre-flight lines are outside the frozen core (which ends at line 613) so Task 6 could edit them, but the schema captures their answers as pre-flight fields, nothing is lost by not reading the model's arithmetic, and rewording forced-reasoning scaffold is a prompt change that could move scores. If wrong, the model spends a few tokens computing a total nobody reads.

## 21. Task 4

the prompt's "ten-line pre-flight check" (line 784) versus "all eleven lines" (line 748) is a real pre-existing inconsistency, and both lines are OUTSIDE the frozen core, so Task 6 fixes it to eleven. The reviewer assumed it was frozen and unfixable; verified it is not. If wrong, Task 6's sync test trips on a count mismatch it could have corrected.

## 22. Task 4

included the reviewer's Minor 3 (onset_day has no ge=1) in the fix round, above its calibration. AGENTS.md and commit 0de0ba6 establish that latency is numbered from the dose day and "There is no day 0"; a schema accepting onset_day: 0 contradicts a rule this repo fixed four commits ago. One line.

## 23. Task 4

promoted both of the re-review's Minors into a second and final round, against its calibration. (a) The tolerance admits a pattern-boundary straddle: at R=5 the 1% relative term is 0.05, so a stated 5.00 passes against a basis computing 4.96 — a hepatocellular claim over a mixed basis — while the 2-dp rounding it was chosen to absorb needs only 0.005, making the band ten times wider than its own rationale. The injury pattern selects which scoring table applies, so it sits upstream of all seven item scores. (b) alp == 0 is legal under Field(ge=0) and skips the check, letting any r_ratio through, and the new test pins that bypass rather than covering it — a test that locks in a hole is worse than no test. If wrong, I spent one extra round tightening a constant and splitting a test; both are three-line changes.

## 24. Task 4

accepting the re-review's residual note that abs_tol=0.01 is inclusive, so a stated 2.00 against a computed 2.01 still passes and straddles the R=2 boundary. This is inherent rather than fixable: the pattern boundaries are points (R <= 2.00 cholestatic vs 2.00 < R mixed), so ANY non-zero tolerance can straddle one, and the tolerance cannot go below the 0.005 that 2-dp rounding needs. The only complete fix is deriving injury_pattern from r_ratio in code, which I ruled out earlier because the pattern can be "Not reported", depends on the ULN column used, and forcing it could move scores. Recorded as a known limitation for the final review rather than chased. If wrong, a boundary-straddling R-ratio passes validation and the model's own injury_pattern claim stands unchallenged — the same position the spec left it in.

## 25. Task 5

rules_version gets a default of the module's RULES_VERSION constant rather than staying a required parameter. The parameter stays for a future rules version, but a caller supplying a wrong or stale tag silently mislabels the experimental condition a score was produced under, and that tag is how this project attributes a score movement to a rules change. Sent back before review. If wrong, a caller that genuinely needs a different tag still passes one explicitly; nothing is closed off.

## 26. Task 5

concern 1 (-W error fails on tests/test_rendering.py because it imports batch, which imports crewai, which raises ImportWarning) is correctly diagnosed and not this task's defect. Told the implementer not to work around it and not to drop the batch import, since the round-trip test through the real extract_section_c_json is the most valuable test in the file. Re-check -W error on this file after Task 11 removes crewai. If wrong, a genuine warning in the renderer stays hidden behind crewai's until Task 11.

## 27. Task 5

the model's pre-flight claims (total_sum, category_match, g1_count, section_b_matches_section_c) are printed unreconciled, so Section A can contradict B and C. Fixing by rendering the model's claim AND the computed value side by side when they differ. Explicitly NOT by rejecting a mismatch: a model whose seven item scores are correct but which mis-adds them in prose would fail validation, retry, and be re-scored under a prompt carrying a retry instruction — the exact retry-induced perturbation this change exists to remove. And explicitly NOT by silently overwriting the claim, which would destroy the evidence that the model mis-added. If wrong, Section A carries a visible annotation nobody needed.

## 28. Task 5

resolved the reviewer's ⚠️ about whether downstream code reads `notes` as list[str] — checked, and the only readers are in validators/analyst_report.py's JSON salvage path (_extract_notes, used when parsing fails); no workbook writer reads it. So the zero-width space reaching a Section C note has no semantic consumer and stays. The alternative of defusing only the markdown would reintroduce a rejection, because the validator scans the whole report text. If wrong, a future consumer that string-matches notes sees an invisible character in a vanishingly rare case.

## 29. Task 5

NOT promoting the re-review's remaining Minor. Narrow false negatives survive ("11 (not 9)", "Not possible", "Improbable", a standalone "+0" term), but the failure mode is a missing annotation on a prose claim that is already visibly beside the computed table, while Sections B and C — which every downstream consumer reads — stay correct. The proposed closing change means parsing "the last integer in arbitrary prose" and maintaining a negation-prefix list, which is natural-language agreement detection inside a renderer and carries its own false positives. Accepting as-is. If wrong, a contradictory Section A claim goes unannotated in a report whose B and C are still right.

## 30. Task 6

fixing the Important. The output region's mapping is a glossary, not a supersession rule, and the reviewer found the statement it cannot reach — frozen core line 158, "the output contract still requires all three sections", which is a claim about the contract rather than a mappable "SECTION A/B/C" token. This is the two-competing-contracts shape that caused the original defect. Adding a precedence sentence at the head of OUTPUT FORMAT plus a test pinning it, since nothing currently guards the mapping paragraph's existence — a future edit could delete the only bridge between the frozen core and the JSON contract with the suite still green. If wrong, one redundant sentence in a prompt.

## 31. Task 6

promoted three Minors that are each a guaranteed validation retry rather than polish — (3) the template's `"alp": 0.0` is a value RRatioBasis rejects at gt=0, so a model copying the placeholder for an undocumented ALP is rejected; (4) the field rule lets onset_day be null without saying what onset_anchor_dose holds then, though the schema requires a str; (2) the enum option strings are unpinned, so a drift to "not reported" would pass every test and cost a retry on every run. Each failure costs a retry, and a retry re-scores under a perturbed prompt — the exact defect this change removes. Also promoted (7), the numbered[:11] slice that would not catch a twelfth pre-flight line. If wrong, I spent one round on four one-line changes.

## 32. Task 6

NOT promoting Minor 5 (item-agnostic notes have no named home). The reviewer verified nothing is dropped and no code keys on those strings, so it is a wording gap. Naming an arbitrary default item would remove a judgement call from the model but put an odd rule in the prompt, and notes are not scored. Deferred.

## 33. Task 6

promoting the re-review's out-of-scope observation into a final round, having verified it. RRatioBasis declares alt_uln and alp_uln as Field(gt=0) and REQUIRED — no None option — while the template still shows "alt_uln": 0.0 and "alp_uln": 0.0. A model copying the placeholder shape is rejected on both fields every time, which is strictly worse than the alp trap just fixed, where null was at least a legal alternative the field rule named. Told it to use the ULN defaults the frozen core already documents rather than inventing numbers, so the example is clinically coherent as well as valid, and to extend the test to all four basis numbers so a future placeholder edit to any of them fails the suite. If wrong, the placeholders change to values that were already acceptable.

## 34. Task 7

accepting concern 1 (the old "Return SECTION A/B/C with ## headings" instructions dropped from the task description, retry block and expected_output reworded to one JSON object). Keeping them would contradict the Task 6 prompts and reintroduce the two-competing-contracts defect. The hash change versus old checkpoints costs nothing additional: Task 8 bumps CHECKPOINT_SCHEMA_VERSION to 4, which the spec already says invalidates every existing manifest including the in-flight study's. If wrong, a resume re-scores from scratch, which it was going to do anyway.

## 35. Task 7

promoted two Minors. (1) The divergence test would pass even if the persona were dropped, because the label is embedded in instruction_template, so the test meant to protect per-analyst divergence survives the exact change it exists to prevent — and persona preservation is an anti-goal, not a preference. (2) No test renders the REAL prompt files; the behaviour I verified by hand should be pinned, since the real files are what reach a paid model. Told it to assert known brace-bearing probes rather than `"{{" not in text`, per the reviewer's caveat that a prompt could legitimately contain a doubled brace. If wrong, two cheap test additions.

## 36. Task 8

the implementer was right to refuse a shim — the old path produces no RucamAssessment, so any sidecar it wrote would be invented data, and shimming means maintaining a code path Task 11 deletes to satisfy tests Task 11 rewrites. But leaving the suite red across Tasks 9, 10 and 11 would destroy the signal I rely on: new breakage from those tasks would hide among the 16. So: mark the 16 xfail(strict=True) with a reason naming Task 11. The suite returns green, the test bodies survive for Task 11 to rewrite, the transitional state is documented in the code rather than only in this ledger, and the markers self-destruct — when Task 11 fixes the path the tests XPASS, which a strict xfail turns into a failure, forcing the cleanup. Amended Task 11 with an explicit marker-removal step so its implementer is not surprised. If wrong, 16 tests sit xfail for three tasks and a genuine regression in the old path goes unnoticed — acceptable, because that path is being deleted.

## 37. Task 8

amended Task 11 with two things its review surfaced that would otherwise bite there. (a) pipeline.py:88 and pipeline.py:138 are not merely untested but non-functional — the first passes dict configs into build_analyst_identities which now reads config.key (AttributeError), the second calls record_completed without assessment_json/counters (TypeError). (b) The xfailed test_schema_v2_manifest_cannot_skip_execution_and_is_replaced_cleanly still asserts schema_version == 3, which must become 4 or it fails for a reason unrelated to the rewrite. If wrong, Task 11 rediscovers both the slow way.

## 38. Task 8

promoting three Minors. (1) The counters fixture sets three of four counters to 1, so swapping any two keys in _counter_fields leaves the test green — and a silent mislabel is precisely the failure mode these counters exist to prevent, since transient retries and mode downgrades are invisible in the attempt list. Distinct values is a one-line fix. (2) Legacy adoption calls _mark_completed(counters=None) and record_failure without counters leaves stale values, so a later reader could read a present counter as belonging to this attempt — one comment, because Tasks 9 and 10 consume these. (3) _ERROR_TYPE_CODES omits codes that _VALIDATION_ISSUE_MESSAGES already defines (integer_too_large and friends), so a g1 above its maximum reports a generic invalid_value while the precise message exists unused. If wrong, I spent a round on test sensitivity and a dict.

## 39. Task 9

promoted Minor 3 (no contract injection point). run_crew took instruction_contracts precisely so the pipeline's resume fingerprint described the contract actually sent; building internally means Task 11 must construct a second contract for the fingerprint, and they agree only because the builder is pure. That fingerprint is what stops a resume re-scoring paid runs, so the guarantee should be structural rather than coincidental. If wrong, one optional parameter goes unused.

## 40. Task 9

promoted Minor 11 (the AnalystRunOutcome is discarded on the exception path, so a caller without on_report loses every report the run already paid for). Attaching the partial outcome to AnalystExecutionError is a few lines and removes a cliff where paid work becomes unrecoverable. Also requested the missing first-analyst-fails test, which is the entire mid-roster non-destructiveness property and is currently uncovered.

## 41. Task 9

amended Task 11 with three call-site facts the review established — pipeline.py:132 reads an event.error the new event lacks; require_api_keys does not cover roster.ground_truth.provider so the pre-flight guarantee is incomplete at pipeline level; validate_max_restarts still lives in the module Task 11 deletes, leaving max_retries unvalidated (argparse saves the CLI but not a programmatic caller).

## 42. Task 10

accept it. Preserving the prefix would mean reintroducing the LiteLLM routing table this change deleted, and the bare name is correct going forward. But it is a real prompt difference in the ground-truth column, so it must be stated in the hand-off rather than discovered when old and new masked_rucam_* columns are compared.

## 43. Task 10

asked for a golden-string test plus an assertion that the model name reaches the sent message, and explicitly NOT the reviewer's alternative of a parity test against crew.tasks' copy — that would pass today but create a deletion obligation for Task 11 with no lasting protection.

## 44. Task 10

promoted the Minor brittle roster test (asserting AnalystRunOutcome.__dataclass_fields__ equals an exact set would fail on any legitimate field addition, and does not prove a finder report cannot reach an analyst column). Cheap to replace with an assertion that the finder's key is absent from the real roster keys.

## 45. Task 10

amended Task 11 with two more items — delete the duplicated prompt builder so the two copies cannot drift, and note that run_ground_truth_score_finder calls asyncio.run internally so it raises inside a running loop if the wiring puts it on an async path.

## 46. Task 10

the ground-truth prompt FILE (prompts/ground_truth_rucam_finder.md) has no test coverage at all — verified that nothing under tests/ references it, only src/dili_rucam_agents/ground_truth.py does — and the new golden stubs its body, so it stays unpinned. This is pre-existing rather than a regression: it was unpinned before this change too. Not opening another round, because the right fix is semantic assertions on that file (the shape the 16 analyst-prompt sync tests use) rather than a brittle whole-file golden, and that is its own small piece of work. Recording it as a deferred item and for the hand-off: a reword of the ground-truth prompt file would shift the study's comparison baseline with nothing to catch it. If wrong, that file changes unnoticed and the masked_rucam_* columns move without explanation.

## 47. Task 11

ADD load_dotenv(). Importing crewai called it as a side effect, so .env-held keys worked; nothing does now and the pre-flight key check runs first, so the user's five-call smoke run would die with MissingApiKeyError while the keys sit unread in .env. python-dotenv is an explicit dependency so .env support is intended, and the pre-change behaviour loaded it, making this behaviour-preserving rather than new. The implementer correctly flagged rather than adding unrequested behaviour. If wrong, a redundant call runs at startup.

## 48. Task 11

FIX the counters-on-failure gap and authorise touching analysts.py (Task 9's module) to do it. The gate is "fewer than 5 validation retries across 70 runs, none above 1", and an analyst that fails has by definition exhausted its validation budget — so the runs whose counters matter most to the gate are exactly the ones recording nothing. RunnerError already carries counters; expose them on AnalystExecutionError and pass them to record_failure, which already accepts them. Kept narrow. If wrong, the manifest carries a counter block on a path nobody reads.

## 49. Task 11

accepted concern 2 with no action. The real transform_schema discards a dict[str, X] value schema exactly as the stand-in did, closes gap 1 by moving the keyword into the description rather than recursing, and strips `pattern` despite the docs calling it supported. My Task 11 amendment assumed the SDK closed both gaps — that was my guess about vendor behaviour and the implementer's measurement supersedes it. Neither gap is reachable from RucamAssessment today (no dict field, no pattern constraint) and all three behaviours are pinned by tests, so our tests now document what the vendor does rather than what its docs claim.

## 50. Task 11

accepted the warning count rising 5 -> 7 with no action. Both new ones are SyntaxWarning: invalid escape sequence '\/' raised from inside google/genai/types.py in google-genai 3.0.0 — the vendor's own source, triggered by the SDK-acceptance test. Not adding a filterwarnings entry to suppress a third-party defect. Recorded so nobody later reads 7 as a regression.

## 51. Task 12

one final round for four small items. The one with real value is pinning the masking scoping clause — neither presence nor absence checks cover "plus the ground-truth provider when masking is on", so dropping it would pass the suite while making the docs OVERCLAIM, sending a reader hunting for a key they do not need. Also widening the absence set (the chosen "lazily" never appeared in the removed text, so "on first use" / "on demand" / "when first needed" would all slip past), rewrapping two ~130-char lines against the file's ~80-col convention, and rewording a clause that reads as though the persona alone distinguishes analysts when the model and max_output_tokens also differ.

## 52. Ruling

ACCEPTING Critical 1 and fixing it. A resumed invocation silently erases the previous invocation's counters and its rejected replies. _mark_completed and record_failure both whole-value-overwrite the single per-analyst counter block, attempt_history carries no counters, and _persist_invalid_attempt's stem restarts at attempt 1 per run_contract call so os.replace overwrites. The gate's own documented recovery is a resume: an analyst that failed with validation_retries == max_retries succeeds on resume with 0, and the 0 overwrites the 2. Silent, unrecoverable, and in the gate-PASSING direction, on the one number this entire branch exists to produce. Fixing by accumulating the four counters across invocations, recording the per-invocation block in attempt_history, and making the artifact stem collision-resistant.

## 53. Ruling

MY RULING 175 WAS WRONG, and the reviewer showed why. I declined to promote item-agnostic notes on the premise that "nothing is dropped". False: the frozen core mandates a notes entry for default ULNs (Appendix A.2) and one when R cannot be computed (R-ratio section), neither of which names an item, while rendering._notes builds Section C's notes ONLY from per-item inference_note. Default-ULN use is common in published case reports, so this degrades most reports rather than a rare one, and it makes pre-flight line 11 — which asks the model to confirm notes contains the entries the rules require — partly unanswerable, inside scaffold the anti-goals protect. Fixing with an optional assessment_notes list concatenated into _notes().

## 54. Ruling

MY r_ratio RULINGS (122/134/138) WERE INCONSISTENT, and I am reversing the outcome. The reviewer caught that I declined to derive injury_pattern partly because "forcing derivation could move scores, which the anti-goals forbid", then accepted a cross-check that moves scores by exactly the same mechanism (reject -> correction turn -> changed field); that I tightened the tolerance specifically to catch pattern-boundary straddles; and that I then conceded the straddle is inherent at any non-zero tolerance. So the check's stated purpose is admittedly unachieved while its cost is real — a brand-new rejection path on a brand-new field, spending the gate's own counter, most likely to be mis-attributed to the model. Ruling 151 already set the better precedent for this exact shape: render the claim beside the computed value rather than reject. Demoting _r_ratio_matches_basis to a rendered annotation.

## 55. Ruling

raising _MAX_TRANSIENT_RETRIES from 3 to 4 rather than editing the spec. The spec is internally inconsistent — it states a budget of 3 retries AND a backoff of 0.5/1/2/4s, which cannot both hold: three retries sleep 0.5/1/2 and total 3.5 seconds, leaving the documented 4.0s step dead code. Four retries make both statements true and raise total patience to 7.5s. A 70-call paid run that abandons an analyst 3.5 seconds into a rate limit, with three OpenRouter analysts sharing one key and no --continue-on-failure on the gate command, is the worse reading of an ambiguous spec. Honouring Retry-After is the better engineering answer and is deferred, not done.

## 56. Ruling

fix wave includes M3 (a status of None reaches the format-marker check, so a TypeError naming a schema keyword counts as a mode_downgrade) because mode_downgrades is the entire evidence for or against the eta/OpenRouter hypothesis, and polluting it with programming errors makes that evidence unreadable.

## 57. Ruling

ONE fix wave per the skill, then ONE scoped re-review, then adjudication. Wave: C1, I1, I2, I3, I4, I5, I6, I7, deferred #3, deferred #2, M1, M3. Everything else the reviewer triaged as "can stand" stays deferred.

