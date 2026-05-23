#!/bin/sh

set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
harness_dir="$repo_root/agent-harness"
tmp_root=""
keep_tmp=0
skip_tests=0

usage() {
  cat <<'EOF'
Usage: ./scripts/release-check.sh [options]

Run the repeatable local release validation flow for TraeCLI.

Options:
  --skip-tests    Skip the full unittest suite
  --keep-tmp      Keep the temporary validation directories
  -h, --help      Show this help text
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  if [ "$keep_tmp" -eq 1 ]; then
    if [ -n "$tmp_root" ]; then
      printf 'Keeping temporary files at %s\n' "$tmp_root"
    fi
    return
  fi
  if [ -n "$tmp_root" ] && [ -d "$tmp_root" ]; then
    rm -rf "$tmp_root"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-tests)
      skip_tests=1
      ;;
    --keep-tmp)
      keep_tmp=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
  shift
done

trap cleanup EXIT INT TERM

command -v python3 >/dev/null 2>&1 || fail "python3 is required"
command -v sh >/dev/null 2>&1 || fail "sh is required"
[ -d "$harness_dir" ] || fail "agent-harness directory not found at $harness_dir"

tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/traecli-release-check.XXXXXX")
manual_venv="$tmp_root/manual-venv"
installer_venv="$tmp_root/installer-venv"
installer_bin="$tmp_root/bin"
bootstrap_venv="$tmp_root/bootstrap-venv"
bootstrap_bin="$tmp_root/bootstrap-bin"
bootstrap_archive_root="$tmp_root/bootstrap-archive"
bootstrap_archive_path="$tmp_root/TraeCli.tar.gz"
installer_shell_rc="$tmp_root/.installer-shellrc"

printf 'Repo root: %s\n' "$repo_root"
printf 'Harness dir: %s\n' "$harness_dir"

if [ "$skip_tests" -ne 1 ]; then
  printf '\n[1/7] Running unit and e2e tests\n'
  (
    cd "$harness_dir"
    python3 -m unittest discover -s cli_anything/trae/tests -v
  )
else
  printf '\n[1/7] Skipping unit and e2e tests\n'
fi

printf '\n[2/7] Checking installer scripts\n'
sh -n "$repo_root/scripts/install-traecli.sh"
sh -n "$repo_root/scripts/bootstrap-install-traecli.sh"
sh -n "$repo_root/scripts/uninstall-traecli.sh"
sh -n "$repo_root/scripts/release-check.sh"
"$repo_root/scripts/install-traecli.sh" --help >/dev/null
"$repo_root/scripts/bootstrap-install-traecli.sh" --help >/dev/null
"$repo_root/scripts/uninstall-traecli.sh" --help >/dev/null

printf '\n[3/7] Verifying editable install flow\n'
python3 -m venv "$manual_venv"
"$manual_venv/bin/pip" install -e "$harness_dir"
"$manual_venv/bin/cli-anything-trae" --help >/dev/null
"$manual_venv/bin/traecli" --help >/dev/null

printf '\n[4/7] Verifying repo installer flow\n'
TRAECLI_SHELL_RC="$installer_shell_rc" "$repo_root/scripts/install-traecli.sh" \
  --skip-init \
  --venv-dir "$installer_venv" \
  --bin-dir "$installer_bin"
"$installer_bin/cli-anything-trae" --help >/dev/null
"$installer_bin/traecli" --help >/dev/null
grep -F "Added by TraeCLI installer" "$installer_shell_rc" >/dev/null

printf '\n[5/7] Verifying uninstall flow\n'
TRAECLI_SHELL_RC="$installer_shell_rc" "$repo_root/scripts/uninstall-traecli.sh" \
  --venv-dir "$installer_venv" \
  --bin-dir "$installer_bin"
[ ! -e "$installer_venv" ]
[ ! -e "$installer_bin/traecli" ]
[ ! -e "$installer_bin/cli-anything-trae" ]
if [ -f "$installer_shell_rc" ]; then
  ! grep -F "Added by TraeCLI installer" "$installer_shell_rc" >/dev/null
fi

printf '\n[6/7] Verifying bootstrap installer flow from a local archive\n'
mkdir -p "$bootstrap_archive_root/TraeCli"
tar -cf - --exclude='.git' -C "$repo_root" . | tar -xf - -C "$bootstrap_archive_root/TraeCli"
tar -czf "$bootstrap_archive_path" -C "$bootstrap_archive_root" TraeCli
TRAECLI_ARCHIVE_URL="file://$bootstrap_archive_path" \
  sh "$repo_root/scripts/bootstrap-install-traecli.sh" \
  --skip-init \
  --no-shell-rc \
  --venv-dir "$bootstrap_venv" \
  --bin-dir "$bootstrap_bin"
"$bootstrap_bin/cli-anything-trae" --help >/dev/null
"$bootstrap_bin/traecli" --help >/dev/null

printf '\n[7/7] Smoke-checking doctor output from clean bootstrap install\n'
"$bootstrap_bin/traecli" --json doctor >/dev/null

printf '\nRelease validation passed.\n'
