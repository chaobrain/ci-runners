# Sandbox prototype: confining a GPU job on a host without root

Experimental. Nothing in the repository installs or runs these files, and only
`tests/test_sandbox.py` imports them (for their id-map and identity arithmetic); no
runner, hook or workflow uses them. They answer one question: can a job run on a host
where we have neither root nor a dedicated account, confined so that it cannot reach
anything of the account it runs under, and can a check prove it?

- `sandbox.py` — the launcher. Standard-library Python plus `newuidmap` / `newgidmap`
  (package `uidmap`) and `setpriv` (util-linux). Linux, x86-64.
- `check.py` — the conformance check: creates its own markers, runs the items inside a
  sandbox variant (or with no sandbox, the control), prints expected against observed.

## The launcher

```
sandbox.py run --tree DIR [--card N] [--relay HOST:PORT] [--policy DIR] [--env DIR]
               [--map subuid|single] [--groups gpu|kept|none] [--setenv K=V]... -- CMD...
sandbox.py probe [--card N]  # exit 0 when this account can build the sandbox here, 125 and why
                             # when not; then which group modes it can build
```

The job runs in new user, mount, pid, ipc, network and uts namespaces:

| inside | what |
|---|---|
| `/job` | `--tree`, read-write; `HOME=/job/home` |
| `/usr`, `/bin`, `/lib*` | the host's, read-only, nosuid |
| `/etc` | generated, read-only: `passwd`/`group` with `root` and `job` only (and `gpu`, the mapped GPU group, under `--groups gpu`), `hosts`, `ld.so.cache`, the CA store |
| `/opt/env` | `--env` (a Python environment), read-only |
| `/ci/policy` | `--policy`, read-only (the stand-in for the hook and its policy) |
| `/proc` | a fresh procfs of the new pid namespace; `/proc/sys` read-only |
| `/sys` | a fresh sysfs, read-only (the NVIDIA driver reads `/sys/module/nvidia`) |
| `/dev` | `null`, `zero`, `full`, `random`, `urandom`; with `--card N`: `nvidiaN`, `nvidiactl`, `nvidia-uvm`, `nvidia-uvm-tools`; a private `/dev/shm` |
| `/tmp`, `/run` | fresh tmpfs |
| network | loopback only; `127.0.0.1:3128` relays to `--relay` (the host's forwarded proxy port), and `https_proxy` points at it |

The account's home, other users' files, the host's processes, its loopback services and
every other card's node do not exist inside.

- **Ids.** `--map subuid` (default) maps the account's uid and primary gid to 0 inside and
  1..65536 to the subordinate range; the job runs as 1000, so its kernel uid and gid are
  the range's first id + 999, not the account's. `--map single` maps only the account's own
  ids (the job runs as the account; for comparison).
- **The card's group: three modes.** On the shared-account host the card nodes and the
  control node are mode 0660 with a GPU group, which the account is in as a supplementary
  group (the UVM nodes are open to everyone). The launcher finds the GPU group from the
  leased card's node (the group that owns `/dev/nvidiaN`; no name or gid is configured).
  - `--groups gpu` (the design's mode (a), the default with `--card`): the GPU group alone.
    `newgidmap` maps only the subordinate ranges in `/etc/subgid` and the caller's passwd
    primary gid (it refuses a caller whose real gid is another, so `sg`/`newgrp` does not
    help), so the account needs a second line, `<account>:<gpu gid>:1`, which the host's
    administrator adds. The launcher reads every line of the account (by name or uid): the
    first one of more than 1000 ids is the subordinate range, mapped at 1..n (n ≤ 65536)
    for both the uid and the gid; a line covering the GPU group's gid is required, and that
    gid is mapped at n + 1, outside the range, 0 (the account) and the job's 1000. The job's
    supplementary groups are then set to n + 1 alone (`setpriv --groups`; `setgroups` is
    allowed after `newgidmap`), so its kernel groups are exactly the GPU group, and
    `/etc/group` inside names it `gpu`. Without the line the launcher refuses with the
    exact line to add and exits 125.
  - `--groups kept` (mode (b)): the job keeps every supplementary group the launcher
    inherited (`setpriv --keep-groups`; they show as `nogroup` inside, the account's
    primary group as `root`). The card works, and the mount namespace is the only barrier
    to the account's group-readable files.
  - `--groups none`: no supplementary group; the card cannot be opened.

  `--map single` maps only the account's own ids, with `setgroups` denied, so it keeps the
  groups (`kept` only). `probe` builds an empty sandbox in each mode the card allows and
  says which ones this account can use here.
- **Privileges.** The job's capability bounding, inheritable and ambient sets are empty,
  no-new-privileges is set, every mount is nosuid, and `user.max_*_namespaces` of the
  sandbox's user namespace are 0, so the job cannot create a nested namespace (and so
  cannot get capabilities back to mount anything).
- **GPU.** The user-space driver is told `ModifyDeviceFiles: 0` (a read-only bind over
  `/proc/driver/nvidia/params`, as libnvidia-container does) and `/proc/driver/nvidia/gpus`
  lists only the leased card; without the first, CUDA fails with error 304 and NVML with
  "GPU access blocked by the operating system". `--card` is the device minor (the N of
  `/dev/nvidiaN`), which need not be nvidia-smi's or CUDA's index.
- **Lifetime.** pid 1 inside is the launcher's init; when the job exits it gives the tree
  back to the account (`chown` to the account's ids) and exits, which kills whatever the job
  left behind.

## The check

```
check.py suite --variant control|subuid-gpu|subuid-kept|subuid-none|single --scratch DIR --env DIR \
               [--card N] [--proxy-port P] [--out FILE]
check.py cost  --scratch DIR --env DIR --card N [--repeat 3] [--out FILE]
```

The variants: `subuid-gpu`, `subuid-kept` and `subuid-none` are `--map subuid` with
`--groups gpu`, `kept` and `none`; `single` is `--map single` (groups kept); `control` runs
the items with no sandbox. The suite makes a run directory under `--scratch` with the job's
tree, the policy stand-in (holding a copy of `check.py`, which runs from there read-only),
a marker file outside the tree and a group marker (F5); it starts a marker process (which allows any same-uid tracer), a marker TCP
listener on the host's loopback and, with `--card`, a marker CUDA process of ours holding
64 MiB on the card. It runs the items, reads the job's kernel ids from outside, removes
everything and prints a table.

| item | a confined job observes |
|---|---|
| I1 | the design's check 8: read outside, the job's kernel uid and gid lie in the account's subordinate ranges (and are not the account's), and its kernel groups are exactly the GPU group (no group under `--groups none` or without `--card`); any other group fails it, so `subuid-kept` fails it by design. Recorded: ids and group names inside, the kernel ids, groups and id maps outside |
| F1–F4 | the marker file is not visible; creating a file in the policy stand-in fails; the account's home does not exist; nothing outside the tree (and the per-job `/tmp`) is writable |
| F5 | a file outside the tree, mode 0640, of one of the account's groups other than the GPU group, is not visible; and the job's kernel credentials could not open it |
| P1–P3 | `/proc` lists only the sandbox's processes, not the marker; signal 0 to the marker fails; `PTRACE_SEIZE` of it fails |
| N1–N3 | the marker listener cannot be reached; the proxy port cannot be reached directly; through the relay an allowlisted URL answers and another is refused by the proxy |
| K1–K3 | a new user namespace, `mount` (also after trying one) and `mknod` of a device node fail |
| G1–G4 | the leased card works (jax); no other card node exists; what nvidia-smi shows; whether a fresh allocation reads the pattern a previous process left |
| R1 | (information) whether a process of the account outside can trace the job |

F5 shows the two barriers separately. "Not visible" is the mount namespace: the file's
directory does not exist inside, whatever the job's credentials. "Could not open" is the
credentials alone, judged outside from the job's kernel ids in `/proc/PID/status` against
the file's owner, group and mode bits: the job is neither its owner nor in its group,
and it is not world-readable. That second barrier is what mode (a) adds: under `kept` the
job's kernel groups hold the file's group, so a file of the account that the mount
namespace exposed by mistake would be open to it; under `gpu` it would not.

The control runs the same items with no sandbox and must report "not confined", which
shows the items can tell the difference. Two cannot: an unprivileged account can create
no device node with or without a sandbox (K3), and on a host that restricts unprivileged
user namespaces the control cannot mount either (K2).

## Measured

On the shared-account host (one idle card, named by its device minor; the account has
the subordinate range and the GPU group's single-gid line in `/etc/subgid`), each
variant once, the same check file for all:

| item | control | subuid-gpu | subuid-kept | subuid-none | single |
|---|---|---|---|---|---|
| I1 | not confined | held | **not held** | held | **not held** |
| F1 | not confined | held | held | held | held |
| F2 | not confined | held | held | held | held |
| F3 | not confined | held | held | held | held |
| F4 | not confined | held | held | held | held |
| F5 | not confined | held | **not held** | held | **not held** |
| P1 | not confined | held | held | held | held |
| P2 | not confined | held | held | held | held |
| P3 | not confined | held | held | held | held |
| N1 | not confined | held | held | held | held |
| N2 | not confined | held | held | held | held |
| N3 | works | works | works | works | works |
| K1 | not confined | held | held | held | held |
| K2 | not confined | held | held | held | held |
| K3 | blocked anyway | held | held | held | held |
| G1 | works | works | works | **fails** | works |
| G2 | not confined | held | held | held | held |
| G3 | recorded | recorded | recorded | recorded | recorded |
| G4 | recorded | recorded | recorded | recorded | recorded |
| R1 | recorded | recorded | recorded | recorded | recorded |

"Not confined" in the control is the expected result (the check sees the difference);
"blocked anyway" means an unprivileged account cannot do it without a sandbox either.
The job's kernel ids, read outside:

| variant | kernel uid, gid | kernel groups | inside |
|---|---|---|---|
| control | the account's | every group of the account (3) | the account's |
| subuid-gpu | a subordinate pair | the GPU group alone | uid and gid 1000, groups `gpu` |
| subuid-kept | a subordinate pair | every group of the account (3) | uid and gid 1000, groups `nogroup`, `nogroup`, `root` |
| subuid-none | a subordinate pair | none | uid and gid 1000, no group |
| single | the account's | every group of the account (3) | uid and gid 0 |

Expected in it: I1 fails under `subuid-kept` and `single` (the account's other groups;
under `single` also the account's uid), and so does F5, whose file of another of the
account's groups the mount namespace hides in every variant but whose group the job
holds there; G1 fails under `subuid-none` (CUDA finds no device and JAX falls back to the
CPU). Under `subuid-gpu` every item holds and the card works. A fresh process that
imports JAX and runs one 1024 × 1024 product takes 3.96 s inside under `gpu` against
3.81 s outside (medians of 5); an empty sandbox takes 0.11 s.

## What it does not do

- **Co-users of the account.** The account owns the sandbox's user namespace, so any process
  of the account has every capability in it: it can trace the job (R1), enter its
  namespaces, and read or change its tree. The sandbox protects the account from the job,
  not the job from the account's other users; integrity against them needs a separate
  kernel uid for the namespace's owner, which only root can provide.
- **No seccomp, no cgroup.** Nested namespaces are blocked by the namespace limits, not by a
  seccomp filter; memory, CPU and pids are not limited (cgroup v1 on the shared-account host,
  no delegation). The host's devices cgroup still applies (the card must be one the
  account's session may open).
- **AppArmor.** Ubuntu 24.04 restricts unprivileged user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns=1`): a namespace gets no capabilities, so
  the launcher cannot build the sandbox (it says so and exits 125) unless root installs an
  AppArmor profile for it and the `uidmap` package.
- **The relay** forwards bytes to the proxy port; the proxy's allowlist does the filtering.
