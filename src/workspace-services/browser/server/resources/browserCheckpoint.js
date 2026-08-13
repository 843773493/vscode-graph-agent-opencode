const CHECKPOINT_VERSION = 1;

async function capturePageState(entry) {
  const pageState = await entry.page.evaluate(() => {
    function selectorFor(element) {
      if (element.id) return `#${CSS.escape(element.id)}`;
      const segments = [];
      let current = element;
      while (current instanceof Element && current !== document.documentElement) {
        const tag = current.tagName.toLowerCase();
        const siblings = current.parentElement
          ? [...current.parentElement.children].filter((item) => item.tagName === current.tagName)
          : [];
        const suffix = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(current) + 1})` : "";
        segments.unshift(`${tag}${suffix}`);
        current = current.parentElement;
      }
      return segments.join(" > ");
    }

    const controls = [...document.querySelectorAll("input, textarea, select")].map((element) => ({
      selector: selectorFor(element),
      value: element.value,
      checked: element instanceof HTMLInputElement ? element.checked : null,
      selected_values: element instanceof HTMLSelectElement
        ? [...element.selectedOptions].map((option) => option.value)
        : null,
    }));
    let sessionStorageState;
    try {
      sessionStorageState = {
        available: true,
        origin: location.origin,
        entries: Object.entries(sessionStorage),
        error: null,
      };
    } catch (error) {
      sessionStorageState = {
        available: false,
        origin: location.origin,
        entries: [],
        error: error instanceof Error ? error.message : String(error),
      };
    }
    return {
      scroll_x: window.scrollX,
      scroll_y: window.scrollY,
      controls,
      session_storage: sessionStorageState,
    };
  });
  return {
    page_id: entry.pageId,
    title: await entry.page.title(),
    url: entry.page.url(),
    requested_url: entry.requestedUrl || entry.page.url(),
    navigation_error: entry.navigationError || null,
    created_at: entry.createdAt,
    ...pageState,
  };
}

export function checkpointRestoreUrl(pageState) {
  const actualUrl = pageState?.url;
  if ((actualUrl === "chrome-error://chromewebdata/" || pageState?.navigation_error)
    && pageState?.requested_url) {
    return pageState.requested_url;
  }
  return actualUrl || pageState?.requested_url || "about:blank";
}

export async function captureBrowserCheckpoint(session) {
  const pages = [];
  for (const entry of session.pageEntries.values()) {
    if (!entry.page.isClosed()) pages.push(await capturePageState(entry));
  }
  if (pages.length === 0) {
    throw new Error(`浏览器没有可写入检查点的标签页: browser_id=${session.id}`);
  }
  // TODO: 改为流式写入临时文件并在序列化过程中执行20MiB上限，避免超大 storageState 先完整驻留内存。
  return {
    version: CHECKPOINT_VERSION,
    browser_id: session.id,
    created_at: new Date().toISOString(),
    active_page_id: session.activePageId,
    storage_state: await session.context.storageState({ indexedDB: true }),
    pages,
    capabilities: {
      cookies: true,
      local_storage: true,
      indexed_db: true,
      session_storage: true,
      form_values: true,
      scroll_position: true,
      javascript_heap: false,
      navigation_history: false,
      live_connections: false,
    },
  };
}

function sessionStorageInitScript(state) {
  if (!state?.available) return null;
  return ({ expectedOrigin, entries }) => {
    if (location.origin !== expectedOrigin) return;
    sessionStorage.clear();
    for (const [key, value] of entries) sessionStorage.setItem(key, value);
  };
}

async function restorePageState(page, pageState) {
  await page.evaluate((state) => {
    for (const control of state.controls) {
      const element = document.querySelector(control.selector);
      if (!(element instanceof HTMLInputElement
        || element instanceof HTMLTextAreaElement
        || element instanceof HTMLSelectElement)) continue;
      element.value = control.value;
      if (element instanceof HTMLInputElement && control.checked !== null) {
        element.checked = control.checked;
      }
      if (element instanceof HTMLSelectElement && Array.isArray(control.selected_values)) {
        const selectedValues = new Set(control.selected_values);
        for (const option of element.options) option.selected = selectedValues.has(option.value);
      }
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    }
    window.scrollTo(state.scroll_x, state.scroll_y);
  }, pageState);
}

export async function restoreBrowserCheckpoint(session, checkpoint) {
  if (checkpoint?.version !== CHECKPOINT_VERSION || checkpoint.browser_id !== session.id) {
    throw new Error(
      `浏览器检查点不兼容: browser_id=${session.id}, version=${checkpoint?.version}, checkpoint_browser_id=${checkpoint?.browser_id}`,
    );
  }
  if (!Array.isArray(checkpoint.pages) || checkpoint.pages.length === 0) {
    throw new Error(`浏览器检查点没有标签页: browser_id=${session.id}`);
  }
  const runtime = await session.manager.runtimePool.acquireContext({
    viewport: session.record.viewport,
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: true,
    storageState: checkpoint.storage_state,
  });
  session.assignRuntime(runtime);
  try {
    for (const pageState of checkpoint.pages) {
      const page = await session.context.newPage();
      const initScript = sessionStorageInitScript(pageState.session_storage);
      if (initScript) {
        await page.addInitScript(initScript, {
          expectedOrigin: pageState.session_storage.origin,
          entries: pageState.session_storage.entries,
        });
      }
      const entry = await session.registerPage(page, {
        pageId: pageState.page_id,
        activate: false,
      });
      entry.createdAt = pageState.created_at;
      entry.requestedUrl = pageState.requested_url;
      const restoreUrl = checkpointRestoreUrl(pageState);
      try {
        await page.goto(restoreUrl, { waitUntil: "domcontentloaded" });
        await restorePageState(page, pageState);
      } catch (error) {
        const wrapped = new Error(
          `恢复浏览器标签页失败: browser_id=${session.id}, page_id=${pageState.page_id}, url=${restoreUrl}, error=${error instanceof Error ? error.message : String(error)}`,
        );
        wrapped.code = "browser_checkpoint_page_restore_failed";
        throw wrapped;
      }
    }
    session.bindContextPageEvents();
    const activePageId = session.pageEntries.has(checkpoint.active_page_id)
      ? checkpoint.active_page_id
      : checkpoint.pages[0].page_id;
    await session.activatePage(activePageId);
  } catch (error) {
    await session.releaseRuntime();
    throw error;
  }
  return checkpoint;
}
