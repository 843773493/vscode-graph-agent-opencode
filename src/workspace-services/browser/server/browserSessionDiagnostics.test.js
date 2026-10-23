import { EventEmitter } from "node:events";
import { runInNewContext } from "node:vm";
import { describe, expect, spyOn, test } from "bun:test";
import { BrowserSession } from "./browserSession.js";

async function registeredPage(existingBrowser = null) {
  const page = new EventEmitter();
  page.url = () => "about:blank";
  page.mainFrame = () => page;
  const cdpSession = new EventEmitter();
  cdpSession.send = async () => undefined;
  const browser = existingBrowser ?? new BrowserSession({
    manager: { persist: async () => undefined },
    record: { browser_id: "browser_network_test", status: "running" },
  });
  browser.browser = {};
  browser.context = { newCDPSession: async () => cdpSession };
  const entry = await browser.registerPage(page, { activate: false });
  browser.page = page;
  browser.cdpSession = cdpSession;
  browser.activePageId = entry.pageId;
  browser.syncAndEmitState = async () => browser.snapshot();
  return { browser, page, entry };
}

describe("标签页事件失败的恢复记录", () => {
  test("主文档请求失败保留Chromium错误码与页面URL，不复制原生位置", async () => {
    const { browser, page, entry } = await registeredPage();
    const requestedUrl = "file:///user/opened.html";
    const nativePath = String.raw`\\host\share\profile`;
    const failureText = `net::ERR_ACCESS_DENIED ${nativePath}`;
    browser.manager.schedulePersist = () => undefined;
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      page.emit("requestfailed", {
        ...request(requestedUrl, failureText), isNavigationRequest: () => true, frame: () => page,
      });
      expect(entry.navigationError.requested_url).toBe(requestedUrl);
      expect(entry.navigationError.message).toContain(requestedUrl);
      expect(entry.navigationError.message).toContain("ERR_ACCESS_DENIED");
      expect(entry.navigationError.message).toContain("stage=request_failed");
      expect(entry.navigationError.message).not.toContain(nativePath);
      expect(logged.mock.calls.some(args => args[0].includes(failureText))).toBe(true);
    } finally { logged.mockRestore(); }
  });

  test.each(["dialog", "filechooser", "close"])("%s 只保存安全消息，完整原因留在诊断", async (event) => {
    const { browser, page: target, entry: targetEntry } = await registeredPage();
    const { page: active, entry: activeEntry } = await registeredPage(browser);
    const nativePath = "/private/browser/profile";
    const cause = new Error(`EACCES ${nativePath}`);
    target.title = async () => { throw cause; };
    browser.syncAndEmitState = BrowserSession.prototype.syncAndEmitState;
    let saved;
    const persisted = new Promise(resolve => { saved = resolve; });
    browser.manager.persist = async () => { saved(browser.toRecord()); };
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      if (event === "close") active.emit("close");
      else if (event === "dialog") target.emit("dialog", {
        type: () => "alert", message: () => "等待处理", defaultValue: () => "",
      });
      else target.emit("filechooser", {});
      const record = await persisted;
      expect(record.resource_transition_error).toContain("stage=sync_page");
      expect(record.resource_transition_error).not.toContain(nativePath);
      expect(record.resource_transition_error).not.toContain(".js:");
      expect(logged.mock.calls.some(args => args.at(-1) === cause)).toBe(true);
      expect(logged.mock.calls.some(args => args.at(-1)?.cause === cause)).toBe(true);
      expect(browser.activePageId).toBe(targetEntry.pageId);
      expect(browser.pageEntries.has(activeEntry.pageId)).toBe(event !== "close");
      if (event === "dialog") expect(browser.pendingDialog.message).toBe("等待处理");
      if (event === "filechooser") expect(browser.pendingFileChooser).not.toBeNull();
    } finally { logged.mockRestore(); }
  });
});

function request(url = "https://example.com/api", failureText = "net::ERR_ABORTED") {
  return {
    method: () => "GET",
    url: () => url,
    resourceType: () => "fetch",
    isNavigationRequest: () => false,
    failure: () => ({ errorText: failureText }),
  };
}

function respond(page, originalRequest, status) {
  page.emit("response", { request: () => originalRequest, status: () => status });
}

async function debugSources(resources) {
  const { browser, page, entry } = await registeredPage();
  entry.title = "来源诊断";
  // 执行实际传给页面的函数；隔离上下文不修改测试进程的DOM或performance全局对象。
  page.evaluate = async (callback) => runInNewContext(`(${callback.toString()})()`, {
    Element: class {},
    document: { documentElement: null },
    performance: { getEntriesByType: () => resources },
  });
  return (await browser.debugSnapshot()).sources;
}

describe("浏览器调试快照的来源范围", () => {
  test("按首次有效URL去重，保留类型、原顺序和舍入耗时", async () => {
    const resource = (name, initiatorType, duration = 1.6) => ({ name, initiatorType, duration });
    const sources = await debugSources([
      resource("", "script"),
      resource("https://example.com/a.js", "img"),
      ...Array.from({ length: 250 }, () => resource("https://example.com/a.js", "fetch", 99)),
      resource("https://example.com/b.css?q=1", "", 2.49),
      resource("https://example.com/c.MAP?x", "other", 4.5),
      resource("https://example.com/ignored.js#fragment", "img"),
      resource("https://example.com/pixel.png", "img"),
      resource("https://example.com/pixel.png", "fetch", 3.5),
      ...["script", "stylesheet", "link", "css", "xmlhttprequest"].map(type => resource(`https://example.com/${type}`, type)),
    ]);

    expect(sources).toEqual([
      { url: "https://example.com/a.js", type: "img", duration_ms: 2 },
      { url: "https://example.com/b.css?q=1", type: "resource", duration_ms: 2 },
      { url: "https://example.com/c.MAP?x", type: "other", duration_ms: 5 },
      { url: "https://example.com/pixel.png", type: "fetch", duration_ms: 4 },
      ...["script", "stylesheet", "link", "css", "xmlhttprequest"].map(type => ({ url: `https://example.com/${type}`, type, duration_ms: 2 })),
    ]);
    expect(await debugSources([])).toEqual([]);
  });

  test("达到200个唯一来源后不再读取后续条目", async () => {
    const resources = Array.from({ length: 200 }, (_, index) => ({
      name: `https://example.com/${index}`, initiatorType: "fetch", duration: index,
    }));
    resources.push({ get name() { throw new Error("不应读取输出上限后的来源"); } });

    const sources = await debugSources(resources);

    expect(sources).toHaveLength(200);
    expect(sources.map(source => source.url)).toEqual(resources.slice(0, 200).map(resource => resource.name));
    expect(sources.at(-1)).toEqual({ url: "https://example.com/199", type: "fetch", duration_ms: 199 });
  });
});

async function debugDirectText(childNodes) {
  const { browser, page, entry } = await registeredPage();
  entry.title = "直接文本诊断";
  class Element {
    attributes = [];
    children = [];
    tagName = "DIV";
    constructor() { this.childNodes = childNodes; }
  }
  page.evaluate = async callback => runInNewContext(`(${callback.toString()})()`, {
    Element, Node: { TEXT_NODE: 3 }, document: { documentElement: new Element() },
    performance: { getEntriesByType: () => [] },
  });
  return (await browser.debugSnapshot()).elements.text;
}

describe("浏览器调试快照的直接文本前缀", () => {
  test.each([
    [[], ""],
    [[" ", "\n\t\uFEFF"], ""],
    [[" \t中文\n", "尾部\u00a0文本 "], "中文 尾部 文本"],
    [["a", "", "b"], "a b"],
    [["\uFEFF\u2028\u2029", " 空白\u2003分隔 "], "空白 分隔"],
    [["\u0085", "\0控制字符"], "\u0085 \0控制字符"],
    [["a".repeat(239), " ", "尾部"], "a".repeat(239) + " "],
    [["a".repeat(240), "尾部"], "a".repeat(240)],
    [["a".repeat(239), "😀结束"], "a".repeat(239) + " "],
    [["a".repeat(239) + "😀结束"], "a".repeat(239) + "\uD83D"],
    [["a".repeat(239), " ".repeat(10_000)], "a".repeat(239)],
    [["汉".repeat(100_000)], "汉".repeat(240)],
  ])("保持空白归一、节点分隔及240字符截断: %#", async (parts, expected) => {
    const childNodes = parts.flatMap(textContent => [
      { nodeType: 8, textContent: "注释不能进入正文" },
      { nodeType: 3, textContent },
      { nodeType: 1, textContent: "后代元素不能进入直接文本" },
    ]);
    expect(await debugDirectText(childNodes)).toBe(expected);
  });

  test("取得前缀后不读取后续文本节点", async () => {
    const childNodes = [
      { nodeType: 3, textContent: "x".repeat(240) },
      { nodeType: 3, get textContent() { throw new Error("不应读取已截断的正文后缀"); } },
    ];

    expect(await debugDirectText(childNodes)).toBe("x".repeat(240));
  });
});

describe("浏览器网络诊断记录的请求归属", () => {
  test("同 URL 并发请求按实际身份接收响应，不按最新未响应行匹配", async () => {
    const { page, entry } = await registeredPage();
    const first = request();
    const second = request();
    page.emit("request", first);
    page.emit("request", second);

    respond(page, first, 201);
    respond(page, second, 503);

    expect(entry.diagnostics.snapshot().network.map((item) => item.status)).toEqual([201, 503]);
    expect(entry.diagnostics.snapshot().network.map((item) => item.failed)).toEqual([false, false]);
  });

  test("同 URL 并发请求的传输错误分别保留，不覆盖另一请求", async () => {
    const { page, entry } = await registeredPage();
    const first = request(undefined, "net::ERR_TIMED_OUT");
    const second = request(undefined, "net::ERR_ABORTED");
    page.emit("request", first);
    page.emit("request", second);

    page.emit("requestfailed", first);
    page.emit("requestfailed", second);

    expect(entry.diagnostics.snapshot().network.map((item) => item.failed)).toEqual([true, true]);
    expect(entry.diagnostics.snapshot().network.map((item) => item.failure_text)).toEqual([
      "net::ERR_TIMED_OUT", "net::ERR_ABORTED",
    ]);
  });

  test("收到响应头后正文传输失败，保留状态码并标记实际失败请求", async () => {
    const { page, entry } = await registeredPage();
    const first = request();
    const second = request();
    page.emit("request", first);
    page.emit("request", second);
    respond(page, first, 200);

    page.emit("requestfailed", first);

    expect(entry.diagnostics.snapshot().network[0]).toMatchObject({
      status: 200, failed: true, failure_text: "net::ERR_ABORTED",
    });
    expect(entry.diagnostics.snapshot().network[1]).toMatchObject({ status: null, failed: false });
  });

  test("有界缓冲区淘汰后同一毫秒的新记录仍有唯一 ID", async () => {
    const { page, entry } = await registeredPage();
    const clock = spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
    const ids = [];
    try {
      for (let index = 0; index < 205; index += 1) {
        const originalRequest = request();
        page.emit("request", originalRequest);
        ids.push(entry.diagnostics.snapshot().network.at(-1).id);
        respond(page, originalRequest, 200);
        page.emit("requestfinished", originalRequest);
      }
    } finally {
      clock.mockRestore();
    }

    expect(entry.diagnostics.snapshot().network).toHaveLength(200);
    expect(new Set(ids).size).toBe(205);
    expect(entry.diagnostics.snapshot().network.map((item) => item.id)).toEqual(ids.slice(-200));
  });

  test("已淘汰请求的晚响应不会认领缓冲区内的同 URL 请求", async () => {
    const { page, entry } = await registeredPage();
    const evicted = request();
    page.emit("request", evicted);
    for (let index = 0; index < 199; index += 1) {
      page.emit("request", request(`https://example.com/${index}`));
    }
    const latest = request();
    page.emit("request", latest);

    respond(page, evicted, 418);

    expect(entry.diagnostics.snapshot().network).toHaveLength(200);
    expect(entry.diagnostics.snapshot().network.at(-1).status).toBeNull();
    respond(page, latest, 202);
    expect(entry.diagnostics.snapshot().network.at(-1).status).toBe(202);
  });

  test("清空后旧请求的晚响应与失败不会污染新记录，也不复用 ID", async () => {
    const { browser, page, entry } = await registeredPage();
    const clock = spyOn(Date, "now").mockReturnValue(1_800_000_000_000);
    try {
      const oldResponse = request();
      const oldFailure = request();
      page.emit("request", oldResponse);
      page.emit("request", oldFailure);
      const previousIds = entry.diagnostics.snapshot().network.map((item) => item.id);
      await browser.clearNetworkRequests();
      expect(entry.diagnostics.snapshot().network).toEqual([]);
      const latest = request();
      page.emit("request", latest);

      respond(page, oldResponse, 418);
      page.emit("requestfailed", oldFailure);

      expect(entry.diagnostics.snapshot().network[0]).toMatchObject({ status: null, failed: false });
      expect(previousIds).not.toContain(entry.diagnostics.snapshot().network[0].id);
      respond(page, latest, 204);
      expect(entry.diagnostics.snapshot().network[0]).toMatchObject({ status: 204, failed: false });
    } finally {
      clock.mockRestore();
    }
  });

  test("完成事件不改写响应头观察时间，错误详情仍有长度上限", async () => {
    const { page, entry } = await registeredPage();
    const complete = request();
    page.emit("request", complete);
    respond(page, complete, 200);
    const headersObservedAt = entry.diagnostics.snapshot().network[0].finished_at;
    page.emit("requestfinished", complete);
    expect(typeof headersObservedAt).toBe("string");
    expect(entry.diagnostics.snapshot().network[0].finished_at).toBe(headersObservedAt);

    const failure = request(undefined, "x".repeat(1_000));
    page.emit("request", failure);
    page.emit("requestfailed", failure);
    expect(entry.diagnostics.snapshot().network[1].failure_text).toBe("x".repeat(512));
  });
});


describe("浏览器页面诊断的职责边界", () => {
  test("控制台消息和页面异常保留各自字段与观察时间", async () => {
    const { page, entry } = await registeredPage();
    const location = { url: "https://example.com/app.js", lineNumber: 7, columnNumber: 3 };
    page.emit("console", {
      type: () => "warning", text: () => "控制台详情", location: () => location,
    });
    page.emit("pageerror", new Error("脚本执行失败"));
    page.emit("pageerror", "非 Error 异常");

    const messages = entry.diagnostics.snapshot().console;
    expect(messages).toHaveLength(3);
    expect(messages[0]).toMatchObject({ level: "warning", text: "控制台详情", location });
    expect(messages[1]).toMatchObject({ level: "error", text: "脚本执行失败", location: null });
    expect(messages[2]).toMatchObject({ level: "error", text: "非 Error 异常", location: null });
    expect(messages.every((message) => Number.isFinite(Date.parse(message.occurred_at)))).toBe(true);
  });

  test("控制台与异常共用200行缓冲，清空网络保留控制台", async () => {
    const { browser, page, entry } = await registeredPage();
    page.emit("request", request());
    for (let index = 0; index < 201; index += 1) {
      page.emit("console", {
        type: () => "log", text: () => `行${index}`, location: () => null,
      });
    }
    page.emit("pageerror", new Error("最后一行"));
    const messages = entry.diagnostics.snapshot().console;
    expect(messages).toHaveLength(200);
    expect(messages[0].text).toBe("行2");
    expect(messages.at(-1).text).toBe("最后一行");
    expect(entry.diagnostics.snapshot().network).toHaveLength(1);

    await browser.clearNetworkRequests();

    expect(entry.diagnostics.snapshot().network).toEqual([]);
    expect(entry.diagnostics.snapshot().console).toEqual(messages);
    messages.length = 0;
    expect(entry.diagnostics.snapshot().console).toHaveLength(200);
  });

  test("标签页独享诊断记录，清空只作用于当前页面", async () => {
    const first = await registeredPage();
    first.page.emit("request", request());
    const second = await registeredPage(first.browser);
    second.page.emit("request", request());

    await second.browser.clearNetworkRequests();

    expect(first.entry.diagnostics.snapshot().network).toHaveLength(1);
    expect(second.entry.diagnostics.snapshot().network).toEqual([]);
    expect(first.browser.pageEntries.size).toBe(2);
  });

  test("重复登记同一页面不会再注册诊断监听或重置记录", async () => {
    const { browser, page, entry } = await registeredPage();
    page.emit("request", request());
    const listenerCounts = Object.fromEntries(page.eventNames().map((name) => [name, page.listenerCount(name)]));

    const repeated = await browser.registerPage(page, { activate: false });

    expect(repeated).toBe(entry);
    expect(entry.diagnostics.snapshot().network).toHaveLength(1);
    expect(Object.fromEntries(page.eventNames().map((name) => [name, page.listenerCount(name)]))).toEqual(listenerCounts);
  });
});
