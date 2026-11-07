# ci-runners: design (contract)

Status: approved by the lead on 2026-11-05 (#1). Decision 11 (2026-11-06) made h100 a
standard test CI for every private chaobrain repository (RUN-5, live since `v1.2.0`).
Decision 11.12 (2026-11-06) retired the first path: the 3090b runners that served
brainspike, the ssh driver to h100, the CI proxy, placement and the public-repository GPU
path. The design as it stood before is the one at tag `v1.2.0`. Decision 12 (2026-11-07,
RUN-7) brings 3090b and 3090a back as local hosts under the RUN-5 model, with the pools,
the host-lock and slot interop and the classes that brainspike's test CI needs.

Guides, in Chinese: [consumers.md](consumers.md) for the repositories that use the CI,
[operations.md](operations.md) for the maintainer of the hosts. This file is the contract;
where a guide and this file disagree, this file holds.

This repository is public (D1). Hosts appear here under role names: **h100**, **3090b**,
**3090a**, the **egress host** and the maintainer's **workstation**. Real host names and ssh aliases,
accounts, paths under a home directory, addresses and allowlists are inventory and stay
on the hosts (§3.3).

## Lead decisions (2026-11-05)

The decisions of the first path. Those that still hold are kept as written; the others
are listed with what replaced them.

1. **Shared runners, registered at the organization level.** Several chaobrain
   repositories use the runner setup, and they share the hosts and runners.
2. *Public repositories on the GPU parts.* Retired by 11.12: public repositories never
   reach the self-hosted runners and keep all their work on GitHub-hosted runners. The
   guard it introduced stays on every job: a workflow sends a job to the self-hosted
   runners only when `github.event.pull_request.head.repo.full_name ==
   github.repository`, or on `push` or `workflow_dispatch`.
3. *h100, option A (runners on 3090b, parts over ssh).* Superseded by 11.3 and retired by
   11.12. Its safeguard 4 stands: **the exit path is a dedicated account from the host's
   admin, then normal runners.**
4. *3090 hosts.* Retired by 11.12.
5. *Proxy.* Retired by 11.12; h100's egress is decision 11.10.
6. **A green run on a head is a full verdict, and the runners never repeat it.** The
   `claim` step exits with success at once when the project's status context is green
   on that sha (§4.1).
7. **Shared slots.** Host-level slots are shared by all projects, so several projects
   cannot oversubscribe a host (§7). *The host lock and the interoperation with
   brainspike's `ci_slot.sh` and `host_lock.sh` are retired by 11.12.*
8. **Generic outcome classification, with fixtures.** The fixtures are the scope's
   OOM kills, the h100 GPU watchdog's kill lines, boot-id changes and a stopped runner.
   Thermal and OOM kills are retried per policy and are not reported as test failures.
9. The choices of §12.
10. **The RUN-1b plan review (lead, 2026-11-05):**
   1. "Bot dispatch: a per-repo switch, off by default. When it is off, a bot dispatch
      goes through the actor allowlist." The policy's `bot_dispatch` is that switch;
      the bot is on no allowlist, so with the switch off a bot-started job is refused
      (`bot`). §6.2.
   2. "Tamper gap (§5.3, T13): accepted for private consumers for now." It stays
      accepted, since only private repositories use the runners (11.1, 11.12).
   3. *The hook's path in job logs.* Moot: no public repository uses the runners.

## Lead decisions (2026-11-06): a standard test CI on h100 (RUN-5)

The request: a standard self-hosted test CI that every chaobrain member can use, for
every chaobrain repository's tests, on h100. These decisions supersede the items they
name. Items 1 to 11 are kept as they were decided; 11.12 amends them where they mention
the first path.

11. **Standard test CI on h100 (RUN-5).**
    1. **Private repositories only.** Every private chaobrain repository may use the h100
       runners. Public repositories keep their CPU work on GitHub-hosted runners, and
       their GPU path stays decision 2, §12 item 2 and RUN-4, unchanged.
       `allows_public_repositories` stays off. `Default` goes from `selected` to every
       private repository (supersedes §12 item 9; the lead makes the change).
       *Amended by 11.12: there is no public GPU path.*
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
       T14), and the lead accepts it for private repositories. *Amended by 11.12: RUN-2
       is retired.*
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
       gets a CI egress: an ssh connection to a restricted account on an egress host.
       Names resolve on the egress host: local DNS answers for blocked names cannot be
       trusted. *Amended by 11.10: the allowlist moves to the egress host, and every
       GitHub name goes through it.*
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
    10. **Every GitHub name goes through the egress, whose allowlist is on the egress
        host (amends 11.8).** Measured 2026-11-06, in the first canary run on h100:
        direct requests to `*.actions.githubusercontent.com` broke off without an
        answer. Three `launch` resolve requests failed in a row and so did the job's
        setup. Two artifact uploads ended `ECONNRESET`. Short probes passed all along
        (80 of 80). So the runners' own traffic and the jobs' both go through the
        egress. Only localhost and the package mirror that answers directly stay direct
        (inventory `tunnel.direct`).

        The Actions service uses names that change: `pipelinesghubeus<N>`,
        `run-actions-<N>-azure-<region>`, and the storage accounts under
        `.blob.core.windows.net`. So the allowlist takes suffix entries: `.example.com`
        matches every name below it, and not `example.com` itself. An `authorized_keys`
        `permitopen` takes literal names only. So the allowlist moves to the egress
        host, where no co-user of h100's account can change it:
        - **On the egress host,** `ci-egress.service` is a system unit that runs
          `ci-run egress` as a dynamic user of its own. It is an HTTP CONNECT proxy on that
          host's 127.0.0.1. It passes only names on its allowlist (`setup/egress/allow`,
          installed root-owned), on port 443, and connects to them directly. It resolves
          a name itself and connects only to global addresses. It answers 200 only once
          its connection is open.
        - **The key** opens one forward, to that port: `restrict,port-forwarding,
          permitopen="127.0.0.1:<egress port>",permitlisten="127.0.0.1:9",
          command="/usr/sbin/nologin"`.
        - **On h100,** `ci-tunnel.service` is `ssh -N -L 127.0.0.1:<http port>:
          127.0.0.1:<egress port>`, and the runners' `https_proxy` points at that local
          port. The inventory's `tunnel` table holds `http_port`, `egress_port` and
          `direct`.
    11. **A part's memory cap where the user manager has no memory controller.** h100
        runs cgroup v1 (measured 2026-11-06, systemd 249). A user scope there has no
        memory controller: with `MemoryMax=64M`, a 200 MB allocation succeeded. And
        systemd 249 refuses `OOMPolicy` on a scope, so every part ended `lost` at once.
        On such a host, the part's supervisor enforces the cap itself. Every 2 s it sums
        the anonymous resident memory (`RssAnon`) of the scope's processes, read from
        the scope's `cgroup.procs` in the systemd hierarchy, so a process that left the
        command's session still counts. Over the cap, it kills every process of the
        scope but itself and records an OOM kill, so the part ends `oom`. `OOMPolicy=
        continue` is passed only on systemd 253 or later, the first to take it on a
        scope; before that, an OOM kill never stopped a scope. The project slices'
        `MemoryMax` (`memory_max`) is not enforced on such a host. The CI slots bound
        the sum instead: at most `slots` parts, each under its cap.
    12. **The first path is retired (2026-11-06).** brainspike, the only consumer of the
        3090b runners, runs all of its workflows on GitHub-hosted runners, and no
        chaobrain repository but the canary of RUN-5 calls this repository (searched
        2026-11-06). So:
        - the 7 runners on 3090b are stopped, deregistered and uninstalled (§10, RUN-6);
        - RUN-1's brainspike pieces go: the host lock (`ci-lock`), the slot interop with
          `ci_slot.sh` (`ci-slot`, tickets without a project, the registry's `default`
          project), the `compile` and `nightly` classes, reservations and `yielded`;
        - the CI proxy goes (`ci-proxy.service`, `ci-proxy-check`, the inventory's
          `proxy` table);
        - `place` and its probes go, and the `place` and `salvage` actions with them. The
          verdict still salvages a part's record on its own host (§4.5);
        - RUN-2 (the ssh driver, the manifest, the keyed verify, `compare`,
          `disagreement`), RUN-3 (3090a) and RUN-4 (`gpu.yml`, public consumers) are not
          built. Public repositories stay on GitHub-hosted runners;
        - the hosted branch of `claim` goes: every job of `h100-test.yml` runs on h100;
        - policy schema 1 goes; the hook reads schema 2 only (§6.5).

        Nothing that a consumer calls changes: `h100-test.yml` and the `claim`, `part`
        and `verdict` actions keep their inputs, but for `part`'s `slot` and `lock`,
        which only the retired classes used, and its `verify` output. No consumer passes
        or reads them, so the release is `v1.3.0`, not `v2` (§9). The inventory keys only
        the first path read (`ssh`, `trusted`, `remote`, `cpu_slowdown`, `proxy` and
        `files.host_lock`) are accepted and ignored by `v1.3`, so that h100's inventory
        needs no change in the window of the release; a later release refuses them.

## Lead decisions (2026-11-07): the 3090 hosts return (RUN-7)

Approved by the lead on 2026-11-07 (#40), each open choice as recommended.

The request: brainspike moves all of its test CI onto these runners while it stays
private. It needs live NEST from conda, compile gates timed on pinned CPUs of its
reference host, gates that use two cards, and its nightly. h100 gives none of the last
three: one card per part (D7), and its cores are slower. So the lead decided to extend
ci-runners and to add runners on 3090b and 3090a again. These decisions amend 11.12
where it retired the 3090 hosts, the host lock, the slot interop and the `compile` and
`nightly` classes. The rest of 11.12 stands: no ssh driver, no manifest or verify draw,
no `place`, no CI proxy, no public-repository path, policy schema 2 only.

12. **3090b and 3090a return as local hosts under the RUN-5 model.**
    1. **The RUN-5 model on two more hosts.** Each 3090 host runs its own runners, hook,
       host layer and user units (`remote = false`, as h100 does, 11.3): the hook reads
       the org snapshot (11.2, §6.5); envs are prebuilt per repository with `ci-run env
       build` from the default branch's `.github/ci-runners.toml` (11.4); private
       repositories only (11.1); no manifest and no verify draw (11.5). 3090b is
       brainspike's reference host (its timings are the gates' reference); 3090a is used
       by other people on their own accounts, and runs brainspike's nightly.
    2. **A dedicated CI account per 3090 host**, with no sudo and no GitHub credential,
       created by the lead (one root step per host, §5.3). The runners, the host layer,
       the units and the envs are that account's. The maintainer's account, which RUN-1
       used, holds a GitHub token and ssh keys to the other hosts, and every accepted job
       could read them (§6.1); `NoNewPrivileges` stops only sudo. No runner registers on
       a host before its account exists.
    3. **Egress through the egress host (11.10).** Measured on 2026-11-07, without a
       proxy: on 3090b `api.github.com` and PyPI answer, `github.com` and
       `conda.anaconda.org` do not; on 3090a `github.com` answers too, conda does not.
       The CI proxy of the first path stays retired. Each 3090 host gets h100's tunnel:
       its own key in `etc/tunnel/`, one more `authorized_keys` line on the egress
       account with the same restriction, `ci-tunnel.service`, and the runners'
       `https_proxy` at its local port. Every GitHub name goes through it, as on h100.
       The allowlist does not change: it already holds the conda names.
    4. **The org snapshot is pushed to every host** from the maintainer's workstation
       (11.9): one `ssh <host> ... org-sync --receive` per host, each with the checks of
       §6.5. A host that cannot be reached ages on its own (`stale` after 60 min); the
       others are not held up. On a 3090 host the push logs in to the CI account with its
       own key, whose `authorized_keys` line is `restrict` and forces the receive command.
    5. **Pools and labels per host** (§5.1). Labels name the host and the class, as h100's
       do, so a job asks for one host's pool: `3090b-light`, `3090b-cpu`, `3090b-gpu` and
       `3090b-gpu<N>`, `3090b-multi`, `3090b-compile`; `3090a-light`, `3090a-cpu`,
       `3090a-gpu` and `3090a-gpu<N>`, `3090a-multi`, `3090a-nightly`. Four runner
       classes are added: `gpu` (one runner per card, what h100's `h100` class is),
       `multi`, `compile` and `nightly`.
    6. **Two-card parts on the 3090 hosts, behind a P2P self-test** (D7 stays for h100).
       A `multi` runner leases both cards of its host (`gpus: 2`). Before the command,
       inside the part's scope and with the part's `CUDA_VISIBLE_DEVICES`, the executor
       runs the P2P self-test: for each ordered pair of the leased cards it copies a
       64 MiB pattern from one card to the other (`cuMemcpyPeer`) and reads it back,
       byte for byte. It checks the data, not the driver's capability flags, because
       h100's fault (§5.4) reports P2P access and returns zeros. The test is stdlib
       Python over the CUDA driver API (`ctypes`), bounded at 60 s. A mismatch, an error
       or the bound ends the part `unavailable` (reason `p2p`) with `next` `none`: a
       broken link is a host fault, not a retry. Card leases are taken in ticket order
       per card, so a waiting `multi` part holds back the later single-card leases of its
       cards and is not starved.
    7. **The `compile` class returns, on 3090b only**: exclusive timing parts. A `compile`
       part takes one CI slot and holds the host lock exclusively for the whole command
       (`lock` defaults to `x`), at nice 0, pinned to the inventory's `compile_cpus`. Its
       command's own requests of the lock are covered by that hold (§7), so they do not
       wait for themselves.
    8. **The `nightly` class returns, on 3090a only.** One runner; a part may run 600 min
       (its job 615). A `nightly` part holds every CI slot and every card lease of its
       host for its life, so no other part runs beside it; parts queued behind it wait up
       to their queue timeout and then end `unavailable`. Its `gpu-mem-mib` is checked on
       the cards its command gets only: the lowest-numbered cool cards with that much
       free, `gpus` of them (lead, 2026-11-07); a local user's job on another card does
       not hold it back. This replaces the first path's
       reservation, grace period and `yielded`, which stay retired.
    9. **The host lock and the slots interoperate with brainspike's again** (D5 returns
       for the 3090 hosts). Both hosts also run work outside the runners: brainspike's
       performance runs, `-x` timing and local CI runs, under its `scripts/host_lock.sh`
       and `scripts/ci_slot.sh`. `ci-lock` implements `host_lock.sh`'s protocol exactly:
       `-s`, `-x`, `-l`, the turnstile, the shared marker, and the nesting variable that
       covers a request made under a hold. `ci-slot` takes the same slot files and ticket
       queue as `ci_slot.sh`. Both are tested against brainspike's scripts. The inventory
       names the files (`files.host_lock`, `files.slot_lock`, `files.slot_queue`; `v1.3`
       ignored `files.host_lock`). Because the CI account is not the account of local
       runs, the files are root-owned with ACL entries giving both accounts `rw`, and a
       `tmpfiles.d` entry recreates them at boot (`/tmp` is emptied then, and
       `fs.protected_regular` forbids one account to open another's file there with
       `O_CREAT`). A `ci_slot.sh` ticket carries no project and counts as the host's
       default project (brainspike, inventory `default_project`). A `nightly` part's
       ticket carries one more word, `every`, so that later tickets wait behind it for
       every slot; `ci_slot.sh` reads only its own `.ticket` files and never sees it
       (read at the stamp, 2026-11-07). The local accounts that share the files are
       inventory (`local_accounts`); the plan prints the root steps (§5.3) and `apply`
       refuses until they are done.
    10. **The env spec gains an optional `conda` table** (§4.9). It is additive, so it
        ships in `v1.4.0`, not `v2` (§9). brainspike's spec asks for `nest-simulator=3.10`
        from `conda-forge`.
    11. **Envs follow their upstreams' branches** (§4.9). A prebuilt env installs each
        upstream at its ref's head at build time, and a project that tests against its
        upstreams' `main` needs them current. `ci-run env refresh <project>` rebuilds the
        env from the spec of its last build, which the env keeps, when an upstream's ref
        has moved; it needs no token when every upstream is public, and refuses
        otherwise. A user timer runs it every 30 min for each project whose registry entry
        sets `refresh = true`. The new env replaces the old in one rename, as a build
        does, so a running part keeps the env it started with. Each part's record and
        summary name the env's build and its upstream commits.
    12. **Consumers may compose the actions** (§4.8). A consumer can call `claim`, `part`,
        `salvage` and `verdict` in its own workflow, not only through `h100-test.yml`.
        brainspike does: a plan job, then a matrix of parts over several hosts.
    13. **`salvage` returns, local only** (§4.5). The verdict reads part records on its
        own host only, and no host reaches another's. So a run whose parts ran on several
        hosts runs one `salvage` job on the light pool of each such host before the
        verdict. It uploads, for each part of that host whose artifacts stop short, the
        outputs its record holds, or `lost`.
    14. **`compile` and `nightly` are opt-in per repository.** Both hold a host
        exclusively, so the hook accepts them only for a repository whose entry in
        `policy.toml` lists the class (`classes`, written by the lead). The other classes
        stay open to every private repository of the snapshot.
    15. **Setup and registration are the lead's** (AGENTS.md). The lead creates the CI
        accounts, places the tunnel keys and the push key, approves each host's
        `setup/plan.sh` output before `apply`, registers the runners and tags `v1.4.0`.

## Decisions of this spec

- **D1. ci-runners is public.** Rulesets protect `main` and the `v*` tags, with only the
  lead able to bypass them. The repository never holds inventory.
  - *Reason:* on Free, rulesets exist only for public repositories, and every org member
    has write access to every repository. In a private ci-runners, any member could move
    `v1` and run code on every runner.
  - *Reason:* the repository's own tests then run free on hosted runners.
- **D2. Two layers.**
  - The *host layer* is installed on each host from a release by `setup/install.sh`, and
    no job ever fetches it. It holds the hook, the executor, slots, GPU leases and
    outcome classification.
  - The *workflow layer* holds the actions and the reusable workflow. It is thin and
    calls the host layer of its own release.
- **D3. Labels name a class and a host.** Pools are shared by projects. Host slots bound
  the load, and runner counts bound only the waiting (§7).
- **D4. The pre-job hook is the boundary on every runner.** Every runner unit sets
  `NoNewPrivileges=yes`, which made `sudo` fail inside a user unit (measured 2026-11-05,
  and on h100 on 2026-11-06).
- **D5. Interoperate with brainspike's lock and slot files on the 3090 hosts.** Retired by
  11.12, back with decision 12.9: on 3090b and 3090a, `ci-lock` and `ci-slot` share
  brainspike's files and protocols, because those hosts also run its local work. h100 keeps
  its own slot files under `state/` and has no host lock.
- **D6. A part exits 1 only when the run is red.** Every infrastructure end exits 0, with
  its outcome and a `next` hint for the retry job.
- **D7. One card per h100 part.** No multi-GPU part runs on h100 in v1 (see the P2P
  fault in §5.4). The 3090 hosts' `multi` pool runs two-card parts behind a P2P self-test
  (decision 12.6).
- **D8.** *The keyed verify draw.* Retired by 11.12.

## 1. Scope

In scope: host setup, runner services, the pre-job hook, the executor of a part, shared
slots and GPU leases, the host lock and the slot interop on the 3090 hosts, the P2P
self-test, outcome classification and retry hints, the claim step, salvage, the
verdict, the org snapshot, the hosts' egress, the env builds, and the reusable workflow
`h100-test.yml`.

Out of scope, and kept by each consumer: what to test, its test selection and markers,
its merge gate and its status semantics beyond `claim` and the verdict.

## 2. GitHub facts (checked 2026-11-05) and what they mean here

The org is on GitHub Free ("plan": "free", 12 seats). Its base permission is `write` on
every repository.

Settings read back from the API:

- ci-runners is public (2026-11-05).
- Ruleset "protect main": the default branch cannot be deleted or pushed non-fast-forward.
- Ruleset "protect release tags": `refs/tags/v*` cannot be created, updated or deleted.
- Both rulesets are active, and their only bypass is the organization admin role
  (`OrganizationAdmin`, `always`; the org has 2 admins).
- `Default` runner group (2026-11-06, decision 11.1): `visibility: all`,
  `allows_public_repositories: false`, `restricted_to_workflows: false`.

| fact | source | consequence |
|---|---|---|
| An org has one Default runner group. Only Team can create more. | docs: runner groups ("organizations using the GitHub Team plan"); API: one group, `Default` | Every org runner is open to every private org repository. The hook's repository allowlist (the org snapshot) is the per-repository boundary. |
| By default, only private repositories can use a runner group. | docs; API default `allows_public_repositories: false` | Public repositories reach no org runner while the flag is off, and it stays off (11.1, 11.12). |
| Rulesets and branch protection exist only for public repositories on Free. | API: a private repository returns 403 "Upgrade to GitHub Pro or make this repository public" | D1 |
| Self-hosted runners and standard hosted runners for public repositories are free. | docs: Actions billing | This repository's CI runs on hosted runners. A private caller of `h100-test.yml` spends no hosted minutes (§4.7). |
| The pre-job hook runs in "Set up runner" with the default variables and the event payload (`GITHUB_EVENT_PATH`). A nonzero exit fails the job. It has no timeout, and its output is visible to anyone who can read the repository. | docs: "Running scripts before or after a job" | The hook bounds itself, reads only local files and prints a reason code, never inventory. |
| A job queued 24 h fails. A runner offline 14 days is removed. A runner registered with `--disableupdate` that misses an update for 30 days gets no jobs. Runners update themselves by default. | docs | §8 bounds. Auto-update stays on. |
| Forks of a repository can run code on a self-hosted runner through a pull request. GitHub recommends self-hosted runners only for private repositories. | docs warnings | Private repositories only (11.1); the org forbids forks of private repositories; the guard and the hook refuse a fork's pull request anyway (§6). |
| A reusable workflow job's runner is not in the API's job record: `runnerName` is empty for the jobs of a called workflow. | measured 2026-11-06 | Which runner ran a part is read from its record on the host (`record.json`), not from the API. |

## 3. Layout

### 3.1 Repository

```
AGENTS.md  README.md  docs/design.md  docs/consumers.md  docs/operations.md
ci_runners/           stdlib-only Python: pure functions, thin I/O
  policy.py           hook decision: repository, actor, event, fork, runner class
  orgsync.py          the org snapshot: members and private repositories (§6.5)
  bridge.py           the egress proxy: HTTP CONNECT to allowlisted names (exact or
                      suffix) on 443, run on the egress host (11.10)
  claim.py github.py  claim decision from a sha's statuses; the status API
  part.py             the executor: gate, slot, lease, scope, supervisor, record
  sampler.py          the GPU sampler and the early thermal stop (§5.4)
  outcome.py          outcome classification and the retry hint
  slots.py gpus.py    fair host slots with caps, GPU leases in ticket order per card
  locks.py            `ci-lock`: brainspike's host-lock protocol on the 3090 hosts (12.9)
  p2p.py              the P2P self-test of a `multi` part, over the CUDA driver API (12.6)
  verdict.py          the verdict: the parts' outcomes, salvage, the status, the summary
  registry.py         the project registry
  envbuild.py         `ci-run env build` and `env refresh` (decisions 11.4, 12.10, 12.11)
  reaper.py           stale scopes, work directories, records and logs
  hook.py             the pre-job and post-job hooks
  setup.py            plan and apply
  inventory.py        schema, loader, lint
  cli.py              `ci-run`
bin/                  ci-run, ci-lock, ci-slot: CLIs over the package
hooks/                job-started.sh, job-completed.sh -> python -I -m ci_runners.hook
setup/                plan.sh, apply.sh, install.sh, register.sh, uninstall.sh
setup/units/          user unit templates: ci-runner@.service, ci-reaper.{service,timer},
                      ci-orgsync.{service,timer}, ci-envrefresh.{service,timer}, slices;
                      ci-tunnel.service (a host's forward to the egress, 11.10, 12.3)
setup/egress/         ci-egress.service and allow: the egress host's proxy unit and its
                      allowlist (11.10), installed by root there
setup/inventory.example.toml  setup/policy.org.example.toml   placeholders only
setup/workstation/    orgsync-push.sh and ci-orgsync-push.{service,timer}: the snapshot
                      push from the maintainer's workstation (decision 11.9)
actions/              claim/, part/, salvage/, verdict/ (composite, action.yml);
                      lib/ci-run.sh, the launcher; RELEASE, the release stamp
.github/workflows/    h100-test.yml (reusable, the standard test CI); test.yml (this
                      repository's tests, hosted only)
tests/                unit tests; fixtures/ (sanitised): event payloads,
                      watchdog lines, nvidia-smi CSV, boot ids, JUnit; mutants.py
tests/test_no_inventory.py     no tracked file holds a denied token (hashed deny list)
```

The deny list holds SHA-256 hashes of the inventory's tokens, so the list does not
publish what it protects. `setup/plan.sh` regenerates it from a host's inventory.

### 3.2 Hosts

Every host has the layout below, rooted at its `data` directory (inventory): on h100 our
subtree of the shared account (decision 11.3), on 3090b and 3090a a directory of the CI
account (decision 12.2).

```
<our subtree>/ci-runners/releases/<tag>/   installed host layer, read-only
<our subtree>/ci-runners/hooks/            the hooks (exec the release `releases/hook` names)
<our subtree>/ci-runners/etc/              inventory, policy, org.json, projects/; mode 700
<our subtree>/ci-runners/etc/tunnel/       the tunnel's key, ssh_config, known_hosts; mode 700
<our subtree>/ci-runners/state/            part records parts/<repo>/<run>-<attempt>/<id>-<n>/
                                           {record.json, status.json, samples.csv, log};
                                           leases; the slot queue; setup.json
<our subtree>/ci-runners/logs/             hook and reaper logs, kept 14 days
<our subtree>/ci-runners/envs/<project>/   the projects' conda envs (decision 11.4)
<our subtree>/actions-runner/<name>/       one runner each: the app, .env, credentials
<our subtree>/ci-work/<name>/              its work directory
~/.config/systemd/user/ci-*                the units: the one place outside our subtree
```

On 3090b and 3090a the host lock and slot files are brainspike's, outside `data`: the
inventory's `files` names them, and §5.3 makes them shared between the two accounts
(decision 12.9).

The hooks and the launcher run the inventory's `python` (3.11 or later; the system's
is 3.10), passed to them as `CI_RUNNERS_PYTHON` in each runner's `.env`. Elsewhere it
defaults to `/usr/bin/python3`.

The egress host, as root: `/opt/ci-runners/releases/<tag>/` (mode 555),
`/opt/ci-runners/etc/egress-allow` and `/etc/systemd/system/ci-egress.service`
(§5.3). The workstation, as the maintainer: a checkout of a release and the push units
(§5.3).

### 3.3 Inventory: where it lives, what is secret

| item | where | secret |
|---|---|---|
| runner credentials (`.runner`, `.credentials`, `.credentials_rsaparams`) | each runner directory | yes: they let anyone act as the runner |
| registration tokens | never stored; minted per registration (valid 1 h) | yes |
| host names, aliases, accounts, paths, ports, NUMA and GPU maps, thresholds | `etc/inventory.toml` | no, but never published |
| repository and actor allowlists, per-class event rules | `etc/policy.toml` | no, but never published |
| project registry (env path, caps, variables, status context) | `etc/projects/<p>.toml` | no |
| the env-build token: fine-grained, Contents read on the repositories whose envs are built | `GH_TOKEN` of the maintainer's shell, for one `ci-run env build`; never stored on h100, since co-users of the account could read every private repository with it | yes |
| the org-sync login (decision 11.9) | the maintainer's own GitHub login on the workstation, read by the push timer at each run; nothing on h100. A host that syncs itself (inventory `orgsync`) keeps a fine-grained read-only token in `etc/orgsync.token`, mode 600 | yes |
| the org snapshot: member logins, private repository names and ids | `etc/org.json` on h100, written by `ci-run org-sync --receive` (or `ci-orgsync`) | no, but never published |
| the tunnel key, its `ssh_config` and the egress host's pinned key (decision 11.10) | `etc/tunnel/` on h100, the key mode 600; the egress host, its address and account are inventory | yes: it opens the one forward to the egress proxy, which passes only its allowlist (T15) |
| the egress allowlist | `setup/egress/allow` in the repository (public names only), installed root-owned on the egress host | no |
| the workstation's push units (decision 11.9): the ssh alias of each host and its paths | the maintainer's `~/.config/systemd/user/` on the workstation | no, but never published |
| the push key's line on a 3090 host (decision 12.4) | the CI account's `authorized_keys`, `restrict` and forced to `org-sync --receive` | no, but never published |

Each host holds its own `policy.toml` and `projects/`. Each hook log line carries the
policy's hash, so a change shows.

**Inventory schema 1.** `this` names the host's role, and `[hosts.<role>]` holds:
`account`, `home`, `data` (the root of §3.2), `slots`, `memory_max`, `disk_min_gib`,
`gpus`, `max_temp_c`, `pools` and `files` (`slot_lock`, `slot_queue`), all required;
`cpus`, `python`, `orgsync`, `watchdog_log`, `watchdog_notice` and the `tunnel` table,
optional. A pool is a count or a table of `count`, `labels`, `class` and `per_card`
(§5.1). `v1.4` adds, all optional: `compile_cpus` (the CPUs of `compile` parts),
`default_project` (the project of a slot ticket without one), `files.host_lock` (the
host lock; read again, decision 12.9), `local_accounts` (the local accounts that share
the lock and slot files, 12.9) and `push_key` (the public key of the org snapshot's
push, an `ssh-ed25519` line, 12.4). A host with `files.host_lock` has the host lock,
and one without it has none. The first path's keys (`ssh`, `trusted`, `remote`,
`cpu_slowdown`, `proxy`) are accepted and ignored in `v1.3` and `v1.4` (11.12). `setup/inventory.example
.toml` holds the schema with placeholder values.

## 4. The consumer interface

A consumer calls the reusable workflow `chaobrain/ci-runners/.github/workflows/
h100-test.yml@v1` (§4.7), or calls the actions `chaobrain/ci-runners/actions/<name>@v1`
in its own workflow (§4.8, decision 12.12). Each action calls the host layer of its own
release (§9). Inputs that the project registry already holds may be left out.

### 4.1 `claim`: skip a head that already has a verdict

| input | default | meaning |
|---|---|---|
| `context` | required | the project's status context, e.g. `myproj/ci` |
| `sha` | the PR head, else `github.sha` | |
| `wait-seconds` | 0 | how long to wait for a claim by another actor to appear |
| `local-lapse-minutes` | 45 | a pending status by an allowlisted actor older than this has lapsed |
| `pending-description` | "queued on the runners" | posted when the runners take the head |

Output `run` is `true` or `false`, with `reason`. Pure rule, on the newest status of
`context` for the sha:

1. success, posted by the workflow bot or by an allowlisted actor: skip, "green";
2. failure posted by an allowlisted actor (a run outside these runners): skip, since
   the fix is a new push;
3. pending posted by an allowlisted actor within the lapse (a live claim elsewhere):
   skip;
4. anything else: run, and post pending.

The allowlist is the hook's, merged with the org snapshot (§6.5). A run whose pending
the bot posted is therefore claimed again by a later run of the same head; one that
ended green is skipped. To run a green head again, push a new head. Permissions:
`statuses: write`.

### 4.2 *`place`* (retired by 11.12)

h100 is the only host, and its labels choose the pool (§5.1); there is nothing to place.

### 4.3 `part`: run one command (the executor)

| input | default | meaning |
|---|---|---|
| `project` | required | registry key. It must match `github.repository`, or the job is refused. |
| `id` | required | the part's name, `[A-Za-z0-9][A-Za-z0-9_.-]*`: its record, scope and artifact are named after it |
| `command` | required | `bash -c` in the job's checkout, inside the project's env |
| `gpus` | 0 | number of cards to lease; 0 is a CPU part. What each class accepts is in §4.8: 1 on a per-card runner (`h100`, `gpu`), 2 on `multi` (decision 12.6), any of the host's cards on `nightly` (it leases them all) |
| `gpu-mem-mib` | registry | free MiB a card needs |
| `memory` | registry | the part's memory cap, at most the registry's maximum |
| `timeout-minutes` | required | inner bound, below the job's `timeout-minutes`; at most 240, or 600 on `nightly` (decision 12.8) |
| `lock` | `x` on `compile`, else `none` | on a host with the host lock (§7): hold it for the whole command, `s`, `x` or `l`. With `none` the command takes it itself, per step, as brainspike's `local_ci.sh` does. Refused (`unavailable`) on a host without the lock. |
| `queue-timeout-minutes` | 60 | longest wait for a slot or a lease, then `unavailable` |
| `results` | none | glob of JUnit XML that the command writes under `CI_RESULTS_DIR` |
| `resume` | false | sets `CI_RESUME=1`, for a command that can resume |
| `attempt-of` | none | the record of the attempt this one retries |

A CPU part (`gpus: 0`) takes one of the host's CI slots; a GPU part takes its card's
lease, which is its slot (§7).

The command's environment holds:

- `CI_RUNNERS_HOST` (the role name), `CI_PROJECT`, `CI_PART_DIR` and `CI_RESULTS_DIR`;
- `CUDA_VISIBLE_DEVICES` (empty for a CPU part) and `CI_CPUS` (the inventory's `cpus`,
  or `compile_cpus` on `compile`);
- the slot path, marked as held; on a host with the host lock, its path, and with `lock`
  set, the protocol's nesting variable at the mode held (decision 12.9);
- `CI_RESUME`;
- the runner's `https_proxy`, `http_proxy` and `no_proxy` (h100's egress, 11.10);
- the project's registry variables.

The command runs in `systemd-run --user --scope --slice=ci-<project>.slice
-p MemoryMax=<memory> -p MemorySwapMax=0`, plus `-p OOMPolicy=continue` on systemd 253 or
later, at nice 10 and ionice 7 (nice 0 on `compile`), with OOM score +500, pinned to
`CI_CPUS`. A `multi` part runs the P2P self-test in the scope first (decision 12.6).
Where the user manager has no memory controller (h100, decision 11.11), the supervisor
enforces `memory` itself. In the scope a supervisor runs the command, ends it at the
inner timeout (TERM, then KILL after 30 s) and writes the exit and the scope's OOM kills
into the record. The scope is not in the runner's cgroup, so a stopped runner leaves the
part running and its record finishing: the verdict salvages it (§4.5). The supervisor
relays the command's output to the job log; once the job's end of it is gone, it drops
the rest, so the command never meets a closed pipe. The supervisor holds the CI slot and
the GPU leases for the part's life; the command does not inherit them.

| output | meaning |
|---|---|
| `outcome`, `reason` | §4.4 |
| `next` | `none`, `retry` or `fallback` |
| `retry-on` | label array for the retry (on h100: a cool card's `h100-gpu<M>`) |
| `resume` | whether the retry should resume |
| `host`, `gpus`, `queue-seconds`, `run-seconds` | for the summary |
| `record` | `<host role>:<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>`, the part record below the host's `state/parts/` |
| `summary` | path to a Markdown block: host, cards, peak temperature per card, waits, events |

The step exits 1 only for `red` (D6). It also uploads these outputs as `outcome.json`, in
an artifact named `ci-part-<id>-<attempt>`, which the verdict reads.

### 4.4 Outcomes and retry hints

Precedence, highest first: stopped, reboot, thermal, oom, unavailable, red, pass. A
nonzero exit with an OOM kill is `oom`, not `red`.

| outcome | detected by | `next` |
|---|---|---|
| pass | exit 0 | none |
| red | nonzero exit or the inner timeout, with no infrastructure cause | none |
| oom | the scope's `memory.events` `oom_kill` > 0, or the supervisor's own cap (decision 11.11) | retry once with `resume` |
| thermal | a watchdog kill line (`GPU <bus> still <t>C -> killed <user>:<pid>, ...`) within the part's window that names our card's bus and a PID the sampler recorded for our scope; or our own early stop (§5.4) | retry once on a cool card |
| reboot | boot id differs from the one recorded at start | as thermal (the board powers off at 101 °C) |
| unavailable | before the command: the health gate, no card fits, the queue timeout, the project's queue full, low disk, a multi-GPU part on h100, `gpus` or `lock` the class does not accept (§4.8), an unknown inventory schema, a malformed registry entry | fallback: once, on a cool card or the same pool |
| unavailable (`p2p`) | the P2P self-test of a `multi` part failed, errored or ran over its bound (decision 12.6) | none: a host fault, which the verdict reports as error |
| lost | the job ended without outputs: the runner stopped (`systemctl show` timestamps after the part's start), or the tunnel dropped the job's connection | the verdict salvages the record (§4.5) |
| stopped | cancelled (concurrency group, TERM) | none |

The watchdog runs on the shared account, so the `<user>` it names is everyone's.
Matching therefore uses our recorded PIDs and the card's bus id. The sampler maps
index to bus with `nvidia-smi --query-gpu=index,pci.bus_id`.

A part is retried at most once (the registry may set 0). A second infrastructure end
is an error in the verdict.

### 4.5 Salvage, inside the verdict and per host

Where a part's artifacts stop short (none at all, or a retry asked for and no retry
artifact), its record on the host that ran it gives the same outputs as `part` would
have uploaded, or `lost` if the record never finished. The verdict reads the records of
its own host itself. No host reaches another's records, so a run whose parts ran on
several hosts adds a `salvage` job on each such host's light pool (decision 12.13):

| input | default | meaning |
|---|---|---|
| `parts` | required | the ids of the parts the run started, space-separated |
| `wait-seconds` | 0 | how long to wait for a part of this host that still runs |

For each part whose newest record on this host belongs to this run and attempt, and
whose artifacts stop short, it uploads `outcome.json` as `ci-part-<id>-<attempt>`, with
`salvaged: true`. A part it does not find on this host is left alone. "Newest" is read
across runs: when a later run's part of the same id ran on this host, the older run's
part has no record of its own here and ends `lost`. A composite action cannot loop, so
`salvage` uploads at most 16 parts per host and fails beyond that (read at the stamp,
2026-11-07). Salvage jobs run
before the verdict, which then reads every artifact as usual. `h100-test.yml` needs none:
its parts all run on h100.

### 4.6 *Reusable workflow `gpu.yml`* (retired by 11.12)

Public repositories do not use the self-hosted runners.

### 4.7 Reusable workflow `h100-test.yml`: the standard test CI

For any private chaobrain repository. Every job runs on h100 runners, so a private
caller spends none of its hosted Actions minutes.

| input | default | meaning |
|---|---|---|
| `project` | required | registry key (§4.3) |
| `cpu-command` | empty | the CPU part's command; empty skips the part |
| `gpu-command` | empty | the GPU part's command; empty skips the part |
| `gpus` | 1 | cards for the GPU part; only 1 is accepted on h100 (D7) |
| `timeout-minutes` | 60 | each part's inner bound, 1 to 240; the job's is this plus 15 |
| `results` | empty | glob of JUnit XML under `CI_RESULTS_DIR` |
| `context` | `<project>/ci` | the status context claim reads and the verdict posts |

Jobs. Each one has the guard: `pull_request` only with
`head.repo.full_name == github.repository`, and `push`, `workflow_dispatch`:

1. `claim` on `[self-hosted, h100-light]` (§4.1). Its first step checks the inputs
   (`timeout-minutes` 1 to 240, at least one command) and gives the later jobs the part
   ids, the context and the jobs' timeout, since an expression cannot add;
2. `cpu` on `[self-hosted, h100-cpu]`, `part` with `gpus: 0`;
3. `gpu` on `[self-hosted, h100]`, `part` with `gpus: 1` (the runner's own card, §5.4
   health gate);
4. `cpu-retry` and `gpu-retry`: each part whose `next` is `retry` or `fallback`, once,
   with `attempt-of` its first record and `resume` as the hint says. The CPU part
   retries on `[self-hosted, h100-cpu]`, the GPU part on the `h100-gpu<M>` that
   `retry-on` names (a cool card after a thermal end), else on `[self-hosted, h100]`. A
   part whose job ended without outputs is not retried here; the verdict salvages it;
5. `verdict` on `[self-hosted, h100-light]`, the `verdict` action, unless the run was
   cancelled (a newer head's run posts instead; the cancelled head keeps its pending).
   It reads the parts' `ci-part-<id>-<attempt>` artifacts, and salvages where they stop
   short (§4.5), else the part is `lost`. A part's last attempt decides: any `red` sets
   `context` to failure; all `pass`, success; else error. The parts' summaries are
   merged into the job summary, and the job fails unless the state is success.

The workflow names the actions it calls with a ref of their own: `@v1` on `main`, the
release's tag in a release (§9).

Permissions: `contents: read`, plus `statuses: write` for claim and verdict. A caller
grants the same at the call site.

### 4.8 Composing the actions (decision 12.12)

A private repository may call `claim`, `part`, `salvage` and `verdict` from its own
workflow, with its own jobs between them (a plan job, a matrix of parts over several
hosts, a coverage job). The rules that `h100-test.yml` keeps for its callers then are the
consumer's:

1. **The guard on every job** (decision 2): `pull_request` only with
   `head.repo.full_name == github.repository`, `push`, `workflow_dispatch`, `schedule`.
2. **Labels.** A job names `self-hosted` and one host's pool label, never a bare class
   name. These are the labels a consumer may target:

   | class | labels | `part` runs | `gpus` | `lock` | slot | `timeout-minutes` |
   |---|---|---|---|---|---|---|
   | `light` | `<host>-light` | short commands (a plan, a summary); `claim`, `salvage`, `verdict` | 0 | `none` | none | ≤ 30 |
   | `part` | `<host>-cpu` | CPU parts | 0 | any (3090 hosts) | one | ≤ 240 |
   | `h100`, `gpu` | `<host>`, `<host>-gpu<N>` on h100; `<host>-gpu`, `<host>-gpu<N>` on the 3090 hosts | one-card parts on the runner's card | 1 | any (3090 hosts) | the card's lease | ≤ 240 |
   | `multi` | `<host>-multi` (3090 hosts) | two-card parts, after the P2P self-test | 2 | any | both cards' leases | ≤ 240 |
   | `compile` | `<host>-compile` (3090b), opt-in (12.14) | exclusive timing parts on `compile_cpus` | 0 | `x` | one | ≤ 240 |
   | `nightly` | `<host>-nightly` (3090a), opt-in (12.14) | the consumer's nightly | 0 to all cards | any | every slot and card lease | ≤ 600 |

   `part` refuses a `gpus` or `lock` value outside its class's row (`unavailable`). A
   `nightly` part holds every lease, whatever `gpus` says; its `gpu-mem-mib` is checked
   only on the cards its command gets (decision 12.8).
3. **One `part` per job**, with the job's `timeout-minutes` at least the part's plus 15.
   A matrix leg's outputs do not reach later jobs one by one, so later jobs read the
   `ci-part-<id>-<attempt>` artifacts. Part ids are unique within a run.
4. **Retries.** A part whose `next` is `retry` or `fallback` runs again at most once, with
   `attempt-of` its first record and `resume` as the hint says, on the labels `retry-on`
   names or on a pool the consumer chooses (another host's, for a `fallback`).
5. **The verdict** runs last, on the light pool of one host that ran parts, after a
   `salvage` job on every other host that did (§4.5). It decides from the parts' last
   attempts, as in §4.7 item 5, and posts the context.
6. **Permissions** as in §4.7. A job that reads another run's artifacts adds
   `actions: read`.

The consumer's own jobs (`light` commands, a coverage job as a `part`) run in its env
like any part. Its test selection, its summary format and its merge gate stay its own
(§1).

### 4.9 The env spec: `conda` and refresh (decisions 12.10, 12.11)

`.github/ci-runners.toml` (schema 1, consumers §1) takes an optional `conda` table:

```toml
[conda]
channels = ["conda-forge"]                   # in order; the build passes --override-channels
packages = ["nest-simulator=3.10"]           # match specs: no options, URLs or paths
```

`ci-run env build` creates the env with `python` and the `conda` packages in one solve,
then runs the pip steps (`requirements`, `install`, the upstreams) as before. A spec
without `conda` builds as in `v1.3`. An unknown key in the table is a spec error, and
nothing is built.

The env keeps the spec it was built from and the commit of each upstream. `ci-run env
refresh <project>` compares each upstream's ref with that commit (`git ls-remote`, through
the host's egress) and, if one moved, builds a new env from the kept spec, renamed into
place as a build is. It refuses a spec with a private upstream, since it holds no token.
`ci-envrefresh.timer` runs it every 30 min for each project whose registry entry sets
`refresh = true`; a failed refresh leaves the old env and logs why. A project's default
branch that changes its spec still needs a maintainer's `env build`.

## 5. Hosts

### 5.1 Runner pools

Every runner also carries GitHub's `self-hosted`, `Linux` and `X64` labels. The runner
class (`CI_RUNNER_CLASS`) decides what the hook and the executor allow (§6.3).

| host | pool (class) | instances | labels | runs |
|---|---|---|---|---|
| h100 | `light` | 2 | `h100-light` | claim and verdict of `h100-test.yml` |
| h100 | `part` | 2 | `h100-cpu` | CPU parts, pinned to the inventory's `cpus` |
| h100 | `h100` | 5 | `h100`, `h100-gpu<N>` | GPU parts on card N |
| 3090b | `light` | 2 | `3090b-light` | claim, salvage, verdict; short consumer commands (a plan) |
| 3090b | `part` | 3 | `3090b-cpu` | CPU parts, pinned to the inventory's `cpus` |
| 3090b | `gpu` | 2 | `3090b-gpu`, `3090b-gpu<N>` | one-card parts on card N |
| 3090b | `multi` | 1 | `3090b-multi` | two-card parts, after the P2P self-test (12.6) |
| 3090b | `compile` | 1 | `3090b-compile` | exclusive timing parts on `compile_cpus` (12.7) |
| 3090a | `light` | 1 | `3090a-light` | as on 3090b |
| 3090a | `part` | 3 | `3090a-cpu` | CPU parts |
| 3090a | `gpu` | 2 | `3090a-gpu`, `3090a-gpu<N>` | one-card parts on card N |
| 3090a | `multi` | 1 | `3090a-multi` | two-card parts, after the P2P self-test |
| 3090a | `nightly` | 1 | `3090a-nightly` | a consumer's nightly, up to 600 min, holding the host (12.8) |

A pool's size is the host's slots (or cards) plus one waiting seat per project allowed on
it (§7); the 3090 hosts have 2 CI slots each, the count of brainspike's `ci_slot.sh`.
Runner names are `<host role>-<pool>-<i>`, except the per-card pools'. All are org-level
in Default (decision 1).

**Each host's labels are its own.** GitHub gives a job to any runner that has all its
labels. So h100's light and CPU runners carry `h100-light` and `h100-cpu`, not the bare
class names: a job of any repository that asks for `[self-hosted, light]` matches no
runner, and a job that asks for `3090b-cpu` never lands on 3090a. The 3090 hosts name
theirs the same way (decision 12.5). `setup` takes the labels from the inventory's pool entry; a pool may not give
itself GitHub's labels or a bare class name. The `h100` pool is `per_card`: one runner
per card, named `h100-gpu<N>`, labelled `h100` and `h100-gpu<N>`, with
`CI_RUNNER_CARD=<N>` in its `.env`. The 3090 hosts' `gpu` pool is `per_card` in the same
way (`3090b-gpu<N>`, labelled `3090b-gpu` and `3090b-gpu<N>`); its class is `gpu`, and a
pool entry names its class where it is not the pool's key (`class = "gpu"`). The `part`
class runs CPU parts only: the executor refuses `gpus > 0` there (`unavailable`), so
only the per-card, `multi` and `nightly` runners lease cards.

### 5.2 Setup scripts

All are idempotent and run as the runner account. None needs sudo.

- `setup/plan.sh <host>` reads the inventory and prints every change it would make:
  files with their diffs, units, `.env` lines and runner directories. It changes
  nothing. The lead approves this output. Save only its standard output as the plan.
- `setup/apply.sh <host> --plan <file>` applies exactly an approved plan. It refuses if
  the plan's text differs from a fresh `plan.sh` output, or while a root step the plan
  lists (§5.3) is not done. It writes a state file, and a
  second run is a no-op.
- `setup/install.sh --tag <tag> --expect <sha>` installs a release into
  `releases/<tag>/` after checking that the tag resolves to the commit the lead
  released (an annotated tag by its peeled commit, matched by exact ref name).
  `--commit <sha>` installs `releases/main-<sha12>/`. It never moves what running jobs
  use (§9): an existing release is kept only if it is a real directory holding that
  commit, and is refused otherwise. `releases/hook` moves only forward (to a higher tag,
  or from a main commit to anything); moving back needs `--rollback`.
- `setup/register.sh <runner>...` is run by the lead with a fresh registration token on
  stdin. It calls the runner app's `config.sh --url https://github.com/chaobrain
  --runnergroup Default --labels ... --work <data>/ci-work/<name> --unattended` under
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

**h100** (decision 11.3). Measured on 2026-11-06 from the shared account: linger on;
`systemctl --user` works; 2 TB of memory, 176 CPUs, 5 cards; `api.github.com` answers
directly, `github.com` and the package indexes do not (decision 11.8), and the Actions
service breaks off (11.10); not in the docker group; the system `python3` is 3.10;
cgroup v1 with systemd 249 (11.11).

| change | detail |
|---|---|
| directories | §3.2 under our subtree; `etc/` mode 700 |
| runner app | the pinned release, checked by sha256, into 9 runner directories (§5.1) |
| `.env` per runner | the two hook variables; `CI_RUNNER_CLASS`; `CI_RUNNERS_ETC`; `CI_RUNNERS_PYTHON` (for the launcher and the units; the hooks have the interpreter written into them by setup, so a job's environment never chooses it); `CI_RUNNER_POOL_LABELS`, the pool's own labels, for a retry's `retry-on`; on an `h100` runner `CI_RUNNER_CARD=<N>`, its card; `https_proxy` and `http_proxy` at the tunnel's local port, and `no_proxy` (inventory `tunnel.direct`: localhost and the package mirror that answers directly, decision 11.10) |
| `ci-runner@.service` (template) | `ExecStart=<data>/actions-runner/%i/run.sh`; `After=`/`Wants=ci-tunnel.service`; `NoNewPrivileges=yes`; `KillMode=mixed`; `Restart=always`, `RestartSec=30`; `Slice=ci.slice`; enabled per instance |
| `ci-tunnel.service` | `ssh -F etc/tunnel/ssh_config -N -L 127.0.0.1:<http port>:127.0.0.1:<egress port>` to the egress account (decision 11.10): batch mode, the egress host's key pinned in `etc/tunnel/known_hosts`, `ExitOnForwardFailure`, keep-alive every 30 s (a dead link ends after 90 s); `Restart=always` after 10 s |
| `etc/tunnel/` | the key pair, `ssh_config` and `known_hosts`, placed by the lead before the plan; `plan.sh` refuses without them, with the key or the directory readable by others, with a link in place of a file, or with an `ssh_config` that sets anything but `Host egress`, its `HostName`, `User` and `Port` (the unit fixes every other option, among them the key, the pinned host key with no global or DNS source, and no agent, gateway port or local command). A changed tunnel table changes the tunnel's unit, and the plan then restarts it. The runner app is fetched only once the tunnel's port answers (ssh binds it after authenticating). |
| `ci-orgsync.{service,timer}` | not on h100 (decision 11.9): the workstation pushes the snapshot |
| `ci-reaper.timer` | every 10 min: stops part scopes whose job is gone (a scope outlives a stopped runner), removes stale work directories, the part records of runs with nothing changed for 14 days, daily hook logs (`logs/hook-<date>.log`) over 14 days, stale tickets and leases |
| slices | `ci.slice` `MemoryMax` (inventory; proposed 512G of 2 TB) and `ci-<project>.slice`; not enforced under cgroup v1, where the slots and each part's own cap bound memory (decision 11.11) |
| slot files | under `state/` (inventory `files`) |
| unit names | every unit file starts with `ci-` (`ci.slice` included); `uninstall.sh` stops and removes only the units an `apply` recorded in `state/setup.json`; `plan.sh` refuses if a unit of that name exists and no earlier `apply` wrote it (another user of the account may have one). `plan.sh ROLE --adopt` instead lists each such unit as `adopt` and plans it as usual (a regular file of this account only), for units an older release wrote without a record; the lead approves the adoption with the plan, and `apply.sh` takes `--adopt` too. A unit an earlier apply recorded that the plan no longer holds is listed as `remove`, and `apply` stops, disables and deletes it. Never on a shared account without checking each listed file. The unit directory exists (mode 700) and is the account's: setup never changes its mode. |
| unchanged | the shared account's shell files, crontab, `authorized_keys`, other users' files, envs and units |

There is no root step on h100: the shared account has no sudo for us, and none is
needed.

**What the account's ownership does not close.** Job code runs as the account that owns
the host layer: `releases/`, `hooks/`, `etc/policy.toml`, each runner's `.env`, the runner
app and the user units. Read-only mode bits do not stop an owner. So one accepted job, or
a dependency it installs, or any co-user of the shared account, can turn the hook off
for every later job on the host (T13). The fixes are a root-owned host layer with system
units running as a CI account (`User=`), or ephemeral runners re-created from a
root-owned template after each job; both need the dedicated account of decision 11.7.

**3090b and 3090a** (decision 12). Measured on 2026-11-07: systemd 255 and cgroup v2 on
both, so the user manager's memory controller enforces each part's cap and the slices
(decision 11.11 does not apply); 28 CPUs and 2 × RTX 3090 each; 188 GiB on 3090b and
125 GiB on 3090a; the GitHub and package names of decision 12.3. The root steps come
first, made by the lead, then the plan. They wait until brainspike's `ci_slot.sh` writes
nothing through a link in the shared queue (T17), and `tests/fixtures/brainspike/ci_slot.sh`
is refreshed from that version:

| change | detail |
|---|---|
| the CI account (root) | a new account with no password and no sudo, linger on, its `data` directory on the data disk (decision 12.2) |
| shared lock and slot files (root) | brainspike's host lock (with its `.shared` and `.turnstile` files), its slot files and its ticket queue directory become root-owned, with ACL entries (and default ACL entries on the queue) giving the CI account and each of the inventory's `local_accounts` `rw`; a `tmpfiles.d` file (`/etc/tmpfiles.d/ci-runners-shared.conf`) recreates them at boot and excludes them from age cleaning. Their paths do not change (decision 12.9). Root-owned, because brainspike's scripts open them with `O_CREAT` (`exec N>>file`): `fs.protected_regular` lets any account do that on a file in sticky `/tmp` that `/tmp`'s owner, root, owns, and refuses it on a file another account owns. `plan.sh` prints the file's text and checks each step (the file as printed, each account present, each file root-owned, not a link, with its `rw` entries after the mask); the lead writes it as root and runs `systemd-tmpfiles --create` on it while no local job holds the files (under `host_lock.sh -x`, with `ci_slot.sh`'s queue empty). `apply` refuses until every step is done. Setup never runs as root, and nothing root runs reads the inventory. |
| the push key (the account) | inventory `push_key`; the plan writes one line in the CI account's `~/.ssh/authorized_keys` (mode 600, in `~/.ssh` of mode 700): `restrict`, forced to `<data>/ci-runners/releases/hook/bin/ci-run org-sync --receive` with `CI_RUNNERS_ETC` and `CI_RUNNERS_PYTHON` set, marked `ci-runners-orgsync-push` so that a new key replaces it; every other line is kept (decision 12.4) |
| `etc/tunnel/` | as on h100, with this host's own key pair; the plan prints the egress account's line for its public key (below) |
| directories | §3.2 under `data`; `etc/` mode 700 |
| runner app | the pinned release, checked by sha256, into 9 runner directories on 3090b and 8 on 3090a (§5.1) |
| `.env` per runner | as on h100; `CI_RUNNER_CARD=<N>` on a `gpu` runner; `https_proxy` at the tunnel |
| `ci-runner@.service`, `ci-tunnel.service`, `ci-reaper.timer` | as on h100 |
| `ci-envrefresh.{service,timer}` | every 30 min, `ci-run env refresh` for each project whose registry sets `refresh` (decision 12.11) |
| slices | `ci.slice` `MemoryMax` (inventory; proposed 120G of 188 GiB on 3090b, 80G of 125 GiB on 3090a) and `ci-<project>.slice` |
| unit names | as on h100; on a dedicated account `plan.sh` still refuses a `ci-` unit that no apply wrote |
| unchanged | the local accounts, their shells, crontabs, envs and checkouts; sudoers; brainspike's scripts, which keep their default lock and slot paths |

The CI account owns its host layer, so T13 holds there as on h100, but no co-user shares
that account: the local users reach it only through root, and job code reaches no
credential of theirs.

**The egress host** takes one root step, made by the lead:

| change | detail |
|---|---|
| the egress account | a system account with no password and `nologin` as its shell (done 2026-11-06) |
| `authorized_keys` | one line per host's tunnel key (h100, and from decision 12.3 3090b and 3090a), each restricted as decision 11.10 states: one `permitopen`, to the egress proxy's port. A host's plan prints its line: `restrict,port-forwarding,permitopen="127.0.0.1:<egress port>",permitlisten="127.0.0.1:9",command="/usr/sbin/nologin" <key>` |
| the release | `setup/install.sh` run by root with `CI_RUNNERS_ETC=/opt/ci-runners/etc`, so the release is root-owned under `/opt/ci-runners/releases/`, readable by others (mode 555); `/usr/bin/python3` is 3.11 or later; git is installed |
| `/opt/ci-runners/etc/egress-allow` | a root-owned copy of the release's `setup/egress/allow`, mode 644, in a directory of mode 755 |
| `ci-egress.service` | the release's `setup/egress/ci-egress.service` in `/etc/systemd/system/`, enabled: `ci-run egress` as a dynamic user on 127.0.0.1:<egress port>, at most 256 connections, journal lines per connection (refused, or open and closed, with the name); hardened (no new privileges, a read-only system, no home, its own `/tmp`, IP and Unix sockets only, no capabilities) and capped at 512 MB |
| unchanged | the egress host's sshd configuration |

**The maintainer's workstation** (decision 11.9), as the maintainer, not as a CI
account:

| change | detail |
|---|---|
| prerequisites | linger for the maintainer's account; an ssh key for h100 usable without an agent prompt; gh's login in its config file, not a keyring that needs a session |
| `ci-orgsync-push.{service,timer}` | user units of the maintainer's account, every 10 min: `ci-run org-sync --fetch` from a checkout of a release, with the login from `gh auth token`, piped to `ssh <host> <release>/bin/ci-run org-sync --receive` for each host in turn (h100, and from decision 12.4 3090b and 3090a, with the push key): the snapshot is fetched once, and each host, named by three words (ssh host, data root, interpreter), is bounded on its own (120 s); a host that fails is reported and does not hold up the others, and the run then ends with status 1; `TimeoutStartSec=10min` |
| unchanged | the maintainer's gh login and ssh access to h100 (both exist); nothing is stored |

### 5.4 The h100 executor

What the executor of §4.3 adds on h100 (a host with a per-card pool):

1. **Health gate.** Before a GPU part leases its card, the card must be below
   `max_temp_c` (80 °C) and read, and the watchdog's notice file (inventory
   `watchdog_notice`) must be absent. If not, the outcome is `unavailable` and
   `retry-on` names a card that passes (`h100-gpu<M>`). A part with `gpus > 1` is
   `unavailable` (D7). Each `h100-gpu<N>` runner leases card N alone
   (`CI_RUNNER_CARD`).
2. **Run.** In the scope of §4.3, with `CUDA_VISIBLE_DEVICES=N` and `taskset` to the
   inventory's `cpus` (NUMA node 0).
3. **Sample.** Every 30 s, record the card's temperature and use, and our compute PIDs
   (`--query-compute-apps` intersected with the scope's `cgroup.procs`), into the
   record's `samples.csv`. If our card stays at 85 °C or more for 3 min, stop the scope
   (`thermal`, early stop). The watchdog throttles at 88 °C for 10 min and kills 10 min
   later.
4. **Classify** (§4.4): the watchdog's log (inventory `watchdog_log`) after the part,
   matched by our PIDs and our card's bus; the boot id; the scope's OOM kills.
5. **Clean up.** `systemctl --user stop` of the part's scope kills its whole cgroup:
   there is no `pkill` on a shared account. The job-completed hook repeats this if the
   step was killed, and the reaper repeats it for a scope whose job is gone.

**GPU facts.**

- P2P copies among cards 0-3 from a lower to a higher index silently return zeros, so a
  part with `gpus > 1` on h100 is `unavailable` (D7).
- The cards sit on NUMA node 1. Parts are pinned to node 0 to keep load off the hot
  package 1.

### 5.5 The 3090 executor (decision 12)

On 3090b and 3090a the executor of §4.3 runs §5.4's steps with these differences:

1. **Health gate**: a card below `max_temp_c` that reads. The hosts have no watchdog
   (no `watchdog_log` or `watchdog_notice`), so `thermal` comes only from the early stop
   and `reboot` from the boot id.
2. **Leases**: a `gpu` runner leases its own card; a `multi` runner both cards, in ticket
   order (decision 12.6), then runs the P2P self-test; a `nightly` runner holds them all,
   after every CI slot, whatever its `gpus`; its command gets the `gpus` lowest-numbered
   cool cards with `gpu-mem-mib` free, and it waits while fewer have (decision 12.8).
3. **The host lock**: held for the whole command when `lock` says so (`x` on `compile`),
   through `ci-lock`, after the slot and the leases; released when the scope ends.
4. **Pinning**: `CI_CPUS` is the inventory's `cpus`, or `compile_cpus` on `compile`.

## 6. Security model

### 6.1 Per host

| host | runs as | what job code can reach | mitigation now | exit path |
|---|---|---|---|---|
| h100 | the shared account, runners on it (decision 11.3) | everything a co-user of the account can reach, and they can reach everything of ours: the runner credentials, the hook, the policy, each job's `GITHUB_TOKEN` while the job runs, the checkout of a private repository, the results and the statuses we post | private repositories only, of org members only (decision 11.1, 11.2); `GITHUB_TOKEN` with `contents: read`, plus `statuses: write` only in claim and verdict; per-job directories, deleted after the job; no other credential there but the tunnel key, which opens only allowlisted HTTPS (T15); `NoNewPrivileges` | a dedicated account (decision 11.7) |
| 3090b, 3090a | a dedicated CI account per host, runners on it (decision 12.2) | the CI account's files: the runner credentials, the hook, the policy, the envs, each job's `GITHUB_TOKEN` while it runs, the checkout of a private repository, the shared lock and slot files (a job can hold them and stall local runs); nothing of the local accounts, no sudo, no GitHub credential | private repositories only, of org members only; `NoNewPrivileges`; `GITHUB_TOKEN` as on h100; per-job directories; the tunnel key opens only allowlisted HTTPS; the push key only `--receive` | a root-owned host layer with system units (§5.3, T13) |
| egress host | a dynamic user of `ci-egress.service` | nothing of a job's: it sees only the names a job connects to | hardened system unit; root-owned release and allowlist | — |
| workstation | the maintainer | nothing: no job runs there | the push reads the login at each run and stores nothing | — |

### 6.2 Per repository class

| class | events accepted | actors | runner classes |
|---|---|---|---|
| private, allowlisted (every private repository of the org snapshot, §6.5, with its id; `compile` and `nightly` only where its `policy.toml` entry lists the class, decision 12.14) | `pull_request` (same repository), `push`, `workflow_dispatch`, `schedule`, `workflow_run`, `status`, `repository_dispatch` | `GITHUB_ACTOR`, `GITHUB_TRIGGERING_ACTOR`, and for a pull request its author and for a push its pusher, all on the actor allowlist (the org's members, §6.5). The workflow bot is accepted only for `workflow_dispatch` and `repository_dispatch`, and only where the repository's `bot_dispatch` switch is on (off by default; decision 10.1). | all |
| any other repository, public ones and ci-runners itself included | none | none | none |

`pull_request_target`, `issue_comment` and other events are refused everywhere unless
the policy lists them for a private repository.

### 6.3 The hook (`job-started`)

`python -I` reads the inventory, the policy, the org snapshot (`etc/org.json`, §6.5) and
`GITHUB_EVENT_PATH` as JSON, and does no network I/O. It bounds itself at 60 s (an
alarm; if that fails, its wrapper kills it at 65 s, which refuses). It applies §6.2 to
the runner's class (`CI_RUNNER_CLASS`) and checks that the event's repository id matches
`GITHUB_REPOSITORY_ID`. It also removes stale work directories of this runner.

It prints one line, `ci-runners: accepted|refused <reason code>`, followed by the
policy's hash, and logs details on the host only. The job-completed hook stops the
job's scopes, releases its leases and slots, and removes its work directory (120 s
bound).

### 6.4 Threats and mitigations

| # | threat | mitigation |
|---|---|---|
| T1 | A fork pull request asks for a self-hosted label by editing the workflow in the fork. | `allows_public_repositories` stays off, so a public repository's job matches no runner; the org forbids forks of private repositories; the guard on every job of `h100-test.yml`; the hook (fork, actor, repository, class); no docker for the runner account; `NoNewPrivileges` |
| T2 | Someone outside the org's members pushes a workflow that asks for our labels (an outside collaborator, a bot). | the hook's actor checks against the snapshot's members |
| T3 | A new or unlisted repository asks for our labels (Default is open to all private repositories). | the hook's repository allowlist (the snapshot); the repository id is checked against the name |
| T4 | Someone moves `v1` in ci-runners to bad code. | D1 rulesets (only the lead can update or delete `v*` and `main`); the hook and executor come from installed releases (D2) |
| T5 | Job code uses sudo, or reaches a GitHub credential beyond its own token. | `NoNewPrivileges`; no stored GitHub credential on h100 (the snapshot is pushed, envs are built with a token held only for the build); `admin:org` never on a runner host |
| T6 | (an accepted gap, §6.1, decision 11.3) Users of the shared h100 account steal credentials, tamper with envs or results, read our source, or kill our processes. | private repositories only; per-job directories, deleted afterwards; kills end as thermal or lost and are retried or salvaged; the exit path (11.7) |
| T7 | Our job code harms other users of the shared account. | only members' code; per-job directories; kill by scope; the lead tells the host's admin what runs there |
| T8 | *Secrets leak to h100 through an ssh environment.* Retired with the driver (11.12). | — |
| T9 | The hook's output leaks inventory in logs. | a reason code only; the inventory lint |
| T10 | A hostile event payload targets the hook. | JSON parsing in `python -I`; no shell interpolation; fixtures with hostile strings |
| T11 | A green status is forged: anyone with write can post statuses on Free. | `claim` counts only the bot's and allowlisted actors' statuses; consumers audit merges |
| T12 | A burst of refused jobs blocks our runners. | refusal in under 10 s; the lead can pause a repository in the policy |
| T13 | (also any co-user of h100's account, an accepted gap; on the 3090 hosts the CI account's own jobs only) An accepted job (or its dependency) edits the host layer it owns: the hook, the policy, `.env` or the units, so later jobs pass unchecked. | private consumers only (§5.3); nothing on the host detects an edited hook, so a changed policy hash in the hook lines shows only a changed policy; the hook writes its log without following links or blocking on FIFOs |
| T14 | A co-user of h100's shared account reads the org-sync token, or edits the snapshot. | no token on h100 (decision 11.9): the snapshot arrives over the maintainer's ssh access, checked by `--receive` (§6.5). A host that syncs itself holds a fine-grained read-only token (members, metadata): it shows the member list and the private repositories' names, nothing else. An edited snapshot is T13's case. |
| T15 | A co-user of h100's shared account takes the tunnel key, or any local user of a host uses the tunnel's port on 127.0.0.1 (on the 3090 hosts, every local account can reach it). | the egress account has no shell and no listening forward, and its key opens only the egress proxy's port (decision 11.10). The proxy passes only its allowlist on port 443: HTTPS to GitHub, the Actions service's storage and the package indexes, which job code reaches through the tunnel anyway. The suffix `.blob.core.windows.net` lets them reach any Azure storage account over HTTPS, and `storage.googleapis.com` any Google Cloud Storage bucket: accepted gaps, since the Actions storage accounts have no stable names and package downloads use those buckets. They appear as the egress host's address. The allowlist is root-owned on the egress host. The lead revokes the key by deleting one line there. |
| T16 | (3090 hosts) A job, or a local user, holds the shared host lock or a slot, and stalls the other side's work; or a local account empties the ticket queue. | the files are `flock` locks, so a hold ends with its process: a part's hold ends with its scope (inner timeout, the job-completed hook, the reaper); a waiting part ends `unavailable` at its queue timeout; the files hold no data, and a lost ticket only loses a place in the queue |
| T17 | (3090 hosts) A job plants a link in the shared ticket queue (a `seq`, a ticket, a holder record, a temp name or a lock file there) so that a script of the other account writes, creates or truncates a file the link names, or blocks on a FIFO it reads. | the queue directory is not sticky and both accounts write in it (decision 12.9; lead, 2026-11-07), so no script of either account writes or creates through a link there. `slots.py` creates each file exclusively under a random name (`O_EXCL`, `O_NOFOLLOW`) and renames it over its name (a rename replaces a link, never its target); it opens `seq.lock` and `take.lock` `O_NOFOLLOW`, and reads a file `O_NOFOLLOW` and `O_NONBLOCK`, only if it is a regular one. brainspike's `ci_slot.sh` follows the same rule (exclusive temp files and atomic renames; a linked `seq.lock` or queue directory refused). The plan checks that the queue is a root-owned directory, not a link. Residual: a job can delete or forge tickets and holder records, which disturbs the queueing only (T16), never a file of a local account. |

### 6.5 The org snapshot

The snapshot is `etc/org.json`: `{schema: 1, fetched_at, members: [login],
repositories: {owner/name: id}}`, lower case. It is made from `GET /orgs/chaobrain/members`
and `GET /orgs/chaobrain/repos?type=private`, following every page, 30 s per call, 3
tries. A failed or partial fetch leaves the old snapshot in place. It is written to a
temporary file and renamed, mode 600.

**The push (decision 11.9).** `ci-run org-sync --fetch --org <name>` takes the login from
`GH_TOKEN`, makes the document and prints it; it writes nothing. `ci-run org-sync
--receive` reads at most 4 MiB from stdin and writes `etc/org.json`, but only if the
document loads as the hook loads it, every repository in it belongs to the policy's
`[org]` name, its `fetched_at` is no later than 5 min from now and no earlier than the
snapshot in place (a replayed old snapshot cannot bring back a removed member). Anything
else leaves the old snapshot, exits 1 and prints why. Receives take a lock, one at a
time. The push timer is monotonic: a workstation suspended for an hour stales h100 too.
A host that holds its own token runs `ci-orgsync.timer` instead (inventory `orgsync`,
`etc/orgsync.token`).

`policy.toml` is schema 2 (`setup/policy.org.example.toml`); the hook refuses any other
(`policy`). Its `[org]` table holds `name`, `members = true`, `private_repositories =
true` (each snapshot repository joins `repositories` as private) and `max_age_minutes =
60`. With `members`, the snapshot's members may act **only in the org's private
repositories of the snapshot**: a repository written by hand is open to them only if it
is private and the snapshot holds it with the same id. A repository written in
`policy.toml` keeps its own entry, so `paused`, `extra_events` and `bot_dispatch` stay
the lead's. A public repository is never added from the snapshot: the call asks only for
private ones, and a snapshot entry already listed as public is ignored.

The hook refuses every job (`stale`) when the snapshot is older than `max_age_minutes`,
and refuses with `policy` when the snapshot is missing, too large or malformed. It
prints the hash of the policy and of the snapshot. `claim` takes its allowlist from the
same merge.

Org members are humans; a bot or an outside collaborator is not one, so both stay
refused. A member who leaves the org is refused within 10 min, or 60 min if the push
fails meanwhile.

## 7. Scheduling and fairness

Shared resources, per host:

- **CI slots**: CPU and `compile` parts take one of the host's slots (inventory `slots`,
  2 on each host), in the files the inventory's `files` names: under `state/` on h100,
  brainspike's slot files on the 3090 hosts (decision 12.9), which its local runs take
  through `ci_slot.sh` too. Each card is its own slot: a GPU part takes its card's lease
  instead. A `nightly` part takes every slot.
- **GPU leases**: per card, at most `max_parts` parts (default 1), and the free-memory
  check (`gpu-mem-mib`) at lease time, which also sees local work on the card. Leases are
  given in ticket order per card, so a `multi` or `nightly` part that waits for several
  cards holds back the later leases of those cards (decision 12.6).
- **Host lock** (3090 hosts): brainspike's `host_lock.sh` protocol, in `ci-lock`
  (decision 12.9). An `-x` request waits for the shared holders already in, so every
  project keeps a shared hold to about 15 min and takes the lock per step for longer
  commands (as brainspike does), and a `compile` part's whole-command `x` waits at most
  that long. A request made under a hold is covered by it (the nesting variable).
- **Memory**: each part's cap (§4.3); `ci.slice` and `ci-<project>.slice` where the user
  manager has a memory controller (decision 11.11).

Rules:

1. **Queue order.** First come, first served, but a ticket whose project already holds
   its cap is skipped. When several projects wait, the next slot goes to the project
   that holds the fewest slots, and ties go to the older ticket.
2. **Per-project caps** come from the registry: `max_slots` per host (default 1),
   `memory_max`, and `max_waiting` per pool (default 1). A part beyond `max_waiting`
   ends `unavailable` ("project queue full"). So one project cannot fill a pool with
   waiters, and GitHub's queue, which is not fair across repositories, stays short.
3. **Light jobs never wait behind heavy ones**: claim and verdict run on their own
   `light` pool and take no slot.
4. **Interop with `ci_slot.sh`** (3090 hosts; decision 12.9). `ci_slot.sh`'s tickets
   carry no project, so they count as the host's `default_project` (brainspike). Its
   waiters apply no caps, so that project's cap is the slot count on those hosts.
5. **Accounting** is the part records under `state/parts/`: queue time, run time and
   outcome per part. No report command is built yet.

## 8. Failure modes and bounds

| failure | effect | handling |
|---|---|---|
| runner offline mid-part | GitHub fails the job without outputs | `lost`: the verdict salvages the record |
| host reboot | running parts end; with linger the units start again, the tunnel first | `reboot` (as thermal); the reaper cleans up |
| thermal kill or early stop | the part's processes die, or we stop them | `thermal`: one retry on a cool card |
| OOM | the scope's processes are killed | `oom`: one retry with `resume` |
| h100's tunnel down | the runners go offline for its length (their own traffic goes through it, decision 11.10); a running part goes on, but its job cannot report and may end `lost`, then is salvaged | ssh's keep-alive ends a dead link in 90 s, and systemd restarts it after 10 s; the runner app retries its connection on its own. The egress proxy answers 502 when its own connection fails |
| the workstation down, or its push failing | the snapshot ages | every job of a host whose push fails refused `stale` after 60 min, until the push is back; the other hosts are not held up |
| a 3090 host's tunnel down | as h100's | as h100's |
| a local run holds the host lock or a slot | parts wait | their queue timeout, then `unavailable` |
| an env refresh fails | the env stays at its old upstream commits | logged; the parts' summaries name the commits, and the next refresh tries again |
| the P2P self-test fails | the `multi` part does not run | `unavailable` (`p2p`), no retry; the verdict reports error |
| slot or lease starvation | a part waits | `queue-timeout-minutes`, then `unavailable` and one retry |
| disk low (under `disk_min_gib` free in the data directory) | | `unavailable` before the command |
| hook hangs | the job blocks in "Set up runner" | the hook's own 60 s bound, then the fixed refusal line; the wrapper's `timeout` at 65 s |
| a host layer that cannot read the host's files (§9) | every job of the host fails at once | the release notes say so; the host's change and the move of `v1` go in one window |
| GitHub down | nothing is assigned | none |

| bound | value |
|---|---|
| job-started hook, job-completed hook | 60 s, 120 s |
| `claim` API calls | 30 s each, 3 tries; the wait as input |
| slot or lease wait | 60 min (input) |
| sampler; early stop | every 30 s; 85 °C for 3 min |
| the supervisor's memory cap | checked every 2 s |
| a part | the consumer's `timeout-minutes`, at most 240 (`nightly`: 600); its job's is 15 min more |
| P2P self-test | 60 s |
| env refresh | every 30 min; one refresh at a time per project |
| tunnel | keep-alive 30 s × 3; restart after 10 s |
| org snapshot | pushed every 10 min; stale after 60 min |
| reaper | every 10 min |
| runner restart | 30 s after a failure |
| GitHub | queued 24 h fails a job; a job lasts at most 5 days; offline 14 days removes a runner |

## 9. Versioning and rollout

- **Tags.** Releases are immutable `vMAJOR.MINOR.PATCH` tags, plus a moving major tag
  `v1`. Consumers pin `@v1`; a canary pins the exact tag. Rulesets reserve tag changes
  for the lead (D1).
- **Release stamp.** At release, `actions/RELEASE` is stamped with the release (on
  `main` it reads `hook`, the release the hook runs). Every action calls
  `actions/lib/ci-run.sh`, which runs `<data>/ci-runners/releases/<that release>/bin/
  ci-run`, so `@v1.4.0` always runs 1.4.0's host layer; one file is stamped, not every
  `action.yml`. A host without the release ends a part `unavailable`. A host keeps every
  release that a major tag points at or pointed at in the last 14 days.
- **Hook and policy.** These are host-level and not pinned by consumers. A new release
  of the hook must accept every major still installed.
- **Rollout of `v1.N`:**
  1. A PR with tests (hosted unit tests, fixtures, a live check on one host).
  2. The stamp PR: `actions/RELEASE` and the reusable workflow's action refs set to
     `v1.N.0`.
  3. The lead tags `v1.N.0` on the stamp's merge commit.
  4. `install.sh` on every host, the egress host included; nothing switches yet.
  5. A canary consumer branch pins `h100-test.yml@v1.N.0` and passes.
  6. The lead moves `v1`. The hosts already have the release, so the order is safe.
  7. The restore PR sets the stamp back to `hook` and the refs back to `@v1`.
  8. Rollback: move `v1` back. The old release is still installed.
- **A reusable workflow's canary (measured with `v1.2.0`, 2026-11-06).** A workflow names
  the actions it calls with a ref of their own. A canary that pins `h100-test.yml@v1.N.0`
  still runs the actions at `@v1`, so the old release's host layer, and it tests only the
  workflow file. So the release stamp also rewrites the reusable workflow's own action refs
  (`chaobrain/ci-runners/actions/<a>@v1`) to `@v1.N.0`, and the PR after the tag sets them
  back to `@v1` with `actions/RELEASE`. A test checks that the refs and the stamp agree.
- **A host change the old release cannot run.** `v1.2.0` changed h100's inventory (the
  tunnel table) in a way `v1.1.0`'s host layer refuses. Between h100's apply and the move
  of `v1`, every h100 job ran `v1.1.0` and failed at once. Such a release says so in its
  notes. Then the lead applies the host's plan and moves `v1` in one window, once every
  host holds the release, or the host layer first learns to read both forms in a release
  of its own (as `v1.3` does for the retired inventory keys, 11.12).
- **Breaking changes** get `v2`: a removed or renamed input that a consumer passes, a
  changed output, a changed label. `v1` stays tagged and installed until its last
  consumer has migrated. Before a removal counts as non-breaking, the org's repositories
  are searched for callers.
- **`v1.4.0` is additive** (decision 12): new labels and classes, the `lock` input, the
  `salvage` action, the env spec's `conda` table and the inventory's optional keys. No
  input a consumer passes is removed or renamed, and h100's inventory and `h100-test.yml`
  callers need no change, so it is a minor release.
- **`v1.4.0` notes** (to be stamped at the tag). Read when the contract was stamped
  (2026-11-07), each recorded where it applies:
  1. a `nightly` part holds every card lease; its `gpu-mem-mib` is checked only on the
     cards its command gets (12.8, §4.8, §5.5);
  2. a `nightly` slot ticket carries the `every` marker, which `ci_slot.sh` never reads
     (12.9);
  3. `salvage` reads the newest record across runs, and uploads at most 16 parts per host
     (§4.5);
  4. on h100, `gpus: 0` is now refused (the `h100` class row of §4.8), and `h100-test.yml`
     checks `gpus == 1`. No caller passing `gpus: 0` was found.
- **Inventory schema.** `schema = N` in `inventory.toml`. The executor refuses an
  unknown schema (`unavailable`) rather than guess.

## 10. Phases

Each phase is one or more PRs of at most about 1,500 lines, each carrying its evidence.

**RUN-0, this spec** (2026-11-05). **RUN-1, the 3090b runners and the generic executor**
(`v1.0.0`, 2026-11-05), served brainspike until 11.12. **RUN-2** (the ssh driver to
h100), **RUN-3** (3090a) and **RUN-4** (the public GPU path) are retired by 11.12, never
built. Their acceptance lists are in the design at `v1.2.0`. RUN-7 (decision 12) brings 3090b and 3090a back under
the RUN-5 model, not RUN-1's or RUN-3's.

**RUN-5: the standard test CI on h100** (decision 11; `v1.1.0` and `v1.2.0`, live since
2026-11-06). Sub-PRs: the org snapshot and policy schema 2; h100 as a local host in setup
(no proxy, the inventory's `python` and labels); the local health gate, sampler and early
stop; `h100-test.yml` and `ci-run env build`; h100's egress (decision 11.8); the snapshot
push (decision 11.9); then `v1.1.0`. Then the egress proxy on the egress host (11.10) and
the supervisor's memory cap (11.11), then `v1.2.0`. *Acceptance:*

1. Unit tests, each failing on a mutant: a snapshot member's push is accepted; an outside
   collaborator, a non-member and a bot are refused; a public repository, and a private
   one whose id differs from the snapshot's, are refused; a stale, a missing and a
   malformed snapshot are refused; a hot card is refused with a cool card's label; the
   early stop; a watchdog kill line counts only with our PID and our card's bus. The
   egress proxy refuses a name off its list (a suffix entry matching names below it
   only), a port other than 443, a request that is not CONNECT and a name that resolves
   to no global address, and answers 502 when its connection fails. On a host without a
   memory controller, a part over its `memory` ends `oom`; `OOMPolicy` is passed on
   systemd 253 and later only. `--receive` refuses another org's snapshot, an older one
   than in place and one from the future.
2. The lead-approved plan is applied on h100, and its 9 runners are online at org level.
   They return within 5 min after `systemctl --user restart`.
3. In a live job, `sudo -n true` fails. A job from a public repository, one from a
   non-member and one from a fork pull request each fail in "Set up runner" in under
   10 s, and print no inventory. A job of another private repository given
   `[self-hosted, light]` is not taken by an h100 runner.
4. A member added to the org can run within 10 min. A snapshot made older than 60 min
   refuses with `stale`. From h100, `github.com` and the Actions service answer through
   the tunnel; a name off the egress proxy's list gets 403, and a forward to any other
   port of the egress host is refused there.
5. A canary private repository: a failing CPU test and a failing GPU test each end red in
   their own part, and only there. A part OOM-killed ends `oom` and is retried; a stopped
   runner ends `lost` and is salvaged; a forced hot reading stops the part as `thermal`,
   retried on a cool card.
6. Five heads of the canary run end to end, with their statuses and summaries right.

*Measured on 2026-11-06* (the canary chaobrain/ci-canary): items 1 and 2 pass; in item 3,
`sudo` fails, a bot's pull request is refused (`actor`), a `[self-hosted, light]` job is
never taken by h100, a public repository's job is never dispatched (the group excludes
public repositories), and a fork pull request cannot exist (the org forbids forks of
private repositories); item 4's tunnel checks pass; in item 5, green end to end, the CPU
and the GPU red each in their own part, `oom` with its retry, and a stopped runner
salvaged; a stale snapshot is refused. Not measured live: the thermal stop (no hot card
could be forced), and a human non-member and a newly added member (no second account).

**RUN-6: retire the first path** (decision 11.12; `v1.3.0`). *Acceptance:*

1. The 3090b runners are stopped and deregistered; 3090b's CI account holds no `ci-`
   unit; the org lists only h100's 9 runners.
2. No module, unit, action, test or fixture of the first path remains; `tests/mutants.py`
   kills every mutant left. The hook refuses a schema-1 policy; the inventory loader
   accepts the retired keys and ignores them.
3. `setup/plan.sh h100` with `v1.3.0` plans no change against `v1.2.0`'s apply.
4. The canary passes green and red on `@v1.3.0`, then on `@v1` after the move.

**RUN-7: the 3090 hosts** (decision 12; `v1.4.0`). Sub-PRs, each with its tests and
evidence:

- RUN-7a: this contract (decision 12), approved by the lead;
- RUN-7b: the env spec's `conda` table and `env refresh` with its timer (12.10, 12.11);
- RUN-7c: the classes `gpu`, `multi`, `compile` and `nightly` in the policy, the
  executor and setup; the per-class limits of §4.8; leases in ticket order; the P2P
  self-test (12.5 to 12.8, 12.14);
- RUN-7d: `ci-lock` and `ci-slot` against brainspike's files, restored from `v1.2.0`
  where they still fit (12.9);
- RUN-7e: the `salvage` action and the `lock` input (12.13);
- RUN-7f: setup for the 3090 hosts (the tunnel, the push to several hosts, the inventory
  keys), the plans for the lead, then the stamp and `v1.4.0`.

*Acceptance:*

1. Unit tests, each failing on a mutant: a `conda` table builds in one solve before pip,
   and a malformed one builds nothing; a refresh rebuilds only on a moved upstream,
   refuses a private one, and keeps the old env on failure; each class refuses a `gpus`,
   `lock` or timeout outside its row of §4.8; `compile` and `nightly` are refused for a
   repository whose policy entry does not list them; a waiting two-card lease holds back
   a later one-card lease of its cards; the P2P self-test fails on a fixture that returns
   zeros, and its failure is `unavailable` (`p2p`) with `next` `none`; a nightly part
   takes every slot and card; salvage uploads only its own host's parts of the run.
2. Interop: `ci-lock` and brainspike's `host_lock.sh` exclude each other in every mode
   pair, across the two accounts, and a request under a hold is covered; `ci-slot` and
   `ci_slot.sh` together never exceed the slot count.
3. The lead-approved plans are applied on 3090b and 3090a, and their runners are online
   at org level. They return within 5 min after `systemctl --user restart` and after a
   tunnel restart; after a reboot if one happens, else stated as not tested. The lock
   files survive a reboot with their ACLs.
4. In a live job on each host, `sudo -n true` fails and nothing of the local accounts is
   readable. A non-member's job, and a `compile` job of a repository not listed for it,
   fail in "Set up runner" in under 10 s and print no inventory. A job asking for
   `3090b-cpu` never lands on 3090a.
5. The P2P self-test passes on both 3090 hosts' card pairs, and a two-card part runs end
   to end on each.
6. A canary private repository composes the actions (§4.8): a plan job, parts on all
   three hosts, a salvage job per host and the verdict. A failing CPU, GPU, two-card and
   compile test each end red in their own part only; a stopped runner on a host other
   than the verdict's is salvaged by that host's salvage job; an OOM-killed part ends
   `oom` and is retried.
7. h100's plan with `v1.4.0` changes nothing against `v1.3.0`'s apply, and the canary of
   `h100-test.yml` passes on `@v1.4.0`, then on `@v1` after the move.

## 11. brainspike on top of this (decision 12)

brainspike composes the actions in its own `ci.yml` (§4.8), on all three hosts. Its
contract is the CI contract of its spec repository; this section lists only what it
takes from here:

| brainspike needs | ci-runners piece |
|---|---|
| claim, its context `brainspike/ci-shadow`, then `brainspike/ci` | `claim` (§4.1) |
| a plan job and a parts matrix | a `part` on `3090b-light`, then one `part` per matrix leg (§4.8) |
| live NEST | the env spec's `conda` table (§4.9) |
| upstreams at their `main` | `env refresh`, `refresh = true` (§4.9) |
| compile gates on pinned CPUs of the reference host | `3090b-compile`, `compile_cpus`, the host lock (12.7, 12.9) |
| two-card gates | `<host>-multi` with the P2P self-test (12.6) |
| the nightly | `3090a-nightly` (12.8) |
| local performance and CI runs beside the runners | `ci-lock` and `ci-slot` on its files (12.9) |
| coverage over the parts | a `part` that combines the parts' data artifacts |
| a verdict over three hosts | a `salvage` job per host, then `verdict` (§4.5) |

## 12. Decided choices (lead, 2026-11-05)

1. **Visibility of ci-runners.** The repository is public, with rulesets on `main` and
   `v*` that only the lead may bypass. It never holds inventory (D1).
2. *Repository-level GPU runners for public consumers.* Retired by 11.12.
3. **Runner pools.** Pools are shared per host and class, with one waiting seat per
   project and `max_waiting`.
4. *The CI proxy.* Retired by 11.12.
5. *First public consumer (RUN-4).* Retired by 11.12.
6. *Verify timing.* Retired by 11.12.
7. **Early thermal stop.** A part whose card stays at 85 °C or more for 3 min is stopped
   and retried on a cool card, before the watchdog throttles or kills it.
8. *Manifest cost.* Retired by 11.12.
9. **Default group's repository access.** Every private repository (decision 11.1);
   `allows_public_repositories` stays off.
10. **Org base permission.** It stays `write` for now: the hook and the rulesets cover
    the runners.
    - *Follow-up for the lead:* tighten it later to `read`, with write granted by team.
      On Free that is the only way to protect each private repository's `main`.
