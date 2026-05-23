#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

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

function getModulePaths(appPath) {
  const base = path.join(appPath, "Contents", "Resources", "app", "node_modules", "@aha-kit");
  return {
    ipc: path.join(base, "ipc", "dist", "index-main.js"),
    rpc: path.join(base, "rpc", "dist", "index.cjs"),
  };
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
  return withTimeout(
    new Promise((resolve, reject) => {
      let settled = false;

      const cleanup = () => {
        rawConnection.off("connect", onReady);
        rawConnection.off("connected", onReady);
        rawConnection.off("error", onError);
        rawConnection.off("disconnect", onDisconnect);
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
      const onDisconnect = () => finish(reject, new Error("AHA IPC disconnected before request completed"));

      rawConnection.on("connect", onReady);
      rawConnection.on("connected", onReady);
      rawConnection.on("error", onError);
      rawConnection.on("disconnect", onDisconnect);
    }),
    timeoutMs,
    "AHA IPC connect",
  );
}

async function main() {
  const rawInput = await readStdin();
  const payload = JSON.parse(rawInput || "{}");
  const appPath = payload.app_path;
  const serviceName = payload.service_name || "ai-agent";
  const runtimeDir = payload.runtime_dir || path.join("/tmp", "aha");
  const requestMethod = payload.request_method || "request";
  const timeoutMs = Number(payload.timeout_ms || 10000);
  const envelope = payload.envelope;

  if (!appPath) {
    throw new Error("Missing `app_path`.");
  }
  if (!envelope || typeof envelope !== "object") {
    throw new Error("Missing `envelope`.");
  }

  const modulePaths = getModulePaths(appPath);
  if (!fs.existsSync(modulePaths.ipc)) {
    throw new Error(`Trae IPC module not found: ${modulePaths.ipc}`);
  }
  if (!fs.existsSync(modulePaths.rpc)) {
    throw new Error(`Trae RPC module not found: ${modulePaths.rpc}`);
  }

  const ipc = require(modulePaths.ipc);
  const { Connection } = require(modulePaths.rpc);

  let rawConnection;
  try {
    rawConnection = ipc.connect(serviceName, { runtimeDir });
    await waitForReady(rawConnection, timeoutMs);

    const connection = new Connection(createChannel(rawConnection));
    const result = await withTimeout(
      connection.sendRequest(requestMethod, envelope),
      timeoutMs,
      `JSON-RPC ${requestMethod}`,
    );

    process.stdout.write(
      JSON.stringify({
        ok: true,
        result,
        meta: {
          service_name: serviceName,
          runtime_dir: runtimeDir,
          request_method: requestMethod,
        },
      }) + "\n",
    );
  } finally {
    rawConnection?.disconnect();
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
