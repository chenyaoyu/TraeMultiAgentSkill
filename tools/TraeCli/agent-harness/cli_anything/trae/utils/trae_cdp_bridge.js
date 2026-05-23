#!/usr/bin/env node
"use strict";

const { execFile } = require("node:child_process");
const { promisify } = require("node:util");

const DEFAULT_DEBUG_HOST = "127.0.0.1";
const DEFAULT_DEBUG_PORT = 9222;
const DEFAULT_DISCOVERY_TIMEOUT_MS = 5000;
const DEFAULT_READY_TIMEOUT_MS = 15000;
const DEFAULT_COMMAND_TIMEOUT_MS = 5000;
const DEFAULT_RESPONSE_TIMEOUT_MS = 30000;
const DEFAULT_RESPONSE_POLL_INTERVAL_MS = 350;
const DEFAULT_RESPONSE_IDLE_MS = 1200;
const DEFAULT_POST_ACTION_DELAY_MS = 350;
const DEFAULT_WEBSOCKET_PROBE_MAX_RECORDS = 24;
const DEFAULT_APP_ACTIVATION_DELAY_MS = 250;

const DEFAULT_COMPOSER_SELECTORS = [
  ".chat-input-v2-input-box-editable",
  "textarea",
  "[contenteditable='true']",
  "input[type='text']",
];
const DEFAULT_SEND_BUTTON_SELECTORS = [
  "button.chat-input-v2-send-button",
  "button[data-testid*='send']",
  "button[aria-label*='Send']",
  "button[type='submit']",
];
const DEFAULT_RESPONSE_SELECTORS = [
  ".assistant-chat-turn-content p.chat-markdown-p",
  ".assistant-chat-turn-content li.chat-markdown-li",
  ".assistant-chat-turn-content pre",
  ".assistant-chat-turn-content .chat-markdown",
  ".assistant-chat-turn-content",
  ".agent-plan-items.assistant-chat-turn-element",
  ".icd-open-folder-card-desc",
  "[data-message-author-role='assistant']",
  "[data-testid*='assistant']",
  "[data-role='assistant']",
  "[data-author='assistant']",
  ".assistant",
];
const DEFAULT_ACTIVITY_SELECTORS = [
  ".chat-content-container",
  ".chat-list-wrapper",
];

const execFileAsync = promisify(execFile);

class BridgeError extends Error {
  constructor(code, message, details = {}) {
    super(message);
    this.name = "BridgeError";
    this.code = code;
    this.details = details;
  }
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let chunks = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      chunks += chunk;
    });
    process.stdin.on("end", () => resolve(chunks));
    process.stdin.on("error", reject);
  });
}

function sleep(ms) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function normalizeComparablePath(value) {
  return String(value || "")
    .trim()
    .replace(/\/+$/, "")
    .toLowerCase();
}

async function runAppleScript(lines, timeoutMs) {
  const commands = Array.isArray(lines)
    ? lines.map((line) => String(line || "").trim()).filter(Boolean)
    : [String(lines || "").trim()].filter(Boolean);
  if (commands.length === 0) {
    return "";
  }
  const args = [];
  for (const line of commands) {
    args.push("-e", line);
  }
  const result = await execFileAsync("osascript", args, {
    encoding: "utf8",
    timeout: timeoutMs,
    windowsHide: true,
  });
  return String(result?.stdout || "").trim();
}

async function readFrontmostApplicationPath(timeoutMs) {
  if (process.platform !== "darwin") {
    return null;
  }
  try {
    const path = await runAppleScript(
      ['POSIX path of (path to frontmost application as text)'],
      timeoutMs,
    );
    return path || null;
  } catch {
    return null;
  }
}

async function activateMacApplication(target, timeoutMs) {
  const normalizedTarget = String(target || "").trim();
  if (!normalizedTarget || process.platform !== "darwin") {
    return false;
  }
  try {
    await runAppleScript(
      [`tell application ${JSON.stringify(normalizedTarget)} to activate`],
      timeoutMs,
    );
    return true;
  } catch {
    return false;
  }
}

async function activateConfiguredApplication(config) {
  const details = {
    attempted: false,
    activated: false,
    restored: false,
    previousAppPath: null,
    targetAppPath: config.appPath || null,
  };
  if (process.platform !== "darwin" || config.activateApp !== true) {
    return details;
  }

  const target = String(config.appPath || config.appName || "").trim();
  if (!target) {
    return details;
  }

  details.attempted = true;
  details.previousAppPath = await readFrontmostApplicationPath(config.commandTimeoutMs);
  if (
    details.previousAppPath &&
    normalizeComparablePath(details.previousAppPath) === normalizeComparablePath(config.appPath)
  ) {
    return details;
  }

  details.activated = await activateMacApplication(target, config.commandTimeoutMs);
  if (details.activated && config.appActivationDelayMs > 0) {
    await sleep(config.appActivationDelayMs);
  }
  return details;
}

async function restorePreviousApplication(config, details) {
  if (
    process.platform !== "darwin" ||
    !details ||
    details.activated !== true ||
    !details.previousAppPath
  ) {
    return details;
  }
  if (
    normalizeComparablePath(details.previousAppPath) === normalizeComparablePath(config.appPath)
  ) {
    return details;
  }
  details.restored = await activateMacApplication(
    details.previousAppPath,
    config.commandTimeoutMs,
  );
  if (details.restored && config.appActivationDelayMs > 0) {
    await sleep(config.appActivationDelayMs);
  }
  return details;
}

function parseSelectorList(value, fallback) {
  if (Array.isArray(value)) {
    const parsed = value.map((item) => String(item).trim()).filter(Boolean);
    return parsed.length > 0 ? parsed : [...fallback];
  }
  const parsed = String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  return parsed.length > 0 ? parsed : [...fallback];
}

function normalizeLoopbackHost(value) {
  const host = String(value || "").trim().toLowerCase();
  if (!host || host === "localhost") {
    return DEFAULT_DEBUG_HOST;
  }
  return host;
}

function serializeError(error) {
  if (!error) {
    return {
      code: "UNKNOWN_ERROR",
      message: "Unknown error",
      details: {},
      stack: null,
    };
  }
  if (error instanceof BridgeError) {
    return {
      code: error.code,
      message: error.message,
      details: error.details || {},
      stack: error.stack || null,
    };
  }
  return {
    code: error.code || "UNEXPECTED_ERROR",
    message: error.message || String(error),
    details: error.details || {},
    stack: error.stack || null,
  };
}

function withTimeout(promise, timeoutMs, code, message, details = {}) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new BridgeError(code, message, details));
    }, timeoutMs);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function buildConfig(payload = {}) {
  return {
    mode: String(payload.mode || "chat").trim().toLowerCase() || "chat",
    host: normalizeLoopbackHost(payload.host || process.env.TRAE_CDP_HOST || DEFAULT_DEBUG_HOST),
    port: Number(payload.port || process.env.TRAE_REMOTE_DEBUGGING_PORT || DEFAULT_DEBUG_PORT),
    targetId: String(payload.target_id || payload.targetId || "").trim(),
    preferFocused: payload.prefer_focused !== false,
    bringToFront: payload.bring_to_front === true || payload.bringToFront === true,
    activateApp: payload.activate_app === true || payload.activateApp === true,
    discoveryTimeoutMs: Number(payload.discovery_timeout_ms || DEFAULT_DISCOVERY_TIMEOUT_MS),
    readyTimeoutMs: Number(payload.ready_timeout_ms || DEFAULT_READY_TIMEOUT_MS),
    commandTimeoutMs: Number(payload.command_timeout_ms || DEFAULT_COMMAND_TIMEOUT_MS),
    responseTimeoutMs: Number(payload.response_timeout_ms || DEFAULT_RESPONSE_TIMEOUT_MS),
    responsePollIntervalMs: Number(
      payload.response_poll_interval_ms || DEFAULT_RESPONSE_POLL_INTERVAL_MS,
    ),
    responseIdleMs: Number(payload.response_idle_ms || DEFAULT_RESPONSE_IDLE_MS),
    postActionDelayMs: Number(payload.post_action_delay_ms || DEFAULT_POST_ACTION_DELAY_MS),
    appActivationDelayMs: Number(
      payload.app_activation_delay_ms || DEFAULT_APP_ACTIVATION_DELAY_MS,
    ),
    titleContains: parseSelectorList(
      payload.title_contains || payload.titleContains || process.env.TRAE_CDP_TARGET_TITLE_CONTAINS,
      [],
    ),
    urlContains: parseSelectorList(
      payload.url_contains || payload.urlContains || process.env.TRAE_CDP_TARGET_URL_CONTAINS,
      [],
    ),
    requiredUrlContains: parseSelectorList(
      payload.required_url_contains || payload.requiredUrlContains,
      [],
    ),
    appPath: String(payload.app_path || payload.appPath || ""),
    appName: String(payload.app_name || payload.appName || ""),
    composerSelectors: parseSelectorList(
      payload.composer_selectors || payload.composerSelectors,
      DEFAULT_COMPOSER_SELECTORS,
    ),
    sendButtonSelectors: parseSelectorList(
      payload.send_button_selectors || payload.sendButtonSelectors,
      DEFAULT_SEND_BUTTON_SELECTORS,
    ),
    responseSelectors: parseSelectorList(
      payload.response_selectors || payload.responseSelectors,
      DEFAULT_RESPONSE_SELECTORS,
    ),
    activitySelectors: parseSelectorList(
      payload.activity_selectors || payload.activitySelectors,
      DEFAULT_ACTIVITY_SELECTORS,
    ),
    prompt: String(payload.prompt || ""),
    reloadBeforeRun:
      payload.reload_before_run === true || payload.reloadBeforeRun === true,
    websocketProbeMaxRecords: Math.max(
      1,
      Number(
        payload.websocket_probe_max_records ||
          payload.websocketProbeMaxRecords ||
          DEFAULT_WEBSOCKET_PROBE_MAX_RECORDS,
      ) || DEFAULT_WEBSOCKET_PROBE_MAX_RECORDS,
    ),
    websocketProbeIdleMs: Number(
      payload.websocket_probe_idle_ms ||
        payload.websocketProbeIdleMs ||
        payload.response_idle_ms ||
        DEFAULT_RESPONSE_IDLE_MS,
    ),
    websocketProbeCaptureFrames:
      payload.websocket_probe_capture_frames !== false &&
      payload.websocketProbeCaptureFrames !== false,
  };
}

async function fetchJson(pathname, config) {
  const controller = new AbortController();
  const timer = setTimeout(() => {
    controller.abort();
  }, config.discoveryTimeoutMs);

  try {
    const response = await fetch(`http://${config.host}:${config.port}${pathname}`, {
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new BridgeError(
        "CDP_DISCOVERY_HTTP_ERROR",
        "Debugger endpoint returned an HTTP error",
        {
          pathname,
          status: response.status,
          host: config.host,
          port: config.port,
        },
      );
    }
    return await response.json();
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new BridgeError(
        "CDP_DISCOVERY_TIMEOUT",
        "Timed out while querying the debugger endpoint",
        {
          pathname,
          timeoutMs: config.discoveryTimeoutMs,
          host: config.host,
          port: config.port,
        },
      );
    }
    if (error instanceof BridgeError) {
      throw error;
    }
    throw new BridgeError(
      "CDP_DISCOVERY_FAILED",
      "Failed to query the debugger endpoint",
      {
        pathname,
        host: config.host,
        port: config.port,
        message: error.message,
      },
    );
  } finally {
    clearTimeout(timer);
  }
}

function isInspectablePageTarget(target) {
  if (!target || typeof target !== "object") {
    return false;
  }
  if (target.type !== "page") {
    return false;
  }
  const url = String(target.url || "");
  return !url.startsWith("devtools://");
}

function normalizeTargetText(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function titleMatchQuality(title, needle) {
  const normalizedTitle = normalizeTargetText(title);
  const normalizedNeedle = normalizeTargetText(needle);
  if (!normalizedTitle || !normalizedNeedle) {
    return 0;
  }
  if (normalizedTitle === normalizedNeedle) {
    return 3;
  }
  const boundaryPattern = new RegExp(
    `(?:^|[^a-z0-9])${escapeRegExp(normalizedNeedle)}(?:$|[^a-z0-9])`,
    "i",
  );
  if (boundaryPattern.test(normalizedTitle)) {
    return 2;
  }
  if (normalizedTitle.includes(normalizedNeedle)) {
    return 1;
  }
  return 0;
}

function selectorScoreTarget(target, config) {
  if (!isInspectablePageTarget(target)) {
    return Number.NEGATIVE_INFINITY;
  }

  const title = String(target.title || "").toLowerCase();
  const url = String(target.url || "").toLowerCase();
  let score = 0;

  if (config.requiredUrlContains.length > 0) {
    const matches = config.requiredUrlContains.filter((needle) =>
      url.includes(String(needle).toLowerCase()),
    );
    if (matches.length === 0) {
      return Number.NEGATIVE_INFINITY;
    }
    score += matches.length * 40;
  }

  if (config.titleContains.length > 0) {
    const qualities = config.titleContains
      .map((needle) => titleMatchQuality(title, needle))
      .filter((quality) => quality > 0);
    if (qualities.length === 0) {
      return Number.NEGATIVE_INFINITY;
    }
    score += qualities.reduce((sum, quality) => sum + quality * 25, 0);
  } else if (title) {
    score += 5;
  }

  if (config.urlContains.length > 0) {
    const matches = config.urlContains.filter((needle) =>
      url.includes(String(needle).toLowerCase()),
    );
    if (matches.length === 0) {
      return Number.NEGATIVE_INFINITY;
    }
    score += matches.length * 10;
  }

  return score;
}

function scoreTarget(target, config) {
  const selectorScore = selectorScoreTarget(target, config);
  if (!Number.isFinite(selectorScore)) {
    return Number.NEGATIVE_INFINITY;
  }

  const title = String(target.title || "").toLowerCase();
  const url = String(target.url || "").toLowerCase();
  let score = selectorScore;

  if (config.titleContains.length === 0 && title) {
    score += 5;
  }
  if (config.urlContains.length === 0 && url && url !== "about:blank") {
    score += 2;
  }

  if (title.includes("trae")) {
    score += 4;
  }
  if (url.includes("trae")) {
    score += 3;
  }
  if (url.includes("workbench")) {
    score += 2;
  }
  if (url.includes("file://")) {
    score += 1;
  }

  return score;
}

async function inspectTargetWindowState(target, config) {
  if (!target || !target.webSocketDebuggerUrl) {
    return null;
  }
  let session = null;
  try {
    session = await createCDPSession({
      webSocketDebuggerUrl: target.webSocketDebuggerUrl,
      commandTimeoutMs: config.commandTimeoutMs,
      bringToFront: false,
    });
    return await session.evaluate(`(() => ({
      hasFocus: Boolean(document.hasFocus && document.hasFocus()),
      visibilityState: String(document.visibilityState || ""),
      hidden: Boolean(document.hidden === true),
      title: String(document.title || ""),
    }))()`);
  } catch {
    return null;
  } finally {
    if (session) {
      await session.close().catch(() => {});
    }
  }
}

function scoreWindowState(windowState) {
  if (!windowState || typeof windowState !== "object") {
    return 0;
  }
  let score = 0;
  if (windowState.hasFocus === true) {
    score += 1000;
  }
  if (String(windowState.visibilityState || "").toLowerCase() === "visible") {
    score += 200;
  }
  if (windowState.hidden === false) {
    score += 50;
  }
  return score;
}

async function selectTarget(targets, config) {
  const ranked = (Array.isArray(targets) ? targets : [])
    .map((target) => ({
      target,
      score: scoreTarget(target, config),
    }))
    .filter((entry) => Number.isFinite(entry.score))
    .sort((left, right) => right.score - left.score);

  if (ranked.length === 0) {
    throw new BridgeError(
      "CDP_TARGET_NOT_FOUND",
      "No matching Trae page target was found",
      {
        inspectedTargetCount: Array.isArray(targets) ? targets.length : 0,
        requiredUrlContains: config.requiredUrlContains,
        titleContains: config.titleContains,
        urlContains: config.urlContains,
        host: config.host,
        port: config.port,
      },
    );
  }

  if (config.targetId) {
    const matched = ranked.find((entry) => String(entry.target.id || "") === config.targetId);
    if (!matched) {
      throw new BridgeError(
        "CDP_BOUND_TARGET_NOT_FOUND",
        "The bound Trae window is no longer available for this workspace",
        {
          targetId: config.targetId,
          inspectedTargetCount: ranked.length,
          host: config.host,
          port: config.port,
        },
      );
    }
    const selectorMatchedScore = selectorScoreTarget(matched.target, config);
    const selectorBestScore = selectorScoreTarget(ranked[0].target, config);
    if (
      Number.isFinite(selectorMatchedScore) &&
      Number.isFinite(selectorBestScore) &&
      selectorBestScore > selectorMatchedScore
    ) {
      return ranked[0].target;
    }
    return matched.target;
  }

  if (config.preferFocused) {
    const inspected = await Promise.all(
      ranked.slice(0, Math.min(ranked.length, 6)).map(async (entry) => ({
        ...entry,
        windowState: await inspectTargetWindowState(entry.target, config),
      })),
    );
    inspected.sort((left, right) => {
      const scoreDelta = right.score - left.score;
      if (scoreDelta !== 0) {
        return scoreDelta;
      }
      const focusDelta = scoreWindowState(right.windowState) - scoreWindowState(left.windowState);
      if (focusDelta !== 0) {
        return focusDelta;
      }
      return 0;
    });
    if (inspected.length > 0) {
      return inspected[0].target;
    }
  }

  return ranked[0].target;
}

async function discoverTarget(config) {
  const [version, targets] = await Promise.all([
    fetchJson("/json/version", config),
    fetchJson("/json/list", config),
  ]);
  const target = await selectTarget(targets, config);
  return {
    version: {
      ...version,
      host: config.host,
      port: config.port,
      baseUrl: `http://${config.host}:${config.port}`,
    },
    target,
    targets,
  };
}

function buildSocketClosedError(webSocketDebuggerUrl, closeInfo = {}) {
  return new BridgeError("CDP_SOCKET_CLOSED", "The CDP socket closed unexpectedly", {
    webSocketDebuggerUrl,
    ...closeInfo,
  });
}

function readMessageData(data) {
  if (typeof data === "string") {
    return Promise.resolve(data);
  }
  if (Buffer.isBuffer(data)) {
    return Promise.resolve(data.toString("utf8"));
  }
  if (data instanceof ArrayBuffer) {
    return Promise.resolve(Buffer.from(data).toString("utf8"));
  }
  if (data && typeof data.text === "function") {
    return data.text();
  }
  return Promise.resolve(String(data));
}

function dispatchSessionEvent(eventListeners, method, params) {
  const listeners = eventListeners.get(method);
  if (!listeners || listeners.size === 0) {
    return;
  }
  for (const handler of [...listeners]) {
    try {
      handler(params);
    } catch {}
  }
}

async function createCDPSession(options = {}) {
  const webSocketDebuggerUrl = String(options.webSocketDebuggerUrl || "").trim();
  if (!webSocketDebuggerUrl) {
    throw new BridgeError("CDP_URL_MISSING", "Missing webSocketDebuggerUrl for the CDP session");
  }

  const WebSocketImpl = globalThis.WebSocket;
  if (typeof WebSocketImpl !== "function") {
    throw new BridgeError(
      "CDP_WEBSOCKET_UNAVAILABLE",
      "This Node runtime does not expose a global WebSocket",
    );
  }

  const commandTimeoutMs = Number(options.commandTimeoutMs || DEFAULT_COMMAND_TIMEOUT_MS);
  const bringToFront = options.bringToFront !== false;
  const socket = new WebSocketImpl(webSocketDebuggerUrl);
  const pendingRequests = new Map();
  const eventListeners = new Map();
  let nextCommandId = 1;
  let isClosed = false;
  let closeInfo = null;
  let openResolved = false;

  const opened = new Promise((resolve, reject) => {
    const onOpen = () => {
      openResolved = true;
      resolve();
    };
    const onError = () => {
      if (!openResolved) {
        reject(
          new BridgeError("CDP_SOCKET_OPEN_FAILED", "Failed to open the CDP socket", {
            webSocketDebuggerUrl,
          }),
        );
      }
    };
    socket.addEventListener("open", onOpen, { once: true });
    socket.addEventListener("error", onError, { once: true });
  });

  socket.addEventListener("close", (event) => {
    isClosed = true;
    closeInfo = {
      code: typeof event.code === "number" ? event.code : null,
      reason: event.reason || "",
    };
    for (const pending of pendingRequests.values()) {
      clearTimeout(pending.timeoutHandle);
      pending.reject(buildSocketClosedError(webSocketDebuggerUrl, closeInfo));
    }
    pendingRequests.clear();
  });

  socket.addEventListener("message", (event) => {
    Promise.resolve(readMessageData(event.data))
      .then((text) => {
        const message = JSON.parse(text);
        if (!message || typeof message !== "object") {
          return;
        }
        if (typeof message.id === "number") {
          const pending = pendingRequests.get(message.id);
          if (!pending) {
            return;
          }
          clearTimeout(pending.timeoutHandle);
          pendingRequests.delete(message.id);
          if (message.error) {
            pending.reject(
              new BridgeError("CDP_COMMAND_FAILED", "The browser rejected a CDP command", {
                method: pending.method,
                responseError: message.error,
              }),
            );
            return;
          }
          pending.resolve(message.result);
          return;
        }
        if (typeof message.method === "string" && message.method) {
          dispatchSessionEvent(eventListeners, message.method, message.params || {});
        }
      })
      .catch((error) => {
        for (const pending of pendingRequests.values()) {
          clearTimeout(pending.timeoutHandle);
          pending.reject(
            new BridgeError("CDP_MESSAGE_PARSE_FAILED", "Failed to parse a CDP message", {
              message: error.message,
            }),
          );
        }
        pendingRequests.clear();
      });
  });

  async function send(method, params = {}, timeoutMs = commandTimeoutMs) {
    await opened;
    if (isClosed || socket.readyState >= WebSocketImpl.CLOSING) {
      throw buildSocketClosedError(webSocketDebuggerUrl, closeInfo || {});
    }

    const id = nextCommandId++;
    const payload = JSON.stringify({ id, method, params });
    return new Promise((resolve, reject) => {
      const timeoutHandle = setTimeout(() => {
        pendingRequests.delete(id);
        reject(
          new BridgeError("CDP_COMMAND_TIMEOUT", "Timed out waiting for a CDP command response", {
            method,
            timeoutMs,
          }),
        );
      }, timeoutMs);

      pendingRequests.set(id, {
        method,
        resolve,
        reject,
        timeoutHandle,
      });

      try {
        socket.send(payload);
      } catch (error) {
        clearTimeout(timeoutHandle);
        pendingRequests.delete(id);
        reject(
          new BridgeError("CDP_COMMAND_SEND_FAILED", "Failed to send a CDP command", {
            method,
            message: error.message,
          }),
        );
      }
    });
  }

  function on(method, handler) {
    if (!eventListeners.has(method)) {
      eventListeners.set(method, new Set());
    }
    const handlers = eventListeners.get(method);
    handlers.add(handler);
    return () => {
      handlers.delete(handler);
      if (handlers.size === 0) {
        eventListeners.delete(method);
      }
    };
  }

  async function evaluate(expression) {
    const result = await send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result && result.exceptionDetails) {
      throw new BridgeError(
        "CDP_EVALUATION_FAILED",
        "The browser rejected a Runtime.evaluate expression",
        {
          exceptionDetails: result.exceptionDetails,
        },
      );
    }
    if (!result || !result.result) {
      return undefined;
    }
    return result.result.value;
  }

  async function close() {
    if (isClosed || socket.readyState === WebSocketImpl.CLOSED) {
      return;
    }
    const closed = new Promise((resolve) => {
      socket.addEventListener("close", () => resolve(), { once: true });
    });
    socket.close();
    await closed;
  }

  await opened;
  await send("Runtime.enable");
  await send("Page.enable");
  if (bringToFront) {
    try {
      await send("Page.bringToFront");
    } catch {}
  }

  return {
    send,
    evaluate,
    on,
    close,
    getSnapshot() {
      return {
        webSocketDebuggerUrl,
        commandTimeoutMs,
        pendingRequestCount: pendingRequests.size,
      };
    },
  };
}

const PAGE_HELPERS_SOURCE = String.raw`
function __collectRoots(root, acc) {
  if (!root || acc.includes(root)) {
    return acc;
  }
  acc.push(root);
  if (!root.querySelectorAll) {
    return acc;
  }
  const descendants = Array.from(root.querySelectorAll("*"));
  for (const element of descendants) {
    if (element && element.shadowRoot) {
      __collectRoots(element.shadowRoot, acc);
    }
  }
  return acc;
}

function __queryAll(selectors) {
  const safeSelectors = Array.isArray(selectors) ? selectors.filter(Boolean) : [];
  const roots = __collectRoots(document, []);
  const result = [];
  const seen = new Set();
  for (const root of roots) {
    for (const selector of safeSelectors) {
      let matches = [];
      try {
        matches = Array.from(root.querySelectorAll(selector));
      } catch (error) {
        continue;
      }
      for (const element of matches) {
        if (!element || seen.has(element)) {
          continue;
        }
        seen.add(element);
        result.push(element);
      }
    }
  }
  result.sort((left, right) => {
    const a = left.getBoundingClientRect();
    const b = right.getBoundingClientRect();
    return a.top - b.top || a.left - b.left;
  });
  return result;
}

function __isVisible(element) {
  if (!element || !element.ownerDocument || !element.isConnected) {
    return false;
  }
  const style = window.getComputedStyle(element);
  if (!style || style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
    return false;
  }
  const rect = element.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}

function __isDisabled(element) {
  if (!element) {
    return true;
  }
  return Boolean(
    element.disabled ||
      element.getAttribute("disabled") !== null ||
      String(element.getAttribute("aria-disabled") || "").toLowerCase() === "true"
  );
}

function __stringParts(parts) {
  return parts
    .filter((item) => item !== null && item !== undefined && String(item).trim())
    .map((item) => String(item).trim());
}

function __elementLabel(element) {
  return __stringParts([
    element.getAttribute && element.getAttribute("aria-label"),
    element.getAttribute && element.getAttribute("title"),
    element.getAttribute && element.getAttribute("placeholder"),
    element.getAttribute && element.getAttribute("data-testid"),
    element.id,
    element.className,
    element.textContent,
  ]).join(" ");
}

function __elementSummary(element) {
  if (!element) {
    return null;
  }
  const rect = element.getBoundingClientRect();
  return {
    tagName: String(element.tagName || "").toLowerCase(),
    className: String(element.className || ""),
    id: String(element.id || ""),
    text: (__readElementText(element, true) || "").slice(0, 240),
    visible: __isVisible(element),
    disabled: __isDisabled(element),
    rect: {
      x: Math.round(rect.x),
      y: Math.round(rect.y),
      width: Math.round(rect.width),
      height: Math.round(rect.height),
    },
  };
}

function __readElementText(element, allowHiddenText) {
  if (!element) {
    return "";
  }
  if (!allowHiddenText && !__isVisible(element)) {
    return "";
  }
  const rawText =
    typeof element.innerText === "string" && element.innerText.trim()
      ? element.innerText
      : typeof element.textContent === "string"
        ? element.textContent
        : "";
  return String(rawText || "").replace(/\u200b/g, "").trim();
}

function __readComposerText(element) {
  if (!element) {
    return "";
  }
  if (element.isContentEditable) {
    return __readElementText(element, true);
  }
  if ("value" in element) {
    return String(element.value || "");
  }
  return __readElementText(element, true);
}

function __scoreComposer(element) {
  if (!element) {
    return Number.NEGATIVE_INFINITY;
  }
  let score = 0;
  if (__isVisible(element)) {
    score += 50;
  }
  if (!__isDisabled(element)) {
    score += 20;
  }
  if (element.matches && element.matches(".chat-input-v2-input-box-editable")) {
    score += 120;
  }
  if (element.isContentEditable) {
    score += 30;
  }
  if (element.matches && element.matches("textarea")) {
    score += 20;
  }
  const label = __elementLabel(element).toLowerCase();
  if (/(chat|message|prompt|ask|input|composer|\u53d1\u9001|\u8f93\u5165|\u63d0\u95ee)/u.test(label)) {
    score += 20;
  }
  const rect = element.getBoundingClientRect();
  score += Math.min(20, Math.round(rect.width / 80));
  score += Math.min(20, Math.round(rect.height / 24));
  if (document.activeElement === element || (element.contains && element.contains(document.activeElement))) {
    score += 40;
  }
  return score;
}

function __findComposer(payload) {
  const selectors = Array.isArray(payload.composerSelectors) ? payload.composerSelectors : [];
  const candidates = __queryAll(selectors.length > 0 ? selectors : ["textarea", "[contenteditable='true']", "input[type='text']"]);
  let best = null;
  let bestScore = Number.NEGATIVE_INFINITY;
  for (const candidate of candidates) {
    const score = __scoreComposer(candidate);
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  return best;
}

function __relationScore(button, composer) {
  if (!button || !composer) {
    return 0;
  }
  let score = 0;
  const buttonForm = button.closest ? button.closest("form") : null;
  if (buttonForm && buttonForm.contains(composer)) {
    score += 80;
  }
  let ancestor = composer.parentElement;
  let depth = 0;
  while (ancestor && depth < 6) {
    if (ancestor.contains(button)) {
      score += Math.max(0, 50 - depth * 8);
      break;
    }
    ancestor = ancestor.parentElement;
    depth += 1;
  }
  return score;
}

function __scoreSendButton(button, composer) {
  if (!button) {
    return Number.NEGATIVE_INFINITY;
  }
  let score = 0;
  if (__isVisible(button)) {
    score += 40;
  }
  if (!__isDisabled(button)) {
    score += 30;
  }
  const label = __elementLabel(button).toLowerCase();
  if (/(send|submit|ask|go|\u53d1\u9001|\u63d0\u4ea4)/u.test(label)) {
    score += 30;
  }
  if (button.matches && button.matches("button.chat-input-v2-send-button")) {
    score += 100;
  }
  score += __relationScore(button, composer);
  return score;
}

function __findSendButton(payload, composer) {
  const selectors = Array.isArray(payload.sendButtonSelectors) ? payload.sendButtonSelectors : [];
  const candidates = __queryAll(
    selectors.length > 0
      ? selectors
      : ["button", "[role='button']", "a"],
  );
  let best = null;
  let bestScore = Number.NEGATIVE_INFINITY;
  for (const candidate of candidates) {
    const score = __scoreSendButton(candidate, composer);
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  return best;
}

function __dispatchInputEvent(element, inputType, data) {
  try {
    const event = new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      inputType: inputType || "insertText",
      data: data === undefined ? null : data,
    });
    element.dispatchEvent(event);
  } catch (error) {
    element.dispatchEvent(new Event("input", { bubbles: true, cancelable: true }));
  }
  element.dispatchEvent(new Event("change", { bubbles: true, cancelable: true }));
}

function __setNativeValue(element, value) {
  const nextValue = String(value || "");
  if (element.isContentEditable) {
    element.textContent = nextValue;
    __dispatchInputEvent(element, nextValue ? "insertText" : "deleteContentBackward", nextValue);
    return;
  }

  const isTextArea = String(element.tagName || "").toLowerCase() === "textarea";
  const prototype = isTextArea ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
  if (descriptor && typeof descriptor.set === "function") {
    descriptor.set.call(element, nextValue);
  } else {
    element.value = nextValue;
  }
  __dispatchInputEvent(element, nextValue ? "insertText" : "deleteContentBackward", nextValue);
}

function __focusComposer(element) {
  element.focus();
  if (element.isContentEditable) {
    const selection = window.getSelection ? window.getSelection() : null;
    if (selection && document.createRange) {
      const range = document.createRange();
      range.selectNodeContents(element);
      range.collapse(false);
      selection.removeAllRanges();
      selection.addRange(range);
    }
  } else if (typeof element.setSelectionRange === "function") {
    const length = String(element.value || "").length;
    element.setSelectionRange(length, length);
  }
}

function __clickElement(element) {
  if (!element) {
    return false;
  }
  element.focus && element.focus();
  try {
    element.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window }));
    element.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window }));
  } catch (error) {
    // Ignore synthetic mouse event failures and fall back to click().
  }
  element.click();
  return true;
}

function __dispatchEnterKey(element) {
  if (!element) {
    return false;
  }
  const events = [
    new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }),
    new KeyboardEvent("keypress", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }),
    new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }),
  ];
  for (const event of events) {
    element.dispatchEvent(event);
  }
  return true;
}

function __captureSnapshot(payload) {
  const selectors = Array.isArray(payload.selectors) ? payload.selectors : [];
  const allowHiddenText = payload.allowHiddenText === true;
  const elements = __queryAll(selectors);
  const entries = [];
  const seen = new Set();
  for (const element of elements) {
    const text = __readElementText(element, allowHiddenText);
    if (!text) {
      continue;
    }
    const container =
      (element.closest && element.closest("[data-message-id], [data-turn-index], .chat-turn, section")) ||
      null;
    const rect = element.getBoundingClientRect();
    const messageId =
      String(element.getAttribute("data-message-id") || "") ||
      String(element.getAttribute("data-id") || "");
    const parentMessageId = String(container?.getAttribute?.("data-message-id") || "");
    const parentTurnIndex = String(container?.getAttribute?.("data-turn-index") || "");
    const key = [
      text,
      messageId,
      parentMessageId,
      parentTurnIndex,
      Math.round(rect.top),
      Math.round(rect.left),
    ].join("::");
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    entries.push({
      text,
      tagName: String(element.tagName || "").toLowerCase(),
      className: String(element.className || ""),
      messageId,
      dataId: String(element.getAttribute("data-id") || ""),
      testId: String(element.getAttribute("data-testid") || ""),
      parentMessageId,
      parentTurnIndex,
      top: Math.round(rect.top),
      left: Math.round(rect.left),
    });
  }
  return entries;
}

function __inspectReadiness(payload) {
  const composer = __findComposer(payload);
  const sendButton = composer ? __findSendButton(payload, composer) : null;
  const responseSnapshot = __captureSnapshot({
    selectors: payload.responseSelectors,
    allowHiddenText: false,
  });
  const activitySnapshot = __captureSnapshot({
    selectors: payload.activitySelectors,
    allowHiddenText: true,
  });
  return {
    ready: Boolean(composer),
    composer: __elementSummary(composer),
    composerText: __readComposerText(composer).slice(0, 240),
    sendButton: __elementSummary(sendButton),
    responseNodeCount: responseSnapshot.length,
    activityNodeCount: activitySnapshot.length,
  };
}

function __prepareComposer(payload) {
  const composer = __findComposer(payload);
  if (!composer) {
    return {
      ok: false,
      code: "COMPOSER_NOT_FOUND",
      message: "Unable to find a visible chat composer",
    };
  }
  __focusComposer(composer);
  if (composer.isContentEditable) {
    __setNativeValue(composer, "");
  } else {
    __setNativeValue(composer, "");
  }
  __focusComposer(composer);
  const sendButton = __findSendButton(payload, composer);
  return {
    ok: true,
    isContentEditable: composer.isContentEditable === true,
    composer: __elementSummary(composer),
    composerText: __readComposerText(composer),
    sendButton: __elementSummary(sendButton),
    sendButtonDisabled: sendButton ? __isDisabled(sendButton) : null,
  };
}

function __submitPrompt(payload) {
  const composer = __findComposer(payload);
  if (!composer) {
    return {
      ok: false,
      code: "COMPOSER_NOT_FOUND",
      message: "Unable to find a visible chat composer",
    };
  }
  const prompt = String(payload.prompt || "");
  if (!payload.skipTextSet) {
    __setNativeValue(composer, prompt);
  }
  __focusComposer(composer);
  if (composer.isContentEditable) {
    __dispatchEnterKey(composer);
    const sendButton = __findSendButton(payload, composer);
    return {
      ok: true,
      trigger: "keyboard",
      clicked: false,
      composerText: __readComposerText(composer),
      composer: __elementSummary(composer),
      sendButton: __elementSummary(sendButton),
      sendButtonDisabled: sendButton ? __isDisabled(sendButton) : null,
    };
  }
  const sendButton = __findSendButton(payload, composer);
  if (sendButton && !__isDisabled(sendButton)) {
    __clickElement(sendButton);
    return {
      ok: true,
      trigger: "button",
      clicked: true,
      composerText: __readComposerText(composer),
      composer: __elementSummary(composer),
      sendButton: __elementSummary(sendButton),
      sendButtonDisabled: false,
    };
  }
  __dispatchEnterKey(composer);
  return {
    ok: true,
    trigger: "keyboard",
    clicked: false,
    composerText: __readComposerText(composer),
    composer: __elementSummary(composer),
    sendButton: __elementSummary(sendButton),
    sendButtonDisabled: sendButton ? __isDisabled(sendButton) : null,
  };
}
`;

const WEBSOCKET_PROBE_SOURCE = String.raw`
function __ensureTraecliWebSocketProbe(payload) {
  const scope = window;
  if (
    scope.__traecliWsProbeState &&
    scope.__traecliWsProbeState.installed &&
    typeof scope.__traecliWsProbeState.originalWebSocket === "function"
  ) {
    scope.__traecliWsProbeState.maxRecords = Math.max(
      1,
      Number(payload.maxRecords || scope.__traecliWsProbeState.maxRecords || 24) || 24
    );
    scope.__traecliWsProbeState.captureFrames = payload.captureFrames !== false;
    scope.__traecliWsProbeState.updatedAtMs = Date.now();
    return __readTraecliWebSocketProbe(payload);
  }

  const OriginalWebSocket = scope.WebSocket;
  if (typeof OriginalWebSocket !== "function") {
    return {
      ok: false,
      installed: false,
      code: "WEBSOCKET_UNAVAILABLE",
      message: "window.WebSocket is not available",
    };
  }

  function __normalizeProtocols(protocols) {
    if (protocols === undefined || protocols === null || protocols === "") {
      return [];
    }
    if (Array.isArray(protocols)) {
      return protocols.map((item) => String(item));
    }
    return [String(protocols)];
  }

  function __normalizeFrame(data) {
    if (typeof data === "string") {
      return {
        kind: "text",
        length: data.length,
        preview: data.slice(0, 240),
      };
    }
    if (data instanceof ArrayBuffer) {
      return {
        kind: "arraybuffer",
        length: data.byteLength,
        preview: "",
      };
    }
    if (typeof Blob !== "undefined" && data instanceof Blob) {
      return {
        kind: "blob",
        length: data.size,
        preview: "",
      };
    }
    const stringified = String(data || "");
    return {
      kind: typeof data,
      length: stringified.length,
      preview: stringified.slice(0, 240),
    };
  }

  function __trimFrames(frames) {
    while (frames.length > 5) {
      frames.shift();
    }
  }

  function __trimRecords(records, maxRecords) {
    while (records.length > maxRecords) {
      records.shift();
    }
  }

  const state = {
    installed: true,
    originalWebSocket: OriginalWebSocket,
    maxRecords: Math.max(1, Number(payload.maxRecords || 24) || 24),
    captureFrames: payload.captureFrames !== false,
    totalCreated: 0,
    nextId: 1,
    updatedAtMs: Date.now(),
    records: [],
  };

  function WrappedWebSocket(url, protocols) {
    const record = {
      id: state.nextId++,
      url: String(url || ""),
      protocols: __normalizeProtocols(protocols),
      selectedProtocol: "",
      extensions: "",
      createdAtMs: Date.now(),
      readyState: "connecting",
      sendCount: 0,
      receiveCount: 0,
      sentFrames: [],
      receivedFrames: [],
      closeCode: null,
      closeReason: "",
      errorCount: 0,
      stack: String(new Error().stack || "").split("\n").slice(1, 8),
    };

    const socket =
      arguments.length >= 2
        ? new OriginalWebSocket(url, protocols)
        : new OriginalWebSocket(url);
    state.totalCreated += 1;
    state.records.push(record);
    __trimRecords(state.records, state.maxRecords);
    state.updatedAtMs = Date.now();

    const originalSend = socket.send.bind(socket);
    socket.send = function patchedSend(data) {
      record.sendCount += 1;
      if (state.captureFrames) {
        record.sentFrames.push(__normalizeFrame(data));
        __trimFrames(record.sentFrames);
      }
      state.updatedAtMs = Date.now();
      return originalSend(data);
    };

    socket.addEventListener("open", () => {
      record.readyState = "open";
      record.selectedProtocol = String(socket.protocol || "");
      record.extensions = String(socket.extensions || "");
      state.updatedAtMs = Date.now();
    });

    socket.addEventListener("message", (event) => {
      record.receiveCount += 1;
      if (state.captureFrames) {
        record.receivedFrames.push(__normalizeFrame(event.data));
        __trimFrames(record.receivedFrames);
      }
      state.updatedAtMs = Date.now();
    });

    socket.addEventListener("error", () => {
      record.errorCount += 1;
      state.updatedAtMs = Date.now();
    });

    socket.addEventListener("close", (event) => {
      record.readyState = "closed";
      record.closeCode = typeof event.code === "number" ? event.code : null;
      record.closeReason = String(event.reason || "");
      state.updatedAtMs = Date.now();
    });

    return socket;
  }

  WrappedWebSocket.prototype = OriginalWebSocket.prototype;
  WrappedWebSocket.prototype.constructor = WrappedWebSocket;
  Object.setPrototypeOf(WrappedWebSocket, OriginalWebSocket);
  for (const key of ["CONNECTING", "OPEN", "CLOSING", "CLOSED"]) {
    if (key in OriginalWebSocket) {
      WrappedWebSocket[key] = OriginalWebSocket[key];
    }
  }

  try {
    Object.defineProperty(scope, "WebSocket", {
      configurable: true,
      writable: true,
      value: WrappedWebSocket,
    });
  } catch (error) {
    scope.WebSocket = WrappedWebSocket;
  }

  scope.__traecliWsProbeState = state;
  return __readTraecliWebSocketProbe();
}

function __readTraecliWebSocketProbe() {
  const state = window.__traecliWsProbeState;
  if (!state) {
    return {
      ok: false,
      installed: false,
      totalCreated: 0,
      updatedAtMs: 0,
      records: [],
    };
  }
  return {
    ok: true,
    installed: state.installed === true,
    totalCreated: Number(state.totalCreated || 0),
    updatedAtMs: Number(state.updatedAtMs || 0),
    records: Array.isArray(state.records)
      ? state.records.map((record) => ({
          id: Number(record.id || 0),
          url: String(record.url || ""),
          protocols: Array.isArray(record.protocols) ? [...record.protocols] : [],
          selectedProtocol: String(record.selectedProtocol || ""),
          extensions: String(record.extensions || ""),
          createdAtMs: Number(record.createdAtMs || 0),
          readyState: String(record.readyState || ""),
          sendCount: Number(record.sendCount || 0),
          receiveCount: Number(record.receiveCount || 0),
          sentFrames: Array.isArray(record.sentFrames)
            ? record.sentFrames.map((item) => ({ ...item }))
            : [],
          receivedFrames: Array.isArray(record.receivedFrames)
            ? record.receivedFrames.map((item) => ({ ...item }))
            : [],
          closeCode: typeof record.closeCode === "number" ? record.closeCode : null,
          closeReason: String(record.closeReason || ""),
          errorCount: Number(record.errorCount || 0),
          stack: Array.isArray(record.stack) ? [...record.stack] : [],
        }))
      : [],
  };
}
`;

function buildInjectedSource(entrypoint, payload, extraSources = [], returnValue = true) {
  return `(() => {
    const __payload = ${JSON.stringify(payload)};
    ${PAGE_HELPERS_SOURCE}
    ${extraSources.join("\n")}
    ${returnValue ? `return ${entrypoint}(__payload);` : `${entrypoint}(__payload);`}
  })()`;
}

function buildPageExpression(entrypoint, payload, extraSources = []) {
  return buildInjectedSource(entrypoint, payload, extraSources, true);
}

function buildPageBootstrapSource(entrypoint, payload, extraSources = []) {
  return buildInjectedSource(entrypoint, payload, extraSources, false);
}

async function evaluatePage(session, entrypoint, payload, extraSources = []) {
  return session.evaluate(buildPageExpression(entrypoint, payload, extraSources));
}

function extractAutomationResponse(snapshot = [], baseline = []) {
  const toComparableEntry = (entry) => ({
    text: String(entry?.text || ""),
    tagName: String(entry?.tagName || ""),
    className: String(entry?.className || ""),
    top: typeof entry?.top === "number" ? entry.top : null,
    left: typeof entry?.left === "number" ? entry.left : null,
    messageId: String(entry?.messageId || ""),
    dataId: String(entry?.dataId || ""),
    testId: String(entry?.testId || ""),
    parentMessageId: String(entry?.parentMessageId || ""),
    parentTurnIndex: String(entry?.parentTurnIndex || ""),
  });
  const normalizeEntryText = (value) =>
    String(value || "")
      .replace(/\s+/g, " ")
      .trim();
  const signatureOf = (entry) =>
    [
      entry.text,
      entry.tagName,
      entry.className,
      entry.messageId,
      entry.dataId,
      entry.testId,
      entry.parentMessageId,
      entry.parentTurnIndex,
    ].join("::");
  const entrySpecificityScore = (entry) => {
    const tagName = String(entry?.tagName || "").toLowerCase();
    const className = String(entry?.className || "").toLowerCase();
    let score = 0;
    if (tagName === "p" || tagName === "li") {
      score += 40;
    } else if (tagName === "pre" || tagName === "code") {
      score += 35;
    } else if (tagName === "span") {
      score += 10;
    }
    if (/\bchat-markdown-p\b/.test(className)) {
      score += 80;
    }
    if (/\bchat-markdown\b/.test(className)) {
      score += 50;
    }
    if (/\bicd-open-folder-card-desc\b/.test(className)) {
      score += 45;
    }
    if (/\bassistant-chat-turn-content\b/.test(className)) {
      score -= 60;
    }
    if (/\bchat-content-container\b/.test(className) || /\bchat-list-wrapper\b/.test(className)) {
      score -= 90;
    }
    score -= Math.min(String(entry?.text || "").length, 400) / 24;
    return score;
  };
  const isSameResponseGroup = (left, right) => {
    if (!left || !right) {
      return false;
    }
    if (
      left.parentMessageId &&
      right.parentMessageId &&
      left.parentMessageId === right.parentMessageId
    ) {
      return true;
    }
    if (left.messageId && right.messageId && left.messageId === right.messageId) {
      return true;
    }
    if (
      left.parentTurnIndex &&
      right.parentTurnIndex &&
      left.parentTurnIndex === right.parentTurnIndex
    ) {
      return true;
    }
    return false;
  };
  const isDominatedEntry = (entry, other) => {
    if (!isSameResponseGroup(entry, other)) {
      return false;
    }
    const entryText = normalizeEntryText(entry.text);
    const otherText = normalizeEntryText(other.text);
    if (!entryText || !otherText) {
      return false;
    }
    if (entryText !== otherText && !entryText.includes(otherText)) {
      return false;
    }
    if (entryText !== otherText && otherText.length < 6) {
      return false;
    }
    return entrySpecificityScore(other) >= entrySpecificityScore(entry) + (entryText === otherText ? 1 : 12);
  };
  const pruneComparableEntries = (entries) =>
    entries.filter(
      (entry, index) =>
        !entries.some((other, otherIndex) => otherIndex !== index && isDominatedEntry(entry, other)),
    );
  const insertedTextOf = (currentText, previousText) => {
    if (!previousText || !currentText || currentText.length <= previousText.length) {
      return "";
    }
    let prefixLength = 0;
    while (
      prefixLength < previousText.length &&
      prefixLength < currentText.length &&
      currentText[prefixLength] === previousText[prefixLength]
    ) {
      prefixLength += 1;
    }
    let suffixLength = 0;
    while (
      suffixLength < previousText.length - prefixLength &&
      suffixLength < currentText.length - prefixLength &&
      currentText[currentText.length - 1 - suffixLength] ===
        previousText[previousText.length - 1 - suffixLength]
    ) {
      suffixLength += 1;
    }
    const inserted = currentText.slice(
      prefixLength,
      currentText.length - suffixLength,
    );
    const removed = previousText.slice(
      prefixLength,
      previousText.length - suffixLength,
    );
    return removed ? "" : inserted;
  };
  const currentEntries = Array.isArray(snapshot)
    ? pruneComparableEntries(
        snapshot.map((entry) => toComparableEntry(entry)).filter((entry) => entry.text),
      )
    : [];
  const baselineEntries = Array.isArray(baseline)
    ? pruneComparableEntries(
        baseline.map((entry) => toComparableEntry(entry)).filter((entry) => entry.text),
      )
    : [];
  const currentTexts = currentEntries.map((entry) => entry.text);
  const baselineTexts = baselineEntries.map((entry) => entry.text);
  const currentSignatures = currentEntries.map((entry) => signatureOf(entry));
  const baselineSignatures = baselineEntries.map((entry) => signatureOf(entry));
  const baselineParentMessageIds = new Set(
    baselineEntries
      .map((entry) => entry.parentMessageId)
      .filter(Boolean),
  );

  if (currentTexts.length === 0) {
    return {
      text: "",
      source: "empty",
      snapshotCount: 0,
    };
  }

  const sameAsBaseline =
    currentSignatures.length === baselineSignatures.length &&
    currentSignatures.every((signature, index) => signature === baselineSignatures[index]);
  if (sameAsBaseline) {
    return {
      text: "",
      source: "unchanged",
      snapshotCount: currentTexts.length,
    };
  }

  const newMessageEntries = currentEntries.filter(
    (entry) =>
      entry.parentMessageId && !baselineParentMessageIds.has(entry.parentMessageId),
  );
  if (newMessageEntries.length > 0) {
    return {
      text: newMessageEntries.map((entry) => entry.text).join("\n\n"),
      source: "new_message_ids",
      snapshotCount: currentTexts.length,
    };
  }

  if (currentTexts.length > baselineTexts.length) {
    return {
      text: currentTexts.slice(baselineTexts.length).join("\n\n"),
      source: "new_nodes",
      snapshotCount: currentTexts.length,
    };
  }

  const currentLast = currentTexts[currentTexts.length - 1];
  const baselineLast = baselineTexts[baselineTexts.length - 1] || "";
  const currentLastSignature = currentSignatures[currentSignatures.length - 1] || "";
  const baselineLastSignature = baselineSignatures[baselineSignatures.length - 1] || "";
  if (baselineLast && currentLast.startsWith(baselineLast) && currentLast.length > baselineLast.length) {
    return {
      text: currentLast.slice(baselineLast.length),
      source: "last_node_growth",
      snapshotCount: currentTexts.length,
    };
  }
  const insertedMiddle = insertedTextOf(currentLast, baselineLast);
  if (insertedMiddle) {
    return {
      text: insertedMiddle,
      source: "last_node_inserted",
      snapshotCount: currentTexts.length,
    };
  }

  if (
    currentTexts.length === baselineTexts.length &&
    currentLast &&
    currentLastSignature !== baselineLastSignature
  ) {
    return {
      text: currentLast,
      source: "last_node_replaced",
      snapshotCount: currentTexts.length,
    };
  }

  if (baselineTexts.length > 0) {
    return {
      text: "",
      source: "unchanged",
      snapshotCount: currentTexts.length,
    };
  }

  return {
    text: currentLast,
    source: "last_node",
    snapshotCount: currentTexts.length,
  };
}

function normalizeComparableText(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function isMeaningfulActivityText(text, prompt) {
  const normalizedText = normalizeComparableText(text);
  if (!normalizedText) {
    return false;
  }
  const normalizedPrompt = normalizeComparableText(prompt);
  if (!normalizedPrompt) {
    return true;
  }
  if (normalizedText === normalizedPrompt) {
    return false;
  }
  return normalizeComparableText(normalizedText.split(normalizedPrompt).join(" ")).length > 0;
}

function sanitizeActivityText(text, prompt) {
  let sanitized = String(text || "");
  const normalizedPrompt = String(prompt || "").trim();
  if (normalizedPrompt) {
    const lastPromptIndex = sanitized.lastIndexOf(normalizedPrompt);
    if (lastPromptIndex >= 0) {
      sanitized = sanitized.slice(lastPromptIndex + normalizedPrompt.length);
    }
  }
  return sanitized
    .replace(/\b\d{1,2}:\d{2}\b/g, " ")
    .replace(/Builder/g, " ")
    .replace(/[^。！？\n]*Thought/gu, " ")
    .replace(/\u6b63\u5728\u5206\u6790\u95ee\u9898\.{0,3}/gu, " ")
    .replace(/\u601d\u8003\u4e2d\.{0,3}/gu, " ")
    .replace(/\u601d\u8003\u8fc7\u7a0b/gu, " ")
    .replace(/\u4efb\u52a1\u5b8c\u6210\s*\d+%/gu, " ")
    .replace(/\u4efb\u52a1\u5b8c\u6210/gu, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/\s+/g, " ")
    .replace(/^[。！？、,;:\s]+/u, "")
    .trim();
}

function looksLikeActionPreface(text) {
  const normalized = String(text || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!normalized || normalized.length > 96) {
    return false;
  }
  if (/[：:]/u.test(normalized)) {
    return false;
  }
  return [
    /(?:^|[。！？\s])(?:我来|我先|让我|先让我)(?:帮(?:你|您))?(?:[^。！？\n]{0,40})?(?:查看|看一下|看看|检查|分析|搜索|查找|读取|了解|浏览|总结|整理|确认|计算|定位|排查)/u,
    /(?:^|[。！？\s])我(?:需要|会)?先(?:[^。！？\n]{0,40})?(?:查看|看一下|看看|检查|分析|搜索|查找|读取|了解|浏览|总结|整理|确认|计算|定位|排查)/u,
    /\b(?:let me|i(?:'ll| will)|first,?\s*i(?:'ll| will))\b.{0,60}\b(?:check|look|inspect|search|read|review|summarize|analyze|explore)\b/i,
  ].some((pattern) => pattern.test(normalized));
}

function isPendingStatusText(text) {
  const rawText = String(text || "").trim();
  if (!rawText) {
    return false;
  }
  if (looksLikeActionPreface(rawText)) {
    return true;
  }
  return [
    /\u53ec\u56de\u4e0a\u4e0b\u6587\u4e2d/iu,
    /\u6b63\u5728(?:\u5206\u6790|\u68c0\u67e5|\u8bfb\u53d6|\u641c\u7d22|\u67e5\u627e|\u6574\u7406|\u603b\u7ed3|\u751f\u6210|\u6267\u884c|\u601d\u8003)/u,
    /\u601d\u8003\u4e2d/iu,
    /\u5904\u7406\u4e2d/iu,
    /\u68c0\u7d22\u4e2d/iu,
    /\b(?:recalling context|analyzing|reading|searching|inspecting|gathering|thinking|planning)\b/i,
  ].some((pattern) => pattern.test(rawText));
}

function buildActivityState(text, prompt) {
  const rawText = String(text || "");
  const sanitizedText = sanitizeActivityText(rawText, prompt);
  const pendingStatus = isPendingStatusText(rawText) || isPendingStatusText(sanitizedText);
  return {
    rawText,
    text: sanitizedText,
    meaningful: isMeaningfulActivityText(sanitizedText, ""),
    pending:
      pendingStatus ||
      /(?:\u6b63\u5728\u5206\u6790\u95ee\u9898|\u601d\u8003\u4e2d|\u601d\u8003\u8fc7\u7a0b)/u.test(rawText),
    terminal: /(?:\u4efb\u52a1\u5b8c\u6210|\u8bf7\u6c42\u5931\u8d25|\u5931\u8d25|\u5f02\u5e38\u6253\u65ad|\u9519\u8bef|error)/iu.test(rawText),
  };
}

function shouldPreferActivityText(finalText, activityState) {
  const normalizedFinal = normalizeComparableText(finalText);
  const normalizedActivity = normalizeComparableText(activityState?.text || "");
  if (!normalizedActivity) {
    return false;
  }
  if (!normalizedFinal) {
    return true;
  }
  if (normalizedActivity === normalizedFinal) {
    return false;
  }
  if (normalizedActivity.includes(normalizedFinal) && normalizedActivity.length > normalizedFinal.length) {
    return true;
  }
  return normalizedActivity.length >= normalizedFinal.length + 12;
}

async function waitForReady(session, config) {
  const startedAtMs = Date.now();
  let lastReadiness = null;
  while (Date.now() - startedAtMs < config.readyTimeoutMs) {
    lastReadiness = await evaluatePage(session, "__inspectReadiness", {
      composerSelectors: config.composerSelectors,
      sendButtonSelectors: config.sendButtonSelectors,
      responseSelectors: config.responseSelectors,
      activitySelectors: config.activitySelectors,
    });
    if (lastReadiness && lastReadiness.ready) {
      return lastReadiness;
    }
    await sleep(Math.min(config.responsePollIntervalMs, 250));
  }
  throw new BridgeError(
    "CDP_SELECTOR_NOT_READY",
    "The Trae chat composer selectors are not ready",
    {
      readyTimeoutMs: config.readyTimeoutMs,
      lastReadiness,
    },
  );
}

async function captureSnapshot(session, selectors, allowHiddenText) {
  return evaluatePage(session, "__captureSnapshot", {
    selectors,
    allowHiddenText,
  });
}

async function submitPrompt(session, config, prompt) {
  const prepared = await evaluatePage(session, "__prepareComposer", {
    composerSelectors: config.composerSelectors,
    sendButtonSelectors: config.sendButtonSelectors,
  });
  if (!prepared || !prepared.ok) {
    throw new BridgeError(
      "CDP_PREPARE_FAILED",
      prepared?.message || "Failed to prepare the Trae composer",
      {
        prepared,
      },
    );
  }

  const canUseInsertText = prepared.isContentEditable && !prepared.composerText;
  if (canUseInsertText) {
    await session.send("Input.insertText", { text: prompt }, config.commandTimeoutMs);
  }

  const submission = await evaluatePage(session, "__submitPrompt", {
    composerSelectors: config.composerSelectors,
    sendButtonSelectors: config.sendButtonSelectors,
    prompt,
    skipTextSet: canUseInsertText,
  });
  if (!submission || !submission.ok) {
    throw new BridgeError(
      "CDP_SUBMIT_FAILED",
      submission?.message || "Failed to submit text through the Trae window",
      {
        prepared,
        submission,
      },
    );
  }
  return {
    prepared,
    submission,
  };
}

async function collectResponse(session, config, prompt, baselineSnapshot, baselineActivitySnapshot) {
  const startedAtMs = Date.now();
  let lastText = "";
  let lastRawFinalText = "";
  let lastFinalText = "";
  let lastActivityText = "";
  let lastActivitySource = "activity";
  let lastActivitySnapshotCount = 0;
  let lastResponseText = "";
  let lastResponseCanFinish = false;
  let lastChangeAtMs = null;
  let lastExtractedResponse = {
    text: "",
    source: "empty",
    snapshotCount: 0,
  };
  let lastActivityState = {
    rawText: "",
    text: "",
    meaningful: false,
    pending: false,
    terminal: false,
  };
  let lastResponseSnapshotTail = [];
  let lastActivitySnapshotTail = [];

  while (Date.now() - startedAtMs < config.responseTimeoutMs) {
    const snapshot = await captureSnapshot(session, config.responseSelectors, true);
    lastResponseSnapshotTail = Array.isArray(snapshot) ? snapshot.slice(-4) : [];
    const extracted = extractAutomationResponse(snapshot, baselineSnapshot);
    lastExtractedResponse = extracted || lastExtractedResponse;
    const finalState = buildActivityState(extracted.text, prompt);
    if (finalState.rawText) {
      lastRawFinalText = finalState.rawText;
    }
    if (finalState.meaningful && !finalState.pending) {
      lastFinalText = finalState.text;
    }

    let activityState = {
      rawText: "",
      text: "",
      meaningful: false,
      pending: false,
      terminal: false,
    };
    if (config.activitySelectors.length > 0) {
      const activitySnapshot = await captureSnapshot(session, config.activitySelectors, true);
      lastActivitySnapshotTail = Array.isArray(activitySnapshot)
        ? activitySnapshot.slice(-4)
        : [];
      const extractedActivity = extractAutomationResponse(
        activitySnapshot,
        baselineActivitySnapshot,
      );
      activityState = buildActivityState(extractedActivity.text, prompt);
      if (activityState.meaningful) {
        lastActivityText = activityState.text;
        lastActivitySource = `activity_${extractedActivity.source}`;
        lastActivitySnapshotCount = extractedActivity.snapshotCount;
      }
    }
    lastActivityState = activityState;

    let candidateText = finalState.meaningful ? finalState.text : "";
    let candidateSource = extracted.source;
    let candidateSnapshotCount = extracted.snapshotCount;
    let candidateCanFinish = finalState.meaningful && !finalState.pending;

    if (
      activityState.meaningful &&
      (
        !candidateText ||
        finalState.pending
      ) &&
      activityState.terminal
    ) {
      candidateText = activityState.text;
      candidateSource = lastActivitySource;
      candidateSnapshotCount = lastActivitySnapshotCount;
      candidateCanFinish = true;
    }

    if (candidateText) {
      lastText = candidateText;
      if (
        candidateText !== lastResponseText ||
        candidateCanFinish !== lastResponseCanFinish ||
        lastChangeAtMs === null
      ) {
        lastResponseText = candidateText;
        lastResponseCanFinish = candidateCanFinish;
        lastChangeAtMs = Date.now();
      }
    }

    if (
      lastResponseCanFinish &&
      lastChangeAtMs !== null &&
      Date.now() - lastChangeAtMs >= config.responseIdleMs
    ) {
      return {
        text: lastResponseText,
        source: candidateSource,
        snapshotCount: candidateSnapshotCount,
      };
    }

    await sleep(config.responsePollIntervalMs);
  }

  const responseText =
    lastResponseText ||
    lastFinalText ||
    (lastActivityState.terminal ? lastActivityText : "");
  if (!responseText) {
    throw new BridgeError(
      "CDP_RESPONSE_TIMEOUT",
      "Timed out waiting for a DOM response from Trae",
      {
        responseTimeoutMs: config.responseTimeoutMs,
        prompt: String(prompt || ""),
        baselineResponseNodeCount: Array.isArray(baselineSnapshot) ? baselineSnapshot.length : 0,
        baselineActivityNodeCount: Array.isArray(baselineActivitySnapshot)
          ? baselineActivitySnapshot.length
          : 0,
        lastExtractedResponse,
        lastRawFinalText,
        lastFinalText,
        lastActivityState,
        lastResponseText,
        lastResponseCanFinish,
        lastResponseSnapshotTail,
        lastActivitySnapshotTail,
      },
    );
  }

  return {
    text: responseText,
    source: lastActivityText && !lastFinalText ? lastActivitySource : "response",
    snapshotCount: lastActivityText && !lastFinalText ? lastActivitySnapshotCount : null,
  };
}

function normalizeNetworkHeaders(headers) {
  if (!headers || typeof headers !== "object") {
    return {};
  }
  return Object.fromEntries(
    Object.entries(headers)
      .filter(([key, value]) => key && value !== undefined && value !== null)
      .map(([key, value]) => [
        String(key),
        typeof value === "string" ? value : JSON.stringify(value),
      ]),
  );
}

function summarizeNetworkFrame(frame) {
  const payloadData = String(frame?.payloadData || "");
  return {
    opcode: typeof frame?.opcode === "number" ? frame.opcode : null,
    mask: frame?.mask === true,
    payloadLength: payloadData.length,
    payloadPreview: payloadData.slice(0, 240),
  };
}

function createWebSocketNetworkRecorder(session, config) {
  const maxRecords = Math.max(
    1,
    Number(config.websocketProbeMaxRecords || DEFAULT_WEBSOCKET_PROBE_MAX_RECORDS) ||
      DEFAULT_WEBSOCKET_PROBE_MAX_RECORDS,
  );
  const state = {
    created: [],
    handshakes: [],
    responses: [],
    sentFrames: [],
    receivedFrames: [],
    closed: [],
    failures: [],
    counters: {
      created: 0,
      handshakes: 0,
      responses: 0,
      sentFrames: 0,
      receivedFrames: 0,
      closed: 0,
      failures: 0,
    },
    updatedAtMs: 0,
  };
  const requestMeta = new Map();

  function push(kind, entry) {
    state.counters[kind] += 1;
    state[kind].push({
      index: state.counters[kind],
      ...entry,
    });
    while (state[kind].length > maxRecords) {
      state[kind].shift();
    }
    state.updatedAtMs = Date.now();
  }

  function rememberRequest(requestId, patch = {}) {
    const key = String(requestId || "");
    if (!key) {
      return;
    }
    requestMeta.set(key, {
      ...(requestMeta.get(key) || {}),
      ...patch,
    });
  }

  const disposers = [
    session.on("Network.webSocketCreated", (params) => {
      const requestId = String(params?.requestId || "");
      const url = String(params?.url || "");
      rememberRequest(requestId, { requestId, url });
      push("created", {
        requestId,
        url,
        initiatorType: String(params?.initiator?.type || ""),
      });
    }),
    session.on("Network.webSocketWillSendHandshakeRequest", (params) => {
      const requestId = String(params?.requestId || "");
      const requestHeaders = normalizeNetworkHeaders(params?.request?.headers);
      const url = String(params?.request?.url || requestMeta.get(requestId)?.url || "");
      rememberRequest(requestId, { requestId, url, requestHeaders });
      push("handshakes", {
        requestId,
        url,
        requestHeaders,
        secWebSocketProtocol:
          requestHeaders["Sec-WebSocket-Protocol"] ||
          requestHeaders["sec-websocket-protocol"] ||
          "",
      });
    }),
    session.on("Network.webSocketHandshakeResponseReceived", (params) => {
      const requestId = String(params?.requestId || "");
      const responseHeaders = normalizeNetworkHeaders(params?.response?.headers);
      const meta = requestMeta.get(requestId) || {};
      const url = String(meta.url || "");
      push("responses", {
        requestId,
        url,
        status: typeof params?.response?.status === "number" ? params.response.status : null,
        statusText: String(params?.response?.statusText || ""),
        responseHeaders,
        secWebSocketProtocol:
          responseHeaders["Sec-WebSocket-Protocol"] ||
          responseHeaders["sec-websocket-protocol"] ||
          "",
      });
    }),
    session.on("Network.webSocketFrameSent", (params) => {
      const requestId = String(params?.requestId || "");
      const meta = requestMeta.get(requestId) || {};
      push("sentFrames", {
        requestId,
        url: String(meta.url || ""),
        frame: summarizeNetworkFrame(params?.response),
      });
    }),
    session.on("Network.webSocketFrameReceived", (params) => {
      const requestId = String(params?.requestId || "");
      const meta = requestMeta.get(requestId) || {};
      push("receivedFrames", {
        requestId,
        url: String(meta.url || ""),
        frame: summarizeNetworkFrame(params?.response),
      });
    }),
    session.on("Network.webSocketClosed", (params) => {
      const requestId = String(params?.requestId || "");
      const meta = requestMeta.get(requestId) || {};
      push("closed", {
        requestId,
        url: String(meta.url || ""),
        timestamp: params?.timestamp ?? null,
      });
    }),
    session.on("Network.loadingFailed", (params) => {
      if (String(params?.type || "") !== "WebSocket") {
        return;
      }
      push("failures", {
        requestId: String(params?.requestId || ""),
        errorText: String(params?.errorText || ""),
        canceled: params?.canceled === true,
      });
    }),
  ];

  function sliceSince(kind, baseline) {
    const minIndex = Number(baseline?.[kind] || 0);
    return state[kind].filter((entry) => Number(entry.index || 0) > minIndex);
  }

  return {
    async enable() {
      await session.send("Network.enable", {}, config.commandTimeoutMs);
    },
    close() {
      for (const dispose of disposers) {
        dispose();
      }
    },
    baseline() {
      return {
        created: state.counters.created,
        handshakes: state.counters.handshakes,
        responses: state.counters.responses,
        sentFrames: state.counters.sentFrames,
        receivedFrames: state.counters.receivedFrames,
        closed: state.counters.closed,
        failures: state.counters.failures,
      };
    },
    snapshotSince(baseline = null) {
      return {
        updatedAtMs: state.updatedAtMs,
        created: sliceSince("created", baseline),
        handshakes: sliceSince("handshakes", baseline),
        responses: sliceSince("responses", baseline),
        sentFrames: sliceSince("sentFrames", baseline),
        receivedFrames: sliceSince("receivedFrames", baseline),
        closed: sliceSince("closed", baseline),
        failures: sliceSince("failures", baseline),
      };
    },
  };
}

async function ensureWebSocketProbeInstalled(session, config) {
  return evaluatePage(
    session,
    "__ensureTraecliWebSocketProbe",
    {
      maxRecords: config.websocketProbeMaxRecords,
      captureFrames: config.websocketProbeCaptureFrames,
    },
    [WEBSOCKET_PROBE_SOURCE],
  );
}

async function readWebSocketProbeState(session) {
  return evaluatePage(session, "__readTraecliWebSocketProbe", {}, [WEBSOCKET_PROBE_SOURCE]);
}

function hasWebSocketProbeActivity(pageRecords, networkSnapshot) {
  if (Array.isArray(pageRecords) && pageRecords.length > 0) {
    return true;
  }
  const collections = [
    "created",
    "handshakes",
    "responses",
    "sentFrames",
    "receivedFrames",
    "closed",
    "failures",
  ];
  return collections.some((key) => Array.isArray(networkSnapshot?.[key]) && networkSnapshot[key].length > 0);
}

async function collectWebSocketProbe(
  session,
  config,
  baselinePageState,
  networkRecorder,
  networkBaseline,
) {
  const startedAtMs = Date.now();
  const baselineRecordId = Math.max(
    0,
    ...((baselinePageState?.records || []).map((record) => Number(record?.id || 0))),
  );
  let lastSnapshot = null;
  let lastSignature = "";
  let lastChangeAtMs = null;

  while (Date.now() - startedAtMs < config.responseTimeoutMs) {
    const pageState = await readWebSocketProbeState(session);
    const pageRecords = Array.isArray(pageState?.records)
      ? pageState.records.filter((record) => Number(record?.id || 0) > baselineRecordId)
      : [];
    const networkSnapshot = networkRecorder.snapshotSince(networkBaseline);
    const snapshot = {
      page: {
        installed: pageState?.installed === true,
        baselineTotalCreated: Number(baselinePageState?.totalCreated || 0),
        totalCreated: Number(pageState?.totalCreated || 0),
        records: pageRecords,
      },
      network: networkSnapshot,
    };

    if (hasWebSocketProbeActivity(pageRecords, networkSnapshot)) {
      const signature = JSON.stringify({
        pageUpdatedAtMs: pageState?.updatedAtMs || 0,
        pageRecordCount: pageRecords.length,
        networkUpdatedAtMs: networkSnapshot.updatedAtMs || 0,
        created: networkSnapshot.created.length,
        handshakes: networkSnapshot.handshakes.length,
        responses: networkSnapshot.responses.length,
        sentFrames: networkSnapshot.sentFrames.length,
        receivedFrames: networkSnapshot.receivedFrames.length,
        closed: networkSnapshot.closed.length,
        failures: networkSnapshot.failures.length,
      });
      if (signature !== lastSignature) {
        lastSignature = signature;
        lastChangeAtMs = Date.now();
        lastSnapshot = snapshot;
      }

      if (
        lastChangeAtMs !== null &&
        Date.now() - lastChangeAtMs >= config.websocketProbeIdleMs
      ) {
        return lastSnapshot;
      }
    }

    await sleep(config.responsePollIntervalMs);
  }

  if (lastSnapshot) {
    return lastSnapshot;
  }

  throw new BridgeError(
    "CDP_WEBSOCKET_PROBE_TIMEOUT",
    "Timed out waiting for WebSocket activity in the Trae renderer",
    {
      responseTimeoutMs: config.responseTimeoutMs,
      baselineTotalCreated: Number(baselinePageState?.totalCreated || 0),
    },
  );
}

function buildCommonOutput(config, discovery) {
  return {
    endpoint: {
      host: config.host,
      port: config.port,
      base_url: `http://${config.host}:${config.port}`,
    },
    version: discovery.version,
    target: {
      id: discovery.target.id,
      title: discovery.target.title,
      url: discovery.target.url,
      webSocketDebuggerUrl: discovery.target.webSocketDebuggerUrl,
    },
  };
}

async function runChatMode(session, config, discovery) {
  const readiness = await waitForReady(session, config);
  const baselineSnapshot = await captureSnapshot(session, config.responseSelectors, true);
  const baselineActivitySnapshot = await captureSnapshot(session, config.activitySelectors, true);

  const submittedAt = Date.now();
  const submitResult = await submitPrompt(session, config, config.prompt);
  if (config.postActionDelayMs > 0) {
    await sleep(config.postActionDelayMs);
  }
  const response = await collectResponse(
    session,
    config,
    config.prompt,
    baselineSnapshot,
    baselineActivitySnapshot,
  );

  return {
    ok: true,
    mode: "chat",
    ...buildCommonOutput(config, discovery),
    readiness,
    submit: submitResult,
    response,
    timings: {
      submitted_at_ms: submittedAt,
      collected_at_ms: Date.now(),
    },
  };
}

async function runWebSocketProbeMode(session, config, discovery) {
  const networkRecorder = createWebSocketNetworkRecorder(session, config);
  try {
    await networkRecorder.enable();
    const bootstrap = await session.send(
      "Page.addScriptToEvaluateOnNewDocument",
      {
        source: buildPageBootstrapSource(
          "__ensureTraecliWebSocketProbe",
          {
            maxRecords: config.websocketProbeMaxRecords,
            captureFrames: config.websocketProbeCaptureFrames,
          },
          [WEBSOCKET_PROBE_SOURCE],
        ),
      },
      config.commandTimeoutMs,
    );
    if (config.reloadBeforeRun) {
      await session.send("Page.reload", { ignoreCache: true }, config.commandTimeoutMs);
    }

    const readiness = await waitForReady(session, config);
    const probeInstall = await ensureWebSocketProbeInstalled(session, config);
    const networkBaseline = networkRecorder.baseline();

    const submittedAt = Date.now();
    const submitResult = await submitPrompt(session, config, config.prompt);
    if (config.postActionDelayMs > 0) {
      await sleep(config.postActionDelayMs);
    }
    const websocketProbe = await collectWebSocketProbe(
      session,
      config,
      probeInstall,
      networkRecorder,
      networkBaseline,
    );

    return {
      ok: true,
      mode: "websocket_probe",
      ...buildCommonOutput(config, discovery),
      readiness,
      probe_install: {
        bootstrap_identifier: bootstrap?.identifier || null,
        reload_before_run: config.reloadBeforeRun,
        current_page: probeInstall,
      },
      submit: submitResult,
      websocket_probe: websocketProbe,
      timings: {
        submitted_at_ms: submittedAt,
        collected_at_ms: Date.now(),
      },
    };
  } finally {
    networkRecorder.close();
  }
}

async function main() {
  const rawInput = await readStdin();
  const payload = JSON.parse(rawInput || "{}");
  const config = buildConfig(payload);
  if (!config.prompt.trim()) {
    throw new BridgeError("PROMPT_REQUIRED", "Missing non-empty `prompt`.");
  }
  if (!["chat", "websocket_probe"].includes(config.mode)) {
    throw new BridgeError("UNSUPPORTED_MODE", "Unsupported CDP bridge mode", {
      mode: config.mode,
    });
  }

  const discovery = await discoverTarget(config);
  const session = await createCDPSession({
    webSocketDebuggerUrl: discovery.target.webSocketDebuggerUrl,
    commandTimeoutMs: config.commandTimeoutMs,
    bringToFront: config.bringToFront === true,
  });
  const foreground = await activateConfiguredApplication(config);

  try {
    let result;
    try {
      result =
        config.mode === "websocket_probe"
          ? await runWebSocketProbeMode(session, config, discovery)
          : await runChatMode(session, config, discovery);
    } catch (error) {
      if (!error.details || typeof error.details !== "object") {
        error.details = {};
      }
      error.details.foreground = foreground;
      throw error;
    } finally {
      await restorePreviousApplication(config, foreground);
    }
    result.foreground = foreground;
    process.stdout.write(JSON.stringify(result) + "\n");
  } finally {
    await session.close().catch(() => {});
  }
}

main().catch((error) => {
  process.stdout.write(
    JSON.stringify({
      ok: false,
      error: serializeError(error),
    }) + "\n",
  );
  process.exit(1);
});
