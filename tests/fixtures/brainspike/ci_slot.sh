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

# The traps come first, so that no signal leaves a ticket behind.
TICKET= sleeper=
trap 'rm -f "$QUEUE/seq.$$"; [ -z "$TICKET" ] || rm -f "$TICKET" "$TICKET.$$"; [ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# take_ticket: the counter and the ticket file change under one lock, so ticket n exists
# before ticket n + 1 is handed out; the number is past the counter, every ticket queued
# and our own earlier ticket, so a new ticket is always at the back. The ticket appears
# by rename, never half written.
SEQ=0 START=$(date +%s) CMD=$*
CMD=${CMD//$'\n'/ }
take_ticket() {
  local qfd n t ok
  mkdir -p "$QUEUE" 2>/dev/null || return 1
  exec {qfd}>>"$QUEUE/seq.lock" || return 1
  flock -x "$qfd"
  n=$(cat "$QUEUE/seq" 2>/dev/null)
  [[ $n =~ ^[0-9]{1,15}$ ]] && n=$((10#$n)) || n=0
  ((SEQ > n)) && n=$SEQ
  for t in "$QUEUE"/*.ticket; do
    [[ ${t##*/} =~ ^([0-9]{12})\.ticket$ ]] || continue
    ((10#${BASH_REMATCH[1]} > n)) && n=$((10#${BASH_REMATCH[1]}))
  done
  SEQ=$((n + 1))
  TICKET=$(printf '%s/%012d.ticket' "$QUEUE" "$SEQ")
  echo "$SEQ" > "$QUEUE/seq.$$" && mv -f "$QUEUE/seq.$$" "$QUEUE/seq" &&
    echo "$SEQ $$ $START $PWD $CMD" > "$TICKET.$$" && mv -f "$TICKET.$$" "$TICKET"
  ok=$?
  rm -f "$QUEUE/seq.$$" "$TICKET.$$"
  flock -u "$qfd"
  exec {qfd}>&-
  return $ok
}
take_ticket 2>/dev/null || { echo "brainspike-ci-slot: cannot write a ticket in $QUEUE" >&2; exit 2; }

shown=
while true; do
  pos=0 ahead=-1 head= msg=
  if [ ! -e "$TICKET" ]; then  # deleted while we were stopped, or with the queue directory
    if take_ticket 2>/dev/null; then
      echo "brainspike-ci-slot: the ticket was gone, re-queued as ticket $SEQ" >&2
      shown=
    else
      msg="the ticket is gone and $QUEUE is not writable, retrying"
    fi
  fi
  [ -n "$msg" ] || for t in "$QUEUE"/*.ticket; do
    [[ ${t##*/} =~ ^([0-9]{12})\.ticket$ ]] || continue  # a stray file, or no ticket at all
    named=${BASH_REMATCH[1]} seq= pid= start= rest=
    read -r seq pid start rest 2>/dev/null < "$t"
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
        trap - EXIT HUP INT TERM
        echo "brainspike-ci-slot: holding $s" >&2
        "$@"
        exit $?
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
