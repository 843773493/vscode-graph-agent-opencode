import { fork } from "node:child_process";

const WORKER_START_TIMEOUT_MS = 10_000;
const WORKER_SHUTDOWN_TIMEOUT_MS = 3_000;

function workerPath() {
  return new URL("./ptyWorker.js", import.meta.url);
}

export class IsolatedPtyProcess {
  constructor() {
    this.child = null;
    this.pid = null;
    this.dataHandlers = new Set();
    this.exitHandlers = new Set();
    this.state = "created";
    this.expectedShutdown = false;
    this.exitEmitted = false;
  }

  get workerPid() {
    return this.child?.pid ?? null;
  }

  onData(handler) {
    this.dataHandlers.add(handler);
    return { dispose: () => this.dataHandlers.delete(handler) };
  }

  onExit(handler) {
    this.exitHandlers.add(handler);
    return { dispose: () => this.exitHandlers.delete(handler) };
  }

  async start({ command, args, options }) {
    if (this.state !== "created") {
      throw new Error(`隔离 PTY Worker 状态不允许启动: state=${this.state}`);
    }
    this.state = "starting";
    const child = fork(workerPath(), [], {
      stdio: ["ignore", "ignore", "ignore", "ipc"],
    });
    this.child = child;
    const startPromise = new Promise((resolve, reject) => {
      let settled = false;
      const timeout = setTimeout(() => {
        failStart(new Error(`隔离 PTY Worker 启动超时: worker_pid=${child.pid}`));
      }, WORKER_START_TIMEOUT_MS);

      const completeStart = (value) => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeout);
        this.state = "running";
        resolve(value);
      };
      const failStart = (error) => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeout);
        this.expectedShutdown = true;
        if (this.state !== "exited") {
          this.state = "stopping";
          child.kill("SIGTERM");
        }
        reject(error);
      };

      child.on("message", (message) => {
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
            for (const handler of this.dataHandlers) {
              handler(message.data);
            }
          } finally {
            if (child.connected) {
              child.send({ type: "ack", messageId: message.messageId });
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
            this.state = "stopping";
            child.kill("SIGKILL");
          }
        }
      });

      child.once("error", (error) => {
        if (this.pid === null) {
          failStart(error);
        } else {
          this._emitExit({ exitCode: -1, signal: 0, workerExit: true, error });
          this.state = "stopping";
          child.kill("SIGKILL");
        }
      });
      child.once("exit", (code, signal) => {
        this.state = "exited";
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
          workerExit: !this.expectedShutdown,
        });
      });
    });

    child.send({ type: "start", command, args, options });
    return await startPromise;
  }

  write(data) {
    this._send({ type: "input", data });
  }

  resize(cols, rows) {
    this._send({ type: "resize", cols, rows });
  }

  async shutdown({ terminatePty = true } = {}) {
    const child = this.child;
    if (!child || this.state === "exited") {
      return;
    }
    this.expectedShutdown = true;
    this.state = "stopping";
    await new Promise((resolve) => {
      const timeout = setTimeout(() => {
        child.kill("SIGKILL");
        resolve();
      }, WORKER_SHUTDOWN_TIMEOUT_MS);
      child.once("exit", () => {
        clearTimeout(timeout);
        resolve();
      });
      child.send({ type: "shutdown", terminatePty });
    });
  }

  _send(message) {
    if (!this.child || this.state !== "running") {
      throw new Error(`隔离 PTY Worker 未运行: state=${this.state}`);
    }
    this.child.send(message);
  }

  _emitExit(event) {
    if (this.exitEmitted) {
      return;
    }
    this.exitEmitted = true;
    for (const handler of this.exitHandlers) {
      handler(event);
    }
  }
}
