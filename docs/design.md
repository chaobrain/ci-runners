# ci-runners: design (contract)

Status: approved by the lead on 2026-11-05 (#1). Decision 11 (2026-11-06) made h100 a
standard test CI for every private chaobrain repository (RUN-5, live since `v1.2.0`).
Decision 11.12 (2026-11-06) retired the first path: the 3090b runners that served
brainspike, the ssh driver to h100, the CI proxy, placement and the public-repository GPU
path. The design as it stood before is the one at tag `v1.2.0`. Decision 12 (2026-11-07,
RUN-7) brings 3090b and 3090a back as local hosts under the RUN-5 model, with the pools,
the host-lock and slot interop and the classes that brainspike's test CI needs.
Decision 12.17 (2026-11-07) retires h100 and `h100-test.yml`: the CI runs on the two
3090 hosts only. Decision 13 (`v1.5.0`, RUN-8) adds a drain switch and busy-host
avoidance, deletes the h100 class and the watchdog, and moves the hooks' path off the disk.
Decision 14 (2026-11-08, RUN-9) adds a time-billed Slurm cluster as a remote target of the
3090 hosts' runners: the `slurm` class, the gate, the site config and the install commands.

Guides, in Chinese: [consumers.md](consumers.md) for the repositories that use the CI,
[operations.md](operations.md) for the maintainer of the hosts. This file is the contract;
where a guide and this file disagree, this file holds.

This repository is public (D1). Hosts appear here under role names: **h100** (retired by 12.17), **3090b**,
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
       to their queue timeout and then end `unavailable` (from `v1.6.0` a first attempt
       with somewhere else to go goes there at once instead, 13.6). Its `gpu-mem-mib` is checked on
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
       - **Fix: the light probes take turns** (issue #69; `v1.6.0`). A light request
         (`-l`) finds a normal shared holder by a non-blocking exclusive try on the shared
         marker, which a concurrent try refuses as if a holder were in: two light requests
         made together could each take the other's try for a holder and enter past a
         waiting exclusive request. brainspike's `host_lock.sh` makes its tries take turns
         under a fourth file, `<stem>.probe.lock` (its #409); `ci-lock` does the same, so
         the turns hold across the two implementations, and the probe file joins the shared
         files: the `tmpfiles.d` entries (root-owned, `rw` ACL entries for both accounts)
         and the private namespace's `BindPaths=` (12.16). Exclusive holds were never at
         stake, only the order behind a waiting exclusive request. A probe file that cannot
         be opened (a host before its apply, where the namespace's `/tmp` lacks it) leaves
         the try without turns, as before. The host's plan changes both root files, so the
         apply restarts the user manager (12.16, §5.3).
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
    16. **The CI account sees no other account's files** (RUN-7g; lead, 2026-11-07,
        #46: a private `/tmp` on both hosts, and the data disks and `/home` with it). Measured before any job ran: the CI account, and so
        a job, could read tens of thousands of the local account's files in `/tmp`,
        `/var/tmp` and `/dev/shm` on 3090b (left there with a 002 or 022 umask: copies of
        private code, logs) and other people's on 3090a, and other people's directories on
        the data disks of both hosts. No local account's file
        modes change. Instead the CI account's user manager, `user@<uid>.service`, runs in
        its own mount namespace, so every runner, part scope and unit of the account sees:
        a private `/tmp` and `/var/tmp` (`PrivateTmp=yes`) and an empty `/dev/shm`; an
        empty `/home` and an empty data-disk root, with only the account's own `home` and
        `data` bound back; and, bound in from the host's `/tmp`, only brainspike's
        six shared lock files and its slot queue (12.9; the probe file since `v1.6.0`), so `ci-lock` and `ci-slot` still
        meet `host_lock.sh` and `ci_slot.sh`. A root drop-in on that one unit does it
        (§5.3); the user units, the executor and the part scopes do not change, since a
        `systemd-run --user --scope` part keeps its caller's namespace and stays in the
        user manager's cgroup tree with its caps (§5.5). It is not done in a user unit,
        because `PrivateTmp=` there needs an unprivileged user namespace, which
        `kernel.apparmor_restrict_unprivileged_userns=1` refuses on both hosts; nor by
        system units with `User=`, which would move every runner unit out of setup's
        user units for the same result. The data-disk and `/home` part goes beyond
        "private `/tmp`"; the lead approved it with the rest (lead, 2026-11-07). A
        repository whose CI needs a path on a data disk gets it bound in explicitly, by
        a policy or inventory entry, with its own plan line; none does yet, so `v1.4.0`
        binds back only the account's own `home` and `data`.
        **Processes too** (lead, 2026-11-07): the same drop-in sets
        `ProtectProc=invisible`, so `/proc` in the namespace shows only the account's own
        processes; `ps` shows no other account's command lines (on 3090b the CI account saw 21
        accounts' processes before). Measured on 3090b on 2026-11-07 in a scratch system
        unit as the CI account, with the drop-in's settings: `ps` showed one account, its
        own (21 without the setting), 13 entries in `/proc` (625 without); a
        `systemd-run --user --scope` child saw the same and shared the namespace, and with
        `MemoryMax=64M MemorySwapMax=0` an allocation of 300 MB was killed (status 137);
        `nvidia-smi -L` showed both cards and `cuInit` and `cuDeviceGetCount` saw two
        devices; `Runner.Listener --version` ran; `ci-lock -s` took the host lock.
        `nvidia-smi --query-compute-apps` still lists another account's compute process,
        with its PID and memory, but with the name `[Not Found]`, and no `/proc/<pid>`
        for it: a PID without an owner. RUN-8 counts such a process as foreign (one the
        account cannot see is never its own), failing closed.
    17. **h100 is retired (lead, 2026-11-07).** h100 crashed and is in maintenance for
        days. Only the canary of RUN-5 ever called `h100-test.yml` (code search, and the
        workflows of every chaobrain repository that runs CI, searched 2026-11-07), and
        brainspike used h100 only for one-card GPU parts, which the 3090 hosts' four
        cards cover. h100 was also the costliest host to keep: a shared account (T6,
        T13, T14), no private `/tmp`, its own egress and push path, the watchdog, the P2P
        fault (D7). So:
        - h100's role in the inventory, its 9 runners and their labels (`h100-light`,
          `h100-cpu`, `h100`, `h100-gpu<N>`) go: the runners are deregistered, and no
          consumer may target them (§4.8);
        - the workstation pushes the snapshot to the 3090 hosts only (12.4);
        - `h100-test.yml` (§4.7) is deleted, not restored to `@v1` after `v1.4.1`: its
          only caller is gone. A consumer composes the actions (§4.8). The release stamp
          is `actions/RELEASE` alone, since no workflow of this repository calls an
          action (§9);
        - the h100 canary, RUN-7 acceptance 7 and `v1.4.0` notes item 6 go: `v1` moves
          to `v1.4.1` after the canary composes the actions at `@v1.4.1` on both 3090
          hosts, with no h100 check before it.

        What stays: the per-card pool code, the `h100` runner class, the health gate's
        watchdog inputs and h100's rows in §3, §5 and §6, which the 3090 rows cite ("as on
        h100"). They are generic, driven by inventory keys and covered by tests; with no
        host using the watchdog keys or the `h100` class, RUN-8 (`v1.5.0`) reviews whether
        they go. The example inventory and the installed units keep their comments, so
        no host's plan changes. h100 comes back, if it returns healthy, by a setup plan
        approved by the lead, a registration and a canary, as any new host (§10).
        Decision 13.4 reviews them: they go.

## Lead decisions (2026-11-07): `v1.5.0` (RUN-8)

The lead approved the scope on 2026-11-07 (a drain switch; busy-host avoidance; the h100
class and the watchdog clean-up; the runner's hook line, option (a)) and delegated the
approval of this text to the maintainer. The defaults below are starting values, tuned
from the load record they add.

13. **Operating two shared hosts.**
    1. **The drain switch.** `ci-run drain on [--pool P]`, `ci-run drain off [--pool P]`
       and `ci-run drain status`, run as the CI account on its host. `on` drains the
       host, or with `--pool` one pool, named by its inventory key or its class (each is
       unique on a host). The state is one file, `state/drain.json`, replaced whole (a
       rename) under `state/drain.lock`, so it survives a restart of the runners, the
       user manager or the host. A missing file is "not drained"; an unreadable or
       malformed one drains the host (fail closed, reason `drain state unreadable`).
       - A drained host or pool takes no new part. `part` reads the drain in its gate,
         before it takes anything, and again once it holds its slot and leases, just
         before the command. Either way the part releases what it took and ends
         `unavailable` (`drained: host` or `drained: pool <P>`), with `next` and
         `retry-on` pointing at the other host's pool of its class (13.3).
       - A part whose command has started finishes. `claim`, `salvage` and `verdict` are
         not parts and are never refused, so runs in flight end and their records stay
         readable. A light part (a plan job) is refused like any other.
       - `off` without `--pool` clears the host and every pool; with `--pool`, that pool
         only. `status` prints the host's and each drained pool's state with the time it
         was set, and the part scopes still running (`ci-part-*.scope`), so the operator
         sees when a drained host is idle before restarting its runners.
       - The hook does not read the drain: a refusal there fails the job without outputs,
         which a consumer cannot retry. Job code runs as the CI account and can change
         the file, as it can the hook (T13): the drain is the operator's tool, not a
         boundary.
    2. **Busy-host avoidance.** Both 3090 hosts run work outside the runners: the local
       brainspike account's runs on both, and other people's on 3090a. A part's first
       attempt reads the host's load record before it takes its slot. If one of the rules
       below held in every sample of its window (the last `busy.window_minutes`, or
       `busy.lock_minutes` for the host lock), the part ends `unavailable` (`busy:
       <rule>`) with `next` `fallback` and `retry-on` the other host's pool of its class
       (13.3). "Other accounts" are every account but the CI account, the local
       maintainer's included: local runs on 3090b send parts to 3090a, as other people's
       work on 3090a sends them to 3090b. A `busy:` reason names the rule, the value
       measured and the threshold (e.g. `busy: cpu 17.2 >= 14 cores for 10 min`), as does
       the part's summary, so the defaults can be tuned from the runs. The rules:
       - **CPU**: other accounts' CPU use at or above `busy.cpu_cores`: the busy time of
         `/proc/stat` (user, nice, system, irq, softirq, steal) minus the CI account's
         (`cpu.stat` of its `user@<uid>.service`), per second between two samples;
       - **memory**: other accounts' memory at or above `busy.memory_gib`: `MemTotal`
         minus `MemAvailable`, minus the CI account's anonymous memory (`anon` in its
         `memory.stat`);
       - **GPU**, for a part that leases cards, on the cards it may lease: other accounts'
         compute memory on one of them at or above `busy.gpu_mib`
         (`nvidia-smi --query-compute-apps`). A process whose `/proc/<pid>` the account
         cannot see is another account's: under `ProtectProc=invisible` (12.16) the
         account sees only its own, so this fails closed;
       - **the host lock** (12.9): an exclusive hold by another account for
         `busy.lock_minutes` or more: a `FLOCK WRITE` in `/proc/locks` on the host lock's
         inode, by a PID the account cannot see, the same PID in every sample of the
         window.

       The load record: a new user timer, `ci-hostload.timer`, runs `ci-run hostload
       sample` every 60 s, which appends one line (the raw counters and the four values)
       to `state/hostload/<date>.jsonl`; the reaper deletes the files of days more than 2
       days old. A window with fewer than half its samples (the timer stopped, a fresh
       host) is not busy: busy avoidance places load and is no boundary, so it fails
       open, and the part waits for its slot as in `v1.4`.

       It is checked only where the part can go elsewhere and only once:
       - for a class with a peer pool (13.3), except `light` (short, no slot);
       - never on a retry (`attempt-of` set), so a retry does not bounce back;
       - once per head and part on a host: a part refused as busy leaves a marker under
         `state/hostload/refused/` named by its repository, head (`GITHUB_SHA`) and id,
         and a later first attempt of the same head and part there (a re-run) waits for
         its slot instead. The reaper deletes markers after 14 days.

       The thresholds are the inventory's optional `busy` table (§3.3), with these
       defaults: `window_minutes` 10; `cpu_cores` half the host's online CPUs (14 on each
       3090 host); `memory_gib` half its `MemTotal`, rounded down (94 on 3090b, 62 on
       3090a); `gpu_mib` 2048; `lock_minutes` 20 (brainspike's shared holds last at most
       about 15 min, so a longer exclusive one is a timing run).

       *Measured on 3090a* (2026-11-07, `sar`'s 10-min means, 28 CPUs; most of the load
       was niced, as the local brainspike account's test runs are). A mean at or above a
       threshold is needed for every sample to be, so these counts bound from above what
       the rule would have found. Other accounts' CPU use
       (user, nice and system, which also hold the CI parts' few minutes) was at or above
       14 cores in 62 of 143 intervals (about 10 h, in spans of up to 130 min), above 16
       cores in 46 and above 20 cores in 25. Memory used reached 62 GiB in 1 interval
       (peak 62.6 GiB). All 6 intervals in which a CI part ran on 3090a were at or above
       14 cores: with these defaults those parts' first attempts would have gone to 3090b,
       which was at or above 14 cores in 14 of its 143 intervals that day. A snapshot on
       2026-11-08 at 00:03: 25 cores and 31 GiB of the local account's, and its 2.6 GiB on
       card 0 at 89 % use. No GPU or host-lock history exists on either host (`sar` has
       no GPU, the lock writes no log); the load record keeps both from `v1.5.0` on.
    3. **Where a refused part goes, and retries across hosts.** A new optional inventory
       key, `peer_labels`, maps a runner class to the labels of the other host's pool of
       that class (on 3090b: `light = ["3090a-light"]`, `part = ["3090a-cpu"]`,
       `gpu = ["3090a-gpu"]`, `multi = ["3090a-multi"]`). A drained or busy part's
       `retry-on` is `self-hosted` and those labels, with `next` `fallback`, within the
       retry budget of §4.4 (on its last attempt, `next` is `none`). A class with no
       entry (`compile` on 3090b, `nightly` on 3090a) has nowhere to go: a drained part
       of it ends `unavailable` with `next` `none`, which the verdict reports as error,
       and busy is not checked for it.
       - **A retry on another host.** `attempt-of` names the first attempt's record, on
         the host that ran it, which no other host can read: `v1.4` refused it there
         (exit 2), although §4.8 rule 4 allows a `fallback` on another host's pool. From
         `v1.5.0` a record of another host is taken by its name alone: the attempt is the
         `n` of its `<id>-<n>` plus one, and its id must be the part's; nothing else is
         read. A record of this host is read as before.
    4. **The h100 clean-up** (12.17's review). These go, with their tests, fixtures and
       mutants:
       - the `h100` runner class: in the policy's classes, the executor's class rows and
         the per-card classes;
       - the h100-style host test and what hangs on it: D7's refusal of a part with
         `gpus > 1`, and a reboot counted as thermal (`reboot` is retried on the same
         host, as on the 3090 hosts now);
       - the watchdog: the inventory keys `watchdog_log` and `watchdog_notice` (an
         inventory that sets one is refused as having an unknown key; no host's does),
         the notice in the health gate, the log read after a part and the kill-line match
         in the classification (`thermal` comes from the early stop only, as it already
         does on the 3090 hosts);
       - §5.4, folded into §5.5, and h100 in §4.3, §4.4 and §5.1. h100's rows in §3,
         §5.3, §6 and §8 stay as history, since the 3090 rows cite them.

       What stays: the per-card pools (class `gpu`), the health gate on temperature, the
       sampler and its early stop, the boot id, and the supervisor's memory cap (11.11)
       for a host without a memory controller. Nothing on the 3090 path changes
       behaviour: the hosts' inventories and policies load as before, and every class's
       row, gate and outcome there is as in `v1.4.1`. The installed units keep their text
       (the comment in `ci-tunnel.service` that names h100 included), so the clean-up
       alone changes no host's plan. h100 comes back, if it does, as a new host with a
       `gpu` pool (§10).
    5. **The hook's path names no disk** (`v1.4.0` notes item 5, option (a)). A new user
       unit, `ci-hooks.service` (oneshot, `RemainAfterExit=yes`), runs `ci-run hooks
       copy`: it copies `job-started.sh` and `job-completed.sh` from
       `<data>/ci-runners/hooks/` into `/tmp/ci-runners-hooks/`, in the CI account's
       private `/tmp` (12.16).
       - The directory is the account's, mode 700, and refused if it is a link or another
         account's; each copy is written under a temporary name, mode 500, and renamed.
       - The hooks find their release through a line setup writes into them
         (`base=<data>/ci-runners`, as it writes the interpreter's), not through their own
         path, so a copy runs the same release as its source. This changes the hook files
         once.
       - Every runner's `.env` names the copies, so the runner's "Run '<hook path>'" line
         in every job log names `/tmp/ci-runners-hooks/`, not the data disk.
       - **Fail closed.** `ci-runner@.service` gets `Requires=` and `After=ci-hooks.service`
         and an `ExecStartPre=` that checks both copies are executable files, so a runner
         does not come up without them. The reaper compares the copies with their sources
         every 10 min and rewrites a missing or different one, and logs it (`/tmp`'s age
         cleaning, 10 days, reaches into a private `/tmp` too). A hook missing when a job
         starts fails that job: the runner fails "Set up runner" when its hook file does
         not exist (checked live, RUN-8 acceptance 4).
       - Setup plans the unit on a host whose CI account has the private namespace
         (12.16): both 3090 hosts. Elsewhere the runners keep the hooks under `data`,
         since in a shared `/tmp` another account could plant the directory first. Apply
         starts `ci-hooks.service` before the runners, and reloads it when a hook
         changes (its `ExecReload=` copies again; a restart would restart every runner
         through `Requires=`).
       - Known limit, unchanged: the runner still writes the machine name into every job
         log.
    6. **No wait behind the nightly, and a retry queues on a full queue** (lead,
       2026-11-08, from the CI-time review of brainspike's runs that morning). While a
       `nightly` part held every slot and card of 3090a, PR parts' first attempts there
       waited out their 20-minute queue timeout before going to 3090b; others ended at once
       with "project queue full", and their retries, piled onto 3090b, could meet the cap
       there too and end `unavailable` with `next` `none`, which the verdict reports as an
       infrastructure error with nothing wrong in the change. So, from `v1.6.0`:
       - **The nightly's hold record.** A `nightly` part writes `state/every.json` before
         it takes its slot ticket and deletes it when it ends: the processes that keep its
         hold, by PID and start time (the executor; from the command's start the
         supervisor too, which keeps the slots and leases after a stopped executor, §4.3),
         and its record. The record holds while one of those processes runs; a record whose
         processes are all gone holds nothing (fail open: the part waits as in `v1.5`).
       - **A first attempt does not wait behind it.** The busy check (13.2) gains a rule
         that comes before the load rules: while the hold record holds, a part's first
         attempt ends `unavailable` (`busy: nightly holds every slot and card`) at once,
         with `next` `fallback` and `retry-on` the other host's pool of its class (13.3).
         It applies where busy does (a class with a peer pool, other than `light` and
         `nightly`; a project with a retry budget; never on a retry), every time and with
         no marker: once in, the nightly holds every slot and card (12.8, §7), and while it
         waits its slot ticket holds back every later slot ticket, so a part sent away
         saves its whole queue timeout. A `gpu` or `multi` part is sent away too while the
         nightly still waits for the slots, though a card may be free then: the nightly
         takes the cards next, and a lease taken in between would hold it back.
         A first attempt that passed the gate before the record appeared checks again
         while it waits: a slot part goes elsewhere unless its slot ticket is older than
         the nightly's, a card part once the nightly runs or holds an older lease ticket
         than its own. A write of the record that fails is ignored (fail open). Generalising to "a hold whose expected end is past
         the queue timeout" is not done: only the nightly holds a whole host, and its
         timeout (600 min) is no estimate of its end.
       - **A retry queues on a full queue.** The project cap of §7 rule 2 (`max_waiting`
         per pool) refuses a first attempt only; a retry (`attempt-of` set) takes its
         ticket past the cap and waits up to its queue timeout, as a retry already does
         on a busy or drained host's peer. The cap still counts a waiting retry, so the
         first attempts that come after it go elsewhere. The backlog stays bounded: a
         retry exists only after its first attempt ended, a part is retried at most once
         (§4.4), and each waiting retry holds a runner of its pool, so a pool never has
         more waiting retries than runners, and each waits at most its queue timeout.
       - A retry that still finds no slot ends `queue timeout: no CI slot` (or `no card
         fits`), `next` `none`, as before: both hosts were full for its whole wait.

## Lead decisions (2026-11-08): a Slurm cluster as a remote target (RUN-9)

Approved by the lead on 2026-11-08, each open choice as recommended, with one amendment
(14.10). The request: add a time-billed Slurm cluster (CPU nodes and several GPU
partitions) as a test host, with one standard install that works under several accounts
of the cluster, to spread its cost, and moves to another Slurm system. The site's names
(its login host, partitions, nodes, accounts and paths) are inventory: they live in a
private site config on the driving hosts (§3.3) and never here.

*Measured on the first site* (2026-11-08, twelve short jobs): compute nodes have no route
out and the login node resolves no outside name; `srun` from the login node fails while
`sbatch` works, and `srun` inside an allocation works; a job's memory is not enforced
(cgroup v1 with no memory controller for the job: a `--mem=1G` job touched 12 GiB); the
only GPU request that works is `--gpus=N`; a job on a free resource starts within 1 s and
`sbatch --test-only` over-estimates the wait by hours; accounting is per second; `scancel`
sends TERM to the batch shell at once, and the time limit TERM then KILL after the site's
`KillWait`; no container runtime on any node; drivers differ per node type; and on one
node type with its two cards on different sockets the driver reports peer access while
peer copies read zeros, so a two-card jax `psum` returned a wrong result with exit 0.

14. **A Slurm cluster as a remote target.**
    1. **Option A, a remote executor.** The runners stay on the 3090 hosts, which have the
       egress, the host layer and the records. A part of the new `slurm` class runs as one
       batch job on the cluster; nothing of ours runs there between parts, so the site
       bills only while a part's job runs (pending is not billed). Rejected: a runner on the
       login node (a resident daemon and a permanent tunnel on a shared login node, and a
       runner credential on the cluster) and a runner per allocation (compute nodes have
       no route to GitHub; runner set-up would be billed card time; a JIT runner needs an
       org-admin credential at the dispatcher).
    2. **The `slurm` class.** A host's `slurm` pool (labels `<host>-slurm`, a shared label
       across hosts, as `3090-multi`) takes no CI slot and no card of its host. It is
       opt-in per repository (`classes` in `policy.toml`, as 12.14). Its `part` takes one
       new optional input, `target`: a target name of the site config, or a list in the
       order to try. It accepts `gpus` up to the target's `max_cards`, `lock: none`, and
       `timeout-minutes` up to the target's `max_minutes`.
    3. **One batch job per part attempt.** The executor (driver side):
       - **gate**: the drain (13.1), the class row, the target enabled for the project
         (14.10), the budget (14.7);
       - **choice**: for each target in the part's order, the eligible accounts, least
         spent this month relative to their weight first, round-robin on ties (14.8);
       - **stage**: `rsync` of the workspace and of the part's inputs under `RUNNER_TEMP`
         to the account's work directory on the cluster's shared file system, through
         the gate's `stage` verb;
       - **submit** through the gate's `submit` verb, with a JSON request (target, cards,
         CPUs, memory, minutes, the project's variables); the gate renders the release's
         batch template, never a flag from the request verbatim: the account, the
         target's partition and node list, the site's GPU request syntax, `--time` = the
         part's timeout + 5 min, a warning signal before it, a job name `ci-<repo>-<id>-<n>`,
         the run's URL as the job's comment, `--export=NONE` with an explicit environment;
       - **pending bound**: a job pending past the site's per-target bound is cancelled
         and the next account or target is tried, within the part's
         `queue-timeout-minutes`; past that the part ends `unavailable` (`pending`);
       - **watch**: the part's supervisor, in its systemd scope on the driver host (so a
         stopped runner leaves it running, §4.3), polls the job's state and relays its log
         by offset over one ssh master connection;
       - **cancel**: the part's TERM cancels the job (`scancel`), and the part ends
         `stopped`;
       - **results**: after the end, `RUNNER_TEMP` and the part's results are synced back,
         so the consumer's upload steps run unchanged; the full log goes into the record.
       In the job, the cluster supervisor (`supervise`, §4.3) enforces the inner timeout
       and the memory cap by `RssAnon` (11.11: the nodes enforce none), and runs the P2P
       self-test before a two-card command (12.6).
    4. **The end, mapped to the outcomes of §4.4** (no new outcome):

       | the cluster's end | outcome, `next` |
       |---|---|
       | the command exits 0 | pass |
       | the command fails, or the inner timeout | red |
       | the cluster supervisor's memory cap | oom, retry with `resume` on the same target |
       | the job's own time limit (the supervisor stuck) | red, with the reason |
       | a node failure (`NODE_FAIL`, `BOOT_FAIL`) | unavailable (`node fail`), retry on the same target |
       | cancelled by another user of the site, or killed by the node | unavailable, fallback |
       | the P2P self-test failed | unavailable (`p2p`), none; the target's `max_cards` drops to 1 until its canary passes again |
       | pending past the bound, the budget, no eligible account, a target not enabled | unavailable, fallback |
       | the login host unreachable for 5 min during a part | lost: the record keeps the job's id; the verdict salvages it, and the reaper cancels the job once its GitHub job is gone |

       The record stays on the driver host, so `salvage` and `verdict` do not change. It
       gains a `slurm` block: site, account alias, target, job id, node, state, elapsed,
       cards, CPUs and the cost by the site's prices; the part's summary line (the step
       summary, `outcome.json`'s `slurm`, the `ci-part-*` artifacts) names the target, the
       job and the cost, not the site, the account or the node, and the executor's own
       reasons pass the redactor too.
       What a part shows on GitHub (the job's log it relays, the gate's and ssh's error
       text, its reason and summary) passes a redactor built from the site config
       (`slurmsite.Redactor`): our site and account aliases (`<site>`, `<account>`: an
       alias may name its owner), each account's root (`<root>`) and its parent (`<home>`),
       also URL-encoded, Slurm account (`<account>`), ssh alias and the login host,
       ProxyJump hosts and users its ssh config names (`ssh -G`: `<host>`, `<user>`), the
       partitions (`<partition>`), the nodes (`<node>`: the config's, the cluster's whole
       node lists from `sinfo` through the gate's `nodes`, cached 1 h, and those a job ran
       on; host lists expanded, and a compressed list in text such as a pending reason's
       `UnavailableNodes:` replaced whole), the login Python, proxy and extra `sbatch`
       arguments. A host or node takes its domain with it, and ANSI escape sequences are
       removed first. A value of 8 characters or more that holds a non-letter is replaced
       wherever it occurs; any other as a whole word only (also after `%2F` or a repr's
       `\n`; a value of letters only not next to `_` either, so `standard` keeps
       `nonstandard` and `test_standard_normal`), a node name always, in any case. A
       value equal to a target, variant or GPU model name or
       one the deny list would not hold (under 4 characters or a common word; nodes from
       2) is kept. Text is redacted before it is cut. The log is redacted by whole lines,
       a line over 64 KiB never cut through a value, so that no name splits across two
       reads. An account whose ssh config `ssh -G` cannot read, or a cluster none of whose
       login hosts gives its node lists (none cached either), ends the part `unavailable`
       before a job; so does an answer that is empty or holds a list that does not parse
       or names over 4096 nodes (unknown, the cache not used). The gate keeps 4000
       characters of a Slurm command's error text, so that the driver redacts it before its
       own cut; an error the driver did not expect ends it `unavailable` (`driver:
       <type>`), its traceback only in the record. The record on the driver host keeps
       everything as it was. The files a part fetches back (`part/` into `CI_PART_DIR`,
       `tmp/` into `RUNNER_TEMP`: results, coverage data, the command's own logs) are
       **not** redacted: they are the consumer's, and a consumer that uploads or prints
       them publishes whatever paths and host names they hold (consumers §7).
    5. **The gate.** The only command a cluster key runs: its `authorized_keys` line is
       `restrict` and forces `<root>/ci-runners/releases/hook/bin/ci-slurm-gate`. Its verbs:
       `stage` (a restricted `rsync --server` into `work/` only), `submit`, `status`, `log`,
       `cancel`, `fetch`, `ledger`, `nodes` (`sinfo`'s node lists, for the redactor), `reap`, `check`. It validates each request against the
       cluster-side copy of the account's entry (targets, caps, minutes) and exits; it
       keeps nothing running.
    6. **The site config** (`etc/slurm/<site>.toml` on each driver host, mode 600; never in
       the repository; `setup/slurm/site.example.toml` holds placeholders, and the deny list
       gains the site's tokens). It holds the site (GPU request syntax, extra `sbatch`
       arguments, the login host's Python, the egress mode, caps, the per-target pending
       bound), the prices (per core-hour, per card-hour by GPU model; whether a GPU job also
       pays its CPUs), the targets (partition, nodes, GPU model, cards per node,
       `max_cards`, env variant, CPUs per card, memory per node, `max_minutes`, `enabled`)
       and the accounts (ssh alias, root on the shared file system, Slurm account,
       targets, weight, monthly cap).
    7. **Budget.** A ledger from each account's `sacct` (all its jobs this month; ours named
       `ci-`), priced by the site table, cached 10 min. A part whose worst case (cards ×
       `--time` × price, or cores × time) would cross the site's daily or monthly cap, or
       the account's, ends `unavailable` (`budget`). It guards against our own faults and
       runaway retries, not against job code (T19, T21); a hard cap is the site's.
    8. **Accounts.** Each account is one config entry and one install (14.9); the
       account's owner installs the key line the plan prints. Choice per part: least
       spent this month relative to `weight`, round-robin on ties, skipping an account
       over its own cap or without the target.
    9. **Install** is `ci-run slurm check|plan|apply|env|canary|status|uninstall <site>
       <account>`, run on a driver host as its CI account:
       - `check` (read-only): ssh in batch mode; `sbatch`, `squeue`, `sacct`, `scancel`,
         `sinfo`, `rsync` and the login Python on the login host; Slurm ≥ 20.11; per
         target: the partition up, the account's groups allowed, its association with
         enough cards, CPUs and nodes, `sbatch --test-only` accepting the rendered
         request; the root writable with 50 GiB free; nothing another user could swap
         it with (`slurmgate.unsafe_dirs`): the root and its `ci-runners` tree the
         account's own directories, not links, that its group and others may not write,
         and every directory above it up to `/` (as written and as resolved) the
         account's or root's, writable by others only with the sticky bit, and a link
         among them root's or the account's (its owner may repoint it) (`prepare` in
         `apply` refuses such a root too; mode bits only, not ACLs); the egress mode's
         prerequisites;
       - `plan` prints every change (the root's directories, mode 700; the release copied
         from the driver, of the tag the driver runs; the account's cluster-side entry; the
         key line; each env build; the canary's jobs and their cost), and changes nothing;
         `apply` applies exactly an approved plan, as `setup/apply.sh` does;
       - `env` builds the project's env on the login host (the nodes' C library; not on the
         driver, whose newer one could pick wheels the nodes cannot load), with egress for
         the build only, by the site's mode: `tunnel` (a reverse forward from the driver to
         a Unix socket in a 700 directory, and on the login host a stdlib relay on
         127.0.0.1 that accepts only the build's random `Proxy-Authorization`, passed in
         the environment; the egress host's allowlist applies, 11.10; nothing listens
         after the build), `push` (a wheelhouse and conda packages downloaded on the
         driver, installed offline), `direct` or `proxy`. The build's steps run with an
         empty `HOME` of their own and no pip config file, so the account's own conda
         and pip settings (a mirror the egress does not allow) do not reach it; a part's
         job reads neither the account's `~/.local` packages (`PYTHONNOUSERSITE=1`) nor
         its pip config, and has no conda on its `PATH`. One build runs at a time per
         root, whatever its project (they share conda and its package cache), and a
         `conda create` that exits 0 yet leaves no python fails the build. The driver's record
         of each variant's build also takes what the login host's links serve. It builds one env for the
         variants the account's targets run that the spec does not name, and one of its own
         for each `[variant.<name>]` the spec holds (its `install` replaces the base's
         and its optional `conda` packages join the base's in the same solve, amended
         2026-11-09 for a CUDA compiler per variant; §4.9),
         and points each `<project>-<variant>` at its build only once every build
         has passed, every build holds the first's upstream commits (one branch that moves
         during the run fails it) and every link path is a link; a job resolves that link
         when it starts. Old builds no link holds go after the grace, but the newest 3 of
         each kind a link still holds. `env --refresh`, a maintainer's command (14.11),
         compares the upstreams' refs with each variant's kept build and rebuilds the
         variants whose upstreams moved or whose build is of an older spec;
       - `canary`, per target, one job of at most 3 min (priced, in the plan, at its
         `--time` of 8 min): the env imports and sees the
         cards, the driver against the variant's floor, the P2P self-test where
         `max_cards` ≥ 2 (a failure sets it to 1), a file round trip, the start latency
         and the accounting fields;
       - `uninstall` refuses while a `ci-` job of the account runs (`--cancel` cancels
         them), marks the account retired (its ledger rows stay for the month and count
         against the site's caps), removes the key lines that force its gate, then the
         root's `ci-runners` tree.
       `apply`, `uninstall` and a `check` before an install run through the account
       owner's own login (`--via`), since the CI key reaches only the gate; `check`'s
       `--test-only` asks for each target's largest part (`max_cards`, `max_minutes`), which
       checks the association's limits; the egress prerequisites are checked by `env`.
       Another Slurm system is another site config; the code assumes only ssh key login
       to a login host, the Slurm client commands there, a file system shared with the
       nodes, and Slurm ≥ 20.11.
    10. **Targets count only after their acceptance gates** (lead, 2026-11-08; amends a
        proposed observation week: no observation periods). A target is `enabled` in the
        site config only after, in order: `check`, `canary`, one real one-card part and
        one real two-card part with the P2P self-test (a CPU target: one real CPU part),
        each green. Until then the executor refuses it (`unavailable`).
    11. **Nothing resident on the cluster.** No unit, timer, tunnel or listener of ours on
        the login host or a node; an env build runs on the login host only while a
        maintainer runs it, at nice 10.
    12. **Threats** T19–T22 (§6.4). CI runs on a dedicated cluster account, or on the
        account owner's own account once the credentials it holds for other tools are
        moved off it (lead, 2026-11-08: a dedicated one cannot always be had); a part can
        read whatever the account holds (T19). The exit path for T19 is a dedicated cluster
        account per site, from the site's admins, with a hard spending cap.

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
  - The *workflow layer* holds the actions (the reusable workflow went with 12.17). It
    is thin and calls the host layer of its own release.
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
- **D7.** *One card per h100 part.* Lapsed with h100 (12.17), its code deleted by 13.4. The
  3090 hosts' `multi` pool runs two-card parts behind a P2P self-test (decision 12.6).
- **D8.** *The keyed verify draw.* Retired by 11.12.

## 1. Scope

In scope: host setup, runner services, the pre-job hook, the executor of a part, shared
slots and GPU leases, the host lock and the slot interop on the 3090 hosts, the P2P
self-test, outcome classification and retry hints, the claim step, salvage, the
verdict, the org snapshot, the hosts' egress and the env builds. The reusable workflow
`h100-test.yml` was retired by 12.17.

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
| Self-hosted runners and standard hosted runners for public repositories are free. | docs: Actions billing | This repository's CI runs on hosted runners. A private caller's self-hosted jobs spend no hosted minutes (§4.8). |
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
  drain.py            the drain switch (decision 13.1)
  hostload.py         the load record and the busy rule (decision 13.2)
  sampler.py          the GPU sampler and the early thermal stop (§5.5)
  outcome.py          outcome classification and the retry hint
  slots.py gpus.py    fair host slots with caps, GPU leases in ticket order per card
  locks.py            `ci-lock`: brainspike's host-lock protocol on the 3090 hosts (12.9)
  p2p.py              the P2P self-test of a `multi` part, over the CUDA driver API (12.6)
  verdict.py          the verdict: the parts' outcomes, salvage, the status, the summary
  registry.py         the project registry
  envbuild.py         `ci-run env build` and `env refresh` (decisions 11.4, 12.10, 12.11)
  reaper.py           stale scopes, work directories, records and logs
  hook.py             the pre-job and post-job hooks; their copy in `/tmp` (decision 13.5)
  setup.py            plan and apply
  inventory.py        schema, loader, lint
  slurmsite.py        the Slurm site config: schema, loader, prices, deny tokens (14.6)
  slurmgate.py        the cluster's gate (14.5), the batch template and the job's supervisor (14.3)
  slurmpart.py        the executor's `slurm` path: choice, budget, stage, submit, watch, end (14.3, 14.4)
  slurmsetup.py       `ci-run slurm check|plan|apply|status|uninstall`: an account's install (14.9)
  slurmlogin.py       the install's steps on the login host, through the owner's own login (14.9)
  slurmenv.py         `ci-run slurm env`: a project's env on the login host, its egress for the build only (14.9, D4)
  slurmcanary.py      `ci-run slurm canary`: a short job per target, and the check it runs on the node (14.9, 14.10)
  cli.py              `ci-run`
bin/                  ci-run, ci-lock, ci-slot: CLIs over the package; ci-slurm-gate, a cluster
                      key's forced command (14.5)
hooks/                job-started.sh, job-completed.sh -> python -I -m ci_runners.hook
setup/                plan.sh, apply.sh, install.sh, register.sh, uninstall.sh
setup/units/          user unit templates: ci-runner@.service, ci-reaper.{service,timer},
                      ci-orgsync.{service,timer}, ci-envrefresh.{service,timer},
                      ci-hostload.{service,timer}, ci-hooks.service, slices;
                      ci-tunnel.service (a host's forward to the egress, 11.10, 12.3)
setup/egress/         ci-egress.service and allow: the egress host's proxy unit and its
                      allowlist (11.10), installed by root there
setup/inventory.example.toml  setup/policy.org.example.toml   placeholders only
setup/slurm/          site.example.toml: a Slurm site config, placeholders only (14.6)
setup/workstation/    orgsync-push.sh and ci-orgsync-push.{service,timer}: the snapshot
                      push from the maintainer's workstation (decision 11.9)
actions/              claim/, part/, salvage/, verdict/ (composite, action.yml);
                      lib/ci-run.sh, the launcher; RELEASE, the release stamp
.github/workflows/    test.yml (this repository's tests, hosted only)
tests/                unit tests; fixtures/ (sanitised): event payloads,
                      nvidia-smi CSV, /proc/stat, /proc/locks, boot ids, JUnit; mutants.py
tests/test_no_inventory.py     no tracked file holds a denied token (hashed deny list)
```

The deny list holds SHA-256 hashes of the inventory's tokens, so the list does not
publish what it protects. `setup/plan.sh` regenerates it from a host's inventory.

### 3.2 Hosts

Every host has the layout below, rooted at its `data` directory (inventory): on 3090b and
3090a a directory of the CI account (decision 12.2); on h100, while it ran (12.17), our
subtree of the shared account (decision 11.3).

```
<our subtree>/ci-runners/releases/<tag>/   installed host layer, read-only
<our subtree>/ci-runners/hooks/            the hooks (exec the release `releases/hook` names)
<our subtree>/ci-runners/etc/              inventory, policy, org.json, projects/; mode 700
<our subtree>/ci-runners/etc/tunnel/       the tunnel's key, ssh_config, known_hosts; mode 700
<our subtree>/ci-runners/state/            part records parts/<repo>/<run>-<attempt>/<id>-<n>/
                                           {record.json, status.json, samples.csv, log};
                                           leases; the slot queue; setup.json; drain.json
                                           (13.1); hostload/ (the load record, 13.2);
                                           every.json (the nightly part's hold, 13.6)
<our subtree>/ci-runners/logs/             hook and reaper logs, kept 14 days
<our subtree>/ci-runners/envs/<project>/   the projects' conda envs (decision 11.4)
<our subtree>/actions-runner/<name>/       one runner each: the app, .env, credentials
<our subtree>/ci-work/<name>/              its work directory
~/.config/systemd/user/ci-*                the units: the one place outside our subtree
/tmp/ci-runners-hooks/                     the hooks' copies the runners run, in the CI
                                           account's private /tmp (3090 hosts, 13.5)
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
`cpus`, `python`, `orgsync` and the `tunnel` table, optional. A pool is a count or a table of `count`, `labels`, `class` and `per_card`
(§5.1). `v1.4` adds, all optional: `compile_cpus` (the CPUs of `compile` parts),
`default_project` (the project of a slot ticket without one), `files.host_lock` (the
host lock; read again, decision 12.9), `local_accounts` (the local accounts that share
the lock and slot files, 12.9) and `push_key` (the public key of the org snapshot's
push, an `ssh-ed25519` line, 12.4). A host with `files.host_lock` has the host lock,
and one without it has none. `v1.5` adds, both optional: `peer_labels` (a table from a
runner class to the labels of the other host's pool of that class, 13.3) and `busy` (a
table of `window_minutes`, `cpu_cores`, `memory_gib`, `gpu_mib` and `lock_minutes`, each
optional with the defaults of 13.2); and removes `watchdog_log` and `watchdog_notice`
(13.4), which no host sets. The first path's keys (`ssh`, `trusted`, `remote`,
`cpu_slowdown`, `proxy`) are accepted and ignored in `v1.3` and `v1.4` (11.12). `setup/inventory.example
.toml` holds the schema with placeholder values.

## 4. The consumer interface

A consumer calls the actions `chaobrain/ci-runners/actions/<name>@v1` in its own
workflow (§4.8, decision 12.12; the reusable workflow `h100-test.yml` was retired by 12.17). Each action calls the host layer of its own
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
| `gpus` | 0 | number of cards to lease; 0 is a CPU part. What each class accepts is in §4.8: 1 on a per-card runner (`gpu`), 2 on `multi` (decision 12.6), any of the host's cards on `nightly` (it leases them all) |
| `gpu-mem-mib` | registry | free MiB a card needs |
| `memory` | registry | the part's memory cap, at most the registry's maximum |
| `timeout-minutes` | required | inner bound, below the job's `timeout-minutes`; at most 240, or 600 on `nightly` (decision 12.8) |
| `lock` | `x` on `compile`, else `none` | on a host with the host lock (§7): hold it for the whole command, `s`, `x` or `l`. With `none` the command takes it itself, per step, as brainspike's `local_ci.sh` does. Refused (`unavailable`) on a host without the lock. |
| `queue-timeout-minutes` | 60 | longest wait for a slot or a lease, then `unavailable` |
| `results` | none | glob of JUnit XML that the command writes under `CI_RESULTS_DIR` |
| `resume` | false | sets `CI_RESUME=1`, for a command that can resume |
| `attempt-of` | none | the record of the attempt this one retries; a record of another host is taken by its name (decision 13.3) |
| `target` | none | `slurm` only, required there (decision 14.2): a target of the site config, or several in the order to try, separated by commas or spaces; `<site>:<target>` where the host has more than one site config |

A CPU part (`gpus: 0`) takes one of the host's CI slots; a GPU part takes its card's
lease, which is its slot (§7).

Before it takes anything, the part passes its gate, in this order: low disk; its `gpus`
and `lock` against its class's row (§4.8); the drain (decision 13.1); busy (13.2 and
13.6, a first attempt only). The health gate (§5.5) comes before the leases. Once it holds its
slot and leases, it reads the drain again before the command. A refusal at any of these
ends it `unavailable` and releases what it took.

The command's environment holds:

- `CI_RUNNERS_HOST` (the role name), `CI_PROJECT`, `CI_PART_DIR` and `CI_RESULTS_DIR`;
- `CUDA_VISIBLE_DEVICES` (empty for a CPU part) and `CI_CPUS` (the inventory's `cpus`,
  or `compile_cpus` on `compile`);
- the slot path, marked as held; on a host with the host lock, its path, and with `lock`
  set, the protocol's nesting variable at the mode held (decision 12.9);
- `CI_RESUME`;
- the runner's `https_proxy`, `http_proxy` and `no_proxy` (the host's egress, 11.10, 12.3);
- the project's registry variables.

The command runs in `systemd-run --user --scope --slice=ci-<project>.slice
-p MemoryMax=<memory> -p MemorySwapMax=0`, plus `-p OOMPolicy=continue` on systemd 253 or
later, at nice 10 and ionice 7 (nice 0 on `compile`), with OOM score +500, pinned to
`CI_CPUS`. A `multi` part runs the P2P self-test in the scope first (decision 12.6).
Where the user manager has no memory controller (decision 11.11; neither 3090 host), the
supervisor enforces `memory` itself. In the scope a supervisor runs the command, ends it at the
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
| `retry-on` | label array for the retry: a cool card's `<host>-gpu<M>`, the other host's pool of the class for a drained or busy part (decision 13.3), else the pool's own labels or the registry's `fallback` |
| `resume` | whether the retry should resume |
| `host`, `gpus`, `queue-seconds`, `run-seconds` | for the summary |
| `record` | `<host role>:<owner>/<repo>/<run>-<run attempt>/<id>-<attempt>`, the part record below the host's `state/parts/` |
| `summary` | path to a Markdown block: host, cards, peak temperature per card, waits, events |

The step exits 1 only for `red` (D6). It also uploads these outputs as `outcome.json`, in
an artifact named `ci-part-<id>-<attempt>`, which the verdict reads. From `v1.5.2` that
file also holds `run-attempt`, the run attempt the part ran in (its record's, or
`GITHUB_RUN_ATTEMPT` where the part has no record, as when the release is not installed);
it is no step output.

### 4.4 Outcomes and retry hints

Precedence, highest first: stopped, reboot, thermal, oom, unavailable, red, pass. A
nonzero exit with an OOM kill is `oom`, not `red`.

| outcome | detected by | `next` |
|---|---|---|
| pass | exit 0 | none |
| red | nonzero exit or the inner timeout, with no infrastructure cause | none |
| oom | the scope's `memory.events` `oom_kill` > 0, or the supervisor's own cap (decision 11.11) | retry once with `resume` |
| thermal | our own early stop (§5.5) | retry once on a cool card |
| reboot | boot id differs from the one recorded at start | retry once on the same host |
| unavailable | before the command: the health gate, no card fits, the queue timeout, the project's queue full, low disk, `gpus` or `lock` the class does not accept (§4.8), an unknown inventory schema, a malformed registry entry | fallback: once, on a cool card or the same pool |
| unavailable (`drained`, `busy`) | the drain (decision 13.1), or a busy host on a first attempt (13.2), or a `nightly` part's hold of the host on a first attempt (13.6) | fallback: once, on the other host's pool of the class (13.3); `none` for a drained part of a class with no such pool |
| unavailable (`p2p`) | the P2P self-test of a `multi` part failed, errored or ran over its bound (decision 12.6) | none: a host fault, which the verdict reports as error |
| lost | the job ended without outputs: the runner stopped (`systemctl show` timestamps after the part's start), or the tunnel dropped the job's connection | the verdict salvages the record (§4.5) |
| stopped | cancelled (concurrency group, TERM) | none |

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

For each part whose artifacts stop short at an attempt, and whose newest record of this
run (`<run id>-<run attempt>`) on this host is that attempt, it uploads `outcome.json`
as `ci-part-<id>-<attempt>`, with `salvaged: true`. A part it does not find in this
run's records here is left alone. Only this run's records are read: every run's part has
its own record (`<repo>/<run>/<id>-<attempt>`), so a part of the same id in another run,
older or newer, changes nothing (`v1.5.1`; until then "newest" was read across runs, and
a later run's part of the same id on the host left the lost part `lost`). A composite
action cannot loop, so `salvage` uploads at most 16 parts per host and fails beyond that
(read at the stamp, 2026-11-07). Salvage jobs run before the verdict, which then reads every artifact as usual.

### 4.6 *Reusable workflow `gpu.yml`* (retired by 11.12)

Public repositories do not use the self-hosted runners.

### 4.7 *Reusable workflow `h100-test.yml`* (retired by 12.17)

Its only caller was the canary of RUN-5. Its jobs, inputs and rules are in the design at
`v1.4.1`; a consumer composes the actions (§4.8).

### 4.8 Composing the actions (decision 12.12)

A private repository may call `claim`, `part`, `salvage` and `verdict` from its own
workflow, with its own jobs between them (a plan job, a matrix of parts over several
hosts, a coverage job). These rules are the consumer's:

1. **The guard on every job** (decision 2): `pull_request` only with
   `head.repo.full_name == github.repository`, `push`, `workflow_dispatch`, `schedule`.
2. **Labels.** A job names `self-hosted` and one host's pool label, never a bare class
   name. These are the labels a consumer may target:

   | class | labels | `part` runs | `gpus` | `lock` | slot | `timeout-minutes` |
   |---|---|---|---|---|---|---|
   | `light` | `<host>-light` | short commands (a plan, a summary); `claim`, `salvage`, `verdict` | 0 | `none` | none | ≤ 30 |
   | `part` | `<host>-cpu` | CPU parts | 0 | any | one | ≤ 240 |
   | `gpu` | `<host>-gpu`, `<host>-gpu<N>` | one-card parts on the runner's card | 1 | any | the card's lease | ≤ 240 |
   | `multi` | `<host>-multi` (3090 hosts) | two-card parts, after the P2P self-test | 2 | any | both cards' leases | ≤ 240 |
   | `compile` | `<host>-compile` (3090b), opt-in (12.14) | exclusive timing parts on `compile_cpus` | 0 | `x` | one | ≤ 240 |
   | `nightly` | `<host>-nightly` (3090a), opt-in (12.14) | the consumer's nightly | 0 to all cards | any | every slot and card lease | ≤ 600 |
   | `slurm` | `<host>-slurm` and a shared label (from RUN-9), opt-in (14.2) | one batch job on a Slurm cluster's `target` (14.3) | up to the target's `max_cards` | `none` | none on its host | ≤ the target's `max_minutes` |

   `part` refuses a `gpus` or `lock` value outside its class's row (`unavailable`). A
   `nightly` part holds every lease, whatever `gpus` says; its `gpu-mem-mib` is checked
   only on the cards its command gets (decision 12.8).
3. **One `part` per job**, with the job's `timeout-minutes` at least the part's plus 15.
   A matrix leg's outputs do not reach later jobs one by one, so later jobs read the
   `ci-part-<id>-<attempt>` artifacts. Part ids are unique within a run.
4. **Retries.** A part whose `next` is `retry` or `fallback` runs again at most once, with
   `attempt-of` its first record and `resume` as the hint says, on the labels `retry-on`
   names or on a pool the consumer chooses (another host's, for a `fallback`). A retry on
   another host names the first host's record in `attempt-of` (decision 13.3).
5. **The verdict** runs last, on the light pool of one host that ran parts, after a
   `salvage` job on every other host that did (§4.5), unless the run was cancelled (a
   newer head's run posts instead). It reads the parts' `ci-part-<id>-<attempt>`
   artifacts, and a part's last attempt decides: any `red` sets the context to failure;
   all `pass`, success; else error. A run's artifacts outlive its GitHub re-runs, so an
   attempt made in an earlier run attempt than the part's first attempt is not read
   (`v1.5.2`, issue #64): the run attempt is the outcome's `run-attempt`, else the one its
   `record` names. It merges the parts' summaries into the job summary and fails unless
   the state is success.
6. **Permissions:** `contents: read`, plus `statuses: write` for claim and verdict. A job
   that reads another run's artifacts adds `actions: read`.

The consumer's own jobs (`light` commands, a coverage job as a `part`) run in its env
like any part. Its test selection, its summary format and its merge gate stay its own
(§1).

### 4.9 The env spec: `conda`, `variant` and refresh (decisions 12.10, 12.11, 14.9)

`.github/ci-runners.toml` (schema 1, consumers §1) takes an optional `conda` table:

```toml
[conda]
channels = ["conda-forge"]                   # in order; the build passes --override-channels
packages = ["nest-simulator=3.10"]           # match specs: no options, URLs or paths
```

and an optional `variant` table, read only by `ci-run slurm env` (14.9):

```toml
[variant.cuda12]                             # a cluster target's env variant
install = ["jax[cuda12]"]                    # replaces the base's install
[variant.cuda12.conda]                       # optional: joins the base's conda table
packages = ["cuda-nvcc=12.2"]                # added to the base's packages, one solve
```

A target of a variant the spec names gets an env of its own, built as the base with the
variant's `install`; a variant the spec does not name uses the base env. A variant's
optional `conda` table takes `packages` and `channels`, checked as the base's: its
packages are added to the base's and solved with them in the same `conda create`, and its
channels (none by default) follow the base's. It names no package or channel the base
names; such a spec, or an unknown key in a variant, is a spec error. A 3090 host's
`ci-run env build` reads the base only: a spec with a variant table builds there exactly
as without it, so a package that changes per variant (jax's CUDA plugin, a CUDA compiler
of the variant's version) goes in the variant, not in a requirement file or the base.

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
class (`CI_RUNNER_CLASS`) decides what the hook and the executor allow (§6.3). h100's
pools went with it (12.17), and its `h100` class with 13.4.

| host | pool (class) | instances | labels | runs |
|---|---|---|---|---|
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
labels. So the light and CPU runners carry `3090b-light` and `3090b-cpu`, not the bare
class names: a job of any repository that asks for `[self-hosted, light]` matches no
runner, and a job that asks for `3090b-cpu` never lands on 3090a (decision 12.5).
`setup` takes the labels from the inventory's pool entry; a pool may not give itself
GitHub's labels or a bare class name. The `gpu` pool is `per_card`: one runner per card,
named `<host>-gpu<N>`, labelled `<host>-gpu` and `<host>-gpu<N>`, with
`CI_RUNNER_CARD=<N>` in its `.env`; a pool entry names its class where it is not the
pool's key (`class = "gpu"`). The inventory's `peer_labels` names the other host's pool
of each class, for a drained or busy part's `retry-on` (decision 13.3). The `part`
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
first, made by the lead, then the plan. Their condition is met: brainspike's `ci_slot.sh`
writes nothing through a link in the shared queue (brainspike ceb68a77) and opens nothing
there that can block (a546b92f), both merged, and `tests/fixtures/brainspike/ci_slot.sh` is
refreshed from a546b92f (T17):

| change | detail |
|---|---|
| the CI account (root) | a new account with no password and no sudo, linger on, its `data` directory on the data disk (decision 12.2) |
| shared lock and slot files (root) | brainspike's host lock (with its `.shared`, `.turnstile` and, from `v1.6.0`, `.probe` files), its slot files and its ticket queue directory become root-owned, with ACL entries (and default ACL entries on the queue) giving the CI account and each of the inventory's `local_accounts` `rw`; a `tmpfiles.d` file (`/etc/tmpfiles.d/ci-runners-shared.conf`) recreates them at boot and excludes them from age cleaning. Their paths do not change (decision 12.9). Root-owned, because brainspike's scripts open them with `O_CREAT` (`exec N>>file`): `fs.protected_regular` lets any account do that on a file in sticky `/tmp` that `/tmp`'s owner, root, owns, and refuses it on a file another account owns. `plan.sh` prints the file's text and checks each step (the file as printed, each account present, each file root-owned, not a link, with its `rw` entries after the mask); the lead writes it as root and runs `systemd-tmpfiles --create` on it while no local job holds the files (under `host_lock.sh -x`, with `ci_slot.sh`'s queue empty). `apply` refuses until every step is done. Setup never runs as root, and nothing root runs reads the inventory. |
| the CI account's own mount namespace (root; decision 12.16) | a drop-in `/etc/systemd/system/user@<uid>.service.d/ci-runners-private.conf`, mode 644, that `plan.sh` prints from the inventory and checks as it does the `tmpfiles.d` file: `[Service]` with `PrivateTmp=yes`, `ProtectProc=invisible` (lead, 2026-11-07), `TemporaryFileSystem=/dev/shm:mode=1777 /home <the data disk root>` (the parent of `data`, never `/`) and `BindPaths=` the account's `home`, its `data` and each shared file of 12.9. `home`, `data` and the data-disk root must each be their own real path (no link), and the root a mount point, or the plan refuses. The steps, in order: the lead installs it; as the account, `systemctl --user disable --now` every runner (a restart of the user manager starts the enabled ones, and a reboot before the drop-in would start them without it); as root, `systemctl daemon-reload` and `systemctl restart user@<uid>.service`; as the account, `systemctl --user start` the first runner only, for the leak check in a live job (T18). `apply` refuses until the drop-in is in place as printed and the manager runs in the namespace it makes, read from its `/proc/<pid>/mountinfo` by the mounts the paths reach: tmpfs at `/home` and the data-disk root and over `/dev/shm`, a private `/tmp` and `/var/tmp`, `/proc` with `hidepid=invisible`, a bind at each bound path and none of a deleted file (a lock file replaced since the restart would split the lock), and no other mount in `/home`, the data-disk root or `/tmp`; `apply` then enables the runners again. A shared file missing at the manager's start fails it (fail closed: no runner comes up); the files exist from boot (`tmpfiles.d`, sysinit). **Reversal**: stop the runners, remove the drop-in, `daemon-reload`, restart the user manager. Measured on 3090b on 2026-11-07 as the CI account, in a scratch system unit with these settings and then (without the `/home` and data-disk part) in this drop-in, removed again after the test: `/tmp`, `/var/tmp` and `/dev/shm` hold no file of any other account; in the scratch unit `/home` and the data-disk root showed only the account's own, its env ran, and no directory of another account was readable to a depth of two below `/`, `/data`, `/home`, `/mnt`, `/opt` and `/srv`; `ci-lock -s` waits out (status 4 at 3 s) under the local account's `host_lock.sh -x` and passes after it, and the local `host_lock.sh -s` waited 6.1 s under our `-x`; `ci-slot` and `ci_slot.sh` both took slots; a `systemd-run --user --scope` child is in `user@<uid>.service`'s cgroup tree, in the same mount namespace, and with `MemoryMax=64M` an allocation of 300 MB was killed (status 137); both cards were visible; `ci-tunnel` runs in the namespace. |
| the push key (the account) | inventory `push_key`; the plan writes one line in the CI account's `~/.ssh/authorized_keys` (mode 600, in `~/.ssh` of mode 700): `restrict`, forced to `<data>/ci-runners/releases/hook/bin/ci-run org-sync --receive` with `CI_RUNNERS_ETC` and `CI_RUNNERS_PYTHON` set, marked `ci-runners-orgsync-push` so that a new key replaces it; every other line is kept (decision 12.4) |
| `etc/tunnel/` | as on h100, with this host's own key pair; the plan prints the egress account's line for its public key (below) |
| directories | §3.2 under `data`; `etc/` mode 700 |
| runner app | the pinned release, checked by sha256, into 9 runner directories on 3090b and 8 on 3090a (§5.1) |
| `.env` per runner | as on h100; `CI_RUNNER_CARD=<N>` on a `gpu` runner; `https_proxy` at the tunnel; from `v1.5` the two hook variables name the copies in `/tmp/ci-runners-hooks/` (decision 13.5) |
| the hooks (`v1.5`) | `hooks/job-started.sh` and `job-completed.sh` under `data` gain the line `base=<data>/ci-runners`, from which they find `releases/hook` (13.5) |
| `ci-runner@.service`, `ci-tunnel.service`, `ci-reaper.timer` | as on h100; from `v1.5` the runner's unit also has `Requires=` and `After=ci-hooks.service` and an `ExecStartPre=` that checks both hook copies are executable files (13.5) |
| `ci-hooks.service` (`v1.5`) | oneshot, `RemainAfterExit=yes`: `ci-run hooks copy` into the private `/tmp` (13.5); enabled; started before the runners, and reloaded (`ExecReload=`, the same copy) by `apply` when a hook changes: a restart would restart every runner through `Requires=` |
| `ci-hostload.{service,timer}` (`v1.5`) | every 60 s, `ci-run hostload sample`: one line of the load record (decision 13.2); `Nice=10`, bounded at 30 s |
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

### 5.4 *The h100 executor* (retired by 12.17 and 13.4)

Its steps that still hold are in §5.5; the rest (the watchdog, D7, a reboot counted as
thermal) went with 13.4. The text is in the design at `v1.4.1`.

### 5.5 The 3090 executor (decisions 12, 13)

On 3090b and 3090a the executor of §4.3 adds:

1. **Gate** (§4.3), before anything is taken:
   - **Drain** (decision 13.1): a drained host or pool ends the part `unavailable`, with
     `retry-on` the other host's pool of its class (13.3). Read again once the slot and
     leases are held.
   - **Busy** (decision 13.2): on a first attempt of a class with a peer pool, other than
     `light`, a host busy for the whole window ends the part `unavailable`, with
     `retry-on` the other host's pool; once per head and part. While a `nightly` part
     holds or waits for every slot and card (13.6), every time.
   - **Health**, before the leases: a GPU part needs as many of its cards below
     `max_temp_c` (80 °C) and read as it asks for. If not, it ends `unavailable`, and
     `retry-on` names a card of the host that passes (`<host>-gpu<M>`) where one does.
2. **Leases**: a `gpu` runner leases its own card (`CI_RUNNER_CARD`); a `multi` runner
   both cards, in ticket order (decision 12.6), then runs the P2P self-test; a `nightly`
   runner holds them all, after every CI slot, whatever its `gpus`; its command gets the
   `gpus` lowest-numbered cool cards with `gpu-mem-mib` free, and it waits while fewer
   have (decision 12.8).
3. **The host lock**: held for the whole command when `lock` says so (`x` on `compile`),
   through `ci-lock`, after the slot and the leases; released when the scope ends.
4. **Run**: in the scope of §4.3, with its cards in `CUDA_VISIBLE_DEVICES` and `taskset`
   to `CI_CPUS`: the inventory's `cpus`, or `compile_cpus` on `compile`.
5. **Sample**: every 30 s, each leased card's temperature and use, and our compute PIDs,
   into the record's `samples.csv`. If a card of ours stays at 85 °C or more for 3 min,
   the scope is stopped (`thermal`, the early stop of §12 item 7).
   There is no watchdog: `thermal` comes only from the early stop, and `reboot` from the
   boot id.
6. **Clean up**: `systemctl --user stop` of the part's scope kills its whole cgroup. The
   job-completed hook repeats this if the step was killed, and the reaper repeats it for
   a scope whose job is gone.
7. **Files**: a part, like every process of the CI account's user manager, runs in the
   account's own mount namespace (decision 12.16): its `/tmp`, `/var/tmp` and `/dev/shm`
   are the account's, shared by its parts but by no other account; `/home` and the data
   disk show only the account's own; the shared lock files and slot queue are bound in
   from the host's `/tmp`. A part's temp files are therefore also invisible to the local
   accounts. `/proc` shows only the account's own processes (`ProtectProc=invisible`).
   The hooks the runners run are copies in that `/tmp` (decision 13.5).

## 6. Security model

### 6.1 Per host

| host | runs as | what job code can reach | mitigation now | exit path |
|---|---|---|---|---|
| h100 | the shared account, runners on it (decision 11.3) | everything a co-user of the account can reach, and they can reach everything of ours: the runner credentials, the hook, the policy, each job's `GITHUB_TOKEN` while the job runs, the checkout of a private repository, the results and the statuses we post | private repositories only, of org members only (decision 11.1, 11.2); `GITHUB_TOKEN` with `contents: read`, plus `statuses: write` only in claim and verdict; per-job directories, deleted after the job; no other credential there but the tunnel key, which opens only allowlisted HTTPS (T15); `NoNewPrivileges` | a dedicated account (decision 11.7) |
| 3090b, 3090a | a dedicated CI account per host, runners on it (decision 12.2) | the CI account's files: the runner credentials, the hook, the policy, the envs, each job's `GITHUB_TOKEN` while it runs, the checkout of a private repository, the shared lock and slot files (a job can hold them and stall local runs); nothing of the local accounts (T18), no sudo, no GitHub credential | private repositories only, of org members only; `NoNewPrivileges`; the account's own mount namespace (decision 12.16); `GITHUB_TOKEN` as on h100; per-job directories; the tunnel key opens only allowlisted HTTPS; the push key only `--receive` | a root-owned host layer with system units (§5.3, T13) |
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
| T13 | (also any co-user of h100's account, an accepted gap; on the 3090 hosts the CI account's own jobs only) An accepted job (or its dependency) edits the host layer it owns: the hook (or its copy in `/tmp`, 13.5), the policy, `.env`, the units, the drain file or the load record (13.1, 13.2), so later jobs pass unchecked. | private consumers only (§5.3); nothing on the host detects an edited hook, so a changed policy hash in the hook lines shows only a changed policy; the hook writes its log without following links or blocking on FIFOs; `ci-run hooks copy` checks its `/tmp` directory through a descriptor (no link, the account's, mode 700) and then writes the copies by path, which only the account itself could race in its private `/tmp` (accepted, 13.5) |
| T14 | A co-user of h100's shared account reads the org-sync token, or edits the snapshot. | no token on h100 (decision 11.9): the snapshot arrives over the maintainer's ssh access, checked by `--receive` (§6.5). A host that syncs itself holds a fine-grained read-only token (members, metadata): it shows the member list and the private repositories' names, nothing else. An edited snapshot is T13's case. |
| T15 | A co-user of h100's shared account takes the tunnel key, or any local user of a host uses the tunnel's port on 127.0.0.1 (on the 3090 hosts, every local account can reach it). | the egress account has no shell and no listening forward, and its key opens only the egress proxy's port (decision 11.10). The proxy passes only its allowlist on port 443: HTTPS to GitHub, the Actions service's storage and the package indexes, which job code reaches through the tunnel anyway. The suffix `.blob.core.windows.net` lets them reach any Azure storage account over HTTPS, and `storage.googleapis.com` any Google Cloud Storage bucket: accepted gaps, since the Actions storage accounts have no stable names and package downloads use those buckets. They appear as the egress host's address. The allowlist is root-owned on the egress host. The lead revokes the key by deleting one line there. |
| T16 | (3090 hosts) A job, or a local user, holds the shared host lock or a slot, and stalls the other side's work; or a local account empties the ticket queue. | the files are `flock` locks, so a hold ends with its process: a part's hold ends with its scope (inner timeout, the job-completed hook, the reaper); a waiting part ends `unavailable` at its queue timeout; the files hold no data, and a lost ticket only loses a place in the queue |
| T17 | (3090 hosts) A job plants a link in the shared ticket queue (a `seq`, a ticket, a holder record, a temp name or a lock file there) so that a script of the other account writes, creates or truncates a file the link names, or blocks on a FIFO it reads. | the queue directory is not sticky and both accounts write in it (decision 12.9; lead, 2026-11-07), so no script of either account writes or creates through a link there. `slots.py` creates each file exclusively under a random name (`O_EXCL`, `O_NOFOLLOW`) and renames it over its name (a rename replaces a link, never its target). A FIFO is covered on each side: `slots.py` opens `seq.lock` and `take.lock` `O_NOFOLLOW` and `O_NONBLOCK` and uses them only if `fstat` shows a regular file, and reads `seq`, tickets and holder records the same way, so a FIFO there is refused at once; brainspike's `ci_slot.sh` (since ceb68a77) writes through exclusive temporaries renamed into place, exits 2 on a linked queue directory or a `seq.lock` that is not a regular file, and deletes a ticket that is a link or not a regular file as malformed, and (since a546b92f) opens every entry of the queue `O_NOFOLLOW` and `O_NONBLOCK` and checks it with `fstat` before it reads, locks or writes, creating `seq.lock` with `O_EXCL`. The plan checks that the queue is a root-owned directory, not a link. Residual: a job can delete or forge tickets and holder records, which disturbs the queueing only (T16), never a file of a local account; and a FIFO or a directory a job leaves at `seq` or `seq.lock` makes the other side refuse or fail until it is removed, never hang. |
| T18 | (3090 hosts) A job reads other accounts' files that are readable by everyone, or their processes' command lines in `/proc`: temp files in `/tmp`, `/var/tmp` and `/dev/shm` left with a 002 or 022 umask (copies of private code, logs, notes), and other people's directories on the data disks. Measured before any job ran (2026-11-07): tens of thousands of the local account's temp files on 3090b, other people's on 3090a, and other people's data-disk directories on both. | the CI account's user manager runs in its own mount namespace (decision 12.16, §5.3): private `/tmp` and `/var/tmp`, an empty `/dev/shm`, and `/home` and the data-disk root emptied but for the account's own `home` and `data`; only brainspike's six shared lock files and the slot queue are bound in; `/proc` shows only the account's own processes (`ProtectProc=invisible`; lead, 2026-11-07). No local account's file modes change. The runners stay stopped until the drop-in is applied on the host, and a leak check inside a live job on each host (no other account's temp or data file readable) comes before any other job. Residual: files that other accounts leave readable elsewhere on the root disk (outside `/home`, the temp directories and the data disk) stay readable, as for any local user; system directories hold no user data. |
| T19 | (the `slurm` class, 14) A job reads a cluster key from the CI account's `etc` (job code runs as that account, T13) and uses the cluster account. | the key's forced command runs only the gate: no shell, no forward, `rsync` only into `work/`; private repositories of org members only; the class is opt-in per repository; the budget ledger | `submit` runs any code as the cluster account by design, so a job can read everything that account holds on the cluster, send it out through its log or artifacts, and submit billed jobs from inside a job. Exit path: a dedicated cluster account (14.12). |
| T20 | (14) Other users of the cluster reach our files or a forward, or read the site's names in our public logs. | the account's root and every part directory mode 700; `check` and `apply` refuse a root another user could swap (its mode, a link, the directories above it); what a part shows on GitHub is redacted (14.4); a part's temporary files on a node only inside a 700 directory of its own; the build's forward is a Unix socket in a 700 directory behind a relay that accepts only the build's random credential | a node's `/tmp` and process table are shared with other users' jobs, as for any user of the site |
| T21 | (14) Runaway cost: a lost driver, a stuck job, a retry loop. | every job has `--time`; the reaper cancels a job whose GitHub job is gone; pending is bounded; one retry (§4.4); the daily and monthly caps (14.7) | job code (T19) can spend up to the site's association limits |
| T22 | (14) A node silently corrupts multi-card results (peer copies that read zeros). | the P2P self-test before every two-card part (12.6); a target's `max_cards` from its canary | – |

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
   `memory_max`, and `max_waiting` per pool (default 1). A first attempt beyond
   `max_waiting` ends `unavailable` ("project queue full") and goes elsewhere; a retry
   queues past it, counted by it (decision 13.6). So one project cannot fill a pool with
   waiters, and GitHub's queue, which is not fair across repositories, stays short.
3. **Light jobs never wait behind heavy ones**: claim and verdict run on their own
   `light` pool and take no slot.
4. **Interop with `ci_slot.sh`** (3090 hosts; decision 12.9). `ci_slot.sh`'s tickets
   carry no project, so they count as the host's `default_project` (brainspike). Its
   waiters apply no caps, so that project's cap is the slot count on those hosts.
5. **Accounting** is the part records under `state/parts/`: queue time, run time and
   outcome per part. No report command is built yet.
6. **Busy hosts and drains** (decision 13). A part does not queue on a host that another
   account has kept busy for the whole window, nor on a drained one: it goes once to the
   other host's pool of its class. A retry, or a re-run of a head already sent away, waits
   for its slot where it lands, so load moves at most once per head and part. Nor does a
   first attempt queue behind a `nightly` part that holds or waits for every slot and
   card (decision 13.6).

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
| a local run holds the host lock or a slot | parts wait | their queue timeout, then `unavailable`; an exclusive hold of `busy.lock_minutes` or more sends a first attempt to the other host (13.2) |
| another account keeps the host busy (CPU, memory, a card) | parts would compete with it | a first attempt goes to the other host's pool (13.2) |
| a host or pool drained | no new part runs there | `unavailable`, one retry on the other host's pool; `none` where there is none (13.1, 13.3) |
| the drain file unreadable or malformed | | the host counts as drained (fail closed) |
| the load timer stopped, or too few samples | busy is not known | not busy (fail open): parts wait for their slots as before |
| a hook copy in `/tmp` missing or changed | a job would start without its hook | the runner fails "Set up runner" for a missing file; the reaper rewrites the copy within 10 min; a runner does not start without both copies (13.5) |
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
| reaper | every 10 min, which also checks the hook copies (13.5) |
| load record | a sample every 60 s, bounded at 30 s; kept 2 days; busy window 10 min (inventory) |
| busy markers | 14 days |
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
  2. The stamp PR: `actions/RELEASE` set to `v1.N.0` (and a reusable workflow's action
     refs, should one return; none since 12.17).
  3. The lead tags `v1.N.0` on the stamp's merge commit.
  4. `install.sh` on every host, the egress host included; nothing switches yet.
  5. A canary consumer branch composes the actions at `@v1.N.0` on both 3090 hosts and
     passes.
  6. The lead moves `v1`. The hosts already have the release, so the order is safe.
  7. The restore PR sets the stamp back to `hook`.
  8. Rollback: move `v1` back. The old release is still installed.
- **A reusable workflow's canary (measured with `v1.2.0`, 2026-11-06).** A workflow names
  the actions it calls with a ref of their own. A canary that pins `h100-test.yml@v1.N.0`
  still runs the actions at `@v1`, so the old release's host layer, and it tests only the
  workflow file. So the release stamp also rewrites the reusable workflow's own action refs
  (`chaobrain/ci-runners/actions/<a>@v1`) to `@v1.N.0`, and the PR after the tag sets them
  back to `@v1` with `actions/RELEASE`. A test checks that the refs and the stamp agree,
  and that the docs' examples read `@v1`; since 12.17 no workflow here calls an action.
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
- **`v1.4.0` notes** (stamped 2026-11-07). Read when the contract was stamped
  (2026-11-07), each recorded where it applies:
  1. a `nightly` part holds every card lease; its `gpu-mem-mib` is checked only on the
     cards its command gets (12.8, §4.8, §5.5);
  2. a `nightly` slot ticket carries the `every` marker, which `ci_slot.sh` never reads
     (12.9);
  3. `salvage` reads the newest record across runs, and uploads at most 16 parts per host
     (§4.5);
  4. on h100, `gpus: 0` is now refused (the `h100` class row of §4.8), and `h100-test.yml`
     checks `gpus == 1`. No caller passing `gpus: 0` was found;
  5. a known limit (lead, 2026-11-07): during "Set up job", before any step, the actions
     runner itself writes the machine name and the hook's path (under `data`) into every
     job log, on every host. No runner option changes either, and a mask acts only on
     later output. The logs are visible to members of the private repositories only
     (11.1). A follow-up points the runners at a copy of the hooks in the account's
     private `/tmp` (12.16), so that the path names no disk;
  6. `v1.4.0` is tagged before h100's checks (acceptance 7): h100 is offline at the tag.
     They run before h100 installs `v1.4.0` and before `v1` moves; until then h100 and
     every `@v1` caller keep `v1.3.0` (lead, 2026-11-07).
- **`v1.4.1`** (patch, 2026-11-07; lead-approved): setup installs and enables
  `ci-envrefresh.{service,timer}` on every host (12.11; issue #49), which `v1.4.0`'s setup
  left out, so `refresh = true` refreshed nothing. A host's plan with `v1.4.1` writes these
  two units and adds the timer to `enable --now`; on a 3090 host the service reads
  `etc/envrefresh.env`. Nothing else changes; `v1` stays at `v1.3.0` until h100's checks
  (notes item 6), which then expect these two writes on h100. Decision 12.17 drops
  item 6 and h100's checks: `v1` moves to `v1.4.1` after the 3090 canary.
- **`v1.5.0` is additive** (decision 13): the drain, the busy rule and the cross-host
  `attempt-of` add outcomes' reasons and accept a retry `v1.4` refused; no input or
  output a consumer passes or reads changes. The deleted `h100` class and watchdog keys
  are used by no runner and no host's inventory (12.17). So it is a minor release.
- **`v1.5.0` notes** (stamped 2026-11-08; `v1` moved to it after the 3090 canary at `@v1.5.0`, 2026-11-08):
  1. the drain and the busy rule live in `part`, so they hold for a caller of
     `@v1.5.0` or later; until `v1` moves, `@v1` callers' parts ignore them. The load
     timer and the hook copies are the host's (`releases/hook`) from the apply on;
  2. each host's plan adds `ci-hooks.service` and `ci-hostload.{service,timer}`, changes
     `ci-runner@.service`, every runner's `.env` (the hook paths) and the two hooks (the
     `base` line), and restarts the runners once: the lead applies it while no part runs
     on the host (drained, `ci-run drain status` showing no scope), keeping the restart
     short. The plan restarts no runner: each is restarted once after the apply (a runner
     reads its `.env` and unit only at its start); until then it runs the hooks under
     `data`, which work there too;
  3. a consumer whose parts can be drained or found busy needs a retry job on
     `retry-on` (§4.8 rule 4) to benefit; without one, such a part ends as error in the
     verdict;
  4. an inventory that sets `watchdog_log` or `watchdog_notice` is refused by `v1.5.0`;
     none does;
  5. a behaviour change on the 3090 path: a retry whose `attempt-of` names another
     host's record runs (as attempt `n + 1`) instead of being refused with exit 2
     (13.3);
  6. the inventory gains two optional host keys, `peer_labels` (13.3) and `busy` (13.2).
     Without `peer_labels` for a class, a drained part of it ends `next` `none` and the
     busy rule is never checked for it. `v1.4`'s loader refuses an unknown key, so every
     `claim`, `part`, `salvage` and `verdict` of a `v1.4` caller fails on a host whose
     inventory has either: a host's inventory gains them only once no caller pins `v1.4`
     (`v1` moved, the pinned consumers bumped). Until then `v1.5.0` runs with neither,
     its drain refusing to `next` `none` and its busy rule off. Known limit: a host whose
     inventory sets `peer_labels` (or `busy`) refuses every action older than `v1.5.0`;
     consumers pin `@v1` (§4.8), never a release tag. Moving the two keys to a file
     `v1.4` does not read would add a second host file, its loader and its lint for a
     gap that closes once no consumer pins a tag, so it stays a note (coordinator,
     2026-11-08);
  7. `ci-run drain on`, or any `--pool` change, refuses on an unreadable drain state;
     only `drain off` without a pool rewrites it (and undrains the whole host).
- **`v1.5.1`** (patch, stamped 2026-11-08; lead-approved as a patch of `v1.5`; `v1` moved to it after the 3090 canary at `@v1.5.1`, 2026-11-08): `salvage`
  reads only this run's records of a part (§4.5; issue #61). `v1.5.0` took the part id's
  newest record across the repository's runs, so a later run's part of the same id on the
  host left a lost part `lost`. No input, output, setup code or hook changes: the install
  moves `releases/hook`, a host's plan adds nothing, and no runner restarts. `v1` moves to
  `v1.5.1` after the 3090 canary.
- **`v1.5.2`** (patch, stamped 2026-11-08; lead-approved as a patch of `v1.5`; `v1` moved to it after the 3090 canary at `@v1.5.2`, 2026-11-08): after a
  GitHub re-run, the verdict no longer reads a part's attempt that an earlier run attempt
  left behind (§4.8 rule 5; issue #64), and `outcome.json` gains `run-attempt` (§4.3).
  No step input or output, setup code or hook changes: the install moves `releases/hook`,
  a host's plan adds nothing, and no runner restarts. A `v1.5.1` artifact without
  `run-attempt` is read by its `record`. `v1` moves to `v1.5.2` after the 3090 canary.
- **`v1.6.0`** (minor, stamped 2026-11-08; lead-approved, decision 13.6 and issue #69; `v1` moved to it after the 3090 canary at `@v1.6.0`, 2026-11-08; both 3090 hosts applied, drained):
  1. a first attempt no longer waits behind a `nightly` part: it goes to the other host's
     pool at once (`busy: nightly holds every slot and card`); a retry queues past the
     project cap (13.6). No input or output a consumer passes or reads changes; the new
     reason is an `unavailable` like 13.2's, so a consumer with a retry job on `retry-on`
     (§4.8 rule 4) needs no change. It holds for a caller of `@v1.6.0` or later; the hold
     record is written by a `nightly` part of `v1.6.0` or later, so until `v1` moves and
     the nightly runs on it, first attempts wait as in `v1.5`;
  2. `ci-lock`'s light probes take turns under the probe file (12.9, issue #69), and each
     3090 host's plan adds that file to the `tmpfiles.d` file and the private namespace's
     `BindPaths=` (12.16): root steps, then the user manager's restart, which restarts
     every runner, so it is applied drained (`ci-run drain on`, no scope left). Until
     a host's apply, `ci-lock` finds no probe file in the account's `/tmp` and probes
     without turns, as in `v1.5`.
  3. also in the tag, unused by any host path: decision 14's text and the Slurm site
     config's loader (RUN-9a, RUN-9b); no action, setup step or hook reads them yet.
- **`v1.7.0`** (minor, stamped 2026-11-09; decision 14, RUN-9b to RUN-9h; `v1` moves to it after the slurm canary at `@v1.7.0`):
  1. **The `slurm` class.** A Slurm cluster becomes a remote target of the 3090 hosts'
     runners (decision 14); a repository opts in through `policy.toml`'s `classes`
     (14.2). A part on a `<host>-slurm` / `3090-slurm` runner runs as one `sbatch` job
     through the account's gate. `part` gains the input `target`: a target of the site
     config, or a list in the order to try. Its outcomes are §4.4's, plus the `slurm`
     block in the record and the cost in the summary. No existing input or output
     changes; a consumer that does not name the class is untouched.
  2. **`ci-run slurm check|plan|apply|env|canary|status|uninstall`** (14.9), on a driver
     host.
  3. **The env spec's optional `[variant.<name>]` table** (§4.9), read by `ci-run slurm
     env` only; a 3090 host's `env build` reads the base, as before. A host running an
     older release refuses the key, so a consumer names a variant only after its hosts
     run `v1.7.0`.
  4. **What a cluster part shows on GitHub is redacted** (14.4; RUN-9h): the job log, the
     gate's and ssh's errors, the reasons, the summary line (target, job, cost only) and
     the artifacts name none of the site's root, account, user, hosts, partitions or
     nodes, nor our site and account aliases. The gate gains the verb `nodes` (`sinfo`'s
     node lists); without them a part ends `unavailable` before any job. `check` and
     `apply` refuse a root that another user could swap. The files a part fetches back
     are the consumer's, and are not redacted (consumers §7).
  5. **Setup:** no new unit. The site config, the cluster key and the gate's ssh alias
     are host files the maintainer writes; a host's plan adds the `slurm` pool's runners
     where its inventory gives the pool.
- **`v1.7.1`** (patch, stamped 2026-11-09; a patch of `v1.7`): a cluster env build runs
  with an empty `HOME` of its own and no pip config file, and a part's job reads neither
  the account's `~/.local` packages (`PYTHONNOUSERSITE=1`) nor its pip config: the
  account's own conda and pip settings (a package mirror the egress does not allow)
  reach neither (14.9). No input or output changes.
- **`v1.7.2`** (patch, stamped 2026-11-09; a patch of `v1.7`): a slurm part's driver
  scope gets the user manager's bus (`XDG_RUNTIME_DIR`, `DBUS_SESSION_BUS_ADDRESS`; at
  `v1.7.1` every slurm part ended `lost` before its driver ran); one cluster env build
  at a time per root, and a `conda create` that exits 0 yet leaves no python fails the
  build; `ci-run slurm status` lists only held targets. No input or output changes.
- **`v1.7.3`** (patch, stamped 2026-11-09; a patch of `v1.7`; `v1` moved to it after the slurm canary at `@v1.7.3`, 2026-11-09: a CPU part, a one-card a40 part, and a part cancelled while its job ran, which ended `stopped` with no job left): two slurm parts of one
  run may make the run's work directory on the cluster at once (at `v1.7.2` the second
  ended `unavailable` before its job). No input or output changes.
- **`v1.7.4`** (patch, stamped 2026-11-09; a patch of `v1.7`): an env spec's
  `[variant.<name>]` may carry a `conda` table whose packages join the base's in the
  same solve (14.9, §4.9); a host on `v1.7.3` or older refuses that key, so a consumer
  adds it only after its hosts run `v1.7.4`. `ci-run slurm env` run from another data
  directory (a maintainer's temporary one) keeps the record of the variants it did not
  build, from what the login host's links serve. No input or output changes.
- **`v1.7.5`** (patch, stamped 2026-11-09; a patch of `v1.7`; `v1` moved to it after the slurm canary at `@v1.7.5` through the second driver host, 2026-11-09: a CPU part and a one-card a40 part): the ssh master's socket on
  a driver host is named by a short hash of the account's alias, not ssh's `%C`, so a data
  root of up to about 75 characters fits a Unix socket path (at `v1.7.4` a driver whose
  data root passed about 45 could not reach its gate); a longer one ends the part
  `unavailable` with that reason. No input or output changes.
- **Inventory schema.** `schema = N` in `inventory.toml`. The executor refuses an
  unknown schema (`unavailable`) rather than guess.

## 10. Phases

Each phase is one or more PRs of at most about 1,500 lines, each carrying its evidence.

**RUN-0, this spec** (2026-11-05). **RUN-1, the 3090b runners and the generic executor**
(`v1.0.0`, 2026-11-05), served brainspike until 11.12. **RUN-2** (the ssh driver to
h100), **RUN-3** (3090a) and **RUN-4** (the public GPU path) are retired by 11.12, never
built. Their acceptance lists are in the design at `v1.2.0`. RUN-7 (decision 12) brings 3090b and 3090a back under
the RUN-5 model, not RUN-1's or RUN-3's.

**RUN-5: the standard test CI on h100** (decision 11; `v1.1.0` and `v1.2.0`, live from
2026-11-06 until 12.17 retired h100 on 2026-11-07). Sub-PRs: the org snapshot and policy schema 2; h100 as a local host in setup
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
6. A canary private repository composes the actions (§4.8): a plan job, parts on both
   3090 hosts (h100 retired, 12.17), a salvage job per host and the verdict. A failing CPU, GPU, two-card and
   compile test each end red in their own part only; a stopped runner on a host other
   than the verdict's is salvaged by that host's salvage job; an OOM-killed part ends
   `oom` and is retried.
7. *h100's plan and the `h100-test.yml` canary.* Retired by 12.17.

*Measured (2026-11-07, `main` 4551806 on both 3090 hosts; h100 offline and unreachable
from them, so its items are not tested yet):*

1. The suite and the mutant table pass (679 of 679 mutants killed at the RUN-7g head).
2. Interop:
   - `ci-lock -s` in a job waited for the local `host_lock.sh -x` (19 s on each host);
   - the local `host_lock.sh -s` waited for the job's `ci-lock -x` (184 s on 3090b,
     33 s on 3090a);
   - a job's `ci-slot` waited 51 s while the local account held both slot files, then
     ran.
3. Both hosts' plans are applied, and the runners are online at org level (9 on 3090b,
   8 on 3090a).
   - They came back 171 s after `systemctl --user restart` and 69 s after a tunnel
     restart.
   - After a reboot: not tested, as none happened. The lock files' ACLs after a reboot:
     not tested.
4. In a live job on each host:
   - `sudo -n true` fails (`NoNewPrivs` 1);
   - `/tmp`, `/var/tmp` and `/dev/shm` hold no entry of another account;
   - `/home` and the data-disk root show only the account's own, and none of another
     account's entries is readable;
   - `ps` shows one account, and pid 1 is hidden (12.16).

   Refusals and placement:
   - An unlisted repository's `compile` job on 3090b, and its `nightly` job on 3090a,
     were refused (`runner-class`) 0.16 s after the job's first runner line.
   - A non-member's job: not tested, as no non-member account is at hand.
   - Four `3090b-cpu` jobs all ran on 3090b.
   - "Print no inventory" holds for the hook's line. The runner itself writes the
     machine name and the hook's path (under `data`) into every job log, on every host,
     h100 included: a known limit of `v1.4.0` (§9, notes item 5).
5. The P2P self-test passed on both hosts, outside a job and in each two-card part.
   A two-card part ran end to end on each host.
6. The canary repository composes the four actions at `main` across both 3090 hosts.
   - A green head passed.
   - On a red head, the CPU, GPU, two-card (both hosts) and compile parts each ended red
     in their own part, and the verdict failed.
   - An OOM-killed part ended `oom` and passed on its retry (`resume`).
   - On another head, the runner of a part on 3090a was stopped mid-part. Its part
     finished in its own scope, and 3090a's salvage job uploaded it (`salvaged`) for the
     verdict on 3090b.
   - Parts on h100: not tested (retired by 12.17).
7. Retired with h100 (12.17), never tested: h100's plan with `v1.4.0` and the
   `h100-test.yml` canary.

**RUN-8: operating two shared hosts** (decision 13; `v1.5.0`). Sub-PRs, each with its
tests and evidence:

- RUN-8a: this contract (decision 13);
- RUN-8b: the h100 clean-up (13.4);
- RUN-8c: the drain switch, `peer_labels` and the retry across hosts (13.1, 13.3);
- RUN-8d: the load record, its timer and the busy rule (13.2);
- RUN-8e: the hooks' copy in the private `/tmp` (13.5);
- RUN-8f: the stamp and `v1.5.0`, the plans applied on both 3090 hosts, the canary, the
  move of `v1`, and the restore.

*Acceptance:*

1. Unit tests, each failing on a mutant:
   - **drain**: `on`, `off` and `status`, for the host and for a pool by key or class; a
     drained host refuses a part of every class, a drained pool only its own; a part
     that waited for its slot while the drain came on is refused before its command and
     releases its slot and leases; a malformed state file drains; the state is read
     afresh by every part (a restart keeps it); a running part's scope is not touched;
     `claim`, `salvage` and `verdict` do not read it;
   - **where it goes**: a drained or busy part's `retry-on` is `self-hosted` and the
     peer pool's labels, with `next` `fallback`; `none` on its last attempt and for a
     drained part of a class with no peer pool;
   - **retry across hosts**: an `attempt-of` of another host gives the attempt from the
     record's name; one naming another part id is refused; a record of this host is read
     as before;
   - **busy**: each of the four rules from fixtures (`/proc/stat`, `/proc/meminfo`, the
     account's `cpu.stat` and `memory.stat`, `nvidia-smi` CSV, `/proc/locks`); a window
     is busy only when every sample is; fewer than half the window's samples is not
     busy; a PID the account cannot see is foreign, one it can see is its own; no check
     on a retry, for `light`, or for a class without a peer pool; a second first attempt
     of the same head and part on the host is not refused; the defaults of 13.2; the
     sampler writes one line and the reaper deletes old days and markers;
   - **clean-up**: an inventory with `watchdog_log` or `watchdog_notice` is refused; the
     policy and the executor refuse the `h100` class; only h100-only tests and mutants
     are deleted, and the tests of the 3090 path keep their assertions (those that used
     h100's example inventory as a fixture use a 3090 host's instead);
   - **hooks**: the copy refuses a linked directory and another account's, writes mode
     500 copies by rename, and the reaper rewrites a missing or changed one; a copy runs
     the release its `base` line names; setup plans `ci-hooks.service`, the runner
     unit's `Requires=`, `After=` and `ExecStartPre=` and the `.env` hook paths only on
     a host with the private namespace (12.16), and the hooks under `data` elsewhere.
2. On each 3090 host, the plan with `v1.5.0` shows only decision 13's changes (the new
   units, the runner unit, the `.env` hook paths, the hooks' `base` line); it is applied
   while no part runs there, and the runners are back online within 5 min.
3. Live, on the canary at `@v1.5.0` (the release PR's body):
   - with 3090b's `part` pool drained, a canary CPU part there ends `unavailable`
     (`drained: pool part`) with `retry-on` `3090a-cpu`, and its retry passes on 3090a;
     `drain status` shows the pool, and the drain holds across a runner restart; `drain
     off` clears it;
   - with a local `host_lock.sh -x` hold on 3090b older than `busy.lock_minutes` (set
     to 2 for the check, then removed), a canary CPU part's first attempt ends `busy`
     with `retry-on` `3090a-cpu`, and its retry runs on 3090a;
   - the load record on both hosts has a line a minute.
4. Live: every job log's hook line names `/tmp/ci-runners-hooks/`; on a drained host
   with a hook copy removed, a canary light job fails in "Set up runner", and the copy is
   back within 10 min (the reaper) or at once (`systemctl --user restart ci-hooks`).
5. The canary passes green and ends red in its own parts on `@v1.5.0` on both hosts, as
   RUN-7 acceptance 6; then `v1` moves to `v1.5.0`.

**RUN-9: a Slurm cluster as a remote target** (decision 14). Sub-PRs, each with its tests
and evidence, none with a site fact:

- RUN-9a: this contract (decision 14) and the guides;
- RUN-9b: the site config: schema, loader, lint, the example with placeholders, the deny
  list's site tokens;
- RUN-9c: the gate and the batch template; the cluster supervisor;
- RUN-9d: the executor's `slurm` path (14.3, 14.4), the record's `slurm` block, the
  reaper's orphan cancel;
- RUN-9e: `ci-run slurm check|plan|apply|env|canary|status|uninstall` and the build relay;
- RUN-9f: a minor release; the `slurm` pools registered by the lead.

*Acceptance:*

1. Unit tests, each failing on a mutant: the site config refuses an unknown key, a target
   without a partition, a GPU target without a price, `max_cards` above `cards_per_node`
   and an account naming an unknown target; the gate refuses a request outside the
   account's targets, caps or minutes, writes only under `work/`, and renders `--time`,
   `--export=NONE` and the site's GPU syntax with no flag from the request verbatim; every
   row of 14.4's table; a pending job cancelled at its bound and the next target tried; a
   budget refusal; TERM cancels the job; the reaper cancels an orphan.
2. Live on one account of the first site, with the lead's approved plan: `check` passes;
   `env` builds the consumer's env with no listener left after it; `canary` passes on
   each target to be enabled, its cost recorded.
3. Live at the release: one CPU part and one one-card part of the canary repository pass
   on the cluster, and a cancelled one ends `stopped` with its job gone; then `v1` moves.

## 11. brainspike on top of this (decision 12)

brainspike composes the actions in its own `ci.yml` (§4.8), on both 3090 hosts. Its
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
| a verdict over both hosts | a `salvage` job per host, then `verdict` (§4.5) |
| overflow parts and GPU-architecture coverage on a Slurm cluster | the `slurm` class (decision 14) |

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
