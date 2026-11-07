#!/usr/bin/env bash
# Run a command while holding one of the reference host's CI slots, first come first served.
#
#   scripts/ci_slot.sh CMD [ARG...]        e.g. scripts/ci_slot.sh scripts/local_ci.sh affected
#
# At most $LOCAL_CI_SLOTS (default 2) full scripts/local_ci.sh runs share the host; each
# runs under this wrapper. Slot 1 is the lock file $LOCAL_CI_SLOT_LOCK (default
# /tmp/brainspike-local-ci.lock), slot i >= 2 is ${LOCAL_CI_SLOT_LOCK%.lock}.i.lock
# (default /tmp/brainspike-local-ci.2.lock), so every copy of this wrapper with the same
# settings (it was /tmp/brainspike-ci-slot.sh before) shares the same slots.
#
# FIFO. Each request takes a ticket in the queue directory $QUEUE ($LOCAL_CI_SLOT_QUEUE,
# default ${LOCAL_CI_SLOT_LOCK%.lock}.queue): under `flock` on $QUEUE/seq.lock it takes
# the next number after the counter $QUEUE/seq, after every ticket queued and after its
# own earlier ticket, and writes $QUEUE/<seq, 12 digits>.ticket holding
# `<seq> <pid> <start time (epoch s)> <cwd> <command>`. Only the lowest live ticket tries
# the slots (`flock -n`, in order); the others wait and print their queue position to
# stderr when it changes. The queue is read again every $LOCAL_CI_SLOT_POLL seconds
# (default 3). Whoever reads the queue deletes a ticket whose process is gone, is a
# zombie, is stopped (`kill -STOP`, a debugger), or is a later process that reuses the
# pid, and a ticket that does not parse; files that are not <12 digits>.ticket are
# ignored. A waiter whose ticket is gone (it was stopped and resumed, or the queue
# directory was deleted) takes a new one, at the back. A request deletes its own ticket
# once it holds a slot, and on exit or on INT, TERM or HUP while it waits; a SIGKILLed
# waiter leaves a dead ticket, which the next reader deletes. A request stopped in the
# moment it takes a ticket holds $QUEUE/seq.lock, so new requests wait until it resumes.
#
# Once a slot is held the wrapper prints `brainspike-ci-slot: holding <lock>` to stderr,
# runs CMD and exits with its status. CMD and the processes it starts inherit the slot's
# fd, so the slot stays held until the last of them ends. The slots only cap the number
# of full runs; scripts/host_lock.sh, which local_ci.sh takes per command, keeps a
# compile-timing command from running beside tests. Waiters still on the old
# /tmp/brainspike-ci-slot.sh poll the slots without a ticket, outside this order.
#
# Newest run only (lead, 2026-11-05; scripts/ci_runs.py). A request whose command ends in
# `affected` or `merge`, from a branch (not a detached HEAD or main), and that is not
# already part of a run (BRAINSPIKE_RUN_ID unset: remote_ci.py sets it), registers itself
# in the host's run registry and stops the older runs of the branch on this host, waiting
# for a slot or running. A request stopped that way (TERM, with the marker of the newer
# run) passes TERM on to CMD, leaves its ticket and slot, and exits 3 with "superseded by
# <id>", not 143. TERM reaches CMD whenever the wrapper is stopped while it runs.
set -u
if [ $# -lt 1 ]; then
  echo "usage: $0 CMD [ARG...]" >&2
  exit 2
fi
LOCK=${LOCAL_CI_SLOT_LOCK:-/tmp/brainspike-local-ci.lock}
N=${LOCAL_CI_SLOTS:-2}
POLL=${LOCAL_CI_SLOT_POLL:-3}
QUEUE=${LOCAL_CI_SLOT_QUEUE:-${LOCK%.lock}.queue}
if [[ ! $N =~ ^[0-9]+$ ]] || ((10#$N < 1)); then
  echo "brainspike-ci-slot: LOCAL_CI_SLOTS=$N is not a positive integer" >&2
  exit 2
fi
if [[ ! $POLL =~ ^([0-9]+(\.[0-9]*)?|\.[0-9]+)$ ]] || [[ ! $POLL =~ [1-9] ]]; then
  echo "brainspike-ci-slot: LOCAL_CI_SLOT_POLL=$POLL is not a positive number of seconds" >&2
  exit 2
fi
SLOTS=("$LOCK")
for ((i = 2; i <= 10#$N; i++)); do SLOTS+=("${LOCK%.lock}.$i.lock"); done

# alive PID EPOCH: PID runs (not a zombie, not stopped) and started no later than EPOCH,
# the ticket's start (a pid reused by a later process does not count).
BOOT=$(awk '/^btime/ { print $2 }' /proc/stat 2>/dev/null)
HZ=$(getconf CLK_TCK 2>/dev/null || echo 100)
alive() {
  local stat
  if [ -z "$BOOT" ]; then kill -0 "$1" 2>/dev/null; return; fi
  stat=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
  set -- "$2" ${stat##*) }  # the fields after `pid (comm)`: state is the 1st, starttime the 20th
  case $2 in Z | X | x | T | t) return 1 ;; esac
  [ $((BOOT + ${21} / HZ)) -le $(($1 + 2)) ]  # 2 s for the rounding of btime and starttime
}

# register_run: the newest run of a branch only (header). Sets RUN_ID and RUN_KEY, and
# exports BRAINSPIKE_RUN_ID, so CMD (local_ci.sh) does not register again; no registry
# entry (a detached HEAD, main, no python, any error) means no supersede: it fails open.
RUN_ID= RUN_KEY= RUNS_PY=$(dirname "$0")/ci_runs.py LAST=${!#}
register_run() {
  local py out line
  [ -z "${BRAINSPIKE_RUN_ID:-}" ] && [ -f "$RUNS_PY" ] || return 0
  case $LAST in affected | merge) ;; *) return 0 ;; esac
  py=$(command -v python3 || command -v python) || return 0
  RUN_ID=local-$(date +%Y%m%d-%H%M%S)-$$
  out=$("$py" "$RUNS_PY" start --id "$RUN_ID" --kind slot --pid $$ 2> /dev/null) || out=
  while IFS= read -r line; do
    case $line in
      key\ *) RUN_KEY=${line#key } ;;
      superseded\ *) echo "brainspike-ci-slot: superseded the older run ${line#superseded }" >&2 ;;
      'not cancelled '*) echo "brainspike-ci-slot: could not cancel the older run ${line#not cancelled }" >&2 ;;
    esac
  done <<< "$out"
  [ -n "$RUN_KEY" ] || { RUN_ID=; return 0; }
  export BRAINSPIKE_RUN_ID=$RUN_ID
}
# superseded_by: the id of the run that stopped this one (empty: none)
superseded_by() {
  [ -n "$RUN_KEY" ] || return 0
  "$(command -v python3 || command -v python)" "$RUNS_PY" superseded --id "$RUN_ID" --key "$RUN_KEY" 2> /dev/null
}
finish_run() { [ -z "$RUN_KEY" ] || "$(command -v python3 || command -v python)" "$RUNS_PY" done --id "$RUN_ID" --key "$RUN_KEY" 2> /dev/null; }
# on_term: TERM while waiting or running; a superseded request ends with status 3
CHILD=
on_term() {
  local by
  by=$(superseded_by)
  [ -z "$CHILD" ] || kill -TERM "$CHILD" 2> /dev/null
  finish_run
  if [ -n "$by" ]; then
    echo "brainspike-ci-slot: superseded by $by (a newer run of this branch started)" >&2
    exit 3
  fi
  exit 143
}

# The traps come first, so that no signal leaves a ticket behind.
TICKET= sleeper=
trap '[ -z "$TICKET" ] || rm -f "$TICKET"; [ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; finish_run' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap on_term TERM
register_run

# put_files: write the files named in the arguments (DIR NAME TEXT [NAME TEXT]...), in
# order, each as TEXT and a newline: to a temporary file in DIR created exclusively and
# without following a link, mode 0660, then renamed onto NAME, all relative to DIR opened
# as a directory without following a link. Exit 1 on any error; the temporary file is
# removed then.
PUT_PY='
import os, sys
d, rest = sys.argv[1], sys.argv[2:]
dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
for name, text in zip(rest[0::2], rest[1::2]):
    for _ in range(100):
        tmp = ".tmp.%d.%s" % (os.getpid(), os.urandom(8).hex())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=dfd)
            break
        except FileExistsError:
            pass
    else:
        sys.exit(1)
    try:
        os.fchmod(fd, 0o660)
        os.write(fd, os.fsencode(text) + b"\n")
        os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        os.unlink(tmp, dir_fd=dfd)
        raise
'
PY=$(command -v python3 || command -v python)

# take_ticket: the counter and the ticket file change under one lock, so ticket n exists
# before ticket n + 1 is handed out; the number is past the counter, every ticket queued
# and our own earlier ticket, so a new ticket is always at the back. The ticket appears
# by rename, never half written. Status 1 with REFUSE set: the queue holds a link or the
# like where it must not (see the header), which is no use to retry.
SEQ=0 START=$(date +%s) CMD=$*
CMD=${CMD//$'\n'/ }
REFUSE=
take_ticket() {
  local qfd n t ok lk=$QUEUE/seq.lock
  REFUSE=
  [ ! -L "$QUEUE" ] || { REFUSE="$QUEUE is a link, not a directory"; return 1; }
  mkdir -p "$QUEUE" 2>/dev/null || return 1
  [ -n "$PY" ] || { REFUSE="no python3 to write the queue safely"; return 1; }
  if [ ! -e "$lk" ] && [ ! -L "$lk" ]; then (umask 007; set -o noclobber; : > "$lk") 2>/dev/null; fi
  if [ -L "$lk" ] || [ ! -f "$lk" ]; then REFUSE="$lk is not a regular file"; return 1; fi
  exec {qfd}< "$lk" || return 1  # read-only: it creates and writes nothing, through a link or not
  if [ -L "$lk" ] || [ ! "$lk" -ef "/proc/self/fd/$qfd" ] || [ ! -f "/proc/self/fd/$qfd" ]; then
    exec {qfd}>&-
    REFUSE="$lk is not a regular file"
    return 1
  fi
  flock -x "$qfd"
  n=0
  if [ -f "$QUEUE/seq" ] && [ ! -L "$QUEUE/seq" ]; then read -r -n 32 n < "$QUEUE/seq" 2>/dev/null; fi
  [[ $n =~ ^[0-9]{1,15}$ ]] && n=$((10#$n)) || n=0
  ((SEQ > n)) && n=$SEQ
  for t in "$QUEUE"/*.ticket; do
    [[ ${t##*/} =~ ^([0-9]{12})\.ticket$ ]] || continue
    ((10#${BASH_REMATCH[1]} > n)) && n=$((10#${BASH_REMATCH[1]}))
  done
  SEQ=$((n + 1))
  TICKET=$(printf '%s/%012d.ticket' "$QUEUE" "$SEQ")
  "$PY" -I -c "$PUT_PY" "$QUEUE" seq "$SEQ" "${TICKET##*/}" "$SEQ $$ $START $PWD $CMD"
  ok=$?
  flock -u "$qfd"
  exec {qfd}>&-
  return $ok
}
refused() { echo "brainspike-ci-slot: refusing to use $QUEUE: $REFUSE" >&2; exit 2; }
take_ticket 2>/dev/null || { [ -z "$REFUSE" ] || refused; echo "brainspike-ci-slot: cannot write a ticket in $QUEUE" >&2; exit 2; }

shown=
while true; do
  pos=0 ahead=-1 head= msg=
  if [ ! -e "$TICKET" ]; then  # deleted while we were stopped, or with the queue directory
    if take_ticket 2>/dev/null; then
      echo "brainspike-ci-slot: the ticket was gone, re-queued as ticket $SEQ" >&2
      shown=
    else
      [ -z "$REFUSE" ] || refused
      msg="the ticket is gone and $QUEUE is not writable, retrying"
    fi
  fi
  [ -n "$msg" ] || for t in "$QUEUE"/*.ticket; do
    [[ ${t##*/} =~ ^([0-9]{12})\.ticket$ ]] || continue  # a stray file, or no ticket at all
    named=${BASH_REMATCH[1]} seq= pid= start= rest=
    if [ -L "$t" ] || [ ! -f "$t" ]; then  # a link or a device, never ours: delete the entry (a link, not its target)
      rm -f "$t" 2>/dev/null
      continue
    fi
    read -r -n 4096 seq pid start rest 2>/dev/null < "$t"
    if [ "$t" != "$TICKET" ]; then
      if [[ ! "$seq $pid $start" =~ ^[0-9]{1,15}\ [1-9][0-9]{0,9}\ [0-9]{1,12}$ ]] ||
        ((10#$seq != 10#$named)) || ! alive "$((10#$pid))" "$((10#$start))"; then
        rm -f "$t"  # malformed or dead
        continue
      fi
    fi
    pos=$((pos + 1))
    [ -n "$head" ] || head="seq $seq, pid $pid, $rest"
    [ "$t" = "$TICKET" ] && ahead=$((pos - 1))
  done
  if [ -n "$msg" ]; then
    :
  elif [ "$ahead" -eq 0 ]; then
    for s in "${SLOTS[@]}"; do
      exec {fd}>>"$s"
      if flock -n "$fd"; then
        rm -f "$TICKET"
        trap - EXIT HUP INT
        echo "brainspike-ci-slot: holding $s" >&2
        "$@" <&0 &
        CHILD=$!
        while true; do  # TERM runs on_term, which passes it on and ends this wrapper (status 3 if superseded)
          wait "$CHILD"
          rc=$?
          kill -0 "$CHILD" 2> /dev/null || break
        done
        finish_run
        exit $rc
      fi
      exec {fd}>&-
    done
    msg="first in the queue, all ${#SLOTS[@]} slot(s) busy"
  elif [ "$ahead" -lt 0 ]; then
    msg="re-queueing"  # the ticket vanished between the check and the scan
  else
    msg="queue position $((ahead + 1)) of $pos (head: $head)"
  fi
  if [ "$msg" != "$shown" ]; then
    echo "brainspike-ci-slot: waiting, $msg" >&2
    shown=$msg
  fi
  sleep "$POLL" &
  sleeper=$!
  wait "$sleeper"
  sleeper=
done
