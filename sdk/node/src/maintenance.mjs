import { setTimeout as sleep } from "node:timers/promises";

const MAX_INTERVAL_MS = 2_147_483_647;

function validateTasks(tasks) {
  if (!Array.isArray(tasks) || tasks.length === 0) throw new TypeError("维护任务必须是非空数组");
  const ids = new Set();
  return tasks.map((task, index) => {
    if (task === null || typeof task !== "object" || Array.isArray(task)) {
      throw new TypeError(`维护任务 ${index} 必须是对象`);
    }
    if (typeof task.id !== "string" || !task.id.trim() || ids.has(task.id)) {
      throw new TypeError(`维护任务 ${index} 的 id 必须非空且唯一`);
    }
    if (!Number.isSafeInteger(task.interval_ms) || task.interval_ms < 1 || task.interval_ms > MAX_INTERVAL_MS) {
      throw new RangeError(`维护任务 ${task.id} 的 interval_ms 必须在 1 至 ${MAX_INTERVAL_MS} 之间`);
    }
    if (typeof task.run_immediately !== "boolean") {
      throw new TypeError(`维护任务 ${task.id} 的 run_immediately 必须是布尔值`);
    }
    if (typeof task.run !== "function") throw new TypeError(`维护任务 ${task.id} 缺少 run 回调`);
    ids.add(task.id);
    return Object.freeze({ ...task });
  });
}

function isAbort(error) {
  return error?.name === "AbortError" || error?.code === "ABORT_ERR";
}

async function runTask(task, identity, reportError, signal) {
  let first = true;
  while (!signal.aborted) {
    if (!first || !task.run_immediately) {
      try {
        await sleep(task.interval_ms, undefined, { signal, ref: false });
      } catch (error) {
        if (signal.aborted && isAbort(error)) return;
        throw error;
      }
    }
    first = false;
    if (signal.aborted) return;

    const startedAt = performance.now();
    try {
      await task.run(signal);
    } catch (error) {
      if (signal.aborted) {
        if (isAbort(error)) return;
        throw error;
      }
      try {
        await reportError(task.id, identity, performance.now() - startedAt, error);
      } catch (reportingError) {
        throw new AggregateError([error, reportingError], `维护任务 ${task.id} 失败且错误报告失败`, { cause: error });
      }
    }
  }
}

/** 注册固定的进程内维护任务；各任务独立运行，关闭会取消等待并排空在途回调。 */
export function startMaintenance({ tasks, identity, reportError }) {
  if (typeof reportError !== "function") throw new TypeError("维护任务需要错误报告回调");
  const validatedTasks = validateTasks(tasks);
  const controller = new AbortController();
  let closePromise = null;
  let closed = false;
  const runners = validatedTasks.map(task => runTask(task, identity, reportError, controller.signal));
  // 正常任务错误已交给 reportError；报告器或循环本身失效时沿宿主未处理错误出口暴露。
  for (const runner of runners) {
    void runner.catch(error => {
      if (!controller.signal.aborted) queueMicrotask(() => { throw error; });
    });
  }

  return Object.freeze({
    close() {
      if (closed) return Promise.resolve();
      if (closePromise) return closePromise;
      controller.abort();
      closePromise = Promise.allSettled(runners).then(results => {
        closed = true;
        const errors = results.filter(result => result.status === "rejected").map(result => result.reason);
        if (errors.length === 1) throw errors[0];
        if (errors.length > 1) throw new AggregateError(errors, "后台维护任务关闭时失败");
      }).finally(() => { closePromise = null; });
      return closePromise;
    },
  });
}
