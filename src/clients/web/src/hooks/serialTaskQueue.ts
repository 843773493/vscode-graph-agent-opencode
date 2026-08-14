export interface SerialTaskQueue {
  enqueue: (task: () => Promise<void>) => Promise<void>;
}

export function createSerialTaskQueue(): SerialTaskQueue {
  let tail = Promise.resolve();
  return {
    enqueue(task) {
      const operation = tail.catch(() => undefined).then(task);
      tail = operation;
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
