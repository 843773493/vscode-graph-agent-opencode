import { createConnection } from "node:net";
import { readFile } from "node:fs/promises";
import { constants, closeSync, fsyncSync, openSync, readFileSync, writeSync } from "node:fs";
import { spawn } from "node:child_process";
import { resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const FORK_OPERATION_TIMEOUT_MS = 60_000;
const FORK_WATCHDOG_ENTRY = fileURLToPath(import.meta.url);
let forkWatchdog;
let forkOperationOrdinal = 0;
const forkOperations = new Map();
const forkActiveDeadlineTokens = new Set();

function forkOperationIdentity(target, operationKey) {
  return `${target}\0${operationKey}`;
}

function processIdentity(pid) {
  const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
  const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
  return { ppid: Number(fields[1]), pgid: Number(fields[2]), sid: Number(fields[3]), startTime: fields[19] };
}

function assertIsolatedDriver(pid) {
  if (process.platform !== "linux") {
    throw new Error("fork deadline watchdog 仅支持 Linux 的隔离 browser-driver session");
  }
  const identity = processIdentity(pid);
  if (identity.pgid !== pid || identity.sid !== pid || identity.pgid <= 1) {
    throw new Error(`fork deadline watchdog 拒绝未隔离的 browser driver: pid=${pid}, pgid=${identity.pgid}, sid=${identity.sid}`);
  }
  return identity;
}

function validateForkArtifactRoot(directory) {
  const artifactRoot = resolve(directory);
  const testsRoot = resolve(process.cwd(), "out/tests");
  if (!artifactRoot.startsWith(testsRoot + sep)) {
    throw new Error(`fork deadline 诊断目录必须位于正式 out/tests 下: ${artifactRoot}`);
  }
  return artifactRoot;
}

class ForkDeadlineWatchdogClient {
  constructor(artifactRoot, targetPid, targetStartTime) {
    this.artifactRoot = validateForkArtifactRoot(artifactRoot);
    this.targetPid = targetPid;
    this.targetStartTime = targetStartTime;
    this.pending = new Map();
    this.buffer = "";
    this.child = spawn(process.env.BOXTEAM_E2E_WATCHDOG_NODE || "node", [
      FORK_WATCHDOG_ENTRY, "--fork-deadline-watchdog", String(targetPid), targetStartTime, this.artifactRoot,
    ], { detached: true, stdio: ["pipe", "pipe", "inherit"] });
    this.child.stdout.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this._read(chunk));
    this.child.on("error", (error) => this._rejectPending(error));
    this.child.on("exit", (code, signal) => {
      if (this.closing && code === 0) return;
      this._rejectPending(new Error(`fork deadline watchdog 意外退出: code=${code}, signal=${signal}`));
    });
  }

  _read(chunk) {
    this.buffer += chunk;
    let newline;
    while ((newline = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      const response = JSON.parse(line);
      const pending = this.pending.get(response.id);
      if (!pending) throw new Error(`fork deadline watchdog 返回未知请求: ${response.id}`);
      this.pending.delete(response.id);
      if (response.error) pending.reject(new Error(response.error));
      else pending.resolve(response.value);
    }
  }

  _rejectPending(error) {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  request(action, values = {}) {
    const id = ++forkOperationOrdinal;
    return new Promise((resolveRequest, rejectRequest) => {
      this.pending.set(id, { resolve: resolveRequest, reject: rejectRequest });
      this.child.stdin.write(`${JSON.stringify({ id, action, ...values })}\n`, (error) => {
        if (!error) return;
        this.pending.delete(id);
        rejectRequest(error);
      });
    });
  }

  async close() {
    if (this.closing) return;
    if (forkOperations.size > 0) {
      throw new Error(`仍有未终结 fork operation，不能停止 deadline watchdog: ${[...forkOperations.keys()].join(", ")}`);
    }
    this.closing = true;
    const exited = new Promise((resolveExit, rejectExit) => {
      this.child.once("exit", (code, signal) => {
        if (code === 0) resolveExit();
        else rejectExit(new Error(`fork deadline watchdog 收停失败: code=${code}, signal=${signal}`));
      });
    });
    await this.request("stop");
    this.child.stdin.end();
    await exited;
  }
}

function forkWatchdogForThisDriver() {
  if (forkWatchdog) return forkWatchdog;
  const identity = assertIsolatedDriver(process.pid);
  const artifactRoot = process.env.BOXTEAM_E2E_FORK_ARTIFACT_ROOT
    || process.env.BOXTEAM_BROWSER_ARTIFACT_ROOT;
  if (!artifactRoot) throw new Error("缺少 fork deadline 诊断目录 BOXTEAM_E2E_FORK_ARTIFACT_ROOT");
  forkWatchdog = new ForkDeadlineWatchdogClient(artifactRoot, process.pid, identity.startTime);
  return forkWatchdog;
}

async function startForkOperationDeadline({ target, operationKey, timeoutMs = FORK_OPERATION_TIMEOUT_MS }) {
  if (!target || operationKey !== null && (!operationKey || typeof operationKey !== "string")) {
    throw new Error("真实 fork deadline 必须绑定目标；已观察 POST 前允许暂不绑定 idempotency key");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0 || timeoutMs > FORK_OPERATION_TIMEOUT_MS) {
    throw new Error("真实 fork deadline 必须在 (0, 60000] 毫秒内");
  }
  const identity = operationKey === null ? null : forkOperationIdentity(target, operationKey);
  const existing = identity === null ? undefined : forkOperations.get(identity);
  if (existing) return existing;
  const watchdog = forkWatchdogForThisDriver();
  const started = await watchdog.request("start", {
    target, operation_key: operationKey, timeout_ns: String(Math.floor(timeoutMs * 1_000_000)),
  });
  const deadline = {
    target,
    operationKey,
    token: started.token,
    deadlineNs: started.deadline_ns,
    watchdog,
    lastStage: "started",
    lastRemainingMs: timeoutMs,
    async remainingMs() {
      const value = await watchdog.request("remaining", { token: this.token });
      this.lastRemainingMs = value.remaining_ns / 1_000_000;
      return this.lastRemainingMs;
    },
    async setStage(stage) {
      this.lastStage = stage;
      await watchdog.request("stage", { token: this.token, last_stage: stage });
    },
    async complete() {
      await watchdog.request("complete", { token: this.token });
      forkActiveDeadlineTokens.delete(this.token);
      if (this.identity !== null) forkOperations.delete(this.identity);
      this.identity = null;
    },
    error() {
      const elapsed = Math.max(0, timeoutMs - this.lastRemainingMs);
      return new Error(`fork operation deadline exceeded: target=${this.target}, `
        + `idempotency_key=${this.operationKey ?? "awaiting_post_request"}, last_phase=${this.lastStage}, `
        + `elapsed_ms=${elapsed.toFixed(3)}, backend_state=unknown; `
        + "客户端等待截止不表示后端操作已取消或回滚");
    },
    identity,
  };
  if (identity !== null) forkOperations.set(identity, deadline);
  forkActiveDeadlineTokens.add(deadline.token);
  return deadline;
}

export async function beginForkOperationDeadline({ target, operationKey, timeoutMs = FORK_OPERATION_TIMEOUT_MS }) {
  if (!operationKey) throw new Error("真实 fork deadline 必须绑定 idempotency key");
  return startForkOperationDeadline({ target, operationKey, timeoutMs });
}

export async function beginForkSubmissionDeadline({ target, timeoutMs = FORK_OPERATION_TIMEOUT_MS }) {
  return startForkOperationDeadline({ target, operationKey: null, timeoutMs });
}

export async function bindForkOperationDeadline(deadline, operationKey) {
  if (typeof operationKey !== "string" || operationKey.length === 0) {
    throw new Error("真实 fork POST 缺少 idempotency key");
  }
  if (deadline.operationKey !== null) {
    if (deadline.operationKey === operationKey) return deadline;
    throw new Error("fork deadline 已绑定不同的 idempotency key");
  }
  const identity = forkOperationIdentity(deadline.target, operationKey);
  const existing = forkOperations.get(identity);
  if (existing && existing !== deadline) {
    await deadline.complete();
    return existing;
  }
  const bound = await deadline.watchdog.request("bind", { token: deadline.token, operation_key: operationKey });
  if (bound.token !== deadline.token) {
    await deadline.complete();
    const matched = forkOperations.get(identity);
    if (!matched) throw new Error("fork watchdog 合并了 deadline，但客户端没有原预算记录");
    return matched;
  }
  deadline.operationKey = operationKey;
  deadline.deadlineNs = bound.deadline_ns;
  deadline.identity = identity;
  forkOperations.set(identity, deadline);
  return deadline;
}

export async function waitForForkOperation(awaitable, deadline, stage) {
  await deadline.setStage(stage);
  const remaining = await deadline.remainingMs();
  if (remaining <= 0) throw deadline.error();
  // 只有独立 Node watcher 可以触发硬截止，避免本地 timer 先退出并关闭尚未 fsync 的 watcher。
  return await awaitable;
}

export async function closeForkDeadlineWatchdog() {
  if (forkActiveDeadlineTokens.size > 0) {
    throw new Error(`仍有未终结的 fork deadline，不能关闭外部 watchdog: ${[...forkActiveDeadlineTokens].join(", ")}`);
  }
  if (!forkWatchdog) return;
  const watchdog = forkWatchdog;
  forkWatchdog = undefined;
  await watchdog.close();
}

export async function assertForkDeadlineCloseSemanticsForTest() {
  const token = "fork-close-active-probe";
  forkActiveDeadlineTokens.add(token);
  try {
    await closeForkDeadlineWatchdog();
    throw new Error("active fork deadline unexpectedly allowed watchdog close");
  } catch (error) {
    if (!String(error).includes("仍有未终结的 fork deadline")) throw error;
  } finally {
    forkActiveDeadlineTokens.delete(token);
  }
  await closeForkDeadlineWatchdog();
}

function procStat(pid) {
  const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
  const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
  return { pgrp: Number(fields[2]), sid: Number(fields[3]), startTime: fields[19] };
}

function writeForkDeadlineDiagnostic(directory, record, now) {
  const path = resolve(directory, `fork-deadline-${now}-${record.token}.json`);
  const payload = {
    target: record.target,
    operation_key: record.operation_key,
    last_stage: record.last_stage,
    elapsed_seconds: Number((Number(now - record.started_ns) / 1_000_000_000).toFixed(3)),
    backend_state: "unknown",
    error: "fork operation deadline exceeded; watchdog terminated the isolated browser driver process group",
  };
  const descriptor = openSync(path, "wx", 0o600);
  try {
    writeSync(descriptor, `${JSON.stringify(payload, null, 2)}\n`);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  const directoryDescriptor = openSync(directory, constants.O_RDONLY);
  try {
    fsyncSync(directoryDescriptor);
  } finally {
    closeSync(directoryDescriptor);
  }
}

async function runForkDeadlineWatchdog(targetPid, targetStartTime, artifactRoot) {
  const own = procStat(process.pid);
  const target = procStat(targetPid);
  if (own.pgrp !== process.pid || own.sid !== process.pid
      || target.pgrp !== targetPid || target.sid !== targetPid || target.startTime !== targetStartTime
      || process.ppid !== targetPid || process.pid === targetPid) {
    throw new Error(`fork watchdog 拒绝不安全进程组边界: watcher=${process.pid}/${own.pgrp}/${own.sid}, `
      + `driver=${targetPid}/${target.pgrp}/${target.sid}, parent=${process.ppid}`);
  }
  const active = new Map();
  const identities = new Map();
  let buffer = "";
  let timer;
  let ordinal = 0;
  let expiring = false;
  let watchdogExitCode = 0;
  const reply = (id, value, error) => process.stdout.write(`${JSON.stringify({ id, value, error })}\n`);
  const ensureTargetGroup = () => {
    let current;
    try {
      current = procStat(targetPid);
    } catch {
      const error = new Error(`browser driver leader 已退出，不能证明目标 process group: pid=${targetPid}`);
      error.code = "ESRCH";
      throw error;
    }
    if (current.startTime !== targetStartTime || current.pgrp !== targetPid || current.sid !== targetPid) {
      throw new Error(`browser driver process group identity 已变化: pid=${targetPid}`);
    }
  };
  const stopTimer = () => clearTimeout(timer);
  const schedule = () => {
    stopTimer();
    const next = [...active.values()].reduce((minimum, record) =>
      minimum === null || record.deadline_ns < minimum ? record.deadline_ns : minimum, null);
    if (next === null) return;
    const remaining = next - process.hrtime.bigint();
    timer = setTimeout(expire, Math.max(0, Number(remaining) / 1_000_000));
  };
  const expire = () => {
    const now = process.hrtime.bigint();
    const record = [...active.values()].find((entry) => now >= entry.deadline_ns);
    if (!record) return schedule();
    expiring = true;
    stopTimer();
    try {
      writeForkDeadlineDiagnostic(artifactRoot, record, now);
    } catch (error) {
      watchdogExitCode = 1;
      try {
        writeSync(2, `fork deadline 诊断写入或 fsync 失败，继续验证并终止隔离 driver: ${String(error)}\n`);
      } catch {
        // 保留非零退出；诊断错误本身已经保存在 watchdogExitCode 中。
      }
    }
    try {
      writeSync(2, `fork deadline exceeded: target=${record.target}, `
        + `operation_key=${record.operation_key}, last_stage=${record.last_stage}, backend_state=unknown\n`);
    } catch {
      watchdogExitCode = 1;
    }
    try {
      ensureTargetGroup();
      process.kill(-targetPid, "SIGTERM");
    } catch (error) {
      watchdogExitCode = 1;
      try {
        writeSync(2, `fork watchdog 无法终止已验证 browser group: ${String(error)}\n`);
      } catch {
        // 目标组身份或信号失败仍通过非零退出报告。
      }
      return process.exit(watchdogExitCode || 1);
    }
    setTimeout(() => {
      try {
        ensureTargetGroup();
        process.kill(-targetPid, "SIGKILL");
      } catch (error) {
        if (error?.code !== "ESRCH") {
          watchdogExitCode = 1;
          try {
            writeSync(2, `fork watchdog 强制回收失败: ${String(error)}\n`);
          } catch {
            // 继续以非零状态结束，避免遮蔽回收错误。
          }
        }
      }
      process.exit(watchdogExitCode);
    }, 500);
  };
  const dispatch = (message) => {
    try {
      if (message.action === "start") {
        const identity = message.operation_key === null ? null
          : forkOperationIdentity(message.target, message.operation_key);
        let record = identity === null ? undefined : identities.get(identity);
        if (!record) {
          const now = process.hrtime.bigint();
          record = { token: `${targetPid}-${++ordinal}`, target: message.target,
            operation_key: message.operation_key, identity, started_ns: now,
            deadline_ns: now + BigInt(message.timeout_ns), last_stage: "started" };
          if (identity !== null) identities.set(identity, record);
          active.set(record.token, record);
          schedule();
        }
        reply(message.id, { token: record.token, deadline_ns: record.deadline_ns.toString() });
      } else if (message.action === "bind") {
        const record = active.get(message.token);
        if (!record) throw new Error(`fork deadline 不再 active: ${message.token}`);
        const identity = forkOperationIdentity(record.target, message.operation_key);
        const existing = identities.get(identity);
        if (existing && existing !== record) {
          reply(message.id, { token: existing.token, deadline_ns: existing.deadline_ns.toString() });
          return;
        }
        if (record.identity !== null) identities.delete(record.identity);
        record.operation_key = message.operation_key;
        record.identity = identity;
        identities.set(identity, record);
        reply(message.id, { token: record.token, deadline_ns: record.deadline_ns.toString() });
      } else if (message.action === "remaining") {
        const record = active.get(message.token);
        if (!record) throw new Error(`fork deadline 不再 active: ${message.token}`);
        const remaining = record.deadline_ns - process.hrtime.bigint();
        if (remaining <= 0n) return expire();
        reply(message.id, { deadline_ns: record.deadline_ns.toString(), remaining_ns: Number(remaining) });
      } else if (message.action === "stage") {
        const record = active.get(message.token);
        if (!record) throw new Error(`fork deadline 不再 active: ${message.token}`);
        record.last_stage = message.last_stage;
        reply(message.id, null);
      } else if (message.action === "complete") {
        const record = active.get(message.token);
        if (record) {
          active.delete(message.token);
          if (record.identity !== null && identities.get(record.identity) === record) {
            identities.delete(record.identity);
          }
          schedule();
        }
        reply(message.id, null);
      } else if (message.action === "stop") {
        if (active.size > 0) {
          reply(message.id, null, "拒绝停止仍保护未终结 fork deadline 的 watchdog");
          return;
        }
        stopTimer();
        process.stdout.write(`${JSON.stringify({ id: message.id, value: null })}\n`, () => process.exit(0));
        process.stdin.pause();
      } else {
        throw new Error(`未知 fork watchdog action: ${message.action}`);
      }
    } catch (error) {
      reply(message.id, null, `${error?.name || "Error"}: ${error?.message || String(error)}`);
    }
  };
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buffer += chunk;
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line) dispatch(JSON.parse(line));
    }
  });
  process.stdin.on("end", () => {
    if (expiring || active.size > 0) return;
    stopTimer();
    process.exit(0);
  });
}

if (process.argv[2] === "--fork-deadline-watchdog") {
  await runForkDeadlineWatchdog(Number(process.argv[3]), process.argv[4], process.argv[5]);
}

export async function testControl(payload) {
  const address = JSON.parse(process.env.BOXTEAM_E2E_TEST_CONTROL);
  return new Promise((resolve, reject) => {
    const connection = createConnection(address.port, address.host);
    connection.setEncoding("utf8");
    let buffer = "";
    connection.on("connect", () => connection.write(JSON.stringify({ ...payload, token: address.token, pid: process.pid }) + "\n"));
    connection.on("data", (chunk) => {
      buffer += chunk;
      const newline = buffer.indexOf("\n");
      if (newline < 0) return;
      connection.end();
      const result = JSON.parse(buffer.slice(0, newline));
      if (result.error) reject(new Error(result.error));
      else resolve(result.value);
    });
    connection.on("error", reject);
    connection.on("end", () => { if (!buffer.includes("\n")) reject(new Error("外置 test control 未返回完整回执")); });
  });
}

export async function planGoldenLoop(sessionId, threadId) {
  const cassette = JSON.parse(await readFile("tests/fixtures/model_stream/handwritten/openai_chat/basic-chat-tool-loop.json", "utf8"));
  // 完整产品的资源与通知各占独立消息；回放按精确调用身份分配原始 SSE。
  const interactions = cassette.interactions.map((interaction) => ({
    ...interaction,
    request: { ...interaction.request, match: {
      model: interaction.request.match.model,
      stream: interaction.request.match.stream,
    } },
  }));
  return testControl({ action: "plan_model", session_id: sessionId, thread_id: threadId, interactions });
}

/** 原始 Provider SSE fixture；产品 parser、工具与存储仍由真实应用执行。 */
export function chatInteraction({ label, reasoning = "", text = "", calls = [], usage }) {
  const frames = [];
  const push = (delta, finishReason = null) => frames.push({
    kind: "data", encoding: "json", payload: {
      id: label, object: "chat.completion.chunk", created: 1, model: "big-pickle",
      choices: [{ index: 0, delta, finish_reason: finishReason }],
    },
  });
  push({ role: "assistant", ...(reasoning ? { reasoning_content: reasoning } : {}) });
  for (const [index, call] of calls.entries()) {
    push({ tool_calls: [{ index, id: call.id, type: "function", function: { name: call.name, arguments: JSON.stringify(call.arguments ?? {}) } }] });
  }
  if (text) push({ content: text });
  push({}, calls.length ? "tool_calls" : "stop");
  if (usage) frames.push({ kind: "data", encoding: "json", payload: {
    id: label, object: "chat.completion.chunk", created: 1, model: "big-pickle", choices: [],
    usage: { prompt_tokens: usage.input, completion_tokens: usage.output, total_tokens: usage.input + usage.output },
  } });
  frames.push({ kind: "done", encoding: "text", payload: "[DONE]" });
  return {
    request: { method: "POST", url: "https://opencode.ai/zen/v1/chat/completions", match: { model: "big-pickle", stream: true } },
    response: { status: 200, headers: { "content-type": "text/event-stream" }, frames },
  };
}

export async function planChatCalls(sessionId, threadId, responses) {
  return testControl({ action: "plan_model", session_id: sessionId, thread_id: threadId, interactions: responses.map(chatInteraction) });
}

export function uuidText(hex) {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
