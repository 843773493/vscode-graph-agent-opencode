import { describe, expect, test } from "bun:test";
import {
  createLatestSerialTaskQueue,
  createSerialTaskQueue,
} from "./serialTaskQueue";

function deferred(): {
  promise: Promise<void>;
  resolve: () => void;
} {
  let resolve: (() => void) | null = null;
  const promise = new Promise<void>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return {
    promise,
    resolve: () => {
      if (!resolve) {
        throw new Error("deferred 尚未初始化");
      }
      resolve();
    },
  };
}

describe("工作区切换串行队列", () => {
  test("普通队列不会并发执行两个 Gateway 激活请求", async () => {
    const queue = createSerialTaskQueue();
    const first = deferred();
    const firstStarted = deferred();
    const calls: string[] = [];
    const firstOperation = queue.enqueue(async () => {
      calls.push("first:start");
      firstStarted.resolve();
      await first.promise;
      calls.push("first:end");
    });
    const secondOperation = queue.enqueue(async () => {
      calls.push("second");
    });

    await firstStarted.promise;
    expect(calls).toEqual(["first:start"]);
    first.resolve();
    await Promise.all([firstOperation, secondOperation]);
    expect(calls).toEqual(["first:start", "first:end", "second"]);
  });

  test("会话队列保留执行中的请求并只执行最新等待目标", async () => {
    const queue = createLatestSerialTaskQueue();
    const first = deferred();
    const firstStarted = deferred();
    const calls: string[] = [];
    const firstOperation = queue.enqueue(async () => {
      calls.push("workspace-a:start");
      firstStarted.resolve();
      await first.promise;
      calls.push("workspace-a:end");
    });
    await firstStarted.promise;
    const skippedOperation = queue.enqueue(async () => {
      calls.push("workspace-b");
    });
    const latestOperation = queue.enqueue(async () => {
      calls.push("workspace-c");
    });

    first.resolve();
    await Promise.all([firstOperation, skippedOperation, latestOperation]);
    expect(calls).toEqual([
      "workspace-a:start",
      "workspace-a:end",
      "workspace-c",
    ]);
  });
});
