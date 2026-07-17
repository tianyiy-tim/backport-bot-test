# Backport Bot — #3337 Demo Walkthrough

A ~5-minute, end-to-end run of the pre-merge backport bot on the real fork
(`tianyiy-tim/aws-lc`), using the CVE-2023-style DH fix (#3337) as the example.
Everything below is already staged; just follow the steps.

## What's already set up

- `fork/main` has been rolled back so the #3337 fix is **absent** again (only the
  backport tool + latest upstream are on it).
- A fresh "fix" PR is open and labelled: **PR #67** — "Recognise known safe DH
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
Open **https://github.com/tianyiy-tim/aws-lc/pull/67** — the DH fix, labelled
`needs-backport`. It touches `crypto/fipsmodule/dh/*`, `crypto/dh_extra/*`, and
`dh_test.cc`.

## Step 2 — Merge it (this triggers the bot)
In the PR UI click **Merge** (keep the label on), or:
```sh
gh pr merge 67 --repo tianyiy-tim/aws-lc --merge
```

## Step 3 — Watch the bot run and read its summary
```sh
gh run watch --repo tianyiy-tim/aws-lc
```
Then refresh PR #67 — the bot posts a **summary table**. Expect roughly:
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
python3 /Users/tianyiy/Documents/projects/backport-bot-test/awslc-backport/src/main.py \
    resolve --pr 67 --no-ai
```
What happens:
- It **reads the bot's summary from PR #67** (no second analysis) and targets
  exactly the conflicting branches.
- By **default it works in your own checkout** (`--in-place`): it checks each
  conflicting branch out in `/Users/tianyiy/aws-lc`, so your open **IDE shows the
  conflict live**. Fix the files, come back to the terminal, answer
  `done resolving <branch>? Y`. (Prefer isolation? add `--worktree`.)
- `git rerere` is on. Resolve `fips-2021-10-20` first; on its twin
  `fips-2021-10-20-1MU` the tool prints **"auto-applied by rerere, just verify"** —
  the files are already fixed, you just confirm.
- When done it asks **"Open PRs? [Y/N]"** → `Y` opens one PR per resolved branch
  and posts an **updated summary** on #67 with those rows now ✅.

> Run it with the full `python3 …/src/main.py` path (as above), not `./backport`
> from inside `awslc-backport`, so checking out a release branch doesn't briefly
> hide the tool folder.

## Step 5 — Show the result
Refresh PR #67: the follow-up summary shows every branch backported (✅), no
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
