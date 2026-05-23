# Test Plan

## Core coverage

- Path discovery and app metadata parsing
- Sanitized auth parsing from `storage.json`
- State inspection from `state.vscdb`
- Model catalog resolution from legacy `selected_model` plus session-relation model maps
- Model switching writes for `globalModelMap` and legacy `selected_model`
- MCP gallery and sandbox snapshot parsing
- Model cache extraction from runtime logs
- Optional live `ps` / `lsof` transport probe parsing
- Restricted-environment handling when `ps` / `lsof` exist but probing is blocked
- Chat turn extraction and ai-agent trace correlation from runtime logs
- Tool-run excerpt sanitization for ANSI / terminal control sequences
- Historical `trace show` lookup across older local log sessions

## End-to-end coverage

- `doctor --json`
- `init --json`
- root interactive entry with an initial prompt
- `exec --json`
- `exec --json` staying stateless without restoring or persisting CLI session history
- `resume --last` reusing the persisted hidden session id
- `resume` picker path without explicit session id
- `sessions --json`
- `sessions --show <session-id> --json`
- `state sessions --json` staying distinct from CLI session history
- picker `j/k/s/q` navigation
- alternate-screen saved-session/detail view rendering and `/back` navigation state
- alternate-screen raw-key input editing and immediate session navigation
- `--workspace /path` one-shot command dispatch
- `-C /path` Codex-style workspace alias
- untrusted workspace prompt on first `open/chat/repl` entry, backed by Trae GUI's `content.trust.model.key`
- `--trust-workspace` for non-interactive or scripted workspace entry that writes Trae GUI trust state
- `state models --json`
- `models current --json`
- `models list --json`
- `models set --json`
- `chat` auto fallback to the bundled `trae` executable when `--add-file` is used
- `chat --dispatch cdp --json` with a mocked CDP bridge result
- `chat --dispatch cdp` plain-text answer output
- `chat --dispatch cdp --json` with `--workspace` flowing into the bundled
  `trae --open-url` launch step
- `chat --dispatch cdp` reporting a stale saved target without auto-rebinding
- `chat --dispatch command --json` through the hidden `trae --open-url` relay
- `chat --dispatch uri --json` with a mocked deep-link launcher
- `chat --inspect --json` with a mocked trace result
- `chat --dispatch command --inspect --json` with mocked trace polling
- `chat --dispatch uri --inspect --json` with mocked deep-link launch and trace polling
- `chat --inspect` fallback output when no new turn is matched after dispatch
- bare-prompt argv rewriting such as `traecli "hello"`
- `logs tail` against a fake latest log session
- `rpc request --json` with a mocked direct AHA RPC bridge response
- `rpc chat --json` with a mocked raw AHA chat round-trip
- `rpc export-chat --json` with a mocked Markdown export result
- `rpc traces --json` for ai-agent stdout request reconstruction
- `trace chats --json`
- `trace show --json`
- `trace show` against a target in an older, non-latest log session
- `rpc transport --json --live`

## Release validation

- Fast path: `./scripts/release-check.sh`
- One-line remote install: `/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/bootstrap-install-traecli.sh)"`
- One-line uninstall: `/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/uninstall-traecli.sh)"`
- Validate the repo-local installer syntax: `sh -n ../scripts/install-traecli.sh`
- Create a clean virtualenv and run `pip install -e .`
- Verify both console scripts: `cli-anything-trae --help` and `traecli --help`
- Verify the default command path enters the REPL and exits cleanly
- In restricted or offline environments, prefer `python3 -m venv --system-site-packages`
  so the local `click` install can satisfy `pip install -e .` without fetching from the network

## Commands

```bash
../scripts/release-check.sh
python3 -m unittest discover -s cli_anything/trae/tests -v
sh -n ../scripts/install-traecli.sh
sh -n ../scripts/bootstrap-install-traecli.sh
sh -n ../scripts/uninstall-traecli.sh
sh -n ../scripts/release-check.sh
../scripts/install-traecli.sh --help
../scripts/bootstrap-install-traecli.sh --help
../scripts/uninstall-traecli.sh --help
python3 -m pip install -e .
cli-anything-trae doctor --json
cli-anything-trae init --json
cli-anything-trae models current --json
cli-anything-trae models list --agent-type dev_builder --json
cli-anything-trae models set GLM-4.7 --agent-type dev_builder --json
cli-anything-trae --json rpc request healthcheck ping
cli-anything-trae --json rpc chat "reply with OK only"
cli-anything-trae rpc export-chat <session-id> --connect-session-id <connect-id> --print-content
cli-anything-trae rpc traces --json --service chat --method get_messages --limit 5
cli-anything-trae rpc transport --json --live
cli-anything-trae trace chats --json --limit 5
cli-anything-trae trace show <frontend-message-id>
# `trace show` should print `Tool runs:` excerpts without ANSI noise and still
# resolve ids that are no longer in the newest log session
cli-anything-trae init
cli-anything-trae exec "reply with OK only"
cli-anything-trae sessions --json
cli-anything-trae sessions --show <session-id>
open -na /Applications/Trae\ CN.app --args --remote-debugging-port=9222
traecli "reply with OK only"
traecli sessions
traecli resume --last
traecli --no-alt-screen "reply with OK only"
cli-anything-trae --workspace /path/to/project chat --dispatch cdp "reply with OK only"
cli-anything-trae -C /path/to/project open .
cli-anything-trae --workspace /path/to/project open .
cli-anything-trae --workspace /path/to/project --trust-workspace --json open .
cli-anything-trae --json chat --dispatch cdp --answer-seconds 30 "reply with OK only"
cli-anything-trae --workspace /path/to/project chat --add-file README.md "summarize this repository"
cli-anything-trae --json chat --dispatch command --inspect --wait-seconds 10 "reply with OK only"
cli-anything-trae --json chat --dispatch uri --inspect --wait-seconds 10 "reply with OK only"
# Choose one:
python3 -m venv /tmp/traecli-deliverable-venv
# Offline / restricted variant:
python3 -m venv --system-site-packages /tmp/traecli-deliverable-venv
/tmp/traecli-deliverable-venv/bin/pip install -e .
/tmp/traecli-deliverable-venv/bin/cli-anything-trae --help
/tmp/traecli-deliverable-venv/bin/traecli --help
/tmp/traecli-deliverable-venv/bin/traecli
```
