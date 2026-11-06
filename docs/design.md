# ci-runners: design (contract)

Status: approved by the lead on 2026-11-05 (#1). RUN-1 is in progress: RUN-1a holds the package's
pure decisions. Consumers onboard through [consumers.md](consumers.md). Decision 11
(2026-11-06) adds RUN-5, a standard test CI on h100 for every private repository.

This repository becomes public (D1). Hosts appear here under role names: **3090b**,
**3090a** and **h100**. Real host names and ssh aliases, accounts, paths under a home
directory, proxy nodes and allowlists are inventory and stay on the hosts (§3.3).
brainspike's private `docs/specs/arch/ci.md` names them.

## Lead decisions (2026-11-05)

1. **Shared runners, registered at the organization level.** Several chaobrain
   repositories use the runner setup, and they share the hosts and runners.
2. **Public repositories may use only the GPU parts, only for pushes by allowlisted
   members, never from forks.** CPU work in public repositories stays on GitHub-hosted
   runners.
   - A pre-job hook on the host, outside any repository, enforces a repository
     allowlist, an actor allowlist and "not a fork event".
   - Workflows send a job to self-hosted runners only when
     `github.event.pull_request.head.repo.full_name == github.repository`, or on `push`
     or `workflow_dispatch` by a member.
3. **h100, option A.** h100 has 5 × H100 PCIe 80 GB and is reached only through a Unix
   account that other lab users share. Our files there live in our own subtree.
   - The runner processes live on 3090b, under `~/data/actions-runner/h100-gpu<N>`. There
     is one runner per H100, N = 0..4, with labels `h100` and `h100-gpu<N>`.
   - A job runs its part on h100 over ssh with `CUDA_VISIBLE_DEVICES=<N>`,
     `taskset -c 0-43,88-131` (NUMA node 0) and a fresh per-job directory under our
     subtree's `ci-work/`. The directory is deleted at the end, also when the job is
     killed.
   - The ssh command passes only an env allowlist. It never forwards `GITHUB_*`,
     `ACTIONS_*` or `RUNNER_*`. No GitHub credential, runner or hook ever lands on the
     shared account.
   - Safeguards:
     1. An integrity manifest is kept on 3090b and checked before each part: hashes of
        `conda-meta`, `site-packages` and the checkout HEADs. A mismatch sends the part
        to its fallback.
     2. Snapshots go by `git archive <sha>`, never with repository history.
     3. h100 runs GPU parts only. Checks, CPU and compile parts run on trusted hosts. A
        random 10 % of h100 parts are re-run on a trusted host, and a disagreement fails
        the head.
     4. The exit path is a dedicated account from the host's admin, then normal runners.
   - Envs and manifests are per project.
4. **3090 hosts.** 3090b is brainspike's reference host (2 × RTX 3090, i7-14700KF).
   3090a has 2 × RTX 3090, is shared with other users and runs brainspike's nightly.
   - Runners live under `~/data`: `~/data/actions-runner/<name>`, with their work
     directories under `~/data`. They run as systemd user services.
   - The maintainer's account has passwordless sudo and linger on both hosts.
   - A dedicated CI account per host, without the admin GitHub token, is preferred.
     Until it exists, the runners run as the maintainer's account behind the allowlist
     hook.
5. **Proxy.** GitHub is reachable only through a proxy client, and each host uses its
   own node. Some nodes are refused from the intranet. Today the proxy is started by
   hand. It becomes a user service, and the runners are ordered after it.
6. **A local green run on a head is a full verdict, and the runners never repeat it**
   (brainspike's rule). The generic layer offers a `claim` step that exits with success
   at once when the project's status context is green on that sha.
7. **Shared slots and locks.** Host-level slots and locks are shared by all projects,
   so several projects cannot oversubscribe a host. They generalise brainspike's
   `scripts/ci_slot.sh` and `scripts/host_lock.sh`.
8. **Generic outcome classification, with fixtures.** The fixtures are journal OOM
   lines, the h100 GPU watchdog's kill lines, boot-id changes and a stopped runner.
   Thermal and OOM kills are retried per policy and are not reported as test failures.
9. **The choices of §12, each as recommended (lead, 2026-11-05):**
   1. ci-runners goes public before the first registration, with rulesets on `main`
      and `v*`;
   2. public consumers get repository-level GPU runners, and
      `allows_public_repositories` stays off;
   3. runner pools are shared per host and class;
   4. the CI proxy is a separate instance;
   5. brainevent is the first public consumer;
   6. the verdict waits for a drawn verify;
   7. the early thermal stop is at 85 °C for 3 min;
   8. the manifest cost is chosen by RUN-2's measurement;
   9. `Default` is restricted to selected repositories;
   10. the org base permission stays `write` for now, to be tightened later.
10. **The RUN-1b plan review (lead, 2026-11-05):**
   1. "Bot dispatch: a per-repo switch, off by default. When it is off, a bot dispatch
      goes through the actor allowlist." The policy's `bot_dispatch` (private
      repositories only) is that switch; the bot is on no allowlist, so with the switch
      off a bot-started job is refused (`bot`). §6.2.
   2. "Tamper gap (§5.3, T13): accepted for private consumers for now. Before any
      public repo uses the runners, the hook and policy move to a root-owned host
      layer, as a separate task." §5.3 option (b).
   3. "Hook path in job logs: accepted while only private brainspike uses the runners."
      In the same task as item 2, before a public consumer: the hook moves to a generic
      path such as `/opt/ci-runners`.

## Lead decisions (2026-11-06): a standard test CI on h100 (RUN-5)

The request: a standard self-hosted test CI that every chaobrain member can use, for
every chaobrain repository's tests, on h100. These decisions supersede the items they
name; where this section and an older one disagree, this section holds for RUN-5.

11. **Standard test CI on h100 (RUN-5).**
    1. **Private repositories only.** Every private chaobrain repository may use the h100
       runners. Public repositories keep their CPU work on GitHub-hosted runners, and
       their GPU path stays decision 2, §12 item 2 and RUN-4, unchanged.
       `allows_public_repositories` stays off. `Default` goes from `selected` to every
       private repository (supersedes §12 item 9; the lead makes the change).
    2. **Allowlists synced from the org.** The actor allowlist is the org's members, and
       the repository allowlist is the org's private repositories with their ids. A
       timer fetches both every 10 min into a local snapshot on h100 (§6.5; where the
       timer runs and whose login it uses: 11.9). The hook
       still reads only local files. Outside collaborators and bots are on neither list.
       A snapshot older than 60 min refuses every job (`stale`). Entries written by hand
       in `policy.toml` still win, so the lead can pause a repository.
    3. **Runners on h100's shared account; CPU and GPU parts both run there.** h100
       becomes a local host (`remote = false`), like 3090b: runner processes, the hook,
       the host layer and the user units live on the shared account, under our subtree,
       except the units. This supersedes decision 3's option A and its safeguard 3 for
       RUN-5. The ssh driver of §5.4 is not built for RUN-5; RUN-2 stays as designed for
       a later h100 path driven from 3090b. The cost is stated in §6.1 and §6.4 (T6, T13,
       T14), and the lead accepts it for private repositories.
    4. **Envs prebuilt per repository.** A maintainer builds each repository's conda env
       on h100 from the `.github/ci-runners.toml` of its default branch (consumers §1),
       with `ci-run env build`. A pull request cannot change an env.
    5. **No manifest and no verify draw on RUN-5.** Both guard against users of the shared
       account. With the runners on that account, those users can edit the hook and the
       results too, so the checks would only look like protection. Decision 3's
       safeguards 1 and 3 apply only to a path driven from 3090b (RUN-2).
    6. **h100 reaches GitHub directly.** Measured 2026-11-06: `api.github.com` answers
       without a proxy. So h100 runs no CI proxy instance (decision 5 applies to the
       3090 hosts). *Amended by 11.8: only part of GitHub answers directly.*
    7. **The exit path** is still decision 3, safeguard 4: a dedicated account from the
       host's admin, which closes T6, T13 and T14 for h100.
    8. **h100's egress goes through an ssh tunnel (amends 11.6).** Measured again on
       2026-11-06: `api.github.com` and `*.actions.githubusercontent.com` answer
       directly, but `github.com` does not, `codeload.github.com` and the release
       downloads fail often, and the package indexes (PyPI, conda) do not answer. So h100
       gets a CI egress of two user units. `ci-tunnel.service` is an ssh connection with
       dynamic forwarding (SOCKS5 on 127.0.0.1) to a restricted account on an egress
       host. `ci-bridge.service` is an HTTP CONNECT proxy on 127.0.0.1 that passes only
       allowlisted names on port 443 to the tunnel; the runner app, git, pip and conda
       all speak HTTP CONNECT, and not all of them speak SOCKS. Both ends keep an
       allowlist: the bridge's (inventory `tunnel.allow`), and the egress account's
       `authorized_keys` line, `restrict,port-forwarding`, one `permitopen` per allowed
       name on 443, `permitlisten` on the discard port only and `command` set to
       `nologin`. The runners' `.env` points `https_proxy` at the bridge, with `no_proxy`
       naming the hosts that answer directly, so the runner's own traffic does not hang
       on the tunnel. Names resolve on the egress host (SOCKS5 with a domain name):
       local DNS answers for blocked names cannot be trusted.
    9. **The org snapshot is pushed from the maintainer's workstation (amends 11.2).** A
       fine-grained token can be minted only in the web UI, and a token on h100's shared
       account is readable by its co-users (T14). So no token lives on h100. A user timer
       of the maintainer's account on the workstation fetches the snapshot every 10 min
       with the maintainer's own GitHub login (`gh auth token`, read at each run, never
       written elsewhere). It sends the snapshot over the maintainer's existing ssh access
       to h100, where `ci-run org-sync --receive` reads it from stdin, checks it (§6.5)
       and writes `etc/org.json`. A workstation that is down ages the snapshot: after
       60 min every h100 job is refused `stale`, never let through. `ci-orgsync.timer`
       stays for a host that holds its own token (inventory `orgsync`).

## Decisions of this spec

- **D1. ci-runners goes public before its first runner is registered (RUN-1).** Rulesets
  protect `main` and the `v*` tags, with only the lead able to bypass them. The repository
  never holds inventory.
  - *Reason:* a public repository can call reusable workflows only from public
    repositories (§2).
  - *Reason:* on Free, rulesets exist only for public repositories, and every org member
    has write access to every repository. In a private ci-runners, any member could move
    `v1` and run code on every runner.
  - *Reason:* the repository's own tests then run free on hosted runners.
- **D2. Two layers.**
  - The *host layer* is installed on each host from a release by `setup/install.sh`, and
    no job ever fetches it. It holds the hook, the executor, the h100 driver, slots,
    locks, GPU leases and outcome classification.
  - The *workflow layer* holds the actions and reusable workflows. It is thin and calls
    the host layer of its own release.
- **D3. Labels name a class and a host.** Pools are shared by projects. Host slots bound
  the load, and runner counts bound only the waiting (§7).
- **D4. The pre-job hook is the boundary on every runner.** Every runner unit sets
  `NoNewPrivileges=yes`, which made `sudo` fail inside a user unit on 3090b (measured
  2026-11-05).
- **D5. Interoperate, don't migrate.** The host's lock and slot files are the ones
  brainspike uses today, named in the inventory. brainspike's local runs, its nightly
  and every project's runner jobs share them from the first day, with no brainspike
  change.
- **D6. A part exits 1 only when the run is red.** Every infrastructure end exits 0, with
  its outcome and a `next` hint for the consumer's fallback job (brainspike ci.md §4).
- **D7. One card per h100 part.** No multi-GPU part runs on h100 in v1 (see the P2P
  fault in §5.4).
- **D8. The verify draw on h100 is keyed** with an HMAC whose key stays on 3090b, so
  users of the shared account cannot predict which parts are checked.

## 1. Scope

In scope: host setup, runner services, the pre-job hook, the executor of a part, the
h100 driver, shared slots, locks and GPU leases, outcome classification and retry
hints, the claim step, placement probes, and a reusable GPU workflow.

Out of scope, and kept by each consumer: what to test (a project's selection, plan and
profiles), its summary format, its merge gate, its nightly logic and its status
semantics beyond `claim`.

## 2. GitHub facts (checked 2026-11-05) and what they mean here

The org is on GitHub Free ("plan": "free", 12 seats). Its base permission is `write` on
every repository.

Settings applied by the lead on 2026-11-05 (D1, §12 items 1 and 9), read back from the
API the same day:

- ci-runners is public.
- Ruleset "protect main": the default branch cannot be deleted or pushed non-fast-forward.
- Ruleset "protect release tags": `refs/tags/v*` cannot be created, updated or deleted.
- Both rulesets are active, and their only bypass is the organization admin role
  (`OrganizationAdmin`, `always`; the org has 2 admins).
- `Default` runner group: `visibility: selected`, with chaobrain/brainspike as its only
  repository; `allows_public_repositories: false`; `restricted_to_workflows: false`.
  Free accepts `selected`.

| fact | source | consequence |
|---|---|---|
| An org has one Default runner group. Only Team can create more. | docs: runner groups ("organizations using the GitHub Team plan"); API: one group, `Default`, `visibility: all`, `allows_public_repositories: false`, `restricted_to_workflows: false`; 0 runners | Every org runner would be open to every private org repository (11 on 2026-11-05). `Default` is now restricted to the selected repositories (above), and the hook's repository allowlist stays the per-repository boundary. |
| By default, only private repositories can use a runner group. | docs; API default `allows_public_repositories: false` | Public repositories reach no org runner unless the flag is turned on. See below. |
| A public caller can use only public reusable workflows. A private caller can use public ones, and private ones whose repository grants Actions access. | docs: "Reusing workflow configurations", access table | Public consumers need ci-runners public (D1). Decided in §12 item 1. |
| Rulesets and branch protection exist only for public repositories on Free. | API: private ci-runners returns 403 "Upgrade to GitHub Pro or make this repository public"; public brainstate has an active ruleset | D1 |
| Self-hosted runners and standard hosted runners for public repositories are free. | docs: Actions billing | This repository's CI runs on hosted runners. |
| The pre-job hook runs in "Set up runner" with the default variables and the event payload (`GITHUB_EVENT_PATH`). A nonzero exit fails the job. It has no timeout, and its output is visible to anyone who can read the repository. | docs: "Running scripts before or after a job" | The hook bounds itself, reads only local files and prints a reason code, never inventory. |
| A job queued 24 h fails. A runner offline 14 days is removed. A runner registered with `--disableupdate` that misses an update for 30 days gets no jobs. Runners update themselves by default. | docs | §8 bounds. Auto-update stays on. |
| Forks of a public repository can run code on a self-hosted runner through a pull request. GitHub recommends self-hosted runners only for private repositories. | docs warnings | §6 |

**Turning on `allows_public_repositories`.** Every runner in Default would become
assignable to jobs of every public chaobrain repository: 19 today, 8 of them active
consumers, and any public repository created later. This includes `pull_request` runs from forks,
which run the fork's copy of the workflow in the base repository's context. So anyone
on GitHub could open a pull request that asks for `runs-on: [self-hosted, compile]`.
GitHub's only gate is approval for first-time contributors.

The hook refuses such a job before its first step, but the job still takes a runner:

- **Set up job runs before the hook.** It downloads the job's actions to the host, and
  for a Dockerfile action it may build the image. RUN-1 records the order (§10). The
  runner account cannot reach docker: 3090b has none, and on 3090a the socket is
  root-only and the account is not in the docker group. `sudo` is blocked by D4.
- **The hook parses attacker-written event text.**
- **A burst of refused jobs delays our own queue.**
- **A hook bug or a policy mistake would open every runner**, including the compile
  host and, until dedicated accounts exist, the maintainer's admin token.

**Does the public GPU path need the flag?** With org-level runners, it does: without it,
a public repository's job matches no runner and fails after 24 h. The path that keeps
the flag off is to register *repository-level* GPU runners on each public consumer.
Then only that repository, including its fork pull requests, which the hook still
refuses, reaches only its own GPU runners. §12 item 2 decides that path.

## 3. Layout

### 3.1 Repository

```
AGENTS.md  README.md  docs/design.md  docs/consumers.md
ci_runners/           stdlib-only Python: pure functions, thin I/O
  policy.py           hook decision: repository, actor, event, fork, runner class
  orgsync.py          the org snapshot: members and private repositories (RUN-5, §6.5)
  bridge.py           h100's egress: HTTP CONNECT to the ssh tunnel, allowlisted (11.8)
  claim.py            claim decision from a sha's statuses
  place.py            host and card choice from probe results
  outcome.py          outcome classification and the retry hint
  slots.py locks.py gpus.py   fair host slots with caps, the host lock, GPU leases
  remote.py           h100 driver: ssh argv, env allowlist, archive transport, sampler
  manifest.py         integrity manifest: build and check
  verify.py           keyed draw; JUnit comparison
  inventory.py        schema, loader, lint
bin/                  ci-run, ci-slot, ci-lock, ci-gpu: CLIs over the package
hooks/                job-started.sh, job-completed.sh -> python -I -m ci_runners.hook
setup/                plan.sh, apply.sh, install.sh, register.sh, uninstall.sh
setup/units/          user unit templates: ci-runner@.service, ci-proxy.service,
                      ci-proxy-check.{service,timer}, ci-reaper.{service,timer}, slices;
                      ci-tunnel.service, ci-bridge.service (h100's egress, 11.8)
setup/inventory.example.toml   the schema, with placeholder values
setup/workstation/    orgsync-push.sh and ci-orgsync-push.{service,timer}: the snapshot
                      push from the maintainer's workstation (decision 11.9)
actions/              claim/, place/, part/, salvage/, verdict/, compare/ (composite, action.yml);
                      lib/ci-run.sh, the launcher; RELEASE, the release stamp
.github/workflows/    gpu.yml (reusable, public consumers); h100-test.yml (reusable, the
                      standard test CI, RUN-5); test.yml (this repo's tests, hosted only)
tests/                unit tests; fixtures/ (sanitised): event payloads, journal and
                      watchdog lines, nvidia-smi CSV, boot ids, systemctl output, JUnit
tests/test_no_inventory.py     no tracked file holds a denied token (hashed deny list)
```

The deny list holds SHA-256 hashes of the inventory's tokens, so the list does not
publish what it protects. `setup/plan.sh` regenerates it from a host's inventory.

### 3.2 Hosts

3090b and 3090a, as the runner account:

```
~/data/ci-runners/releases/<tag>/   installed host layer, read-only
~/data/ci-runners/hooks/            the hooks (exec the newest installed release's hook)
~/data/ci-runners/etc/              inventory, mode 700 (§3.3)
~/data/ci-runners/state/            part records parts/<repo>/<run>-<attempt>/<job>/
                                    {status.json, samples.csv, pids, log}; leases; queues
~/data/ci-runners/logs/             hook and reaper logs, kept 14 days
~/data/actions-runner/<name>/       one runner each: the app, .env, credentials
~/data/ci-work/<name>/              its work directory
~/.config/systemd/user/             the units of setup/units/
```

h100, on the shared account: only `<our subtree>/ci-work/<run>-<attempt>-<job>/` per
job, deleted after it; the per-project conda envs and upstream sources that exist
today; one `authorized_keys` line for the CI key. Nothing else.

h100 with RUN-5 (decision 11.3), on the shared account. The layout of 3090b, rooted at
our subtree instead of `~/data`:

```
<our subtree>/ci-runners/releases/<tag>/   installed host layer
<our subtree>/ci-runners/hooks/            the hooks
<our subtree>/ci-runners/etc/              inventory, policy, org.json; mode 700
<our subtree>/ci-runners/etc/tunnel/       the tunnel's key, ssh_config, known_hosts (11.8); mode 700
<our subtree>/ci-runners/state/  logs/     as on 3090b
<our subtree>/ci-runners/envs/<project>/   the projects' conda envs (decision 11.4)
<our subtree>/actions-runner/<name>/       one runner each
<our subtree>/ci-work/<name>/              its work directory
~/.config/systemd/user/ci-*                the units: the one place outside our subtree
```

The hooks and the launcher run the inventory's `python` (3.11 or later; the system's
is 3.10), passed to them as `CI_RUNNERS_PYTHON` in each runner's `.env`. Elsewhere it
defaults to `/usr/bin/python3`.

### 3.3 Inventory: where it lives, what is secret

| item | where | secret |
|---|---|---|
| runner credentials (`.runner`, `.credentials`, `.credentials_rsaparams`) | each runner directory | yes: they let anyone act as the runner |
| the CI ssh key, its `ssh_config` and `known_hosts` | `etc/ssh/` on 3090b and 3090a | the key is |
| the verify key | `etc/verify.key` on 3090b | yes: it predicts the draws |
| registration tokens | never stored; the lead mints one per registration (valid 1 h) | yes |
| host names, aliases, accounts, paths, proxy node and ports, NUMA and GPU maps, thresholds | `etc/inventory.toml` | no, but never published |
| repository and actor allowlists, per-class event rules | `etc/policy.toml` | no, but never published |
| project registry (env names, caps, env allowlist, upstream list, status context) | `etc/projects/<p>.toml` | no |
| integrity manifests | `etc/manifests/<p>/<remote>.json` on 3090b | no; tamper-relevant, so only on trusted hosts |
| the env-build token (RUN-5): fine-grained, Contents read on the repositories whose envs are built | `GH_TOKEN` of the maintainer's shell, for one `ci-run env build`; never stored on h100, since co-users of the account could read every private repository with it | yes |
| the org-sync login (RUN-5, decision 11.9) | the maintainer's own GitHub login on the workstation, read by the push timer at each run; nothing on h100. A host that syncs itself (inventory `orgsync`) keeps a fine-grained read-only token in `etc/orgsync.token`, mode 600 | yes |
| the org snapshot (RUN-5): member logins, private repository names and ids | `etc/org.json` on h100, written by `ci-run org-sync --receive` (or `ci-orgsync`) | no, but never published |
| the tunnel key, its `ssh_config` and the egress host's pinned key (RUN-5, decision 11.8) | `etc/tunnel/` on h100, the key mode 600; the egress host, its address and account are inventory | yes: it opens the allowlisted forwards through the egress host (T15) |
| the workstation's push units (RUN-5, decision 11.9): the ssh alias of h100 and its paths | the maintainer's `~/.config/systemd/user/` on the workstation | no, but never published |

3090b holds the master copy of `policy.toml` and `projects/`, and `setup/apply.sh`
copies them to 3090a. Each hook log line carries the policy's hash, so drift shows.

## 4. The consumer interface

All actions are `chaobrain/ci-runners/actions/<name>@v1`. Each calls the host layer of
its own release (§9). Inputs that the project registry already holds (env, caps,
defaults) may be left out.

### 4.1 `claim`: skip a head that already has a verdict

| input | default | meaning |
|---|---|---|
| `context` | required | the project's status context, e.g. `brainspike/ci` |
| `sha` | the PR head, else `github.sha` | |
| `wait-seconds` | 0 | how long to wait for a local claim to appear (brainspike: 60) |
| `local-lapse-minutes` | 45 | a pending local claim older than this has lapsed |
| `pending-description` | "queued on the runners" | posted when the runners take the head |

Output `run` is `true` or `false`, with `reason`. Pure rule, on the newest status of
`context` for the sha:

1. success, posted by the workflow bot or by an allowlisted actor: skip, "green";
2. failure posted by an allowlisted actor (a local run): skip, since the fix is a new
   push;
3. pending posted by an allowlisted actor within the lapse (a live local claim): skip;
4. anything else: run, and post pending.

On a hosted runner, which happens only for public consumers, there is no allowlist, so
only the bot's statuses count. Permissions: `statuses: write`.

### 4.2 `place`: choose hosts and cards (private consumers, on `light`)

Input `parts`, or `parts-file` naming a file, is a JSON list of `{id, class:
cpu|gpu|compile, gpus, gpu-mem-mib, memory, estimate-minutes}`. Optional inputs: `prefer` (host order per class; the
project registry's by default) and `exclude` (hosts).

Output `plan` gives, per part, `runs-on` (a label array), `fallback` (a label array or
none) and `timeout-minutes`. Without `prefer`, every class tries the host that runs
`place` first, then the inventory's other hosts in order (RUN-1: 3090b alone). The timeout is twice the estimate plus 30, scaled by the
host's CPU slowdown (h100: 2.7), and at most 240.

`place` probes each host over ssh: reachable, runner services active, proxy answering,
no reservation held, the slot queue, and per card the free memory, temperature, use by
others and the h100 health gate. A probe gets 60 s and the whole step 5 min. A host
that fails a probe gets no part. `place()` is a pure function of the probe results. A field a
probe does not state counts against its host. RUN-1's probe reads neither a host
reservation nor the cards' use by others; 3090b has neither, and RUN-3 adds both before
3090a joins.

### 4.3 `part`: run one command (the executor)

| input | default | meaning |
|---|---|---|
| `project` | required | registry key. It must match `github.repository`, or the job is refused. |
| `id` | required | the part's name, `[A-Za-z0-9][A-Za-z0-9_.-]*`: its record, scope and artifact are named after it |
| `command` | required | `bash -c` in the job's checkout, inside the project's env |
| `env` | registry | conda env name on this host |
| `gpus` | 0 | number of cards to lease; 0 is a CPU part |
| `gpu-mem-mib` | registry | free MiB a card needs (S2; brainspike 6000) |
| `memory` | registry | `MemoryMax` of the part's scope, at most the registry's maximum |
| `slot` | `full` on `part`, `compile` and `nightly`; `none` on `light` | takes a host CI slot |
| `lock` | `none` | host lock for the whole command: `s`, `x` or `l`. With `none`, the command takes it per step, as `local_ci.sh` does. |
| `timeout-minutes` | required | inner bound, below the job's `timeout-minutes` |
| `queue-timeout-minutes` | 60 | longest wait for a slot or a lease, then `unavailable` |
| `results` | none | glob of JUnit XML that the command writes. Required on `h100` (verify). |
| `resume` | false | sets `CI_RESUME=1` (brainspike maps it to `-R`) |
| `attempt-of` | none | the record of the attempt this one retries |

The command's environment holds:

- `CI_RUNNERS_HOST` (the role name), `CI_PROJECT`, `CI_PART_DIR` and `CI_RESULTS_DIR`;
- `CUDA_VISIBLE_DEVICES` and `CI_CPUS`;
- the host lock and slot paths, with the slot marked as held;
- `CI_RESUME`;
- the project's registry variables, for example brainspike's `BRAINSPIKE_HOST`,
  `LOCAL_CI_HOST_LOCK` and `LOCAL_CI_SLOT_LOCK`.

On 3090b and 3090a the command runs in
`systemd-run --user --scope --slice=ci-<project>.slice -p MemoryMax=<memory>
-p MemorySwapMax=0 -p OOMPolicy=continue`, at nice 10 and ionice 7 (nice 0 for
`compile`), with OOM score +500. In the scope a supervisor runs the command, ends it at
the inner timeout (TERM, then KILL after 30 s) and writes the exit and the scope's OOM
kills into the record. The scope is not in the runner's cgroup, so a stopped runner
leaves the part running and its record finishing: `salvage` reads it. The supervisor
relays the command's output to the job log; once the job's end of it is gone, it drops
the rest, so the command never meets a closed pipe. The supervisor holds the CI slot and
the GPU leases for the part's life; the command does not inherit them.

| output | meaning |
|---|---|
| `outcome`, `reason` | §4.4 |
| `next` | `none`, `retry` or `fallback` |
| `retry-on` | label array for the retry or the fallback (on h100: a cool card's `h100-gpu<M>`) |
| `host`, `gpus`, `queue-seconds`, `run-seconds` | for the consumer's summary |
| `verify` | `true` if the keyed draw picked this h100 part |
| `record` | `<host role>:<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>`, the part record below the host's `state/parts/`, for `salvage` |
| `summary` | path to a Markdown block: host, cards, peak temperature per card, CPU frequency cap, waits, events |

The step exits 1 only for `red` (D6). It also uploads these outputs as `outcome.json`, in
an artifact named `ci-part-<id>-<attempt>`. A matrix leg's outputs do not reach later
jobs one by one, so the fallback and verdict jobs read the artifacts instead.

### 4.4 Outcomes and retry hints

Precedence, highest first: stopped, reboot, thermal, oom, yielded, unavailable, red,
pass. A nonzero exit with an OOM kill is `oom`, not `red`.

| outcome | detected by | `next` |
|---|---|---|
| pass | exit 0 | none |
| red | nonzero exit or the inner timeout, with no infrastructure cause | none |
| oom | the scope's `memory.events` `oom_kill` > 0, or a journal OOM line for the scope (fixtures) | retry once with `resume`, on a host with more memory headroom |
| thermal | h100: a watchdog kill line (`GPU <bus> still <t>C -> killed <user>:<pid>, ...`) within the part's window that names our card's bus and a PID the sampler recorded for our scope; or our own early stop (§5.4) | retry once on a cool card of the same host, else fallback |
| reboot | boot id differs from the one recorded at start | h100: as thermal (the board powers off at 101 °C); others: retry once |
| yielded | the host reservation's yield marker is newer than the part's start (§7) | fallback |
| unavailable | before the command: health gate, manifest mismatch, no card fits, upstream not at the required commit, queue timeout, the project's queue full, low disk, multi-GPU on h100, unknown inventory schema | fallback |
| lost | the job ended without outputs: the runner stopped (`systemctl show` timestamps after the part's start), ssh silent past its bound, proxy down | `salvage` the record, else retry once |
| stopped | cancelled (concurrency group, TERM) | none |
| disagreement | the verify part's JUnit results differ from the h100 part's (`compare`) | none: the verdict fails |

The watchdog runs on the shared account, so the `<user>` it names is everyone's.
Matching therefore uses our recorded PIDs and the card's bus id. The sampler maps
index to bus with `nvidia-smi --query-gpu=index,pci.bus_id`.

A part is retried at most once (the registry may set 0). A second infrastructure end
is the consumer's verdict to make (brainspike: error).

### 4.5 `salvage` and `compare`

`compare` ships with RUN-2, where its first consumer (the h100 verify part) is; `gpu.yml`
(§4.6) ships with RUN-4, its first public consumer; `h100-test.yml` (§4.7) ships with RUN-5. `v1.0.0` has `claim`, `place`,
`part` and `salvage`.

- `salvage` runs on `light`. It reads a part record over ssh and returns the same
  outputs as `part`, or `lost` if the record never finished.
- `compare` is a pure function over two JUnit sets. A test that passes on one host and
  fails on the other is a disagreement. A test that runs on only one host is listed, but
  does not fail the comparison.

### 4.6 Reusable workflow `gpu.yml` (mainly for public consumers)

Inputs: `project`, `command`, `gpus` (1), `gpu-mem-mib`, `memory`, `context`,
`timeout-minutes` and `results`. Jobs:

1. `guard` (hosted). It passes for `pull_request` with
   `head.repo.full_name == github.repository`, and for `push` and `workflow_dispatch`.
   The hook checks the actor on the host as well.
2. `claim` (hosted).
3. `gpu` on `[self-hosted, h100]`.
4. `fallback` on `retry-on`, when `next` is `fallback` or `retry`.
5. `verify` on `[self-hosted, gpu]`, when `verify` is true.
6. `verdict` (hosted) posts `context`: success, failure (red or disagreement) or error
   (an infrastructure end twice).

Permissions: `contents: read`, plus `statuses: write` for claim and verdict.

### 4.7 Reusable workflow `h100-test.yml`: the standard test CI (RUN-5)

For any private chaobrain repository. Every job runs on h100 runners, so a private
caller spends none of its hosted Actions minutes.

| input | default | meaning |
|---|---|---|
| `project` | required | registry key (§4.3) |
| `cpu-command` | empty | the CPU part's command; empty skips the part |
| `gpu-command` | empty | the GPU part's command; empty skips the part |
| `gpus` | 1 | cards for the GPU part; only 1 is accepted on h100 (D7) |
| `timeout-minutes` | 60 | each part's inner bound; the job's is this plus 15 |
| `results` | empty | glob of JUnit XML under `CI_RESULTS_DIR` |
| `context` | `<project>/ci` | the status context claim reads and the verdict posts |

Jobs. Each one has the guard: `pull_request` only with
`head.repo.full_name == github.repository`, and `push`, `workflow_dispatch`:

1. `claim` on `[self-hosted, h100-light]` (§4.1). Its first step checks the inputs
   (`timeout-minutes` 1 to 240, at least one command) and gives the later jobs the part
   ids, the context and the jobs' timeout, since an expression cannot add;
2. `cpu` on `[self-hosted, h100-cpu]`, `part` with `gpus: 0`;
3. `gpu` on `[self-hosted, h100]`, `part` with `gpus: 1` (the executor picks the card,
   §5.4 health gate);
4. `cpu-retry` and `gpu-retry`: each part whose `next` is `retry` or `fallback`, once,
   with `attempt-of` its first record and `resume` as the hint says. h100 is the only
   host, so the retry stays there: the CPU part on `[self-hosted, h100-cpu]`, the GPU part
   on the `h100-gpu<M>` that `retry-on` names (a cool card after a thermal end), else on
   `[self-hosted, h100]`. `retry-on` is not taken as is, because it names runner
   classes (`part`), which are not h100's labels (§5.1). A part whose job ended without
   outputs is not retried here; the verdict salvages it;
5. `verdict` on `[self-hosted, h100-light]`, the `verdict` action, unless the run was
   cancelled (a newer head's run posts instead; the cancelled head keeps its pending).
   It reads the parts' `ci-part-<id>-<attempt>` artifacts. Where a part's artifacts stop
   short (none at all, or a retry asked for and no retry artifact), it reads that
   attempt's record on this host by `salvage`, else the part is `lost`. A part's last
   attempt decides: any `red` or `disagreement` sets `context` to failure; all `pass`,
   success; else error. The parts' summaries are merged into the job summary, and the
   job fails unless the state is success.

The workflow calls the actions at `@v1`, since an expression cannot pin `uses:`. A canary
of a new minor therefore exercises the new workflow with `v1`'s actions until the lead
moves `v1` (§9).

Permissions: `contents: read`, plus `statuses: write` for claim and verdict. A caller
grants the same at the call site.

## 5. Hosts

### 5.1 Runner pools

Every runner also carries GitHub's `self-hosted`, `Linux` and `X64` labels. A class
label decides which repository classes the hook accepts (§6.3).

| host | pool (class) | instances | labels | runs |
|---|---|---|---|---|
| 3090b | `light` | 2 | `light`, `3090b` | claim, plan, place, verdict, salvage, merge gates |
| 3090b | `part` | 4 | `part`, `3090b` | CPU and GPU parts of private consumers |
| 3090b | `compile` | 1 | `compile`, `3090b` | exclusive-lock timing parts |
| 3090b (drives h100) | `h100` | 5 | `h100`, `h100-gpu<N>` | GPU parts on card N, over ssh |
| 3090a | `light` | 1 | `light`, `3090a` | light jobs when 3090b is down |
| 3090a | `part` | 3 | `part`, `3090a` | CPU and GPU parts |
| 3090a | `nightly` | 1 | `nightly`, `3090a` | nightlies, up to 600 min |
| 3090b, 3090a (RUN-4) | `gpu` | 1 each | `gpu`, host | GPU-only parts, including public consumers' |
| h100 (RUN-5) | `light` | 2 | `h100-light` | claim, verdict, salvage of `h100-test.yml` |
| h100 (RUN-5) | `part` | 2 | `h100-cpu` | CPU parts of private consumers, pinned to the inventory's `cpus` |
| h100 (RUN-5) | `h100` | 5 | `h100`, `h100-gpu<N>` | GPU parts on card N, run locally |

Pool size is the host's slots plus one waiting seat per project allowed on the pool
(§7). Runner names are `<host role>-<pool>-<i>`. All are org-level in Default (lead
decision 1), except public consumers' GPU runners (§12 item 2).

RUN-5's h100 runners replace the row "3090b (drives h100)" while RUN-2 is not built; the
two never coexist on the same cards.

**h100's labels share nothing with the 3090 pools.** GitHub gives a job to any runner
that has all its labels, and the hosts keep separate policies: 3090b's lists brainspike
alone. With plain `light` or `part` labels on h100, a job of another private repository
could be given to a 3090b runner and be refused there, and brainspike's `[light]` jobs
could land on h100. So h100's light and CPU runners carry only `h100-light` and
`h100-cpu`. Their runner class (`CI_RUNNER_CLASS`) is still `light` and `part`, which is
what the hook and the executor read. `setup` takes the labels from the inventory's pool
entry where it gives them (RUN-5). The `h100` pool is `per_card`: one runner per card,
named `h100-gpu<N>` rather than `<role>-<pool>-<i>`, labelled `h100` and `h100-gpu<N>`,
with `CI_RUNNER_CARD=<N>` in its `.env`. On h100 the `part` class runs CPU parts only: the
executor refuses `gpus > 0` there (`unavailable`), so only the `h100` runners lease
cards.

### 5.2 Setup scripts

All are idempotent and run as the runner account. None needs sudo.

- `setup/plan.sh <host>` reads the inventory and prints every change it would make:
  files with their diffs, units, `.env` lines, `authorized_keys` lines and runner
  registrations. It changes nothing. The lead approves this output.
- `setup/apply.sh <host> --plan <file>` applies exactly an approved plan. It refuses if
  the plan's hash differs from a fresh `plan.sh` output. It writes a state file, and a
  second run is a no-op.
- `setup/install.sh --tag <tag> --expect <sha>` installs a release into
  `releases/<tag>/` after checking that the tag resolves to the commit the lead
  released (an annotated tag by its peeled commit, matched by exact ref name).
  `--commit <sha>` installs `releases/main-<sha12>/`, the bootstrap before the first
  tag. It never moves what running jobs use (§9): an existing release is kept only if
  it is a real directory holding that commit, and is refused otherwise. `releases/hook`
  moves only forward (to a higher tag, or from a main commit to anything); moving back
  needs `--rollback`.
- `setup/register.sh <runner>...` is run by the lead with a fresh registration token on
  stdin. It calls the runner app's `config.sh --url https://github.com/chaobrain
  --runnergroup Default --labels ... --work ~/data/ci-work/<name> --unattended` under
  `env -i`, with the token in `ACTIONS_RUNNER_INPUT_TOKEN` (never in an argv, never
  stored) and only the proxy's three variables read from `.env` as data. It refuses
  while any runner of the account is active, since job code runs as that account and
  could read the token from the environment of `config.sh`. It starts the runners
  once all are configured. Auto-update stays on.
- `setup/uninstall.sh <host>` reverses `apply` from its state file: it disables each
  unit on its own, removes only the unit files the state file records (nothing if a
  unit is still active), leaves the runners for the lead to deregister, and leaves
  `etc/` for the lead to delete.

### 5.3 System changes per host (the lead approves the plan before it runs)

**3090b, RUN-1** (RUN-2 adds the h100 rows):

| change | detail |
|---|---|
| directories | §3.2 under `~/data`; `etc/` mode 700 |
| runner app | the pinned `actions/runner` release, checked by sha256, into 7 runner directories (RUN-2: 12) |
| `.env` per runner | `https_proxy`, `http_proxy`, `no_proxy` (localhost, the intranet); `ACTIONS_RUNNER_HOOK_JOB_STARTED` and `..._COMPLETED` pointing at `~/data/ci-runners/hooks/`; `CI_RUNNER_CLASS`; `CI_RUNNERS_ETC` |
| `ci-runner@.service` (template) | `ExecStart=%h/data/actions-runner/%i/run.sh`; `After=` and `Wants=ci-proxy.service`; `NoNewPrivileges=yes`; `KillMode=mixed`; `Restart=always`, `RestartSec=30`; `Slice=ci.slice`; enabled per instance |
| `ci-proxy.service` | a separate instance of the host's proxy client, with its own config copy and node (inventory) and its own local ports. The interactive proxy is left alone (§12 item 4). `Restart=always`. |
| `ci-proxy-check.timer` | every 2 min, a request to `api.github.com` through the CI proxy. Any HTTP answer counts as working (a 403 or 429 is GitHub's rate limit on the shared egress, not the node). After 3 failures in a row it switches to the next allowed node and restarts the proxy. State goes to `state/proxy.json`. |
| `ci-reaper.timer` | every 10 min: stops part scopes whose job is gone (a scope outlives a stopped runner), removes stale work directories, the part records of runs with nothing changed for 14 days, daily hook logs (`logs/hook-<date>.log`) over 14 days, stale tickets and leases, and (RUN-2) orphaned h100 job directories |
| slices | `ci.slice` `MemoryMax` (inventory; proposed 120G of 188G) and `ci-<project>.slice` per project |
| lock and slot files | none new: the inventory names brainspike's current files (D5) |
| ssh (RUN-2) | a CI key pair in `etc/ssh/`; a CI-only `ssh_config` passed with `-F` (§5.4) |
| unchanged | linger (already on), sudoers, crontab, the interactive proxy and shell files, brainspike's checkouts and envs |

**3090b with a dedicated CI account (RUN-1b; proposed, for the lead's approval).** The
3090b account holds a GitHub token with `admin:public_key`, so job code never runs as it
(decision 4's preferred path, taken first). The runner account above is then a new,
dedicated account with no sudo and no GitHub credential. A few one-time root steps come
before `setup/plan.sh`, and the lead approves them with the plan:

| change | detail |
|---|---|
| account | a new account with no password and no sudo; linger on; its `~/data` is a directory on the data disk |
| shared lock and slot files | 3090b sets `fs.protected_regular = 2`. A second account therefore cannot open another account's files in `/tmp` with `O_CREAT`, which `host_lock.sh` (`>>`) and flock(1) do. brainspike's lock files become root-owned, with ACL entries giving both accounts `rw`. The queue directory gets ACL and default-ACL entries for the CI account. A `tmpfiles.d` file recreates both at boot, because `/tmp` is emptied then, and excludes them from age cleaning. Their paths do not change (D5). |
| proxy client | a copy of the host's proxy client, and the source node configs (mode 600), are placed into the CI account's tree |
| envs | the project's env is built as the CI account (consumers §2 step 3); nothing in the maintainer's tree is shared |

**What a dedicated account does not close (for the lead's decision).** Job code runs as
the CI account, and that account owns the host layer: `releases/`, `hooks/`,
`etc/policy.toml`, each runner's `.env`, the runner app and the user units. Read-only
mode bits do not stop an owner. So one accepted job, or a dependency it installs, can
turn the hook off for every later job on the host (T1–T3 then rest on GitHub's own
settings alone, §2). It cannot reach the maintainer's token, files or sudo. Two further
facts:

- the runner prints `Run '<hook path>'` in every job's log, refused jobs included, so
  the hook's path (and with it the account's home) is in public logs once a public
  repository uses the runners. Role-named paths (`/home/<ci account>/...`) are the
  mitigation; the account's name is chosen to be generic.
- registration tokens and the hook's log are readable by any job of the account.

Options: (a) accept the gap for RUN-1 (private consumers whose code the lead reviews;
the exit is (b) or (c)); (b) a root-owned host layer: `releases/`, `hooks/`, `etc/`, the
`.env` files and the units owned by root (or the maintainer) and read-only to the CI
account, with system units running as the CI account (`User=`), which moves setup to
sudo; (c) ephemeral runners (`--ephemeral`) re-created from a root-owned template after
each job. Recommendation: (a) now, (b) before a public repository uses the runners.
Decided (lead, 2026-11-05; decision 10.2–10.3): (a) for private consumers now. Before
any public repository uses the runners, one separate task moves the hook and the
policy to a root-owned host layer (b) and the hook to a generic path such as
`/opt/ci-runners`, so that job logs no longer show the CI account's home.

**3090a, RUN-3**: as 3090b, with 5 runners (§5.1) and no ssh key to h100. Also:

- 3090b's CI public key in `authorized_keys`, with `restrict`, for `place` and
  `salvage`;

- `ci.slice` `MemoryMax=80G` (125 GiB host; the cap of today's `brainspike-rci.slice`);
- the reservation entry for brainspike's nightly, naming its current lock, yield and
  marker files;
- 3090a's own node in the inventory.

**h100, RUN-2**:

- one `authorized_keys` line for the CI public key, with `restrict` (no forwarding, no
  pty);
- the `ci-work/` directory under our subtree.

Envs and upstream sources are those of today, put under the manifest (§5.4). There are
no units, no cron, no proxy change, no runner, no hook and no GitHub credential. Option
A needs no GitHub access on h100: everything arrives from 3090b.

**h100, RUN-5** (decision 11.3; the lead approves the plan, as for 3090b). Measured on
2026-11-06 from the shared account: linger on; `systemctl --user` works; 2 TB of memory,
176 CPUs, 5 cards; `api.github.com` answers directly, `github.com` and the package
indexes do not (decision 11.8); not in the docker group; the system `python3` is 3.10.

| change | detail |
|---|---|
| directories | §3.2 (RUN-5) under our subtree; `etc/` mode 700 |
| runner app | the pinned release, checked by sha256, into 9 runner directories (§5.1) |
| `.env` per runner | the two hook variables; `CI_RUNNER_CLASS`; `CI_RUNNERS_ETC`; `CI_RUNNERS_PYTHON` (for the launcher and the units; the hooks have the interpreter written into them by setup, so a job's environment never chooses it); `CI_RUNNER_POOL_LABELS`, the pool's own labels (`h100-light`, `h100-cpu`, `h100`), for a retry's `retry-on`; on an `h100` runner `CI_RUNNER_CARD=<N>`, its card; `https_proxy` and `http_proxy` at the bridge, and `no_proxy` (inventory `tunnel.no_proxy`: localhost and the hosts that answer directly) |
| `ci-runner@.service` | as on 3090b, with `After=`/`Wants=ci-bridge.service` instead of the CI proxy |
| `ci-tunnel.service` | `ssh -F etc/tunnel/ssh_config -N -D 127.0.0.1:<socks port>` to the egress account (decision 11.8): batch mode, the egress host's key pinned in `etc/tunnel/known_hosts`, `ExitOnForwardFailure`, keep-alive every 30 s (a dead link ends after 90 s); `Restart=always` after 10 s |
| `ci-bridge.service` | `ci-run bridge`: HTTP CONNECT on 127.0.0.1:<http port>, to `tunnel.allow` names on port 443 only (anything else: 403), handed to the tunnel as SOCKS5 with the name; at most 256 connections; one journal line per connection (name, result) |
| `etc/tunnel/` | the key pair, `ssh_config` and `known_hosts`, placed by the lead before the plan; `plan.sh` refuses without them, or with the key readable by others |
| `ci-orgsync.{service,timer}` | not on h100 (decision 11.9): the workstation pushes the snapshot |
| `ci-reaper.timer` | as on 3090b |
| slices | `ci.slice` `MemoryMax` (inventory; proposed 512G of 2 TB) and `ci-<project>.slice` |
| lock and slot files | new ones under `state/`: h100 has no brainspike files to share (D5 does not apply) |
| unit names | every unit file starts with `ci-` (`ci.slice` included); `uninstall.sh` stops and removes only the units an `apply` recorded in `state/setup.json`; `plan.sh` refuses if a unit of that name exists and no earlier `apply` wrote it (another user of the account may have one). The unit directory exists (mode 700) and is the account's: setup never changes its mode. |
| unchanged | the shared account's shell files, crontab, `authorized_keys`, other users' files, envs and units; no proxy client |

There is no root step on h100: the shared account has no sudo for us, and none is
needed. The egress host takes one, made by the lead (done 2026-11-06): a system account
with no password, `nologin` as its shell, and one `authorized_keys` line for the tunnel
key, restricted as decision 11.8 states. Its sshd configuration is unchanged.

**The maintainer's workstation, RUN-5** (decision 11.9), as the maintainer, not as a CI
account:

| change | detail |
|---|---|
| `ci-orgsync-push.{service,timer}` | user units of the maintainer's account, every 10 min: `ci-run org-sync --fetch` from a checkout of a release, with the login from `gh auth token`, piped to `ssh <h100> <release>/bin/ci-run org-sync --receive`; `TimeoutStartSec=5min` |
| unchanged | the maintainer's gh login and ssh access to h100 (both exist); nothing is stored |

### 5.4 The h100 driver (option A)

*RUN-5 (decision 11.3) does not build this driver.* With the runners on h100, the local
executor of §4.3 runs a part there, as on 3090b. It keeps these pieces of this section,
run locally: the health gate (phase 1, without the package temperature probe if the
inventory gives none), the sampler and early stop (phase 5), thermal and reboot
classification (phase 6), CPU pinning to the inventory's `cpus`, and D7. Each `h100-gpu<N>`
runner runs its parts on card N alone: setup writes `CI_RUNNER_CARD=<N>` into its `.env`. The integrity
check (phase 2), the archive transport (phase 3), the env allowlist and the draw
(phase 8) do not apply (decision 11.5). The rest of this section is RUN-2's.

A `part` step on an `h100-gpu<N>` runner (on 3090b) runs these phases, all over one
ssh control connection:

1. **Health gate.** Card N must be below 80 °C and not in heavy use by others. Package
   1 must be below 90 °C, `scaling_max_freq` above 1.2 GHz, and the watchdog's notice
   file must be absent. If not, the outcome is `unavailable` and `retry-on` names a
   card that passes (`h100-gpu<M>`), else the fallback host.
2. **Integrity.** Check the project's h100 manifest (below). A mismatch gives
   `unavailable` ("manifest: <up to 5 paths>"). Two mismatches in 24 h quarantine h100
   in 3090b's state: `place` skips it until a maintainer rebuilds the manifest.
3. **Prepare.** Make a fresh `ci-work/<run>-<attempt>-<job>/`, record the boot id, and
   send `git archive --format=tar <sha> | ssh ... tar -x`. No `.git` arrives, so a
   command that needs history cannot run on h100.
4. **Run.** Over ssh, with stdin a generated script and the command
   `exec env -i <allowlisted vars> bash --noprofile --norc -s`. The script raises
   `nofile` to the hard limit and enters the directory. It then runs
   `systemd-run --user --scope --unit=ci-<id> -p MemoryMax=128G -p MemorySwapMax=0
   taskset -c 0-43,88-131 nice -n 10`, with the project's env activated and
   `CUDA_VISIBLE_DEVICES=N`. Output streams to the job log and to `log`.
5. **Sample.** Every 30 s, record the card's temperature and use, and our compute PIDs
   (`--query-compute-apps` intersected with the scope's `cgroup.procs`), into the
   record's `samples.csv`. If our card stays at 85 °C or more for 3 min, stop the scope
   (`thermal`, early stop). The watchdog throttles at 88 °C for 10 min and kills 10 min
   later.
6. **Classify** (§4.4) and fetch the JUnit files back.
7. **Clean up.** `systemctl --user stop ci-<id>.scope` kills the whole cgroup: there is
   no `pkill` on a shared account. Then `rm -rf` the directory. The job-completed hook
   repeats this if the step was killed. The reaper repeats it for any directory whose
   3090b job (PID and start time in the record) is gone.
8. **Draw.** If `HMAC-SHA256(verify.key, repo|sha|job|attempt) mod 1000 < 100`, output
   `verify=true`.

**Env allowlist.** The remote sees only:

- a fixed `PATH`, `HOME` and `LANG`;
- `CUDA_VISIBLE_DEVICES` and our `CI_*` variables;
- the names in the project's `remote_env`.

`GITHUB_*`, `ACTIONS_*`, `RUNNER_*`, `SSH_AUTH_SOCK` and every name containing `TOKEN`,
`SECRET` or `PASSWORD` are refused, even if a project lists them.

ssh runs with `-F <CI ssh_config>`, so the user's config and its `SendEnv` do not
apply. It uses `-a -x -o ClearAllForwardings=yes -o BatchMode=yes
-o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=4` and a
`ControlPath` under `state/`.

**Manifest.** `ci-run manifest build --project <p> --remote h100` runs after an env
build or an upstream sync and writes `etc/manifests/<p>/h100.json` on 3090b. It holds:

- the sha256 of every file under the env's `conda-meta/`, `site-packages/` and
  `bin/python*`;
- for each upstream source, its HEAD, an empty `git status`, and its tracked files'
  blob hashes, checked against 3090b's checkout of the same commit.

Upstream syncs come from 3090b by `git archive` of the commit 3090b holds, under the
project's env lock on h100. The env's hashes are trusted on first use, at build time.

**Limits, stated plainly.** Everything on the shared account, including the shell
startup that runs before our `env -i`, is under other users' control. The manifest
catches drift and casual tampering, not a determined co-user. Three things bound the
rest: h100 never runs checks, CPU or compile parts; the keyed 10 % verify; and the exit
path of decision 3, safeguard 4.

**GPU facts carried over.**

- P2P copies among cards 0-3 from a lower to a higher index silently return zeros, so a
  part with `gpus > 1` on h100 is `unavailable` (D7).
- Per-core speed is about 2.7 × below 3090a (`cpu.4`: 2m45s against 59-62 s).
- The cards sit on NUMA node 1. Parts are pinned to node 0 to keep load off the hot
  package 1.

## 6. Security model

### 6.1 Per host

| host | runs as | what job code can reach | mitigation now | exit path |
|---|---|---|---|---|
| 3090b, 3090a | the maintainer's account | the maintainer's GitHub token where one is stored (3090b, 2026-11-05: `repo`, `workflow`, `admin:public_key`, `read:org`, `gist`; `admin:public_key` would let job code add an ssh key to the account), personal ssh keys to other hosts, all of that account's files; root through passwordless sudo | the hook's allowlists; `NoNewPrivileges=yes` (sudo refused, measured); no stored secrets; least-privilege `GITHUB_TOKEN`; per-job work directories; `admin:org` is granted only on the lead's laptop, never on a runner host | a dedicated CI account per host, without the token, with ssh keys only to the other CI accounts (needs a host admin) |
| h100 | the shared account, driven from 3090b | our snapshot of the head, our envs, other users' files there (the account is in the docker group: effectively root) | option A: no GitHub credential there; env allowlist; archive transport; GPU parts only; manifest; keyed verify; kill by scope only | a dedicated account from the host's admin, then one runner per card there; retire the driver |
| h100, RUN-5 | the shared account, runners on it (decision 11.3) | everything a co-user of the account can reach, and they can reach everything of ours: the runner credentials, the hook, the policy, each job's `GITHUB_TOKEN` while the job runs, the checkout of a private repository, the results and the statuses we post | private repositories only, of org members only (decision 11.1, 11.2); `GITHUB_TOKEN` with `contents: read`, plus `statuses: write` only in claim and verdict; per-job directories, deleted after the job; no other credential there but the tunnel key, which opens only allowlisted HTTPS (T15); `NoNewPrivileges` | the same dedicated account (decision 11.7) |

`InaccessiblePaths` (hiding the token directory from the runner) does not take effect in
a user unit on 3090b (measured 2026-11-05), so it is not used.

### 6.2 Per repository class

| class | events accepted | actors | runner classes |
|---|---|---|---|
| private, allowlisted (on h100: every private repository of the org snapshot, §6.5) | `pull_request` (same repository), `push`, `workflow_dispatch`, `schedule`, `workflow_run`, `status`, `repository_dispatch` | `GITHUB_ACTOR`, `GITHUB_TRIGGERING_ACTOR`, and for a pull request its author and for a push its pusher, all on the actor allowlist (on h100: the org's members, §6.5). The workflow bot is accepted only for `workflow_dispatch` and `repository_dispatch`, and only where the repository's `bot_dispatch` switch is on (off by default; decision 10.1). | all |
| public, allowlisted | `pull_request` with `head.repo.full_name == repository`, `push`, `workflow_dispatch` | as above, humans only (no bot) | `gpu`, `h100` |
| any other repository, including ci-runners itself | none | none | none |

`pull_request_target`, `issue_comment` and other events are refused everywhere unless
the policy lists them for a private repository.

### 6.3 The hook (`job-started`)

`python -I` reads the inventory, the policy and `GITHUB_EVENT_PATH` as JSON, and does
no network I/O. On h100 it also reads the org snapshot (`etc/org.json`, §6.5). It bounds itself at 60 s (an alarm; if that fails, its wrapper kills it at 65 s, which refuses). It applies §6.2 to the runner's class
(`CI_RUNNER_CLASS`) and checks that the event's repository id matches
`GITHUB_REPOSITORY_ID`. It also removes stale work directories of this runner.

It prints one line, `ci-runners: accepted|refused <reason code>`, followed by the
policy's hash, and logs details on the host only. The job-completed hook stops the
job's scopes, releases its leases and slots, and removes its work directory (120 s
bound).

### 6.4 Threats and mitigations

| # | threat | mitigation |
|---|---|---|
| T1 | A fork pull request on a public repository asks for a self-hosted label by editing the workflow in the fork. | the flag stays off, or repo-level GPU runners only (§2); the hook (fork, actor, repository, class); no docker for the runner account; `NoNewPrivileges` |
| T2 | A member not on the allowlist pushes a workflow that asks for our labels (the base permission is write everywhere). | the hook's actor checks |
| T3 | A new or unlisted org repository asks for our labels (Default is open to all private repositories). | the hook's repository allowlist; the repository id is checked against the name |
| T4 | Someone moves `v1` in ci-runners to bad code. | D1 rulesets (only the lead can update or delete `v*` and `main`); the hook and h100 driver come from installed releases (D2) |
| T5 | Job code from an allowlisted actor reads the maintainer's token or ssh keys (with `admin:public_key` it could add a persistent ssh key to the GitHub account), or uses sudo. | `NoNewPrivileges`; `admin:org` never on a runner host; dedicated CI accounts (exit path); the consumer reviews its own code |
| T6 | (RUN-2; on RUN-5 an accepted gap, §6.1, decision 11.3) Users of the shared h100 account steal credentials, tamper with envs or results, read our source, or kill our processes. | none of our credentials there; manifest; archive of one sha, deleted afterwards; GPU parts only; keyed 10 % verify; kills end as thermal or lost and are retried. Reading the snapshot is accepted, as S7 accepted it. |
| T7 | Our job code harms other users of the shared account. | only allowlisted actors' code; per-job directories; kill by scope; the lead tells the host's admin what runs there |
| T8 | Secrets leak to h100 through the environment. | env allowlist with refused patterns; `env -i`; CI-only `ssh_config`; a test dumps the remote environ (RUN-2) |
| T9 | The hook's output leaks inventory in public logs. | a reason code only; the inventory lint. The runner itself logs the hook's path (§5.3): the CI account's name is generic |
| T10 | A hostile event payload targets the hook. | JSON parsing in `python -I`; no shell interpolation; fixtures with hostile strings |
| T11 | A green status is forged: anyone with write can post statuses on Free. | `claim` counts only the bot's and allowlisted actors' statuses; consumers audit merges (brainspike: daily) |
| T13 | (on RUN-5 also any co-user of h100's account, an accepted gap) An accepted job (or its dependency) edits the host layer it owns: the hook, the policy, `.env` or the units, so later jobs pass unchecked. | private consumers only until the host layer is root-owned (§5.3, option (b)); nothing on the host detects an edited hook, so a changed policy hash in the hook lines shows only a changed policy; the hook writes its log without following links or blocking on FIFOs |
| T14 | (RUN-5) A co-user of h100's shared account reads the org-sync token, or edits the snapshot. | no token on h100 (decision 11.9): the snapshot arrives over the maintainer's ssh access, checked by `--receive` (§6.5). A host that syncs itself holds a fine-grained read-only token (members, metadata): it shows the member list and the private repositories' names, nothing else. An edited snapshot is T13's case. |
| T15 | (RUN-5) A co-user of h100's shared account takes the tunnel key, or any local user of h100 uses the bridge or the SOCKS port on 127.0.0.1. | the egress account has no shell and no listening forward, and opens only the allowlisted names on port 443 (decision 11.8): HTTPS to GitHub and the package indexes, which job code reaches through the bridge anyway. They appear as the egress host's address. The lead revokes the key by deleting one line on the egress host. |
| T12 | A burst of refused jobs blocks our runners. | refusal in under 10 s (RUN-1 acceptance); repo-level public runners do not share pools with private ones; the lead can pause a repository in the policy |

### 6.5 The org snapshot (RUN-5)

`ci-orgsync` runs every 10 min on h100, as the runner account, with `etc/orgsync.token`.
It reads `GET /orgs/chaobrain/members` and `GET /orgs/chaobrain/repos?type=private`,
following every page, and writes `etc/org.json`: `{schema: 1, fetched_at, members:
[login], repositories: {owner/name: id}}`, lower case. It writes a temporary file and
renames it, mode 600. A failed or partial fetch leaves the old snapshot in place. 30 s
per call, 3 tries.

`policy.toml` schema 2 adds `[org]`: `name`, `members = true`, `private_repositories =
true` (each snapshot repository joins `repositories` as private) and `max_age_minutes =
60`. With `members`, the snapshot's members may act **only in the org's private
repositories of the snapshot**: a repository written by hand is open to them only if it
is private and the snapshot holds it with the same id. A public repository written by
hand (the GPU path of decision 2) keeps the hand-kept `actors` alone. A repository written in `policy.toml` keeps its
own entry, so `paused`, `extra_events` and `bot_dispatch` stay the lead's. A public
repository is never added from the snapshot: the call asks only for private ones, and a
snapshot entry already listed as public is ignored. Schema 1 is read as before (3090b).

The hook refuses every job (`stale`) when the snapshot is older than `max_age_minutes`,
and refuses with `policy` when the snapshot is missing, too large or malformed. It
prints the hash of the policy and of the snapshot. `claim` takes its allowlist from the
same merge.

Org members are humans; a bot or an outside collaborator is not one, so both stay
refused. A member who leaves the org is refused within 10 min, or 60 min if the fetch
fails meanwhile.

**The push (decision 11.9).** `ci-run org-sync --fetch --org <name>` takes the login from
`GH_TOKEN`, makes the same document and prints it; it writes nothing. `ci-run org-sync
--receive` reads at most 4 MiB from stdin and writes `etc/org.json` as above, but only if
the document loads as the hook loads it, every repository in it belongs to the policy's
`[org]` name, its `fetched_at` is no later than 5 min from now and no earlier than the
snapshot in place (a replayed old snapshot cannot bring back a removed member). Anything
else leaves the old snapshot, exits 1 and prints why.

## 7. Scheduling and fairness

Shared resources, per host:

- **CI slots** (full runs): 2 on 3090b and 2 on 3090a, the files brainspike uses today
  (D5). On h100 each card is its own slot. On h100 with RUN-5, CPU parts take the
  host's own CPU slots (inventory `slots`, proposed 2), in files under `state/`.
- **Host lock**: brainspike's `host_lock.sh` protocol exactly (`-s`, `-x`, `-l`, the
  turnstile and the shared marker), reimplemented in `ci-lock` and tested against the
  original. An `-x` request still waits for shared holders already in, so every
  project keeps a shared hold to about 15 min and splits longer commands (brainspike
  L57).
- **GPU leases**: per card, at most `max_parts` parts (default 1), and the S2
  free-memory check at lease time.
- **Memory**: `ci.slice`, then `ci-<project>.slice` (the project's cap), then the part's
  scope.
- **Reservations**: an owner job can hold a host. brainspike's nightly on 3090a is the
  first. Parts hold the reservation lock shared. The owner waits a grace period (1 h),
  then stops their scopes, and they end `yielded`. These are the same files as today's
  `pr.lock` and `pr.yield`.

Rules:

1. **Queue order.** First come, first served, but a ticket whose project already holds
   its cap is skipped. When several projects wait, the next slot goes to the project
   that holds the fewest slots, and ties go to the older ticket.
2. **Per-project caps** come from the registry: `max_slots` per host (brainspike: 2 on
   each 3090; others: 1), `max_cards` on h100 (brainspike 3, others 2), `memory_max`,
   and `max_waiting` per pool (default 1). A part beyond `max_waiting` ends
   `unavailable` ("project queue full") and goes to its fallback. So one project cannot
   fill a pool with waiters, and GitHub's queue, which is not fair across repositories,
   stays short.
3. **Light jobs never wait behind heavy ones**: separate `light` pools, and `-l`
   semantics on the host lock.
4. **Compatibility.** brainspike's `ci_slot.sh` tickets carry no project, so they count
   as the host's default project (brainspike). Its waiters do not apply caps, so the
   default project's cap equals the slot count until brainspike calls `ci-slot`.
5. **Accounting.** `ci-run report` prints parts, queue time, run time and retries per
   project, host and day. Process metrics stay on the hosts.

## 8. Failure modes and bounds

| failure | effect | handling |
|---|---|---|
| proxy down on a host | its runners drop offline; queued jobs for its labels wait | `ci-proxy-check` switches node after 3 failures (6 min); `place` skips a host whose proxy fails; running jobs end `lost` and are salvaged |
| runner offline mid-part | GitHub fails the job without outputs | `lost`: `salvage` reads the record, else one retry |
| host reboot | running parts end; with linger the units start again, proxy first | `reboot` (h100: thermal); the reaper cleans up |
| thermal kill or early stop (h100) | the part's processes die, or we stop them | `thermal`: one retry on a cool card, else fallback |
| OOM | the scope is killed | `oom`: one retry with `resume` on a host with headroom |
| h100 unreachable or quarantined | no part placed there | fallback order of the project |
| h100's tunnel down (RUN-5) | the runners stay online (their own traffic is direct, `no_proxy`); a checkout or a package download in a job fails, and that part ends red | ssh's keep-alive ends a dead link in 90 s, and systemd restarts it after 10 s; the bridge answers 502 meanwhile |
| the workstation down, or its push failing (RUN-5) | the snapshot ages | every h100 job refused `stale` after 60 min, until the push is back |
| slot or lease starvation | a part waits | `queue-timeout-minutes`, then `unavailable` and fallback |
| disk low (under 50 GiB free in `~/data`) | | `unavailable` before the command |
| hook hangs | the job blocks in "Set up runner" | the hook's own 60 s bound, then the fixed refusal line; the wrapper's `timeout` at 65 s |
| GitHub down | nothing is assigned | the consumer's local path (brainspike: local runs, decision 2) |

| bound | value |
|---|---|
| job-started hook, job-completed hook | 60 s, 120 s |
| `claim` API calls | 30 s each, 3 tries; the wait as input |
| `place` | 60 s per host probe, 5 min in total |
| slot or lease wait | 60 min (input) |
| ssh | connect 20 s; silent 2 min (30 s × 4) drops the connection; a remote out of contact for 30 min is `lost` |
| sampler; early stop | every 30 s; 85 °C for 3 min |
| a part | the consumer's `timeout-minutes`, at most 240 (`nightly`: 600) |
| reaper | every 10 min |
| proxy check | every 2 min, node switch after 3 failures |
| runner restart | 30 s after a failure |
| GitHub | queued 24 h fails a job; a job lasts at most 5 days; offline 14 days removes a runner |

These are starting values, tuned from RUN-1's measured runs.

## 9. Versioning and rollout

- **Tags.** Releases are immutable `vMAJOR.MINOR.PATCH` tags, plus a moving major tag
  `v1`. Consumers pin `@v1`; a canary pins the exact tag. Rulesets reserve tag changes
  for the lead (D1).
- **Release stamp.** At release, `actions/RELEASE` is stamped with the release (on
  `main` it reads `hook`, the release the hook runs). Every action calls
  `actions/lib/ci-run.sh`, which runs
  `~/data/ci-runners/releases/<that release>/bin/ci-run`, so `@v1.4.0` always runs
  1.4.0's host layer; one file is stamped, not every `action.yml`. A host without the
  release ends a part `unavailable` (to its fallback). On a GitHub-hosted runner only
  `claim` runs, from the action's own checkout, with `--hosted`. A host keeps every release that a major tag
  points at or pointed at in the last 14 days.
- **Hook and policy.** These are host-level and not pinned by consumers. A new release
  of the hook must accept every major still installed.
- **Rollout of `v1.N`:**
  1. A PR with tests (hosted unit tests, fixtures, a live check on one host).
  2. The lead tags `v1.N.0`.
  3. `install.sh` on every host; nothing switches yet.
  4. A canary consumer branch pins `@v1.N.0` and passes.
  5. The lead moves `v1`. The hosts already have the release, so the order is safe.
  6. Rollback: move `v1` back. The old release is still installed.
- **Breaking changes** get `v2`. `v1` stays tagged and installed until its last
  consumer has migrated. The registry lists each consumer's major, and the executor logs
  the release that called it.
- **Inventory schema.** `schema = N` in `inventory.toml`. The executor refuses an
  unknown schema (`unavailable`) rather than guess.

## 10. Phases

Each phase is one or more PRs of at most about 1,500 lines, each carrying its evidence.

**RUN-0, this spec.** *Acceptance:* the lead approves it and decides §12 (done 2026-11-05). AGENTS.md is
merged.

**RUN-1: 3090b runners and the generic executor.** Sub-PRs:

- RUN-1a: the package's pure functions and tests on hosted runners;
- RUN-1b: the host layer, hooks, setup and units, with live checks on 3090b;
- RUN-1c: the actions and `v1.0.0`.

*Acceptance:*

1. Unit tests for the hook policy, claim, place, outcome classification, the fair
   queue, the env allowlist and the inventory lint, each failing on a mutant. The
   fixtures include hostile payloads: a fork head, a bot actor, an unlisted repository,
   `pull_request_target`, and a public repository on a `part` runner.
2. Interop: `ci-lock` and brainspike's `host_lock.sh` exclude each other in every mode
   pair. `ci-slot` and `ci_slot.sh` together never exceed the slot count.
3. The lead-approved plan is applied on 3090b and the runners are online at org level.
   They return within 5 min, measured, after `systemctl --user restart` and after a
   proxy restart. After a reboot if one happens, else stated as not tested.
4. In a live job, `sudo -n true` fails. A job from an unlisted repository and one from
   an unlisted actor each fail in "Set up runner" in under 10 s, and print no
   inventory.
5. A refused job whose workflow uses a Dockerfile action shows whether anything ran
   before the hook. The result is recorded in §2.
6. Fault injection: a runner stopped mid-part ends `lost` and is salvaged; the proxy
   stopped at place time makes the host skipped; a part's scope OOM-killed ends `oom`
   and is retried. Each happens within its bound.
7. The inventory lint passes. The lead makes ci-runners public and adds the rulesets
   before `v1.0.0` is tagged.
8. brainspike's CI-1a runs its shadow graph on it (by brainspike's own acceptance).

**RUN-2: h100, option A.** *Acceptance:*

1. Pure functions for the health gate, card choice, thermal classification (fixture
   watchdog lines with bus ids, PID matching), boot ids, the keyed draw (rate
   10 ± 1 % over 10,000 seeded draws) and `compare`, each failing on a mutant.
2. A part that dumps its environ on h100 shows only allowlisted names. Nothing under
   our subtree holds a GitHub token pattern.
3. The 3090b job is killed in each phase (prepare, transfer, run, sample, cleanup).
   After the reaper's next pass, no process and no directory of it is left on h100.
4. A changed `.py` in `site-packages`, and separately a dirty upstream checkout, each
   send the part to its fallback. The check's time per part is measured and reported.
5. A fixture watchdog kill line gives `thermal` and one retry on another card. A forced
   hot reading triggers the early stop.
6. A part with `gpus: 2` on h100 is `unavailable`.
7. One real brainspike `gpu` part runs end to end, with its summary block. A forced
   disagreement fails the head.

**RUN-3: 3090a.** *Acceptance:* the runners are online. The reservation and yield work
with brainspike's nightly files on a test night. With the proxy stopped on 3090a, parts
go to 3090b within the bounds. brainspike's CI-3 runs the nightly on the `nightly`
pool.

**RUN-5: the standard test CI on h100** (decision 11). Sub-PRs: the org snapshot and
policy schema 2; h100 as a local host in setup (no proxy, the inventory's `python` and
labels); the local health gate, sampler and early stop; `h100-test.yml` and
`ci-run env build`; h100's egress (tunnel and bridge, decision 11.8); the snapshot push
(decision 11.9); then `v1.1.0`. *Acceptance:*

1. Unit tests, each failing on a mutant: a snapshot member's push is accepted; an outside
   collaborator, a non-member and a bot are refused; a public repository, and a private
   one whose id differs from the snapshot's, are refused; a stale, a missing and a
   malformed snapshot are refused; schema 1 decides as before; a hot card is refused
   with a cool card's label; the early stop; a watchdog kill line counts only with our
   PID and our card's bus. The bridge refuses a name off its list, a port other than 443
   and a request that is not CONNECT, and answers 502 when the tunnel refuses.
   `--receive` refuses another org's snapshot, an older one than in place and one from
   the future.
2. The lead-approved plan is applied on h100, and its 9 runners are online at org level.
   They return within 5 min after `systemctl --user restart`.
3. In a live job, `sudo -n true` fails. A job from a public repository, one from a
   non-member and one from a fork pull request each fail in "Set up runner" in under
   10 s, and print no inventory. A job of another private repository given
   `[self-hosted, light]` is not taken by an h100 runner.
4. A member added to the org can run within 10 min. A snapshot made older than 60 min
   refuses with `stale`. From h100, `github.com` answers through the bridge; a name off
   the bridge's list gets 403, and a forward to a name off the egress account's list is
   refused there.
5. A canary private repository: a failing CPU test and a failing GPU test each end red in
   their own part, and only there. A part OOM-killed ends `oom` and is retried; a stopped
   runner ends `lost` and is salvaged; a forced hot reading stops the part as `thermal`,
   retried on a cool card.
6. Five heads of the canary run end to end, with their statuses and summaries right.

**RUN-4: the first public-repo GPU consumer**, brainevent (§12 item 5).
*Acceptance:* the lead registers its repository-level GPU runners (§12 item 2). The hook refuses:

- a real pull request from a fork by an account not on the allowlist;
- a push by a member not on the allowlist;
- that repository's job on `light`, `part` or `compile`.

`gpu.yml` runs a GPU part from the public repository. Its CPU jobs stay on hosted
runners.

## 11. brainspike on top of this

brainspike's `docs/specs/arch/ci.md` stands, and is implemented with these pieces:

| ci.md piece | ci-runners piece |
|---|---|
| §5.1 claim job, with its 60 s wait and live local claims | `claim` with `wait-seconds: 60`, `local-lapse-minutes: 45` |
| §4.1 placement and probes; §4 plan job | brainspike's plan job selects and splits; `place` places the parts |
| §4 part, fallback; `scripts/ci_job.py` | `part` running `local_ci.sh -O <part>` on the uploaded plan; `ci_job.py` keeps only the plan download and the command line |
| §4.2 outcomes | §4.4, plus `reboot` and `disagreement` |
| §4.1 h100 rules | §5.4 |
| §3 hook, locks, slots, memory scopes | §6.3, §7 |
| §6 nightly on 3090a | the `nightly` pool and the reservation |
| §4 verdict, §5.2 merge gate, profile choice, summary | brainspike's own, on `light` |

**CI-1a plugs in** after RUN-1. It pins `@v1` and drops its own `scripts/runner/` (host
notes and the hook), which move here. Its acceptance 1 keeps the claim, profile and
placement-order tests it needs, and leaves the actor-allowlist test to RUN-1. CI-1a's
3090a half waits for RUN-3, and CI-1b is RUN-2.

**ci.md edits brainspike will need** (a lead-approved edit of its spec, A.9; not made
here):

1. §3: runners are registered at org level in Default and shared with other projects,
   not on the repository. The hook belongs to ci-runners.
2. §3 table: labels become `light`, `part`, `compile` and `nightly` with a host label
   (`ref` is `[part, 3090b]`, `ref-compile` is `[compile, 3090b]`, `r3090a` is
   `[part, 3090a]`). There are 4 `part` runners on 3090b. h100 gets 5 `h100-gpu<N>`
   runners instead of 2 `h100`.
3. §3 security: add `NoNewPrivileges` (sudo refused) and the shared-runner exposure,
   and point to this §6. Correct the token's scopes to those measured on 3090b on
   2026-11-05: `repo`, `workflow`, `admin:public_key`, `read:org`, `gist`, and no
   `delete_repo`.
4. §3 and §8: `ci_slot.sh` and `host_lock.sh` become the host-wide slots and lock that
   all projects share. brainspike's 2 slots become its cap within them. Its lock
   defaults are read from the host's ci-runners env when present.
5. §4 table: the claim, part, fallback and salvage jobs use ci-runners' actions, and
   `ci_job.py` shrinks to the command wrapper.
6. §4.1 h100 rules: one runner per card. Chunk rotation across cards becomes card choice
   per part. A hot card ends the part as `thermal`, and the retry goes to a cool card.
   The `gpu` commands must run in a tree without `.git`. The manifest and the keyed
   10 % verify are added, with the JUnit output that verify needs.
7. §4.2: add `reboot` (on h100 counted as thermal), `disagreement` (the verdict fails)
   and "project queue full" under `unavailable`. Thermal matching is by recorded PIDs and
   bus id, because the watchdog names the shared account.
8. §5.1: the claim counts only the bot's and allowlisted actors' statuses.
9. §6: the nightly's yield becomes a ci-runners reservation over the same files.
10. §7: add the hook bound, the ssh bounds, the queue timeout and the proxy service.
11. §2: add the Free-plan facts of this §2 (one Default group, public repositories off,
    private reusable workflows closed to public callers, rulesets on public
    repositories only).
12. Milestone: CI-1a's file list drops `scripts/runner/` and pins `chaobrain/ci-runners@v1`.
    CI-1b's acceptance points to RUN-2's.

## 12. Decided choices (lead, 2026-11-05)

1. **Visibility of ci-runners.** The repository becomes public before RUN-1's first
   runner registration, with rulesets on `main` and `v*` that only the lead may bypass.
   It never holds inventory (D1). The lead makes both changes (lead, 2026-11-05).
2. **How public consumers reach the runners.** Each public consumer gets
   repository-level `gpu` and `h100-gpu<N>` runners, registered on that repository.
   `allows_public_repositories` stays off on `Default`. This is an exception to "org
   level" for public repositories only (lead, 2026-11-05).
3. **Runner pools.** Pools are shared per host and class, with one waiting seat per
   project and `max_waiting` (lead, 2026-11-05).
4. **The CI proxy.** A separate CI instance runs on its own ports. Stopping the
   interactive proxy or switching its node never drops the runners (lead, 2026-11-05).
5. **First public consumer (RUN-4).** brainevent (lead, 2026-11-05).
6. **Verify timing.** The verdict waits for a drawn verify part (lead, 2026-11-05).
7. **Early thermal stop.** A part whose card stays at 85 °C or more for 3 min is stopped
   and retried on a cool card, before the watchdog throttles or kills it (lead,
   2026-11-05).
8. **Manifest cost.** Every file is hashed before each part if RUN-2 measures the check
   at 60 s or less. Otherwise the check covers every `.py`, `.pth` and metadata file
   plus a rotating 10 % of the rest (lead, 2026-11-05).
9. **Default group's repository access.** (Superseded for RUN-5 by decision 11.1:
   every private repository.) `Default` is restricted to the onboarded
   private repositories (`selected`) when RUN-1 starts, as defence in depth. If Free
   refuses that, the hook alone is the boundary. The lead makes this change (lead,
   2026-11-05).
10. **Org base permission.** It stays `write` for now: the hook and the rulesets cover
    the runners (lead, 2026-11-05).
    - *Follow-up for the lead:* tighten it later to `read`, with write granted by team.
      On Free that is the only way to protect each private repository's `main`.
