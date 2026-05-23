#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { app, ahaIpc } = require("electron");

function getModulePaths(appPath) {
  const base = path.join(appPath, "Contents", "Resources", "app", "node_modules", "@aha-kit");
  return {
    rpc: path.join(base, "rpc", "dist", "index.cjs"),
  };
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

function createChannel(rawConnection) {
  return {
    send(data) {
      rawConnection.send(data);
      return Promise.resolve();
    },
    onData(callback) {
      const handler = (data) => callback(data);
      rawConnection.on("message", handler);
      return {
        dispose() {
          rawConnection.off("message", handler);
        },
      };
    },
  };
}

function serializeError(error) {
  if (!error) {
    return { message: "Unknown error" };
  }
  let fallbackMessage;
  if (typeof error === "string") {
    fallbackMessage = error;
  } else {
    try {
      fallbackMessage = JSON.stringify(error);
    } catch {
      fallbackMessage = String(error);
    }
  }
  return {
    name: error.name || "Error",
    message: error.message || fallbackMessage,
    stack: error.stack || null,
    code: error.code || null,
  };
}

function withTimeout(promise, timeoutMs, label) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label} timed out after ${timeoutMs}ms`));
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

async function waitForReady(rawConnection, timeoutMs) {
  if (!rawConnection || typeof rawConnection.on !== "function") {
    return;
  }
  if (
    rawConnection.connected === true ||
    rawConnection.ready === true ||
    rawConnection.isConnected === true
  ) {
    return;
  }
  return withTimeout(
    new Promise((resolve, reject) => {
      let settled = false;

      const cleanup = () => {
        rawConnection.off?.("connect", onReady);
        rawConnection.off?.("connected", onReady);
        rawConnection.off?.("ready", onReady);
        rawConnection.off?.("error", onError);
        rawConnection.off?.("disconnect", onDisconnect);
      };

      const finish = (fn, value) => {
        if (settled) {
          return;
        }
        settled = true;
        cleanup();
        fn(value);
      };

      const onReady = () => finish(resolve, true);
      const onError = (error) => finish(reject, error);
      const onDisconnect = () =>
        finish(reject, new Error("AHA IPC disconnected before request completed"));

      rawConnection.on?.("connect", onReady);
      rawConnection.on?.("connected", onReady);
      rawConnection.on?.("ready", onReady);
      rawConnection.on?.("error", onError);
      rawConnection.on?.("disconnect", onDisconnect);

      setTimeout(() => {
        if (
          rawConnection.connected === true ||
          rawConnection.ready === true ||
          rawConnection.isConnected === true
        ) {
          finish(resolve, true);
        }
      }, 0);
    }),
    timeoutMs,
    "Electron AHA IPC connect",
  );
}

async function main() {
  const rawInput = await readStdin();
  const payload = JSON.parse(rawInput || "{}");
  const appPath = payload.app_path;
  const serviceName = payload.service_name || "ai-agent";
  const requestMethod = payload.request_method || "request";
  const timeoutMs = Number(payload.timeout_ms || 10000);
  const envelope = payload.envelope;

  if (!appPath) {
    throw new Error("Missing `app_path`.");
  }
  if (!ahaIpc || typeof ahaIpc.connect !== "function") {
    throw new Error("`electron.ahaIpc.connect` is not available.");
  }
  if (!envelope || typeof envelope !== "object") {
    throw new Error("Missing `envelope`.");
  }
  const modulePaths = getModulePaths(appPath);
  if (!fs.existsSync(modulePaths.rpc)) {
    throw new Error(`Trae RPC module not found: ${modulePaths.rpc}`);
  }
  const { Connection } = require(modulePaths.rpc);

  app.on("window-all-closed", (event) => {
    event.preventDefault();
  });
  await app.whenReady();

  try {
    app.dock?.hide();
  } catch {}

  let rawConnection;
  try {
    rawConnection = await Promise.resolve(ahaIpc.connect(serviceName));
    await waitForReady(rawConnection, timeoutMs);

    const connection = new Connection(createChannel(rawConnection));
    const result = await withTimeout(
      connection.sendRequest(requestMethod, envelope),
      timeoutMs,
      `Electron JSON-RPC ${requestMethod}`,
    );

    process.stdout.write(
      JSON.stringify({
        ok: true,
        result,
        meta: {
          transport: "electron.ahaIpc",
          service_name: serviceName,
          request_method: requestMethod,
          process_pid: process.pid,
        },
      }) + "\n",
    );
  } finally {
    rawConnection?.disconnect?.();
    setTimeout(() => {
      app.quit();
    }, 0);
  }
}

main().catch((error) => {
  process.stdout.write(
    JSON.stringify({
      ok: false,
      error: serializeError(error),
    }) + "\n",
  );
  setTimeout(() => {
    app.exit(1);
  }, 0);
});
