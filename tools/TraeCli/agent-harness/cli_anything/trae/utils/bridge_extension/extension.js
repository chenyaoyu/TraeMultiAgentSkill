"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const vscode = require("vscode");

const EXTENSION_ID = "traecli.headless-bridge";
const STATE_FILE_NAME = "traecli-headless-bridge.json";
const STATE_INSTANCES_DIR_NAME = "instances";
const SERIALIZE_MAX_DEPTH = 16;

let outputChannel;
let server;
let stateToken;
let serverPort;
let startedAt;
let statePath;
let instanceStatePath;
let bridgeInstanceId;
let activeContext;
let managerExchangePromise;
let managerExchangeReconnectTimer;
let managerExchangeRawConnection;
let managerExchangeConnection;
let managerExchangeSessionId;
let managerExchangeConnectedAt;
let managerExchangeLastError;
let managerExchangeStatus = "idle";

const VSCODE_SURFACE_NAMESPACES = [
  "icube",
  "icubeAI",
  "icubeBootConfig",
  "icubeContextState",
  "icubeGit",
  "icubeNative",
  "icubeNet",
  "icubeUser",
];

function log(message, ...rest) {
  if (!outputChannel) {
    return;
  }
  outputChannel.appendLine(
    `[${new Date().toISOString()}] ${message}${rest.length ? ` ${rest.join(" ")}` : ""}`,
  );
}

async function ensureStateDir() {
  const stateDir = path.dirname(getStatePath());
  await fs.promises.mkdir(stateDir, { recursive: true });
  await fs.promises.mkdir(path.dirname(getInstanceStatePath()), { recursive: true });
}

function getStatePath() {
  if (!statePath) {
    throw new Error("Bridge state path has not been initialized.");
  }
  return statePath;
}

function getInstanceStatePath() {
  if (!instanceStatePath) {
    throw new Error("Bridge instance state path has not been initialized.");
  }
  return instanceStatePath;
}

function serializeValue(value, depth = 0, seen = new WeakSet()) {
  if (value === null || value === undefined) {
    return value;
  }
  if (depth >= SERIALIZE_MAX_DEPTH) {
    return `[max-depth:${typeof value}]`;
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "bigint") {
    return value.toString();
  }
  if (typeof value === "function") {
    return `[function:${value.name || "anonymous"}]`;
  }
  if (value instanceof Error) {
    return {
      name: value.name,
      message: value.message,
      stack: value.stack || null,
    };
  }
  if (Array.isArray(value)) {
    return value.map(item => serializeValue(item, depth + 1, seen));
  }
  if (typeof value === "object") {
    if (seen.has(value)) {
      return "[circular]";
    }
    seen.add(value);
    const result = {};
    for (const [key, item] of Object.entries(value)) {
      result[key] = serializeValue(item, depth + 1, seen);
    }
    return result;
  }
  return String(value);
}

function stringifyJsonValue(value) {
  try {
    const serialized = JSON.stringify(value);
    if (typeof serialized === "string") {
      return serialized;
    }
  } catch {}
  return JSON.stringify(serializeValue(value));
}

function parseJsonArgument(value) {
  if (typeof value !== "string") {
    return value;
  }
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

function managerExchangeSnapshot() {
  return {
    status: managerExchangeStatus,
    connect_session_id: managerExchangeSessionId || null,
    connected_at: managerExchangeConnectedAt || null,
    last_error: managerExchangeLastError || null,
  };
}

function resetManagerExchangeState() {
  managerExchangePromise = undefined;
  managerExchangeConnection = undefined;
  managerExchangeRawConnection = undefined;
  managerExchangeSessionId = undefined;
  managerExchangeConnectedAt = undefined;
}

function scheduleManagerExchangeReconnect() {
  if (!activeContext || managerExchangeReconnectTimer) {
    return;
  }
  managerExchangeReconnectTimer = setTimeout(() => {
    managerExchangeReconnectTimer = undefined;
    startManagerExchange(activeContext).catch(error => {
      log(
        "manager exchange reconnect failed",
        error && error.stack ? error.stack : String(error),
      );
      scheduleManagerExchangeReconnect();
    });
  }, 1000);
}

async function getAhaConnectionCtor() {
  let electron;
  try {
    electron = require("electron");
  } catch (error) {
    throw new Error(
      `Extension host cannot load \`electron\`: ${
        error && error.message ? error.message : String(error)
      }`,
    );
  }
  if (!electron?.ahaIpc || typeof electron.ahaIpc.connect !== "function") {
    throw new Error("`electron.ahaIpc.connect` is not available in the extension host.");
  }
  const appRoot = vscode.env.appRoot;
  if (!appRoot) {
    throw new Error("`vscode.env.appRoot` is unavailable.");
  }
  const rpcPath = path.join(appRoot, "node_modules", "@aha-kit", "rpc", "dist", "index.cjs");
  if (!fs.existsSync(rpcPath)) {
    throw new Error(`Trae RPC module not found: ${rpcPath}`);
  }
  const { Connection } = require(rpcPath);
  return { electron, appRoot, Connection };
}

async function handleManagerExchangeExecuteCommand(payload, sessionId) {
  const channelId = payload && typeof payload === "object" ? payload.channel_id || null : null;
  const request = payload && typeof payload === "object" ? payload.params || {} : {};
  const commandId = String(request.command_id || "").trim();
  const timeoutMs = Number(request.timeout_ms || 0);
  const args = Array.isArray(request.args) ? request.args.map(parseJsonArgument) : [];
  let result;
  let error;

  if (!commandId) {
    error = new Error("Missing `command_id` in execute_command payload.");
  } else {
    try {
      const execution = Promise.resolve(vscode.commands.executeCommand(commandId, ...args));
      result =
        timeoutMs > 0
          ? await withTimeout(execution, timeoutMs, `Command ${commandId}`)
          : await execution;
    } catch (requestError) {
      error = requestError;
    }
  }

  if (error) {
    log(
      `[CommandAdapter][execute_command] command=${commandId || "<missing>"} session=${sessionId || "<unknown>"} error`,
      error && error.stack ? error.stack : String(error),
    );
  }

  return {
    results: {
      stream_id: channelId,
      msg_type: "execute_command_result",
      error: error ? String(error && error.message ? error.message : error) : undefined,
      payload: stringifyJsonValue(result),
    },
    base_resp: {
      status_message: "ok",
      status_code: 0,
      extra: null,
    },
  };
}

async function startManagerExchange(context) {
  if (managerExchangePromise) {
    return managerExchangePromise;
  }

  managerExchangePromise = (async () => {
    managerExchangeStatus = "starting";
    managerExchangeLastError = null;
    await writeStateFile(context);

    const { electron, Connection } = await getAhaConnectionCtor();
    const sessionId = await vscode.commands.executeCommand("icube.cloudide.aiSessionID");
    const normalizedSessionId = String(sessionId || "").trim();
    if (!normalizedSessionId) {
      throw new Error("`icube.cloudide.aiSessionID` returned an empty value.");
    }

    const rawConnection = await Promise.resolve(electron.ahaIpc.connect("ai-agent"));
    const connection = new Connection(createAhaChannel(rawConnection));
    connection.onRequest("execute_command", payload =>
      handleManagerExchangeExecuteCommand(payload, normalizedSessionId),
    );

    rawConnection.on?.("disconnect", () => {
      log("manager exchange disconnected");
      managerExchangeStatus = "disconnected";
      managerExchangeLastError = {
        message: "AHA IPC disconnected",
      };
      resetManagerExchangeState();
      writeStateFile(context).catch(() => {});
      scheduleManagerExchangeReconnect();
    });
    rawConnection.on?.("error", error => {
      log(
        "manager exchange error",
        error && error.stack ? error.stack : String(error),
      );
    });

    managerExchangeRawConnection = rawConnection;
    managerExchangeConnection = connection;
    managerExchangeSessionId = normalizedSessionId;
    managerExchangeConnectedAt = new Date().toISOString();
    managerExchangeLastError = null;
    managerExchangeStatus = "ready";
    await writeStateFile(context);
    log(`manager exchange ready session=${normalizedSessionId}`);
  })().catch(async error => {
    managerExchangeStatus = "error";
    managerExchangeLastError = serializeValue(error);
    resetManagerExchangeState();
    await writeStateFile(context).catch(() => {});
    throw error;
  });

  return managerExchangePromise;
}

function getWorkspaceFolders() {
  return (vscode.workspace.workspaceFolders || []).map(folder => ({
    name: folder.name,
    uri: folder.uri.toString(),
    fsPath: folder.uri.fsPath,
  }));
}

function buildSnapshot(context) {
  const activeEditor = vscode.window.activeTextEditor;
  return {
    instance_id: bridgeInstanceId,
    extension_id: context.extension.id,
    extension_version: context.extension.packageJSON.version,
    host: "127.0.0.1",
    port: serverPort,
    token: stateToken,
    pid: process.pid,
    started_at: startedAt,
    last_updated_at: new Date().toISOString(),
    app_name: vscode.env.appName,
    language: vscode.env.language,
    remote_name: vscode.env.remoteName || null,
    ui_kind: String(vscode.env.uiKind),
    window_focused: Boolean(vscode.window.state?.focused),
    state_path: getInstanceStatePath(),
    workspace_folders: getWorkspaceFolders(),
    connect_session_id: managerExchangeStatus === "ready" ? managerExchangeSessionId || null : null,
    manager_exchange: managerExchangeSnapshot(),
    active_editor: activeEditor
      ? {
          uri: activeEditor.document.uri.toString(),
          fsPath: activeEditor.document.uri.fsPath,
          languageId: activeEditor.document.languageId,
        }
      : null,
  };
}

async function writeStateFile(context) {
  await ensureStateDir();
  const payload = buildSnapshot(context);
  await fs.promises.writeFile(getStatePath(), JSON.stringify(payload, null, 2));
  await fs.promises.writeFile(getInstanceStatePath(), JSON.stringify(payload, null, 2));
  return payload;
}

async function removeStateFile(targetPath) {
  if (!targetPath) {
    return;
  }
  try {
    await fs.promises.unlink(targetPath);
  } catch {}
}

async function cleanupStateFiles() {
  const legacyPath = statePath;
  const instancePath = instanceStatePath;
  if (legacyPath) {
    try {
      const payload = JSON.parse(await fs.promises.readFile(legacyPath, "utf8"));
      if (payload && payload.instance_id === bridgeInstanceId) {
        await removeStateFile(legacyPath);
      }
    } catch {}
  }
  await removeStateFile(instancePath);
}

function isAuthorized(request) {
  const headerToken = request.headers["x-traecli-token"];
  if (typeof headerToken === "string" && headerToken === stateToken) {
    return true;
  }
  const authorization = request.headers.authorization;
  if (typeof authorization === "string" && authorization === `Bearer ${stateToken}`) {
    return true;
  }
  return false;
}

function sendJson(response, statusCode, payload) {
  response.writeHead(statusCode, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(`${JSON.stringify(payload)}\n`);
}

function readBody(request) {
  return new Promise((resolve, reject) => {
    let chunks = "";
    request.setEncoding("utf8");
    request.on("data", chunk => {
      chunks += chunk;
      if (chunks.length > 1024 * 1024) {
        reject(new Error("request body too large"));
      }
    });
    request.on("end", () => resolve(chunks));
    request.on("error", reject);
  });
}

function withTimeout(promise, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label} timed out after ${timeoutMs}ms`));
    }, timeoutMs);
    promise.then(
      value => {
        clearTimeout(timer);
        resolve(value);
      },
      error => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function createAhaChannel(rawConnection) {
  return {
    send(data) {
      rawConnection.send(data);
      return Promise.resolve();
    },
    onData(callback) {
      const handler = data => callback(data);
      rawConnection.on("message", handler);
      return {
        dispose() {
          rawConnection.off?.("message", handler);
        },
      };
    },
  };
}

function cloneJsonValue(value) {
  return JSON.parse(JSON.stringify(value));
}

function normalizeManagerExchangeEnvelope(envelope) {
  const normalized =
    envelope && typeof envelope === "object" ? cloneJsonValue(envelope) : {};
  if (!managerExchangeSessionId) {
    return normalized;
  }
  normalized.session_id = managerExchangeSessionId;
  if (!normalized.params || typeof normalized.params !== "object") {
    normalized.params = {};
  }
  if (!normalized.params.client_info || typeof normalized.params.client_info !== "object") {
    normalized.params.client_info = {};
  }
  normalized.params.client_info.connect_session_id = managerExchangeSessionId;
  return normalized;
}

function resolveVscodePath(pathExpression) {
  const rawPath = String(pathExpression || "").trim();
  if (!rawPath) {
    return {
      found: true,
      path: "",
      parent: null,
      value: vscode,
    };
  }
  const segments = rawPath.split(".").filter(Boolean);
  let parent = null;
  let value = vscode;
  for (const segment of segments) {
    if (value === null || value === undefined) {
      return {
        found: false,
        path: rawPath,
        parent,
        value: undefined,
        error: `Path segment \`${segment}\` resolved against nullish value.`,
      };
    }
    parent = value;
    try {
      value = value[segment];
    } catch (error) {
      return {
        found: false,
        path: rawPath,
        parent,
        value: undefined,
        error:
          error && error.message ? error.message : String(error),
      };
    }
  }
  return {
    found: true,
    path: rawPath,
    parent,
    value,
  };
}

function describeValue(value) {
  const description = {
    type: value === null ? "null" : typeof value,
    is_array: Array.isArray(value),
    is_null: value === null,
  };
  if (value === null || value === undefined) {
    description.value = value;
    return description;
  }
  if (
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    typeof value === "bigint"
  ) {
    description.value = serializeValue(value);
    return description;
  }
  if (typeof value === "function") {
    description.name = value.name || null;
    description.length = value.length;
    return description;
  }
  const keys = [];
  const members = [];
  for (const key of Object.keys(value).sort()) {
    keys.push(key);
    try {
      const member = value[key];
      members.push({
        name: key,
        type: member === null ? "null" : typeof member,
        is_function: typeof member === "function",
      });
    } catch (error) {
      members.push({
        name: key,
        type: "error",
        is_function: false,
        error: error && error.message ? error.message : String(error),
      });
    }
  }
  description.keys = keys;
  description.member_count = keys.length;
  description.members = members;
  return description;
}

async function handleVSCodeSurface(url) {
  const targetPath = (url.searchParams.get("path") || "").trim();
  if (targetPath) {
    const resolved = resolveVscodePath(targetPath);
    if (!resolved.found) {
      throw new Error(`VS Code path not found: ${targetPath}. ${resolved.error || ""}`.trim());
    }
    return {
      ok: true,
      path: targetPath,
      description: describeValue(resolved.value),
    };
  }
  const namespaces = {};
  for (const namespaceName of VSCODE_SURFACE_NAMESPACES) {
    const resolved = resolveVscodePath(namespaceName);
    namespaces[namespaceName] = resolved.found
      ? describeValue(resolved.value)
      : {
          type: "undefined",
          is_array: false,
          is_null: false,
          error: resolved.error || "not found",
        };
  }
  return {
    ok: true,
    namespaces,
  };
}

async function handleVSCodeApi(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    throw new Error("Expected a JSON object body.");
  }
  const targetPath = String(requestBody.path || "").trim();
  const args = Array.isArray(requestBody.args) ? requestBody.args : [];
  if (!targetPath) {
    throw new Error("Missing `path`.");
  }
  const resolved = resolveVscodePath(targetPath);
  if (!resolved.found) {
    throw new Error(`VS Code path not found: ${targetPath}. ${resolved.error || ""}`.trim());
  }
  let result = resolved.value;
  let invocationType = "property";
  if (typeof resolved.value === "function") {
    invocationType = "function";
    result = await Promise.resolve(resolved.value.apply(resolved.parent, args));
  }
  return {
    ok: true,
    path: targetPath,
    invocation_type: invocationType,
    arg_count: args.length,
    result: serializeValue(result),
  };
}

async function handleICubeRpc(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    throw new Error("Expected a JSON object body.");
  }
  const icube = vscode.icube;
  if (!icube || typeof icube.getWebSocketRpcClient !== "function") {
    throw new Error("`vscode.icube.getWebSocketRpcClient` is unavailable.");
  }
  const clientUrl = String(requestBody.client_url || "").trim();
  const clientOptions =
    requestBody.client_options && typeof requestBody.client_options === "object"
      ? requestBody.client_options
      : {};
  const rpcMethod = String(requestBody.method || "").trim();
  const args = Array.isArray(requestBody.args) ? requestBody.args : [];
  const closeAfter = requestBody.close_after !== false;
  if (!clientUrl) {
    throw new Error("Missing `client_url`.");
  }
  if (!rpcMethod) {
    throw new Error("Missing `method`.");
  }

  const client = await icube.getWebSocketRpcClient(clientUrl, clientOptions);
  if (!client || typeof client[rpcMethod] !== "function") {
    try {
      await Promise.resolve(client?.close?.());
    } catch {}
    throw new Error(`RPC client method not available: ${rpcMethod}`);
  }

  try {
    const result = await Promise.resolve(client[rpcMethod](...args));
    return {
      ok: true,
      client_url: clientUrl,
      client_options: serializeValue(clientOptions),
      method: rpcMethod,
      arg_count: args.length,
      result: serializeValue(result),
    };
  } finally {
    if (closeAfter) {
      try {
        await Promise.resolve(client.close?.());
      } catch {}
    }
  }
}

function createProbeLogger(capture) {
  return {
    info(...args) {
      capture.log.push({
        level: "info",
        args: serializeValue(args),
      });
    },
    debug(...args) {
      capture.log.push({
        level: "debug",
        args: serializeValue(args),
      });
    },
    warn(...args) {
      capture.log.push({
        level: "warn",
        args: serializeValue(args),
      });
    },
    error(...args) {
      capture.log.push({
        level: "error",
        args: serializeValue(args),
      });
    },
  };
}

function createProbeWebSocketCtor(capture) {
  class ProbeWebSocket {
    constructor(url, protocols) {
      this.url = url;
      this.protocol = Array.isArray(protocols)
        ? String(protocols[0] || "")
        : String(protocols || "");
      this.extensions = "";
      this.binaryType = "arraybuffer";
      this.readyState = ProbeWebSocket.CONNECTING;
      this._listeners = new Map();
      capture.connections.push({
        url,
        protocols: Array.isArray(protocols)
          ? protocols.slice()
          : protocols === undefined
            ? []
            : [protocols],
        protocol: this.protocol,
        constructed_at: new Date().toISOString(),
      });
      setTimeout(() => {
        if (this.readyState !== ProbeWebSocket.CONNECTING) {
          return;
        }
        this.readyState = ProbeWebSocket.OPEN;
        this._dispatch("open", {
          type: "open",
        });
      }, 0);
    }

    addEventListener(type, listener, options) {
      const entries = this._listeners.get(type) || [];
      entries.push({
        listener,
        once: Boolean(options && typeof options === "object" ? options.once : options),
      });
      this._listeners.set(type, entries);
    }

    removeEventListener(type, listener) {
      const entries = this._listeners.get(type) || [];
      this._listeners.set(
        type,
        entries.filter(entry => entry.listener !== listener),
      );
    }

    send(data) {
      capture.sent_messages.push(serializeValue(data));
    }

    close(code = 1000, reason = "") {
      if (this.readyState === ProbeWebSocket.CLOSED) {
        return;
      }
      this.readyState = ProbeWebSocket.CLOSED;
      this._dispatch("close", {
        type: "close",
        code,
        reason,
      });
    }

    _dispatch(type, event) {
      const entries = this._listeners.get(type) || [];
      if (!entries.length) {
        return;
      }
      const remaining = [];
      for (const entry of entries) {
        const { listener, once } = entry;
        try {
          if (typeof listener === "function") {
            listener(event);
          } else if (listener && typeof listener.handleEvent === "function") {
            listener.handleEvent(event);
          }
        } catch {}
        if (!once) {
          remaining.push(entry);
        }
      }
      this._listeners.set(type, remaining);
    }
  }

  ProbeWebSocket.CONNECTING = 0;
  ProbeWebSocket.OPEN = 1;
  ProbeWebSocket.CLOSING = 2;
  ProbeWebSocket.CLOSED = 3;
  return ProbeWebSocket;
}

async function handleICubeRpcInspect(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    throw new Error("Expected a JSON object body.");
  }
  const icube = vscode.icube;
  if (!icube || typeof icube.getWebSocketRpcClient !== "function") {
    throw new Error("`vscode.icube.getWebSocketRpcClient` is unavailable.");
  }
  const clientUrl = String(requestBody.client_url || "").trim();
  const clientOptions =
    requestBody.client_options && typeof requestBody.client_options === "object"
      ? requestBody.client_options
      : {};
  const rpcMethod = String(requestBody.method || "").trim();
  const args = Array.isArray(requestBody.args) ? requestBody.args : [];
  const waitMs =
    Number.isFinite(Number(requestBody.wait_ms)) && Number(requestBody.wait_ms) >= 0
      ? Number(requestBody.wait_ms)
      : 100;
  if (!clientUrl) {
    throw new Error("Missing `client_url`.");
  }

  const capture = {
    connections: [],
    sent_messages: [],
    log: [],
  };
  const ProbeWebSocketCtor = createProbeWebSocketCtor(capture);
  const logger = createProbeLogger(capture);
  const mergedClientOptions = {
    ...clientOptions,
    reconnectTimeout:
      Number.isFinite(Number(clientOptions.reconnectTimeout)) &&
      Number(clientOptions.reconnectTimeout) >= 0
        ? Number(clientOptions.reconnectTimeout)
        : 1,
    WebSocketCtor: ProbeWebSocketCtor,
    logger,
  };

  let client;
  const globalObject = globalThis;
  const previousWebSocket = globalObject.WebSocket;
  try {
    globalObject.WebSocket = ProbeWebSocketCtor;
    client = await icube.getWebSocketRpcClient(clientUrl, mergedClientOptions);
    if (rpcMethod) {
      if (typeof client?.[rpcMethod] !== "function") {
        throw new Error(`RPC client method not available: ${rpcMethod}`);
      }
      Promise.resolve(client[rpcMethod](...args))
        .then(result => {
          capture.method_result = serializeValue(result);
        })
        .catch(error => {
          capture.method_error = serializeValue(error);
        });
    }
    await new Promise(resolve => setTimeout(resolve, waitMs));
  } finally {
    try {
      client?.close?.(1000, "probe");
    } catch {}
    globalObject.WebSocket = previousWebSocket;
  }

  return {
    ok: true,
    client_url: clientUrl,
    client_options: serializeValue(clientOptions),
    method: rpcMethod || null,
    capture: serializeValue(capture),
  };
}

async function handleAhaRpc(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    throw new Error("Expected a JSON object body.");
  }
  const serviceName = String(requestBody.service_name || "ai-agent").trim();
  const requestMethod = String(requestBody.request_method || "request").trim();
  const timeoutMs = Number(requestBody.timeout_ms || 10000);
  const envelope = requestBody.envelope;

  if (!serviceName) {
    throw new Error("Missing `service_name`.");
  }
  if (!requestMethod) {
    throw new Error("Missing `request_method`.");
  }
  if (!envelope || typeof envelope !== "object") {
    throw new Error("Missing `envelope`.");
  }

  const { electron, appRoot, Connection } = await getAhaConnectionCtor();
  const requestedConnectSessionId = String(
    envelope?.params?.client_info?.connect_session_id || "",
  ).trim();
  const requestedSessionId = String(envelope?.session_id || "").trim();

  if (serviceName === "ai-agent" && activeContext) {
    try {
      await startManagerExchange(activeContext);
    } catch (error) {
      log(
        "manager exchange warmup failed",
        error && error.stack ? error.stack : String(error),
      );
    }
  }

  if (
    serviceName === "ai-agent" &&
    managerExchangeStatus === "ready" &&
    managerExchangeConnection &&
    managerExchangeSessionId
  ) {
    const normalizedEnvelope = normalizeManagerExchangeEnvelope(envelope);
    const result = await withTimeout(
      managerExchangeConnection.sendRequest(requestMethod, normalizedEnvelope),
      timeoutMs,
      `Manager exchange JSON-RPC ${requestMethod}`,
    );

    return {
      ok: true,
      result: serializeValue(result),
      meta: {
        transport: "bridge.managerExchange",
        service_name: serviceName,
        request_method: requestMethod,
        app_root: appRoot,
        process_pid: process.pid,
        connect_session_id: managerExchangeSessionId,
        requested_connect_session_id: requestedConnectSessionId || null,
        requested_session_id: requestedSessionId || null,
      },
    };
  }

  let rawConnection;
  try {
    rawConnection = await Promise.resolve(electron.ahaIpc.connect(serviceName));

    const connection = new Connection(createAhaChannel(rawConnection));
    const result = await withTimeout(
      connection.sendRequest(requestMethod, envelope),
      timeoutMs,
      `Electron JSON-RPC ${requestMethod}`,
    );

    return {
      ok: true,
      result: serializeValue(result),
      meta: {
        transport: "bridge.electron.ahaIpc",
        service_name: serviceName,
        request_method: requestMethod,
        app_root: appRoot,
        process_pid: process.pid,
      },
    };
  } finally {
    rawConnection?.disconnect?.();
  }
}

async function handleCommands(url) {
  const match = (url.searchParams.get("match") || "").trim().toLowerCase();
  const includeInternal = (url.searchParams.get("internal") || "true") !== "false";
  const commands = await vscode.commands.getCommands(includeInternal);
  const filtered = match
    ? commands.filter(command => command.toLowerCase().includes(match))
    : commands;
  filtered.sort();
  return {
    ok: true,
    count: filtered.length,
    commands: filtered,
  };
}

async function handleExecute(requestBody) {
  if (!requestBody || typeof requestBody !== "object") {
    throw new Error("Expected a JSON object body.");
  }
  const command = String(requestBody.command || "").trim();
  const args = Array.isArray(requestBody.args) ? requestBody.args : [];
  if (!command) {
    throw new Error("Missing `command`.");
  }
  const result = await vscode.commands.executeCommand(command, ...args);
  return {
    ok: true,
    command,
    arg_count: args.length,
    result: serializeValue(result),
  };
}

async function handleRequest(context, request, response) {
  if (!isAuthorized(request)) {
    sendJson(response, 401, {
      ok: false,
      error: {
        code: "unauthorized",
        message: "Missing or invalid bridge token.",
      },
    });
    return;
  }

  const url = new URL(request.url || "/", "http://127.0.0.1");
  try {
    if (request.method === "GET" && url.pathname === "/health") {
      const snapshot = await writeStateFile(context);
      sendJson(response, 200, {
        ok: true,
        bridge: snapshot,
      });
      return;
    }

    if (request.method === "GET" && url.pathname === "/commands") {
      sendJson(response, 200, await handleCommands(url));
      return;
    }

    if (request.method === "POST" && url.pathname === "/execute") {
      const body = await readBody(request);
      const payload = body ? JSON.parse(body) : {};
      sendJson(response, 200, await handleExecute(payload));
      return;
    }

    if (request.method === "POST" && url.pathname === "/aha-rpc") {
      const body = await readBody(request);
      const payload = body ? JSON.parse(body) : {};
      sendJson(response, 200, await handleAhaRpc(payload));
      return;
    }

    if (request.method === "GET" && url.pathname === "/vscode-surface") {
      sendJson(response, 200, await handleVSCodeSurface(url));
      return;
    }

    if (request.method === "POST" && url.pathname === "/vscode-api") {
      const body = await readBody(request);
      const payload = body ? JSON.parse(body) : {};
      sendJson(response, 200, await handleVSCodeApi(payload));
      return;
    }

    if (request.method === "POST" && url.pathname === "/icube-rpc") {
      const body = await readBody(request);
      const payload = body ? JSON.parse(body) : {};
      sendJson(response, 200, await handleICubeRpc(payload));
      return;
    }

    if (request.method === "POST" && url.pathname === "/icube-rpc-inspect") {
      const body = await readBody(request);
      const payload = body ? JSON.parse(body) : {};
      sendJson(response, 200, await handleICubeRpcInspect(payload));
      return;
    }

    sendJson(response, 404, {
      ok: false,
      error: {
        code: "not-found",
        message: `Unknown route: ${request.method} ${url.pathname}`,
      },
    });
  } catch (error) {
    log("request error", error && error.stack ? error.stack : String(error));
    sendJson(response, 500, {
      ok: false,
      error: serializeValue(error),
    });
  }
}

async function startServer(context) {
  if (server) {
    return;
  }

  await ensureStateDir();
  stateToken = crypto.randomBytes(24).toString("hex");
  startedAt = new Date().toISOString();
  server = http.createServer((request, response) => {
    handleRequest(context, request, response);
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });

  const address = server.address();
  if (!address || typeof address !== "object") {
    throw new Error("Failed to resolve bridge listen address.");
  }
  serverPort = address.port;
  await writeStateFile(context);
  log(`bridge listening on 127.0.0.1:${serverPort}`);

  context.subscriptions.push({
    dispose() {
      try {
        server?.close();
      } catch {}
      server = undefined;
    },
  });
}

async function showStatus(context) {
  const snapshot = await writeStateFile(context);
  const commandCount = (await vscode.commands.getCommands(true)).length;
  const message = [
    `Trae CLI bridge: 127.0.0.1:${snapshot.port}`,
    `Commands visible: ${commandCount}`,
    `Workspace folders: ${snapshot.workspace_folders.length}`,
  ].join("\n");
  vscode.window.showInformationMessage(message);
}

async function activate(context) {
  activeContext = context;
  outputChannel = vscode.window.createOutputChannel("Trae CLI Headless");
  context.subscriptions.push(outputChannel);
  bridgeInstanceId = `${process.pid}`;
  statePath = path.join(context.globalStorageUri.fsPath, STATE_FILE_NAME);
  instanceStatePath = path.join(
    context.globalStorageUri.fsPath,
    STATE_INSTANCES_DIR_NAME,
    `${bridgeInstanceId}.json`,
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("traecli.headlessBridge.showStatus", async () => {
      await showStatus(context);
    }),
  );

  context.subscriptions.push(
    vscode.workspace.onDidChangeWorkspaceFolders(async () => {
      await writeStateFile(context);
    }),
  );
  context.subscriptions.push(
    vscode.window.onDidChangeActiveTextEditor(async () => {
      await writeStateFile(context);
    }),
  );
  context.subscriptions.push(
    vscode.window.onDidChangeWindowState(async () => {
      await writeStateFile(context);
    }),
  );

  await startServer(context);
  try {
    await startManagerExchange(context);
  } catch (error) {
    log(
      "manager exchange init failed",
      error && error.stack ? error.stack : String(error),
    );
    scheduleManagerExchangeReconnect();
  }
}

async function deactivate() {
  activeContext = undefined;
  if (managerExchangeReconnectTimer) {
    clearTimeout(managerExchangeReconnectTimer);
    managerExchangeReconnectTimer = undefined;
  }
  managerExchangeRawConnection?.disconnect?.();
  resetManagerExchangeState();
  await cleanupStateFiles();
  await new Promise(resolve => {
    if (!server) {
      resolve();
      return;
    }
    server.close(() => resolve());
    server = undefined;
  });
}

module.exports = {
  activate,
  deactivate,
  EXTENSION_ID,
};
