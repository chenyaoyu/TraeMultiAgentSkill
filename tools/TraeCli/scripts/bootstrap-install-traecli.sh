#!/bin/sh

set -eu

repo_slug=${TRAECLI_GITHUB_REPO:-firerlAGI/TraeCli}
repo_ref=${TRAECLI_REF:-main}
archive_url=${TRAECLI_ARCHIVE_URL:-"https://github.com/$repo_slug/archive/refs/heads/$repo_ref.tar.gz"}
tmp_root=""

usage() {
  cat <<'EOF'
Usage: bootstrap-install-traecli.sh [install-options]

Download the TraeCLI repository tarball from GitHub, then run the repo-local
installer. Any extra arguments are forwarded to `scripts/install-traecli.sh`.

Environment:
  TRAECLI_GITHUB_REPO   Override the GitHub repo slug (default: firerlAGI/TraeCli)
  TRAECLI_REF           Override the Git ref to download (default: main)
  TRAECLI_ARCHIVE_URL   Override the archive URL directly
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [ -n "$tmp_root" ] && [ -d "$tmp_root" ]; then
    rm -rf "$tmp_root"
  fi
}

download_archive() {
  output_path=$1
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$archive_url" -o "$output_path"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$archive_url" "$output_path" <<'PY'
import sys
import urllib.request

urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
    return 0
  fi
  fail "curl or python3 is required to download $archive_url"
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

command -v tar >/dev/null 2>&1 || fail "tar is required"

tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/traecli-bootstrap.XXXXXX")
trap cleanup EXIT INT TERM

archive_path="$tmp_root/traecli.tar.gz"
printf 'Downloading %s\n' "$archive_url"
download_archive "$archive_path"

tar -xzf "$archive_path" -C "$tmp_root"
repo_root=$(find "$tmp_root" -mindepth 1 -maxdepth 1 -type d | head -n 1)
[ -n "$repo_root" ] || fail "failed to unpack repository archive"
[ -x "$repo_root/scripts/install-traecli.sh" ] || fail "install script not found in archive"

exec "$repo_root/scripts/install-traecli.sh" "$@"
