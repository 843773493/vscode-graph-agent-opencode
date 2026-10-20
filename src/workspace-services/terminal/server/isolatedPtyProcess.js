import { fork } from "node:child_process";

const WORKER_START_TIMEOUT_MS = 10_000;
const WORKER_SHUTDOWN_GRACE_MS = 3_000;
const WORKER_EXIT_CONFIRMATION_TIMEOUT_MS = 3_000;

function workerPath() {
  return new URL("./ptyWorker.js", import.meta.url);
}

export class IsolatedPtyProcess {
  constructor() {
    /** @type {import("node:child_process").ChildProcess|null} 持有PTY的隔离Node进程，不是PTY执行根。 */
    this.workerProcess = null;
    /** @type {number|null} worker就绪消息中的PTY执行根PID；保留与node-pty相同的pid含义。 */
    this.pid = null;
    this.ptyOutputHandlers = new Set();
    this.exitObservationHandlers = new Set();
    /** @type {"created"|"starting"|"running"|"stopping"|"exited"} worker生命周期，不是终端业务状态。 */
    this.workerState = "created";
    // 仅表示宿主主动安排worker退出，启动失败也可置为true。
    this.workerShutdownExpected = false;
    // PTY退出消息与worker退出事件共用一次交付；不表示OS进程树已核实回收。
    this.exitObservationEmitted = false;
  }

  /** 隔离Node进程的PID，与pid中的PTY执行根分开。 */
  get workerPid() {
    return this.workerProcess?.pid ?? null;
  }

  onData(handler) {
    this.ptyOutputHandlers.add(handler);
    return { dispose: () => this.ptyOutputHandlers.delete(handler) };
  }

  onExit(handler) {
    this.exitObservationHandlers.add(handler);
    return { dispose: () => this.exitObservationHandlers.delete(handler) };
  }

  async start({ command, args, options }) {
    if (this.workerState !== "created") {
      throw new Error(`隔离 PTY Worker 状态不允许启动: state=${this.workerState}`);
    }
    this.workerState = "starting";
    const workerProcess = fork(workerPath(), [], {
      stdio: ["ignore", "ignore", "ignore", "ipc"],
    });
    this.workerProcess = workerProcess;
    const ptyReadyPromise = new Promise((resolve, reject) => {
      let readySettled = false;
      const timeout = setTimeout(() => {
        failStart(new Error(`隔离 PTY Worker 启动超时: worker_pid=${workerProcess.pid}`));
      }, WORKER_START_TIMEOUT_MS);

      const completeStart = (value) => {
        if (readySettled) {
          return;
        }
        readySettled = true;
        clearTimeout(timeout);
        this.workerState = "running";
        resolve(value);
      };
      const failStart = (error) => {
        if (readySettled) {
          return;
        }
        readySettled = true;
        clearTimeout(timeout);
        this.workerShutdownExpected = true;
        if (this.workerState !== "exited") {
          this.workerState = "stopping";
          workerProcess.kill("SIGTERM");
        }
        reject(error);
      };

      workerProcess.on("message", (message) => {
        if (!message || typeof message !== "object") {
          return;
        }
        if (message.type === "ready") {
          this.pid = message.pid;
          completeStart({ pid: message.pid });
          return;
        }
        if (message.type === "output") {
          try {
            for (const handler of this.ptyOutputHandlers) {
              handler(message.data);
            }
          } finally {
            if (workerProcess.connected) {
              // 单个IPC输出包已结束宿主分发，仅释放worker读取背压，不表示UI已显示或模型已消费。
              workerProcess.send({ type: "ack", messageId: message.messageId });
            }
          }
          return;
        }
        if (message.type === "exit") {
          this._emitExit({
            exitCode: message.exitCode,
            signal: message.signal,
            workerExit: false,
            cleanupResult: message.cleanupResult,
            error: message.error ? new Error(message.error) : undefined,
          });
          return;
        }
        if (message.type === "startupError") {
          failStart(new Error(`隔离 PTY Worker 错误: ${message.message}`));
          return;
        }
        if (message.type === "error") {
          const error = new Error(`隔离 PTY Worker 错误: ${message.message}`);
          if (this.pid === null) {
            failStart(error);
          } else {
            this._emitExit({ exitCode: -1, signal: 0, workerExit: true, error });
            this.workerState = "stopping";
            workerProcess.kill("SIGKILL");
          }
        }
      });

      workerProcess.once("error", (error) => {
        if (this.pid === null) {
          failStart(error);
        } else {
          this._emitExit({ exitCode: -1, signal: 0, workerExit: true, error });
          this.workerState = "stopping";
          workerProcess.kill("SIGKILL");
        }
      });
      workerProcess.once("exit", (code, signal) => {
        this.workerState = "exited";
        if (this.pid === null) {
          failStart(
            new Error(
              `隔离 PTY Worker 在终端就绪前退出: code=${code}, signal=${signal}`,
            ),
          );
          return;
        }
        this._emitExit({
          exitCode: code ?? -1,
          signal: signal ?? 0,
          workerExit: !this.workerShutdownExpected,
        });
      });
    });

    workerProcess.send({ type: "start", command, args, options });
    return await ptyReadyPromise;
  }

  write(data) {
    this._send({ type: "input", data });
  }

  resize(cols, rows) {
    this._send({ type: "resize", cols, rows });
  }

  async shutdown({ terminatePty = true } = {}) {
    const workerProcess = this.workerProcess;
    if (!workerProcess || this.workerState === "exited") {
      return;
    }
    this.workerShutdownExpected = true;
    this.workerState = "stopping";
    await new Promise((resolve, reject) => {
      let exitConfirmationTimeout;
      const cleanup = () => {
        clearTimeout(forceKillTimeout);
        clearTimeout(exitConfirmationTimeout);
        workerProcess.removeListener("exit", onExit);
      };
      const onExit = () => {
        cleanup();
        resolve();
      };
      const fail = (error) => {
        cleanup();
        reject(error);
      };
      const forceKillTimeout = setTimeout(() => {
        // 信号已发送不代表进程已退出；强杀后仍须有界等待exit确认。
        exitConfirmationTimeout = setTimeout(() => {
          fail(new Error(`隔离 PTY Worker 退出未确认: worker_pid=${workerProcess.pid}`));
        }, WORKER_EXIT_CONFIRMATION_TIMEOUT_MS);
        try {
          workerProcess.kill("SIGKILL");
        } catch (error) {
          fail(error);
        }
      }, WORKER_SHUTDOWN_GRACE_MS);
      workerProcess.once("exit", onExit);
      try {
        workerProcess.send({ type: "shutdown", terminatePty });
      } catch (error) {
        fail(error);
      }
    });
  }

  _send(message) {
    if (!this.workerProcess || this.workerState !== "running") {
      throw new Error(`隔离 PTY Worker 未运行: state=${this.workerState}`);
    }
    this.workerProcess.send(message);
  }

  /**
   * 首次退出观察可来自PTY消息或worker事件；异常worker退出时，退出码与信号属于worker。
   * @param {{exitCode: number, signal: number|string, workerExit: boolean, cleanupResult?: string, error?: Error}} event
   */
  _emitExit(event) {
    if (this.exitObservationEmitted) {
      return;
    }
    this.exitObservationEmitted = true;
    for (const handler of this.exitObservationHandlers) {
      handler(event);
    }
  }
}
