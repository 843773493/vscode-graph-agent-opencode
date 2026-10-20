import { nowIso } from "../url.js";

const MAX_DIAGNOSTIC_RECORDS = 200;
const MAX_NETWORK_FAILURE_TEXT_LENGTH = 512;

/**
 * 有界网络诊断缓冲区中的一行，按 Playwright Request 实例关联。
 * @typedef {object} BrowserNetworkRequestRecord
 * @property {string} id 当前标签页生命周期内唯一，清空或淘汰记录不回收编号。
 * @property {string} method HTTP 方法。
 * @property {string} url 请求 URL，不作为并发请求的身份。
 * @property {string} resource_type Playwright 资源类型。
 * @property {number|null} status 已观察到的响应状态码；尚无响应头时为 null。
 * @property {boolean} failed 是否观察到传输失败；HTTP 4xx/5xx 本身不算传输失败。
 * @property {string} started_at Node 观察到 request 事件的 ISO 时间。
 * @property {string} [finished_at] 最近一次响应头或传输失败的观察时间，不代表正文接收完成。
 * @property {string} [failure_text] 传输失败详情，最多保留 512 个字符。
 */

/**
 * 当前标签页的被动诊断记录，不参与导航或资源治理。
 * 页面事件闭包和记录随该页面一起释放，不持有 Session。
 */
export class BrowserPageDiagnostics {
  #consoleMessages = [];
  /** @type {BrowserNetworkRequestRecord[]} */
  #networkRequests = [];
  #networkRequestSequence = 0;
  #pendingNetworkRequestRecords = new WeakMap();

  /** @param {import("playwright").Page} page */
  constructor(page) {
    page.on("request", (request) => {
      const networkRecord = {
        id: `${Date.now()}_${this.#networkRequestSequence++}`,
        method: request.method(),
        url: request.url(),
        resource_type: request.resourceType(),
        status: null,
        failed: false,
        started_at: nowIso(),
      };
      this.#networkRequests.push(networkRecord);
      this.#pendingNetworkRequestRecords.set(request, networkRecord);
      if (this.#networkRequests.length > MAX_DIAGNOSTIC_RECORDS) this.#networkRequests.shift();
    });
    page.on("response", (response) => {
      const networkRecord = this.#pendingNetworkRequestRecords.get(response.request());
      if (networkRecord) {
        networkRecord.status = response.status();
        networkRecord.finished_at = nowIso();
      }
    });
    page.on("requestfinished", (request) => {
      this.#pendingNetworkRequestRecords.delete(request);
    });
    page.on("requestfailed", (request) => {
      const networkRecord = this.#pendingNetworkRequestRecords.get(request);
      if (networkRecord) {
        networkRecord.failed = true;
        networkRecord.failure_text = (request.failure()?.errorText || "请求失败")
          .slice(0, MAX_NETWORK_FAILURE_TEXT_LENGTH);
        networkRecord.finished_at = nowIso();
      }
      this.#pendingNetworkRequestRecords.delete(request);
    });
    page.on("console", (message) => {
      this.#consoleMessages.push({
        level: message.type(),
        text: message.text(),
        location: message.location(),
        occurred_at: nowIso(),
      });
      if (this.#consoleMessages.length > MAX_DIAGNOSTIC_RECORDS) this.#consoleMessages.shift();
    });
    page.on("pageerror", (error) => {
      this.#consoleMessages.push({
        level: "error",
        text: error instanceof Error ? error.message : String(error),
        location: null,
        occurred_at: nowIso(),
      });
      if (this.#consoleMessages.length > MAX_DIAGNOSTIC_RECORDS) this.#consoleMessages.shift();
    });
  }

  snapshot() {
    return {
      console: [...this.#consoleMessages],
      network: [...this.#networkRequests],
    };
  }

  clearNetworkRequests() {
    this.#networkRequests.length = 0;
    this.#pendingNetworkRequestRecords = new WeakMap();
  }
}
