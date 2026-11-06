# ci-runners

Self-hosted GitHub Actions runners shared by chaobrain projects: host setup, the
executor, the pre-job hook, and the actions and reusable workflows that projects call.
The contract is [docs/design.md](docs/design.md); onboarding is
[docs/consumers.md](docs/consumers.md).

## Working agreement

1. **One task, one PR, from a worktree branch, never on `main`.** Branch from
   `origin/main` into `../ci-runners-<slug>`; the first push opens a draft PR.
   Squash-merge, delete the remote branch, `git fetch origin && git merge --ff-only
   origin/main` in the main checkout, then remove the worktree and its branch.
2. **A pushed branch takes `main` by `merge origin/main`.** Never rebase or force-push
   it.
3. **Contract first, code second.** A change to an action's or workflow's inputs and
   outputs, the labels, the outcomes, the host layout, the hook policy or the
   inventory schema needs a lead-approved edit of `docs/design.md`, in a draft PR,
   before the code. A change that breaks a consumer is a new major version (design §8).
4. **Every PR carries its test evidence** in its body: the commands run, on which host,
   and their result. A change that touches a host also carries its dry-run plan
   (`setup/plan.sh`) and the live check on that host. Tests are stdlib Python with
   fixtures; each new test is shown to fail on a mutant of the line it guards.
5. **Review.** Changes to the hook, the policy, the egress proxy and its allowlist,
   the org snapshot or the setup scripts get an independent review; for the rest
   the maintainer reads the diff.

## Rules

- **No inventory in the repository.** Host names and ssh aliases, IP addresses, Unix
  account names, home-directory paths, egress hosts, allowlists and any token stay on
  the hosts (design §3.3). `tests/test_no_inventory.py` enforces it from RUN-1.
- **Waiting.** Wait on a PID (`while kill -0 "$PID"; do ...`) or on an exit file the job
  writes; never on a `pgrep -f` loop (its own command line matches). Start a long wait
  once, in the background, with a timeout, and never re-issue it.
- **Killing.** Kill by PID, checked against its start time, or stop the job's systemd
  scope. Never `pkill -f` or `killall`: on a shared account they hit other people's
  processes.
- **Shared hosts.** Heavy commands run under the host lock and a memory scope. On a
  shared account, touch nothing outside our own subtree.
- **GitHub records read as a human team's operations.** PR titles and bodies, commit
  messages, issues, comments, workflow and job names, job summaries and status
  descriptions name no agent, model, effort level, token or prompt.
- **This repository's own CI runs on GitHub-hosted runners only.** None of its
  workflows targets a self-hosted label, and the hook refuses this repository.

## High-impact actions go through the lead

Do not act; list the item under "Needs the lead" in the PR body (context, options,
recommendation) and keep working on the rest:

- registering, removing or relabelling runners; changing the runner group;
- creating or changing host accounts, ssh keys, `authorized_keys`, systemd services
  or timers (applying a setup plan);
- changing an org or repository setting (visibility, rulesets, Actions access);
- a release tag, or moving a major tag (`v1`);
- adding a repository or an actor to the hook's allowlist.
