import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { stripVTControlCharacters } from "node:util";
import { normalizeBrowserUrl, nowIso } from "./url.js";
import { BrowserPointerController, dispatchKey, insertText } from "./browserInput.js";
import {
  clickElement,
  dragElement,
  handleDialog,
  hoverElement,
  inspectPageElement,
  readBrowserSummary,
  runPlaywrightCode,
  screenshotPage,
  typeInPage,
} from "./browserPageActions.js";
import {
  DEFAULT_VIEWPORT,
  NAVIGATION_TIMEOUT_MS,
  TOOL_TIMEOUT_MS,
} from "./browserRuntime.js";
import {
  browserDeviceContextOptions,
  browserDeviceEmulationOptions,
  DEFAULT_BROWSER_DEVICE_ORIENTATION,
  DEFAULT_BROWSER_DEVICE_PROFILE,
  DEFAULT_BROWSER_NETWORK_PROFILE,
  getBrowserNetworkProfile,
  listBrowserDeviceProfiles,
  listBrowserNetworkProfiles,
  resolveBrowserDeviceState,
} from "./browserDeviceProfiles.js";
import { BrowserOperationQueue } from "./browserOperationQueue.js";
import {
  captureBrowserCheckpoint,
  restoreBrowserCheckpoint,
} from "./resources/browserCheckpoint.js";

const BROWSER_MODAL_DETECTION_MS = 1000;
const MAX_DEBUG_BUFFER_ENTRIES = 200;
const MAX_NETWORK_FAILURE_TEXT_LENGTH = 512;
const AGENT_LOCK_LEASE_MS = 45_000;
const SOFT_PROTECTION_LEASE_MS = 5 * 60_000;
const PROTECTION_INSPECTION_RETRY_MS = 5_000;
const STREAM_RELAX_DELAY_MS = 5_000;
const SHUTDOWN_OPERATION_DRAIN_MS = 5_000;
const STREAM_PROFILES = Object.freeze({
  interactive: Object.freeze({ quality: 65, everyNthFrame: 2 }),
  relaxed: Object.freeze({ quality: 50, everyNthFrame: 4 }),
});

function navigationFailure({ pageId, requestedUrl, actualUrl, error }) {
  const detail = stripVTControlCharacters(error instanceof Error ? error.message : String(error));
  return {
    page_id: pageId,
    requested_url: requestedUrl,
    actual_url: actualUrl,
    message: `打开 ${requestedUrl} 失败: ${detail}`,
    occurred_at: nowIso(),
  };
}

function visiblePageUrl(entry, actualUrl) {
  if (actualUrl === "chrome-error://chromewebdata/" && entry?.requestedUrl) {
    return entry.requestedUrl;
  }
  return actualUrl;
}

export class BrowserSession extends EventEmitter {
  constructor({ manager, record }) {
    super();
    this.manager = manager;
    this.record = {
      viewport: { ...DEFAULT_VIEWPORT },
      viewport_override: null,
      device_profile: DEFAULT_BROWSER_DEVICE_PROFILE,
      device_orientation: DEFAULT_BROWSER_DEVICE_ORIENTATION,
      device_scale_factor_override: null,
      user_agent_override: null,
      touch_simulation_override: null,
      network_profile_id: DEFAULT_BROWSER_NETWORK_PROFILE,
      device_presets: [],
      client_count: 0,
      sequence: 0,
      resource_state: "background",
      resource_policy: "automatic",
      ...record,
    };
    const baseDeviceState = resolveBrowserDeviceState(
      this.record.device_profile,
      this.record.device_orientation,
    );
    this.record.device_orientation = baseDeviceState.orientation;
    if (this.record.viewport_override === undefined) {
      const storedViewport = this.record.viewport;
      const isCustomViewport = storedViewport
        && (storedViewport.width !== baseDeviceState.viewport.width
          || storedViewport.height !== baseDeviceState.viewport.height);
      this.record.viewport_override = isCustomViewport ? { ...storedViewport } : null;
    }
    this.record.device_scale_factor_override = this.record.device_scale_factor_override ?? null;
    this.record.user_agent_override = this.record.user_agent_override || null;
    this.record.touch_simulation_override = this.record.touch_simulation_override ?? null;
    this.record.network_profile_id = this.record.network_profile_id || DEFAULT_BROWSER_NETWORK_PROFILE;
    this.record.device_presets = Array.isArray(this.record.device_presets)
      ? this.record.device_presets
      : [];
    this.syncDeviceStateRecord();
    this.browser = null;
    this.browserHandle = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.streaming = false;
    this.clients = new Set();
    this.pendingDialog = null;
    this.pendingFileChooser = null;
    this.refSelectors = new Map();
    this.documentRevision = Number(this.record.document_revision || 0);
    this.operationQueue = new BrowserOperationQueue({
      owner: this,
      revision: this.record.operation_revision,
    });
    this.lastFrame = null;
    this.pageEntries = new Map();
    this.activePageId = null;
    this.pageRegistrationPromises = new WeakMap();
    this.pointerController = new BrowserPointerController();
    this.baseUserAgent = null;
    this.basePlatform = null;
    this.deviceEmulationApplied = false;
    this.runtimeGeneration = null;
    this.inFlightOperations = 0;
    this.pendingOperations = 0;
    this.pendingAttachRequests = 0;
    this.activeDownloads = 0;
    this.cachedProtectionReasons = [];
    this.cachedHardProtectionReasons = [];
    this.cachedSoftProtectionReasons = [];
    this.softProtectionObservedAtMs = new Map();
    this.protectionInspectionFailureUntilMs = 0;
    this.lastNetworkActivityAtMs = Date.parse(this.record.last_network_activity_at || "") || 0;
    this.lastWebSocketActivityAtMs = Date.parse(this.record.last_websocket_activity_at || "") || 0;
    this.resourceTransition = Promise.resolve();
    this.streamProfile = null;
    this.streamTransition = Promise.resolve();
    this.streamRequestedProfile = null;
    this.streamTransitionActiveProfile = null;
    this.streamRelaxTimer = null;
    this.streamSamples = [];
    this.frameSequence = 0;
    this.attachRequestedAtMs = null;
    this.closingRequested = false;
  }

  get id() {
    return this.record.browser_id;
  }

  get sessionId() {
    return this.record.session_id;
  }

  get status() {
    return this.record.status;
  }

  deviceStateOverrides() {
    return {
      ...(this.record.viewport_override ? { viewport: this.record.viewport_override } : {}),
      ...(this.record.device_scale_factor_override !== null
        ? { deviceScaleFactor: this.record.device_scale_factor_override }
        : {}),
      ...(this.record.touch_simulation_override !== null
        ? { touchEnabled: this.record.touch_simulation_override }
        : {}),
      ...(this.record.user_agent_override
        ? { userAgent: this.record.user_agent_override }
        : {}),
    };
  }

  effectiveDeviceState() {
    return resolveBrowserDeviceState(
      this.record.device_profile,
      this.record.device_orientation,
      this.deviceStateOverrides(),
    );
  }

  syncDeviceStateRecord() {
    const state = this.effectiveDeviceState();
    this.record.viewport = { ...state.viewport };
    this.record.device_scale_factor = state.deviceScaleFactor;
    this.record.touch_simulation_enabled = state.hasTouch;
  }

  noteNetworkActivity() {
    const nowMs = Date.now();
    if (nowMs - this.lastNetworkActivityAtMs < 1_000) return;
    this.lastNetworkActivityAtMs = nowMs;
    this.record.last_network_activity_at = new Date(nowMs).toISOString();
  }

  noteWebSocketActivity() {
    const nowMs = Date.now();
    this.lastWebSocketActivityAtMs = nowMs;
    this.record.last_websocket_activity_at = new Date(nowMs).toISOString();
    this.noteNetworkActivity();
  }

  async prepareForOperation({ actor }) {
    if (this.closingRequested) {
      const error = new Error(`浏览器正在关闭，不能开始新操作: browser_id=${this.id}`);
      error.code = "browser_closing";
      throw error;
    }
    this.pendingOperations += 1;
    try {
      await this.wake({ reason: `operation:${actor}` });
    } catch (error) {
      this.pendingOperations = Math.max(0, this.pendingOperations - 1);
      throw error;
    }
  }

  beginOperation(operation) {
    this.pendingOperations = Math.max(0, this.pendingOperations - 1);
    this.inFlightOperations += 1;
    const timestamp = nowIso();
    if (String(operation.actor).startsWith("agent")) {
      this.record.last_agent_operation_at = timestamp;
    } else {
      this.record.last_user_interaction_at = timestamp;
    }
    if (!["frozen", "discarded", "restoring"].includes(this.record.resource_state)) {
      this.record.resource_state = "active";
    }
    if (operation.visible !== false || operation.interactive === true) {
      this.boostScreencast();
    }
  }

  boostScreencast() {
    if (this.streamRelaxTimer) clearTimeout(this.streamRelaxTimer);
    if (this.streaming) {
      void this.queueScreencastProfile("interactive").catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        this.record.stream_error = `切换交互流失败: ${message}`;
        console.error(`[browser-session] ${this.record.stream_error}`);
      });
    }
    this.streamRelaxTimer = setTimeout(() => {
      this.streamRelaxTimer = null;
      if (!this.streaming || this.clients.size === 0 || this.inFlightOperations > 0) return;
      void this.queueScreencastProfile("relaxed").catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        this.record.stream_error = `切换低活动流失败: ${message}`;
        console.error(`[browser-session] ${this.record.stream_error}`);
      });
    }, STREAM_RELAX_DELAY_MS);
    this.streamRelaxTimer.unref?.();
  }

  endOperation() {
    this.inFlightOperations = Math.max(0, this.inFlightOperations - 1);
    if (this.inFlightOperations === 0
      && this.clients.size === 0
      && this.record.resource_state === "active") {
      this.record.resource_state = "background";
    }
  }

  synchronousHardProtectionReasons() {
    const reasons = [];
    if (this.clients.size > 0) reasons.push("user_attached");
    if (this.pendingAttachRequests > 0) reasons.push("user_attach_pending");
    if (this.pendingOperations > 0) reasons.push("operation_pending");
    if (this.inFlightOperations > 0) reasons.push("operation_in_flight");
    if (this.pendingDialog) reasons.push("dialog_pending");
    if (this.pendingFileChooser) reasons.push("file_chooser_pending");
    if (this.activeDownloads > 0) reasons.push("download_active");
    const lockExpiresAt = Date.parse(this.record.agent_lock_expires_at || "");
    if (this.record.agent_access_locked === true
      && Number.isFinite(lockExpiresAt)
      && lockExpiresAt > Date.now()) {
      reasons.push("user_agent_lock");
    }
    return reasons;
  }

  synchronousSoftProtectionReasons() {
    const reasons = [];
    if (this.record.resource_policy === "keep_alive") reasons.push("keep_alive");
    const hasOpenWebSocket = [...this.pageEntries.values()]
      .some((entry) => (entry.webSockets?.size || 0) > 0);
    if (hasOpenWebSocket
      && Date.now() - this.lastWebSocketActivityAtMs <= SOFT_PROTECTION_LEASE_MS) {
      reasons.push("websocket_recent_activity");
    }
    return reasons;
  }

  synchronousProtectionReasons() {
    return [
      ...this.synchronousHardProtectionReasons(),
      ...this.synchronousSoftProtectionReasons(),
    ];
  }

  validCachedSoftProtectionReasons() {
    const nowMs = Date.now();
    return this.cachedSoftProtectionReasons.filter((reason) => {
      if (reason === "keep_alive") return true;
      if (reason === "websocket_recent_activity") {
        return nowMs - this.lastWebSocketActivityAtMs <= SOFT_PROTECTION_LEASE_MS;
      }
      const observedAtMs = this.softProtectionObservedAtMs.get(reason) || 0;
      return observedAtMs > 0 && nowMs - observedAtMs <= SOFT_PROTECTION_LEASE_MS;
    });
  }

  activeInspectionFailureReasons() {
    if (Date.now() >= this.protectionInspectionFailureUntilMs) return [];
    return this.cachedHardProtectionReasons.filter((reason) => (
      String(reason).startsWith("protection_inspection_failed:")
    ));
  }

  protectionEntries(hardReasons, softReasons) {
    const nowMs = Date.now();
    return [
      ...hardReasons.map((code) => ({
        code,
        class: "hard",
        observed_at: new Date(nowMs).toISOString(),
        expires_at: null,
      })),
      ...softReasons.map((code) => ({
        code,
        class: "soft",
        observed_at: new Date(
          code === "websocket_recent_activity"
            ? this.lastWebSocketActivityAtMs
            : (this.softProtectionObservedAtMs.get(code) || nowMs),
        ).toISOString(),
        expires_at: code === "keep_alive"
          ? null
          : new Date(
              (code === "websocket_recent_activity"
                ? this.lastWebSocketActivityAtMs
                : (this.softProtectionObservedAtMs.get(code) || nowMs))
              + SOFT_PROTECTION_LEASE_MS,
            ).toISOString(),
      })),
    ];
  }

  async inspectPageEntryProtectionReasons(entry) {
    const reasons = [];
    if (entry.page.isClosed()) return reasons;
    const state = await entry.page.evaluate(() => {
      const mediaElements = [...document.querySelectorAll("audio, video")];
      const playingMedia = mediaElements.some((element) => !element.paused && !element.ended);
      const liveMediaStream = mediaElements.some((element) => {
        const stream = element.srcObject;
        return stream instanceof MediaStream
          && stream.getTracks().some((track) => track.readyState === "live");
      });
      return {
        playingMedia,
        liveMediaStream,
        pictureInPicture: document.pictureInPictureElement !== null,
      };
    });
    if (state.playingMedia) reasons.push(`media_playing:${entry.pageId}`);
    if (state.liveMediaStream) reasons.push(`webrtc_media_live:${entry.pageId}`);
    if (state.pictureInPicture) reasons.push(`picture_in_picture:${entry.pageId}`);
    return reasons;
  }

  async inspectPageProtectionReasons() {
    const reasons = [];
    for (const entry of this.pageEntries.values()) {
      reasons.push(...await this.inspectPageEntryProtectionReasons(entry));
    }
    return reasons;
  }

  resourcePolicySnapshot() {
    const hardReasons = this.synchronousHardProtectionReasons();
    const softReasons = this.synchronousSoftProtectionReasons();
    if (this.record.resource_state === "frozen") {
      hardReasons.push(...this.cachedHardProtectionReasons);
      softReasons.push(...this.validCachedSoftProtectionReasons());
    } else if (["active", "background"].includes(this.record.resource_state)) {
      hardReasons.push(...this.activeInspectionFailureReasons());
    }
    const uniqueHardReasons = [...new Set(hardReasons)];
    const uniqueSoftReasons = [...new Set(softReasons)];
    return {
      browser_id: this.id,
      resource_state: this.record.resource_state,
      resource_policy: this.record.resource_policy,
      client_count: this.clients.size,
      created_at: this.record.created_at,
      last_user_interaction_at: this.record.last_user_interaction_at,
      last_agent_operation_at: this.record.last_agent_operation_at,
      last_attach_at: this.record.last_attach_at,
      frozen_at: this.record.frozen_at,
      resource_protections: this.protectionEntries(uniqueHardReasons, uniqueSoftReasons),
      resource_hard_protection_reasons: uniqueHardReasons,
      resource_soft_protection_reasons: uniqueSoftReasons,
      resource_protection_reasons: [...new Set([...uniqueHardReasons, ...uniqueSoftReasons])],
    };
  }

  async resourceSnapshot({ inspectPage = true } = {}) {
    if (!inspectPage) return this.resourcePolicySnapshot();
    const hardReasons = this.synchronousHardProtectionReasons();
    const softReasons = this.synchronousSoftProtectionReasons();
    if (["active", "background"].includes(this.record.resource_state)
      && this.browser
      && this.context) {
      try {
        const inspectedSoftReasons = await this.inspectPageProtectionReasons();
        for (const reason of [...this.softProtectionObservedAtMs.keys()]) {
          if (reason !== "keep_alive" && reason !== "websocket_recent_activity") {
            this.softProtectionObservedAtMs.delete(reason);
          }
        }
        const observedAtMs = Date.now();
        for (const reason of inspectedSoftReasons) {
          this.softProtectionObservedAtMs.set(reason, observedAtMs);
        }
        softReasons.push(...inspectedSoftReasons);
        this.protectionInspectionFailureUntilMs = 0;
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        hardReasons.push(`protection_inspection_failed:${message}`);
        this.protectionInspectionFailureUntilMs = Date.now() + PROTECTION_INSPECTION_RETRY_MS;
      }
    } else if (this.record.resource_state === "frozen") {
      hardReasons.push(...this.cachedHardProtectionReasons);
      softReasons.push(...this.validCachedSoftProtectionReasons());
    }
    this.cachedHardProtectionReasons = [...new Set(hardReasons)];
    this.cachedSoftProtectionReasons = [...new Set(softReasons)];
    this.cachedProtectionReasons = [
      ...this.cachedHardProtectionReasons,
      ...this.cachedSoftProtectionReasons,
    ];
    return {
      ...this.snapshot(),
      resource_protections: this.protectionEntries(
        this.cachedHardProtectionReasons,
        this.cachedSoftProtectionReasons,
      ),
      resource_hard_protection_reasons: this.cachedHardProtectionReasons,
      resource_soft_protection_reasons: this.cachedSoftProtectionReasons,
      resource_protection_reasons: this.cachedProtectionReasons,
    };
  }

  async runResourceTransition(callback) {
    const execution = this.resourceTransition.then(callback);
    this.resourceTransition = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  async exposeResourceTransitionFailure(error) {
    this.record.resource_transition_error = error instanceof Error ? error.message : String(error);
    this.record.updated_at = nowIso();
    await this.manager.persist();
    this.emit("state", this.snapshot());
  }

  assertNoHardProtectionDuringTransition(stage) {
    const reasons = this.synchronousHardProtectionReasons();
    if (reasons.length === 0) return;
    const error = new Error(
      `浏览器资源转换期间出现hard保护: browser_id=${this.id}, stage=${stage}, reasons=${reasons.join(",")}`,
    );
    error.code = "browser_resource_protected_during_transition";
    throw error;
  }

  async freeze({
    reason = "resource_policy",
    allowSoftProtection = false,
    allowHardProtection = false,
  } = {}) {
    return await this.runResourceTransition(async () => {
      if (this.record.resource_state === "frozen") return this.snapshot();
      this.assertRunning();
      const state = await this.resourceSnapshot();
      const blockingReasons = [
        ...(allowHardProtection ? [] : state.resource_hard_protection_reasons),
        ...(allowSoftProtection ? [] : state.resource_soft_protection_reasons),
      ];
      if (blockingReasons.length > 0) {
        const error = new Error(
          `浏览器当前不能冻结: browser_id=${this.id}, reasons=${blockingReasons.join(",")}`,
        );
        error.code = "browser_resource_protected";
        await this.exposeResourceTransitionFailure(error);
        throw error;
      }
      let checkpoint;
      let checkpointWrite;
      try {
        ({ checkpoint, checkpointWrite } = await this.manager.runCheckpointCapture(async () => {
          const captured = await captureBrowserCheckpoint(this);
          const refreshedState = await this.resourceSnapshot();
          const refreshedBlockingReasons = [
            ...(allowHardProtection ? [] : refreshedState.resource_hard_protection_reasons),
            ...(allowSoftProtection ? [] : refreshedState.resource_soft_protection_reasons),
          ];
          if (refreshedBlockingReasons.length > 0) {
            const error = new Error(
              `浏览器检查点捕获期间出现新的保护状态: browser_id=${this.id}, reasons=${refreshedBlockingReasons.join(",")}`,
            );
            error.code = "browser_resource_protected_during_transition";
            throw error;
          }
          const write = await this.manager.stateStore.writeCheckpoint(this.id, captured);
          return { checkpoint: captured, checkpointWrite: write };
        }));
      } catch (error) {
        await this.exposeResourceTransitionFailure(error);
        throw error;
      }
      this.record.checkpoint = {
        version: checkpoint.version,
        path: checkpointWrite.path,
        size_bytes: checkpointWrite.size_bytes,
        created_at: checkpoint.created_at,
        capabilities: checkpoint.capabilities,
      };
      const changedProtectionReasons = [
        ...(allowHardProtection ? [] : this.synchronousHardProtectionReasons()),
        ...(allowSoftProtection ? [] : this.synchronousSoftProtectionReasons()),
      ];
      if (changedProtectionReasons.length > 0) {
        const error = new Error(
          `浏览器冻结准备期间出现新的保护状态: browser_id=${this.id}, reasons=${changedProtectionReasons.join(",")}`,
        );
        error.code = "browser_resource_protected_during_transition";
        await this.exposeResourceTransitionFailure(error);
        throw error;
      }
      this.record.resource_state = "freezing";
      this.record.resource_transition_reason = reason;
      this.emit("state", this.snapshot());
      const frozenEntries = [];
      try {
        if (this.streaming) await this.stopScreencast();
        for (const entry of this.pageEntries.values()) {
          if (!allowHardProtection) {
            this.assertNoHardProtectionDuringTransition("before_suspend_rendering");
          }
          if (!allowSoftProtection) {
            const entrySoftReasons = await this.inspectPageEntryProtectionReasons(entry);
            if (entrySoftReasons.length > 0) {
              const error = new Error(
                `浏览器冻结期间出现新的soft保护: browser_id=${this.id}, reasons=${entrySoftReasons.join(",")}`,
              );
              error.code = "browser_resource_protected_during_transition";
              throw error;
            }
          }
          frozenEntries.push(entry);
          await this.suspendPageRendering(entry);
          if (!allowHardProtection) {
            this.assertNoHardProtectionDuringTransition("before_lifecycle_freeze");
          }
          await entry.cdpSession.send("Page.setWebLifecycleState", { state: "frozen" });
          if (!allowHardProtection) {
            this.assertNoHardProtectionDuringTransition("before_disable_script");
          }
          await entry.cdpSession.send("Emulation.setScriptExecutionDisabled", { value: true });
        }
      } catch (error) {
        await Promise.allSettled(
          frozenEntries.map((entry) => this.resumePageRendering(entry)),
        );
        this.record.resource_state = this.clients.size > 0 ? "active" : "background";
        this.record.resource_transition_error = error instanceof Error ? error.message : String(error);
        await this.manager.persist();
        this.emit("state", this.snapshot());
        throw error;
      }
      this.record.resource_state = "frozen";
      this.record.frozen_at = nowIso();
      this.record.resource_transition_error = null;
      this.record.updated_at = this.record.frozen_at;
      await this.manager.persist();
      this.emit("state", this.snapshot());
      return this.snapshot();
    });
  }

  async wake({ reason = "resource_access" } = {}) {
    return await this.runResourceTransition(async () => {
      if (!["frozen", "discarded"].includes(this.record.resource_state)) return this.snapshot();
      const previousState = this.record.resource_state;
      const previousStatus = this.record.status;
      if (previousState === "frozen") this.assertRunning();
      if (previousState === "discarded" && !["running", "lost"].includes(this.record.status)) {
        throw new Error(`已冷回收的浏览器状态非法: browser_id=${this.id}, status=${this.record.status}`);
      }
      this.record.resource_state = "restoring";
      this.record.resource_transition_reason = reason;
      this.emit("state", this.snapshot());
      try {
        if (previousState === "discarded") {
          const checkpoint = await this.manager.stateStore.readCheckpoint(this.id);
          if (!checkpoint) {
            throw new Error(`浏览器检查点不存在: browser_id=${this.id}`);
          }
          await restoreBrowserCheckpoint(this, checkpoint);
          this.record.discarded_pages = null;
          this.record.runtime_generation = this.runtimeGeneration;
        } else {
          for (const entry of this.pageEntries.values()) {
            await this.resumePageRendering(entry);
            entry.lastFrame = null;
          }
        }
      } catch (error) {
        this.record.status = previousStatus;
        this.record.resource_state = previousState;
        this.record.resource_transition_error = error instanceof Error ? error.message : String(error);
        await this.manager.persist();
        this.emit("state", this.snapshot());
        throw error;
      }
      this.lastFrame = null;
      this.record.status = "running";
      this.record.ended_at = null;
      this.record.error_message = null;
      this.cachedProtectionReasons = [];
      this.cachedHardProtectionReasons = [];
      this.cachedSoftProtectionReasons = [];
      this.softProtectionObservedAtMs.clear();
      this.record.resource_state = this.clients.size > 0 || this.inFlightOperations > 0
        ? "active"
        : "background";
      this.record.last_wake_at = nowIso();
      this.record.resource_transition_error = null;
      this.record.updated_at = this.record.last_wake_at;
      await this.manager.persist();
      this.emit("state", this.snapshot());
      return this.snapshot();
    });
  }

  async discard({
    reason = "resource_policy",
    allowSoftProtection = false,
    allowHardProtection = false,
  } = {}) {
    return await this.runResourceTransition(async () => {
      if (this.record.resource_state === "discarded") return this.snapshot();
      if (this.record.resource_state !== "frozen") {
        const error = new Error(`浏览器必须先冻结才能冷回收: browser_id=${this.id}, state=${this.record.resource_state}`);
        error.code = "browser_must_be_frozen_before_discard";
        throw error;
      }
      const reasons = [
        ...(allowHardProtection ? [] : this.synchronousHardProtectionReasons()),
        ...(allowHardProtection ? [] : this.cachedHardProtectionReasons),
        ...(allowSoftProtection
          ? []
          : [...this.synchronousSoftProtectionReasons(), ...this.validCachedSoftProtectionReasons()]),
      ];
      if (reasons.length > 0) {
        const error = new Error(`浏览器当前不能冷回收: browser_id=${this.id}, reasons=${reasons.join(",")}`);
        error.code = "browser_resource_protected";
        await this.exposeResourceTransitionFailure(error);
        throw error;
      }
      const checkpoint = await this.manager.stateStore.readCheckpoint(this.id);
      if (!checkpoint) {
        const error = new Error(`浏览器冷回收检查点不存在: browser_id=${this.id}`);
        error.code = "browser_checkpoint_unavailable";
        throw error;
      }
      const changedProtectionReasons = [
        ...(allowHardProtection ? [] : this.synchronousHardProtectionReasons()),
        ...(allowHardProtection ? [] : this.cachedHardProtectionReasons),
        ...(allowSoftProtection
          ? []
          : [...this.synchronousSoftProtectionReasons(), ...this.validCachedSoftProtectionReasons()]),
      ];
      if (changedProtectionReasons.length > 0) {
        const error = new Error(
          `浏览器冷回收准备期间出现新的保护状态: browser_id=${this.id}, reasons=${changedProtectionReasons.join(",")}`,
        );
        error.code = "browser_resource_protected_during_transition";
        await this.exposeResourceTransitionFailure(error);
        throw error;
      }
      this.record.resource_state = "discarding";
      this.record.resource_transition_reason = reason;
      this.emit("state", this.snapshot());
      await this.releaseRuntime();
      this.record.resource_state = "discarded";
      this.record.discarded_at = nowIso();
      this.record.discarded_pages = checkpoint.pages.map((page) => ({
        page_id: page.page_id,
        title: page.title || "无标题",
        url: page.requested_url || page.url,
        actual_url: page.url,
        navigation_error: null,
        active: page.page_id === checkpoint.active_page_id,
        created_at: page.created_at,
      }));
      this.record.resource_transition_error = null;
      this.record.updated_at = this.record.discarded_at;
      this.cachedProtectionReasons = [];
      this.cachedHardProtectionReasons = [];
      this.cachedSoftProtectionReasons = [];
      this.softProtectionObservedAtMs.clear();
      await this.manager.persist();
      this.emit("state", this.snapshot());
      return this.snapshot();
    });
  }

  async checkpointForManagerShutdown(
    reason,
    { operationDrainTimeoutMs = SHUTDOWN_OPERATION_DRAIN_MS } = {},
  ) {
    this.closingRequested = true;
    const deadline = Date.now() + operationDrainTimeoutMs;
    let drainTimer = null;
    const queuedOperationsDrained = await Promise.race([
      this.operationQueue.tail.then(() => true),
      new Promise((resolve) => {
        drainTimer = setTimeout(resolve, operationDrainTimeoutMs, false);
      }),
    ]);
    if (drainTimer !== null) {
      clearTimeout(drainTimer);
    }
    if (!queuedOperationsDrained) {
      const error = new Error(
        `浏览器关闭前队列未能排空: browser_id=${this.id}, pending=${this.pendingOperations}, in_flight=${this.inFlightOperations}`,
      );
      error.code = "browser_shutdown_operation_drain_timeout";
      throw error;
    }
    while (this.inFlightOperations > 0 && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    if (this.inFlightOperations > 0) {
      const error = new Error(
        `浏览器关闭前操作未能排空: browser_id=${this.id}, in_flight=${this.inFlightOperations}`,
      );
      error.code = "browser_shutdown_operation_drain_timeout";
      throw error;
    }
    this.clients.clear();
    this.pendingAttachRequests = 0;
    this.pendingDialog = null;
    this.pendingFileChooser = null;
    this.record.agent_access_locked = false;
    this.record.agent_lock_owner_id = null;
    this.record.agent_lock_expires_at = null;
    if (this.record.resource_state !== "frozen") {
      await this.freeze({
        reason,
        allowSoftProtection: true,
        allowHardProtection: true,
      });
    }
    await this.discard({
      reason,
      allowSoftProtection: true,
      allowHardProtection: true,
    });
    this.record.release_reason = reason;
    this.record.updated_at = nowIso();
    await this.manager.persist();
    return this.snapshot();
  }

  async markManagerShutdownCheckpointFailed(reason, error) {
    this.closingRequested = true;
    this.record.status = "lost";
    this.record.resource_state = "lost";
    this.record.release_reason = `${reason}_checkpoint_failed`;
    this.record.error_message = error instanceof Error ? error.message : String(error);
    this.record.ended_at = nowIso();
    this.record.updated_at = this.record.ended_at;
    await this.releaseRuntime();
    this.clients.clear();
    await this.manager.stateStore.deleteCheckpoint(this.id);
    this.record.checkpoint = null;
    this.record.discarded_pages = null;
    await this.manager.persist();
    this.emit("state", this.snapshot());
    return this.snapshot();
  }

  async suspendPageRendering(entry) {
    await entry.cdpSession.send("DOM.enable");
    await entry.cdpSession.send("CSS.enable");
    if (!entry.freezeStyleSheetId) {
      const frameTree = await entry.cdpSession.send("Page.getFrameTree");
      const created = await entry.cdpSession.send("CSS.createStyleSheet", {
        frameId: frameTree.frameTree.frame.id,
      });
      entry.freezeStyleSheetId = created.styleSheetId;
    }
    await entry.cdpSession.send("CSS.setStyleSheetText", {
      styleSheetId: entry.freezeStyleSheetId,
      text: "*,*::before,*::after{animation-play-state:paused!important;transition:none!important;caret-color:transparent!important}",
    });
  }

  async resumePageRendering(entry) {
    await entry.cdpSession.send("Emulation.setScriptExecutionDisabled", { value: false });
    await entry.cdpSession.send("Page.setWebLifecycleState", { state: "active" });
    if (entry.freezeStyleSheetId) {
      await entry.cdpSession.send("CSS.setStyleSheetText", {
        styleSheetId: entry.freezeStyleSheetId,
        text: "",
      });
      entry.freezeStyleSheetId = null;
    }
  }

  async handleRuntimeDisconnect(generation) {
    if (generation !== this.runtimeGeneration
      || this.record.status !== "running"
      || this.record.resource_state === "discarded"
      || !this.context) return;
    this.browser = null;
    this.browserHandle = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.deviceEmulationApplied = false;
    this.pageEntries.clear();
    this.activePageId = null;
    this.record.status = "lost";
    this.record.resource_state = "lost";
    this.record.release_reason = "shared_browser_runtime_disconnected";
    this.record.error_message = `共享 Chromium 运行时意外断开: generation=${generation}`;
    this.record.ended_at = nowIso();
    this.record.updated_at = this.record.ended_at;
    await this.manager.persist();
    this.emit("state", this.snapshot());
  }

  async start() {
    if (this.browser) {
      return this.snapshot();
    }
    try {
      const deviceOptions = browserDeviceContextOptions(
        this.record.device_profile,
        this.record.device_orientation,
        this.record.viewport_override,
        this.deviceStateOverrides(),
      );
      const runtime = await this.manager.runtimePool.acquireContext({
        ...deviceOptions,
        ignoreHTTPSErrors: true,
      });
      this.assignRuntime(runtime);
      const initialPage = await this.context.newPage();
      await this.registerPage(initialPage, { pageId: this.id, activate: true });
      if (!deviceOptions.isMobile) {
        const browserDefaults = await initialPage.evaluate(() => ({
          userAgent: navigator.userAgent,
          platform: navigator.platform,
        }));
        this.baseUserAgent = browserDefaults.userAgent;
        this.basePlatform = browserDefaults.platform;
      }
      this.deviceEmulationApplied = true;
      await this.applyDeviceEmulation(this.pageEntries.get(this.id));
      this.bindContextPageEvents();
      this.record.status = "running";
      this.record.resource_state = "background";
      this.record.runtime_generation = this.runtimeGeneration;
      this.record.started_at = this.record.started_at || nowIso();
      await this.goto(this.record.url || "about:blank");
      return this.snapshot();
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      await this.failStartup(message);
      throw error;
    }
  }

  assignRuntime(runtime) {
    this.browser = runtime.browser;
    this.browserHandle = runtime.browserHandle;
    this.context = runtime.context;
    this.runtimeGeneration = runtime.runtimeGeneration;
  }

  bindContextPageEvents() {
    if (!this.context) throw new Error(`浏览器 context 尚未建立: browser_id=${this.id}`);
    this.context.on("page", (page) => {
      if ([...this.pageEntries.values()].some((entry) => entry.page === page)) {
        return;
      }
      const registration = this.registerPage(page, { activate: true });
      this.pageRegistrationPromises.set(page, registration);
      void registration.catch((error) => {
        const message = error instanceof Error ? (error.stack || error.message) : String(error);
        this.record.error_message = `注册新标签页失败: ${message}`;
        this.emit("state", this.snapshot());
        console.error("[browser-session] 注册新标签页失败:", message);
      });
    });
  }

  async releaseRuntime() {
    const context = this.context;
    this.browser = null;
    this.browserHandle = null;
    this.context = null;
    this.page = null;
    this.cdpSession = null;
    this.deviceEmulationApplied = false;
    this.streaming = false;
    this.pageEntries.clear();
    this.activePageId = null;
    this.lastFrame = null;
    if (this.streamRelaxTimer) clearTimeout(this.streamRelaxTimer);
    this.streamRelaxTimer = null;
    this.streamProfile = null;
    this.streamSamples = [];
    await this.pointerController.reset();
    if (context) await this.manager.runtimePool.releaseContext(context);
  }

  async registerPage(page, { pageId = `page_${randomUUID().replaceAll("-", "")}`, activate = true } = {}) {
    const existing = [...this.pageEntries.values()].find((entry) => entry.page === page);
    if (existing) {
      if (activate) {
        await this.activatePage(existing.pageId);
      }
      return existing;
    }
    const cdpSession = await this.context.newCDPSession(page);
    await cdpSession.send("Page.enable");
    await cdpSession.send("Runtime.enable");
    await cdpSession.send("Network.enable");
    const entry = {
      pageId,
      page,
      cdpSession,
      documentRevision: 0,
      refSelectors: new Map(),
      lastFrame: null,
      streaming: false,
      title: "",
      url: page.url(),
      actualUrl: page.url(),
      requestedUrl: page.url(),
      navigationError: null,
      createdAt: nowIso(),
      webSockets: new Set(),
      freezeStyleSheetId: null,
      consoleMessages: [],
      networkRequests: [],
    };
    if (this.deviceEmulationApplied) {
      await this.applyDeviceEmulation(entry);
    }
    this.pageEntries.set(pageId, entry);
    cdpSession.on("Page.screencastFrame", (event) => {
      void this.handleScreencastFrame(event, entry);
    });
    cdpSession.on("Network.webSocketCreated", ({ requestId }) => {
      entry.webSockets.add(requestId);
      this.noteWebSocketActivity();
    });
    cdpSession.on("Network.webSocketClosed", ({ requestId }) => {
      entry.webSockets.delete(requestId);
      this.noteWebSocketActivity();
    });
    cdpSession.on("Network.webSocketFrameReceived", () => this.noteWebSocketActivity());
    cdpSession.on("Network.webSocketFrameSent", () => this.noteWebSocketActivity());
    page.on("framenavigated", (frame) => {
      if (frame !== page.mainFrame()) {
        return;
      }
      entry.documentRevision += 1;
      entry.refSelectors.clear();
      if (this.activePageId === pageId) {
        this.documentRevision = entry.documentRevision;
        this.record.document_revision = this.documentRevision;
        void this.syncAndEmitState({ persist: false }).then(() => this.manager.schedulePersist());
      }
    });
    page.on("request", (request) => {
      this.noteNetworkActivity();
      entry.networkRequests.push({
        id: `${Date.now()}_${entry.networkRequests.length}`,
        method: request.method(),
        url: request.url(),
        resource_type: request.resourceType(),
        status: null,
        failed: false,
        started_at: nowIso(),
      });
      if (entry.networkRequests.length > MAX_DEBUG_BUFFER_ENTRIES) entry.networkRequests.shift();
      if (!request.isNavigationRequest() || request.frame() !== page.mainFrame()) {
        return;
      }
      entry.requestedUrl = request.url();
      entry.navigationError = null;
    });
    page.on("response", (response) => {
      const request = response.request();
      const matchingRequest = [...entry.networkRequests].reverse().find((item) => item.url === request.url()
        && item.status === null);
      if (matchingRequest) {
        matchingRequest.status = response.status();
        matchingRequest.finished_at = nowIso();
      }
    });
    page.on("requestfailed", (request) => {
      const matchingRequest = [...entry.networkRequests].reverse().find((item) => item.url === request.url()
        && item.status === null);
      if (matchingRequest) {
        matchingRequest.failed = true;
        matchingRequest.failure_text = (request.failure()?.errorText || "请求失败")
          .slice(0, MAX_NETWORK_FAILURE_TEXT_LENGTH);
        matchingRequest.finished_at = nowIso();
      }
    });
    page.on("requestfailed", (request) => {
      if (!request.isNavigationRequest() || request.frame() !== page.mainFrame()) {
        return;
      }
      const requestedUrl = request.url() || entry.requestedUrl || page.url();
      const errorText = request.failure()?.errorText || "未知网络错误";
      entry.requestedUrl = requestedUrl;
      entry.navigationError = navigationFailure({
        pageId,
        requestedUrl,
        actualUrl: page.url(),
        error: errorText,
      });
      console.error(
        `[browser-session] 主文档导航失败: browser_id=${this.id} page_id=${pageId} url=${requestedUrl} error=${errorText}`,
      );
      if (this.activePageId === pageId) {
        void this.syncAndEmitState({ persist: false }).then(() => this.manager.schedulePersist());
      }
    });
    page.on("console", (message) => {
      entry.consoleMessages.push({
        level: message.type(),
        text: message.text(),
        location: message.location(),
        occurred_at: nowIso(),
      });
      if (entry.consoleMessages.length > MAX_DEBUG_BUFFER_ENTRIES) entry.consoleMessages.shift();
    });
    page.on("pageerror", (error) => {
      entry.consoleMessages.push({
        level: "error",
        text: error instanceof Error ? error.message : String(error),
        location: null,
        occurred_at: nowIso(),
      });
      if (entry.consoleMessages.length > MAX_DEBUG_BUFFER_ENTRIES) entry.consoleMessages.shift();
    });
    page.on("domcontentloaded", () => {
      if (this.activePageId === pageId) {
        void this.syncAndEmitState({ persist: false }).then(() => this.manager.schedulePersist());
      }
    });
    page.on("load", () => {
      if (this.activePageId === pageId) {
        void this.syncAndEmitState({ persist: false }).then(() => this.manager.schedulePersist());
      }
    });
    page.on("dialog", (dialog) => {
      this.pendingDialog = {
        type: dialog.type(),
        message: dialog.message(),
        defaultValue: dialog.defaultValue(),
        pageId,
        dialog,
      };
      this.emit("browser-modal", { kind: "dialog" });
      void this.activatePage(pageId).catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        this.record.resource_transition_error = `激活浏览器对话框标签页失败: page_id=${pageId}, error=${message}`;
        console.error(`[browser-session] ${this.record.resource_transition_error}`);
        return this.manager.persist();
      });
    });
    page.on("filechooser", (fileChooser) => {
      this.pendingFileChooser = fileChooser;
      this.emit("browser-modal", { kind: "filechooser" });
      void this.activatePage(pageId).catch((error) => {
        const message = error instanceof Error ? error.message : String(error);
        this.record.resource_transition_error = `激活文件选择器标签页失败: page_id=${pageId}, error=${message}`;
        console.error(`[browser-session] ${this.record.resource_transition_error}`);
        return this.manager.persist();
      });
    });
    page.on("download", (download) => {
      void this.handleDownload(download);
    });
    page.on("close", () => {
      void this.handlePageClosed(pageId).catch((error) => {
        const message = error instanceof Error ? (error.stack || error.message) : String(error);
        this.record.resource_transition_error = `处理标签页关闭事件失败: page_id=${pageId}, error=${message}`;
        console.error(`[browser-session] ${this.record.resource_transition_error}`);
        return this.manager.persist();
      });
    });
    if (activate) {
      await this.activatePage(pageId);
    }
    return entry;
  }

  async activatePage(pageId) {
    const entry = this.pageEntries.get(pageId);
    if (!entry) {
      throw new Error(`浏览器标签页不存在: ${pageId}`);
    }
    if (this.activePageId === pageId) {
      return this.snapshot();
    }
    if (this.streaming && this.cdpSession) {
      await this.stopScreencast();
    }
    await this.pointerController.reset();
    this.activePageId = pageId;
    this.page = entry.page;
    this.cdpSession = entry.cdpSession;
    this.refSelectors = entry.refSelectors;
    this.documentRevision = entry.documentRevision;
    this.lastFrame = entry.lastFrame;
    this.record.page_id = pageId;
    this.record.document_revision = this.documentRevision;
    if (this.clients.size > 0 && this.record.status === "running") {
      await this.startScreencast();
    }
    if (this.record.status === "running") {
      const state = await this.syncAndEmitState();
      if (this.clients.size > 0) {
        await this.captureCurrentPageFrame(entry);
      }
      return state;
    }
    return this.snapshot();
  }

  async handlePageClosed(pageId) {
    const entry = this.pageEntries.get(pageId);
    if (!entry) {
      return;
    }
    this.pageEntries.delete(pageId);
    if (this.closingRequested) {
      return;
    }
    if (this.activePageId !== pageId) {
      this.emit("state", this.snapshot());
      return;
    }
    this.streaming = false;
    entry.streaming = false;
    const replacement = [...this.pageEntries.keys()].at(-1);
    if (replacement) {
      this.activePageId = null;
      await this.activatePage(replacement);
      return;
    }
    if (this.record.status === "running" || this.record.status === "created") {
      this.record.status = "closed";
      this.record.ended_at = this.record.ended_at || nowIso();
      this.record.updated_at = nowIso();
      await this.manager.persist();
      this.emit("state", this.snapshot());
    }
  }

  async createPage(url = "about:blank") {
    this.assertRunning();
    const page = await this.context.newPage();
    const registration = this.pageRegistrationPromises.get(page);
    const entry = registration
      ? await registration
      : await this.registerPage(page, { activate: true });
    if (url && url !== "about:blank") {
      await this.goto(url);
    }
    return { ...(await this.syncAndEmitState()), created_page_id: entry.pageId };
  }

  async closePage(pageId) {
    this.assertRunning();
    const entry = this.pageEntries.get(pageId);
    if (!entry) {
      throw new Error(`浏览器标签页不存在: ${pageId}`);
    }
    await entry.page.close();
    return this.snapshot();
  }

  async handleDownload(download) {
    this.activeDownloads += 1;
    try {
      const record = await this.manager.writeDownload(this.id, download);
      this.record.downloads = [record, ...(this.record.downloads || [])].slice(0, 50);
      this.record.updated_at = nowIso();
      await this.manager.persist();
      this.emit("state", this.snapshot());
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.record.download_error = message;
      this.record.updated_at = nowIso();
      await this.manager.persist();
      this.emit("state", this.snapshot());
      console.error("[browser-session] 保存下载失败:", message);
    } finally {
      this.activeDownloads = Math.max(0, this.activeDownloads - 1);
    }
  }

  download(downloadId) {
    const record = (this.record.downloads || []).find((item) => item.download_id === downloadId);
    if (!record) {
      throw new Error(`浏览器下载不存在: ${downloadId}`);
    }
    return record;
  }

  assertRunning() {
    if (!this.browser || !this.context || !this.page || !this.cdpSession) {
      throw new Error(`浏览器页面尚未启动: ${this.id}`);
    }
    if (this.record.status !== "running") {
      throw new Error(`浏览器页面当前不可操作: browser_id=${this.id}, status=${this.record.status}`);
    }
  }

  snapshot() {
    const lockExpiresAt = Date.parse(this.record.agent_lock_expires_at || "");
    const agentLockActive = this.record.agent_access_locked === true
      && Number.isFinite(lockExpiresAt)
      && lockExpiresAt > Date.now();
    const frozen = this.record.resource_state === "frozen";
    const hardProtectionReasons = [...new Set([
      ...(frozen ? this.cachedHardProtectionReasons : this.activeInspectionFailureReasons()),
      ...this.synchronousHardProtectionReasons(),
    ])];
    const softProtectionReasons = [...new Set([
      ...(frozen ? this.validCachedSoftProtectionReasons() : []),
      ...this.synchronousSoftProtectionReasons(),
    ])];
    const deviceState = this.effectiveDeviceState();
    return {
      ...this.record,
      device_emulation: {
        profile_id: deviceState.id,
        label: deviceState.label,
        orientation: deviceState.orientation,
        angle: deviceState.angle,
        viewport: { ...deviceState.viewport },
        base_viewport: { ...deviceState.baseViewport },
        viewport_override: this.record.viewport_override
          ? { ...this.record.viewport_override }
          : null,
        pixel_ratio: deviceState.deviceScaleFactor,
        base_pixel_ratio: deviceState.baseDeviceScaleFactor,
        touch_simulation: deviceState.hasTouch,
        touch_simulation_override: this.record.touch_simulation_override,
        user_agent: deviceState.userAgent,
        user_agent_override: this.record.user_agent_override,
        network_profile_id: this.record.network_profile_id,
      },
      device_profiles: listBrowserDeviceProfiles(),
      network_profiles: listBrowserNetworkProfiles(),
      device_presets: this.record.device_presets,
      agent_access_locked: agentLockActive,
      agent_lock_owner_id: agentLockActive ? this.record.agent_lock_owner_id : null,
      agent_lock_expires_at: agentLockActive ? this.record.agent_lock_expires_at : null,
      attach_url: this.manager.attachUrl(this.id),
      client_count: this.clients.size,
      document_revision: this.documentRevision,
      operation_revision: this.operationQueue.revision,
      resource_state: this.record.resource_state,
      resource_policy: this.record.resource_policy,
      resource_hard_protection_reasons: hardProtectionReasons,
      resource_soft_protection_reasons: softProtectionReasons,
      resource_protections: this.protectionEntries(hardProtectionReasons, softProtectionReasons),
      resource_protection_reasons: [...new Set([...hardProtectionReasons, ...softProtectionReasons])],
      active_operation_count: this.inFlightOperations,
      stream_metrics: this.streamMetrics(),
      runtime_generation: this.runtimeGeneration ?? this.record.runtime_generation ?? null,
      active_page_id: this.activePageId || this.record.discarded_pages?.find((page) => page.active)?.page_id || null,
      pages: this.record.resource_state === "discarded"
        ? (this.record.discarded_pages || [])
        : [...this.pageEntries.values()].map((entry) => ({
        page_id: entry.pageId,
        title: entry.title || "无标题",
        url: entry.url || "about:blank",
        actual_url: entry.actualUrl || entry.url || "about:blank",
        navigation_error: entry.navigationError,
        active: entry.pageId === this.activePageId,
        created_at: entry.createdAt,
          })),
      participants: [...this.clients].map((client) => ({
        participant_id: client.participantId || "unknown_user",
        kind: client.kind || "user",
        connected_at: client.connectedAt || null,
      })),
      pending_dialog: this.pendingDialog
        ? {
            type: this.pendingDialog.type,
            message: this.pendingDialog.message,
            defaultValue: this.pendingDialog.defaultValue,
          }
        : null,
      pending_file_chooser: this.pendingFileChooser ? true : false,
    };
  }

  assertAgentAccessAllowed() {
    const lockExpiresAt = Date.parse(this.record.agent_lock_expires_at || "");
    if (this.record.agent_access_locked === true
      && (!Number.isFinite(lockExpiresAt) || lockExpiresAt <= Date.now())) {
      this.record.agent_access_locked = false;
      this.record.agent_lock_owner_id = null;
      this.record.agent_lock_expires_at = null;
      this.record.updated_at = nowIso();
      void this.manager.persist();
      this.emit("state", this.snapshot());
    }
    if (this.record.agent_access_locked === true) {
      const error = new Error(`用户锁定了浏览器，你暂时不能操作这个页面: browser_id=${this.id}`);
      error.code = "browser_agent_access_locked";
      throw error;
    }
  }

  async setAgentAccessLocked(locked, ownerId) {
    if (typeof locked !== "boolean") {
      throw new Error("agent lock 的 locked 必须是 boolean");
    }
    if (typeof ownerId !== "string" || !/^user_[a-zA-Z0-9_-]{8,80}$/.test(ownerId)) {
      throw new Error("agent lock 的 owner_id 格式非法");
    }
    this.assertAgentAccessAllowedForOwner(ownerId);
    this.record.agent_access_locked = locked;
    this.record.agent_lock_owner_id = locked ? ownerId : null;
    this.record.agent_lock_expires_at = locked
      ? new Date(Date.now() + AGENT_LOCK_LEASE_MS).toISOString()
      : null;
    this.record.agent_lock_updated_at = nowIso();
    this.record.updated_at = this.record.agent_lock_updated_at;
    await this.manager.persist();
    const state = this.snapshot();
    this.emit("state", state);
    return state;
  }

  async setResourcePolicy(policy) {
    if (!["automatic", "keep_alive"].includes(policy)) {
      throw new Error(`未知浏览器资源策略: ${policy}`);
    }
    this.record.resource_policy = policy;
    this.record.updated_at = nowIso();
    await this.manager.persist();
    const state = this.snapshot();
    this.emit("state", state);
    return state;
  }

  assertAgentAccessAllowedForOwner(ownerId) {
    if (this.record.agent_access_locked !== true) {
      return;
    }
    const lockExpiresAt = Date.parse(this.record.agent_lock_expires_at || "");
    if (!Number.isFinite(lockExpiresAt) || lockExpiresAt <= Date.now()) {
      return;
    }
    if (this.record.agent_lock_owner_id !== ownerId) {
      const error = new Error("另一位用户锁定了 AI 操作；只有锁所有者可以续期或解锁。");
      error.code = "browser_agent_lock_owned_by_another_user";
      throw error;
    }
  }

  async enqueueOperation({ actor, action, visible = true }, callback) {
    return await this.operationQueue.enqueue({ actor, action, visible }, callback);
  }

  async runInteractiveOperation({ actor, action }, callback) {
    return await this.operationQueue.runConcurrent(
      { actor, action, visible: false, interactive: true },
      callback,
    );
  }

  async syncPageState({ persist = true } = {}) {
    this.assertRunning();
    const activeEntry = this.pageEntries.get(this.activePageId);
    const actualUrl = this.page.url();
    this.record.url = visiblePageUrl(activeEntry, actualUrl);
    this.record.actual_url = actualUrl;
    this.record.navigation_error = activeEntry?.navigationError || null;
    this.record.title = await this.page.title();
    if (activeEntry) {
      activeEntry.url = this.record.url;
      activeEntry.actualUrl = actualUrl;
      activeEntry.title = this.record.title;
      activeEntry.documentRevision = this.documentRevision;
    }
    this.record.client_count = this.clients.size;
    this.record.updated_at = nowIso();
    this.record.sequence = Number(this.record.sequence || 0) + 1;
    if (persist) {
      await this.manager.persist();
    }
    return this.snapshot();
  }

  async syncAndEmitState(options = {}) {
    const state = await this.syncPageState(options);
    this.emit("state", state);
    return state;
  }

  async goto(rawUrl) {
    this.assertRunning();
    const url = normalizeBrowserUrl(rawUrl);
    const activeEntry = this.pageEntries.get(this.activePageId);
    if (activeEntry) {
      activeEntry.requestedUrl = url;
      activeEntry.navigationError = null;
    }
    this.record.url = url;
    this.record.navigation_error = null;
    try {
      if (url === "about:blank") {
        await this.page.goto(url);
      } else {
        await this.page.goto(url, {
          waitUntil: "domcontentloaded",
          timeout: NAVIGATION_TIMEOUT_MS,
        });
      }
    } catch (error) {
      const failure = navigationFailure({
        pageId: this.activePageId,
        requestedUrl: url,
        actualUrl: this.page.url(),
        error,
      });
      if (activeEntry) {
        activeEntry.navigationError = failure;
      }
      this.record.navigation_error = failure;
      console.error(
        `[browser-session] 导航命令失败: browser_id=${this.id} page_id=${this.activePageId} url=${url} error=${failure.message}`,
      );
      await this.syncAndEmitState();
      if (this.clients.size > 0 && activeEntry) {
        await this.captureCurrentPageFrame(activeEntry);
      }
      throw error;
    }
    if (activeEntry) {
      activeEntry.navigationError = null;
    }
    const state = await this.syncAndEmitState();
    if (this.clients.size > 0 && activeEntry) {
      await this.captureCurrentPageFrame(activeEntry);
    }
    return state;
  }

  async navigate(type = "url", url = null) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(async () => {
      if (type === "reload") {
        await this.page.reload({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "back") {
        await this.page.goBack({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "forward") {
        await this.page.goForward({ waitUntil: "domcontentloaded", timeout: NAVIGATION_TIMEOUT_MS });
      } else if (type === "url") {
        if (!url) {
          throw new Error("navigate type=url 需要 url");
        }
        await this.goto(url);
      } else {
        throw new Error(`未知 navigate type: ${type}`);
      }
    });
    return await this.syncAndEmitState();
  }

  async stopLoading() {
    this.assertRunning();
    await this.cdpSession.send("Page.stopLoading");
    return await this.syncAndEmitState();
  }

  async close({ status = "closed", reason = "browser_closed" } = {}) {
    this.closingRequested = true;
    return await this.runResourceTransition(async () => {
      if (this.record.status === "deleted") return this.snapshot();
      if (this.streaming && this.browser && this.cdpSession && this.record.status === "running") {
        await this.stopScreencast();
      }
      this.record.status = status;
      this.record.release_reason = reason;
      this.record.ended_at = this.record.ended_at || nowIso();
      this.record.updated_at = nowIso();
      await this.releaseRuntime();
      this.clients.clear();
      await this.manager.stateStore.deleteCheckpoint(this.id);
      this.record.checkpoint = null;
      this.record.discarded_pages = null;
      await this.manager.persist();
      return this.snapshot();
    });
  }

  async failStartup(message) {
    this.closingRequested = true;
    return await this.runResourceTransition(async () => {
      this.record.status = "failed";
      this.record.release_reason = "browser_initial_navigation_failed";
      this.record.error_message = message;
      this.record.ended_at = this.record.ended_at || nowIso();
      this.record.updated_at = nowIso();
      await this.releaseRuntime();
      this.clients.clear();
      await this.manager.stateStore.deleteCheckpoint(this.id);
      await this.manager.persist();
      this.emit("state", this.snapshot());
      return this.snapshot();
    });
  }

  async attachClient(client) {
    if (this.closingRequested) {
      const error = new Error(`浏览器正在关闭，不能 attach: browser_id=${this.id}`);
      error.code = "browser_closing";
      throw error;
    }
    this.attachRequestedAtMs = Date.now();
    this.pendingAttachRequests += 1;
    try {
      await this.wake({ reason: "user_attach" });
      this.assertRunning();
      this.clients.add(client);
    } finally {
      this.pendingAttachRequests = Math.max(0, this.pendingAttachRequests - 1);
    }
    this.record.resource_state = "active";
    this.record.last_attach_at = nowIso();
    this.record.last_user_interaction_at = this.record.last_attach_at;
    if (!this.streaming) {
      await this.startScreencast("interactive");
    }
    this.boostScreencast();
    if (this.lastFrame) {
      client.sendFrame(this.lastFrame);
    } else {
      const entry = this.pageEntries.get(this.activePageId);
      if (entry) {
        await this.captureCurrentPageFrame(entry, client);
      }
    }
    return await this.syncAndEmitState({ persist: false });
  }

  async detachClient(client) {
    this.clients.delete(client);
    if (this.record.status !== "running" || !this.browser || !this.cdpSession) {
      this.streaming = false;
      this.record.client_count = this.clients.size;
      this.record.updated_at = nowIso();
      await this.manager.persist();
      return this.snapshot();
    }
    if (this.clients.size === 0 && this.streaming) {
      await this.stopScreencast();
    }
    if (this.clients.size === 0 && this.record.resource_state === "active") {
      this.record.resource_state = "background";
      this.record.last_detach_at = nowIso();
    }
    return await this.syncAndEmitState({ persist: false });
  }

  async startScreencast(profile = "interactive") {
    this.assertRunning();
    const settings = STREAM_PROFILES[profile];
    if (!settings) throw new Error(`未知浏览器流配置: ${profile}`);
    await this.cdpSession.send("Page.startScreencast", {
      format: "jpeg",
      quality: settings.quality,
      maxWidth: this.record.viewport?.width || DEFAULT_VIEWPORT.width,
      maxHeight: this.record.viewport?.height || DEFAULT_VIEWPORT.height,
      everyNthFrame: settings.everyNthFrame,
    });
    this.streaming = true;
    this.streamProfile = profile;
    this.record.stream_error = null;
    const entry = this.pageEntries.get(this.activePageId);
    if (entry) {
      entry.streaming = true;
    }
  }

  async stopScreencast() {
    this.assertRunning();
    await this.cdpSession.send("Page.stopScreencast");
    this.streaming = false;
    this.streamProfile = null;
    const entry = this.pageEntries.get(this.activePageId);
    if (entry) {
      entry.streaming = false;
    }
  }

  async restartScreencast(profile) {
    if (!this.streaming || this.streamProfile === profile) return;
    await this.stopScreencast();
    await this.startScreencast(profile);
  }

  async queueScreencastProfile(profile) {
    if (!STREAM_PROFILES[profile]) {
      throw new Error(`未知浏览器流配置: ${profile}`);
    }
    if (this.streamRequestedProfile === null
      && (this.streamTransitionActiveProfile === profile
        || (this.streamTransitionActiveProfile === null && this.streamProfile === profile))) {
      return await this.streamTransition;
    }
    this.streamRequestedProfile = profile;
    const execution = this.streamTransition.then(async () => {
      while (this.streaming && this.streamRequestedProfile !== null) {
        const requestedProfile = this.streamRequestedProfile;
        this.streamRequestedProfile = null;
        this.streamTransitionActiveProfile = requestedProfile;
        try {
          await this.restartScreencast(requestedProfile);
        } finally {
          this.streamTransitionActiveProfile = null;
        }
      }
    });
    this.streamTransition = execution.then(() => undefined, () => undefined);
    return await execution;
  }

  recordStreamFrame(byteLength) {
    const timestampMs = Date.now();
    this.streamSamples.push({ timestampMs, byteLength });
    const cutoff = timestampMs - 5_000;
    while (this.streamSamples[0]?.timestampMs < cutoff) this.streamSamples.shift();
    if (this.attachRequestedAtMs !== null) {
      this.record.last_attach_first_frame_ms = timestampMs - this.attachRequestedAtMs;
      this.attachRequestedAtMs = null;
    }
  }

  streamMetrics() {
    const samples = this.streamSamples;
    const deliveryClients = [...this.clients]
      .map((client) => client.frameFlowSnapshot?.())
      .filter(Boolean);
    const delivery = {
      client_count: deliveryClients.length,
      frames_sent: deliveryClients.reduce((total, item) => total + item.frames_sent, 0),
      frames_superseded: deliveryClients.reduce(
        (total, item) => total + item.frames_superseded,
        0,
      ),
      ack_timeouts: deliveryClients.reduce((total, item) => total + item.ack_timeouts, 0),
      max_ack_rtt_ms: deliveryClients.reduce(
        (maximum, item) => Math.max(maximum, item.max_ack_rtt_ms),
        0,
      ),
      clients: deliveryClients,
    };
    if (samples.length < 2) {
      return {
        profile: this.streamProfile,
        fps: 0,
        bitrate_bps: 0,
        sample_frames: samples.length,
        last_attach_first_frame_ms: this.record.last_attach_first_frame_ms ?? null,
        delivery,
      };
    }
    const durationSeconds = Math.max(
      0.001,
      (samples.at(-1).timestampMs - samples[0].timestampMs) / 1_000,
    );
    const bytes = samples.reduce((total, sample) => total + sample.byteLength, 0);
    return {
      profile: this.streamProfile,
      fps: Number(((samples.length - 1) / durationSeconds).toFixed(2)),
      bitrate_bps: Math.round((bytes * 8) / durationSeconds),
      sample_frames: samples.length,
      last_attach_first_frame_ms: this.record.last_attach_first_frame_ms ?? null,
      delivery,
    };
  }

  async captureCurrentPageFrame(entry, client = null) {
    if (!entry || entry.page.isClosed() || entry.pageId !== this.activePageId) {
      return;
    }
    const screenshot = await this.captureFrameScreenshot(entry, 80);
    const frameViewport = this.frameViewport();
    const frame = {
      frameId: ++this.frameSequence,
      browserId: this.id,
      pageId: entry.pageId,
      jpeg: screenshot.jpeg,
      width: frameViewport.width,
      height: frameViewport.height,
      pixelWidth: screenshot.pixelWidth,
      pixelHeight: screenshot.pixelHeight,
      pageScaleFactor: 1,
      timestamp: Date.now() / 1000,
    };
    entry.lastFrame = frame;
    this.lastFrame = frame;
    this.recordStreamFrame(frame.jpeg.byteLength);
    if (client) {
      client.sendFrame(frame);
      return;
    }
    this.emit("frame", frame);
  }

  async handleScreencastFrame(event, entry = this.pageEntries.get(this.activePageId)) {
    if (!entry || entry.page.isClosed()) {
      return;
    }
    const frameViewport = this.frameViewport();
    const screenshot = frameViewport.pixelRatio > 1
      ? await this.captureFrameScreenshot(entry, STREAM_PROFILES[this.streamProfile || "interactive"].quality)
      : {
        jpeg: Buffer.from(event.data, "base64"),
        pixelWidth: frameViewport.width,
        pixelHeight: frameViewport.height,
      };
    entry.lastFrame = {
      frameId: ++this.frameSequence,
      browserId: this.id,
      pageId: entry.pageId,
      jpeg: screenshot.jpeg,
      width: frameViewport.width,
      height: frameViewport.height,
      pixelWidth: screenshot.pixelWidth,
      pixelHeight: screenshot.pixelHeight,
      pageScaleFactor: event.metadata.pageScaleFactor || 1,
      timestamp: event.metadata.timestamp || Date.now() / 1000,
    };
    if (entry.pageId !== this.activePageId) {
      await entry.cdpSession.send("Page.screencastFrameAck", {
        sessionId: event.sessionId,
      });
      return;
    }
    this.lastFrame = entry.lastFrame;
    this.recordStreamFrame(this.lastFrame.jpeg.byteLength);
    this.emit("frame", this.lastFrame);
    await entry.cdpSession.send("Page.screencastFrameAck", {
      sessionId: event.sessionId,
    });
  }

  frameViewport() {
    const deviceState = this.effectiveDeviceState();
    const width = deviceState.viewport.width || DEFAULT_VIEWPORT.width;
    const height = deviceState.viewport.height || DEFAULT_VIEWPORT.height;
    return {
      width,
      height,
      pixelRatio: deviceState.deviceScaleFactor,
    };
  }

  async captureFrameScreenshot(entry, quality) {
    const frameViewport = this.frameViewport();
    const screenshot = await entry.cdpSession.send("Page.captureScreenshot", {
      format: "jpeg",
      quality,
      fromSurface: true,
    });
    return {
      jpeg: Buffer.from(screenshot.data, "base64"),
      pixelWidth: Math.round(frameViewport.width * frameViewport.pixelRatio),
      pixelHeight: Math.round(frameViewport.height * frameViewport.pixelRatio),
    };
  }

  async applyDeviceEmulation(entry) {
    const deviceState = this.effectiveDeviceState();
    const emulation = browserDeviceEmulationOptions(
      this.record.device_profile,
      this.record.device_orientation,
      {
        fallbackUserAgent: this.baseUserAgent,
        fallbackPlatform: this.basePlatform,
        viewport: deviceState.viewport,
        deviceScaleFactor: deviceState.deviceScaleFactor,
        touchEnabled: deviceState.hasTouch,
        ...(this.record.user_agent_override
          ? { userAgent: this.record.user_agent_override }
          : {}),
      },
    );
    await entry.page.setViewportSize({ width: emulation.width, height: emulation.height });
    await entry.cdpSession.send("Emulation.setDeviceMetricsOverride", {
      width: emulation.width,
      height: emulation.height,
      deviceScaleFactor: emulation.deviceScaleFactor,
      mobile: emulation.mobile,
      screenWidth: emulation.screenWidth,
      screenHeight: emulation.screenHeight,
      screenOrientation: emulation.screenOrientation,
    });
    await entry.cdpSession.send("Emulation.setTouchEmulationEnabled", {
      enabled: emulation.touchEnabled,
      ...(emulation.touchEnabled ? { maxTouchPoints: emulation.maxTouchPoints } : {}),
    });
    await entry.cdpSession.send("Emulation.setEmitTouchEventsForMouse", {
      enabled: emulation.touchEnabled,
      configuration: emulation.touchEnabled ? "mobile" : "desktop",
    });
    await entry.cdpSession.send("Emulation.setUserAgentOverride", {
      userAgent: emulation.userAgent,
      platform: emulation.platform,
    });
    const network = getBrowserNetworkProfile(this.record.network_profile_id);
    await entry.cdpSession.send("Network.emulateNetworkConditions", {
      offline: network.offline,
      latency: network.latency,
      downloadThroughput: network.downloadThroughput,
      uploadThroughput: network.uploadThroughput,
      connectionType: network.offline ? "none" : "wifi",
    });
  }

  async setViewport(width, height) {
    return await this.setDeviceSettings({ width, height });
  }

  async setDeviceProfile(profileId, orientation = DEFAULT_BROWSER_DEVICE_ORIENTATION) {
    return await this.setDeviceSettings({
      profileId,
      orientation,
      resetProfileSettings: true,
    });
  }

  async setDeviceSettings(settings = {}) {
    this.assertRunning();
    if (!settings || typeof settings !== "object" || Array.isArray(settings)) {
      throw new Error("浏览器设备设置必须是 object");
    }
    if (settings.deviceScaleFactor !== undefined && settings.deviceScaleFactor !== null
      && (!Number.isFinite(settings.deviceScaleFactor)
        || settings.deviceScaleFactor < 0.1
        || settings.deviceScaleFactor > 8)) {
      throw new Error("deviceScaleFactor 必须在 0.1 到 8 之间");
    }
    if (settings.userAgent !== undefined && settings.userAgent !== null
      && (typeof settings.userAgent !== "string" || settings.userAgent.length > 2000)) {
      throw new Error("userAgent 必须是长度不超过 2000 的字符串");
    }
    if (settings.touchSimulation !== undefined && settings.touchSimulation !== null
      && typeof settings.touchSimulation !== "boolean") {
      throw new Error("touchSimulation 必须是 boolean");
    }
    const profileId = settings.profileId ?? this.record.device_profile;
    const orientation = settings.orientation ?? this.record.device_orientation;
    if (typeof profileId !== "string" || !profileId.trim()) {
      throw new Error("浏览器设备配置不能为空");
    }
    const nextBaseState = resolveBrowserDeviceState(profileId, orientation);
    const previousSettings = {
      device_profile: this.record.device_profile,
      device_orientation: this.record.device_orientation,
      viewport_override: this.record.viewport_override
        ? { ...this.record.viewport_override }
        : null,
      device_scale_factor_override: this.record.device_scale_factor_override,
      user_agent_override: this.record.user_agent_override,
      touch_simulation_override: this.record.touch_simulation_override,
      network_profile_id: this.record.network_profile_id,
    };
    const profileChanged = previousSettings.device_profile !== nextBaseState.id;
    const orientationChanged = previousSettings.device_orientation !== nextBaseState.orientation;
    const resetProfileSettings = settings.resetProfileSettings === true;
    const resetAll = settings.reset === true;
    const nextNetworkProfileId = resetAll || resetProfileSettings
      ? DEFAULT_BROWSER_NETWORK_PROFILE
      : (settings.networkProfileId ?? (profileChanged
        ? DEFAULT_BROWSER_NETWORK_PROFILE
        : previousSettings.network_profile_id));
    getBrowserNetworkProfile(nextNetworkProfileId);
    this.record.device_profile = nextBaseState.id;
    this.record.device_orientation = nextBaseState.orientation;

    let viewportOverride = previousSettings.viewport_override;
    if (resetAll || resetProfileSettings || profileChanged) {
      viewportOverride = null;
    }
    if (orientationChanged && viewportOverride && settings.width === undefined && settings.height === undefined) {
      viewportOverride = {
        width: viewportOverride.height,
        height: viewportOverride.width,
      };
    }
    if (settings.width !== undefined || settings.height !== undefined) {
      const currentViewport = viewportOverride || nextBaseState.viewport;
      const width = settings.width ?? currentViewport.width;
      const height = settings.height ?? currentViewport.height;
      if (!Number.isInteger(width) || width <= 0 || width > 4096
        || !Number.isInteger(height) || height <= 0 || height > 4096) {
        throw new Error(`非法 viewport: ${width}x${height}`);
      }
      viewportOverride = {
        width,
        height,
      };
    }
    this.record.viewport_override = viewportOverride;
    this.record.device_scale_factor_override = resetAll || resetProfileSettings
      ? null
      : (settings.deviceScaleFactor === undefined
        ? profileChanged ? null : previousSettings.device_scale_factor_override
        : settings.deviceScaleFactor);
    this.record.user_agent_override = resetAll || resetProfileSettings
      ? null
      : (settings.userAgent === undefined
        ? profileChanged ? null : previousSettings.user_agent_override
        : settings.userAgent || null);
    this.record.touch_simulation_override = resetAll || resetProfileSettings
      ? null
      : (settings.touchSimulation === undefined
        ? profileChanged ? null : previousSettings.touch_simulation_override
        : settings.touchSimulation);
    this.record.network_profile_id = nextNetworkProfileId;
    this.syncDeviceStateRecord();

    const previousEffectiveState = resolveBrowserDeviceState(
      previousSettings.device_profile,
      previousSettings.device_orientation,
      {
        ...(previousSettings.viewport_override
          ? { viewport: previousSettings.viewport_override }
          : {}),
        ...(previousSettings.device_scale_factor_override !== null
          ? { deviceScaleFactor: previousSettings.device_scale_factor_override }
          : {}),
        ...(previousSettings.touch_simulation_override !== null
          ? { touchEnabled: previousSettings.touch_simulation_override }
          : {}),
        ...(previousSettings.user_agent_override
          ? { userAgent: previousSettings.user_agent_override }
          : {}),
      },
    );
    const shouldStream = this.streaming || this.clients.size > 0;
    try {
      if (this.streaming) {
        await this.stopScreencast();
      }
      for (const entry of this.pageEntries.values()) {
        await this.applyDeviceEmulation(entry);
      }
      const deviceEmulationChanged = profileChanged
        || orientationChanged
        || previousEffectiveState.userAgent !== this.effectiveDeviceState().userAgent
        || previousEffectiveState.hasTouch !== this.effectiveDeviceState().hasTouch;
      if (deviceEmulationChanged) {
        for (const entry of this.pageEntries.values()) {
          const targetUrl = entry.actualUrl || entry.url || this.record.url || "about:blank";
          // TODO: 动态切换 UA 后 Chromium 可能复用切换前的桌面文档缓存，必须强制重新获取页面。
          await entry.cdpSession.send("Network.setCacheDisabled", { cacheDisabled: true });
          await entry.cdpSession.send("Network.setBypassServiceWorker", { bypass: true });
          try {
            const navigationUrl = targetUrl === "about:blank"
              ? targetUrl
              : (() => {
                const url = new URL(targetUrl);
                url.searchParams.set(
                  "__boxteam_device_profile",
                  `${this.record.device_profile}-${this.record.device_orientation}-${Date.now()}`,
                );
                return url.toString();
              })();
            let navigated = false;
            for (let attempt = 0; attempt < 2 && !navigated; attempt += 1) {
              try {
                await entry.page.goto(navigationUrl, {
                  timeout: NAVIGATION_TIMEOUT_MS,
                  waitUntil: "domcontentloaded",
                });
                navigated = true;
              } catch (error) {
                const message = error instanceof Error ? error.message : String(error);
                if (attempt === 1 || !message.includes("ERR_NETWORK_CHANGED")) {
                  throw error;
                }
                await new Promise((resolve) => setTimeout(resolve, 250));
              }
            }
            await new Promise((resolve) => setTimeout(resolve, 250));
            if (navigationUrl !== targetUrl) {
              await entry.page.evaluate((url) => {
                window.history.replaceState(null, document.title, url);
              }, targetUrl);
            }
          } finally {
            await entry.cdpSession.send("Network.setBypassServiceWorker", { bypass: false });
            await entry.cdpSession.send("Network.setCacheDisabled", { cacheDisabled: false });
          }
        }
      }
      if (shouldStream) {
        await this.startScreencast();
        const activeEntry = this.pageEntries.get(this.activePageId);
        if (activeEntry) {
          await this.captureCurrentPageFrame(activeEntry);
        }
      }
      return await this.syncAndEmitState();
    } catch (error) {
      this.record.device_profile = previousSettings.device_profile;
      this.record.device_orientation = previousSettings.device_orientation;
      this.record.viewport_override = previousSettings.viewport_override;
      this.record.device_scale_factor_override = previousSettings.device_scale_factor_override;
      this.record.user_agent_override = previousSettings.user_agent_override;
      this.record.touch_simulation_override = previousSettings.touch_simulation_override;
      this.record.network_profile_id = previousSettings.network_profile_id;
      this.syncDeviceStateRecord();
      for (const entry of this.pageEntries.values()) {
        await this.applyDeviceEmulation(entry);
      }
      throw error;
    }
  }

  async saveDevicePreset(name) {
    this.assertRunning();
    if (typeof name !== "string" || !name.trim()) {
      throw new Error("设备预设名称不能为空");
    }
    const preset = {
      id: `preset_${randomUUID().replaceAll("-", "")}`,
      name: name.trim().slice(0, 80),
      profile_id: this.record.device_profile,
      orientation: this.record.device_orientation,
      viewport: { ...this.effectiveDeviceState().viewport },
      device_scale_factor: this.effectiveDeviceState().deviceScaleFactor,
      user_agent: this.record.user_agent_override,
      touch_simulation: this.effectiveDeviceState().hasTouch,
      network_profile_id: this.record.network_profile_id,
      created_at: nowIso(),
    };
    this.record.device_presets = [
      ...this.record.device_presets.filter((item) => item.name !== preset.name),
      preset,
    ].slice(-20);
    return await this.syncAndEmitState();
  }

  async dispatchPointer(message) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(
      () => this.pointerController.dispatch(this.cdpSession, message),
    );
  }

  async dispatchKey(message) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => dispatchKey(this.cdpSession, message));
  }

  async insertText(text) {
    this.assertRunning();
    await insertText(this.cdpSession, text);
  }

  async readClipboardText() {
    this.assertRunning();
    return await this.page.evaluate(() => {
      const active = document.activeElement;
      if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
        const start = active.selectionStart;
        const end = active.selectionEnd;
        if (typeof start === "number" && typeof end === "number" && end > start) {
          return active.value.slice(start, end);
        }
      }
      return window.getSelection()?.toString() || "";
    });
  }

  async findText(query, backwards = false) {
    this.assertRunning();
    // TODO: window.find 是 Chromium 集成浏览器的兼容接口，未来替换为可返回匹配计数的查找实现。
    const found = await this.page.evaluate(
      ({ text, searchBackwards }) => window.find(text, false, searchBackwards, true, false, false, false),
      { text: query, searchBackwards: backwards },
    );
    return { ...this.snapshot(), find_found: found, find_query: query };
  }

  async inspectElement(point) {
    this.assertRunning();
    return await inspectPageElement(this.page, this.refSelectors, point, this.documentRevision);
  }

  async readSummary() {
    this.assertRunning();
    const result = await readBrowserSummary(this.page, this.refSelectors, this.documentRevision);
    this.record.url = result.url;
    this.record.title = result.title;
    const activeEntry = this.pageEntries.get(this.activePageId);
    if (activeEntry) {
      activeEntry.url = result.url;
      activeEntry.title = result.title;
    }
    const state = this.snapshot();
    return {
      ...result,
      active_page_id: state.active_page_id,
      pages: state.pages,
    };
  }

  async runPageActionWithModalDetection(action) {
    if (this.pendingDialog || this.pendingFileChooser) {
      throw new Error("当前页面已有待处理浏览器对话框，请先调用 handleDialog。");
    }

    let onModal = null;
    let timer = null;
    const modalPromise = new Promise((resolve) => {
      onModal = (event) => resolve({ source: "modal", kind: event.kind });
      this.on("browser-modal", onModal);
      timer = setTimeout(
        () => resolve({ source: "timeout", kind: null }),
        BROWSER_MODAL_DETECTION_MS,
      );
    });
    const actionPromise = Promise.resolve().then(action);
    try {
      const winner = await Promise.race([
        actionPromise.then(() => ({ source: "action", kind: null })),
        modalPromise,
      ]);
      if (winner.source === "modal") {
        actionPromise.catch(() => undefined);
        await this.syncAndEmitState();
        return winner.kind;
      }
      await actionPromise;
      return null;
    } finally {
      if (onModal) {
        this.off("browser-modal", onModal);
      }
      if (timer) {
        clearTimeout(timer);
      }
    }

  }

  async click(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => clickElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async hover(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => hoverElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async typeInPage(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => typeInPage(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async drag(args) {
    this.assertRunning();
    await this.runPageActionWithModalDetection(() => dragElement(this.page, this.refSelectors, args));
    return await this.syncAndEmitState();
  }

  async handleDialog(args) {
    this.assertRunning();
    const result = await handleDialog(this, args);
    return { ...result, state: await this.syncAndEmitState() };
  }

  async screenshot(args) {
    this.assertRunning();
    const buffer = await screenshotPage(this.page, this.refSelectors, args);
    const imagePath = await this.manager.writeScreenshot(this.id, buffer);
    await this.syncAndEmitState();
    return {
      image_path: imagePath,
      mime_type: "image/png",
      byte_length: buffer.byteLength,
    };
  }

  async captureClientScreenshot() {
    this.assertRunning();
    const screenshot = await this.cdpSession.send("Page.captureScreenshot", {
      format: "png",
      fromSurface: true,
    });
    return Buffer.from(screenshot.data, "base64");
  }

  async debugSnapshot() {
    this.assertRunning();
    const entry = this.pageEntries.get(this.activePageId);
    if (!entry) {
      throw new Error(`当前没有活动浏览器标签页: browser_id=${this.id}`);
    }
    const elements = await entry.page.evaluate(() => {
      const maxDepth = 5;
      const maxChildren = 80;
      const summarize = (node, path, depth) => {
        if (!(node instanceof Element) || depth > maxDepth) return null;
        const attributes = {};
        for (const attribute of [...node.attributes].slice(0, 20)) {
          attributes[attribute.name] = attribute.value.slice(0, 300);
        }
        const text = [...node.childNodes]
          .filter((child) => child.nodeType === Node.TEXT_NODE)
          .map((child) => child.textContent || "")
          .join(" ")
          .replace(/\s+/g, " ")
          .trim()
          .slice(0, 240);
        return {
          node_id: path,
          tag: node.tagName.toLowerCase(),
          attributes,
          text,
          children: [...node.children]
            .slice(0, maxChildren)
            .map((child, index) => summarize(child, `${path}.${index}`, depth + 1))
            .filter(Boolean),
        };
      };
      return summarize(document.documentElement, "0", 0);
    });
    const sources = await entry.page.evaluate(() => {
      const sourceTypes = new Set(["script", "stylesheet", "link", "css", "fetch", "xmlhttprequest"]);
      const seen = new Set();
      return performance.getEntriesByType("resource")
        .filter((resource) => resource.name && (sourceTypes.has(resource.initiatorType) || /\.(?:js|css|map)(?:$|\?)/i.test(resource.name)))
        .map((resource) => ({
          url: resource.name,
          type: resource.initiatorType || "resource",
          duration_ms: Math.round(resource.duration),
        }))
        .filter((resource) => {
          if (seen.has(resource.url)) return false;
          seen.add(resource.url);
          return true;
        })
        .slice(0, 200);
    });
    return {
      page_id: entry.pageId,
      title: entry.title || await this.page.title(),
      url: entry.url || this.page.url(),
      elements,
      sources,
      console: [...(entry.consoleMessages || [])],
      network: [...(entry.networkRequests || [])],
      captured_at: nowIso(),
    };
  }

  async clearNetworkRequests() {
    this.assertRunning();
    const activeEntry = this.pageEntries.get(this.activePageId);
    if (activeEntry) {
      activeEntry.networkRequests.length = 0;
    }
    return await this.syncAndEmitState({ persist: false });
  }

  async runPlaywrightCode(args) {
    this.assertRunning();
    let result = null;
    const modalKind = await this.runPageActionWithModalDetection(async () => {
      result = await runPlaywrightCode(
        { page: this.page, context: this.context, browser: this.browserHandle },
        args,
      );
    });
    await this.syncAndEmitState();
    if (modalKind) {
      return {
        result: null,
        summary: `${modalKind === "dialog" ? "Playwright 代码触发了浏览器对话框" : "Playwright 代码触发了文件选择对话框"}，请调用 handleDialog 继续。`,
        state: this.snapshot(),
      };
    }
    return result;
  }
}
