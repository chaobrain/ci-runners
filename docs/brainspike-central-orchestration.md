# Central consumer orchestration: approved additive contract

Status: implementation approved by the lead on 2026-11-25. Keep deployed commit `0a4fa2b`
unchanged. This proposal completes the consumer migration after the approved
GPU contract; it does not replace its resource, P2P, source or cleanup guards.

## Evidence and the boundary of the completed canary

The two-node canaries establish real pair computation, the native P2P integrity
check, live NEST simulation, genuine failure without a second execution, active
cancellation, independent SSH reads and complete cleanup. Their contexts are
independent of production. They use four cores, 8 GiB and two workers.

Consumer graph inspection identifies three remaining contract gaps:

1. BrainSpike's existing manual PR dispatch and Main replay can explicitly test
   another commit. `GitHub.source()` currently requires a non-PR source run's
   `head_sha` to equal the tested SHA. The actual rejected dispatch
   [45590776574](https://github.com/chaobrain/brainspike/actions/runs/45590776574)
   confirms that restriction. Removing the comparison globally would destroy
   source provenance; omitting replay would remove an existing consumer feature.
2. Legacy compile commands use the reference affinity and run with nice 0.
   `parallel.task_execution.supervise()` currently builds normal commands with
   nice 10. Full CPU-group reservation does not by itself reproduce priority,
   the existing host lock, or the reference-machine requirement.
3. A native successful command without JUnit can complete its execution batch
   successfully; an independent reader then rejects the missing report. The
   [live probe](https://github.com/chaobrain/brainspike/actions/runs/45591668333)
   proves why the execution status must not be the production final status.
   Dependabot job tokens cannot be assumed to have status-write permission, so
   final publication must stay with the controller's existing credential.
   GitHub documents the default read-only Dependabot token and the settings or
   permission changes needed to grant more access in
   [Dependabot on Actions](https://docs.github.com/en/code-security/reference/supply-chain-security/troubleshoot-dependabot/dependabot-on-actions).

These observations are not failures of model or coverage tests. CPU/RAM budget
changes and GPU Slurm site binding remain separate private deployment reviews.

## 1. Explicit source-linked dispatch and replay

Add optional workflow input `test-source`, a bounded JSON object forwarded as
`source.test_source`. Omission means exactly the current `run-head` contract.
The existing `source.sha` remains the tested commit, never the workflow's commit.
The controller additionally records authoritative `run_head_sha` from the
Actions run response. Do not claim that this API field independently proves
the compiled workflow's `GITHUB_WORKFLOW_SHA`.

The only new selectors are:

- `{"kind":"pull-request","number":N}`: workflow_dispatch only. The controller
  reads that PR, verifies its repository IDs and full names against the source
  repository, and requires its current head to equal the requested test SHA.
  Store the PR number and resolved head; use a PR concurrency key. A later retry
  must not silently replace the selected SHA with a newer PR head.
- `{"kind":"commit-replay"}`: workflow_dispatch only. Resolve the exact full
  tested SHA as a commit through the source repository API. An optional full
  `range_base` is immutable source data and must be an ancestor of the tested
  commit. Consumer first-parent range and explicit-base rules still apply; a
  failed range check produces no shortened plan.

Selectors have exact keys and JSON types. Reject unknown kinds/keys, booleans in
integer positions, abbreviated SHAs and selectors on push/schedule/PR events.
They are not raw refs, URLs, repository overrides or arbitrary Git arguments.

For either override, both the original and triggering actors must resolve through
GitHub to humans with write/admin permission on the same private repository.
Do not accept a dispatch solely because its caller supplied an actor name or a
PR author. Normal private same-repository Dependabot pull_request admission stays
unchanged; this proposal adds no bot-dispatch exception or hook allowlist entry.
Fork/public dispatches and foreign repository IDs remain refused.

Every plan, allocation, result, report receipt and commit status retains the
tested SHA and source run/attempt. Preserve actual run-head provenance separately
through allocation metadata and read-side comparison. Source event, actor,
repository and cancellation checks still come from GitHub. Existing callers
omitting the selector continue to receive the current strict equality behavior.

## 2. Explicit reference timing isolation

Add optional task `isolation`, default `shared`; its only new value is `timing`.
Timing requires a logical `timing_target`, resolves to one controller-owned
device profile, and cannot be GPU or Slurm work. Ordinary callers cannot supply
CPU numbers, lock paths, nice values, accounts or a replacement profile.

The profile is disabled until separately reviewed on the host. It pins the
logical device, reference CPU identity, required affinity, registered CPU-group
count and host-lock identity. These values stay in private deployment input.
The task must request that device's entire CPU-group budget, explicit original
workers and unchanged RAM; it cannot occupy a smaller generic CPU allocation.

Reserve all groups and RAM atomically. Recheck the current complete profile at
reservation, claim and launch, including a digest in the signed grant. A resized,
rebound or revoked profile cannot leave an old grant eligible. Existing CPU/GPU
allocations must finish before timing reserves; while it holds all groups no
other central CPU/GPU allocation on the device can start. Do not change the
other devices' scheduling behavior.

The executor must preserve nice 0 and take the inventory-owned exclusive host
lock for the timing command. Verify the required affinity is available and the
reference identity matches before starting; do not replace the original mask
with a convenient different mask. Supply a genuine inherited lock hold to the
existing consumer lock protocol, whose shared/exclusive commands must recognize
the same live ancestor and avoid reacquiring their own lock. Existing quiet-load
checks, benchmark commands and compile/cache gates remain unchanged.

Before enabling a profile, account for non-central work and control runners on
that host. An unmanaged workload outside the old lock protocol, or an inherited
nice value that cannot be restored to zero, is a failed preflight, not evidence
of equivalent timing. Capture the measured affinity, priority and lock receipt.

Use allocation schema 5 and executor capability 5 for this local timing envelope;
older executors must never receive it. CPU, single-card, pair and GPU Slurm grant
schemas 1 through 4 remain unchanged. Cleanup releases lock, groups and RAM only
after verified process-scope termination, on pass, failure, timeout, cancellation
and restart reconciliation.

## 3. Report-gated primary publication

Add optional workflow input `report-gate`, default false. On activation, the
controller-owned repository-ID/context policy requires it for the migrated
production contexts; a request cannot disable a required gate. Existing contexts
and callers not opted in keep their current behavior.

Maintain separate execution, report and publication states. Execution success is
not publication success. Expose them explicitly in batch reads, and keep the
primary status pending until the gate completes. A genuine test failure stays a
failure even when report verification or projection also fails; retain the
separate report error. A cancelled/superseded/incomplete source never becomes a
successful skipped test.

The combined consumer workflow orders the existing native plan and tasks, a
read-only `verify-reports/<batch_id>` job, then its verdict job. Add CLI operation
`execution-result` for the report job's dependency without waiting for final
publication. The gated `batch-result` returns publication state; ungated behavior
is unchanged. This ordering avoids a verdict/report dependency deadlock.

The read job independently fetches the authoritative native plan/allocation
references and checks the original complete selection, commands, resource
requests, reports, dependency provenance and retry outcomes. It projects the
verified original report layout and seals publication. It then uploads a bounded
JSON report receipt through the existing SSH store and calls `central report`
with its immutable reference. No status-write credential reaches that job or any
project command.

The receipt uses the existing `project` bundle kind in the controller-configured
consumer compatibility namespace. Its reserved name is derived from batch ID;
do not add a new storage kind that older compatibility readers would mistake for
a foreign namespace. Keep existing retention classes and cutover/history rules.
Exercise coexistence with native plan/allocation objects and old artifact-name
patterns before activation.

The controller must independently validate all of the following, rather than
trusting a `verified: true` flag:

- The namespace/store binding, repository ID/name, tested SHA, run, attempt,
  batch, controller-issued plan reference and the entire allocation/attempt set.
- Exact references already accepted by the controller, complete grants and
  terminated scopes/runners. Genuine red evidence cannot be replaced by a later
  green allocation or omitted from the receipt.
- Required native report files and every required projected bundle/seal. Match
  projection payload names and content digests to the corresponding original
  native report trees, excluding only defined compatibility metadata. No copied
  different-SHA report, empty rows or missing original outcome can satisfy it.
- The reporting job's identity in this source attempt: an unambiguous expected
  job name, its actual runner and GitHub job ID. Record the submission while it
  is running, then require authoritative successful job completion before posting
  success. Requiring completion before accepting the submission would deadlock
  the submitting job.

Add optional task `required_files` for bounded relative manifest-path patterns
and positive minimum byte counts; default is empty for existing callers. Match
against verified bundle manifest entries, never arbitrary host filesystem paths.
Consumer plans declare required JUnit, nonempty rows, selected coverage and
architecture files without deleting optional impact-map behavior. The report
job also verifies the retained original selection; the controller need not
import BrainSpike's selector implementation.

Persist accepted report references, reporting-job identity and publication state
durably, separately from allocation release. Duplicate identical submissions are
idempotent; conflicting ones fail closed. A stopped/missing/failed report job or
source completion without its required receipt terminates publication as error
after bounded reconciliation. Keep an existing genuine test failure red.
Re-running failed jobs alone must not mix attempt 1 objects into attempt 2;
re-run the full workflow to create the new source attempt.

Only the controller posts the primary commit status using its existing private
credential. Read jobs and Dependabot test jobs need contents/actions/PR read
permissions only. Posting a primary success before read-side publication, or
relying on a Dependabot token to repair the status afterwards, is not acceptable.

## Consumer work that does not need new shared authority

The consumer can prepare authenticated history, labels, map/report reads and
Main range data in a separate read-only control step, using the workflow revision
of its helper. The native plan command continues to receive neither GH_TOKEN nor
GITHUB_TOKEN. Missing maps remain conservative, missing required history/range
remains fatal, and absent/unreadable timing labels never enable extra privilege.

The schema converter must preserve the original entire plan and exact command
strings, report paths, time/queue limits, 56-GiB default requests and explicit
54-GiB sharding requests. Each part has an explicit reviewed CPU/worker/device
request. A hash-bound original-selection witness lets the read job reject a
truncated matrix or changed command/resources. Until timing is implemented, a
plan containing timing is refused in full, never submitted as a supported subset.

Allocation-local PR dependency isolation remains required by the approved GPU
addendum: test the checkout's actual dependency changes, preserve NEST and the
selected CUDA variant, publish installed distributions plus pip check and source
provenance, and exercise a real admitted Dependabot event. A main-branch cached
environment, or a CPU-only CI-tool probe, is not that acceptance. This proposal
does not permit forwarding credentials into dependency installation.

## Acceptance and deployment order

Implement the approved contract with stdlib fixtures and focused safety mutants:

1. Unchanged strict run-head sources; authorized PR dispatch and historical
   replay; wrong/fork/moved PR head, read-only/bot override actor, false run head,
   bad/short SHA, wrong source attempt and non-ancestor base refusal.
2. Entire-device timing exclusion against concurrent CPU and GPU allocations;
   required affinity, nice 0, live lock inheritance, changed profile and old
   executor refusal; pass/red/timeout/cancel/restart cleanup.
3. Successful command with missing/empty/wrong-source reports stays non-green;
   complete success publishes only after successful reporting-job completion;
   genuine failure with bad delivery stays red; forged job/ref/grant, omitted
   attempt, conflicting duplicate, attempt mixing and lost reporter fail closed.
4. Existing ungated CPU/GPU/Slurm consumers and schemas 1 through 4 retain their
   behavior. No private inventory or credential appears in repository fixtures.

Then prepare exact private controller/executor/profile deployment diffs, drain
and database backups, compatibility and rollback procedures, budget review and
host timing preflight. Changes to services, settings, runner labels, releases,
budgets and Slurm bindings are not approved by this document or by the previous
two-node GPU canary authorization. No production context is enabled until the
complete consumer graph and formal PR/Main/Nightly/cluster acceptance pass.

## Implementation handover (2026-11-25)

The candidate now implements the three approved sections. `source_selection.py`
keeps strict run-head matching as the default and resolves explicit selectors
only through authoritative same-repository GitHub records and human write/admin
permissions. Source provenance accompanies each native allocation result.

`timing.py` pins the private reference affinity, nice zero and live exclusive
lock. The controller reserves the complete device budget; reservation, claim and
scope launch independently reject stale/revoked profiles. The executor advertises
capability 5. A private timing profile also renders the matching inventory lock;
it cannot silently bind a partial CPU budget or multiple lock identities.

`reporting.py` compares immutable native and compatibility manifests, required
files, producer metadata and seals. Compatibility project identities retain the
source run's head SHA; the verified producer metadata separately binds the exact
tested checkout SHA. Dispatch/replay therefore preserves the existing history
reader's namespace without conflating the two revisions. The reporting job
submits while running; only its authoritative successful completion can release
a pending primary success. Cancellation withdraws that success; superseding a
pending report closes it without overwriting completed historical results.

Optional host-owned `source-selectors.json`, `report-gates.json` and
`timing-profiles.json` inputs render absolute policy references. Source/report
policy is controller-only; timing policy is also executor-local. Their contents
are hashed into the reviewed deployment input. No actual policy, device, account
or endpoint inventory is committed here, and no profile is enabled by omission.

The scheduler database advances to schema 5 after a complete drain. Existing
ungated history remains ungated. Deployment must back up the drained database
and units, install matching candidate controller/executors and workflow/action
revisions, verify compatibility, then opt in only the reviewed BrainSpike
contexts. The reusable template's release references still require the normal
matching-release procedure; new inputs must not be used with an older action or
controller. No release tag or service entry was changed for this implementation.
Do not restore an old database backup after unrelated consumers have resumed.

Native Linux validation uses private exports on the existing second canary
account, the shared host lock, a 2-GiB memory scope and two-CPU quota:

- `python -m unittest discover -s parallel/tests -t . -p 'test_*.py' -q`:
  318 passed.
- `python -m unittest discover -s tests -t . -p 'test_*.py' -q`:
  1,448 tests: suite passed, one skipped. This suite expects umask 022 for its temporary
  setup-mode fixture; the export and its parent remain private.
- `python parallel/tests/orchestration_mutants.py`: all 49 source/timing/report guards,
  with each new behavioral test mapped to a killing mutation in that file.
- `python parallel/tests/multi_gpu_mutants.py`: all 18 existing GPU/Slurm safety
  mutations killed after updating their exact source targets for capability 5.
- BrainSpike's converter, native receipt bridge, compatibility adapter and
  original golden-plan reader: 115 passed, no skips, against this source export;
  all 10 consumer conversion/report faults killed.
- All 3,033 registered mutation snippets match the current stamped source.
  The existing schema-upgrade mutation also kills its original v1-history test
  through the production runner after updating its schema-5 source target.

These are implementation/codec/state-machine checks. Physical reference-machine
comparison, deployed selector/report acceptance, allocation-local PR dependency
isolation, the complete consumer graph, original RAM capacity and site-bound GPU
Slurm/model acceptance remain outstanding. The running `0a4fa2b` canary remains
unchanged; this handover is not production activation or full CI acceptance.


### Mixed-executor compatibility correction (2026-11-26)

The approved source/report features require a matching executor collector as well
as matching controller, client and workflow revisions. Reserve CPU tasks carrying
`report_gate` or resolved `source_selection` only on executor capability 5 or
newer supported implementations. This also keeps their explicit RAM requests away
from the older core-derived RAM codec. The existing timing and GPU capability
checks remain independent; ungated, unmodified sources retain their original
allocation behavior. This corrects the missing reservation-side check for the
already approved opt-in contracts and adds no action/workflow input or grant
schema. It does not authorize deploying a new revision or upgrading another host.

The real canary exposed the gap when an unrestricted CPU request selected an older
executor. Its workflow failed before the test step; recovery verified no local
execution record/physical leases, preserved the original grant, wrote an unstarted
cancellation tombstone through the existing ledger interface and delivered the
normal authenticated release event. The consumer canary is also explicitly pinned
to the reviewed local device. Tests must prove both new opt-ins wait at capability
4 and reserve at 5, while the existing legacy CPU contract continues to reserve.


Correction validation on the existing second native node, from a private source
export under the host lock, 2-GiB scope and two-CPU quota: scheduler stdlib suite
321 passed; base stdlib suite 1,448 tests passed with the same one skipped fixture;
53 orchestration mutants and all 18 GPU/Slurm mutants were killed. Commands are
`python -m unittest discover -s parallel/tests -t . -p 'test_*.py' -q`,
`python -m unittest discover -s tests -t . -p 'test_*.py' -q`,
`python parallel/tests/orchestration_mutants.py`, and
`python parallel/tests/multi_gpu_mutants.py`. The new tests and exact killing
mutations are listed in the orchestration mutant table. The base suite uses umask
022 inside the private export. This correction has not replaced the deployed
`5fe30b9` runtime; its installation still requires the reviewed deployment process.
