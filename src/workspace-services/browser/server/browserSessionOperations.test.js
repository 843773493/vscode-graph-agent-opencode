import { EventEmitter } from "node:events";
import { describe, expect, spyOn, test } from "bun:test";
import { BrowserSession } from "./browserSession.js";
import { BrowserPageDiagnostics } from "./diagnostics/pageDiagnostics.js";
import { BrowserRuntimePool } from "./resources/browserRuntimePool.js";

function session() {
  return new BrowserSession({
    manager: {
      persist: async () => undefined,
      stateStore: { deleteCheckpoint: async () => undefined },
    },
    record: {
      browser_id: "browser_operation_test",
      workspace_id: "00000000-0000-4000-8000-000000000001",
      transport_owner_revision: 1,
      reconciliation_status: "verified",
      status: "running",
    },
  });
}

function liveSession() {
  const browser = session();
  const cdpCalls = [];
  let navigationCount = 0;
  const page = Object.assign(new EventEmitter(), {
    setViewportSize: async () => undefined,
    goto: async () => {
      navigationCount += 1;
    },
    evaluate: async () => undefined,
  });
  const cdpSession = {
    send: async (method, params) => {
      cdpCalls.push({ method, params });
    },
  };
  browser.browser = {};
  browser.context = {};
  browser.page = page;
  browser.cdpSession = cdpSession;
  browser.activePageId = "page_device";
  browser.pageEntries.set("page_device", {
    pageId: "page_device",
    page,
    cdpSession,
    webSockets: new Set(),
    diagnostics: new BrowserPageDiagnostics(page),
  });
  browser.syncAndEmitState = async () => browser.snapshot();
  return { browser, cdpCalls, getNavigationCount: () => navigationCount };
}

function transitionSession(onCommand = () => undefined) {
  const browser = session();
  const commands = [];
  const persisted = [];
  const states = new Map();
  browser.record.resource_state = "background";
  browser.browser = {};
  browser.context = { storageState: async () => ({ cookies: [], origins: [] }) };
  browser.manager.persist = async () => { persisted.push(browser.snapshot()); };
  browser.manager.runCheckpointCapture = async (callback) => await callback();
  browser.manager.stateStore.writeCheckpoint = async () => ({ size_bytes: 1 });
  browser.manager.runtimePool = { releaseContext: async () => undefined };
  browser.inspectResourceSnapshot = async () => ({ resource_hard_protection_reasons: [], resource_soft_protection_reasons: [] });
  browser.inspectPageEntryProtectionReasons = async () => [];
  for (const pageId of ["page_first", "page_second"]) {
    const state = { lifecycle: "active", disabled: false };
    states.set(pageId, state);
    const page = {
      isClosed: () => false, title: async () => pageId, url: () => "about:blank",
      evaluate: async () => ({ scroll_x: 0, scroll_y: 0, controls: [], session_storage: { available: false } }),
    };
    const cdpSession = { send: async (method, params) => {
      commands.push([pageId, method, params]);
      onCommand(pageId, method, params);
      if (method === "Page.setWebLifecycleState") state.lifecycle = params.state;
      if (method === "Emulation.setScriptExecutionDisabled") state.disabled = params.value;
      if (method === "Page.getFrameTree") return { frameTree: { frame: { id: pageId } } };
      if (method === "CSS.createStyleSheet") return { styleSheetId: `style_${pageId}` };
    } };
    browser.pageEntries.set(pageId, { pageId, page, cdpSession, webSockets: new Set() });
  }
  const active = browser.pageEntries.get("page_first");
  browser.activePageId = active.pageId;
  browser.page = active.page;
  browser.cdpSession = active.cdpSession;
  return { browser, commands, persisted, states };
}

describe("浏览器帧回调与关闭的生命周期", () => {
  function pendingFrame() {
    const { browser } = liveSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    let closed = false;
    let finishAck;
    let failAck;
    let ackCount = 0;
    const ack = new Promise((resolve, reject) => { finishAck = resolve; failAck = reject; });
    entry.page.isClosed = () => closed;
    entry.page.close = async () => { closed = true; };
    browser.manager.runtimePool = { releaseContext: async () => { closed = true; } };
    entry.cdpSession.send = async (method) => {
      if (method === "Page.screencastFrameAck") { ackCount += 1; await ack; }
    };
    const event = { data: Buffer.from("jpeg").toString("base64"), metadata: {}, sessionId: 1 };
    browser.receiveScreencastFrame(event, entry);
    return { browser, entry, event, finishAck, failAck,
      isClosed: () => closed, ackCount: () => ackCount };
  }

  test.each(["runtime", "page"])("%s关闭先排空已接纳帧，拒绝退役页面新帧", async (scope) => {
    const { browser, entry, event, finishAck, isClosed, ackCount } = pendingFrame();
    const closing = scope === "runtime" ? browser.releaseRuntime() : browser.closePage(entry.pageId);
    expect(entry.retiring).toBe(true);
    expect(browser.screencastFrames.size).toBe(1);
    browser.receiveScreencastFrame(event, entry);
    expect(ackCount()).toBe(1);
    expect(isClosed()).toBe(false);
    finishAck();
    await closing;
    expect(isClosed()).toBe(true);
    expect(browser.screencastFrames.size).toBe(0);
  });

  test("意外帧ACK错误明确发布；关闭排空失败保留context并报告未知", async () => {
    const { browser, entry, failAck, isClosed } = pendingFrame();
    const context = browser.context;
    const states = [];
    browser.on("state", (state) => states.push(state));
    const cause = Object.assign(new Error("Target closed /private/chromium"), { code: "EIO" });
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const closing = browser.releaseRuntime().catch((error) => error);
      failAck(cause);
      const failure = await closing;
      expect(failure.message).toContain("stage=drain_screencast_frames");
      expect(failure.cause.errors[0].cause).toBe(cause);
      expect(failure.message).not.toContain("/private/chromium");
      expect(browser.context).toBe(context);
      expect(isClosed()).toBe(false);
      expect(browser.record.reconciliation_status).toBe("unknown");
      expect(states.some((state) => state.stream_error?.includes("stage=screencast_frame"))).toBe(true);
      expect(browser.record.stream_error).not.toContain("/private/chromium");
      expect(diagnostics.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      expect(entry.retiring).toBe(true);
    } finally { diagnostics.mockRestore(); }
  });

  test("帧排空有界超时不关闭context，迟到旧页面错误不污染新页面", async () => {
    const { browser, entry, finishAck, isClosed } = pendingFrame();
    await expect(browser.drainScreencastFrames([entry], 1))
      .rejects.toMatchObject({ code: "browser_frame_drain_timeout" });
    expect(isClosed()).toBe(false);
    expect(browser.screencastFrames.size).toBe(1);
    finishAck();
    await Promise.allSettled([...browser.screencastFrames.keys()]);
    const cause = new Error("意外断开 /private/chromium");
    entry.retiring = false;
    entry.cdpSession.send = async () => { throw cause; };
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      browser.receiveScreencastFrame({ data: "", metadata: {}, sessionId: 2 }, entry);
      const executions = [...browser.screencastFrames.keys()];
      browser.pageEntries.set(entry.pageId, { ...entry });
      browser.record.stream_error = "新页面的状态";
      await Promise.allSettled(executions);
      expect(browser.record.stream_error).toBe("新页面的状态");
      expect(diagnostics.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
    } finally { diagnostics.mockRestore(); }
  });

  test("关闭单页的排空超时保留页面并报告未知", async () => {
    const { browser, entry, finishAck, isClosed } = pendingFrame();
    const drain = browser.drainScreencastFrames.bind(browser);
    browser.drainScreencastFrames = (entries) => drain(entries, 1);
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await expect(browser.closePage(entry.pageId)).rejects.toMatchObject({ code: "browser_frame_drain_timeout" });
      expect(browser.record.reconciliation_status).toBe("unknown");
      expect(isClosed()).toBe(false);
      expect(entry.retiring).toBe(true);
      finishAck();
      await Promise.allSettled([...browser.screencastFrames.keys()]);
    } finally { diagnostics.mockRestore(); }
  });
});

describe("浏览器启动的运行时错误归属", () => {
  function startingSession(stage, cause, cleanupCause = null) {
    const browser = session();
    const persisted = [];
    const calls = [];
    const at = async (operation, value) => {
      calls.push(operation);
      if (stage === operation) throw cause;
      return value;
    };
    const page = Object.assign(new EventEmitter(), {
      url: () => "about:blank", title: async () => "初始页面", isClosed: () => false,
      goto: async () => undefined,
      evaluate: () => at("read_browser_defaults", { userAgent: "desktop-agent", platform: "Linux" }),
      setViewportSize: () => at("set_viewport"),
    });
    const cdp = Object.assign(new EventEmitter(), { send: (method) => at(method) });
    const context = Object.assign(new EventEmitter(), {
      newPage: () => at("create_page", page), newCDPSession: async () => cdp,
    });
    browser.record.status = "created";
    browser.manager.persist = async () => { persisted.push(browser.toRecord()); };
    browser.manager.runtimePool = {
      acquireContext: () => at("acquire_context", { browser: {}, context, chromiumInstanceGeneration: 7 }),
      releaseContext: async () => { calls.push("release_context"); if (cleanupCause) throw cleanupCause; },
    };
    return { browser, persisted, calls, context };
  }

  test.each([
    "acquire_context", "create_page", "read_browser_defaults", "set_viewport",
    "Emulation.setDeviceMetricsOverride", "Emulation.setTouchEmulationEnabled",
    "Emulation.setEmitTouchEventsForMouse", "Emulation.setUserAgentOverride", "Network.emulateNetworkConditions",
  ])("启动失败由实际阶段产生错误并释放已取得的context: %s", async (stage) => {
    const cause = Object.assign(new Error("EACCES /private/chromium/profile"), { code: "EACCES", statusCode: 503 });
    const { browser, persisted, calls } = startingSession(stage, cause);
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.start().catch((error) => error);
      expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
      expect(failure.message).toContain(`stage=${stage}`);
      expect(failure.message).toContain(browser.id);
      expect(failure.message).not.toContain("/private/chromium/profile");
      expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      expect(persisted.at(-1)).toMatchObject({ status: "failed", error_message: failure.message, reconciliation_status: "verified" });
      expect(JSON.stringify(persisted)).not.toContain("/private/chromium/profile");
      expect(calls.filter((call) => call === "release_context")).toHaveLength(stage === "acquire_context" ? 0 : 1);
      expect(browser.context).toBeNull();
      expect(browser.pageEntries.size).toBe(0);
    } finally { logged.mockRestore(); }
  });

  test.each(["/private/chromium/profile", String.raw`C:\private\chromium\profile`, String.raw`\\host\share\profile`])(
    "启动和清理同时失败保留原错分类、两个原因及未知句柄: %s", async (nativePath) => {
      const primary = Object.assign(new Error(`启动 ${nativePath}`), { code: "EACCES", statusCode: 503 });
      const cleanup = Object.assign(new Error(`关闭 ${nativePath}`), { code: "EIO", statusCode: 500 });
      const { browser, context, persisted } = startingSession("create_page", primary, cleanup);
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await browser.start().catch((error) => error);
        expect(failure).toBeInstanceOf(AggregateError);
        expect(failure.errors.map((error) => error.cause)).toEqual([primary, cleanup]);
        expect(failure).toMatchObject({ cause: failure.errors[0], code: "EACCES", statusCode: 503 });
        expect(failure.message).not.toContain(nativePath);
        expect(persisted.at(-1)).toMatchObject({ status: "failed", reconciliation_status: "unknown" });
        expect(JSON.stringify(persisted)).not.toContain(nativePath);
        expect(browser.context).toBe(context);
      } finally { logged.mockRestore(); }
    },
  );

  test("启动成功保留原生默认UA及运行时代际，再次start复用同一context", async () => {
    const { browser, calls } = startingSession(null);
    expect(await browser.start()).toMatchObject({ status: "running", runtime_generation: 7 });
    expect(browser.baseUserAgent).toBe("desktop-agent");
    expect(browser.basePlatform).toBe("Linux");
    await browser.start();
    expect(calls.filter((call) => call === "acquire_context")).toHaveLength(1);
    expect(calls).not.toContain("release_context");
  });
});

describe("浏览器资源生命周期错误归属", () => {
  test.each([
    "/private/browser/script-output", String.raw`C:\private\browser\script-output`,
    String.raw`\\host\share\script-output`, String.raw`\\?\UNC\host\share\script-output`,
  ])("脚本失败在写操作回执前发布执行阶段，原异常仅留诊断: %s", async (nativePath) => {
    const { browser } = liveSession();
    const cause = Object.assign(new Error(`EACCES ${nativePath}`), { code: "EACCES", statusCode: 503, errno: -13, syscall: "open" });
    browser.page.evaluate = async () => { throw cause; };
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.runInteractiveOperation({ actor: "user:test", action: "run" },
        () => browser.runPlaywrightCode({ code: "return await page.evaluate(() => 1);" }))
        .catch((error) => error);
      expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
      expect(failure.message).toBe(`浏览器运行时操作失败: browser_id=${browser.id}, page_id=page_device, stage=run_code, code=EACCES`);
      expect(browser.record.last_operation).toMatchObject({ action: "run", status: "failed", error: failure.message });
      expect(JSON.stringify(browser.toRecord())).not.toContain(nativePath);
      expect(browser.record.status).toBe("running");
      expect(browser.pageEntries.size).toBe(1);
      expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      const retried = await browser.runInteractiveOperation({ actor: "user:test", action: "run" },
        () => browser.runPlaywrightCode({ code: "return 'script-retry-ok';" }));
      expect(retried.result).toBe("script-retry-ok");
      expect(browser.record.last_operation).toMatchObject({ action: "run", status: "completed" });
    } finally { logged.mockRestore(); }
  });

  test.each([
    ["throw new Error('用户脚本失败 /api/example');", "用户脚本失败 /api/example", "Error", undefined],
    ["throw Object.assign(new Error('业务校验失败'), {code: 'business_rule'});", "业务校验失败", "Error", "business_rule"],
    ["const = ;", null, "SyntaxError", undefined],
    ["throw '用户脚本拒绝';", "用户脚本拒绝", undefined, undefined],
  ])("普通脚本与语法错误保持既有正文和分类: %s", async (code, message, name, errorCode) => {
    const { browser } = liveSession();
    const failure = await browser.runInteractiveOperation({ actor: "user:test", action: "run" },
      () => browser.runPlaywrightCode({ code })).catch((error) => error);
    const observedMessage = typeof failure === "string" ? failure : failure.message;
    if (message === null) {
      // JS 引擎的语法文案不同；保留原生分类与正文，不改写为运行时阶段错误。
      expect(typeof observedMessage).toBe("string");
      expect(observedMessage.length).toBeGreaterThan(0);
      expect(failure.cause).toBeUndefined();
    } else {
      expect(observedMessage).toBe(message);
    }
    expect(failure.name).toBe(name);
    expect(failure.code).toBe(errorCode);
    expect(browser.record.last_operation).toMatchObject({ action: "run", status: "failed", error: observedMessage });
  });

  test.each(["read_clipboard", "find_text"])(
    "文本查询原生失败保留页面身份和原因，不复制到操作记录: %s", async (stage) => {
      const { browser } = liveSession();
      const cause = Object.assign(new Error("EACCES /private/browser/text-query"), { code: "EACCES", statusCode: 503 });
      browser.page.evaluate = async () => { throw cause; };
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await browser.runInteractiveOperation({ actor: "user:test", action: stage },
          () => stage === "read_clipboard" ? browser.readClipboardText() : browser.findText("marker"))
          .catch((error) => error);
        expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
        expect(failure.message).toContain(`stage=${stage}`);
        expect(failure.message).toContain("page_id=page_device");
        expect(browser.record.last_operation).toMatchObject({ action: stage, status: "failed", error: failure.message });
        expect(JSON.stringify(browser.toRecord())).not.toContain("/private/browser/text-query");
        expect(browser.record.status).toBe("running");
        expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      } finally { logged.mockRestore(); }
    },
  );

  test.each(["debug_dom", "debug_sources", "debug_title"])(
    "调试快照原生失败保留采集阶段和原因，不复制到操作记录: %s", async (stage) => {
      const { browser } = liveSession();
      const entry = browser.pageEntries.get(browser.activePageId);
      const cause = Object.assign(new Error("EACCES /private/browser/debug-profile"), { code: "EACCES", statusCode: 503 });
      let evaluations = 0;
      entry.page.evaluate = async () => {
        evaluations += 1;
        if (stage === (evaluations === 1 ? "debug_dom" : "debug_sources")) throw cause;
        return evaluations === 1 ? { node_id: "0" } : [];
      };
      entry.page.title = async () => { if (stage === "debug_title") throw cause; return "调试页面"; };
      entry.page.url = () => "about:blank";
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await browser.runInteractiveOperation({ actor: "user:test", action: "debug-snapshot" },
          () => browser.debugSnapshot()).catch((error) => error);
        expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
        expect(failure.message).toContain(`stage=${stage}`);
        expect(failure.message).toContain("page_id=page_device");
        expect(JSON.stringify(browser.toRecord())).not.toContain("/private/browser/debug-profile");
        expect(browser.record.last_operation).toMatchObject({ action: "debug-snapshot", status: "failed", error: failure.message });
        expect(browser.record.status).toBe("running");
        expect(browser.pageEntries.size).toBe(1);
        expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      } finally { logged.mockRestore(); }
    },
  );

  test.each(["create_page", "close_page", "Page.stopLoading", "Page.captureScreenshot"])(
    "页面控制的原生错误保留身份、分类和原因: %s", async (stage) => {
      const cause = Object.assign(new Error(String.raw`EACCES C:\private\chromium\profile`), { code: "EACCES", statusCode: 503 });
      const { browser } = liveSession();
      const fail = async () => { throw cause; };
      browser.context.newPage = fail;
      browser.page.close = fail;
      browser.cdpSession.send = fail;
      const operations = {
        create_page: () => browser.createPage(), close_page: () => browser.closePage(browser.activePageId),
        "Page.stopLoading": () => browser.stopLoading(), "Page.captureScreenshot": () => browser.captureClientScreenshot(),
      };
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await operations[stage]().catch((error) => error);
        expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
        expect(failure.message).toContain(`browser_id=${browser.id}`);
        expect(failure.message).toContain(`stage=${stage}`);
        expect(failure.message).toContain(`page_id=${stage === "create_page" ? null : "page_device"}`);
        expect(failure.message).not.toContain(String.raw`C:\private\chromium\profile`);
        expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
        expect(browser.record.status).toBe("running");
        expect(browser.activePageId).toBe("page_device");
        expect(browser.pageEntries.size).toBe(1);
      } finally { logged.mockRestore(); }
    },
  );

  test("客户端截图仍返回原始PNG字节，停止加载不改变活动标签页", async () => {
    const { browser, cdpCalls } = liveSession();
    const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
    browser.cdpSession.send = async (method, params) => {
      cdpCalls.push({ method, params });
      return { data: png.toString("base64") };
    };
    expect(await browser.captureClientScreenshot()).toEqual(png);
    expect(await browser.stopLoading()).toMatchObject({ status: "running", active_page_id: "page_device" });
    expect(cdpCalls.map((call) => call.method)).toEqual(["Page.captureScreenshot", "Page.stopLoading"]);
  });

  test.each(["Page.stopScreencast", "reset_pointer", "Page.startScreencast", "sync_page", "Page.captureScreenshot"])(
    "标签页切换在实际失败阶段保留原因与已执行事实: %s", async (stage) => {
      const cause = Object.assign(new Error("EACCES /private/browser/profile"), { code: "EACCES", statusCode: 503 });
      const { browser } = transitionSession((_pageId, method) => { if (method === stage) throw cause; });
      browser.clients.add({});
      browser.streaming = true;
      if (stage === "reset_pointer") browser.pointerController.reset = async () => { throw cause; };
      if (stage === "sync_page") browser.pageEntries.get("page_second").page.title = async () => { throw cause; };
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await browser.activatePage("page_second").catch((error) => error);
        expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
        expect(failure.message).toContain(`stage=${stage}`);
        expect(failure.message).not.toContain("/private/browser/profile");
        expect(logged.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
        expect(browser.activePageId).toBe(["Page.stopScreencast", "reset_pointer"].includes(stage) ? "page_first" : "page_second");
        expect(browser.streaming).toBe(!["reset_pointer", "Page.startScreencast"].includes(stage));
      } finally { logged.mockRestore(); }
    },
  );

  test("标签页状态同步不改写持久化owner的错误归属", async () => {
    const { browser } = transitionSession();
    const cause = Object.assign(new Error("保存失败: stage=publish_state"), { code: "EACCES" });
    browser.manager.persist = async () => { throw cause; };
    await expect(browser.activatePage("page_second")).rejects.toBe(cause);
  });

  test.each(["/private/chromium/profile", String.raw`C:\private\chromium\profile`, String.raw`\\host\share\profile`])(
    "冻结与回滚失败只向诊断交付原生位置: %s", async (nativePath) => {
      const primary = Object.assign(new Error(`冻结失败 ${nativePath}`), { code: "EACCES", statusCode: 500 });
      const rollback = new Error(`回滚失败 ${nativePath}`);
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const { browser, persisted } = transitionSession((pageId, method, params) => {
          if (pageId === "page_second" && method === "Emulation.setScriptExecutionDisabled" && params.value) throw primary;
          if (pageId === "page_first" && method === "Page.setWebLifecycleState" && params.state === "active") throw rollback;
        });
        const failure = await browser.freeze().catch((error) => error);
        expect(failure).toBeInstanceOf(AggregateError);
        expect(failure.errors.map((error) => error.cause)).toEqual([primary, rollback]);
        expect(failure.errors[0]).toMatchObject({ code: "EACCES", statusCode: 500 });
        expect(logged.mock.calls.some((args) => args.at(-1) === primary)).toBe(true);
        expect(logged.mock.calls.some((args) => args.at(-1) === rollback)).toBe(true);
        expect(failure.message).not.toContain(nativePath);
        expect(JSON.stringify(browser.toRecord())).not.toContain(nativePath);
        expect(persisted.at(-1)).toMatchObject({ reconciliation_status: "unknown", resource_state: "freezing" });
        expect(persisted.at(-1).resource_transition_error).toBe(failure.message);
      } finally { logged.mockRestore(); }
    },
  );

  test("保护检查失败仍阻止冻结，保护码与恢复记录不复制原异常", async () => {
    const { browser } = transitionSession();
    const cause = new Error("检查失败 /private/chromium/profile");
    browser.inspectResourceSnapshot = BrowserSession.prototype.inspectResourceSnapshot;
    browser.inspectPageEntryProtectionReasons = BrowserSession.prototype.inspectPageEntryProtectionReasons;
    for (const entry of browser.pageEntries.values()) entry.page.evaluate = async () => { throw cause; };
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await expect(browser.freeze()).rejects.toMatchObject({ code: "browser_resource_protected" });
      expect(browser.snapshot().resource_hard_protection_reasons).toEqual(["protection_inspection_failed:stage=inspect_protection"]);
      expect(JSON.stringify(browser.toRecord())).not.toContain(cause.message);
      expect(logged.mock.calls.some((args) => args.at(-1).cause === cause)).toBe(true);
      expect(browser.record.reconciliation_status).toBe("verified");
    } finally { logged.mockRestore(); }
  });
});

describe("浏览器统一操作队列", () => {
  test.each([false, true])("冻结中途失败只有已核实恢复才能保留 verified: rollback_failed=%s", async (rollbackFailed) => {
    const { browser, persisted, states } = transitionSession((pageId, method, params) => {
      if (pageId === "page_second" && method === "Emulation.setScriptExecutionDisabled" && params.value) {
        throw new Error("第二页冻结失败");
      }
      if (rollbackFailed && pageId === "page_first" && method === "Page.setWebLifecycleState" && params.state === "active") {
        throw new Error("第一页恢复失败");
      }
    });
    await expect(browser.freeze()).rejects.toThrow("Emulation.setScriptExecutionDisabled");
    expect(states.get("page_second")).toEqual({ lifecycle: "active", disabled: false });
    expect(persisted.at(-1).reconciliation_status).toBe(rollbackFailed ? "unknown" : "verified");
    expect(persisted.at(-1).resource_state).toBe(rollbackFailed ? "freezing" : "background");
    if (!rollbackFailed) {
      expect(states.get("page_first")).toEqual({ lifecycle: "active", disabled: false });
      return;
    }
    expect(persisted.at(-1).resource_transition_error).toContain("Page.setWebLifecycleState");
    let calls = 0;
    await expect(browser.enqueueOperation({ actor: "agent", action: "read" }, () => { calls += 1; }))
      .rejects.toMatchObject({ code: "browser_resource_unknown" });
    const client = {};
    browser.transports.set(client, { transportOwnerRevision: 1 });
    expect(() => browser.assertTransport(client)).toThrow("尚未核实");
    expect(calls).toBe(0);
    expect(browser.pendingOperations).toBe(0);
    expect(browser.inFlightOperations).toBe(0);
    browser.transports.clear();
    await expect(browser.close()).resolves.toMatchObject({ status: "closed", reconciliation_status: "verified" });
  });

  test("多页 wake 中途失败不会把已恢复页伪装为 frozen，未知事实阻止后续 callback", async () => {
    let waking = false;
    const { browser, persisted, states } = transitionSession((pageId, method, params) => {
      if (waking && pageId === "page_second" && method === "Page.setWebLifecycleState" && params.state === "active") {
        throw new Error("第二页唤醒失败");
      }
    });
    await browser.freeze();
    waking = true;
    await expect(browser.wake()).rejects.toThrow("Page.setWebLifecycleState");
    expect(states.get("page_first")).toEqual({ lifecycle: "active", disabled: false });
    expect(states.get("page_second").lifecycle).toBe("frozen");
    expect(persisted.at(-1)).toMatchObject({ resource_state: "restoring", reconciliation_status: "unknown" });
    let calls = 0;
    await expect(browser.runInteractiveOperation({ actor: "user", action: "click" }, () => { calls += 1; }))
      .rejects.toMatchObject({ code: "browser_resource_unknown" });
    await expect(browser.wake()).rejects.toMatchObject({ code: "browser_resource_unknown" });
    expect(calls).toBe(0);
    expect(browser.pendingOperations).toBe(0);
  });

  test("冻结前的明确保护拒绝保持 verified 且没有页面副作用", async () => {
    const { browser, commands, persisted } = transitionSession();
    browser.inspectResourceSnapshot = async () => ({ resource_hard_protection_reasons: ["pending_dialog"], resource_soft_protection_reasons: [] });
    await expect(browser.freeze()).rejects.toMatchObject({ code: "browser_resource_protected" });
    expect(commands).toEqual([]);
    expect(persisted.at(-1)).toMatchObject({ resource_state: "background", reconciliation_status: "verified" });
  });

  test.each([false, true])("冷回收恢复失败只有运行时释放已核实才能保留 discarded: release_failed=%s", async (releaseFailed) => {
    const browser = session();
    const persisted = [];
    let releases = 0;
    browser.record.resource_state = "discarded";
    browser.manager.persist = async () => { persisted.push(browser.snapshot()); };
    browser.manager.stateStore.readCheckpoint = async () => ({
      version: 1, browser_id: browser.id, pages: [{ page_id: "page_restore" }], storage_state: {},
    });
    browser.manager.runtimePool = {
      acquireContext: async () => ({ browser: {}, context: { newPage: async () => { throw new Error("新页恢复失败"); } } }),
      releaseContext: async () => {
        releases += 1;
        if (releaseFailed) throw new Error("运行时释放失败");
      },
    };
    await expect(browser.wake()).rejects.toThrow("stage=create_page");
    expect(releases).toBe(1);
    expect(persisted.at(-1)).toMatchObject({
      resource_state: releaseFailed ? "restoring" : "discarded",
      reconciliation_status: releaseFailed ? "unknown" : "verified",
    });
    if (releaseFailed) expect(persisted.at(-1).resource_transition_error).toContain("stage=release_context");
  });

  test.each(["close", "delete", "discard"])("%s 的 context 释放失败保留同一真实句柄和 unknown，显式关闭重试才核终态", async (action) => {
    const { browser, persisted } = transitionSession();
    const context = browser.context;
    let failed = true;
    let closes = 0;
    context.close = async () => {
      closes += 1;
      if (failed) throw new Error("context 关闭失败");
    };
    const pool = new BrowserRuntimePool();
    pool.contexts.add(context);
    browser.manager.runtimePool = pool;
    if (action === "discard") {
      await browser.freeze();
      browser.manager.stateStore.readCheckpoint = async () => ({ pages: [
        { page_id: "page_first", title: "First", url: "about:blank" },
      ] });
    }
    const release = action === "discard" ? browser.discard() : browser.close({ status: action === "delete" ? "deleted" : "closed" });
    await expect(release).rejects.toThrow("stage=release_context");
    expect(browser.context).toBe(context);
    expect(pool.contexts.has(context)).toBe(true);
    expect(persisted.at(-1)).toMatchObject({ status: "running", reconciliation_status: "unknown" });
    if (action === "discard") expect(persisted.at(-1).resource_state).toBe("discarding");
    let calls = 0;
    await expect(browser.enqueueOperation({ actor: "agent", action: "run" }, () => { calls += 1; }))
      .rejects.toThrow();
    expect(calls).toBe(0);
    failed = false;
    await expect(browser.close({ status: "deleted" }))
      .resolves.toMatchObject({ status: "deleted", reconciliation_status: "verified" });
    expect(closes).toBe(2);
    expect(browser.context).toBeNull();
    expect(pool.contexts.size).toBe(0);
    pool.cancelIdleShutdown();
  });

  test.each(["queued", "interactive"])("prepare await 期间撤销 %s 操作不泄漏计数，也不执行 callback", async (mode) => {
    const browser = session();
    let release;
    let prepared;
    const preparation = new Promise((resolve) => { prepared = resolve; });
    const barrier = new Promise((resolve) => { release = resolve; });
    browser.wake = async () => { prepared(); await barrier; };
    let calls = 0;
    const method = mode === "queued" ? "enqueueOperation" : "runInteractiveOperation";
    const result = browser[method]({ actor: "agent", action: "read", transportOwnerRevision: 1 }, async () => { calls += 1; })
      .catch((error) => error);
    await preparation;
    expect(browser.pendingOperations).toBe(1);
    browser.syncOwnerRevision(2);
    release();
    expect(await result).toMatchObject({ code: "browser_transport_revoked" });
    await browser.operationQueue.drain();
    expect(calls).toBe(0);
    expect(browser.pendingOperations).toBe(0);
    expect(browser.inFlightOperations).toBe(0);
    expect(browser.operationQueue.acceptedOperationExecutions.size).toBe(0);
  });

  test("撤销等待已开始的动作结束，队列中旧版本动作不执行", async () => {
    const browser = session();
    let release;
    let started;
    const entered = new Promise((resolve) => { started = resolve; });
    const barrier = new Promise((resolve) => { release = resolve; });
    const first = browser.enqueueOperation({ actor: "agent", action: "run", transportOwnerRevision: 1 }, async () => {
      started();
      await barrier;
      return { value: "旧回执" };
    }).catch((error) => error);
    await entered;
    let queuedCalls = 0;
    const second = browser.enqueueOperation({ actor: "agent", action: "read", transportOwnerRevision: 1 }, async () => { queuedCalls += 1; })
      .catch((error) => error);
    browser.syncOwnerRevision(2);
    let drained = false;
    const drain = browser.operationQueue.drain().then(() => { drained = true; });
    await Promise.resolve();
    expect(drained).toBe(false);
    release();
    await drain;
    expect(await first).toMatchObject({ code: "browser_transport_revoked" });
    expect(await second).toMatchObject({ code: "browser_transport_revoked" });
    expect(queuedCalls).toBe(0);
    expect(browser.pendingOperations).toBe(0);
    expect(browser.inFlightOperations).toBe(0);
  });

  test("attach wake await 期间撤销不加入客户端，旧传输明确拒绝", async () => {
    const browser = session();
    let release;
    let waking;
    const entered = new Promise((resolve) => { waking = resolve; });
    const barrier = new Promise((resolve) => { release = resolve; });
    const closed = [];
    const client = { close: (...arguments_) => closed.push(arguments_) };
    browser.transports.set(client, { transportOwnerRevision: 1 });
    browser.wake = async () => { waking(); await barrier; };
    const attach = browser.attachClient(client).catch((error) => error);
    await entered;
    expect(browser.pendingAttachRequests).toBe(1);
    browser.syncOwnerRevision(2);
    release();
    expect(await attach).toMatchObject({ code: "browser_transport_revoked" });
    expect(browser.pendingAttachRequests).toBe(0);
    expect(browser.clients.size).toBe(0);
    expect(browser.transportAuthorized(client)).toBe(false);
    expect(() => browser.assertTransport(client)).toThrow("授权已过期");
    expect(closed).toEqual([[4009, "browser owner changed"]]);
  });

  test("同一新标签页并发注册只生成一个稳定 pageId", async () => {
    const browser = session();
    let cdpSessionCount = 0;
    const page = {
      url: () => "about:blank",
      mainFrame: () => page,
      on: () => undefined,
      isClosed: () => false,
      title: async () => "Blank",
    };
    browser.browser = {};
    browser.context = {
      newCDPSession: async () => {
        cdpSessionCount += 1;
        await new Promise((resolve) => setTimeout(resolve, 0));
        return {
          send: async () => undefined,
          on: () => undefined,
        };
      },
    };
    browser.syncAndEmitState = async () => browser.snapshot();

    const first = browser.registerPage(page, { pageId: "page_first" });
    const second = browser.registerPage(page, { pageId: "page_second" });
    const [firstEntry, secondEntry] = await Promise.all([first, second]);

    expect(firstEntry).toBe(secondEntry);
    expect(firstEntry.pageId).toBe("page_first");
    expect(cdpSessionCount).toBe(1);
    expect(browser.pageEntries.size).toBe(1);
  });

  test("并发注册共享同一失败回执，原生异常只保留在 cause 和日志", async () => {
    const browser = session();
    const context = new EventEmitter();
    const cause = Object.assign(new Error("EACCES: /private/browser/profile; C:\\Users\\test\\profile"), {
      code: "EACCES", statusCode: 500,
    });
    let failRegistration;
    let attempts = 0;
    context.newCDPSession = () => {
      attempts += 1;
      return new Promise((_, reject) => { failRegistration = reject; });
    };
    browser.context = context;
    browser.browser = {};
    const states = [];
    browser.on("state", (state) => states.push(state));
    browser.bindContextPageEvents();
    const page = {};
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      context.emit("page", page);
      const shared = browser.pageRegistrationPromises.get(page);
      const second = browser.registerPage(page, { pageId: "ignored_second_id" }).catch((error) => error);
      failRegistration(cause);
      const failure = await shared.catch((error) => error);
      expect(await second).toBe(failure);
      await new Promise((resolve) => setTimeout(resolve, 0));
      expect(failure).toMatchObject({ code: "EACCES", statusCode: 500, cause });
      expect(failure.message).toContain(`browser_id=${browser.id}`);
      expect(failure.message).toContain("stage=register_page");
      expect(failure.message).not.toContain(cause.message);
      expect(attempts).toBe(1);
      expect(browser.record.status).toBe("running");
      expect(browser.pageEntries.size).toBe(0);
      expect(browser.pageRegistrationPromises.has(page)).toBe(false);
      expect(browser.toRecord().error_message).toBe(failure.message);
      expect(states).toHaveLength(1);
      expect(states[0].error_message).toBe(failure.message);
      expect(JSON.stringify(states)).not.toContain(cause.message);
      expect(diagnostics).toHaveBeenCalledTimes(1);
      expect(diagnostics.mock.calls[0][2]).toBe(cause);
    } finally {
      diagnostics.mockRestore();
    }
  });

  test.each([
    ["Network.setCacheDisabled", true], ["Network.setBypassServiceWorker", true],
    ["device_profile_goto", true], ["restore_device_page_url", true],
    ["Network.setBypassServiceWorker", false], ["Network.setCacheDisabled", false],
  ])("动态设备设置在原生边界保留原因且恢复所有已尝试的开关: %s/%s", async (stage, enabling) => {
    const { browser, cdpCalls } = liveSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    entry.url = "https://example.test/device";
    const cause = Object.assign(new Error(String.raw`EACCES C:\private\device-profile`), { code: "EACCES", statusCode: 503 });
    let injected = false;
    const failOnce = () => { if (!injected) { injected = true; throw cause; } };
    entry.cdpSession.send = async (method, params) => {
      cdpCalls.push({ method, params });
      if (method === stage && (params.cacheDisabled ?? params.bypass) === enabling) failOnce();
    };
    if (stage === "device_profile_goto") entry.page.goto = async () => failOnce();
    if (stage === "restore_device_page_url") entry.page.evaluate = async () => failOnce();
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.setDeviceProfile("pixel-7").catch((error) => error);
      expect(injected).toBe(true);
      expect(failure).toMatchObject({ cause, code: "EACCES", statusCode: 503 });
      expect(failure.message).toContain(`stage=${stage}`);
      expect(failure.message).not.toContain("private");
      expect(browser.record.device_profile).toBe("desktop");
      expect(browser.record.reconciliation_status).toBe(enabling ? "verified" : "unknown");
      expect(cdpCalls.some(({ method, params }) => method === "Network.setCacheDisabled" && params.cacheDisabled === false)).toBe(true);
      if (stage !== "Network.setCacheDisabled" || !enabling) {
        expect(cdpCalls.some(({ method, params }) => method === "Network.setBypassServiceWorker" && params.bypass === false)).toBe(true);
      }
      expect(diagnostics.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      if (enabling) {
        await browser.setDeviceProfile("pixel-7");
        expect(browser.record.device_profile).toBe("pixel-7");
      } else {
        expect(() => browser.assertReconciled()).toThrow("外部状态尚未核实");
      }
    } finally { diagnostics.mockRestore(); }
  });

  test("设备导航、两项缓存恢复和多页回滚失败保留有序因果链并继续清理", async () => {
    const { browser, cdpCalls } = liveSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    const primary = Object.assign(new Error("导航 /private/device"), { code: "EACCES", statusCode: 503 });
    const bypass = new Error("worker /private/device");
    const cache = new Error("cache /private/device");
    const rollback = new Error("viewport /private/device");
    let navigating = false;
    let siblingRestored = false;
    entry.page.goto = async () => { navigating = true; throw primary; };
    entry.page.setViewportSize = async () => { if (navigating) throw rollback; };
    entry.cdpSession.send = async (method, params) => {
      cdpCalls.push({ method, params });
      if (method === "Network.setBypassServiceWorker" && params.bypass === false) throw bypass;
      if (method === "Network.setCacheDisabled" && params.cacheDisabled === false) throw cache;
    };
    browser.pageEntries.set("sibling", { ...entry, pageId: "sibling", page: {
      ...entry.page, setViewportSize: async () => { if (navigating) siblingRestored = true; },
    } });
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.setDeviceProfile("pixel-7").catch((error) => error);
      expect(failure).toBeInstanceOf(AggregateError);
      expect(failure).toMatchObject({ cause: failure.errors[0], code: "EACCES", statusCode: 503 });
      expect(failure.errors[0].errors.map((error) => error.cause)).toEqual([primary, bypass, cache]);
      expect(failure.errors[1].cause).toBe(rollback);
      expect(siblingRestored).toBe(true);
      expect(failure.message).not.toContain("/private/device");
      expect(browser.record.reconciliation_status).toBe("unknown");
      for (const cause of [primary, bypass, cache, rollback]) {
        expect(diagnostics.mock.calls.some((args) => args.at(-1) === cause)).toBe(true);
      }
    } finally { diagnostics.mockRestore(); }
  });

  test.each([false, true])("设备失败恢复已有帧流，恢复失败保留主错并明确未知: %s", async (restartFails) => {
    const { browser, cdpCalls } = liveSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    browser.streaming = true;
    browser.streamProfile = "relaxed";
    browser.clients.add({});
    const primary = Object.assign(new Error("导航 /private/device"), { code: "EACCES" });
    const restart = Object.assign(new Error("流 /private/device"), { code: "EIO" });
    entry.page.goto = async () => { throw primary; };
    entry.cdpSession.send = async (method, params) => {
      cdpCalls.push({ method, params });
      if (method === "Page.startScreencast" && restartFails) throw restart;
    };
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.setDeviceProfile("pixel-7").catch((error) => error);
      expect(failure.code).toBe("EACCES");
      expect(failure.message).not.toContain("/private/device");
      expect(cdpCalls.filter(({ method }) => method.includes("Screencast")).map(({ method }) => method))
        .toEqual(["Page.stopScreencast", "Page.startScreencast"]);
      expect(browser.record.device_profile).toBe("desktop");
      if (restartFails) {
        expect(failure.errors.map((error) => error.cause)).toEqual([primary, restart]);
        expect(browser.record.reconciliation_status).toBe("unknown");
        expect(browser.streaming).toBe(false);
      } else {
        expect(failure.cause).toBe(primary);
        expect(browser.record.reconciliation_status).toBe("verified");
        expect(browser.streaming).toBe(true);
        expect(browser.streamProfile).toBe("relaxed");
      }
    } finally { diagnostics.mockRestore(); }
  });

  test("设备重载保留一次网络变化重试和原页面URL", async () => {
    const { browser, cdpCalls } = liveSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    entry.url = "file:///tmp/device%20page.html";
    const urls = [];
    let restored;
    entry.page.goto = async (url) => {
      urls.push(url);
      if (urls.length === 1) throw new Error("net::ERR_NETWORK_CHANGED");
    };
    entry.page.evaluate = async (_fn, url) => { restored = url; };
    await browser.setDeviceProfile("pixel-7");
    expect(urls).toHaveLength(2);
    expect(urls[0]).toBe(urls[1]);
    expect(new URL(urls[0]).searchParams.get("__boxteam_device_profile")).toContain("pixel-7-portrait-");
    expect(restored).toBe(entry.url);
    expect(cdpCalls.filter(({ method }) => method === "Network.setCacheDisabled").map(({ params }) => params.cacheDisabled)).toEqual([true, false]);
    expect(browser.record.reconciliation_status).toBe("verified");
  });

  test("设备型号切换更新设备参数并重新应用页面布局", async () => {
    const { browser, cdpCalls, getNavigationCount } = liveSession();

    await browser.setDeviceProfile("pixel-7");
    expect(browser.record).toMatchObject({
      device_profile: "pixel-7",
      device_orientation: "portrait",
      viewport: { width: 412, height: 839 },
      device_scale_factor: 2.625,
      touch_simulation_enabled: true,
    });
    expect(cdpCalls.find((call) => call.method === "Emulation.setTouchEmulationEnabled"))
      .toMatchObject({ params: { enabled: true, maxTouchPoints: 5 } });

    await browser.setDeviceProfile("pixel-7", "landscape");
    expect(browser.record).toMatchObject({
      device_profile: "pixel-7",
      device_orientation: "landscape",
      viewport: { width: 839, height: 412 },
    });

    await browser.setDeviceProfile("desktop");
    expect(browser.record).toMatchObject({
      device_profile: "desktop",
      viewport: { width: 1280, height: 800 },
      device_scale_factor: 1,
      touch_simulation_enabled: false,
    });
    expect(getNavigationCount()).toBe(3);
    expect(cdpCalls.find((call) => call.method === "Emulation.setEmitTouchEventsForMouse"
      && call.params.enabled === false)).toMatchObject({
      method: "Emulation.setEmitTouchEventsForMouse",
      params: { enabled: false, configuration: "desktop" },
    });
  });

  test("切换保存预设时保留自定义 DPR、UA、触摸和网络设置", async () => {
    const { browser } = liveSession();

    await browser.setDeviceSettings({
      profileId: "iphone-13",
      orientation: "portrait",
      width: 264,
      height: 478,
      deviceScaleFactor: 1,
      userAgent: "Custom Mobile UA",
      touchSimulation: false,
      networkProfileId: "slow-3g",
    });

    expect(browser.snapshot()).toMatchObject({
      device_profile: "iphone-13",
      viewport: { width: 264, height: 478 },
      device_scale_factor_override: 1,
      user_agent_override: "Custom Mobile UA",
      touch_simulation_override: false,
      network_profile_id: "slow-3g",
    });
  });

  test("清空网络记录会释放当前页面的轻量缓冲区", async () => {
    const { browser } = liveSession();
    const entry = browser.pageEntries.get("page_device");
    for (const url of ["https://example.com/one", "https://example.com/two"]) {
      entry.page.emit("request", {
        method: () => "GET", url: () => url, resourceType: () => "fetch",
      });
    }
    expect(entry.diagnostics.snapshot().network).toHaveLength(2);

    await browser.clearNetworkRequests();

    expect(entry.diagnostics.snapshot().network).toEqual([]);
  });

  test("用户和模型操作按入队顺序执行并获得单调操作序号", async () => {
    const browser = session();
    const order = [];
    const first = browser.enqueueOperation({ actor: "user:1", action: "click" }, async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
      order.push("user");
      return { value: 1 };
    });
    const second = browser.enqueueOperation({ actor: "agent", action: "read" }, async () => {
      order.push("agent");
      return { value: 2 };
    });

    const [firstResult, secondResult] = await Promise.all([first, second]);
    expect(order).toEqual(["user", "agent"]);
    expect(firstResult.operation_revision).toBe(1);
    expect(secondResult.operation_revision).toBe(2);
  });

  test("实时用户输入不会等待长模型操作", async () => {
    const browser = session();
    let releaseModel;
    const modelBarrier = new Promise((resolve) => {
      releaseModel = resolve;
    });
    const model = browser.enqueueOperation({ actor: "agent", action: "run" }, async () => {
      await modelBarrier;
      return { value: "agent" };
    });
    await new Promise((resolve) => setTimeout(resolve, 5));

    const user = await browser.runInteractiveOperation(
      { actor: "user:1", action: "pointer:down" },
      async () => ({ value: "user" }),
    );
    expect(user.value).toBe("user");
    expect(user.operation_revision).toBe(2);

    releaseModel();
    const modelResult = await model;
    expect(modelResult.operation_revision).toBe(1);
    expect(browser.record.operation_revision).toBe(2);
    expect(browser.record.last_operation.operation_revision).toBe(2);
  });

  test("实时用户输入会立即恢复交互画面档位", async () => {
    const browser = session();
    let boostCount = 0;
    browser.boostScreencast = () => {
      boostCount += 1;
    };

    await browser.runInteractiveOperation(
      { actor: "user:1", action: "pointer:move" },
      async () => undefined,
    );

    expect(boostCount).toBe(1);
  });

  test("降档切换尚未完成时的用户输入最终保留交互档", async () => {
    const browser = session();
    browser.streaming = true;
    browser.streamProfile = "interactive";
    let releaseRelaxed;
    const relaxedBarrier = new Promise((resolve) => {
      releaseRelaxed = resolve;
    });
    browser.restartScreencast = async (profile) => {
      if (profile === "relaxed") {
        await relaxedBarrier;
      }
      browser.streamProfile = profile;
    };

    const relaxing = browser.queueScreencastProfile("relaxed");
    await new Promise((resolve) => setTimeout(resolve, 0));
    browser.boostScreencast();
    releaseRelaxed();
    await relaxing;
    await browser.streamTransition;
    clearTimeout(browser.streamRelaxTimer);

    expect(browser.streamProfile).toBe("interactive");
  });

  test("页面动作完成后不遗留 browser-modal listener", async () => {
    const browser = session();
    await browser.runPageActionWithModalDetection(async () => undefined);
    expect(browser.listenerCount("browser-modal")).toBe(0);
  });

  test("页面动作触发 modal 后立即释放等待", async () => {
    const browser = session();
    browser.syncAndEmitState = async () => browser.snapshot();
    const neverSettles = new Promise(() => undefined);
    queueMicrotask(() => browser.emit("browser-modal", { kind: "dialog" }));

    const modal = await browser.runPageActionWithModalDetection(() => neverSettles);
    expect(modal).toBe("dialog");
    expect(browser.listenerCount("browser-modal")).toBe(0);
  });

  test("Playwright 代码超时后隔离页面并允许键盘、读取和截图重试", async () => {
    const browser = session();
    let oldPageClosed = false;
    let pressedKey = null;
    const oldPage = {
      close: async () => {
        oldPageClosed = true;
        await browser.handlePageClosed("page_timeout", oldPage);
      },
      goto: async () => undefined,
      isClosed: () => false,
    };
    const replacementPage = {
      close: async () => undefined,
      goto: async () => undefined,
      isClosed: () => false,
      keyboard: {
        press: async (key) => {
          pressedKey = key;
        },
      },
      evaluate: async () => ({
        title: "Recovered page",
        url: "about:blank",
        documentRevision: 0,
        refs: [],
        text: "recovered",
      }),
      screenshot: async () => Buffer.from("recovered-png"),
      title: async () => "Recovered page",
      url: () => "about:blank",
    };
    const oldEntry = {
      pageId: "page_timeout",
      page: oldPage,
      requestedUrl: "http://127.0.0.1:8011/",
    };
    const replacementEntry = {
      pageId: "page_timeout_replacement",
      page: replacementPage,
      requestedUrl: "about:blank",
    };
    browser.browser = {};
    browser.context = {
      newPage: async () => {
        browser.pendingDialog = { pageId: oldEntry.pageId };
        browser.pendingFileChooser = {};
        return replacementPage;
      },
    };
    browser.page = oldPage;
    browser.cdpSession = {};
    browser.activePageId = oldEntry.pageId;
    browser.pageEntries.set(oldEntry.pageId, oldEntry);
    browser.pageEntries.set(replacementEntry.pageId, replacementEntry);
    browser.pageRegistrationPromises.set(
      replacementPage,
      Promise.resolve(replacementEntry),
    );
    browser.activatePage = async (pageId) => {
      browser.activePageId = pageId;
      browser.page = browser.pageEntries.get(pageId).page;
      browser.cdpSession = {};
    };
    browser.goto = async (url) => {
      browser.record.url = url;
    };
    browser.syncAndEmitState = async () => browser.snapshot();
    browser.manager.writeScreenshot = async () => ({
      screenshot_id: "screenshot_timeout_recovery",
    });

    await expect(browser.runPlaywrightCode({
      code: "await new Promise(() => undefined);",
      timeoutMs: 10,
    })).rejects.toMatchObject({
      code: "browser_tool_timeout",
      timeout_ms: 10,
      retryable: true,
      recovery: "page_reset",
    });

    // 超时错误先返回给调用方；直接调用 BrowserSession 的测试需要等待
    // 同一恢复屏障，真实 HTTP 入队操作会在下一次操作前自动等待它。
    await browser.timeoutRecovery;

    expect(oldPageClosed).toBe(true);
    expect(browser.activePageId).toBe(replacementEntry.pageId);
    expect(browser.page).toBe(replacementPage);
    expect(browser.pageEntries.has(oldEntry.pageId)).toBe(false);
    expect(browser.record.url).toBe("http://127.0.0.1:8011/");
    expect(browser.pendingDialog).toBeNull();
    expect(browser.pendingFileChooser).toBeNull();

    const keyboardResult = await browser.runPlaywrightCode({
      code: "await page.keyboard.press('r'); return 'keyboard-ok';",
      timeoutMs: 100,
    });
    expect(keyboardResult.result).toBe("keyboard-ok");
    expect(pressedKey).toBe("r");

    const summary = await browser.readSummary();
    expect(summary.title).toBe("Recovered page");

    const screenshot = await browser.screenshot({});
    expect(screenshot).toMatchObject({
      screenshot_id: "screenshot_timeout_recovery",
      screenshot_url: "/api/browsers/browser_operation_test/screenshots/screenshot_timeout_recovery",
      mime_type: "image/png",
    });
  });

  test("恢复失败的同一屏障阻止后续操作，原生异常只留 cause 和诊断", async () => {
    const browser = session();
    const page = { isClosed: () => false };
    const cause = new Error("EACCES: /private/browser/profile");
    browser.page = page;
    browser.browser = {};
    browser.cdpSession = {};
    browser.context = { newPage: async () => { throw cause; } };
    browser.activePageId = "page_failed_recovery";
    browser.pageEntries.set(browser.activePageId, { pageId: browser.activePageId, page });
    const diagnostics = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.runPlaywrightCode({
        code: "await new Promise(() => undefined);", timeoutMs: 5,
      }).catch((error) => error);
      expect(failure.code).toBe("browser_tool_timeout");
      const barrierFailure = await browser.timeoutRecovery.catch((error) => error);
      expect(barrierFailure.message).toBe(`浏览器页面恢复失败: browser_id=${browser.id}, page_id=page_failed_recovery, stage=reset_page`);
      const creationFailure = barrierFailure.cause;
      expect(creationFailure.message).toContain("stage=create_page");
      expect(creationFailure.cause).toBe(cause);
      expect(failure.cause).toBe(barrierFailure);
      expect(failure.recovery).toBe("page_reset_failed");
      let executed = false;
      const queuedFailure = await browser.enqueueOperation({ actor: "agent", action: "read" }, async () => {
        executed = true;
      }).catch((error) => error);
      expect(queuedFailure).toBe(barrierFailure);
      expect(executed).toBe(false);
      expect(browser.pendingOperations).toBe(0);
      expect(browser.recoveringTimedOutPageIds.size).toBe(0);
      expect(diagnostics).toHaveBeenCalledTimes(2);
      expect(diagnostics.mock.calls[0][2]).toBe(cause);
      expect(diagnostics.mock.calls[1][2]).toBe(barrierFailure);
    } finally {
      diagnostics.mockRestore();
    }
  });

  test("readSummary 超时立即返回可重试错误并在下一次操作前恢复页面", async () => {
    const browser = session();
    let oldPageClosed = false;
    const oldPage = {
      close: async () => {
        oldPageClosed = true;
        await browser.handlePageClosed("page_read_timeout", oldPage);
      },
      evaluate: async () => new Promise(() => undefined),
      isClosed: () => false,
      title: async () => "旧页面",
      url: () => "http://127.0.0.1:8765/",
    };
    const replacementPage = {
      close: async () => undefined,
      evaluate: async () => ({
        title: "Recovered read page",
        url: "http://127.0.0.1:8765/",
        documentRevision: 0,
        refs: [],
        text: "read-recovered",
      }),
      goto: async () => undefined,
      isClosed: () => false,
      title: async () => "Recovered read page",
      url: () => "http://127.0.0.1:8765/",
    };
    const oldEntry = {
      pageId: "page_read_timeout",
      page: oldPage,
      requestedUrl: "http://127.0.0.1:8765/",
    };
    const replacementEntry = {
      pageId: "page_read_timeout_replacement",
      page: replacementPage,
      requestedUrl: "about:blank",
    };
    browser.browser = {};
    browser.context = {
      newPage: async () => replacementPage,
    };
    browser.page = oldPage;
    browser.cdpSession = {};
    browser.activePageId = oldEntry.pageId;
    browser.pageEntries.set(oldEntry.pageId, oldEntry);
    browser.pageEntries.set(replacementEntry.pageId, replacementEntry);
    browser.pageRegistrationPromises.set(
      replacementPage,
      Promise.resolve(replacementEntry),
    );
    browser.activatePage = async (pageId) => {
      browser.activePageId = pageId;
      browser.page = browser.pageEntries.get(pageId).page;
      browser.cdpSession = {};
    };
    browser.goto = async (url) => {
      browser.record.url = url;
    };
    browser.syncAndEmitState = async () => browser.snapshot();

    await expect(browser.readSummary({ timeoutMs: 10 })).rejects.toMatchObject({
      code: "browser_tool_timeout",
      timeout_ms: 10,
      retryable: true,
      recovery: "page_reset",
    });

    await browser.timeoutRecovery;

    expect(oldPageClosed).toBe(true);
    expect(browser.activePageId).toBe(replacementEntry.pageId);
    expect(browser.page).toBe(replacementPage);
    expect(browser.pageEntries.has(oldEntry.pageId)).toBe(false);

    const summary = await browser.readSummary({ timeoutMs: 100 });
    expect(summary.summary).toContain("read-recovered");
  });

  test("截图超时返回可重试错误并复用读取超时的页面恢复机制", async () => {
    const browser = session();
    const oldPage = {
      close: async () => {
        await browser.handlePageClosed("page_screenshot_timeout", oldPage);
      },
      isClosed: () => false,
      screenshot: async () => new Promise(() => undefined),
    };
    const replacementPage = {
      close: async () => undefined,
      goto: async () => undefined,
      isClosed: () => false,
      screenshot: async () => Buffer.from("recovered-png"),
      title: async () => "Recovered screenshot page",
      url: () => "about:blank",
    };
    const oldEntry = {
      pageId: "page_screenshot_timeout",
      page: oldPage,
      requestedUrl: "about:blank",
    };
    const replacementEntry = {
      pageId: "page_screenshot_timeout_replacement",
      page: replacementPage,
      requestedUrl: "about:blank",
    };
    browser.browser = {};
    browser.context = {
      newPage: async () => replacementPage,
    };
    browser.page = oldPage;
    browser.cdpSession = {};
    browser.activePageId = oldEntry.pageId;
    browser.pageEntries.set(oldEntry.pageId, oldEntry);
    browser.pageEntries.set(replacementEntry.pageId, replacementEntry);
    browser.pageRegistrationPromises.set(
      replacementPage,
      Promise.resolve(replacementEntry),
    );
    browser.activatePage = async (pageId) => {
      browser.activePageId = pageId;
      browser.page = browser.pageEntries.get(pageId).page;
      browser.cdpSession = {};
    };
    browser.goto = async () => undefined;
    browser.syncAndEmitState = async () => browser.snapshot();
    browser.manager.writeScreenshot = async () => ({
      screenshot_id: "screenshot_recovery",
    });

    await expect(browser.screenshot({}, { timeoutMs: 10 })).rejects.toMatchObject({
      code: "browser_tool_timeout",
      retryable: true,
      recovery: "page_reset",
    });

    await browser.timeoutRecovery;

    const screenshot = await browser.screenshot({});
    expect(screenshot).toMatchObject({
      screenshot_id: "screenshot_recovery",
      screenshot_url: "/api/browsers/browser_operation_test/screenshots/screenshot_recovery",
      mime_type: "image/png",
    });
  });

  test("替换页面后旧标签页的关闭事件不会删除新标签页记录", async () => {
    const browser = session();
    const replacementPage = {};
    browser.pageEntries.set("page_replacement", {
      pageId: "page_replacement",
      page: replacementPage,
    });

    await browser.handlePageClosed("page_replacement", {});

    expect(browser.pageEntries.has("page_replacement")).toBe(true);
  });

  test.each(["url", "reload", "back", "forward"])("%s 导航失败保留目标与错误码，原生位置只留诊断", async (type) => {
    const browser = session();
    const pageId = "page_navigation_failure";
    const nativePath = String.raw`C:\private\browser\profile`;
    const cause = Object.assign(new Error(`page.goto: net::ERR_CONNECTION_REFUSED ${nativePath}\n\u001b[2mCall log\u001b[22m`), { statusCode: 503 });
    const method = { url: "goto", reload: "reload", back: "goBack", forward: "goForward" }[type];
    browser.page = {
      [method]: async () => { throw cause; },
      url: () => "chrome-error://chromewebdata/",
      title: async () => "",
    };
    browser.context = {};
    browser.browser = {};
    browser.cdpSession = {};
    browser.activePageId = pageId;
    browser.pageEntries.set(pageId, {
      pageId,
      page: browser.page,
      documentRevision: 0,
      requestedUrl: "file:///user/opened.html",
      navigationError: null,
      url: "about:blank",
      actualUrl: "about:blank",
      title: "",
    });

    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const failure = await browser.navigate(type, "file:///user/opened.html").catch(error => error);
      expect(failure).toMatchObject({ cause, statusCode: 503 });
      expect(failure.message).toContain(`stage=page.${method}`);
      expect(failure.message).toContain("ERR_CONNECTION_REFUSED");
      expect(failure.message).not.toContain(nativePath);
      expect(logged.mock.calls.some(args => args.at(-1) === cause)).toBe(true);
      const snapshot = browser.snapshot();
      expect(snapshot.url).toBe("file:///user/opened.html");
      expect(snapshot.actual_url).toBe("chrome-error://chromewebdata/");
      expect(snapshot.navigation_error.message).toBe(failure.message);
      expect(snapshot.navigation_error.message).not.toContain("\u001b");
      expect(snapshot.pages[0].url).toBe("file:///user/opened.html");
      expect(snapshot.pages[0].actual_url).toBe("chrome-error://chromewebdata/");
      expect(JSON.stringify(browser.toRecord())).not.toContain(nativePath);
    } finally { logged.mockRestore(); }
  });

  test.each(["back", "forward"])("%s 无历史时保留已有导航错误", async (type) => {
    const { browser } = transitionSession();
    const entry = browser.pageEntries.get(browser.activePageId);
    const failure = { message: "已有请求失败", requested_url: "https://example.test/failed" };
    entry.navigationError = failure;
    browser.page[type === "back" ? "goBack" : "goForward"] = async () => null;
    await browser.navigate(type);
    expect(browser.record.navigation_error).toBe(failure);
    expect(entry.navigationError).toBe(failure);
  });

  test.each(["read_navigation_url", "sync_navigation_state", "capture_navigation_frame"])(
    "导航和%s同时失败保留两份原因及主错误分类", async (stage) => {
      const { browser } = transitionSession();
      const primary = Object.assign(new Error("导航失败 /private/profile"), { code: "EACCES", statusCode: 503 });
      const secondary = new Error("状态发布失败 /private/second-profile");
      browser.page.goto = async () => { throw primary; };
      if (stage === "read_navigation_url") browser.page.url = () => { throw secondary; };
      else if (stage === "sync_navigation_state") browser.page.title = async () => { throw secondary; };
      else {
        browser.clients.add({});
        browser.cdpSession.send = async () => { throw secondary; };
      }
      const logged = spyOn(console, "error").mockImplementation(() => undefined);
      try {
        const failure = await browser.goto("file:///user/opened.html").catch(error => error);
        expect(failure).toBeInstanceOf(AggregateError);
        expect(failure).toMatchObject({ code: "EACCES", statusCode: 503 });
        expect(failure.cause).toBe(failure.errors[0]);
        expect(failure.errors.map(error => error.cause)).toEqual([primary, secondary]);
        expect(failure.message).toContain("file:///user/opened.html");
        expect(failure.message).not.toContain("/private/");
      } finally { logged.mockRestore(); }
    },
  );

  test("导航失败归属发起标签页，切换后的页面不接收旧错误", async () => {
    const { browser } = transitionSession();
    const sourceEntry = browser.pageEntries.get(browser.activePageId);
    const cause = new Error("导航失败 /private/browser/profile");
    let rejectNavigation;
    sourceEntry.page.goto = () => new Promise((_resolve, reject) => { rejectNavigation = reject; });
    const logged = spyOn(console, "error").mockImplementation(() => undefined);
    try {
      const navigation = browser.goto("https://example.test/slow").catch(error => error);
      await browser.activatePage("page_second");
      rejectNavigation(cause);
      const failure = await navigation;
      expect(failure.cause).toBe(cause);
      expect(failure.message).toContain(`page_id=${sourceEntry.pageId}`);
      expect(sourceEntry.navigationError.page_id).toBe(sourceEntry.pageId);
      expect(browser.pageEntries.get("page_second").navigationError).toBeUndefined();
      expect(browser.record.navigation_error).toBeNull();
    } finally { logged.mockRestore(); }
  });

  test("冷回收会话不再依赖已释放的共享运行时", async () => {
    const browser = session();
    browser.record.resource_state = "discarded";
    browser.chromiumInstanceGeneration = 3;

    await browser.handleChromiumDisconnect(3);

    expect(browser.record.status).toBe("running");
    expect(browser.record.resource_state).toBe("discarded");
  });

  test("冻结资源的快速策略快照不会再次执行页面脚本", () => {
    const browser = session();
    browser.record.resource_state = "frozen";
    browser.cachedSoftProtectionReasons = ["media_playing:page_test"];
    browser.softProtectionObservedAtUnixMsByReason.set("media_playing:page_test", Date.now());
    browser.pageEntries.set("page_test", {
      pageId: "page_test",
      page: {
        isClosed: () => false,
        evaluate: () => {
          throw new Error("冻结页面不应执行 evaluate");
        },
      },
      webSockets: new Set(),
    });

    const snapshot = browser.resourcePolicySnapshot();

    expect(snapshot.resource_soft_protection_reasons).toEqual(["media_playing:page_test"]);
    expect(snapshot.resource_hard_protection_reasons).toEqual([]);
    expect(snapshot.pages).toBeUndefined();
    expect(snapshot.stream_metrics).toBeUndefined();
  });

  test("WebSocket只有近期帧活动才提供可过期的soft保护", () => {
    const browser = session();
    browser.pageEntries.set("page_socket", {
      pageId: "page_socket",
      webSockets: new Set(["socket_1"]),
    });
    browser.lastWebSocketActivityAtUnixMs = Date.now();

    const recent = browser.resourcePolicySnapshot();
    browser.lastWebSocketActivityAtUnixMs = Date.now() - 6 * 60_000;
    const expired = browser.resourcePolicySnapshot();

    expect(recent.resource_soft_protection_reasons).toContain("websocket_recent_activity");
    expect(recent.resource_protections.find((item) => item.code === "websocket_recent_activity"))
      .toMatchObject({ class: "soft" });
    expect(expired.resource_soft_protection_reasons).not.toContain("websocket_recent_activity");
  });

  test("冻结资源不会永久保留过期的WebSocket缓存保护", () => {
    const browser = session();
    browser.record.resource_state = "frozen";
    browser.cachedSoftProtectionReasons = ["websocket_recent_activity"];
    browser.lastWebSocketActivityAtUnixMs = Date.now() - 6 * 60_000;
    browser.pageEntries.set("page_socket", {
      pageId: "page_socket",
      webSockets: new Set(["socket_1"]),
    });

    expect(browser.resourcePolicySnapshot().resource_soft_protection_reasons)
      .not.toContain("websocket_recent_activity");
  });

  test("普通冷回收尊重缓存soft保护而严重压力可显式越过", async () => {
    const browser = session();
    browser.record.resource_state = "frozen";
    browser.cachedSoftProtectionReasons = ["media_playing:page_media"];
    browser.softProtectionObservedAtUnixMsByReason.set("media_playing:page_media", Date.now());
    browser.manager.stateStore.readCheckpoint = async () => ({
      pages: [{ page_id: "page_media", title: "Media", url: "about:blank" }],
      active_page_id: "page_media",
    });
    browser.releaseRuntime = async () => undefined;

    await expect(browser.discard())
      .rejects.toMatchObject({ code: "browser_resource_protected" });
    await expect(browser.discard({ allowSoftProtection: true }))
      .resolves.toMatchObject({ resource_state: "discarded" });
  });

  test("冻结媒体保护租约过期后不再永久占用frozen名额", () => {
    const browser = session();
    browser.record.resource_state = "frozen";
    browser.cachedSoftProtectionReasons = ["media_playing:page_media"];
    browser.softProtectionObservedAtUnixMsByReason.set("media_playing:page_media", Date.now() - 6 * 60_000);

    expect(browser.resourcePolicySnapshot().resource_soft_protection_reasons)
      .not.toContain("media_playing:page_media");
  });

  test("恢复渲染后丢弃旧文档的冻结样式表ID", async () => {
    const browser = session();
    const commands = [];
    const entry = {
      freezeStyleSheetId: "stylesheet_old_document",
      cdpSession: {
        send: async (method, params) => {
          commands.push([method, params]);
        },
      },
    };

    await browser.resumePageRendering(entry);

    expect(entry.freezeStyleSheetId).toBeNull();
    expect(commands.at(-1)).toEqual([
      "CSS.setStyleSheetText",
      { styleSheetId: "stylesheet_old_document", text: "" },
    ]);
  });

  test("关闭请求等待进行中的资源转换且阻止新操作", async () => {
    const browser = session();
    const order = [];
    let releaseTransition;
    const transitionBarrier = new Promise((resolve) => {
      releaseTransition = resolve;
    });
    browser.releaseRuntime = async () => {
      order.push("close");
    };
    const transition = browser.runResourceTransition(async () => {
      order.push("transition_start");
      await transitionBarrier;
      order.push("transition_end");
    });
    await Promise.resolve();

    const closing = browser.close();
    await Promise.resolve();
    await expect(browser.prepareForOperation({ actor: "agent" }))
      .rejects.toMatchObject({ code: "browser_closing" });
    expect(order).toEqual(["transition_start"]);

    releaseTransition();
    await Promise.all([transition, closing]);
    expect(order).toEqual(["transition_start", "transition_end", "close"]);
    expect(browser.record.status).toBe("closed");
  });

  test("管理器关闭期间的标签页关闭事件不再切换已销毁页面", async () => {
    const browser = session();
    browser.closingRequested = true;
    browser.activePageId = "page_closing";
    browser.pageEntries.set("page_closing", { pageId: "page_closing" });
    browser.pageEntries.set("page_replacement", { pageId: "page_replacement" });
    browser.activatePage = async () => {
      throw new Error("关机期间不应切换页面");
    };

    await browser.handlePageClosed("page_closing");

    expect(browser.pageEntries.has("page_closing")).toBe(false);
    expect(browser.activePageId).toBe("page_closing");
  });

  test("管理器关闭时生成检查点并冷回收而不是关闭资源", async () => {
    const browser = session();
    browser.record.resource_state = "background";
    browser.record.agent_access_locked = true;
    browser.record.agent_lock_owner_id = "user_test";
    browser.record.agent_lock_expires_at = new Date(Date.now() + 60_000).toISOString();
    browser.clients.add({ participantId: "user_attached" });
    const transitions = [];
    browser.freeze = async (options) => {
      transitions.push(["freeze", options]);
      browser.record.resource_state = "frozen";
      return browser.snapshot();
    };
    browser.discard = async (options) => {
      transitions.push(["discard", options]);
      browser.record.resource_state = "discarded";
      browser.record.checkpoint = { path: "/checkpoint/browser.json" };
      return browser.snapshot();
    };

    const result = await browser.checkpointForManagerShutdown("browser_manager_sigterm");

    expect(result.status).toBe("running");
    expect(result.resource_state).toBe("discarded");
    expect(result.checkpoint).toEqual({ path: "/checkpoint/browser.json" });
    expect(browser.clients.size).toBe(0);
    expect(browser.record.agent_access_locked).toBe(false);
    expect(transitions).toEqual([
      ["freeze", {
        reason: "browser_manager_sigterm",
        allowSoftProtection: true,
        allowHardProtection: true,
      }],
      ["discard", {
        reason: "browser_manager_sigterm",
        allowSoftProtection: true,
        allowHardProtection: true,
      }],
    ]);
  });

  test("管理器关闭时队列无法排空会明确失败而不无限等待", async () => {
    const browser = session();
    browser.operationQueue.serialOperationTail = new Promise(() => undefined);

    await expect(
      browser.checkpointForManagerShutdown("browser_manager_shutdown", {
        operationDrainTimeoutMs: 5,
      }),
    ).rejects.toMatchObject({ code: "browser_shutdown_operation_drain_timeout" });
  });

  test.each([-60_000, 60_000])("管理器关闭排空不因系统校时提前失败: wall_clock_jump_ms=%s", async (wallClockJumpMs) => {
    const browser = session();
    const transitions = [];
    let elapsedMs = 0;
    let wallOffsetMs = 0;
    browser.inFlightOperations = 1;
    browser.record.resource_state = "frozen";
    browser.discard = async () => {
      transitions.push("discard");
      browser.record.resource_state = "discarded";
    };
    browser.operationQueue.serialOperationTail = Promise.resolve().then(() => {
      wallOffsetMs = wallClockJumpMs;
    });
    // 只替换时间边界；队列等待、在途检查和关闭入口仍执行真实Session方法。
    const elapsedClock = spyOn(performance, "now").mockImplementation(() => elapsedMs);
    const wallClock = spyOn(Date, "now").mockImplementation(() => 1_800_000_000_000 + elapsedMs + wallOffsetMs);
    const drainTimer = {};
    const clearedTimers = [];
    const timers = spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
      if (delay === 100) return drainTimer;
      expect(delay).toBe(25);
      queueMicrotask(() => {
        elapsedMs += delay;
        if (elapsedMs >= 50) browser.inFlightOperations = 0;
        callback(...args);
      });
      return {};
    });
    const clearTimers = spyOn(globalThis, "clearTimeout").mockImplementation((timer) => {
      clearedTimers.push(timer);
    });
    try {
      const result = await browser.checkpointForManagerShutdown("clock_test", { operationDrainTimeoutMs: 100 });
      expect(result.resource_state).toBe("discarded");
      expect(elapsedMs).toBe(50);
      expect(transitions).toEqual(["discard"]);
      expect(clearedTimers).toEqual([drainTimer]);
    } finally {
      clearTimers.mockRestore();
      timers.mockRestore();
      wallClock.mockRestore();
      elapsedClock.mockRestore();
    }
  });

  test.each([
    [-60_000, 0], [60_000, 0], [-60_000, 40], [60_000, 40],
  ])("管理器关闭串行队列与在途操作共享单调预算: wall_clock_jump_ms=%s, queued_ms=%s", async (wallClockJumpMs, queuedMs) => {
    const browser = session();
    const attachedClient = {};
    browser.clients.add(attachedClient);
    browser.record.agent_access_locked = true;
    browser.inFlightOperations = 1;
    let elapsedMs = 0;
    let wallOffsetMs = 0;
    const sideEffects = [];
    browser.freeze = async () => { sideEffects.push("freeze"); };
    browser.discard = async () => { sideEffects.push("discard"); };
    browser.manager.persist = async () => { sideEffects.push("persist"); };
    browser.operationQueue.serialOperationTail = Promise.resolve().then(() => {
      elapsedMs = queuedMs;
      wallOffsetMs = wallClockJumpMs;
    });
    const elapsedClock = spyOn(performance, "now").mockImplementation(() => elapsedMs);
    const wallClock = spyOn(Date, "now").mockImplementation(() => 1_800_000_000_000 + elapsedMs + wallOffsetMs);
    const drainTimer = {};
    const clearedTimers = [];
    const timers = spyOn(globalThis, "setTimeout").mockImplementation((callback, delay, ...args) => {
      if (delay === 50) return drainTimer;
      expect(delay).toBe(25);
      queueMicrotask(() => {
        elapsedMs += delay;
        // 错误实现回拨后仍有有限终点，不能让回归测试本身无限循环。
        if (elapsedMs >= 100) browser.inFlightOperations = 0;
        callback(...args);
      });
      return {};
    });
    const clearTimers = spyOn(globalThis, "clearTimeout").mockImplementation((timer) => {
      clearedTimers.push(timer);
    });
    try {
      await expect(browser.checkpointForManagerShutdown("clock_test", { operationDrainTimeoutMs: 50 }))
        .rejects.toMatchObject({ code: "browser_shutdown_operation_drain_timeout" });
      expect(elapsedMs).toBe(queuedMs === 0 ? 50 : 65);
      expect(browser.inFlightOperations).toBe(1);
      expect(sideEffects).toEqual([]);
      expect(browser.clients.has(attachedClient)).toBe(true);
      expect(browser.record.agent_access_locked).toBe(true);
      expect(clearedTimers).toEqual([drainTimer]);
    } finally {
      clearTimers.mockRestore();
      timers.mockRestore();
      wallClock.mockRestore();
      elapsedClock.mockRestore();
    }
  });
});
