import { describe, expect, test } from "bun:test";
import { BrowserSession } from "./browserSession.js";

function session() {
  return new BrowserSession({
    manager: {
      attachUrl: (id) => `http://browser.test/?browserId=${id}`,
      persist: async () => undefined,
      stateStore: { deleteCheckpoint: async () => undefined },
    },
    record: {
      browser_id: "browser_operation_test",
      session_id: "session_operation_test",
      status: "running",
    },
  });
}

function liveSession() {
  const browser = session();
  const cdpCalls = [];
  let navigationCount = 0;
  const page = {
    setViewportSize: async () => undefined,
    goto: async () => {
      navigationCount += 1;
    },
    evaluate: async () => undefined,
  };
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
  });
  browser.syncAndEmitState = async () => browser.snapshot();
  return { browser, cdpCalls, getNavigationCount: () => navigationCount };
}

describe("浏览器统一操作队列", () => {
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
    entry.networkRequests = [
      { url: "https://example.com/one", status: 200 },
      { url: "https://example.com/two", status: null },
    ];

    await browser.clearNetworkRequests();

    expect(entry.networkRequests).toEqual([]);
  });

  test("用户和模型操作按入队顺序执行并获得单调 revision", async () => {
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
    browser.manager.writeScreenshot = async () => "/tmp/browser-timeout-recovery.png";

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
      image_path: "/tmp/browser-timeout-recovery.png",
      mime_type: "image/png",
    });
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
    browser.manager.writeScreenshot = async () => "/tmp/browser-screenshot-recovery.png";

    await expect(browser.screenshot({}, { timeoutMs: 10 })).rejects.toMatchObject({
      code: "browser_tool_timeout",
      retryable: true,
      recovery: "page_reset",
    });

    await browser.timeoutRecovery;

    const screenshot = await browser.screenshot({});
    expect(screenshot).toMatchObject({
      image_path: "/tmp/browser-screenshot-recovery.png",
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

  test("导航失败保留用户请求地址并暴露详细错误", async () => {
    const browser = session();
    const pageId = "page_navigation_failure";
    browser.page = {
      goto: async () => {
        throw new Error("page.goto: net::ERR_CONNECTION_REFUSED\n\u001b[2mCall log\u001b[22m");
      },
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
      requestedUrl: "about:blank",
      navigationError: null,
      url: "about:blank",
      actualUrl: "about:blank",
      title: "",
    });

    await expect(browser.goto("http://127.0.0.1:1/")).rejects.toThrow("ERR_CONNECTION_REFUSED");

    const snapshot = browser.snapshot();
    expect(snapshot.url).toBe("http://127.0.0.1:1/");
    expect(snapshot.actual_url).toBe("chrome-error://chromewebdata/");
    expect(snapshot.navigation_error.message).toContain("ERR_CONNECTION_REFUSED");
    expect(snapshot.navigation_error.message).not.toContain("\u001b");
    expect(snapshot.pages[0].url).toBe("http://127.0.0.1:1/");
    expect(snapshot.pages[0].actual_url).toBe("chrome-error://chromewebdata/");
  });

  test("冷回收会话不再依赖已释放的共享运行时", async () => {
    const browser = session();
    browser.record.resource_state = "discarded";
    browser.runtimeGeneration = 3;

    await browser.handleRuntimeDisconnect(3);

    expect(browser.record.status).toBe("running");
    expect(browser.record.resource_state).toBe("discarded");
  });

  test("冻结资源的快速策略快照不会再次执行页面脚本", () => {
    const browser = session();
    browser.record.resource_state = "frozen";
    browser.cachedSoftProtectionReasons = ["media_playing:page_test"];
    browser.softProtectionObservedAtMs.set("media_playing:page_test", Date.now());
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
    browser.lastWebSocketActivityAtMs = Date.now();

    const recent = browser.resourcePolicySnapshot();
    browser.lastWebSocketActivityAtMs = Date.now() - 6 * 60_000;
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
    browser.lastWebSocketActivityAtMs = Date.now() - 6 * 60_000;
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
    browser.softProtectionObservedAtMs.set("media_playing:page_media", Date.now());
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
    browser.softProtectionObservedAtMs.set("media_playing:page_media", Date.now() - 6 * 60_000);

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
    browser.operationQueue.tail = new Promise(() => undefined);

    await expect(
      browser.checkpointForManagerShutdown("browser_manager_shutdown", {
        operationDrainTimeoutMs: 5,
      }),
    ).rejects.toMatchObject({ code: "browser_shutdown_operation_drain_timeout" });
  });
});
