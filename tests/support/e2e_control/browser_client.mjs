import { createConnection } from "node:net";
import { readFile } from "node:fs/promises";
import { constants, closeSync, existsSync, fsyncSync, openSync, writeSync } from "node:fs";
import { spawn, spawnSync } from "node:child_process";
import { resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const FORK_OPERATION_TIMEOUT_MS = 60_000;
const FORK_WATCHDOG_ENTRY = fileURLToPath(import.meta.url);
const FORK_DEADLINE_ENTRY = resolve(process.cwd(), "tests/support/fork_deadline.py");
let forkWatchdog;
let forkOperationOrdinal = 0;
const forkOperations = new Map();
const forkActiveDeadlineTokens = new Set();

function forkOperationIdentity(target, operationKey) {
  return `${target}\0${operationKey}`;
}

function forkDeadlinePython() {
  const environmentRoot = process.env.VIRTUAL_ENV
    || process.env.UV_PROJECT_ENVIRONMENT
    || resolve(process.cwd(), ".venv");
  const executable = resolve(
    environmentRoot,
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  if (!existsSync(executable)) {
    throw new Error(`fork deadline Python 不存在: platform=${process.platform}, path=${executable}`);
  }
  return executable;
}

function runForkDeadlinePython(arguments_) {
  const result = spawnSync(forkDeadlinePython(), [FORK_DEADLINE_ENTRY, ...arguments_], {
    cwd: process.cwd(),
    encoding: "utf8",
    timeout: 15_000,
  });
  if (result.error || result.status !== 0) {
    throw new Error(
      "fork deadline 跨平台进程操作失败: "
      + `action=${arguments_[0]}, status=${String(result.status)}, signal=${String(result.signal)}, `
      + `stderr=${result.stderr?.trim() || "<empty>"}`,
      { cause: result.error },
    );
  }
  return result.stdout.trim();
}

function assertIsolatedDriver(pid) {
  const result = runForkDeadlinePython(["--browser-driver-identity", String(pid)]);
  const identity = JSON.parse(result);
  if (identity.pid !== pid) throw new Error(`fork deadline 返回了错误的浏览器驱动身份: ${result}`);
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
  constructor(artifactRoot, targetPid, targetIdentity) {
    this.artifactRoot = validateForkArtifactRoot(artifactRoot);
    this.pending = new Map();
    this.buffer = "";
    this.child = spawn(process.execPath, [
      FORK_WATCHDOG_ENTRY,
      "--browser-driver-watchdog",
      String(targetPid),
      JSON.stringify(targetIdentity),
      this.artifactRoot,
    ], { cwd: process.cwd(), detached: true, stdio: ["pipe", "pipe", "inherit"] });
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
  forkWatchdog = new ForkDeadlineWatchdogClient(artifactRoot, process.pid, identity);
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

function writeForkDeadlineDiagnostic(directory, record, now) {
  const path = resolve(directory, `fork-deadline-${now}-${record.token}.json`);
  const payload = {
    target: record.target,
    operation_key: record.operation_key,
    last_stage: record.last_stage,
    elapsed_seconds: Number((Number(now - record.started_ns) / 1_000_000_000).toFixed(3)),
    backend_state: "unknown",
    error: "fork operation deadline exceeded; watchdog terminated the isolated browser driver process scope",
  };
  const descriptor = openSync(path, "wx", 0o600);
  try {
    writeSync(descriptor, `${JSON.stringify(payload, null, 2)}\n`);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  if (process.platform === "win32") return;
  const directoryDescriptor = openSync(directory, constants.O_RDONLY);
  try {
    fsyncSync(directoryDescriptor);
  } finally {
    closeSync(directoryDescriptor);
  }
}

async function runForkDeadlineWatchdog(targetPid, targetIdentity, artifactRoot) {
  runForkDeadlinePython([
    "--browser-driver-validate",
    String(targetPid),
    String(process.pid),
    JSON.stringify(targetIdentity),
  ]);
  const active = new Map();
  const identities = new Map();
  let buffer = "";
  let timer;
  let ordinal = 0;
  let expiring = false;
  let watchdogExitCode = 0;
  const reply = (id, value, error) => process.stdout.write(`${JSON.stringify({ id, value, error })}\n`);
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
      runForkDeadlinePython([
        "--browser-driver-terminate",
        String(targetPid),
        String(process.pid),
        JSON.stringify(targetIdentity),
      ]);
    } catch (error) {
      watchdogExitCode = 1;
      try {
        writeSync(2, `fork watchdog 无法终止已验证 browser driver: ${String(error)}\n`);
      } catch {
        // 目标树身份或终止失败仍通过非零退出报告。
      }
      return process.exit(watchdogExitCode || 1);
    }
    process.exit(watchdogExitCode);
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

if (process.argv[2] === "--browser-driver-watchdog") {
  await runForkDeadlineWatchdog(Number(process.argv[3]), JSON.parse(process.argv[4]), process.argv[5]);
}

export async function testControl(payload) {
  const address = JSON.parse(process.env.BOXTEAM_E2E_TEST_CONTROL);
  return new Promise((resolve, reject) => {
    const connection = createConnection(address.port, address.host);
    connection.setEncoding("utf8");
    const responseChunks = [];
    let receivedReply = false;
    connection.on("connect", () => connection.write(JSON.stringify({ ...payload, token: address.token, pid: process.pid }) + "\n"));
    connection.on("data", (chunk) => {
      if (receivedReply) return;
      const newline = chunk.indexOf("\n");
      if (newline < 0) {
        responseChunks.push(chunk);
        return;
      }
      receivedReply = true;
      responseChunks.push(chunk.slice(0, newline));
      connection.end();
      const responseText = responseChunks.join("");
      responseChunks.length = 0;
      try {
        const result = JSON.parse(responseText);
        if (result.error) reject(new Error(result.error));
        else resolve(result.value);
      } catch (error) {
        connection.destroy();
        reject(error);
      }
    });
    connection.on("error", reject);
    connection.on("end", () => { if (!receivedReply) reject(new Error("外置 test control 未返回完整回执")); });
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

/** Web 只声明响应内容；原始 stream 资产由 Python model_stream 统一生成。 */
export async function planChatCalls(sessionId, threadId, responses) {
  const planned = responses.map(({ label, reasoning = "", text = "", calls = [], usage }) => ({
    response_id: label,
    deltas: [
      { role: "assistant", ...(reasoning ? { reasoning_content: reasoning } : {}) },
      ...calls.map((call, index) => ({ tool_calls: [{ index, id: call.id, type: "function",
        function: { name: call.name, arguments: JSON.stringify(call.arguments ?? {}) } }] })),
      ...(text ? [{ content: text }] : []),
    ],
    finish_reason: calls.length ? "tool_calls" : "stop",
    ...(usage ? { usage: { prompt_tokens: usage.input, completion_tokens: usage.output,
      total_tokens: usage.input + usage.output } } : {}),
  }));
  return testControl({ action: "plan_chat", session_id: sessionId, thread_id: threadId, responses: planned });
}

export function uuidText(hex) {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
