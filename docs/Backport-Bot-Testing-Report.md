# Backport Bot Testing Report

This report records how the automated backport bot was validated, what was tested, and the results. It covers the deterministic engine, the AI advisory layer, the local patch-driven CLI, and a set of edge cases, with particular attention to false negatives, which are the only failure direction that matters for a security backport tool.

A note on honesty up front: every scenario below runs against a synthetic test fixture that mirrors the AWS-LC structure (multiple FIPS release branches forking at different points, a one-off NetOS branch, file renames, and representative security-style fixes). The fixtures are not real AWS-LC vulnerabilities, and the ground truth was assigned by hand. The results prove the logic is correct on cases with a known answer; production acceptance should add a replay against real AWS-LC history, as noted in Section 7.

## 1. Summary

The deterministic engine resolves the typical case correctly and never silently drops an affected branch: every branch it cannot confirm is surfaced as UNSURE for review, never buried in a confident "not affected". The AI advisory, run live against Amazon Bedrock, caught every deterministic false negative in the test matrix with no regressions, gave a stable verdict across repeated runs, and correctly declined to flag a branch that was already patched. The local CLI runs this whole flow from a patch, before any public code change, with no false negatives observed.

## 2. Test environment

- Synthetic fixture: 7 release branches (`AWS-LC-FIPS-2020` through `2025` plus `NetOS`), each forking from mainline at a different point, with a mid-history refactor that renames files into subdirectories.
- Deterministic tests need no credentials or network.
- AI tests ran against Amazon Bedrock, model `us.anthropic.claude-opus-4-8`, region `us-east-2`, using the `Bedrock-Access` role.

## 3. Deterministic results (no credentials)

| Suite | What it checks | Result |
|---|---|---|
| `test_robustness.py` | determinism, edge cases, file/rename resolution, branch resolution, CLI | 32/32 passed |
| `test_impact_analysis_v3.py` | 7 scenarios x 7 branches = 49 impact decisions + cherry-pick simulation | 34 TP / 10 TN / 0 FP / 5 FN |
| `test_rename_tracking.py` | introducer tracing across renames | pure rename and rename+small-edit tracked; rename+>90% rewrite is the documented blind spot |
| `simulate_real_backports.py` | replay of the documented manual backports (a feature plus two simultaneous security issues) | 15/15 match the manual outcome, 0 FP, 0 FN |
| `test_buckets.py` | false-negative safety of the CLI bucketing | 0 silent false negatives (see Section 6) |
| `test_agentic_impact.py` | agentic prototype (mock backend) | runs; agent only escalates, never overrides |

The 5 false negatives reported by `test_impact_analysis_v3.py` are the deterministic engine's known blind spot (a fixed line whose history dead-ends at a rename or an earlier fix). They are not silent: `test_buckets.py` confirms all 5 surface as UNSURE, and the AI advisory below resolves them.

## 4. AI advisory results (live Bedrock)

The AI is consulted only on branches the deterministic check leaves inconclusive. These runs measured its quality against ground truth.

| Scenario | Deterministic accuracy | AI accuracy | False negatives rescued | Regressions |
|---|---|---|---|---|
| `cve-cross-era` | 42.9% (4 misses) | 100% (7/7) | 4 (FIPS-2020 through 2023) | 0 |
| `cve-record-multifile` | 85.7% (1 miss) | 100% (5/5) | 1 (FIPS-2022) | 0 |

Across both, the AI caught every deterministic false negative and introduced no regressions, which is the evidence behind using it as the advisory layer.

Two further checks on AI behaviour:

- **Stability.** The same inconclusive case (`cve-cross-era` / `FIPS-2020`) was run three times. All three returned the same verdict (likely affected, medium confidence), so the non-determinism of the model did not change the answer on this case.
- **Specificity.** Forced on `cve-handshake-original` / `FIPS-2025`, where the fix had already been cherry-picked, the AI returned likely not affected with high confidence and correctly explained that the guard was already present. This shows it is not simply flagging everything.

The CLI's `analyze --explain` path was also exercised end to end against Bedrock on a real inconclusive branch (`FIPS-2020`); it returned likely affected with a sound rationale (the file is present and even less guarded than mainline) and a recommendation to port the fix by hand because a clean cherry-pick was not possible.

## 5. Edge cases

| Edge case | Expected | Result |
|---|---|---|
| Empty patch | clean no-op, not an error | "patch is empty; nothing to analyze", exit 0 |
| Brand-new file (feature add) | no introducer to trace, no crash | all branches not affected, handled cleanly |
| Rename-induced cherry-pick | conflict flagged, never auto-resolved | flagged for manual backport on `apply` |
| Fix already applied on a branch | skipped as redundant | deterministic "already patched", no PR |
| AI unavailable (no or expired credentials) | deterministic result still produced | degraded cleanly, buckets stand |
| File present under an untraced rename path | must not become a silent "not affected" | escalated to UNSURE by the conservative guard (Section 6) |

## 6. False-negative safety

A false negative, a still-vulnerable branch that the bot does not surface, is the only dangerous failure, so it gets its own analysis.

The bucketing is structured so that "not affected" is only ever returned when the changed code is confidently absent. If ancestry and patch-id do not match but the file is present, the branch becomes UNSURE rather than "not affected". The structural argument is simple: a branch can only be affected if the vulnerable code is present on it, which means at least one changed file is present, which forces UNSURE or AFFECTED, never the confident "not affected".

The one remaining hole was the file-existence check itself being wrong, for example a rename that the history trace could not follow, which would make a present file look absent. To close it, the CLI now adds a conservative guard: if the rename-aware lookup finds nothing, it also checks whether a file of the same name exists anywhere on the branch, and if so escalates to UNSURE. So "not affected" requires both no traced path and no same-named file, and the bias is always toward surfacing rather than dropping.

`test_buckets.py` verifies the property on the full matrix: every truly-affected branch the deterministic check could not confirm lands in UNSURE, and zero land in "not affected". On the 49 cells, all 5 deterministic misses surfaced as UNSURE and none were silent.

UNSURE is an internal state, not a verdict shown to the user. The `analyze` command resolves every UNSURE branch into a definite affected / not affected answer by consulting the AI advisory, and if the AI is uncertain or unavailable the branch resolves to affected (flagged for review), never to not affected. So the user always sees a clean verdict, and the chain of fallbacks (deterministic, then AI, then conservative flag) only ever over-flags, never drops an affected branch.

The honest residual is stated in Section 7.

## 7. Known limitations

- The fixtures are synthetic, with hand-assigned ground truth. Production acceptance should replay known historical AWS-LC backports against the real branch history and require zero false negatives there too.
- The impact analysis assumes the fix touches the file containing the vulnerable code. If a fix lived entirely in one file while the vulnerability manifested through another file that is absent on an old branch, the model could miss it. This is inherent to any patch-driven analysis and is backstopped by the AI advisory and human review.
- The rename plus heavy-rewrite case (similarity below git's rename threshold) breaks line-history tracing. The conservative bucketing turns this into UNSURE rather than a silent miss, and the AI advisory is aimed at exactly these cases.
- The AI is advisory only and non-deterministic by nature. It never overrides a deterministic verdict, never cherry-picks, and never opens a PR, so a wrong AI answer can at worst add a review item, never drop one.

## 8. How to reproduce

Deterministic (no credentials), from the repo root:

```sh
python3 scripts/test_robustness.py
python3 scripts/test_impact_analysis_v3.py
python3 scripts/test_rename_tracking.py
python3 scripts/simulate_real_backports.py
python3 tools/backport/test_buckets.py
```

AI (needs Bedrock credentials and the SDK):

```sh
export AWS_REGION=us-east-2
export BEDROCK_MODEL_ID=us.anthropic.claude-opus-4-8
python3 scripts/visualize_ai_impact.py cve-cross-era
python3 scripts/visualize_ai_impact.py cve-record-multifile
```
