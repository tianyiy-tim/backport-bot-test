# Backport Bot — #3337 Demo Walkthrough

A ~5-minute, end-to-end run of the pre-merge backport bot on the real fork
(`tianyiy-tim/aws-lc`), using the CVE-2023-style DH fix (#3337) as the example.
Everything below is already staged; just follow the steps.

## What's already set up

- `fork/main` has been rolled back so the #3337 fix is **absent** again (only the
  backport tool + latest upstream are on it).
- A fresh "fix" PR is open and labelled: **PR #59** — "Recognise known safe DH
  groups/primes …" into `main`, carrying the **`needs-backport`** label.
- The local checkout at `/Users/tianyiy/aws-lc` is on `main`, clean.
- No other backport PRs/branches are open (previous demo cleaned up).

## The story to tell

1. A security fix is about to merge. 2. On merge (with the label), the bot analyses
every supported release branch and opens a backport PR for each affected one.
3. Clean cherry-picks + test-only clashes become PRs automatically; only real
source conflicts are left. 4. You resolve those locally with a guided flow, and it
opens the remaining PRs and updates the summary.

---

## Step 1 — Look at the incoming fix
Open **https://github.com/tianyiy-tim/aws-lc/pull/59** — the DH fix, labelled
`needs-backport`. It touches `crypto/fipsmodule/dh/*`, `crypto/dh_extra/*`, and
`dh_test.cc`.

## Step 2 — Merge it (this triggers the bot)
In the PR UI click **Merge** (keep the label on), or:
```sh
gh pr merge 59 --repo tianyiy-tim/aws-lc --merge
```

## Step 3 — Watch the bot run and read its summary
```sh
gh run watch --repo tianyiy-tim/aws-lc
```
Then refresh PR #59 — the bot posts a **summary table**. Expect roughly:
- ✅ normal backport PRs opened for the newest branches
  (`fips-2026-06-26-snapshot`, `fips-2025-09-12-lts`) — clean cherry-picks.
- ✅ a PR for a branch whose only clash was the **test file**
  (auto-resolved: source fix applied, test hunk dropped, noted in the PR body).
- ⚠️ **merge conflict** rows for the oldest branches (`fips-2021-10-20`,
  `fips-2021-10-20-1MU`, `fips-2022-11-02`, `fips-NetOS-2024-06-11`) — real source
  conflicts, with the clashing files listed and a pointer to `backport resolve`.

## Step 4 — Resolve the conflicts locally (the interactive part)
```sh
cd /Users/tianyiy/aws-lc && git fetch origin
cd awslc-backport
./backport resolve --pr 59 --no-ai
```
What happens:
- It **reads the bot's summary from PR #59** (no second analysis) and targets
  exactly the conflicting branches.
- For each one it drops you into a shell **inside that branch's checkout** with the
  fix applied and the conflict live. Edit the files, then type `exit`.
  (Prefer your own editor/IDE window? add `--in-place` to swap the branch into your
  working checkout instead — needs a clean tree.)
- `git rerere` is on, so once you resolve `fips-2021-10-20`, its twin
  `fips-2021-10-20-1MU` auto-applies — you just verify and `exit`.
- When done it asks **"Create PRs? [Y/N]"** → `Y` opens one PR per resolved branch
  and posts an **updated summary** on #59 with those rows now ✅.

## Step 5 — Show the result
Refresh PR #59: the follow-up summary shows every branch backported (✅), no
conflict rows left. Open a couple of the backport PRs to show they're normal,
reviewable PRs (nothing auto-merged).

---

## Tips / recovery
- Re-run anytime: it's all on the fork, nothing touches `aws/aws-lc` (the tool
  refuses upstream, and `git push upstream` is disabled locally).
- To reset the demo from scratch, ping me — it's a rollback of `fork/main` + a
  fresh PR (this doc's "already set up" state).
- `git worktree list` shows any leftover resolve worktrees; `git worktree prune`
  cleans them.
- `--no-ai` keeps `resolve` fully local (no AWS creds needed). The CI run in
  step 2/3 uses the Bedrock role that's already configured.
