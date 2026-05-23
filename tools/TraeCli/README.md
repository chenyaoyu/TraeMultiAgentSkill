# TraeCli

## Preview

> `TraeCli` is still in preview. The CLI surface, installer defaults, and app
> automation paths are still being stabilized and may change between commits.
>
> This repository is not an official Trae release channel. If you need a
> repeatable setup, prefer a local checkout or an explicit archive URL instead
> of tracking the one-line bootstrap on `main`.

`TraeCli` is a lightweight Python CLI harness for the Trae desktop app, with
`Trae CN` as the default target when both CN and global builds are installed.

The actual implementation lives under [agent-harness](./agent-harness). It wraps
Trae's real local surfaces.
TraeCLI still depends on a running desktop Trae session.

## Features

- `traecli` and `cli-anything-trae` console entrypoints
- interactive-by-default entrypoint with Codex CLI style `exec`, `resume`, and bare-prompt startup
- lightweight alternate-screen session UI with dedicated `chat`, `sessions`, `detail`, and `output` views
- runtime inspection for auth, model state, logs, RPC traffic, and chat traces
- bridge-assisted command execution inside the running Trae instance
- experimental chat automation paths through the installed app

## Preview Install

Install in one line:

```bash
/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/bootstrap-install-traecli.sh)"
traecli "reply with OK only"
```

The one-line bootstrap currently follows `main`. For a pinned remote install,
override `TRAECLI_ARCHIVE_URL`; otherwise use a local checkout.

Uninstall in one line:

```bash
/bin/sh -c "$(curl -fsSL https://raw.githubusercontent.com/firerlAGI/TraeCli/main/scripts/uninstall-traecli.sh)"
```

The installer creates a dedicated virtualenv at `~/.trae/traecli-venv`, links
`traecli` and `cli-anything-trae` into `~/.local/bin`, and runs `traecli init`
by default. Use `./scripts/install-traecli.sh --skip-init` if you only want the
CLI binaries first. If you want a repo-local fallback instead of the remote
bootstrap, the equivalent commands are `./scripts/install-traecli.sh` and
`./scripts/uninstall-traecli.sh`.

Manual repo-local install is still available:

```bash
cd agent-harness
python3 -m pip install -e .
traecli --help
```

Repeatable release validation is available from the repo root:

```bash
./scripts/release-check.sh
```

## Interactive TUI

When `traecli` is attached to a real TTY, it opens an alternate-screen session UI.

- chat view: type directly into the live input bar and press Enter to send
- sessions view: `/sessions`, then `Up` / `Down` or `j` / `k` to move, `Enter` or `o` / `s` to open detail
- detail or output view: `Esc` or `/back` returns to the previous screen, `/chat` jumps back to conversation
- input editing: `Left` / `Right`, `Home`, `End`, `Backspace`, and `Delete` work inside the live input bar
- disable the full-screen UI with `--no-alt-screen`

## Repository Layout

- [agent-harness/cli_anything/trae/README.md](./agent-harness/cli_anything/trae/README.md): user-facing CLI usage
- [agent-harness/TRAE.md](./agent-harness/TRAE.md): confirmed Trae runtime/backend notes
- [agent-harness/TEST.md](./agent-harness/TEST.md): test plan and validation commands
- [CONTRIBUTING.md](./CONTRIBUTING.md): contribution and commit conventions
