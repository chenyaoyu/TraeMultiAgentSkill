#!/bin/sh

set -eu

venv_dir=${TRAECLI_VENV_DIR:-"$HOME/.trae/traecli-venv"}
bin_dir=${TRAECLI_BIN_DIR:-"$HOME/.local/bin"}
shell_rc=${TRAECLI_SHELL_RC:-""}
remove_shell_rc=1
purge_state=0

usage() {
  cat <<'EOF'
Usage: ./scripts/uninstall-traecli.sh [options]

Remove the TraeCLI virtualenv, unlink the installed console scripts, and
optionally clean up CLI state files.

Options:
  --no-shell-rc   Do not remove the PATH block added by the installer
  --shell-rc PATH Override the shell rc file to clean
  --venv-dir PATH Override the virtualenv directory to remove
  --bin-dir PATH  Override the user bin directory to clean
  --purge-state   Also remove CLI config/session files from ~/.trae and ~/.trae-cn
  -h, --help      Show this help text
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

detect_shell_rc() {
  if [ -n "$shell_rc" ]; then
    printf '%s\n' "$shell_rc"
    return 0
  fi
  shell_name=$(basename "${SHELL:-}")
  case "$shell_name" in
    zsh)
      printf '%s\n' "$HOME/.zshrc"
      ;;
    bash)
      if [ -f "$HOME/.bash_profile" ]; then
        printf '%s\n' "$HOME/.bash_profile"
      else
        printf '%s\n' "$HOME/.bashrc"
      fi
      ;;
    *)
      printf '%s\n' ""
      ;;
  esac
}

remove_shell_path_block() {
  if [ "$remove_shell_rc" -ne 1 ]; then
    return 0
  fi
  rc_path=$(detect_shell_rc)
  if [ -z "$rc_path" ] || [ ! -f "$rc_path" ]; then
    return 0
  fi
  python3 - "$rc_path" "$bin_dir" <<'PY'
import pathlib
import sys

rc_path = pathlib.Path(sys.argv[1])
bin_dir = sys.argv[2]
marker = "# Added by TraeCLI installer"
path_line = f'export PATH="{bin_dir}:$PATH"'
lines = rc_path.read_text(encoding="utf-8").splitlines()
kept = []
skip_next = False
changed = False
for line in lines:
    if skip_next:
        skip_next = False
        if line == path_line:
            changed = True
            continue
    if line == marker:
        skip_next = True
        changed = True
        continue
    if line == path_line:
        changed = True
        continue
    kept.append(line)
if changed:
    text = "\n".join(kept).rstrip()
    rc_path.write_text((text + "\n") if text else "", encoding="utf-8")
PY
}

purge_state_files() {
  for root_dir in "$HOME/.trae" "$HOME/.trae-cn"; do
    rm -f \
      "$root_dir/traecli.json" \
      "$root_dir/traecli-sessions.json" \
      "$root_dir/traecli-bindings.json"
  done
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-shell-rc)
      remove_shell_rc=0
      ;;
    --shell-rc)
      shift
      [ $# -gt 0 ] || fail "--shell-rc requires a path"
      shell_rc=$1
      ;;
    --venv-dir)
      shift
      [ $# -gt 0 ] || fail "--venv-dir requires a path"
      venv_dir=$1
      ;;
    --bin-dir)
      shift
      [ $# -gt 0 ] || fail "--bin-dir requires a path"
      bin_dir=$1
      ;;
    --purge-state)
      purge_state=1
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

printf 'Removing TraeCLI symlinks from %s\n' "$bin_dir"
rm -f "$bin_dir/traecli" "$bin_dir/cli-anything-trae"

printf 'Removing TraeCLI virtualenv at %s\n' "$venv_dir"
rm -rf "$venv_dir"

remove_shell_path_block

if [ "$purge_state" -eq 1 ]; then
  printf 'Removing CLI state from ~/.trae and ~/.trae-cn\n'
  purge_state_files
fi

printf 'TraeCLI uninstall complete.\n'
