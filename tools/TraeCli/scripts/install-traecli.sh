#!/bin/sh

set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
harness_dir="$repo_root/agent-harness"
venv_dir=${TRAECLI_VENV_DIR:-"$HOME/.trae/traecli-venv"}
bin_dir=${TRAECLI_BIN_DIR:-"$HOME/.local/bin"}
shell_rc=${TRAECLI_SHELL_RC:-""}
run_init=1
launch_prepared_copy=1
write_shell_rc=1

usage() {
  cat <<'EOF'
Usage: ./scripts/install-traecli.sh [options]

Install TraeCLI from the current repository checkout into a dedicated virtualenv,
link the entrypoints into a user bin directory, and optionally run `traecli init`.

Options:
  --skip-init         Install the CLI but skip `traecli init`
  --no-launch         Do not auto-open a prepared headless app copy
  --no-shell-rc       Do not append the bin dir to your shell rc file
  --shell-rc PATH     Override the shell rc file to update
  --venv-dir PATH     Override the virtualenv directory
  --bin-dir PATH      Override the user bin directory
  -h, --help          Show this help text
EOF
}

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

have_command() {
  command -v "$1" >/dev/null 2>&1
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

path_contains_bin_dir() {
  case ":${PATH:-}:" in
    *:"$bin_dir":*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

ensure_shell_path() {
  if path_contains_bin_dir; then
    return 0
  fi
  if [ "$write_shell_rc" -ne 1 ]; then
    printf 'Note: %s is not on PATH. Add it before using `traecli`.\n' "$bin_dir"
    return 0
  fi
  rc_path=$(detect_shell_rc)
  if [ -z "$rc_path" ]; then
    printf 'Note: could not infer your shell rc file. Add %s to PATH manually.\n' "$bin_dir"
    return 0
  fi
  mkdir -p "$(dirname "$rc_path")"
  touch "$rc_path"
  path_line="export PATH=\"$bin_dir:\$PATH\""
  marker="# Added by TraeCLI installer"
  if ! grep -Fqs "$path_line" "$rc_path"; then
    printf '\n%s\n%s\n' "$marker" "$path_line" >>"$rc_path"
    printf 'Updated %s to include %s on PATH.\n' "$rc_path" "$bin_dir"
  fi
}

read_json_field() {
  python3 - "$1" "$2" <<'PY'
import json
import sys

path = sys.argv[1]
field = sys.argv[2]
with open(path, "r", encoding="utf-8") as handle:
    data = json.load(handle)
value = data
for part in field.split("."):
    if not isinstance(value, dict):
        value = ""
        break
    value = value.get(part, "")
if isinstance(value, bool):
    sys.stdout.write("1" if value else "0")
elif value is None:
    sys.stdout.write("")
else:
    sys.stdout.write(str(value))
PY
}

print_init_summary() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
verification = data.get("verification") or {}
print(f"Init mode: {data.get('mode') or 'unknown'}")
print(f"Selected app: {data.get('selected_app_path') or 'unknown'}")
print(f"Verification: {verification.get('status') or 'unknown'}")
for step in data.get("next_steps") or []:
    print(f"Next: {step}")
PY
}

print_status_summary() {
  python3 - "$1" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
print(f"Headless command visible: {'yes' if data.get('command_available') else 'no'}")
if data.get("command_error"):
    print(f"Bridge check: {data['command_error']}")
PY
}

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-init)
      run_init=0
      ;;
    --no-launch)
      launch_prepared_copy=0
      ;;
    --no-shell-rc)
      write_shell_rc=0
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

have_command python3 || fail "python3 is required"
[ -d "$harness_dir" ] || fail "agent-harness directory not found at $harness_dir"

printf 'Installing TraeCLI from %s\n' "$harness_dir"
mkdir -p "$(dirname "$venv_dir")" "$bin_dir"

if [ ! -x "$venv_dir/bin/python" ]; then
  printf 'Creating virtualenv at %s\n' "$venv_dir"
  python3 -m venv "$venv_dir"
fi

printf 'Installing Python package into %s\n' "$venv_dir"
"$venv_dir/bin/pip" install --upgrade "$harness_dir"

ln -sfn "$venv_dir/bin/traecli" "$bin_dir/traecli"
ln -sfn "$venv_dir/bin/cli-anything-trae" "$bin_dir/cli-anything-trae"
ensure_shell_path

traecli_cmd="$venv_dir/bin/traecli"
"$traecli_cmd" --help >/dev/null

printf 'Installed commands:\n'
printf '  %s\n' "$bin_dir/traecli"
printf '  %s\n' "$bin_dir/cli-anything-trae"

if [ "$run_init" -ne 1 ]; then
  printf 'Skipped `traecli init`. Run `%s init` when you are ready.\n' "$bin_dir/traecli"
  exit 0
fi

init_json=$(mktemp "${TMPDIR:-/tmp}/traecli-init.XXXXXX.json")
status_json=$(mktemp "${TMPDIR:-/tmp}/traecli-status.XXXXXX.json")
cleanup() {
  rm -f "$init_json" "$status_json"
}
trap cleanup EXIT INT TERM

printf 'Running `traecli init`...\n'
"$traecli_cmd" --json init >"$init_json"
print_init_summary "$init_json"

mode=$(read_json_field "$init_json" "mode")
selected_app_path=$(read_json_field "$init_json" "selected_app_path")
ready=$(read_json_field "$init_json" "ready")

if [ "$mode" = "prepared-copy" ] && [ "$launch_prepared_copy" -eq 1 ] && [ -n "$selected_app_path" ]; then
  if [ "$(uname -s)" = "Darwin" ] && have_command open; then
    printf 'Launching prepared app copy: %s\n' "$selected_app_path"
    open -na "$selected_app_path"
    sleep 3
    if "$traecli_cmd" --json headless status --ping >"$status_json" 2>/dev/null; then
      print_status_summary "$status_json"
    fi
  else
    printf 'Prepared app copy was created. Launch it manually: %s\n' "$selected_app_path"
  fi
fi

if [ "$ready" = "1" ]; then
  printf '\nTraeCLI is ready.\n'
else
  printf '\nTraeCLI install completed, but first-run verification still depends on Trae finishing startup.\n'
fi

printf 'Try it with:\n'
printf '  traecli "收到回复我"\n'
printf '  traecli -C /path/to/project "帮我看这个项目"\n'
printf '  traecli\n'

if ! path_contains_bin_dir; then
  printf 'If your current shell still cannot find `traecli`, run:\n'
  printf '  export PATH="%s:$PATH"\n' "$bin_dir"
fi
