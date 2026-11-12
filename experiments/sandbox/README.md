# Sandbox prototype: confining a GPU job on a host without root

Experimental. Nothing in the repository imports, installs or runs these files; no
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
               [--map subuid|single] [--groups gpu|none] [--setenv K=V]... -- CMD...
sandbox.py probe      # exit 0 when this account can build the sandbox here, 125 and why when not
```

The job runs in new user, mount, pid, ipc, network and uts namespaces:

| inside | what |
|---|---|
| `/job` | `--tree`, read-write; `HOME=/job/home` |
| `/usr`, `/bin`, `/lib*` | the host's, read-only, nosuid |
| `/etc` | generated, read-only: `passwd`/`group` with `root` and `job` only, `hosts`, `ld.so.cache`, the CA store |
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
- **The card's group.** On the shared-account host the card nodes are mode 0660 with a GPU
  group the account belongs to as a supplementary group. `newgidmap` (shadow 4.8.1) maps only
  the subordinate range and the caller's *primary* gid: it refuses a caller whose real gid is
  not the passwd one, so `sg`/`newgrp` does not help, and the GPU group cannot be mapped.
  A process keeps supplementary groups it does not drop, though, so `--groups gpu` keeps them
  (`setpriv --keep-groups`): the job ends with the subordinate uid and gid and *all* the
  account's supplementary groups (they show as `nogroup` inside). `--groups none` drops them
  all, and then the card cannot be opened. Keeping the GPU group and nothing else needs root:
  one line `<account>:<gpu gid>:1` in `/etc/subgid` lets `newgidmap` map exactly that gid.
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
check.py suite --variant control|subuid-gpu|subuid-none|single --scratch DIR --env DIR \
               [--card N] [--proxy-port P] [--out FILE]
check.py cost  --scratch DIR --env DIR --card N [--repeat 3] [--out FILE]
```

The suite makes a run directory under `--scratch` with the job's tree, the policy stand-in
(holding a copy of `check.py`, which runs from there read-only) and a marker file outside
the tree; it starts a marker process (which allows any same-uid tracer), a marker TCP
listener on the host's loopback and, with `--card`, a marker CUDA process of ours holding
64 MiB on the card. It runs the items, reads the job's kernel ids from outside, removes
everything and prints a table.

| item | a confined job observes |
|---|---|
| I1 | uid, gid and groups inside; the kernel ids outside; the kernel uid is not the account's |
| F1–F4 | the marker file is not visible; creating a file in the policy stand-in fails; the account's home does not exist; nothing outside the tree (and the per-job `/tmp`) is writable |
| P1–P3 | `/proc` lists only the sandbox's processes, not the marker; signal 0 to the marker fails; `PTRACE_SEIZE` of it fails |
| N1–N3 | the marker listener cannot be reached; the proxy port cannot be reached directly; through the relay an allowlisted URL answers and another is refused by the proxy |
| K1–K3 | a new user namespace, `mount` (also after trying one) and `mknod` of a device node fail |
| G1–G4 | the leased card works (jax); no other card node exists; what nvidia-smi shows; whether a fresh allocation reads the pattern a previous process left |
| R1 | (information) whether a process of the account outside can trace the job |

The control runs the same items with no sandbox and must report "not confined", which
shows the items can tell the difference. Two cannot: an unprivileged account can create
no device node with or without a sandbox (K3), and on a host that restricts unprivileged
user namespaces the control cannot mount either (K2).

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
