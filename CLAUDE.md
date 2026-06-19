# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A GitHub Actions-based backport bot for AWS-LC (Amazon's cryptographic library). When a PR is merged on `main` with the `needs-backport` label, the bot automatically determines which supported release branches are affected by the fix, cherry-picks to each, and opens PRs for human review. No backport PR is ever auto-merged.

The repo doubles as a POC test environment: it contains a synthetic multi-branch history (`AWS-LC-FIPS-2020` through `AWS-LC-FIPS-2025` plus `NetOS`) with C fixture files (`app.c`, `crypto/`, `tls/`, `utils/`) designed to exercise rename tracking, cherry-pick conflicts, and impact-analysis edge cases.

## Running the bot manually

The workflow can be triggered from the Actions tab without waiting for a real PR merge:

```
Actions → Backport Bot → Run workflow
  merge_commit: <fix commit SHA>
  pr_number: <originating PR number>
  smoke_test_only: true   ← set this to validate creds without running backports
```

## Pre-flight check

Before a full run, validate all runtime dependencies (SDK, AWS creds, Bedrock reachability, `gh` CLI, `BACKPORT_REPO`):

```bash
python scripts/smoke_test.py
```

Exit code 0 = ready. Non-zero = at least one hard failure with a specific message.

## Running tests

The test scripts exercise the deterministic pipeline against the synthetic fixture branches. Run from the repo root:

```bash
# Comprehensive: impact analysis + cherry-pick simulation across 7 CVE scenarios × 7 branches
python scripts/test_impact_analysis_v3.py

# Earlier iteration (impact analysis only, no cherry-pick simulation)
python scripts/test_impact_analysis_v2.py

# Rename-tracking edge cases
python scripts/test_rename_tracking.py

# Agentic impact analysis prototype (mock backend by default, no creds needed)
python scripts/test_agentic_impact.py

# Run the agentic prototype against real Bedrock
AGENTIC_BACKEND=bedrock python scripts/test_agentic_impact.py
```

No test framework is used — all test scripts are standalone and print tabular pass/fail results.

## Architecture

### Core pipeline (`scripts/backport_bot.py`)

Five sequential steps, all deterministic:

1. **Branch resolver** — `get_supported_branches()`: reads `git branch -r`, filters by `SUPPORTED_BRANCH_PREFIXES` (env-overridable, default covers `fips-*`, `AWS-LC-FIPS-*`, `NetOS`).

2. **Impact analyzer** — determines per-branch whether the fix is needed:
   - `find_introducing_commit()`: for each file changed by the fix, walks the diff hunks and calls `_find_line_origin()` → `git log -L --reverse` to find the *oldest* commit that wrote those lines. Falls back to `git blame -w -M -C`. Returns a set of introducer SHAs.
   - `is_branch_affected()`: **Path 1** — `git merge-base --is-ancestor` (SHA ancestry). **Path 2** — `git patch-id --stable` content matching (catches cherry-picked introducers with different SHAs). **Path 3** — AI advisory via `ai_impact_analysis()` when both paths are inconclusive (returns `False` + advisory dict; never auto-backports on AI output alone).
   - `is_already_patched()`: patch-id comparison against divergent commits on the branch, skips redundant backports.

3. **Backport engine** — `cherry_pick_to_branch()`: checks out `origin/<branch>`, attempts `git cherry-pick`. Returns `("success", new_branch_name)` or `("conflict", conflict_output_str)`. Always cleans up on failure.

4. **PR creation** — `open_pr()`: idempotent (reuses existing open PR for the same head/base). Pins to `BACKPORT_REPO` env var (critical on forks to avoid targeting upstream).

5. **Summary** — `post_summary()`: posts a markdown comment on the original PR. Status markers: `[OK]` success, `[!!]` conflict, `[>>]` not affected, `[==]` already patched, `[??]` not affected (AI advisory attached).

### AI advisory function (`scripts/backport_bot.py`)

AI is used for **impact analysis only**. The deterministic engine owns every action with a side effect (branch resolution, cherry-pick, PR creation, summary); AI never cherry-picks, opens PRs, or resolves conflicts. Its single job is to give the deterministic engine a second opinion on the *affected / not affected* question when git ancestry and patch-id matching are both inconclusive.

`ai_impact_analysis()` uses `AnthropicBedrock` via the `anthropic` SDK. Model: `_BEDROCK_MODEL_ID` (cross-region inference profile, verify in AWS console; env-overridable). It uses `thinking: {"type": "adaptive"}` and streaming (`with client.messages.stream(...) as stream: stream.get_final_message()`). `_ai_client()` resolves credentials via the boto3 default chain; if the SDK or credentials are unavailable it returns `None` and the AI path silently skips, leaving a clean deterministic result.

- **`ai_impact_analysis()`**: called as Path 3 in `is_branch_affected()`. Sends the fix diff plus rename-aware file snapshots from the target branch. Output is a `<details>` advisory block in the PR summary comment. It is returned alongside the deterministic verdict and never overrides it.

AI output is **advisory only** — it is never committed, auto-applied, or used to suppress a deterministic finding. Cherry-pick conflicts are never auto-resolved: the bot aborts the pick and flags the branch (`[!!]`) for a human engineer to backport manually.

### Agentic prototype (`scripts/agentic_impact.py`)

A separate research prototype for cases requiring multi-step investigation (e.g., a bug introduced partway through a line's history). The agent is given three read-only tools (`get_fix_diff`, `read_file_on_branch`, `grep_branch`) and a `submit_verdict` tool for structured output. The loop is bounded at `MAX_STEPS = 6`. Two backends: `mock` (default, no creds) and `bedrock` (AWS Bedrock Converse API via `boto3`). This is not integrated into the main bot.

### Workflow (`.github/workflows/backport.yml`)

- **Triggers**: `pull_request: [closed, labeled]` (automatic) + `workflow_dispatch` (manual with `merge_commit`, `pr_number`, `smoke_test_only` inputs).
- **Condition**: runs only when `merged == true && label == needs-backport`, or when manually dispatched.
- **AWS auth**: GitHub OIDC (`id-token: write` permission) + `aws-actions/configure-aws-credentials@v4` assumes `AWS_BACKPORT_ROLE_ARN`. No static AWS secrets stored.
- **Steps**: full-history checkout → git identity → Python 3.11 → `pip install "anthropic[bedrock]"` → smoke test → bot (skipped if `smoke_test_only`).
- **`BACKPORT_REPO`**: always set to `github.repository` to prevent `gh` from targeting upstream on a fork.

## Key env vars / secrets

| Name | Where set | Purpose |
|---|---|---|
| `AWS_BACKPORT_ROLE_ARN` | GitHub variable | IAM role assumed via OIDC for Bedrock |
| `AWS_REGION` | GitHub variable | Bedrock region (default `us-east-1`) |
| `GITHUB_TOKEN` | automatic | `gh` CLI auth + `git push` |
| `BACKPORT_REPO` | auto (`github.repository`) | Pins PRs/comments to this repo |
| `BACKPORT_BRANCH_PREFIXES` | optional env | Override supported branch prefixes (comma-separated) |
| `BACKPORT_MAINLINE_REF` | optional env | Override mainline ref (default `origin/main`) |

## Known design constraints

- **`git log -L --reverse` takes the oldest introducer**: this over-flags (false positives) rather than misses (false negatives), which is the safer direction for security. The AI path exists to reduce false positives in ambiguous cases.
- **Conflicts are never auto-resolved**: when cherry-pick fails, the bot always aborts and flags for human review. The AI conflict advisory is a suggestion only.
- **`BACKPORT_REPO` is a safety guard**: without it, `gh pr create`/`gh pr comment` on a fork defaults the target to the upstream parent (`aws/aws-lc`). Always set it.
