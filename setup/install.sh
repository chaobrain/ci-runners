#!/bin/sh
# setup/install.sh --tag TAG --expect SHA | --commit SHA: install a release of
# chaobrain/ci-runners into ~/data/ci-runners/releases/<TAG or main-<sha12>>/,
# read-only, then point releases/hook at it (design §5.2, §9). A tag must resolve to
# SHA, the commit the lead released. What running jobs use is never moved: an existing
# release directory is left as it is. Network through $https_proxy.
set -eu
repo=https://github.com/chaobrain/ci-runners
rel=$HOME/data/ci-runners/releases
case ${1:-} in
  --tag) [ $# -eq 4 ] && [ "$3" = --expect ] || { echo "usage: $0 --tag TAG --expect SHA" >&2; exit 2; }
         tag=$2 sha=$4 id=$2
         got=$(git ls-remote "$repo" "refs/tags/$tag^{}" "refs/tags/$tag" | awk '{print $1}' | head -1)
         [ "$got" = "$sha" ] || { echo "install.sh: $tag resolves to ${got:-nothing}, not $sha" >&2; exit 1; } ;;
  --commit) [ $# -eq 2 ] || { echo "usage: $0 --commit SHA" >&2; exit 2; }
         sha=$2 id=main-$(printf %.12s "$2") ;;
  *) echo "usage: $0 --tag TAG --expect SHA | --commit SHA" >&2; exit 2 ;;
esac
case $sha in *[!0-9a-f]*|'') echo "install.sh: SHA must be a full hex commit id" >&2; exit 2 ;; esac
[ ${#sha} -eq 40 ] || { echo "install.sh: SHA must have 40 hex digits" >&2; exit 2; }
mkdir -p "$rel"
if [ ! -d "$rel/$id" ]; then
  tmp=$(mktemp -d "$rel/.$id.XXXXXX")
  trap 'chmod -R u+w "$tmp" 2>/dev/null; rm -rf "$tmp"' EXIT
  curl -fsSL --max-time 300 "$repo/archive/$sha.tar.gz" | tar -xz -C "$tmp" --strip-components=1
  echo "$sha" > "$tmp/COMMIT"
  chmod -R a-w "$tmp"
  chmod u+w "$tmp"; mv "$tmp" "$rel/$id"; chmod u-w "$rel/$id"
  trap - EXIT
fi
ln -sfn "$id" "$rel/hook.new" && mv -T "$rel/hook.new" "$rel/hook"
echo "install.sh: $id ($sha) installed; releases/hook -> $id"
