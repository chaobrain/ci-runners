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
# waiter leaves a dead ticket, which the next reader deletes.
#
# The queue directory may be writable by another account (the CI runners' account on the
# 3090 hosts), which can create, rename or delete any entry at any moment, so nothing in
# it is trusted: no write follows a link and no open can block. The queue is read and
# written by one python3 helper (QUEUE_PY below), which opens every entry once, by name
# relative to the queue directory (itself opened without following a link), with
# O_NOFOLLOW | O_NONBLOCK (a FIFO planted between a check and the open cannot block it),
# takes fstat of the open file, and reads or locks it only if it is a regular file.
# Writes go to a temporary file created exclusively (O_EXCL | O_NOFOLLOW, mode 0660, a
# name no one can guess) and are renamed onto seq or the ticket name, which replaces a
# link and never writes through it. $QUEUE must be a real directory and seq.lock a regular
# file (a link or another kind is refused, status 2). The lock is the helper's flock on
# seq.lock, taken while it reads the counter and the queue and writes both. A counter or
# a ticket that is not a regular file counts as malformed (a ticket is deleted: rm removes
# a link, not its target). A hostile entry can disturb the queueing, never a local file
# and never the script's progress.
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
TICKET= TAKEN= sleeper=
# drop_ticket: delete our ticket. TAKEN is the helper's whole answer, assigned by the same
# command that runs it, so a signal that ends us as the ticket appears still finds its number.
drop_ticket() {
  [ -z "$TICKET" ] || rm -f "$TICKET"
  case $TAKEN in 'ok '[0-9]*) rm -f "$(printf '%s/%012d.ticket' "$QUEUE" "${TAKEN#ok }")" ;; esac
}
trap 'drop_ticket; [ -z "$sleeper" ] || kill "$sleeper" 2>/dev/null; finish_run' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap on_term TERM
register_run

# QUEUE_PY: the queue's only reader and writer (see the header), run as
#   python3 -I -c "$QUEUE_PY" take DIR SEQ PID START CWD CMD   under flock on DIR/seq.lock:
#       prints `ok <n>` after writing seq and the ticket <n>, n past SEQ, the counter and
#       every ticket queued; `refuse <why>` (status 3) for a link or a non-regular file
#       where one must not be; any other failure is status 1;
#   python3 -I -c "$QUEUE_PY" scan DIR   prints `<name> ok <first line>` for each
#       <12 digits>.ticket that is a regular file (at most 4096 bytes read), `<name> bad`
#       for any other kind of entry, nothing if DIR cannot be opened.
QUEUE_PY='
import errno, fcntl, os, re, stat, sys
RD = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
TICKET = re.compile(r"([0-9]{12})\.ticket")


class Refuse(Exception):
    pass


def opendir(d):
    try:
        return os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refuse("%s is a link, not a directory" % d)
        raise


def read(dfd, name, limit):
    """The first `limit` bytes of the regular file `name`, None for anything else."""
    try:
        fd = os.open(name, RD, dir_fd=dfd)
    except OSError:
        return None
    try:
        return os.read(fd, limit) if stat.S_ISREG(os.fstat(fd).st_mode) else None
    except OSError:
        return None
    finally:
        os.close(fd)


def put(dfd, name, text):
    for _ in range(100):
        tmp = ".tmp.%d.%s" % (os.getpid(), os.urandom(8).hex())
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o660, dir_fd=dfd)
            break
        except FileExistsError:
            pass
    else:
        raise OSError("no temporary name")
    try:
        os.fchmod(fd, 0o660)
        os.write(fd, os.fsencode(text) + b"\n")
        os.close(fd)
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        os.unlink(tmp, dir_fd=dfd)
        raise


def lock(dfd, d):
    why = Refuse("%s/seq.lock is not a regular file" % d)
    for _ in range(3):
        try:
            fd = os.open("seq.lock", RD, dir_fd=dfd)
        except FileNotFoundError:
            try:
                fd = os.open("seq.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             0o660, dir_fd=dfd)
                os.fchmod(fd, 0o660)
            except FileExistsError:
                continue  # made meanwhile: open that one
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENXIO, errno.EISDIR):
                raise why
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise why
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    raise why


def take(d, seq, pid, start, cwd, cmd):
    dfd = opendir(d)
    lock(dfd, d)  # held until this process ends
    n = 0
    data = read(dfd, "seq", 32)
    if data is not None:
        word = data.split(b"\n")[0].strip()
        if re.fullmatch(rb"[0-9]{1,15}", word):
            n = int(word)
    n = max(n, int(seq))
    for name in os.listdir(dfd):
        m = TICKET.fullmatch(name)
        if m:
            n = max(n, int(m.group(1)))
    n += 1
    put(dfd, "seq", str(n))
    put(dfd, "%012d.ticket" % n, "%d %s %s %s %s" % (n, pid, start, cwd, cmd))
    print("ok %d" % n)


def scan(d):
    try:
        dfd = opendir(d)
    except OSError:
        return
    for name in sorted(os.listdir(dfd)):
        if TICKET.fullmatch(name):
            data = read(dfd, name, 4096)
            if data is None:
                print(name, "bad")
            else:
                print(name, "ok", data.split(b"\n")[0].decode("utf-8", "replace"))


try:
    if sys.argv[1] == "take":
        take(*sys.argv[2:])
    else:
        scan(sys.argv[2])
except Refuse as e:
    print("refuse", e)
    sys.exit(3)
except Exception:
    sys.exit(1)
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
  local rc
  REFUSE=
  [ ! -L "$QUEUE" ] || { REFUSE="$QUEUE is a link, not a directory"; return 1; }
  mkdir -p "$QUEUE" 2>/dev/null || return 1
  [ -n "$PY" ] || { REFUSE="no python3 to write the queue safely"; return 1; }
  TAKEN=$("$PY" -I -c "$QUEUE_PY" take "$QUEUE" "$SEQ" "$$" "$START" "$PWD" "$CMD")
  rc=$?
  case $rc in
    0) SEQ=${TAKEN#ok } TICKET=$(printf '%s/%012d.ticket' "$QUEUE" "$SEQ") ;;
    3) REFUSE=${TAKEN#refuse } ;;
  esac
  return $((rc != 0))
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
  [ -n "$msg" ] || while read -r name state line; do
    [[ $name =~ ^([0-9]{12})\.ticket$ ]] || continue  # the helper lists no other name
    named=${BASH_REMATCH[1]} seq= pid= start= rest= t=$QUEUE/$name
    if [ "$state" != ok ]; then  # a link, a FIFO, a device, a directory: not a ticket, never ours
      rm -f "$t" 2>/dev/null
      continue
    fi
    read -r seq pid start rest <<< "$line"
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
  done < <("$PY" -I -c "$QUEUE_PY" scan "$QUEUE" 2>/dev/null)
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
