# Consumer guide (sketch)

How a chaobrain project runs its CI parts on the shared runners. The contract is
[design.md](design.md). Nothing here is implemented before RUN-1. Inputs, labels and
outcomes follow the design's §4 and §5.1.

## 1. What a project provides

| item | where | notes |
|---|---|---|
| **Registry entry** | on the hosts, `etc/projects/<p>.toml`, added by the lead | name, repository, class (private or public), status context, env name per host, caps (`max_slots`, `max_cards`, `memory`, `memory_max`, `max_waiting`), `gpu-mem-mib`, `remote_env`, fallback order per part class, retries (0 or 1), the upstream update command |
| **Env spec** | `.github/ci-runners.toml` in the project, read **from the default branch only** | conda env name, Python version, requirement files, editable upstream checkouts and their ref policy (for example `origin/main`), variables to set. A pull request cannot change a host's env. |
| **Envs** | built on each host it uses by `ci-run env build <p> --host <role>`, run by a maintainer | on h100, the manifest is built in the same step (design §5.4) |
| **Commands** | in the project | the contract below |
| **Workflow** | `.github/workflows/` in the project | §3 (private) or §4 (public) |
| **Status context** | one per head, `<project>/ci` | `claim` reads it; the project's verdict job posts it |

**Command contract.** A part's command:

- runs from the job's checkout, and exits nonzero only for a red result;
- handles TERM by stopping its own process tree;
- takes the host lock per step (`$CI_HOST_LOCK`, or `ci-lock -s|-x|-l`), holding a
  shared lock for at most about 15 min. Otherwise the part declares `lock`;
- reads `CUDA_VISIBLE_DEVICES` and `CI_CPUS`, and does not choose its own cards;
- writes JUnit XML into `$CI_RESULTS_DIR` (required on h100, where verify compares it);
- honours `CI_RESUME=1`, if it can resume;
- runs in a tree without `.git` if it is to run on h100 (snapshot by `git archive`).

## 2. Onboarding

1. **Ask the lead.** The lead adds the repository, its class and its actors to the
   hosts' policy and registry, through `setup/plan.sh` and `apply.sh`. A public
   repository also gets its own repository-level GPU runners (design §12 item 2).
2. **Add the env spec** to the project's default branch.
3. **Build the envs** on each host the project uses. For h100, build the env and its
   manifest from 3090b.
4. **Write the workflow** (§3 or §4), pinning `@v1`.
5. **Canary.** A branch with one failing test in each part class ends red in that part,
   and only there. A stopped runner and an OOM end as `lost` and `oom`, not red.
6. **Done** when 5 heads have run end to end and their statuses and summaries read
   right. Queue and run times are on the host's `ci-run report`.

## 3. A private consumer's workflow (sketch)

```yaml
name: CI
on:
  pull_request: {types: [opened, synchronize, reopened, ready_for_review]}
  workflow_dispatch: {inputs: {sha: {required: true}}}
concurrency:
  group: ci-${{ github.event.pull_request.number || inputs.sha }}
  cancel-in-progress: true
permissions: {contents: read}
jobs:
  claim:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    runs-on: [self-hosted, light]
    permissions: {statuses: write}
    outputs: {run: "${{ steps.c.outputs.run }}"}
    steps:
      - id: c
        uses: chaobrain/ci-runners/actions/claim@v1
        with: {context: myproj/ci}
  plan:                      # the project's own selection, then placement
    needs: claim
    if: needs.claim.outputs.run == 'true'
    runs-on: [self-hosted, light]
    outputs: {plan: "${{ steps.place.outputs.plan }}"}
    steps:
      - uses: actions/checkout@v4
      - id: sel
        run: python scripts/select_parts.py > "$RUNNER_TEMP/parts.json"   # project code
      - id: place
        uses: chaobrain/ci-runners/actions/place@v1
        with: {parts-file: "${{ runner.temp }}/parts.json"}
  part:
    needs: plan
    strategy: {fail-fast: false, matrix: {part: "${{ fromJSON(needs.plan.outputs.plan) }}"}}
    runs-on: ${{ matrix.part.runs-on }}
    timeout-minutes: ${{ matrix.part.timeout-minutes }}
    steps:
      - uses: actions/checkout@v4
      - uses: chaobrain/ci-runners/actions/part@v1
        with:
          project: myproj
          id: ${{ matrix.part.id }}
          command: scripts/run_part.sh ${{ matrix.part.id }}
          timeout-minutes: 25     # inner bound: below the plan's, which is at least 30
          # a GPU part also sets gpus; the plan names only id, runs-on, fallback, timeout-minutes
          results: "*.xml"
  # fallback: reads the ci-part-* artifacts and reruns each part whose `next` is retry
  # or fallback, once, on its `retry-on`. verdict (always, on light): salvage the parts
  # without an artifact, merge the summaries, post myproj/ci.
```

## 3a. The standard test CI on h100 (RUN-5)

For any private chaobrain repository (design decision 11, §4.7). The hook already
accepts every private repository of the org and every org member (design §6.5), so no
allowlist edit is needed.

**Onboarding.**

1. Add `.github/ci-runners.toml` to the default branch (below).
2. Ask a maintainer to add the registry entry (`etc/projects/<p>.toml` on h100, design
   §4) and to run `ci-run env build <p> [--conda PATH] [--dry-run]`. It reads that file
   and the requirement files it names (with their `-r` and `-c` files) at the default
   branch's head, builds the env into `envs/<p>-<stamp>/`, links `envs/<p>` to it only
   once every step has passed, and keeps the two builds before it. It prints the
   registry lines to add: `env` and the spec's `variables`. Rerun it after a dependency
   change has merged. The maintainer runs it with their own `GH_TOKEN` (Contents: read on
   the repository and on any private upstream) set for that one command; no such token
   is ever stored on h100. conda and pip reach the internet through h100's egress, so
   the shell takes a runner's proxy lines for the build: `https_proxy`, `http_proxy` and
   `no_proxy` from `<data>/actions-runner/<runner>/.env`. Without `no_proxy`, the hosts
   in the inventory's `tunnel.direct` (a package mirror among them) go to the egress
   proxy, which refuses them with 403.
3. Add the workflow below, pinning `@v1`.
4. Canary and done as in §2, steps 5 and 6.

**`.github/ci-runners.toml`** (schema 1):

```toml
schema = 1
python = "3.12"                                   # conda python version of the env
requirements = ["requirements-dev.txt"]           # pip -r, in order; paths inside the repository
install = ["pytest-xdist"]                        # more requirement specifiers: no options, URLs or paths
[[upstream]]                                      # optional: editable sibling checkouts
repository = "chaobrain/brainstate"
ref = "origin/main"
[variables]                                       # for the registry entry (registry RESERVED names refused)
XLA_PYTHON_CLIENT_PREALLOCATE = "false"
```

The project itself is not installed into the env: an editable install would point at a
build's temporary checkout. A part runs from the job's checkout, so `python -m pytest`
imports the project from there. Upstreams are cloned into the build and installed
editable, at the branch head when the env was built.

**Workflow.**

```yaml
name: test
on:
  pull_request: {}
  push: {branches: [main]}
  workflow_dispatch: {}
concurrency:
  group: test-${{ github.event.pull_request.number || github.sha }}
  cancel-in-progress: true
permissions: {contents: read}
jobs:
  test:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    uses: chaobrain/ci-runners/.github/workflows/h100-test.yml@v1
    permissions: {contents: read, statuses: write}
    with:
      project: myproj
      cpu-command: python -m pytest -m "not gpu" --junitxml="$CI_RESULTS_DIR/cpu.xml"
      gpu-command: python -m pytest -m gpu --junitxml="$CI_RESULTS_DIR/gpu.xml"
      results: "*.xml"
      timeout-minutes: 60
```

The command contract of §1 holds; the GPU part gets one card in `CUDA_VISIBLE_DEVICES`.
The `myproj/ci` status is the verdict. A run by someone outside the org fails in "Set up
runner" with `ci-runners: refused actor`.

**What the shared account means for you.** h100's runners run on an account that other
lab users share (design §6.1). They can read your checkout while a job runs and could
alter a result. Don't send secrets to these jobs, and keep a repository whose code must
not be seen off this CI.

## 4. A public consumer's workflow (sketch)

CPU jobs stay on `ubuntu-latest`. Only the GPU part comes to our runners, through the
reusable workflow, and only for same-repository pull requests, pushes and dispatches.
A `claim` on a GitHub-hosted runner runs the action's own checkout with the image's
`/usr/bin/python3`, which must be 3.11 or later (`ubuntu-24.04`, today's `ubuntu-latest`).

```yaml
on:
  push: {branches: [main]}
  pull_request: {}
  workflow_dispatch: {}
jobs:
  cpu:
    runs-on: ubuntu-latest
    steps: [...]
  gpu:
    if: github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository
    uses: chaobrain/ci-runners/.github/workflows/gpu.yml@v1
    permissions: {contents: read, statuses: write}
    with:
      project: brainevent
      command: python -m pytest -m gpu --junitxml="$CI_RESULTS_DIR/gpu.xml"
      context: brainevent/gpu
      results: "*.xml"
      timeout-minutes: 60
```

A member who is not on the actor allowlist sees `gpu` fail in "Set up runner" with
`ci-runners: refused actor`. Fork pull requests skip `gpu`, by the `if:` and by the hook.

## 5. brainspike, the first consumer

brainspike's contract is its `docs/specs/arch/ci.md`. The project keeps everything about
what to test:

- `ci_select.py plan` and the profile choice (`affected` or `merge -L`);
- the `local_ci.sh` stages and its summary format;
- the verdict, the merge gate, the sweeper, the audit and the nightly logic.

ci-runners runs it.

### 5.1 Registry entry (values without inventory)

| key | value |
|---|---|
| repository, class, context | `chaobrain/brainspike`, private, `brainspike/ci` (shadow phase: `brainspike/ci-shadow`) |
| env | `brainspike-dev` on 3090b, 3090a and h100 |
| caps | 3090b and 3090a: `max_slots` 2 (all slots); h100: `max_cards` 3; `max_waiting` 1 per pool |
| memory | `memory` 56G and `memory_max` 80G on the 3090 hosts (today's `REMOTE_CI_MEMORY` and slice); 128G on h100 |
| GPU | `gpu-mem-mib` 6000 (S2, L64); `max_parts` 1 per card (two workers inside it) |
| `remote_env` | `BRAINSPIKE_HOST`, `XLA_PYTHON_CLIENT_PREALLOCATE`, `CUDA_DEVICE_ORDER` |
| upstream | saiunit, brainevent, brainstate, braintools, braintrace at `origin/main`; update command `scripts/nightly_host.sh --update`, under the project's env lock (today's `pr.lock`) |
| lock variables | `LOCAL_CI_HOST_LOCK` and `LOCAL_CI_SLOT_LOCK` set to the host's lock and slot files, which are today's files (design D5) |
| fallback order | as ci.md §4.1: `gpu` h100, then 3090a, then 3090b; `cpu`, `gates`, `sharding`, `nest`, `inventory` and `cov` 3090a, then 3090b; `compile` 3090b only, with no fallback |
| reservation | 3090a: the nightly holds it after a 1 h grace (today's yield protocol) |
| retries | 1 |

### 5.2 Jobs of `ci.yml`

| ci.md job | runs on | ci-runners piece | brainspike code |
|---|---|---|---|
| claim | `[light]` | `claim` (`wait-seconds: 60`, `local-lapse-minutes: 45`) | none |
| checks | `[light]` | `part` (`slot: none`, `lock: l`) | `local_ci.sh light` |
| plan | `[light]` | `place`, fed by the plan's parts | `ci_select.py plan`; split into parts; upload the plan |
| part | `place`'s `runs-on`: `[part, 3090a]`, `[part, 3090b]`, `[compile, 3090b]`, `[h100-gpu<N>]` | `part` (`resume` on retry; `results` for `gpu`) | `ci_job.py`: download the plan, then `local_ci.sh -O <part> [-R]` |
| fallback | `retry-on` | `part` again, `attempt-of` the first record | as part |
| verify | `[part, 3090b]` or `[part, 3090a]`, GPU | `part` + `compare` | as part, on the same `gpu` commands |
| verdict | `[light]`, always | `salvage` for parts without an artifact | the summary block, `brainspike/ci`, the `high-risk` label |
| merge gate, sweeper, audit | `[light]` | none | `merge_gate.py` |
| nightly | `[nightly, 3090a]` | `part` (`slot: full`, 600 min), reservation owner | `nightly_host.sh` |

### 5.3 What brainspike changes, and when

- **CI-1a, after RUN-1 (3090b) and RUN-3 (3090a).**
  - `ci.yml` pins `@v1`, and `ci_job.py` shrinks to the command wrapper.
  - `scripts/runner/` is not written, since the hook and host notes live here.
  - `local_ci.sh` gains its plan option, as CI-1a already plans.
- **CI-1b, with RUN-2.**
  - The `gpu` stage writes JUnit and runs from an archive tree.
  - The in-run chunk rotation becomes per-part card choice.
- **ci.md edits**, listed in design §11, go in one lead-approved spec PR in brainspike
  before CI-1a's code.
