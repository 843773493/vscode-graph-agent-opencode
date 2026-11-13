export class SnapshotReadSupersededError extends Error {
  constructor() {
    super("同步读取已被更新的读取或写入取代");
    this.name = "SnapshotReadSupersededError";
  }
}

interface PendingRead<T> {
  epoch: number;
  controller: AbortController;
  promise: Promise<T>;
}

export interface SnapshotReadCoordinator {
  read<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Promise<T>;
  invalidate(key: string): void;
  dispose(): void;
}

export function createSnapshotReadCoordinator(): SnapshotReadCoordinator {
  const epochs = new Map<string, number>();
  const pending = new Map<string, PendingRead<unknown>>();

  return {
    read<T>(key: string, load: (signal: AbortSignal) => Promise<T>): Promise<T> {
      const current = pending.get(key);
      if (current) return current.promise as Promise<T>;
      const epoch = epochs.get(key) ?? 0;
      const controller = new AbortController();
      const assertCurrent = () => {
        if ((epochs.get(key) ?? 0) !== epoch || controller.signal.aborted) {
          throw new SnapshotReadSupersededError();
        }
      };
      const promise = Promise.resolve().then(() => {
        assertCurrent();
        return load(controller.signal);
      }).then((value) => {
        assertCurrent();
        return value;
      }, (error: unknown) => {
        assertCurrent();
        throw error;
      }).finally(() => {
        if (pending.get(key)?.promise === promise) pending.delete(key);
      });
      pending.set(key, { epoch, controller, promise });
      return promise;
    },
    invalidate(key: string): void {
      const active = pending.get(key);
      if (active) {
        epochs.set(key, active.epoch + 1);
        active.controller.abort();
      } else {
        epochs.delete(key);
      }
      pending.delete(key);
    },
    dispose(): void {
      for (const key of pending.keys()) {
        epochs.set(key, (epochs.get(key) ?? 0) + 1);
        pending.get(key)?.controller.abort();
      }
      pending.clear();
      epochs.clear();
    },
  };
}

export function applyVersionedValue<T>(
  current: T | null,
  incoming: T,
  versionOf: (value: T) => number,
): T {
  return current !== null && versionOf(incoming) < versionOf(current)
    ? current
    : incoming;
}

export function assertMinimumVersion(actual: number, minimum: number): void {
  if (actual < minimum) {
    throw new Error(`同步读取版本不足: actual=${actual}, minimum=${minimum}`);
  }
}

/** 同一 owner 的控制操作串行；前一项失败时，已排队的依赖操作不再发送。 */
export function createSerialOperationQueue() {
  const pending = new Map<string, Promise<unknown>>();
  return {
    run<T>(key: string, operation: () => Promise<T>): Promise<T> {
      const request = (pending.get(key) ?? Promise.resolve()).then(operation);
      pending.set(key, request);
      const release = () => { if (pending.get(key) === request) pending.delete(key); };
      void request.then(release, release);
      return request;
    },
  };
}
