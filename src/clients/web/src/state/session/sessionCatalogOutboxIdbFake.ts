/**
 * 会话目录 outbox 测试专用 IndexedDB 桩（仅测试使用，不进入生产打包路径）。
 *
 * 仓库不为测试引入 fake-indexeddb 依赖，因此这里只实现 outbox 持久层真正用到的那一小片
 * API：`open`/`onupgradeneeded`/`createObjectStore`、复合 keyPath 的 `put`/`delete`、
 * 分区索引 `getAll`，以及事务完成回调。它不是通用 IndexedDB 实现：不支持游标、版本
 * 升级、跨标签页版本变更或并发事务隔离；一旦生产代码用到这里没有的语义，必须显式
 * 补桩或改用真实浏览器验证，而不是放宽断言。
 */

type StoredRecord = Record<string, unknown>;

class StubRequest<T> {
  onsuccess: (() => void) | null = null;
  onerror: (() => void) | null = null;
  error: { message: string } | null = null;
  result!: T;

  settle(result: T): void {
    this.result = result;
    queueMicrotask(() => this.onsuccess?.());
  }
}

class StubObjectStore {
  private readonly records = new Map<string, StoredRecord>();

  constructor(private readonly keyPath: readonly string[]) {}

  put(record: StoredRecord): StubRequest<string> {
    const key = JSON.stringify(this.keyPath.map((field) => record[field]));
    this.records.set(key, record);
    const request = new StubRequest<string>();
    request.settle(key);
    return request;
  }

  delete(key: readonly unknown[]): StubRequest<undefined> {
    this.records.delete(JSON.stringify(key));
    const request = new StubRequest<undefined>();
    request.settle(undefined);
    return request;
  }

  getAllByIndex(field: string, value: unknown): StubRequest<StoredRecord[]> {
    const request = new StubRequest<StoredRecord[]>();
    request.settle([...this.records.values()].filter((record) => record[field] === value));
    return request;
  }

  index(field: string): { getAll: (value: unknown) => StubRequest<StoredRecord[]> } {
    return { getAll: (value) => this.getAllByIndex(field, value) };
  }

  createIndex(name: string, field: string, options: { unique: boolean }): void {
    void name;
    void field;
    void options;
  }
}

class StubTransaction {
  oncomplete: (() => void) | null = null;
  onabort: (() => void) | null = null;
  onerror: (() => void) | null = null;
  error: { message: string } | null = null;

  constructor(private readonly stores: ReadonlyMap<string, StubObjectStore>) {
    queueMicrotask(() => this.oncomplete?.());
  }

  objectStore(name: string): StubObjectStore {
    const store = this.stores.get(name);
    if (!store) throw new Error(`桩数据库没有该对象仓库: ${name}`);
    return store;
  }
}

class StubDatabase {
  readonly objectStoreNames: { contains: (name: string) => boolean };

  constructor(private readonly stores: Map<string, StubObjectStore>) {
    this.objectStoreNames = { contains: (name) => stores.has(name) };
  }

  createObjectStore(name: string, options: { keyPath: readonly string[] }): StubObjectStore {
    const store = new StubObjectStore(options.keyPath);
    this.stores.set(name, store);
    return store;
  }

  transaction(name: string): StubTransaction {
    void name;
    return new StubTransaction(this.stores);
  }

  close(): void {}
}

/**
 * 进程级桩工厂：同名数据库共享同一份仓库表，从而模拟「刷新/重开后读到原数据」。
 * `deleteDatabase` 会真正丢弃数据，供用例之间隔离。
 */
export function createIndexedDbFake(): IDBFactory {
  const databases = new Map<string, StubDatabase>();
  const storesByName = new Map<string, Map<string, StubObjectStore>>();

  const open = (name: string): IDBOpenDBRequest => {
    const stores = storesByName.get(name) ?? new Map<string, StubObjectStore>();
    storesByName.set(name, stores);
    const isNew = !databases.has(name);
    const database = databases.get(name) ?? new StubDatabase(stores);
    databases.set(name, database);

    const request = new StubRequest<StubDatabase>();
    const handle = request as unknown as {
      onupgradeneeded: (() => void) | null;
      onsuccess: (() => void) | null;
      onerror: (() => void) | null;
      result: StubDatabase;
    };
    handle.onupgradeneeded = null;
    handle.result = database;
    if (isNew) queueMicrotask(() => handle.onupgradeneeded?.());
    queueMicrotask(() => handle.onsuccess?.());
    return handle as unknown as IDBOpenDBRequest;
  };

  const deleteDatabase = (name: string): IDBOpenDBRequest => {
    databases.delete(name);
    storesByName.delete(name);
    const request = new StubRequest<undefined>();
    request.settle(undefined);
    return request as unknown as IDBOpenDBRequest;
  };

  return { open, deleteDatabase } as unknown as IDBFactory;
}
