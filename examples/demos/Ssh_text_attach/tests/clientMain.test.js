import { afterEach, expect, test } from "bun:test";

let restoreGlobals = () => {};

afterEach(() => {
  restoreGlobals();
});

function createElement() {
  const listeners = new Map();
  return {
    value: "",
    textContent: "",
    className: "",
    hidden: false,
    disabled: false,
    dataset: {},
    children: [],
    addEventListener(type, listener) {
      const typeListeners = listeners.get(type) || [];
      typeListeners.push(listener);
      listeners.set(type, typeListeners);
    },
    dispatch(type) {
      for (const listener of listeners.get(type) || []) {
        listener({ target: this });
      }
    },
    click() {
      this.dispatch("click");
    },
    append(...children) {
      this.children.push(...children);
    },
    replaceChildren(...children) {
      this.children = [...children];
    },
  };
}

function createResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}

function createDeferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

async function waitFor(predicate, description) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (predicate()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`等待客户端状态超时: ${description}`);
}

test("保存和刷新响应不会覆盖请求期间继续编辑的草稿", async () => {
  const originalGlobals = {
    document: globalThis.document,
    fetch: globalThis.fetch,
    window: globalThis.window,
  };
  restoreGlobals = () => {
    for (const [name, value] of Object.entries(originalGlobals)) {
      if (value === undefined) {
        Reflect.deleteProperty(globalThis, name);
      } else {
        globalThis[name] = value;
      }
    }
    restoreGlobals = () => {};
  };

  const selectors = [
    "#api-origin",
    "#target-list",
    "#active-kind",
    "#active-title",
    "#attach-button",
    "#refresh-button",
    "#save-button",
    "#status-text",
    "#file-path",
    "#updated-at",
    "#file-editor",
    "#toast",
  ];
  const elements = new Map(selectors.map((selector) => [selector, createElement()]));
  const deferredRequests = [];
  let managedFileReads = 0;
  const initialFile = {
    path: ".boxteam/note.txt",
    content: "initial content",
    updatedAt: "2026-10-22T12:00:00.000Z",
  };

  globalThis.document = {
    querySelector: (selector) => elements.get(selector) || null,
    createElement,
  };
  globalThis.window = {
    location: { protocol: "http:", hostname: "localhost" },
  };
  globalThis.fetch = async (input, options = {}) => {
    const url = String(input);
    if (url === "/runtime-config") {
      return createResponse({ gatewayPort: 8014 });
    }
    if (new URL(url).pathname === "/api/targets") {
      return createResponse({
        targets: [{ id: "local", name: "本地目标", kind: "local", backendOrigin: "http://127.0.0.1:8010" }],
        defaultTargetId: "local",
      });
    }

    const method = options.method || "GET";
    if (method === "GET" && managedFileReads === 0) {
      managedFileReads += 1;
      return createResponse({ file: initialFile });
    }

    const deferred = createDeferred();
    deferredRequests.push({ method, body: options.body, ...deferred });
    return deferred.promise;
  };

  await import(`../src/client/main.js?fixture=${Date.now()}`);
  const element = (selector) => elements.get(selector);
  await waitFor(() => element("#attach-button").disabled === false, "目标列表初始化");
  element("#attach-button").click();
  await waitFor(() => element("#file-editor").value === initialFile.content, "attach 初始文件");

  const editor = element("#file-editor");
  const targetButton = element("#target-list").children[0];
  editor.value = "保存请求中的内容";
  editor.dispatch("input");
  expect(element("#target-list").children[0]).toBe(targetButton);
  element("#save-button").click();
  await waitFor(() => deferredRequests.some((request) => request.method === "PUT"), "保存请求发出");

  const saveRequest = deferredRequests.find((request) => request.method === "PUT");
  expect(JSON.parse(saveRequest.body)).toEqual({ content: "保存请求中的内容" });
  expect(element("#save-button").disabled).toBe(true);
  expect(editor.disabled).toBe(false);
  const savingTargetButton = element("#target-list").children[0];
  editor.value = "保存期间新写的草稿";
  editor.dispatch("input");
  expect(element("#target-list").children[0]).toBe(savingTargetButton);
  saveRequest.resolve(createResponse({
    file: {
      ...initialFile,
      content: "保存请求中的内容",
      updatedAt: "2026-10-22T12:01:00.000Z",
    },
  }));

  await waitFor(() => element("#status-text").textContent === "已修改", "保存后保留新草稿");
  expect(editor.value).toBe("保存期间新写的草稿");
  expect(element("#save-button").disabled).toBe(false);

  element("#refresh-button").click();
  await waitFor(() => deferredRequests.some((request) => request.method === "GET"), "刷新请求发出");
  const refreshRequest = deferredRequests.find((request) => request.method === "GET");
  expect(element("#refresh-button").disabled).toBe(true);
  editor.value = "刷新期间新写的草稿";
  editor.dispatch("input");
  refreshRequest.resolve(createResponse({
    file: {
      ...initialFile,
      content: "服务端刷新内容",
      updatedAt: "2026-10-22T12:02:00.000Z",
    },
  }));

  await waitFor(() => element("#status-text").textContent === "已修改", "刷新后保留新草稿");
  expect(editor.value).toBe("刷新期间新写的草稿");
  expect(element("#updated-at").textContent).not.toBe("-");

  element("#refresh-button").click();
  await waitFor(() => deferredRequests.filter((request) => request.method === "GET").length === 2, "后续刷新请求发出");
  const latestRefreshRequest = deferredRequests.filter((request) => request.method === "GET")[1];
  const latestFile = {
    ...initialFile,
    content: "无并发编辑时使用服务端快照",
    updatedAt: "2026-10-22T12:03:00.000Z",
  };
  latestRefreshRequest.resolve(createResponse({ file: latestFile }));

  await waitFor(() => editor.value === latestFile.content, "刷新快照替换未变化的草稿");
  expect(element("#status-text").textContent).toBe("已 attach");
});
