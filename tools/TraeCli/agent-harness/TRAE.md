# Trae CLI Harness

This harness wraps the scriptable surfaces that Trae actually exposes today and
documents the ones that remain GUI-bound. The current default target is
`Trae CN`, with the global build remaining available through `--app-path`.
Trae remains desktop GUI-bound in this harness as well.

## Confirmed backend surfaces

- Bundled editor CLI: `Contents/Resources/app/bin/trae-cn` on `Trae CN`,
  `Contents/Resources/app/bin/trae` on the global build
- Hidden CLI URL relay: `trae-cn --open-url -- <url>` on `Trae CN`
- URL scheme: `trae-cn://` on `Trae CN`, `trae://` on the global build
- Product config: `Contents/Resources/app/product.json`
- User state database: `User/globalStorage/state.vscdb`
- Auth/config cache: `User/globalStorage/storage.json`
- Runtime logs: `logs/<session>/...`
- Modular runtime data: `ModularData/ai-agent`, `ModularData/ckg_server`
- MCP gallery cache: `User/globalStorage/.mcp_gallery_cache`
- Sandbox permission snapshots: `ModularData/ai-agent/sandbox/*.json`

## What this harness can do

- Proxy real `trae-cn` and `trae-cn chat` invocations on the default CN path
- Proxy the global bundle's `trae` invocations when `--app-path /Applications/Trae.app` is selected
- Accept a bare one-shot prompt such as `traecli "收到回复我"` and rewrite it to the chat path
- Dispatch a side-chat prompt through Trae's reverse-engineered deep link
  `trae://trae.ai-ide/side-chat?query=...`
- Dispatch a side-chat prompt through Trae's hidden command-URI relay
  `trae --open-url -- 'command:workbench.action.chat.icube.open?...'`
- Bootstrap the local bridge and preferred app path with `traecli init`, which
  also persists defaults to `~/.trae/traecli.json`
- Submit a side-chat prompt and capture the rendered answer text through
  Chrome DevTools Protocol DOM automation with `chat --dispatch cdp`
- Inspect local auth, model selection, session badges, MCP gallery entries
- Resolve current model selection through Trae's newer
  `ai-chat:sessionRelation:globalModelMap` / `globalModeMap` state, not only the
  legacy `AI.agent.model.selected_model` record
- Update model selection by writing the shared GUI model map in `state.vscdb`,
  with optional `workbench.action.reloadWindow` dispatch for faster pickup
- Inspect latest runtime logs and model cache summaries
- Inspect sandbox snapshots and modular runtime metadata
- Extract bundled RPC services and method inventories from `ai-modules-chat`
- Summarize real `TransportManager` traffic from renderer logs
- Reconstruct ai-agent stdout RPC traces, including `chat.get_messages`
  counts, turn counts, response sizes, and `query_history_state`
  enrichment status
- Send experimental real AHA RPC envelopes through Trae's own bundled
  `@aha-kit/ipc` + `@aha-kit/rpc` stack with `rpc request`
- Export a completed chat transcript through Trae's internal
  `chat.export_past_chat` RPC and read the generated Markdown back with
  `rpc export-chat`
- Recover recent `project_id`, `session_id`, and `message_id` context from logs
- Extract recent chat turns from logs and, when available, correlate renderer
  `message_id` values with ai-agent `trace_id`, `task_id`, and backend
  `message_id`
- Search across local log sessions for a specific chat turn id with `trace show`
- Reconstruct renderer-observed tool commands, exit codes, and cleaned output
  excerpts from `ToolingTerminalTrace` and `[tooling] ... result:` lines
- Return a `chat --inspect` diagnostic even when no new turn matches the
  dispatch window, including the latest observed local turn for debugging
- Classify the live transport topology from logs, including AHA IPC, CKG gRPC,
  and the local OAuth callback port
- Optionally correlate the current Electron, ai-agent, and ckg helper processes
  with `rpc transport --live`
- Run as a stateful REPL with wrapper-side undo/redo for session settings

## Current limitations

- The installed desktop bundle still does not expose an official documented
  "ask and stream answer to stdout" API surface. Current automation paths are
  reverse-engineered bridges layered on top of internal, unsupported behavior
- `chat --dispatch cdp` requires Trae to be launched with a remote debugging
  port, for example
  `open -na /Applications/Trae\ CN.app --args --remote-debugging-port=9222`
- The app-level RPC transport is still behind Trae's internal AHA/Electron
  client layer. Current log evidence points to `doRequest` going over local AHA
  IPC to `ai-agent`, where a `jsonrpsee` server handles service/method routing
  by `channel_id`
- In the installed desktop bundle, the local AHA layer is wired through
  Electron's built-in `ahaIpc.connect/serve` API rather than a documented public
  TCP listener
- The new direct `rpc request` / `rpc export-chat` bridge depends on a live local
  `ai-agent` AHA socket and the host having `node` available
- Node-side and remote extension-host logs do show `@aha-kit/ipc` style
  `ipc://.../aha/<service>.sock` addresses, so named socket paths are confirmed
  for that side of the stack even though the local desktop helper does not
  expose an obvious named `ai-agent.sock` through `lsof`
- `rpc transport --live` depends on local `ps` and `lsof` availability and is
  intended as a best-effort runtime probe rather than a guaranteed API surface
- Chat trace correlation is also best-effort: some completed turns only expose
  renderer-side lifecycle events, so `trace chats` may show status/model/tool
  counts without a matching ai-agent `trace_id` or `task_id`
- Port `51002` is the CKG sidecar gRPC server, not the generic Trae request
  envelope endpoint
- Port `17790` in the inspected session is a `SupabaseOAuthLocalServer` callback
  port, not an RPC listener
- Modular data stores for `ai-agent` and `ckg_server` are opaque binary blobs in
  this installation and are not queried directly
- The exposed deep link path is intentionally narrow: this harness currently
  supports prompt submission plus optional `newChat=true`, but not file
  attachments or bundled `trae chat` window-management flags on the URI path
- The hidden `command:` relay is promising and does reach the internal chat
  command path, but by itself it still does not export the final rendered
  answer; `chat --dispatch cdp` is the layer that reads the DOM back
- Finished-session export is now exposed experimentally via `rpc export-chat`,
  while direct AHA live answer streaming remains unavailable in this install
- The CDP bridge depends on selector heuristics derived from the current Trae
  renderer DOM. Future desktop updates may require selector refreshes
