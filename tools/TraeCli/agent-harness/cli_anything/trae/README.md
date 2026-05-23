# cli-anything-trae

## Preview

> `cli-anything-trae` and `traecli` are still preview entrypoints. Expect CLI
> and TUI behavior, installer defaults, and app automation behavior to keep
> moving while the project is being stabilized.

`cli-anything-trae` is a CLI-Anything harness for the Trae desktop app.

It wraps the installed app's real scriptable surfaces and inspects Trae's local
runtime state.
TraeCLI still depends on a running desktop Trae session.

Installed console scripts:

- `cli-anything-trae`
- `traecli`

## Install

One-line remote install:

```bash
/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/bootstrap-install-traecli.sh)"
```

This bootstrap path currently tracks the repository `main` branch. For a
repeatable install, use a local checkout or an explicit `TRAECLI_ARCHIVE_URL`.

One-line uninstall:

```bash
/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/uninstall-traecli.sh)"
```

From the repository root, the local fallback is:

```bash
./scripts/install-traecli.sh
./scripts/uninstall-traecli.sh
```

The installer creates `~/.trae/traecli-venv`, links `traecli` into
`~/.local/bin`, and runs `traecli init` unless `--skip-init` is passed.

Manual install from `agent-harness` is still supported:

```bash
python3 -m pip install -e .
```

For release validation from the repository root, run:

```bash
./scripts/release-check.sh
```

If `traecli` is still not found after install, check whether `pip` used a user base and add `$(python3 -m site --user-base)/bin` to `PATH`.

## Examples

```bash
cli-anything-trae doctor
cli-anything-trae init
cli-anything-trae -C /path/to/project open .
cli-anything-trae "summarize this repository"
cli-anything-trae exec "reply with OK only"
cli-anything-trae --json "reply with OK only"
cli-anything-trae sessions
cli-anything-trae sessions --show <session-id>
cli-anything-trae resume --last
cli-anything-trae --no-alt-screen "reply with OK only"
cli-anything-trae -C /path/to/project "summarize this repository"
cli-anything-trae --workspace /path/to/project chat "summarize this repository"
cli-anything-trae state sessions
cli-anything-trae state auth
cli-anything-trae state models --json
cli-anything-trae models current
cli-anything-trae models list --agent-type dev_builder
cli-anything-trae models set GLM-4.7 --agent-type dev_builder
cli-anything-trae logs list
cli-anything-trae logs tail --match ai-agent
cli-anything-trae rpc services
cli-anything-trae rpc activity
cli-anything-trae rpc traces --service chat --method get_messages --limit 5
cli-anything-trae rpc transport
cli-anything-trae rpc transport --live
cli-anything-trae --json rpc request healthcheck ping
cli-anything-trae --json rpc chat "收到回复我"
cli-anything-trae rpc export-chat <session-id> --connect-session-id <connect-id> --print-content
cli-anything-trae trace chats --limit 5
cli-anything-trae trace show <frontend-message-id>
cli-anything-trae --json rpc describe chat chat
cli-anything-trae chat "summarize this repository" --add-file README.md
open -na /Applications/Trae\ CN.app --args --remote-debugging-port=9222
traecli "收到回复我"
traecli exec "收到回复我"
traecli sessions
traecli sessions --show <session-id>
traecli resume --last
traecli -C /path/to/project "summarize this repository"
cli-anything-trae --workspace /path/to/project chat --dispatch cdp "收到回复我"
cli-anything-trae chat --dispatch cli "summarize this repository"
cli-anything-trae --json chat --dispatch cdp --answer-seconds 30 "reply with OK only"
cli-anything-trae --json chat --dispatch command --inspect --wait-seconds 10 "reply with OK only"
cli-anything-trae --json chat --dispatch uri --inspect --wait-seconds 10 "reply with OK only"
cli-anything-trae mcp gallery
cli-anything-trae
```

## Notes

- The default app target is CN-first: it prefers `/Applications/Trae CN.app` and falls back to `/Applications/Trae.app`; you can always override this with `--app-path` or `TRAECLI_APP_PATH`
- The harness resolves the bundled CLI script by bundle layout, so `Trae CN.app` uses `bin/trae-cn` while `Trae.app` uses `bin/trae`
- `init` is the recommended bootstrap path: it installs the bridge and writes the selected defaults to `~/.trae/traecli.json`
- `traecli` now starts an interactive session by default, and `traecli "收到回复我"` starts that interactive session with an initial prompt
- Interactive sessions render in an alternate screen when attached to a real TTY; the screen now has dedicated `chat`, `sessions`, `detail`, and `output` views plus a live input bar. Use `--no-alt-screen` to keep inline terminal scrollback
- The main `chat` panel now follows a Codex-like full-width, bottom-anchored transcript layout: the latest turns stay near the prompt, older content scroll upward, and `Up` / `Down` scroll the transcript when the input is empty
- The empty chat state and non-alt-screen REPL bootstrap now render a compact ASCII icon card instead of a wide text banner, so startup feels closer to the icon-first style used by tools like opencode
- Inside the alternate-screen session, typing `/` opens a live slash palette in the side rail; keep typing to filter commands and settings, use `Up` / `Down` to select, then press `Enter` to run or fill the selected item. The palette keeps Chinese descriptions so slash discovery still reads naturally in Chinese locales
- The slash surface now exposes Codex-style shortcuts including `/init`, `/status`, `/diff`, `/prompts`, `/model`, and `/approvals`, adapted to TraeCLI's local workspace/runtime model
- `traecli exec "收到回复我"` is the explicit stateless one-shot path: it does not restore saved CLI context and does not write to `~/.trae/traecli-sessions.json`; `traecli --json "收到回复我"` is rewritten to that non-interactive path automatically
- `resume --last` reopens the most recent resumable conversation for the current workspace
- `resume` without arguments opens a simple numbered picker for saved resumable sessions in the current workspace
- `sessions` lists saved local CLI session history from `~/.trae/traecli-sessions.json`
- `sessions --show <session-id>` prints the saved session transcript summary and recent messages
- `state sessions` is different: it inspects Trae's local app session state rather than the CLI session history file
- `state models` now reports both the legacy `selected_model` record and the resolved current model per agent bucket (`dev_builder`, `solo_coder`, `solo_builder`)
- `models current` shows the current resolved model per agent bucket, `models list` exposes the available catalog derived from Trae's local `model_list_map`, and `models set` updates the shared GUI model mapping in `state.vscdb`
- Inside the alternate-screen session, `/sessions` opens the saved-session browser, `Up` / `Down` or `j` / `k` move the cursor immediately, `Enter` or `o` / `s` opens the selected detail view, and `Esc` or `/back` returns to the previous screen
- Inside the interactive session, `/models current`, `/models list`, and `/models set <model>` are available through both slash search and direct typed commands
- `/init` creates missing `AGENTS.md` and `memory.md` files in the current workspace without overwriting existing copies, so repository rules and progress tracking can be bootstrapped from the slash menu
- `/status` summarizes the current workspace, saved-session count, model, trust state, and whether `AGENTS.md` / `memory.md` are present; `/diff` shows a local git change summary; `/prompts` prints starter prompt ideas; `/model` and `/approvals` provide compact status views for the selected model and trust context
- `-C /path/to/project` is an alias for `--workspace /path/to/project` so project targeting feels closer to Codex CLI
- Use `--app-path` and `--support-dir` to point the harness at another install
- `--json` is supported on every subcommand
- `rpc *` commands reverse-engineer bundled service metadata and recent log traffic
- `rpc traces` reconstructs per-request ai-agent stdout traces, including `chat.get_messages` counts, response sizes, and `query_history_state` enrichment failures
- `rpc transport` classifies the current AHA IPC / gRPC / OAuth local endpoints
- `rpc transport --live` additionally probes current Trae helper processes with `ps` and `lsof`
- `rpc transport --live` reports when `ps` or `lsof` are blocked by sandbox/macOS permissions so partial live data is explicit
- `rpc request` is an experimental direct AHA RPC bridge that reuses Trae-bundled `@aha-kit/ipc` and `@aha-kit/rpc`
- `rpc chat` is an experimental raw AHA chat path: it auto-runs `project.create_project`, `chat.create_session`, `chat.chat`, and `chat.get_messages` for the current workspace
- `rpc export-chat` wraps Trae's internal `chat.export_past_chat` path and can print the exported Markdown transcript of a completed session
- `rpc request`, `rpc chat`, and `rpc export-chat` now normalize both known response shapes: direct `code/data` payloads and Electron bridge `response.params.code/data` envelopes
- `rpc request`, `rpc chat`, and `rpc export-chat` require a live local `ai-agent` AHA socket; if Trae is not running, they fail explicitly
- `rpc chat` automatically injects local auth-derived `user_info` and supports `--client-info-json` for fields such as `project_id`, but the current assistant answer is still reverse-engineered and may come back as selection/codeblock-shaped content instead of a clean natural-language final answer
- `--json` output now redacts auth tokens plus `encrypted_model_params` and similar sensitive model-session fields discovered in raw RPC responses
- `chat` is the session-aware non-interactive send path: unless `--new-chat` is used, it may continue the latest saved context for the current workspace
- `exec` is the Codex-style explicit stateless one-shot path: it does not restore saved context and does not persist CLI session history
- Interactive sessions persist their saved session ids to `~/.trae/traecli-sessions.json`, so `resume --last` can continue the latest conversation across shell invocations
- The resume picker now supports `j` / `k` movement, `s` to select the current row, number shortcuts, and `q` to cancel
- Inside the interactive session, `/session detail` opens the current session detail view, `/sessions show <id>` opens another saved session detail view, `/back` returns to the prior screen, `/chat` jumps back to conversation mode, and the live input bar supports `Left` / `Right`, `Home`, `End`, `Backspace`, and `Delete`
- `chat --dispatch cdp` is an explicit diagnostic send path: it opens Trae's side chat through the hidden command URI, then drives the visible DOM through Chrome DevTools Protocol and prints the captured assistant text to stdout
- `chat --dispatch cdp` does not auto-relaunch Trae or auto-rebind a missing target by default; if the debugger endpoint or saved target is wrong, it fails fast with guidance
- `chat --dispatch cdp` requires Trae to be started with a remote debugging port, for example `open -na /Applications/Trae\ CN.app --args --remote-debugging-port=9222`
- `--launch-debug` is opt-in on `chat --dispatch cdp` when you explicitly want the CLI to try launching Trae with a remote debugging port
- `chat --dispatch cdp` supports `--workspace` for the hidden `trae --open-url` launch step, so the bundled CLI still inherits the target project cwd
- `chat --dispatch command` uses Trae's hidden `trae --open-url -- command:...` path to invoke the internal side-chat command more directly than the deep link path
- `chat --dispatch command` currently supports the same prompt-only surface as `chat --dispatch uri`
- `chat --dispatch uri` launches Trae via its side-chat deep link instead of `trae chat`
- `chat --dispatch uri` currently supports prompt submission only; bundled CLI flags like `--add-file` stay on the `cli` path
- `trace show` and `chat --inspect` surface a `first_token_preview` when Modular logs expose the first reply chunk
- `trace show` searches local Trae log sessions until it finds the requested message, session, trace, or task id
- `trace show` reconstructs `Tool runs:` with command lines, exit codes, and cleaned terminal output excerpts when renderer logs expose them
- The bundled Trae desktop app still does not expose a documented official live "stream answer to stdout" API surface; current automation paths are reverse-engineered and remain GUI-bound
- The default command starts an interactive CLI session; inside it, plain text sends a chat prompt directly and slash-prefixed lines stay as control commands
