#!/bin/sh
# setup/install.sh --tag TAG --expect SHA | --commit SHA  [--rollback]: install a
# release of chaobrain/ci-runners into ~/data/ci-runners/releases/<TAG or main-<sha12>>/,
# read-only, then point releases/hook at it (design §5.2, §9). A tag must resolve to
# SHA, the commit the lead released (an annotated tag by its peeled commit). What
# running jobs use is never moved: an existing release is kept only if it is a real
# directory of that same commit, and refused otherwise. The hook moves only forward:
# to a higher tag, or from a main commit (the bootstrap) to anything; back needs
# --rollback. Network through $https_proxy.
set -eu
repo=https://github.com/chaobrain/ci-runners
rel=$HOME/data/ci-runners/releases
usage() { echo "usage: $0 --tag TAG --expect SHA | --commit SHA  [--rollback]" >&2; exit 2; }
die() { echo "install.sh: $*" >&2; exit 1; }
back=
case ${1:-} in
  --tag) [ $# -ge 4 ] && [ "$3" = --expect ] || usage
         tag=$2 sha=$4 id=$2; shift 4
         echo "$tag" | grep -Eqx 'v[0-9]+\.[0-9]+\.[0-9]+' || { echo "install.sh: TAG must be vX.Y.Z" >&2; exit 2; } ;;
  --commit) [ $# -ge 2 ] || usage
         tag= sha=$2 id=main-$(printf %.12s "$2"); shift 2 ;;
  *) usage ;;
esac
case ${1:-} in --rollback) back=1; shift ;; esac
[ $# -eq 0 ] || usage
case $sha in *[!0-9a-f]*|'') echo "install.sh: SHA must be a full hex commit id" >&2; exit 2 ;; esac
[ ${#sha} -eq 40 ] || { echo "install.sh: SHA must have 40 hex digits" >&2; exit 2; }
if [ -n "$tag" ]; then
  refs=$(git ls-remote "$repo" "refs/tags/$tag" "refs/tags/$tag^{}")
  got=$(echo "$refs" | awk -v r="refs/tags/$tag^{}" '$2 == r {print $1}')
  [ -n "$got" ] || got=$(echo "$refs" | awk -v r="refs/tags/$tag" '$2 == r {print $1}')
  [ "$got" = "$sha" ] || die "$tag resolves to ${got:-nothing}, not $sha"
fi
cur=
[ -L "$rel/hook" ] && cur=$(readlink "$rel/hook")
if [ -n "$cur" ] && [ "$cur" != "$id" ] && [ -z "$back" ]; then
  case $cur in
    main-*) ;;  # the bootstrap release: anything replaces it
    v*) [ -n "$tag" ] || die "releases/hook is at $cur: a main commit replaces a tag only with --rollback"
        top=$(printf '%s\n%s\n' "$cur" "$tag" | sort -V | tail -1)
        [ "$top" = "$tag" ] || die "releases/hook is at $cur, above $tag: moving back needs --rollback" ;;
    *) die "releases/hook points at $cur, not a release" ;;
  esac
fi
mkdir -p "$rel"
if [ -e "$rel/$id" ] || [ -L "$rel/$id" ]; then
  [ -d "$rel/$id" ] && [ ! -L "$rel/$id" ] || die "$rel/$id exists and is not a directory: remove it"
  [ "$(cat "$rel/$id/COMMIT" 2>/dev/null)" = "$sha" ] || die "$rel/$id is not a release of $sha: remove it"
else
  tmp=$(mktemp -d "$rel/.$id.XXXXXX")
  trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
  curl -fsSL --max-time 300 "$repo/archive/$sha.tar.gz" | tar -xz -C "$tmp" --strip-components=1
  [ -d "$tmp/ci_runners" ] || die "the archive of $sha has no ci_runners/"
  echo "$sha" > "$tmp/COMMIT"
  chmod -R a-w "$tmp"
  chmod u+w "$tmp"; mv -T "$tmp" "$rel/$id"; chmod u-w "$rel/$id"
  trap - EXIT
fi
ln -sfn "$id" "$rel/hook.new" && mv -T "$rel/hook.new" "$rel/hook"
echo "install.sh: $id ($sha) installed; releases/hook -> $id"
