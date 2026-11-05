#!/usr/bin/env bash
# Run a command under the reference host's lock, shared or exclusive.
#
#   scripts/host_lock.sh -s|-x|-l CMD [ARG...]
#
# -s (shared) for load: test runs, an ad-hoc pytest with -n >= 2, benchmarks. A waiting
# exclusive request waits for every shared job already in, for its whole length, so an
# ad-hoc -s job holds at most about 15 min: split a longer one into shorter locked
# commands (one per test file or round), which let a waiting exclusive request in
# between them (lessons L57).
# -x (exclusive) for jobs that time compiles or steps.
# -l (light) for small jobs that time nothing and load at most a core for about a
# minute (local_ci.sh's inventory, types, source scans and coverage report, and its
# light test commands: in process, at most 30 s of tests by the impact map): shared.
# While a normal shared job (-s, or a host_lock.py shared hold) is in, a light job
# enters without the turnstile, so it does not wait behind an exclusive request that
# waits for that job (a docs-only `affected` once waited 35 min behind an exclusive
# request that itself waited for a 30-min gpu run). With no normal shared job in, it
# queues like -s: behind an exclusive request that waits, so a stream of light jobs
# cannot keep that request waiting (#222 review, LOW-11). So an exclusive job runs
# alone, and a waiting one is delayed by at most the normal shared jobs already in and
# one light job that entered beside them. Normal shared holders mark themselves with a
# shared hold on ${LOCAL_CI_HOST_LOCK%.lock}.shared.lock, which a light request probes.
# The host lock is $LOCAL_CI_HOST_LOCK (default /tmp/brainspike-host.lock);
# scripts/local_ci.sh takes it per command through the same functions (it sources this
# file).
#
# Writer-preferring, via a turnstile: every request first takes the turnstile
# ${LOCAL_CI_HOST_LOCK%.lock}.turnstile.lock exclusively. A shared request releases it
# as soon as it holds the host lock; an exclusive one keeps it until its command ends.
# So a waiting exclusive request stops new shared requests from entering, and it runs
# once the shared holders already in have finished (plain flock lets new readers keep
# a writer waiting indefinitely). Requests blocked in flock(2) are woken in the order
# they blocked, so the exclusive requests take the turnstile first come, first served;
# host_lock.py's timed waits block the same way (flock -w), never by polling. No deadlock: whoever holds the host lock
# exclusively also holds the turnstile, so a shared request never holds the turnstile
# while waiting for an exclusive holder that waits for it.
#
# Prints the mode and the wait to stderr and exits with CMD's status. CMD runs without
# the lock fds, so a process it leaves behind does not keep the lock held.
#
# Nesting. CMD gets BRAINSPIKE_HOST_LOCK=s|x, the mode it runs under. A request made
# under BRAINSPIKE_HOST_LOCK=x is a no-op (the outer exclusive hold covers it); under
# =s a shared request is a no-op and an exclusive one fails with status 3, since it
# would wait for its own caller's shared hold forever. A cover is checked before it is
# trusted: this process or one of its ancestors must have the host lock open (the
# holder that exported it keeps it open while CMD runs), and a non-blocking probe on a
# fresh fd of the host lock (shared under x, exclusive under s) must be refused. If
# either fails, the holder that exported the cover is gone (CMD left a child running
# and exited, or the holder was stopped), and a hold the probe sees is another job's
# (#222 review, LOW-12): the cover is stale, that is said on stderr, and the request
# takes the lock normally. Any other value is an error (status 2). scripts/host_lock.py is the same protocol in
# Python, for jobs that lock only part of their run (the compile gates: build shared,
# time exclusive; architecture §8 rule 4).

HOST_LOCK=${LOCAL_CI_HOST_LOCK:-/tmp/brainspike-host.lock}
HOST_LOCK_TURNSTILE=${HOST_LOCK%.lock}.turnstile.lock
HOST_LOCK_SHARED=${HOST_LOCK%.lock}.shared.lock

# _hl_held_here: whether this shell or one of its ancestors has the host lock open
# (the holder whose cover BRAINSPIKE_HOST_LOCK is)
_hl_held_here() {
  local pid=$$ path stat ppid n=0
  path=$(readlink -f "$HOST_LOCK") || return 1
  while [ "$pid" -gt 1 ] && [ $n -lt 100 ]; do
    [ -n "$(find "/proc/$pid/fd" -maxdepth 1 -lname "$path" -print -quit 2> /dev/null)" ] && return 0
    stat=$(< "/proc/$pid/stat") 2> /dev/null || return 1
    read -r _ ppid _ <<< "${stat##*) }"
    pid=$ppid n=$((n + 1))
  done
  return 1
}

# host_lock_acquire -s|-x|-l: take the host lock; sets HOST_LOCK_WAITED (seconds),
# HOST_LOCK_COVERED (1 when the caller's live hold covers the request) and
# HOST_LOCK_STALE (the stale cover it ignored, if any). Status 3: an exclusive request
# under a live shared hold; status 2: an unknown BRAINSPIKE_HOST_LOCK.
host_lock_acquire() {
  local start=$SECONDS held=${BRAINSPIKE_HOST_LOCK:-} probe kind light=
  _HL_MODE=${1#-} HOST_LOCK_WAITED=0 HOST_LOCK_COVERED= HOST_LOCK_STALE= _HL_FD= _HL_TS= _HL_SH=
  if [ "$1" = -l ]; then light=1 _HL_MODE=s; set -- -s; fi  # a shared hold to its command
  case $held in
    '') ;;
    x|s)
      if [ "$held" = x ]; then probe=-s; else probe=-x; fi
      # stale: no holder of ours (the hold a probe sees may be another job's), or no
      # holder of that mode at all
      if ! _hl_held_here || flock -n "$probe" "$HOST_LOCK" true 2> /dev/null; then
        HOST_LOCK_STALE=$held
        if [ "$held" = x ]; then kind='exclusive holder'; else kind=holder; fi
        echo "host_lock: stale BRAINSPIKE_HOST_LOCK=$held cover (no $kind of $HOST_LOCK): taking the lock" >&2
      elif [ "$held" = x ] || [ "$_HL_MODE" = s ]; then
        _HL_MODE=$held HOST_LOCK_COVERED=1
        return 0
      else
        echo "host_lock: an exclusive request under the caller's shared hold" \
             "(BRAINSPIKE_HOST_LOCK=s) would wait for itself" >&2
        return 3
      fi ;;
    *) echo "host_lock: BRAINSPIKE_HOST_LOCK=$held: expected s or x (or unset)" >&2
       return 2 ;;
  esac
  if [ -n "$light" ] && ! flock -n -x "$HOST_LOCK_SHARED" true 2> /dev/null; then
    # a normal shared job is in: enter beside it, past an exclusive request that waits
    # for it anyway
    exec {_HL_FD}>>"$HOST_LOCK"
    flock -s "$_HL_FD"
    HOST_LOCK_WAITED=$((SECONDS - start))
    return 0
  fi
  exec {_HL_TS}>>"$HOST_LOCK_TURNSTILE" {_HL_FD}>>"$HOST_LOCK"
  flock -x "$_HL_TS"
  flock "$1" "$_HL_FD"
  if [ "$1" = -s ]; then
    if [ -z "$light" ]; then  # a normal shared holder: light requests may enter beside it
      exec {_HL_SH}>>"$HOST_LOCK_SHARED"
      flock -s "$_HL_SH"
    fi
    flock -u "$_HL_TS"
    exec {_HL_TS}>&-
    _HL_TS=
  fi
  HOST_LOCK_WAITED=$((SECONDS - start))
}

# host_lock_run CMD...: run CMD with the lock fds closed in it and
# BRAINSPIKE_HOST_LOCK set to the mode it runs under.
host_lock_run() {
  if [ -n "$_HL_TS" ]; then BRAINSPIKE_HOST_LOCK=$_HL_MODE "$@" {_HL_FD}>&- {_HL_TS}>&-
  elif [ -n "$_HL_SH" ]; then BRAINSPIKE_HOST_LOCK=$_HL_MODE "$@" {_HL_FD}>&- {_HL_SH}>&-
  elif [ -n "$_HL_FD" ]; then BRAINSPIKE_HOST_LOCK=$_HL_MODE "$@" {_HL_FD}>&-
  else BRAINSPIKE_HOST_LOCK=$_HL_MODE "$@"
  fi
}

# host_lock_release: release what host_lock_acquire took.
host_lock_release() {
  [ -n "$_HL_FD" ] || return 0
  if [ -n "$_HL_SH" ]; then
    flock -u "$_HL_SH"
    exec {_HL_SH}>&-
    _HL_SH=
  fi
  flock -u "$_HL_FD"
  exec {_HL_FD}>&-
  _HL_FD=
  if [ -n "$_HL_TS" ]; then
    flock -u "$_HL_TS"
    exec {_HL_TS}>&-
    _HL_TS=
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ $# -lt 2 ] || { [ "$1" != -s ] && [ "$1" != -x ] && [ "$1" != -l ]; }; then
    echo "usage: $0 -s|-x|-l CMD [ARG...]" >&2
    exit 2
  fi
  mode=$1
  shift
  host_lock_acquire "$mode" || exit $?
  if [ -n "$HOST_LOCK_COVERED" ]; then
    echo "host_lock: ${mode#-} request covered by the caller's $BRAINSPIKE_HOST_LOCK hold" >&2
  else
    echo "host_lock: ${mode#-} lock on $HOST_LOCK after $((HOST_LOCK_WAITED / 60))m$((HOST_LOCK_WAITED % 60))s" >&2
  fi
  host_lock_run "$@"
  code=$?
  host_lock_release
  exit $code
fi
