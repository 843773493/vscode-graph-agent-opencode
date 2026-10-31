// 测试专用就绪回执屏障：保留真实PTY/IPC，仅延迟向Session交付原就绪结果。
import { appendFile, readFile, rename, writeFile } from "node:fs/promises";
import { appendFileSync } from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const projectRoot = process.env.BOXTEAM_TEST_PROJECT_ROOT;
const controlRoot = process.env.BOXTEAM_TEST_TERMINAL_CONTROL;
const moduleUrl = (relative) => pathToFileURL(path.join(projectRoot, relative)).href;
const { IsolatedPtyProcess } = await import(moduleUrl("src/workspace-services/terminal/server/processes/isolatedPtyProcess.js"));
const { readProcessStat } = await import(moduleUrl("src/workspace-services/terminal/server/processes/terminalProcessLifecycle.js"));

async function readControl(name) {
  try {
    return await readFile(path.join(controlRoot, name), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

const start = IsolatedPtyProcess.prototype.start;
IsolatedPtyProcess.prototype.start = async function (options) {
  const pending = start.call(this, options);
  const worker = this.workerProcess;
  const send = worker.send.bind(worker);
  worker.send = (message, ...args) => {
    if (message.type === "shutdown") {
      // 观察真实关闭派发，重入同一关闭Promise不应被计作另一份副作用。
      appendFileSync(path.join(controlRoot, "shutdowns.jsonl"), `${JSON.stringify({ pid: this.pid, terminatePty: message.terminatePty })}\n`);
    }
    return send(message, ...args);
  };
  const ready = await pending;
  const identity = await readProcessStat(ready.pid);
  await appendFile(path.join(controlRoot, "starts.jsonl"), `${JSON.stringify({ ...identity, worker_pid: this.workerPid })}\n`);
  if (!options.args.some((arg) => arg.includes("gated-terminal-start"))) return ready;
  await writeFile(path.join(controlRoot, "arrived.pending"), JSON.stringify({ ...identity, worker_pid: this.workerPid }));
  await rename(path.join(controlRoot, "arrived.pending"), path.join(controlRoot, "arrived.json"));
  let fatalTriggered = false;
  while (await readControl("release") === null) {
    const fatal = await readControl("fatal");
    if (fatal && !fatalTriggered) {
      fatalTriggered = true;
      const failure = new Error(`terminal-test-fatal-${fatal}`);
      if (fatal === "exception") setTimeout(() => { throw failure; }, 0);
      else void Promise.reject(failure);
    }
    await delay(10);
  }
  return ready;
};
