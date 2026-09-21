"use strict";

// Exercise the real HTTP handler with no socket, files, or WhatsApp connection.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

let handler;
let closed = 0;
const logger = { info() {}, warn() {}, error() {}, debug() {} };
const modules = {
  fs: {
    mkdirSync() {}, chmodSync() {}, writeFileSync() {}, unlinkSync() {},
    existsSync() { return true; }, statSync() { return { size: 0 }; },
  },
  http: {
    createServer(callback) {
      handler = callback;
      return { listen() {}, close() { closed += 1; } };
    },
  },
  path,
  pino: () => logger,
  qrcode: {},
  baileys: { useMultiFileAuthState: () => new Promise(() => {}) },
};
const context = {
  require(name) {
    assert.ok(Object.hasOwn(modules, name), "unexpected dependency " + name);
    return modules[name];
  },
  process: {
    argv: ["node", "sidecar.js", "--session-dir", "/fixture/session",
      "--inbox", "/fixture/inbox", "--owner-id", "primary"],
    on() {}, exit() { throw new Error("unexpected process exit"); },
  },
  console,
  URL,
  Buffer,
  setTimeout() { return { unref() {} }; },
};
vm.runInNewContext(fs.readFileSync(
  path.join(__dirname, "../bridge/whatsapp/sidecar.js"), "utf8"), context);

async function request(method, url, headers = {}) {
  let code;
  let body;
  await handler({ method, url, headers }, {
    writeHead(value) { code = value; },
    end(value) { body = JSON.parse(value); },
  });
  return { code, body };
}

(async () => {
  const status = await request("GET", "/status");
  assert.equal(status.body.owner_id, "primary");
  assert.equal((await request("POST", "/stop")).code, 409);
  assert.equal((await request("POST", "/stop", {
    "x-vira-instance": "branch:demo",
  })).code, 409);
  assert.equal(closed, 0);
  assert.equal((await request("POST", "/stop", {
    "x-vira-instance": "primary",
  })).code, 200);
  assert.equal(closed, 1);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
