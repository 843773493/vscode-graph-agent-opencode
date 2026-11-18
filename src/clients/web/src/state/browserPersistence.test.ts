import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, spyOn, test } from "bun:test";
import { LOCAL_DATABASES, LocalDatabaseError, inspectLocalDatabases, openBrowserDatabase, setLocalDatabaseReporter } from "./browserPersistence";
import { ReportedError } from "../utils/errorMessage";

const definition = { name: "local-connection-test", version: 2 };
const names = [definition.name, ...Object.values(LOCAL_DATABASES).map(database => database.name)];
async function clearDatabases() {
  for (const name of names) await new Promise<void>((resolve, reject) => {
    const request = indexedDB.deleteDatabase(name);
    request.onsuccess = () => resolve(); request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error(`测试连接未释放：${name}`));
  });
}
beforeEach(clearDatabases);
afterEach(clearDatabases);

test("schema 失败原子回滚，普通打开能够重试", async () => {
  await expect(openBrowserDatabase(definition, database => {
    database.createObjectStore("records"); throw new Error("注入建表失败");
  })).rejects.toThrow("注入建表失败");
  const database = await openBrowserDatabase(definition, database => { database.createObjectStore("records"); });
  expect(database.version).toBe(2);
  expect(Array.from(database.objectStoreNames)).toEqual(["records"]);
  database.close();
});

test("阻塞请求失败后，旧连接释放不会迟到升级", async () => {
  const held = await openBrowserDatabase({ ...definition, version: 1 }, database => { database.createObjectStore("records"); });
  held.onversionchange = () => {};
  await expect(openBrowserDatabase(definition, database => { database.createObjectStore("late"); })).rejects.toThrow("旧窗口阻塞");
  held.close();
  const database = await openBrowserDatabase({ name: definition.name });
  expect(database.version).toBe(1);
  expect(database.objectStoreNames.contains("late")).toBe(false);
  database.close();
});

test("旧窗口仍占库时管理入口可查看状态，不把诊断和重试排在阻塞请求后", async () => {
  const held = await openBrowserDatabase({ name: LOCAL_DATABASES.catalog.name, version: 2 }, database => { database.createObjectStore("operations"); });
  held.onversionchange = () => {};
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await expect(openBrowserDatabase(LOCAL_DATABASES.catalog, () => {})).rejects.toThrow("旧窗口阻塞");
    const complete = inspectLocalDatabases().then(status => status.find(item => item.name === LOCAL_DATABASES.catalog.name));
    const result = await Promise.race([complete, new Promise<undefined>(resolve => { timer = setTimeout(() => resolve(undefined), 500); })]);
    expect(result).toMatchObject({ storedVersion: 2, stores: {}, error: expect.stringContaining("旧窗口阻塞") });
    await expect(openBrowserDatabase(LOCAL_DATABASES.catalog, () => {})).rejects.toThrow("旧窗口阻塞");
  } finally {
    clearTimeout(timer); held.close();
    // 等待已放弃的升级请求完成 abort，释放该测试的队列。
    (await openBrowserDatabase({ name: LOCAL_DATABASES.catalog.name })).close();
  }
  const database = await openBrowserDatabase(LOCAL_DATABASES.catalog, database => { database.createObjectStore("recovered"); });
  expect(database.version).toBe(3); database.close();
});

test("只有实际交付通知才标记 ReportedError，通知出口失败也会拒绝而不挂起", async () => {
  const original = new LocalDatabaseError(definition.name, "identity", new Error("保留旧身份"));
  const fail = () => openBrowserDatabase(definition, () => { throw original; });
  await expect(fail()).rejects.toBe(original);
  const reported: LocalDatabaseError[] = [];
  let dispose = setLocalDatabaseReporter(error => { reported.push(error); });
  try {
    await expect(fail()).rejects.toBeInstanceOf(ReportedError);
    expect(reported).toContain(original);
  } finally { dispose(); }
  // 先成功打开清除前次故障，再单独验证通知故障。
  (await openBrowserDatabase(definition, () => {})).close();
  dispose = setLocalDatabaseReporter(() => { throw new Error("通知出口故障"); });
  try { await expect(openBrowserDatabase({ ...definition, version: 3 }, () => { throw original; })).rejects.toBeInstanceOf(AggregateError); }
  finally { dispose(); }
});

test("按需诊断只计数，不升级旧库、不读正文，单库故障不遮蔽其他库", async () => {
  const catalog = await openBrowserDatabase({ name: LOCAL_DATABASES.catalog.name, version: 2 }, database => { database.createObjectStore("operations"); });
  await new Promise<void>((resolve, reject) => {
    const tx = catalog.transaction("operations", "readwrite");
    tx.objectStore("operations").put({ opaque: "原命令正文" }, "original");
    tx.oncomplete = () => resolve(); tx.onabort = () => reject(tx.error);
  });
  catalog.close();
  (await openBrowserDatabase(LOCAL_DATABASES.views, database => { database.createObjectStore("views"); })).close();
  const get = spyOn(IDBObjectStore.prototype, "get");
  const getAll = spyOn(IDBObjectStore.prototype, "getAll");
  const originalOpen = indexedDB.open.bind(indexedDB);
  const open = spyOn(indexedDB, "open").mockImplementation((name, version) => {
    if (name === LOCAL_DATABASES.views.name) throw new Error("注入单库不可读");
    return version === undefined ? originalOpen(name) : originalOpen(name, version);
  });
  try {
    const status = await inspectLocalDatabases();
    expect(status.find(item => item.name === LOCAL_DATABASES.views.name)?.error).toBe("注入单库不可读");
    expect(status.find(item => item.name === LOCAL_DATABASES.catalog.name)).toMatchObject({ storedVersion: 2, stores: { operations: 1 } });
    expect(status.find(item => item.name === LOCAL_DATABASES.files.name)?.storedVersion).toBeUndefined();
    expect(get).not.toHaveBeenCalled(); expect(getAll).not.toHaveBeenCalled();
    expect((await indexedDB.databases()).find(item => item.name === LOCAL_DATABASES.catalog.name)?.version).toBe(2);
    expect((await indexedDB.databases()).some(item => item.name === LOCAL_DATABASES.files.name)).toBe(false);
  } finally { open.mockRestore(); get.mockRestore(); getAll.mockRestore(); }
});
