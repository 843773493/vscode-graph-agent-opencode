export interface SerialTaskQueue {
  enqueue: (task: () => Promise<void>) => Promise<void>;
}

/** 完整 Gateway 列表的 I/O 按调用顺序执行；失败只传给该任务，后续修改仍会执行。 */
export function createSerialTaskQueue() {
  let tail = Promise.resolve();
  return {
    enqueue<Result>(task: () => Promise<Result>, consume?: (result: Result) => void): Promise<Result> {
      const operation = tail.then(async () => {
        const result = await task();
        consume?.(result);
        return result;
      });
      tail = operation.then(() => undefined, () => undefined);
      return operation;
    },
  };
}

export function createLatestSerialTaskQueue(): SerialTaskQueue {
  let tail = Promise.resolve();
  let latestSequence = 0;
  return {
    enqueue(task) {
      const sequence = ++latestSequence;
      const operation = tail
        .catch(() => undefined)
        .then(async () => {
          if (sequence !== latestSequence) {
            return;
          }
          try {
            await task();
          } catch (error) {
            if (sequence !== latestSequence) {
              return;
            }
            throw error;
          }
        });
      tail = operation;
      return operation;
    },
  };
}
