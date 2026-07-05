import { createRequire } from "node:module";

export const FIXED_TERMINAL_ID = "uuid:12";

function normalizeEnv(env) {
  const normalized = {};
  for (const [key, value] of Object.entries(env)) {
    if (typeof value === "string") {
      normalized[key] = value;
    }
  }
  return normalized;
}

export function resolveDefaultShell(env = process.env) {
  if (process.platform === "win32") {
    return env.COMSPEC || "cmd.exe";
  }
  return env.SHELL || "/bin/sh";
}

export function createNodePtySpawn() {
  const require = createRequire(import.meta.url);
  const pty = require("node-pty");
  return (shell, args, options) => pty.spawn(shell, args, options);
}

function assertNonEmptyString(value, fieldName) {
  if (typeof value !== "string" || value.trim() === "") {
    throw new Error(`${fieldName} 不能为空`);
  }
}

function assertString(value, fieldName) {
  if (typeof value !== "string") {
    throw new Error(`${fieldName} 必须是字符串`);
  }
}

function assertPositiveInteger(value, fieldName) {
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${fieldName} 必须是正整数`);
  }
}

function assertTerminalId(terminalId, supportedTerminalId) {
  assertNonEmptyString(terminalId, "terminalId");
  if (terminalId !== supportedTerminalId) {
    throw new Error(`不支持的 terminalId: ${terminalId}`);
  }
}

class TerminalSession {
  constructor({ terminalId, shell, cwd, env, ptySpawn, cols, rows }) {
    this.terminalId = terminalId;
    this.shell = shell;
    this.cwd = cwd;
    this.env = env;
    this.cols = cols;
    this.rows = rows;
    this.clients = new Map();
    this.closed = false;
    this.exitEvent = null;
    this.pty = ptySpawn(shell, [], {
      name: "xterm-256color",
      cols,
      rows,
      cwd,
      env,
    });
    this.dataSubscription = this.pty.onData((data) => this.broadcastOutput(data));
    this.exitSubscription = this.pty.onExit((event) => this.broadcastExit(event));
  }

  attach(clientId, handlers) {
    assertNonEmptyString(clientId, "clientId");
    if (this.closed) {
      throw new Error(`终端 ${this.terminalId} 已退出，不能继续 attach`);
    }
    if (typeof handlers?.onOutput !== "function") {
      throw new Error("onOutput 必须是函数");
    }
    if (typeof handlers?.onExit !== "function") {
      throw new Error("onExit 必须是函数");
    }
    this.clients.set(clientId, handlers);
  }

  detach(clientId) {
    assertNonEmptyString(clientId, "clientId");
    if (!this.clients.has(clientId)) {
      throw new Error(`客户端 ${clientId} 未 attach 到终端 ${this.terminalId}`);
    }
    this.clients.delete(clientId);
  }

  write(data) {
    assertString(data, "data");
    if (this.closed) {
      throw new Error(`终端 ${this.terminalId} 已退出，不能写入`);
    }
    this.pty.write(data);
  }

  resize(cols, rows) {
    assertPositiveInteger(cols, "cols");
    assertPositiveInteger(rows, "rows");
    if (this.closed) {
      throw new Error(`终端 ${this.terminalId} 已退出，不能 resize`);
    }
    this.cols = cols;
    this.rows = rows;
    this.pty.resize(cols, rows);
  }

  kill() {
    if (this.closed) {
      return;
    }
    this.pty.kill();
  }

  broadcastOutput(data) {
    for (const { onOutput } of this.clients.values()) {
      onOutput(data);
    }
  }

  broadcastExit(event) {
    this.closed = true;
    this.exitEvent = event;
    for (const { onExit } of this.clients.values()) {
      onExit(event);
    }
  }
}

export class TerminalManager {
  constructor({
    cwd = process.cwd(),
    env = process.env,
    shell = resolveDefaultShell(env),
    supportedTerminalId = FIXED_TERMINAL_ID,
    ptySpawn = createNodePtySpawn(),
    defaultCols = 80,
    defaultRows = 24,
  } = {}) {
    assertNonEmptyString(cwd, "cwd");
    assertNonEmptyString(shell, "shell");
    assertPositiveInteger(defaultCols, "defaultCols");
    assertPositiveInteger(defaultRows, "defaultRows");
    this.cwd = cwd;
    this.env = normalizeEnv(env);
    this.shell = shell;
    this.supportedTerminalId = supportedTerminalId;
    this.ptySpawn = ptySpawn;
    this.defaultCols = defaultCols;
    this.defaultRows = defaultRows;
    this.sessions = new Map();
  }

  attach({ terminalId, clientId, onOutput, onExit, cols = this.defaultCols, rows = this.defaultRows }) {
    assertTerminalId(terminalId, this.supportedTerminalId);
    assertPositiveInteger(cols, "cols");
    assertPositiveInteger(rows, "rows");
    const session = this.getOrCreateSession(terminalId, cols, rows);
    session.attach(clientId, { onOutput, onExit });
    session.resize(cols, rows);
    return {
      terminalId,
      clientId,
      shell: session.shell,
      cwd: session.cwd,
      cols: session.cols,
      rows: session.rows,
    };
  }

  detach(terminalId, clientId) {
    const session = this.getExistingSession(terminalId);
    session.detach(clientId);
  }

  write(terminalId, data) {
    const session = this.getExistingSession(terminalId);
    session.write(data);
  }

  agentWrite(terminalId, data) {
    const session = this.getExistingSession(terminalId);
    session.write(data);
  }

  resize(terminalId, cols, rows) {
    const session = this.getExistingSession(terminalId);
    session.resize(cols, rows);
  }

  kill(terminalId) {
    const session = this.getExistingSession(terminalId);
    session.kill();
    this.sessions.delete(terminalId);
  }

  getOrCreateSession(terminalId, cols, rows) {
    assertTerminalId(terminalId, this.supportedTerminalId);
    const existing = this.sessions.get(terminalId);
    if (existing && !existing.closed) {
      return existing;
    }
    if (existing?.closed) {
      this.sessions.delete(terminalId);
    }
    const session = new TerminalSession({
      terminalId,
      shell: this.shell,
      cwd: this.cwd,
      env: this.env,
      ptySpawn: this.ptySpawn,
      cols,
      rows,
    });
    this.sessions.set(terminalId, session);
    return session;
  }

  getExistingSession(terminalId) {
    assertTerminalId(terminalId, this.supportedTerminalId);
    const session = this.sessions.get(terminalId);
    if (!session) {
      throw new Error(`终端 ${terminalId} 尚未创建，请先 attach`);
    }
    return session;
  }
}
