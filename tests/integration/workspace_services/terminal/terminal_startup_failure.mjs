// 只在真实Worker就绪IPC交付前注入错误，不代填PTY、Session、进程身份或终态。
import { appendFile, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";

const projectRoot = process.env.BOXTEAM_TEST_PROJECT_ROOT;
const controlRoot = process.env.BOXTEAM_TEST_TERMINAL_CONTROL;
const moduleUrl = relative => pathToFileURL(path.join(projectRoot, relative)).href;
const { IsolatedPtyProcess } = await import(moduleUrl("src/workspace-services/terminal/server/processes/isolatedPtyProcess.js"));
const { readProcessStat } = await import(moduleUrl("src/workspace-services/terminal/server/processes/terminalProcessLifecycle.js"));
const start = IsolatedPtyProcess.prototype.start;

IsolatedPtyProcess.prototype.start = async function (options) {
  const pending = start.call(this, options);
  if (!options.args.some(arg => arg.includes("gated-terminal-startup-cleanup"))) {
    const ready = await pending;
    await appendFile(path.join(controlRoot, "starts.jsonl"), `${JSON.stringify(await readProcessStat(ready.pid))}\n`);
    return ready;
  }
  const worker = this.workerProcess;
  const emit = worker.emit.bind(worker);
  const kill = worker.kill.bind(worker);
  worker.kill = signal => {
    if (signal === "SIGTERM") throw new Error("测试屏障拒绝启动失败时的Worker信号");
    return kill(signal);
  };
  worker.emit = (event, ...arguments_) => {
    const [message] = arguments_;
    if (event !== "message" || message?.type !== "ready") return emit(event, ...arguments_);
    void (async () => {
      let workload;
      const deadline = performance.now() + 5000;
      while (performance.now() < deadline) {
        try {
          workload = JSON.parse(await readFile(path.join(controlRoot, "workload.json"), "utf8"));
          break;
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          await delay(10);
        }
      }
      if (!workload) throw new Error("真实PTY工作负载未在期限内交付身份");
      if (workload.pid !== message.pid) throw new Error("真实PTY就绪与工作负载身份不符");
      const identity = await readProcessStat(message.pid);
      const observed = { root: identity, child: await readProcessStat(workload.child_pid), worker: await readProcessStat(this.workerPid) };
      await writeFile(path.join(controlRoot, "observed.pending"), JSON.stringify(observed));
      await rename(path.join(controlRoot, "observed.pending"), path.join(controlRoot, "observed.json"));
      const mode = await readFile(path.join(controlRoot, "mode"), "utf8");
      if (mode === "stop-fails") {
        const sendSignal = process.kill.bind(process);
        let rejected = false;
        process.kill = (pid, signal) => {
          if (!rejected && pid === message.pid && signal === "SIGTERM") {
            rejected = true;
            throw Object.assign(new Error("测试屏障拒绝首次PTY停止"), { code: "EPERM" });
          }
          return sendSignal(pid, signal);
        };
      }
      emit("error", new Error("测试屏障：原生创建后、就绪交付前失败"));
    })().catch(error => emit("error", error));
    return true;
  };
  return await pending;
};
