import { browserRuntimeFailure } from "../browserRuntime.js";

const CHECKPOINT_VERSION = 1;

async function capturePageState(session, entry) {
  try {
    const pageState = await entry.page.evaluate(() => {
      // 本次同步采集内按父节点索引一次兄弟顺序；后一次采集必须重新读取DOM。
      const siblingIndexesByParent = new WeakMap();
      function selectorFor(element) {
        if (element.id) return `#${CSS.escape(element.id)}`;
        const segments = [];
        let current = element;
        while (current instanceof Element && current !== document.documentElement) {
          const tag = current.tagName.toLowerCase();
          const parent = current.parentElement;
          let siblings = parent ? siblingIndexesByParent.get(parent) : null;
          if (parent && !siblings) {
            siblings = { counts: new Map(), positions: new Map() };
            for (const child of parent.children) {
              const position = (siblings.counts.get(child.tagName) || 0) + 1;
              siblings.counts.set(child.tagName, position);
              siblings.positions.set(child, position);
            }
            siblingIndexesByParent.set(parent, siblings);
          }
          const suffix = siblings && siblings.counts.get(current.tagName) > 1
            ? `:nth-of-type(${siblings.positions.get(current)})`
            : "";
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
  } catch (cause) {
    throw browserRuntimeFailure({ browserId: session.id, pageId: entry.pageId, stage: "capture_page", cause });
  }
}

export function checkpointRestoreUrl(pageState) {
  const actualUrl = pageState?.url;
  if ((actualUrl === "chrome-error://chromewebdata/" || pageState?.navigation_error)
    && pageState?.requested_url) {
    return pageState.requested_url;
  }
  return actualUrl || pageState?.requested_url || "about:blank";
}

/**
 * created_at在各页面状态采集后、context.storageState读取前由Node生成；不是落盘完成时间。
 * pages[].created_at沿用原标签页创建时刻；页面与storage顺序采集，不表示同一原子时刻。
 */
export async function captureBrowserCheckpoint(session) {
  const pages = [];
  for (const entry of session.pageEntries.values()) {
    if (!entry.page.isClosed()) pages.push(await capturePageState(session, entry));
  }
  if (pages.length === 0) {
    throw new Error(`浏览器没有可写入检查点的标签页: browser_id=${session.id}`);
  }
  const createdAt = new Date().toISOString();
  let storageState;
  try {
    storageState = await session.context.storageState({ indexedDB: true });
  } catch (cause) {
    throw browserRuntimeFailure({ browserId: session.id, stage: "capture_context", cause });
  }
  // TODO: 改为流式写入临时文件并在序列化过程中执行20MiB上限，避免超大 storageState 先完整驻留内存。
  return {
    version: CHECKPOINT_VERSION,
    browser_id: session.id,
    created_at: createdAt,
    active_page_id: session.activePageId,
    storage_state: storageState,
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
  await session.acquireRuntime({
    viewport: session.record.viewport,
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: true,
    storageState: checkpoint.storage_state,
  });
  try {
    for (const pageState of checkpoint.pages) {
      const restoreUrl = checkpointRestoreUrl(pageState);
      const page = await session.createRuntimePage(pageState.page_id);
      let stage = "restore_session_storage";
      try {
        const initScript = sessionStorageInitScript(pageState.session_storage);
        if (initScript) {
          await page.addInitScript(initScript, {
            expectedOrigin: pageState.session_storage.origin,
            entries: pageState.session_storage.entries,
          });
        }
        stage = "register_page";
        const entry = await session.registerPage(page, {
          pageId: pageState.page_id,
          activate: false,
        });
        entry.createdAt = pageState.created_at;
        entry.requestedUrl = pageState.requested_url;
        stage = "restore_navigation";
        await page.goto(restoreUrl, { waitUntil: "domcontentloaded" });
        stage = "restore_controls";
        await restorePageState(page, pageState);
      } catch (cause) {
        const error = browserRuntimeFailure({ browserId: session.id, pageId: pageState.page_id, stage, cause });
        if (stage === "restore_navigation" || stage === "restore_controls") {
          // 页面URL是具名例外；第三方异常中的其它位置仍只保留在诊断中。
          error.message += `, url=${restoreUrl}`;
          error.code = "browser_checkpoint_page_restore_failed";
        }
        throw error;
      }
    }
    session.bindContextPageEvents();
    const activePageId = session.pageEntries.has(checkpoint.active_page_id)
      ? checkpoint.active_page_id
      : checkpoint.pages[0].page_id;
    await session.activatePage(activePageId);
  } catch (error) {
    try {
      await session.releaseRuntime();
    } catch (releaseError) {
      session.record.reconciliation_status = "unknown";
      throw new AggregateError(
        [error, releaseError],
        `恢复浏览器失败且运行时释放未核实: ${error.message}; ${releaseError.message}`,
      );
    }
    throw error;
  }
  return checkpoint;
}
