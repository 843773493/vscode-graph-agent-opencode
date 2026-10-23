import assert from "node:assert/strict";
import { appendFile, chmod, readFile, rename, stat, unlink, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
import { dirname, join } from "node:path";
import process from "node:process";
import { chromium } from "playwright";
import { beginForkOperationDeadline, beginForkSubmissionDeadline, bindForkOperationDeadline,
  assertForkDeadlineCloseSemanticsForTest, closeForkDeadlineWatchdog, planGoldenLoop, planChatCalls, testControl,
  uuidText, waitForForkOperation } from "../../../support/e2e_control/browser_client.mjs";
import { expectedTypedInternalHistoryMembership } from "./generated-internal-history-oracle.mjs";

function requiredEnvironment(name) {
  const value = process.env[name];
  if (!value) throw new Error(`缺少环境变量 ${name}`);
  return value;
}

const firstPrompt = "请读取 README.md，并确认首轮工具调用已完成。";
const firstFinalText = "首轮工具调用已完成。";
const secondPrompt = "请再次读取 README.md，确认历史上下文仍然可用。";
const secondFinalText = "第二轮工具调用也已完成。";
const SECTION8_SELECTION_CONTRACT_ARG = "--section8-selection-contract";

function section8ScenarioRegistry(surfaces, facts) {
  let foreign;
  const teamTarget = async () => foreign ??= await createProductSession(surfaces.b, "团队拒绝跨 Session 目标");
  return {
    basic_chat: { default: true, run: () => runBasicChat(surfaces.a) },
    goal_owner: { default: true, run: () => runGoalOwner(surfaces.a) },
    session_page_selection: { default: true, run: () => runSessionPageSelection(surfaces.a) },
    child: { default: true, run: () => runChildFifoAndResidency(surfaces.a) },
    runtime_shared_resources: { default: true, run: () => runRuntimeSharedResources(surfaces.a) },
    background_mailbox: { default: true, run: () => runBackgroundMailboxRecovery(surfaces.a) },
    team_turn: { default: true, run: async () => runTeamActivation(surfaces.a, "turn", await teamTarget()) },
    team_model_call: { default: true, run: async () => runTeamActivation(surfaces.c, "model_call", await teamTarget()) },
    mcp_turn: { default: true, run: () => runMcpActivation(surfaces.a, "turn") },
    mcp_model_call: { default: true, run: () => runMcpActivation(surfaces.c, "model_call") },
    weekly: { default: true, run: () => runWeeklyReport(surfaces) },
    empty_fork_ui: { default: true, run: () => runEmptyForkUi(surfaces.a) },
    fork_child_ui: { default: true, run: () => runDefaultChildForkUi(surfaces.a) },
    forks: { default: true, run: () => runPublicForks(surfaces) },
    live_resource_forks: { default: false, run: () => runLiveResourceForks(surfaces.a, []) },
    copy_attachments: { default: false, run: () => runCopyAttachmentFocus(surfaces.a) },
    message_stream_after20: { default: false, run: () => runMessageStreamAfter20(surfaces.a) },
    copy_missing_attachment: {
      default: false,
      run: async () => {
        const owners = [];
        return runCopyMissingAttachment(surfaces.a, owners);
      },
    },
    optional_seed_revisit: { default: false, run: () => runOptionalSeedRevisit(surfaces.a) },
    compaction: { default: true, run: () => runRealCompaction(surfaces.a) },
    communication_recovery: { default: true, run: () => runCommunicationRecovery(surfaces) },
    read_recovery: { default: true, run: () => facts.weekly?.frozenRead ?? runReadRecovery(surfaces) },
    wait_recovery: { default: true, run: () => runWaitRecovery(surfaces) },
    observation_deletion: { default: true, run: () => runObservationDeletion(surfaces) },
    lifecycle_admission_deletion: { default: true, run: () => runLifecycleAdmissionDeletion(surfaces) },
    navigation_dates: { default: true, run: () => runNavigationDates(surfaces) },
    navigation_optimism: { default: true, run: () => runNavigationOptimism(surfaces) },
    initial_admission_recovery: { default: true, run: () => runInitialAdmissionRecovery(surfaces) },
    generated_internal_history: { default: true, run: () => runGeneratedInternalHistory(surfaces) },
    owner_turns_navigation_smoke: { default: false, run: () => runOwnerTurnsNavigationSmoke(surfaces.a) },
    turn_navigation: { default: true, run: () => runTurnNavigation(surfaces.a) },
    read_cursor_expiry: { default: true, run: () => runReadCursorExpiry(surfaces, facts.read_recovery) },
    skill_untrack: { default: true, run: () => runSkillUntrack(surfaces.a) },
  };
}

function section8SelectionContract(raw) {
  if (typeof raw !== "string" || !raw || raw.trim() !== raw) {
    throw new Error("BOXTEAM_E2E_SECTION8_FOCUS 必须显式设置为 default 或合法 scenario key 列表");
  }
  const registry = section8ScenarioRegistry({}, {});
  const entries = Object.entries(registry);
  const validScenarios = entries.map(([name]) => name);
  const defaultScenarios = entries.filter(([, scenario]) => scenario.default).map(([name]) => name);
  if (raw === "default") {
    return { mode: "default", requested: ["default"], selected: defaultScenarios, validScenarios, defaultScenarios };
  }
  const requested = raw.split(",");
  if (requested.some((name) => !name || name.trim() !== name || !/^[a-z][a-z0-9_]*$/.test(name))) {
    throw new Error("BOXTEAM_E2E_SECTION8_FOCUS 含空白、空项或非法 scenario key");
  }
  if (new Set(requested).size !== requested.length) {
    throw new Error("BOXTEAM_E2E_SECTION8_FOCUS 不得重复选择 scenario");
  }
  if (requested.includes("default") || requested.some((name) => !validScenarios.includes(name))) {
    throw new Error("BOXTEAM_E2E_SECTION8_FOCUS 包含 default 混选或未知 scenario key");
  }
  if (requested.includes("read_cursor_expiry")) {
    if (!requested.includes("read_recovery")) {
      throw new Error("read_cursor_expiry focus 必须按顺序选择 read_recovery,read_cursor_expiry");
    }
    if (requested.at(-1) !== "read_cursor_expiry") {
      throw new Error("read_cursor_expiry focus 必须按顺序选择 read_recovery,read_cursor_expiry");
    }
  }
  return { mode: "focus", requested, selected: requested, validScenarios, defaultScenarios };
}

const messageStreamDiagnosticsSelfTest = process.argv.includes("--message-stream-diagnostics-self-test");
const forkDeadlineClassifierSelfTest = process.argv.includes("--fork-deadline-classifier-self-test");
const forkPeerContextCleanupSelfTest = process.argv.includes("--fork-peer-context-cleanup-self-test");
const controlledRestartClassifierSelfTest = process.argv.includes("--controlled-restart-classifier-self-test");
if ([messageStreamDiagnosticsSelfTest, forkDeadlineClassifierSelfTest, forkPeerContextCleanupSelfTest,
  controlledRestartClassifierSelfTest].filter(Boolean).length > 1) {
  throw new Error("一次只能选择一个 MJS self-test");
}
const standaloneSelfTest = messageStreamDiagnosticsSelfTest || forkDeadlineClassifierSelfTest
  || forkPeerContextCleanupSelfTest || controlledRestartClassifierSelfTest;
const section8Selection = standaloneSelfTest
  ? null
  : section8SelectionContract(process.env.BOXTEAM_E2E_SECTION8_FOCUS);
if (process.argv.includes(SECTION8_SELECTION_CONTRACT_ARG)) {
  if (standaloneSelfTest) throw new Error("selector contract 与 self-test 不能同时选择");
  process.stdout.write(JSON.stringify(section8Selection) + "\n");
  process.exit(0);
}
const baseUrl = standaloneSelfTest ? "" : requiredEnvironment("BOXTEAM_BROWSER_BASE_URL");
const expectedWorkspaceId = standaloneSelfTest ? "" : requiredEnvironment("BOXTEAM_BROWSER_WORKSPACE_ID");
const resultPath = standaloneSelfTest ? "" : requiredEnvironment("BOXTEAM_BROWSER_RESULT_PATH");
const screenshotPath = standaloneSelfTest ? "" : requiredEnvironment("BOXTEAM_BROWSER_SCREENSHOT_PATH");
const executablePath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined;
const TRANSCRIPT_NAVIGATION_TIMEOUT_MS = 60_000;
const FORK_OPERATION_DEADLINE_MS = 60_000;
const FORK_HISTORY_VALIDATION_TIMEOUT_MS = 60_000;
const CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS = 5_000;
const forkOperationDeadlines = new Map();
const VIRTUOSO_AT_BOTTOM_THRESHOLD_PX = 4;
const HISTORY_TIMING_EVENT_LIMIT = 32;
// 生产 residency worker 每 60 秒扫描；后台卸载观察统一覆盖一个扫描周期。
const residencyScanObservationTimeoutMs = 70_000;

function recordHistoryTiming(timing, phase, startedAt, outcome, details = {}) {
  if (!timing) return;
  recordHistoryDuration(timing, phase, performance.now() - startedAt, outcome, details);
}

function recordHistoryDuration(timing, phase, durationMs, outcome, details = {}) {
  if (!timing || timing.events.length >= HISTORY_TIMING_EVENT_LIMIT) return;
  timing.events.push({ phase, duration_ms: Number(durationMs.toFixed(3)), outcome, ...details });
}

async function measureHistoryPhase(timing, phase, operation, details = {}) {
  if (!timing) return operation();
  const startedAt = performance.now();
  let outcome = "completed";
  try {
    return await operation();
  } catch (error) {
    outcome = "failed";
    throw error;
  } finally {
    recordHistoryTiming(timing, phase, startedAt, outcome,
      typeof details === "function" ? details() : details);
  }
}

async function localToken(page) {
  return page.evaluate(async () => {
    const response = await fetch("/api/gateway/auth/local-credential");
    if (!response.ok) throw new Error(`获取本地凭据失败: HTTP ${response.status}`);
    const payload = await response.json();
    if (typeof payload?.data?.token !== "string") throw new Error("本地凭据缺少 token");
    return payload.data.token;
  });
}

async function api(page, pathname, init = {}, expectedStatus, timing = null) {
  const token = await localToken(page);
  const requestStartedAt = timing ? performance.now() : null;
  let value;
  try {
    value = await page.evaluate(
      async ({ pathname: path, init: requestInit, token: localTokenValue, expectedStatusValue, collectTiming }) => {
        const headers = new Headers(requestInit.headers ?? {});
        headers.set("X-Local-Token", localTokenValue);
        const requestStartedAt = collectTiming ? performance.now() : null;
        const response = await fetch(path, {
          ...requestInit,
          headers,
          credentials: "include",
        });
        const body = await response.text();
        const statusMatches = expectedStatusValue === undefined ? response.ok : response.status === expectedStatusValue;
        if (!statusMatches) throw new Error(`API ${response.status}: ${body}`);
        const payload = body ? JSON.parse(body) : null;
        const requestToBodyParseMs = collectTiming ? performance.now() - requestStartedAt : null;
        const responseBodyUtf8Bytes = collectTiming ? new TextEncoder().encode(body).byteLength : null;
        const requestId = response.headers.get("X-Request-ID");
        if (!requestId || payload?.request_id && payload.request_id !== requestId) throw new Error(`API 请求身份不一致: ${path}`);
        return collectTiming ? { payload, requestToBodyParseMs, responseBodyUtf8Bytes } : payload;
      },
      { pathname, init, token, expectedStatusValue: expectedStatus, collectTiming: Boolean(timing) },
    );
  } catch (error) {
    if (timing) {
      timing.requestToBodyParseMs = performance.now() - requestStartedAt;
      timing.outcome = "failed";
    }
    throw error;
  }
  if (timing) {
    timing.requestToBodyParseMs = value.requestToBodyParseMs;
    timing.responseBodyUtf8Bytes = value.responseBodyUtf8Bytes;
    timing.outcome = "completed";
    return value.payload;
  }
  return value;
}

async function ownerApi(owner, pathname, init = {}, expectedStatus, timing = null) {
  return api(owner.page, pathname, {
    ...init, headers: { ...init.headers, "X-BoxTeam-Workspace-Id": owner.gatewayWorkspaceId },
  }, expectedStatus, timing);
}

async function ownerApiResponse(owner, pathname, init = {}) {
  const token = await localToken(owner.page);
  const { body, ...requestOptions } = init;
  const response = await owner.page.request.fetch(new URL(pathname, owner.page.url()).toString(), {
    ...requestOptions,
    headers: { ...init.headers, "X-Local-Token": token, "X-BoxTeam-Workspace-Id": owner.gatewayWorkspaceId },
    ...(body === undefined ? {} : { data: body }),
  });
  try {
    const text = await response.text();
    const payload = text ? JSON.parse(text) : null;
    const requestId = response.headers()["x-request-id"];
    assert.ok(requestId, `API ${pathname} 缺少 X-Request-ID`);
    assert.equal(payload?.request_id, requestId, `API ${pathname} 响应体与 X-Request-ID 不一致`);
    return { status: response.status(), requestId, body: payload };
  } finally {
    await response.dispose();
  }
}

async function waitUntil(predicate, label, timeout = 30_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`等待${label}超时`);
}

async function ensureGuest(page, url = baseUrl) {
  await page.controlledRestartCdpSetup;
  await drainPageResponseBodies(page);
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
  const current = await page.evaluate(async () => {
    const response = await fetch("/api/gateway/users/current");
    return { status: response.status, body: await response.text() };
  });
  if (current.status === 401) {
    await api(page, "/api/gateway/users/guest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracking: { source: "basic-chat-tool-loop-e2e" } }),
    });
    await drainPageResponseBodies(page);
    await page.reload({ waitUntil: "domcontentloaded" });
  } else if (current.status !== 200) {
    throw new Error(`检查游客访问失败: ${current.status} ${current.body}`);
  }
}

function isSessionMessageRequest(request) {
  return request.method() === "POST"
    && /\/api\/v1\/sessions\/[^/]+\/messages$/.test(new URL(request.url()).pathname);
}

function messageStreamTarget(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const match = /^\/api\/v1\/sessions\/([^/]+)\/turns\/([^/]+)\/message-stream$/.exec(parsed.pathname);
  if (!match) return null;
  const threadIds = parsed.searchParams.getAll("thread_id");
  const afterSeqs = parsed.searchParams.getAll("after_seq");
  const streamIds = parsed.searchParams.getAll("turn_stream_id");
  if (threadIds.length > 1 || afterSeqs.length > 1 || streamIds.length > 1) return null;
  return {
    sessionId: match[1],
    turnId: match[2],
    threadId: threadIds.length === 0 ? null : threadIds[0],
    turnStreamId: streamIds.length === 0 ? null : streamIds[0],
    afterSeq: afterSeqs.length === 0 ? null : afterSeqs[0],
  };
}

function isEventStreamResponse(response) {
  const contentType = response.headers?.contentType;
  return typeof contentType === "string"
    && contentType.split(";", 1)[0].trim().toLowerCase() === "text/event-stream";
}

async function readBoundedMessageStreamErrorBody(response, fact) {
  const maxBytes = 4096;
  const contentLength = response.headers()["content-length"];
  if (!/^(0|[1-9][0-9]*)$/.test(contentLength ?? "")) {
    fact.bodyError = "404 响应缺少有效 Content-Length，未读取响应体";
    return;
  }
  if (!Number.isSafeInteger(Number(contentLength)) || Number(contentLength) > maxBytes) {
    fact.bodyTooLarge = true;
    fact.bodyLimitBytes = maxBytes;
    return;
  }
  const bytes = await response.body();
  fact.bodyBytes = bytes.byteLength;
  if (bytes.byteLength > maxBytes) {
    fact.bodyTooLarge = true;
    fact.bodyLimitBytes = maxBytes;
    return;
  }
  fact.body = JSON.parse(bytes.toString("utf8"));
}

function runningAdmissionThreadId(admission, refusal, target) {
  const data = admission.body?.data;
  const dispatch = data?.dispatch;
  const admissionUrl = new URL(admission.url);
  const admissionPath = /^\/api\/v1\/sessions\/([^/]+)\/(?:threads\/([^/]+)\/)?messages$/.exec(admissionUrl.pathname);
  const ownerThreadId = data?.thread_id;
  const refusalUrl = new URL(refusal.url);
  const isOwner = admission.kind === "admission"
    && admission.status === 200
    && admission.method === "POST"
    && admissionUrl.origin === refusalUrl.origin
    && admissionPath?.[1] === target.sessionId
    && (admissionPath[2] === undefined || admissionPath[2] === ownerThreadId)
    && admission.threadId === (admissionPath?.[2] ?? null)
    && admission.pageId === refusal.pageId
    && admission.documentId === refusal.documentId
    && Number.isInteger(admission.pageId)
    && Number.isInteger(admission.documentId)
    && Number.isInteger(refusal.pageId)
    && Number.isInteger(refusal.documentId)
    && admission.sessionId === target.sessionId
    && admission.workspaceHeader === refusal.workspaceHeader
    && typeof admission.workspaceHeader === "string"
    && admission.workspaceHeader.length > 0
    && Number.isFinite(admission.requestedAt)
    && Number.isFinite(admission.receivedAt)
    && admission.requestedAt <= admission.receivedAt
    && Number.isFinite(refusal.requestedAt)
    && admission.receivedAt <= refusal.requestedAt
    && typeof admission.requestId === "string"
    && admission.requestId.length > 0
    && admission.body?.request_id === admission.requestId
    && typeof data?.job_id === "string"
    && data.job_id.length > 0
    && typeof ownerThreadId === "string"
    && ownerThreadId.length > 0
    && (target.threadId === null || target.threadId === ownerThreadId)
    && dispatch?.job_id === data.job_id
    && data?.turn_id === target.turnId
    && data?.status === "running"
    && dispatch?.thread_id === ownerThreadId
    && dispatch?.session_id === target.sessionId
    && dispatch?.job_status === "running"
    && data?.thread_id === dispatch?.thread_id;
  return isOwner ? ownerThreadId : null;
}

function classifyMessageStreamErrors(consoleErrorFacts, consoleErrors, messageStreamResponses, admissions) {
  const handled = [];
  const consumedFacts = new Set();
  for (const error of consoleErrorFacts) {
    if (error.text !== "Failed to load resource: the server responded with a status of 404 (Not Found)"
        || typeof error.url !== "string" || !error.url) continue;
    const target = messageStreamTarget(error.url);
    if (!target || target.threadId === "" || target.afterSeq !== "0") continue;
    const candidates = messageStreamResponses.filter((response) => response.status === 404
      && !consumedFacts.has(response)
      && response.method === "GET"
      && response.pageId === error.pageId
      && response.documentId === error.documentId
      && Number.isInteger(response.pageId)
      && Number.isInteger(response.documentId)
      && response.url === error.url
      && response.sessionId === target.sessionId
      && response.turnId === target.turnId
      && response.threadId === target.threadId
      && response.afterSeq === target.afterSeq
      && (() => {
        const responseTarget = messageStreamTarget(response.url);
        return responseTarget?.sessionId === target.sessionId
          && responseTarget.turnId === target.turnId
          && responseTarget.threadId === target.threadId
          && responseTarget.afterSeq === "0";
      })()
      && response.requestId
      && response.workspaceHeader);
    const eligible = candidates.map((refusal) => {
      if (typeof refusal.requestId !== "string" || refusal.requestId.length === 0
          || typeof refusal.body?.request_id !== "string"
          || refusal.body.request_id !== refusal.requestId
          || typeof refusal.body?.detail !== "string"
          || refusal.body.detail !== `消息流不存在: session_id=${target.sessionId} turn_id=${target.turnId}`
          || refusal.bodyTooLarge === true || refusal.bodyError
          || !Number.isFinite(refusal.requestedAt) || !Number.isFinite(refusal.receivedAt)
          || refusal.receivedAt < refusal.requestedAt) return null;
      const owners = admissions.map((admission) => ({ admission,
        threadId: consumedFacts.has(admission) ? null : runningAdmissionThreadId(admission, refusal, target) }))
        .filter((owner) => owner.threadId !== null);
      if (owners.length !== 1) return null;
      const { admission: owner, threadId: ownerThreadId } = owners[0];
      const recoveries = messageStreamResponses.filter((response) => response.status === 200
        && !consumedFacts.has(response)
        && response.method === "GET"
        && response.pageId === error.pageId
        && response.documentId === error.documentId
        && new URL(response.url).origin === new URL(refusal.url).origin
        && response.sessionId === target.sessionId
        && response.turnId === target.turnId
        && response.afterSeq === "0"
        && (response.threadId === null || response.threadId === ownerThreadId)
        && (() => {
          const responseTarget = messageStreamTarget(response.url);
          return responseTarget?.sessionId === target.sessionId
            && responseTarget.turnId === target.turnId
            && responseTarget.afterSeq === "0"
            && (responseTarget.threadId === null || responseTarget.threadId === ownerThreadId)
            && response.threadId === responseTarget.threadId;
        })()
        && response.workspaceHeader === refusal.workspaceHeader
        && typeof response.workspaceHeader === "string"
        && response.workspaceHeader.length > 0
        && typeof response.requestId === "string"
        && response.requestId.length > 0
        && isEventStreamResponse(response)
        && Number.isFinite(response.requestedAt)
        && response.requestedAt >= refusal.receivedAt
        && Number.isFinite(response.receivedAt)
        && response.receivedAt >= response.requestedAt
        && typeof response.messageStreamId === "string"
        && response.messageStreamId.length > 0);
      if (recoveries.length === 0) return null;
      const recovery = recoveries[0];
      return {
        responseFact: refusal,
        recoveryFact: recovery,
        admissionFact: owner,
        responseRequestId: refusal.requestId,
        responseBodyRequestId: refusal.body.request_id,
        admissionRequestId: owner.requestId,
        recoveryRequestId: recovery.requestId,
        recoveryStreamId: recovery.messageStreamId,
        jobId: owner.body.data.job_id,
        sessionId: target.sessionId,
        threadId: ownerThreadId,
        turnId: target.turnId,
      };
    });
    if (eligible.length !== 1 || eligible[0] === null) continue;
    const fact = eligible[0];
    if (consumedFacts.has(fact.responseFact) || consumedFacts.has(fact.recoveryFact)
        || consumedFacts.has(fact.admissionFact)) continue;
    consumedFacts.add(fact.responseFact);
    consumedFacts.add(fact.recoveryFact);
    consumedFacts.add(fact.admissionFact);
    const { responseFact, recoveryFact, admissionFact, ...diagnostic } = fact;
    handled.push({ consoleIndex: error.index, ...diagnostic });
  }
  return {
    handledMessageStreamErrors: handled,
    unhandledConsoleErrors: consoleErrors.filter((_error, index) => !handled.some((fact) => fact.consoleIndex === index)),
  };
}

function classifyIncompleteMessageStreamErrors(consoleErrorFacts, failedRequests, messageStreamResponses, admissions) {
  const handled = [];
  const consumedFailures = new Set();
  const consumedResponses = new Set();
  const consumedRecoveries = new Set();
  const consumedConsoleIndexes = new Set();
  for (const [failureIndex, failure] of failedRequests.entries()) {
    if (failure.error !== "net::ERR_INCOMPLETE_CHUNKED_ENCODING"
        || typeof failure.url !== "string" || !failure.url
        || !Number.isInteger(failure.pageId) || !Number.isInteger(failure.documentId)
        || !Number.isFinite(failure.requestedAt) || !Number.isFinite(failure.failedAt)
        || failure.failedAt < failure.requestedAt) continue;
    const responseCandidates = messageStreamResponses.filter((fact) => fact.requestId === failure.responseRequestId
      && fact.url === failure.url && fact.method === "GET" && fact.status === 200
      && fact.pageId === failure.pageId && fact.documentId === failure.documentId
      && fact.receivedAt <= failure.failedAt && isEventStreamResponse(fact)
      && typeof fact.requestId === "string" && fact.requestId.length > 0
      && typeof fact.messageStreamId === "string" && fact.messageStreamId.length > 0
      && typeof fact.workspaceHeader === "string" && fact.workspaceHeader.length > 0
      && failure.status === fact.status && failure.sessionId === fact.sessionId
      && failure.turnId === fact.turnId && failure.threadId === fact.threadId
      && failure.turnStreamId === fact.turnStreamId && failure.afterSeq === fact.afterSeq
      && failure.workspaceHeader === fact.workspaceHeader && failure.messageStreamId === fact.messageStreamId);
    if (responseCandidates.length !== 1) continue;
    const [response] = responseCandidates;
    if (consumedResponses.has(response)) continue;
    const target = messageStreamTarget(response.url);
    if (!target || target.afterSeq === null || !/^(0|[1-9]\d*)$/.test(target.afterSeq)
        || target.turnStreamId !== null && target.turnStreamId !== response.messageStreamId) continue;
    const ownerThreads = admissions.map((admission) => runningAdmissionThreadId(admission, response, target))
      .filter((threadId) => threadId !== null);
    if (ownerThreads.length !== 1 || target.threadId !== null && target.threadId !== ownerThreads[0]) continue;
    const afterSeq = Number(target.afterSeq);
    if (!Number.isSafeInteger(afterSeq)) continue;
    const recoveries = messageStreamResponses.filter((fact) => fact.status === 200 && fact.method === "GET"
      && !consumedRecoveries.has(fact)
      && fact.failure === undefined && !Number.isFinite(fact.failedAt)
      && !failedRequests.some((failed) => failed.error === "net::ERR_INCOMPLETE_CHUNKED_ENCODING"
        && failed.responseRequestId === fact.requestId && failed.url === fact.url
        && failed.pageId === fact.pageId && failed.documentId === fact.documentId
        && failed.failedAt >= fact.receivedAt)
      && new URL(fact.url).origin === new URL(response.url).origin
      && fact.pageId === response.pageId && fact.documentId === response.documentId
      && fact.sessionId === target.sessionId && fact.turnId === target.turnId
      && (fact.threadId === null || fact.threadId === ownerThreads[0])
      && fact.workspaceHeader === response.workspaceHeader
      && fact.requestId && fact.requestId !== response.requestId
      && fact.messageStreamId === response.messageStreamId
      && isEventStreamResponse(fact)
      && Number.isFinite(fact.requestedAt) && fact.requestedAt >= failure.failedAt
      && Number.isFinite(fact.receivedAt) && fact.receivedAt >= fact.requestedAt
      && (() => {
        const recoveryTarget = messageStreamTarget(fact.url);
        return recoveryTarget?.sessionId === target.sessionId
          && recoveryTarget.turnId === target.turnId
          && (recoveryTarget.threadId === null || recoveryTarget.threadId === ownerThreads[0])
          && recoveryTarget.turnStreamId === fact.messageStreamId
          && recoveryTarget.afterSeq !== null
          && Number.isSafeInteger(Number(recoveryTarget.afterSeq))
          && Number(recoveryTarget.afterSeq) >= afterSeq;
      })())
      .sort((left, right) => left.requestedAt - right.requestedAt);
    const recovery = recoveries[0];
    if (!recovery || recoveries[1]?.requestedAt === recovery.requestedAt) continue;
    const consoleCandidates = consoleErrorFacts.filter((fact) => !consumedConsoleIndexes.has(fact.index)
      && fact.text === "Failed to load resource: net::ERR_INCOMPLETE_CHUNKED_ENCODING"
      && fact.url === failure.url && fact.pageId === failure.pageId && fact.documentId === failure.documentId
      && Number.isFinite(fact.receivedAt) && Math.abs(fact.receivedAt - failure.failedAt) <= 5_000)
      .sort((left, right) => Math.abs(left.receivedAt - failure.failedAt) - Math.abs(right.receivedAt - failure.failedAt));
    const consoleError = consoleCandidates[0];
    if (consoleError && consoleCandidates[1]
        && Math.abs(consoleCandidates[1].receivedAt - failure.failedAt)
          === Math.abs(consoleError.receivedAt - failure.failedAt)) continue;
    consumedFailures.add(failureIndex);
    consumedResponses.add(response);
    consumedRecoveries.add(recovery);
    if (consoleError) consumedConsoleIndexes.add(consoleError.index);
    handled.push({ failedRequestIndex: failureIndex, consoleIndex: consoleError?.index ?? null,
      responseRequestId: response.requestId, recoveryRequestId: recovery.requestId,
      messageStreamId: response.messageStreamId, workspaceId: response.workspaceHeader,
      sessionId: target.sessionId, threadId: ownerThreads[0], turnId: target.turnId,
      failedAfterSeq: target.afterSeq, recoveredAfterSeq: messageStreamTarget(recovery.url).afterSeq });
  }
  return {
    handled,
    consumedConsoleIndexes,
    consumedFailureIndexes: consumedFailures,
    unhandledFailedRequests: failedRequests.filter((_failure, index) => !consumedFailures.has(index)),
  };
}

function sessionTargetFromUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const sessionPath = /^\/api\/v1\/sessions\/([^/]+)(?:\/|$)/.exec(parsed.pathname);
  const breadcrumbPath = /^\/api\/v1\/session-catalog\/breadcrumb\/([^/]+)$/.exec(parsed.pathname);
  const sessionId = sessionPath?.[1] ?? breadcrumbPath?.[1];
  return sessionId ?? null;
}

function controlledRestartPageBinding(window, pageId) {
  return window.pageBindings?.find((binding) => binding.pageId === pageId) ?? null;
}

function controlledRestartEndpoint(url, window, pageBinding) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  if (parsed.origin !== window.origin || parsed.hash) return null;
  if (parsed.pathname === "/api/v1/workspace/files" && parsed.search === "?scope=workspace") return "workspace-files";
  if (parsed.pathname === "/api/v1/workspace" && !parsed.search) return "workspace-bootstrap";
  if (parsed.pathname === "/api/v1/session-catalog/children" && parsed.search === "?limit=100") return "catalog-children";
  if (parsed.pathname === "/api/v1/workspace/observations/stream" && !parsed.search) return "observations";
  if (parsed.pathname === "/api/v1/session-catalog/events" && parsed.search === "?after=0&limit=1000") return "catalog-events";
  if (parsed.pathname === "/api/v1/session-catalog/snapshot" && !parsed.search) return "catalog-snapshot";
  const sessionId = sessionTargetFromUrl(url);
  if (!sessionId || !pageBinding.sessionIds.includes(sessionId) || parsed.search) return null;
  if (parsed.pathname === `/api/v1/sessions/${sessionId}/history`) return "history";
  if (parsed.pathname === `/api/v1/sessions/${sessionId}/main-thread`
      || parsed.pathname === `/api/v1/sessions/${sessionId}/file-tree-settings`
      || parsed.pathname === `/api/v1/sessions/${sessionId}/goal`
      || parsed.pathname === `/api/v1/sessions/${sessionId}/bootstrap`
      || parsed.pathname === `/api/v1/session-catalog/breadcrumb/${sessionId}`) return "session-read";
  return null;
}

function validControlledRestartWindow(window) {
  const restart = window.backendRestart;
  const pageBindings = window.pageBindings;
  const expectedPageRoles = ["copy-request", "foreground-before-restart", "foreground-after-restart"];
  const actualPageRoles = Array.isArray(pageBindings) ? pageBindings.map((binding) => binding.role) : [];
  let gatewayOrigin;
  try {
    gatewayOrigin = new URL(window.origin);
  } catch {
    return false;
  }
  return typeof window.operationId === "string" && window.operationId.length > 0
    && window.operationId === window.captureOperationId
    && ["copy.source.partial", "copy.source.before_captured_cas"].includes(window.phase)
    && typeof window.sourceSessionId === "string" && window.sourceSessionId.length > 0
    && typeof window.sourceThreadId === "string" && window.sourceThreadId.length > 0
    && typeof window.targetSessionId === "string" && window.targetSessionId.length > 0
    && typeof window.activeSessionId === "string" && window.activeSessionId.length > 0
    && typeof window.workspaceId === "string" && window.workspaceId.length > 0
    && typeof window.origin === "string" && gatewayOrigin.origin === window.origin
    && typeof window.targetName === "string" && window.targetName.length > 0
    && Number.isInteger(window.gatewayPort) && window.gatewayPort > 0
    && Number(gatewayOrigin.port) === window.gatewayPort
    && Array.isArray(pageBindings) && pageBindings.length === expectedPageRoles.length
    && new Set(actualPageRoles).size === expectedPageRoles.length
    && expectedPageRoles.every((role) => actualPageRoles.includes(role))
    && pageBindings.every((binding) => Number.isInteger(binding.pageId)
      && Array.isArray(binding.documentIds) && binding.documentIds.length > 0
      && binding.documentIds.every((documentId) => Number.isInteger(documentId) && documentId > 0)
      && Array.isArray(binding.sessionIds) && binding.sessionIds.every((sessionId) => typeof sessionId === "string" && sessionId.length > 0))
    && new Set(pageBindings.map((binding) => binding.pageId)).size === pageBindings.length
    && restart?.targetName === window.targetName
    && Number.isInteger(restart.port) && restart.port > 0
    && restart.port !== window.gatewayPort
    && Number.isInteger(restart.beforePid) && restart.beforePid > 1
    && Number.isInteger(restart.afterPid) && restart.afterPid > 1
    && restart.beforePid !== restart.afterPid
    && Number.isFinite(restart.stopRequestedAt) && Number.isFinite(restart.stoppedAt)
    && Number.isFinite(restart.startRequestedAt) && Number.isFinite(restart.startedAt)
    && window.startedAt <= restart.stopRequestedAt && restart.stopRequestedAt <= restart.stoppedAt
    && restart.stoppedAt <= restart.startRequestedAt && restart.startRequestedAt <= restart.startedAt
    && Number.isFinite(window.startedAt) && Number.isFinite(window.endedAt)
    && window.endedAt >= restart.startedAt;
}

function hasConnectedObservationTargets(response, subscriptions) {
  // subscription_id 属于一次登记；重建后核同一领域目标和过滤范围，并绑定新 ID 的 connected 回执。
  return subscriptions.length > 0 && subscriptions.every((original) => {
    if (!["session_activity", "session_trace", "workspace_files"].includes(original.kind)) return false;
    const recovered = response.subscriptions?.filter((candidate) => candidate.kind === original.kind
      && (candidate.session_id ?? null) === (original.session_id ?? null)
      && JSON.stringify(candidate.paths ?? []) === JSON.stringify(original.paths ?? [])) ?? [];
    return recovered.length === 1 && response.connectedSubscriptions?.includes(recovered[0].subscription_id);
  });
}

function classifyControlledRestartErrors(consoleErrorFacts, consoleErrors, failedRequests, requestFacts, responseFacts, restartWindows) {
  const handled = [];
  const consumedConsoleIndexes = new Set();
  const consumedFailureIndexes = new Set();
  const consumedResponses = new Set();
  const recordResponseError = (error, window, endpoint, request, response) => {
    consumedResponses.add(response);
    consumedConsoleIndexes.add(error.index);
    handled.push({ consoleIndex: error.index, requestFactId: request.requestFactId,
      responseRequestId: response.requestId, responseBodyRequestId: response.bodyRequestId,
      method: response.method, url: response.url, requestBody: request.body,
      responseBody: response.body, requestedAt: response.requestedAt, receivedAt: response.receivedAt,
      operationId: window.operationId, phase: window.phase, sourceSessionId: window.sourceSessionId,
      sourceThreadId: window.sourceThreadId, targetSessionId: window.targetSessionId,
      activeSessionId: window.activeSessionId, workspaceId: window.workspaceId,
      pageId: response.pageId, documentId: response.documentId, pageRole: response.pageRole,
      targetName: window.targetName, gatewayOrigin: window.origin, gatewayPort: window.gatewayPort,
      backendPort: window.backendRestart.port, backendBeforePid: window.backendRestart.beforePid,
      backendAfterPid: window.backendRestart.afterPid, endpoint });
  };
  for (const error of consoleErrorFacts) {
    const matchingWindows = restartWindows.filter((candidate) => candidate.operationId === error.restartWindowId);
    if (matchingWindows.length !== 1) continue;
    const [window] = matchingWindows;
    const pageBinding = controlledRestartPageBinding(window, error.pageId);
    if (!validControlledRestartWindow(window)
        || !Number.isInteger(error.index) || consumedConsoleIndexes.has(error.index)
        || !pageBinding || pageBinding.role !== error.pageRole || !pageBinding.documentIds.includes(error.documentId)
        || !Number.isFinite(error.receivedAt) || error.receivedAt < window.startedAt || error.receivedAt > window.endedAt) continue;
    const endpoint = controlledRestartEndpoint(error.url, window, pageBinding);
    if (!endpoint) continue;
    const isBadGateway = error.text === "Failed to load resource: the server responded with a status of 502 (Bad Gateway)";
    const isObservationsPostBadGateway = endpoint === "observations" && isBadGateway;
    const isIncompleteObservationStream = endpoint === "observations"
      && error.text === "Failed to load resource: net::ERR_INCOMPLETE_CHUNKED_ENCODING";
    if (isObservationsPostBadGateway) {
      const observationsTarget = `/api/v1/workspace/observations/stream`;
      const detailPrefix = `无法连接工作区后端 http://127.0.0.1:${window.backendRestart.port}${observationsTarget}: `;
      const responses = responseFacts.filter((response) => !consumedResponses.has(response)
        && response.restartWindowId === window.operationId && response.method === "POST" && response.status === 502
        && typeof error.cdpRequestId === "string" && error.cdpRequestId.length > 0
        && response.cdpRequestId === error.cdpRequestId
        && response.url === error.url && response.pageId === error.pageId && response.documentId === error.documentId
        && response.pageRole === pageBinding.role && typeof response.requestFactId === "string"
        && response.requestFactId.length > 0 && typeof response.requestId === "string" && response.requestId.length > 0
        && response.body && typeof response.body === "object" && response.body.request_id === response.requestId
        && response.bodyRequestId === response.requestId && response.bodyError === undefined
        && typeof response.body.detail === "string" && response.body.detail.startsWith(detailPrefix)
        && response.workspaceHeader === window.workspaceId
        && Number.isFinite(response.requestedAt) && Number.isFinite(response.receivedAt)
        && response.requestedAt <= response.receivedAt && response.receivedAt >= window.backendRestart.stopRequestedAt
        && response.receivedAt <= window.backendRestart.startedAt
        && Math.abs(response.receivedAt - error.receivedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (responses.length !== 1) continue;
      const [response] = responses;
      if (responseFacts.filter((candidate) => candidate.requestFactId === response.requestFactId).length !== 1) continue;
      const requests = requestFacts.filter((request) => request.requestFactId === response.requestFactId
        && request.restartWindowId === window.operationId && request.url === response.url
        && request.method === "POST" && request.pageId === response.pageId
        && request.documentId === response.documentId && request.pageRole === response.pageRole
        && request.requestedAt === response.requestedAt && request.workspaceHeader === response.workspaceHeader
        && request.bodyError === undefined && request.body && typeof request.body === "object"
        && !Array.isArray(request.body) && Array.isArray(request.body.subscriptions)
        && request.body.subscriptions.length > 0);
      if (requests.length !== 1
          || requestFacts.filter((candidate) => candidate.requestFactId === response.requestFactId).length !== 1) continue;
      const [request] = requests;
      const competingErrors = consoleErrorFacts.filter((candidate) =>
        candidate.text === error.text && candidate.restartWindowId === window.operationId
        && candidate.cdpRequestId === error.cdpRequestId
        && candidate.pageId === response.pageId && candidate.documentId === response.documentId
        && candidate.pageRole === response.pageRole && candidate.url === response.url
        && Number.isInteger(candidate.index) && Number.isFinite(candidate.receivedAt)
        && candidate.receivedAt >= window.startedAt && candidate.receivedAt <= window.endedAt
        && Math.abs(candidate.receivedAt - response.receivedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (competingErrors.length !== 1 || competingErrors[0] !== error) continue;
      recordResponseError(error, window, "observations-post-502", request, response);
      continue;
    }
    if (isBadGateway) {
      const detailPrefix = `无法连接工作区后端 http://127.0.0.1:${window.backendRestart.port}${new URL(error.url).pathname}: `;
      const responses = responseFacts.filter((response) => !consumedResponses.has(response)
        && response.restartWindowId === window.operationId
        && response.method === (endpoint === "history" ? "POST" : "GET") && response.status === 502
        && typeof error.cdpRequestId === "string" && error.cdpRequestId.length > 0
        && response.cdpRequestId === error.cdpRequestId
        && response.url === error.url && response.pageId === error.pageId && response.documentId === error.documentId
        && response.pageRole === pageBinding.role && typeof response.requestId === "string" && response.requestId.length > 0
        && typeof response.requestFactId === "string" && response.requestFactId.length > 0
        && response.body && typeof response.body === "object" && response.body.request_id === response.requestId
        && response.bodyRequestId === response.requestId && response.bodyError === undefined
        && typeof response.body.detail === "string" && response.body.detail.startsWith(detailPrefix)
        && response.workspaceHeader === window.workspaceId
        && Number.isFinite(response.requestedAt) && Number.isFinite(response.receivedAt)
        && response.requestedAt <= response.receivedAt && response.receivedAt >= window.backendRestart.stopRequestedAt
        && response.receivedAt <= window.backendRestart.startedAt
        && Math.abs(response.receivedAt - error.receivedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (responses.length !== 1) continue;
      const [response] = responses;
      if (responseFacts.filter((candidate) => candidate.requestFactId === response.requestFactId).length !== 1) continue;
      const requests = requestFacts.filter((request) => request.requestFactId === response.requestFactId
        && typeof request.requestFactId === "string" && request.requestFactId.length > 0
        && request.restartWindowId === window.operationId && request.url === response.url
        && request.method === response.method && request.pageId === response.pageId
        && request.documentId === response.documentId && request.pageRole === response.pageRole
        && request.requestedAt === response.requestedAt && request.workspaceHeader === response.workspaceHeader
        && request.bodyError === undefined);
      if (requests.length !== 1
          || requestFacts.filter((candidate) => candidate.requestFactId === response.requestFactId).length !== 1) continue;
      const [request] = requests;
      const competingErrors = consoleErrorFacts.filter((candidate) =>
        candidate.text === error.text && candidate.restartWindowId === window.operationId
        && candidate.cdpRequestId === error.cdpRequestId
        && candidate.pageId === response.pageId && candidate.documentId === response.documentId
        && candidate.pageRole === response.pageRole
        && candidate.url === response.url && Number.isInteger(candidate.index)
        && Number.isFinite(candidate.receivedAt) && candidate.receivedAt >= window.startedAt
        && candidate.receivedAt <= window.endedAt
        && Math.abs(candidate.receivedAt - response.receivedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (competingErrors.length !== 1 || competingErrors[0] !== error) continue;
      recordResponseError(error, window, endpoint, request, response);
      continue;
    }
    if (!isIncompleteObservationStream) continue;
    const failures = failedRequests.map((failure, index) => ({ failure, index })).filter(({ failure, index }) =>
      !consumedFailureIndexes.has(index) && failure.error === "net::ERR_INCOMPLETE_CHUNKED_ENCODING"
      && failure.method === "POST"
      && failure.restartWindowId === window.operationId && failure.pageRole === pageBinding.role
      && failure.url === error.url && failure.pageId === error.pageId && failure.documentId === error.documentId
      && Number.isFinite(failure.requestedAt) && Number.isFinite(failure.failedAt)
      && failure.requestedAt <= window.startedAt && failure.failedAt >= window.backendRestart.stopRequestedAt
      && failure.failedAt <= window.backendRestart.startedAt && Math.abs(failure.failedAt - error.receivedAt) <= 5_000);
    if (failures.length !== 1) continue;
    const [{ failure, index }] = failures;
    const requests = requestFacts.filter((request) => request.requestFactId === failure.requestFactId
      && request.method === "POST" && request.url === error.url && request.pageId === error.pageId
      && request.documentId === error.documentId
      && (request.restartWindowId === null || request.restartWindowId === window.operationId)
      && request.workspaceHeader === window.workspaceId && request.requestedAt <= window.startedAt
      && request.body && typeof request.body === "object" && Array.isArray(request.body.subscriptions)
      && request.body.subscriptions.length > 0
      && request.body.subscriptions.every((subscription) => typeof subscription.subscription_id === "string"
        && subscription.subscription_id.length > 0)
      && requestFacts.filter((candidate) => candidate.requestFactId === request.requestFactId).length === 1);
    if (requests.length !== 1) continue;
    const [request] = requests;
    const recoveries = responseFacts.filter((response) => response.method === "POST" && response.status === 200
      && response.url === error.url && response.pageId === error.pageId && response.documentId === error.documentId
      && response.restartWindowId === window.operationId && response.pageRole === pageBinding.role
      && response.workspaceHeader === window.workspaceId
      && Number.isFinite(response.requestedAt) && response.requestedAt >= window.backendRestart.startedAt
      && Number.isFinite(response.receivedAt) && response.receivedAt >= response.requestedAt
      && response.receivedAt <= window.endedAt
      && typeof response.requestId === "string" && response.requestId.length > 0
      && typeof response.contentType === "string"
      && response.contentType.split(";", 1)[0].trim().toLowerCase() === "text/event-stream"
      && responseFacts.filter((candidate) => candidate.requestId === response.requestId).length === 1
      && Array.isArray(response.connectedSubscriptions)
      && hasConnectedObservationTargets(response, request.body.subscriptions))
      .sort((left, right) => left.requestedAt - right.requestedAt);
    if (recoveries.length === 0) continue;
    const competingErrors = consoleErrorFacts.filter((candidate) => candidate.text === error.text
      && candidate.url === error.url && candidate.pageId === error.pageId && candidate.documentId === error.documentId
      && Number.isFinite(candidate.receivedAt) && candidate.receivedAt >= window.backendRestart.stopRequestedAt
      && candidate.receivedAt <= window.backendRestart.startedAt);
    if (competingErrors.length !== 1) continue;
    consumedFailureIndexes.add(index);
    consumedConsoleIndexes.add(error.index);
    handled.push({ consoleIndex: error.index, failedRequestIndex: index, requestFactId: request.requestFactId,
      recoveryRequestId: recoveries[0].requestId, recoveredSubscriptions: recoveries[0].subscriptions,
      operationId: window.operationId,
      phase: window.phase, sourceSessionId: window.sourceSessionId, sourceThreadId: window.sourceThreadId,
      targetSessionId: window.targetSessionId, activeSessionId: window.activeSessionId, endpoint,
      targetName: window.targetName, backendPort: window.backendRestart.port,
      backendBeforePid: window.backendRestart.beforePid, backendAfterPid: window.backendRestart.afterPid,
      pageId: error.pageId, documentId: error.documentId, pageRole: pageBinding.role,
      requestedAt: failure.requestedAt, failedAt: failure.failedAt });
  }
  return {
    handled,
    consumedConsoleIndexes,
    consumedFailureIndexes,
    unhandledConsoleErrors: consoleErrors.filter((_text, index) => !consumedConsoleIndexes.has(index)),
    unhandledFailedRequests: failedRequests.filter((_failure, index) => !consumedFailureIndexes.has(index)),
  };
}

function classifyForkAttachmentDeletionConflicts(consoleErrorFacts, conflictResponses, raceFacts) {
  const handled = [];
  const consumedResponses = new Set();
  const consumedRaces = new Set();
  for (const error of consoleErrorFacts) {
    if (error.text !== "Failed to load resource: the server responded with a status of 409 (Conflict)"
        || typeof error.url !== "string" || !error.url) continue;
    const responses = conflictResponses.filter((response) => response.status === 409
      && response.method === "POST" && response.kind === "fork_context"
      && response.url === error.url && response.pageId === error.pageId
      && response.documentId === error.documentId
      && typeof response.requestId === "string" && response.requestId.length > 0
      && response.body?.request_id === response.requestId
      && response.bodyError === undefined
      && Number.isFinite(response.requestedAt) && Number.isFinite(response.receivedAt)
      && response.receivedAt >= response.requestedAt
      && typeof response.workspaceHeader === "string" && response.workspaceHeader.length > 0
      && response.forkRequest && typeof response.forkRequest.idempotency_key === "string"
      && response.forkRequestBodyError === undefined);
    if (responses.length !== 1) continue;
    const [response] = responses;
    if (consumedResponses.has(response)) continue;
    const match = /^https?:\/\/[^/]+\/api\/v1\/sessions\/([^/]+)\/fork-context$/.exec(response.url);
    if (!match || match[1] !== response.sessionId) continue;
    const operationId = forkOperationId(response.forkRequest);
    const matchingRaces = raceFacts.filter((race) => {
      const detail = `Session catalog 节点非 active，拒绝附件准入/发布: session_id=${race.targetSessionId}, state='deleting'`;
      const requestOwner = /\/resources\/session\/([^/]+)\/threads\/([^/]+)$/.exec(race.request?.source_uri ?? "");
      return race.finalizerFirst === false
        && race.operationId === operationId
        && race.request?.idempotency_key === response.forkRequest.idempotency_key
        && JSON.stringify(race.request) === JSON.stringify(response.forkRequest)
        && race.capture?.copy_operation_id === operationId
        && race.capture?.source_session_id === race.sourceSessionId
        && race.capture?.source_thread_id === race.sourceThreadId
        && race.capture?.target_session_id === race.targetSessionId
        && race.capture?.target_thread_id === race.targetThreadId
        && requestOwner?.[1] === race.sourceSessionId
        && requestOwner?.[2] === race.sourceThreadId
        && match[1] === race.sourceSessionId
        && response.sessionId === race.sourceSessionId
        && response.workspaceHeader === race.workspaceId
        && response.pageId === error.pageId && response.documentId === error.documentId
        && response.pageId === race.requestPageId && response.documentId === race.requestDocumentId
        && race.deletion?.intent?.kind === "delete_session"
        && race.deletion.intent.target_node_id === race.targetSessionId
        && race.initialBarrier?.phase === "copy.attachments.before_finalize"
        && race.initialBarrier.operation_id === operationId
        && race.deletionCatalogBarrier?.phase === "deletion.catalog_deleting"
        && race.deletionCatalogBarrier.operation_id === race.deletion.operationId
        && race.deletionDrainBarrier?.phase === `deletion.drain_waiting/${race.targetSessionId}`
        && race.deletionDrainBarrier.operation_id === race.deletion.operationId
        && Number.isFinite(race.requestStartedAt) && Number.isFinite(response.requestedAt)
        && response.requestedAt >= race.requestStartedAt
        && Number.isFinite(race.deletionCatalogAt) && response.requestedAt <= race.deletionCatalogAt
        && Number.isFinite(race.initialReleasedAt) && race.initialReleasedAt >= race.deletionCatalogAt
        && Number.isFinite(race.responseReceivedAt) && response.receivedAt <= race.responseReceivedAt
        && race.responseReceivedAt >= race.initialReleasedAt
        && Number.isFinite(race.deletionDrainReleasedAt) && race.deletionDrainReleasedAt >= race.responseReceivedAt
        && race.response?.status === 409
        && race.response?.requestId === response.requestId
        && race.response?.body?.request_id === response.body.request_id
        && race.response?.body?.detail === detail
        && response.body.detail === detail;
    });
    if (matchingRaces.length !== 1) continue;
    const [race] = matchingRaces;
    if (consumedRaces.has(race)) continue;
    consumedResponses.add(response);
    consumedRaces.add(race);
    handled.push({ consoleIndex: error.index, responseRequestId: response.requestId,
      operationId, sourceSessionId: race.sourceSessionId, targetSessionId: race.targetSessionId,
      deletionOperationId: race.deletion.operationId });
  }
  return handled;
}

function runMessageStreamDiagnosticsSelfTest() {
  const url = "http://127.0.0.1:19000/api/v1/sessions/ses_owner/turns/turn_appended/message-stream?thread_id=thr_owner&after_seq=0";
  const owner = {
    url: "http://127.0.0.1:19000/api/v1/sessions/ses_owner/messages",
    method: "POST", status: 200, pageId: 1, documentId: 2, kind: "admission",
    sessionId: "ses_owner", threadId: null, workspaceHeader: "workspace-owner",
    requestId: "request-admission", requestedAt: 10, receivedAt: 20,
    body: { request_id: "request-admission", data: { job_id: "job_appended", turn_id: "turn_appended", thread_id: "thr_owner", status: "running",
      dispatch: { session_id: "ses_owner", thread_id: "thr_owner", job_id: "job_appended", job_status: "running" } } },
  };
  const refusal = {
    url, method: "GET", status: 404, pageId: 1, documentId: 2, sessionId: "ses_owner", turnId: "turn_appended",
    threadId: "thr_owner", afterSeq: "0", workspaceHeader: "workspace-owner", requestId: "request-refusal",
    requestedAt: 30, receivedAt: 40,
    body: { detail: "消息流不存在: session_id=ses_owner turn_id=turn_appended", request_id: "request-refusal" },
  };
  const recovery = {
    url, method: "GET", status: 200, pageId: 1, documentId: 2, sessionId: "ses_owner", turnId: "turn_appended",
    threadId: "thr_owner", afterSeq: "0", workspaceHeader: "workspace-owner", requestId: "request-recovery", messageStreamId: "stream-appended",
    headers: { contentType: "text/event-stream; charset=utf-8" },
    requestedAt: 50, receivedAt: 60,
  };
  const basicOwner = { page: { diagnosticId: 1 }, sessionId: "ses_owner", threadId: "thr_owner",
    gatewayWorkspaceId: "workspace-owner" };
  const trace = { pageId: 1, documentId: 2, status: 200, workspaceHeader: "workspace-owner",
    contentType: "text/event-stream; charset=utf-8", requestId: "trace-request", cdpRequestId: "cdp-trace",
    subscriptions: [{ kind: "session_trace", session_id: "ses_owner", subscription_id: "trace-1" }],
    connectedSubscriptions: ["trace-1"] };
  assert.equal(basicStreamConnections(basicOwner, [trace, { ...trace, requestId: "physical-reconnect" }], [recovery]).trace.length, 1);
  assert.equal(basicStreamConnections(basicOwner, [trace, { ...trace, documentId: 3 }], [recovery]).trace.length, 2);
  for (const changed of [{ pageId: 2 }, { workspaceHeader: "other" }, { status: 502 },
    { connectedSubscriptions: [] }, { requestId: null }, { cdpRequestId: null },
    { subscriptions: [{ kind: "session_trace", session_id: "other", subscription_id: "trace-1" }] }]) {
    assert.equal(basicStreamConnections(basicOwner, [{ ...trace, ...changed }], []).trace.length, 0);
  }
  assert.equal(basicStreamConnections(basicOwner, [trace], [recovery]).message.length, 1);
  assert.equal(basicStreamConnections(basicOwner, [trace], [{ ...recovery, threadId: "other" }]).message.length, 0);
  const consoleError = { index: 0, text: "Failed to load resource: the server responded with a status of 404 (Not Found)",
    url, pageId: 1, documentId: 2 };
  const classify = (responses = [refusal, recovery], admissions = [owner], errors = [consoleError], texts = [consoleError.text]) =>
    classifyMessageStreamErrors(errors, texts, responses, admissions);

  assert.equal(classify().handledMessageStreamErrors.length, 1, "精确匹配 admission、404 与后续 SSE 应分类为瞬态");
  assert.equal(classify().unhandledConsoleErrors.length, 0);
  const threadlessUrl = "http://127.0.0.1:19000/api/v1/sessions/ses_owner/turns/turn_appended/message-stream?after_seq=0";
  const threadlessRefusal = { ...refusal, url: threadlessUrl, threadId: null };
  const threadlessRecovery = { ...recovery, url: threadlessUrl, threadId: null };
  const threadlessConsoleError = { ...consoleError, url: threadlessUrl };
  const classifyThreadless = (responses = [threadlessRefusal, threadlessRecovery], admissions = [owner]) =>
    classify(responses, admissions, [threadlessConsoleError], [threadlessConsoleError.text]);
  const threadlessResult = classifyThreadless();
  assert.equal(threadlessResult.handledMessageStreamErrors.length, 1,
    "缺少 thread_id 的 404 只能由唯一 running admission 和后续无 thread_id SSE 恢复");
  assert.equal(threadlessResult.handledMessageStreamErrors[0].threadId, "thr_owner",
    "缺失的 stream thread 必须从 admission owner 恢复");
  assert.equal(classifyThreadless([threadlessRefusal, { ...threadlessRecovery,
    url: `${threadlessUrl}&thread_id=thr_owner`, threadId: "thr_owner" }]).unhandledConsoleErrors.length, 0,
  "缺失 thread_id 的拒绝允许后续 SSE 显式携带 admission owner thread");
  assert.equal(classifyThreadless([threadlessRefusal]).unhandledConsoleErrors.length, 1,
    "缺失 thread_id 的拒绝没有后续 SSE 必须保留");
  assert.equal(classifyThreadless([threadlessRefusal, threadlessRecovery], []).unhandledConsoleErrors.length, 1,
    "缺失 thread_id 的拒绝没有 owner admission 必须保留");
  assert.equal(classifyThreadless([threadlessRefusal, threadlessRecovery], [owner, { ...owner,
    requestId: "request-admission-duplicate", body: { ...owner.body, request_id: "request-admission-duplicate" },
  }]).unhandledConsoleErrors.length, 1, "多个 owner admission 必须保留为未处理错误");
  const conflictingAdmission = { ...owner,
    url: "http://127.0.0.1:19000/api/v1/sessions/ses_owner/threads/thr_other/messages",
    threadId: "thr_other",
  };
  assert.equal(classifyThreadless([threadlessRefusal, threadlessRecovery], [conflictingAdmission]).unhandledConsoleErrors.length, 1,
    "准入路径 thread 与响应 body owner 冲突必须保留");
  assert.equal(classifyThreadless([threadlessRefusal, threadlessRecovery], [{ ...owner, body: { ...owner.body, data: {
    ...owner.body.data, thread_id: "thr_other",
  } } }]).unhandledConsoleErrors.length, 1, "准入 body thread 与 dispatch owner 冲突必须保留");
  assert.equal(classifyThreadless([threadlessRefusal, { ...threadlessRecovery,
    url: `${threadlessUrl}&thread_id=thr_other`, threadId: "thr_other",
  }]).unhandledConsoleErrors.length, 1, "后续 SSE thread 与 admission owner 冲突必须保留");
  const otherUrl = "http://127.0.0.1:19000/api/v1/sessions/ses_other/turns/turn_other/message-stream?thread_id=thr_other&after_seq=0";
  const otherOwner = {
    ...owner,
    url: "http://127.0.0.1:19000/api/v1/sessions/ses_other/messages",
    pageId: 3,
    documentId: 4,
    sessionId: "ses_other",
    workspaceHeader: "workspace-other",
    requestId: "request-admission-other",
    body: { request_id: "request-admission-other", data: { ...owner.body.data, job_id: "job_other", turn_id: "turn_other",
      thread_id: "thr_other", dispatch: { ...owner.body.data.dispatch, session_id: "ses_other", thread_id: "thr_other", job_id: "job_other" } } },
  };
  const otherRefusal = {
    ...refusal,
    url: otherUrl,
    pageId: 3,
    documentId: 4,
    sessionId: "ses_other",
    turnId: "turn_other",
    threadId: "thr_other",
    workspaceHeader: "workspace-other",
    body: { detail: "消息流不存在: session_id=ses_other turn_id=turn_other", request_id: refusal.requestId },
  };
  const otherRecovery = { ...recovery, url: otherUrl, pageId: 3, documentId: 4, sessionId: "ses_other", turnId: "turn_other",
    threadId: "thr_other", workspaceHeader: "workspace-other", requestId: "request-recovery-other", messageStreamId: "stream-other" };
  const otherConsoleError = { ...consoleError, index: 1, url: otherUrl, pageId: 3, documentId: 4 };
  const duplicateRequestIdResult = classify(
    [refusal, recovery, otherRefusal, otherRecovery], [owner, otherOwner], [consoleError, otherConsoleError],
    [consoleError.text, otherConsoleError.text],
  );
  assert.equal(otherRefusal.requestId, refusal.requestId, "两个独立 404 响应可复用 request ID");
  assert.equal(duplicateRequestIdResult.handledMessageStreamErrors.length, 2,
    "不同 page/document/URL 的合法 404 即使 request ID 相同也都应分类");
  assert.equal(duplicateRequestIdResult.unhandledConsoleErrors.length, 0);
  assert.equal(classify([refusal, recovery], []).unhandledConsoleErrors.length, 1, "缺 admission 必须保留为未处理错误");
  assert.equal(classify([refusal, { ...recovery, threadId: "thr_other" }]).unhandledConsoleErrors.length, 1,
    "后续 SSE owner thread 错误必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, body: { ...owner.body, data: {
    ...owner.body.data, thread_id: "thr_other", dispatch: { ...owner.body.data.dispatch, thread_id: "thr_other" },
  } } }]).unhandledConsoleErrors.length, 1, "准入 owner thread 错误必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, body: { ...owner.body, data: {
    ...owner.body.data, job_id: "job_other",
  } } }]).unhandledConsoleErrors.length, 1, "准入 job target 不一致必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, workspaceHeader: "workspace_other" }]).unhandledConsoleErrors.length, 1,
    "准入 workspace 不一致必须保留");
  assert.equal(classify([refusal]).unhandledConsoleErrors.length, 1, "缺后续 200 SSE 必须保留");
  assert.equal(classify([refusal, { ...recovery, headers: { contentType: "application/json" } }]).unhandledConsoleErrors.length, 1,
    "后续 200 非 SSE 响应必须保留");
  assert.equal(classify([{ ...refusal, body: { ...refusal.body, request_id: "request-other" } }, recovery])
    .unhandledConsoleErrors.length, 1, "404 body/header request ID 不一致必须保留");
  assert.equal(classify([{ ...refusal, body: { detail: refusal.body.detail } }, recovery])
    .unhandledConsoleErrors.length, 1, "404 body 缺少 request ID 必须保留");
  assert.equal(classify([{ ...refusal, body: { detail: "消息流不存在: session_id=ses_owner turn_id=turn_missing" } }, recovery])
    .unhandledConsoleErrors.length, 1, "缺失 Turn 或其它 detail 必须保留");
  assert.equal(classify([refusal, { ...recovery, messageStreamId: "" }]).unhandledConsoleErrors.length, 1,
    "缺 X-Message-Stream-ID 必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, body: { ...owner.body, request_id: "request-other" } }])
    .unhandledConsoleErrors.length, 1, "admission body/header request ID 不一致必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, status: 404 }]).unhandledConsoleErrors.length, 1,
    "准入不是 HTTP 200 必须保留");
  assert.equal(classify([refusal, recovery], [{ ...owner, body: { ...owner.body, data: {
    ...owner.body.data, status: "queued", dispatch: { ...owner.body.data.dispatch, job_status: "queued" },
  } } }]).unhandledConsoleErrors.length, 1, "非 running 准入必须保留");
  assert.equal(classify([refusal, { ...recovery, documentId: 3 }]).unhandledConsoleErrors.length, 1,
    "跨 document 的后续 SSE 必须保留");
  assert.equal(classify([refusal, { ...recovery, workspaceHeader: "workspace_other" }]).unhandledConsoleErrors.length, 1,
    "后续 SSE workspace 不一致必须保留");
  assert.equal(classify([refusal, { ...recovery, requestId: null }]).unhandledConsoleErrors.length, 1,
    "后续 SSE 缺 request ID 必须保留");
  assert.equal(classify([refusal, recovery], [owner], [{ ...consoleError, pageId: 2 }], [consoleError.text])
    .unhandledConsoleErrors.length, 1, "跨 page 的 console error 不得借用响应证据");
  assert.equal(classify([refusal, recovery], [owner], [{ ...consoleError, url: `${url}&turn_stream_id=other` }],
    [consoleError.text]).unhandledConsoleErrors.length, 1, "console URL 必须精确匹配实际 404 URL");
  assert.equal(classify([refusal, recovery], [owner], [{ ...consoleError, url: url.replace("&after_seq=0", "") }],
    [consoleError.text]).unhandledConsoleErrors.length, 1, "初始 stream 之外的游标必须保持未处理");
  const duplicate = { ...consoleError, index: 1 };
  const duplicateResult = classify([refusal, recovery], [owner], [consoleError, duplicate], [consoleError.text, duplicate.text]);
  assert.equal(duplicateResult.handledMessageStreamErrors.length, 1, "单个 404 response fact 只能消费一次");
  assert.equal(duplicateResult.unhandledConsoleErrors.length, 1, "重复 console error 不能借用已消费 response");

  const chunkedUrl = "http://127.0.0.1:19000/api/v1/sessions/ses_owner/turns/turn_appended/message-stream?thread_id=thr_owner&turn_stream_id=stream-appended&after_seq=20";
  const interruptedResponse = { url: chunkedUrl, method: "GET", status: 200, pageId: 1, documentId: 2,
    sessionId: "ses_owner", turnId: "turn_appended", threadId: "thr_owner", turnStreamId: "stream-appended",
    afterSeq: "20", workspaceHeader: "workspace-owner", requestId: "request-stream-interrupted",
    messageStreamId: "stream-appended", requestedAt: 70, receivedAt: 80,
    headers: { contentType: "text/event-stream; charset=utf-8" } };
  const reconnect = { ...interruptedResponse, requestId: "request-stream-reconnect", requestedAt: 100, receivedAt: 110 };
  const failedRequest = { url: chunkedUrl, error: "net::ERR_INCOMPLETE_CHUNKED_ENCODING", pageId: 1, documentId: 2,
    requestedAt: 70, failedAt: 90, responseRequestId: "request-stream-interrupted", status: 200,
    sessionId: "ses_owner", turnId: "turn_appended", threadId: "thr_owner", turnStreamId: "stream-appended",
    afterSeq: "20", workspaceHeader: "workspace-owner", messageStreamId: "stream-appended" };
  const chunkedConsoleError = { index: 0, text: "Failed to load resource: net::ERR_INCOMPLETE_CHUNKED_ENCODING",
    url: chunkedUrl, pageId: 1, documentId: 2, receivedAt: 90 };
  const classifyChunked = (responses = [interruptedResponse, reconnect], admissions = [owner], failures = [failedRequest], errors = [chunkedConsoleError]) =>
    classifyIncompleteMessageStreamErrors(errors, failures, responses, admissions);
  const assertChunkedIsUnhandled = (responses, admissions, failures, errors, message) => {
    const classified = classifyChunked(responses, admissions, failures, errors);
    assert.equal(classified.unhandledFailedRequests.length, 1, message);
    assert.equal(classified.consumedConsoleIndexes.size, 0, `${message}；对应 console error 也必须保留`);
  };
  const recoveredChunked = classifyChunked();
  assert.equal(recoveredChunked.handled.length, 1,
    "相同 page/document、owner、workspace、stream ID 的后续 SSE 重连才允许分类 chunked 中断");
  assert.deepEqual([...recoveredChunked.consumedConsoleIndexes], [0]);
  assert.deepEqual(recoveredChunked.unhandledFailedRequests, []);
  assertChunkedIsUnhandled([interruptedResponse], [owner], [failedRequest], [chunkedConsoleError],
    "没有后续 SSE 响应时必须保留 chunked 中断");
  assertChunkedIsUnhandled([interruptedResponse, { ...reconnect,
    messageStreamId: "stream-other", turnStreamId: "stream-other",
  }], [owner], [failedRequest], [chunkedConsoleError], "重连必须保持原 stream 身份");
  assertChunkedIsUnhandled([interruptedResponse, { ...reconnect, workspaceHeader: "workspace-other" }],
    [owner], [failedRequest], [chunkedConsoleError], "跨 workspace 重连不能处理原 stream 中断");
  assertChunkedIsUnhandled([interruptedResponse, { ...reconnect, documentId: 3 }],
    [owner], [failedRequest], [chunkedConsoleError], "跨 document 重连不能处理原 stream 中断");
  assertChunkedIsUnhandled([interruptedResponse, reconnect], [], [failedRequest], [chunkedConsoleError],
    "缺少唯一 admission owner 时必须保留 chunked 中断");
  assertChunkedIsUnhandled([interruptedResponse, reconnect], [owner, { ...owner, requestId: "request-admission-duplicate",
    body: { ...owner.body, request_id: "request-admission-duplicate" },
  }], [failedRequest], [chunkedConsoleError], "多个 owner admission 时必须保留 chunked 中断");
  assertChunkedIsUnhandled([interruptedResponse, reconnect], [owner], [{ ...failedRequest,
    responseRequestId: "request-other",
  }], [chunkedConsoleError], "failed request 必须绑定触发它的 stream response request ID");
  assertChunkedIsUnhandled([interruptedResponse, { ...reconnect,
    url: `${chunkedUrl.replace("stream-appended", "stream-other")}`,
  }], [owner], [failedRequest], [chunkedConsoleError], "重连 URL 的 turn_stream_id 必须等于 response owner");
  assertChunkedIsUnhandled([interruptedResponse, { ...reconnect, url: reconnect.url.replace("after_seq=20", "after_seq=10"), afterSeq: "10" }],
    [owner], [failedRequest], [chunkedConsoleError], "重连不能倒退到已中断响应的 cursor 之前");
  const reconnectFailure = { ...failedRequest, requestedAt: reconnect.requestedAt, failedAt: 130,
    responseRequestId: reconnect.requestId };
  const failedReconnect = { ...reconnect, failedAt: 130, failure: "net::ERR_INCOMPLETE_CHUNKED_ENCODING" };
  const secondChunkedConsoleError = { ...chunkedConsoleError, index: 1, receivedAt: 130 };
  const twoAfterTwentyFailures = classifyChunked([interruptedResponse, failedReconnect], [owner],
    [failedRequest, reconnectFailure], [chunkedConsoleError, secondChunkedConsoleError]);
  assert.equal(twoAfterTwentyFailures.handled.length, 0,
    "after_seq=20 的响应 body 也断流时，两个 200 headers 都不能充当恢复证据");
  assert.equal(twoAfterTwentyFailures.unhandledFailedRequests.length, 2,
    "连续两个 after_seq=20 chunked failures 都必须保留为失败");
  assert.deepEqual([...twoAfterTwentyFailures.consumedConsoleIndexes], [],
    "连续断流的 console errors 都必须保留");
  const initialStreamUrl = "http://127.0.0.1:19000/api/v1/sessions/ses_owner/turns/turn_appended/message-stream?after_seq=0";
  const initialStreamResponse = { ...interruptedResponse, url: initialStreamUrl, threadId: null,
    turnStreamId: null, afterSeq: "0", requestId: "request-stream-initial" };
  const initialStreamFailure = { ...failedRequest, url: initialStreamUrl, threadId: null,
    turnStreamId: null, afterSeq: "0", responseRequestId: "request-stream-initial" };
  const brokenInitialRecovery = classifyChunked([initialStreamResponse, failedReconnect], [owner],
    [initialStreamFailure, reconnectFailure], [chunkedConsoleError, secondChunkedConsoleError]);
  assert.equal(brokenInitialRecovery.handled.length, 0,
    "after_seq=0 的原请求只能由 body 正常结束的后续 SSE 响应恢复");
  assert.equal(brokenInitialRecovery.unhandledFailedRequests.length, 2);

  const forkRequest = {
    idempotency_key: "fork-delete-race-key",
    source_uri: "boxteam://gateway/workspace/ws_source/resources/session/ses_source/threads/thr_source",
    mode: "full_rollout_copy",
  };
  const forkOperation = forkOperationId(forkRequest);
  const forkUrl = "http://127.0.0.1:19000/api/v1/sessions/ses_source/fork-context";
  const forkDetail = "Session catalog 节点非 active，拒绝附件准入/发布: session_id=ses_target, state='deleting'";
  const forkRequestId = "request-fork-delete-race";
  const forkResponse = {
    url: forkUrl, method: "POST", status: 409, kind: "fork_context", pageId: 7, documentId: 8,
    sessionId: "ses_source", workspaceHeader: "workspace-source", requestId: forkRequestId,
    requestedAt: 120, receivedAt: 170, body: { request_id: forkRequestId, detail: forkDetail },
    forkRequest: structuredClone(forkRequest),
  };
  const race = {
    finalizerFirst: false, operationId: forkOperation, sourceSessionId: "ses_source", sourceThreadId: "thr_source",
    targetSessionId: "ses_target", targetThreadId: "thr_target", workspaceId: "workspace-source",
    request: structuredClone(forkRequest), requestPageId: 7, requestDocumentId: 8, requestStartedAt: 100,
    response: { status: 409, requestId: forkRequestId, body: structuredClone(forkResponse.body) }, responseReceivedAt: 180,
    initialBarrier: { operation_id: forkOperation, phase: "copy.attachments.before_finalize" },
    deletionCatalogBarrier: { operation_id: "op_delete_target", phase: "deletion.catalog_deleting" },
    deletionCatalogAt: 130, initialReleasedAt: 140,
    deletionDrainBarrier: { operation_id: "op_delete_target", phase: "deletion.drain_waiting/ses_target" },
    deletionDrainReleasedAt: 190,
    capture: { copy_operation_id: forkOperation, source_session_id: "ses_source", source_thread_id: "thr_source",
      target_session_id: "ses_target", target_thread_id: "thr_target" },
    deletion: { operationId: "op_delete_target", intent: { kind: "delete_session", target_node_id: "ses_target" } },
  };
  const forkConsoleError = { index: 0,
    text: "Failed to load resource: the server responded with a status of 409 (Conflict)",
    url: forkUrl, pageId: 7, documentId: 8 };
  const gatewayTimeoutText = "Failed to load resource: the server responded with a status of 504 (Gateway Timeout)";
  const unmatchedChunkedText = "Failed to load resource: net::ERR_INCOMPLETE_CHUNKED_ENCODING";
  const unmatchedChunkedFailure = { url: `${url}&after_seq=20`, error: "net::ERR_INCOMPLETE_CHUNKED_ENCODING",
    pageId: 7, documentId: 8, requestedAt: 200, failedAt: 210 };
  const classifyPipeline = ({ races = [race], responses = [forkResponse], forkErrors = [forkConsoleError],
    chunkedFailure = unmatchedChunkedFailure, streamResponses = [], admissions = [] } = {}) => {
    const consoleErrorFacts = [...forkErrors,
      { index: forkErrors.length, text: gatewayTimeoutText, url: "http://127.0.0.1:19000/api/v1/probe/timeout", pageId: 7, documentId: 8 },
      { index: forkErrors.length + 1, text: unmatchedChunkedText, url: chunkedFailure.url,
        pageId: chunkedFailure.pageId, documentId: chunkedFailure.documentId, receivedAt: chunkedFailure.failedAt }];
    const consoleErrors = consoleErrorFacts.map((fact) => fact.text);
    return classifyConsoleErrors({ consoleErrorFacts, consoleErrors,
      failedRequests: [chunkedFailure], conflictResponses: responses, forkDeletionRaceFacts: races,
      messageStreamResponses: streamResponses, jobOwnerResponses: admissions, historyErrorResponses: [], lifecycleErrorResponses: [],
      lifecycleDeletionProofs: [], turnNavigationMissingTurnProofs: [], historyLiveProofs: [], historyRecoveryResponses: [] });
  };
  const classifiedFork = classifyPipeline();
  assert.equal(classifiedFork.handledForkAttachmentDeletionConflicts.length, 1,
    "完整 console 分类 pipeline 必须识别唯一、同 owner、删除先行的 fork 409");
  assert.equal(classifiedFork.handledForkAttachmentDeletionConflicts[0].responseRequestId, forkRequestId);
  assert.deepEqual(classifiedFork.unhandledConsoleErrors, [gatewayTimeoutText, unmatchedChunkedText],
    "无恢复证据的 504 与 chunked console error 必须继续未处理");
  assert.deepEqual(classifiedFork.unhandledFailedRequests, [unmatchedChunkedFailure],
    "没有有效 SSE 重连配对的 chunked request 必须继续未处理");
  assert.equal(classifyPipeline({ races: [] }).handledForkAttachmentDeletionConflicts.length, 0,
    "没有 race 删除证据时不得分类 fork 409");
  const crossOwnerRace = { ...race, sourceSessionId: "ses_foreign" };
  assert.equal(classifyPipeline({ races: [crossOwnerRace] }).handledForkAttachmentDeletionConflicts.length, 0,
    "fork request、capture 与 race source owner 不一致时不得分类");
  const crossOriginFailure = { ...failedRequest, pageId: 7, documentId: 8 };
  const crossOriginResponses = [
    { ...interruptedResponse, pageId: 7, documentId: 8 },
    { ...reconnect, url: reconnect.url.replace("127.0.0.1:19000", "127.0.0.1:19001"), pageId: 7, documentId: 8 },
  ];
  const crossOriginPipeline = classifyPipeline({ chunkedFailure: crossOriginFailure,
    streamResponses: crossOriginResponses, admissions: [{ ...owner, pageId: 7, documentId: 8 }] });
  assert.deepEqual(crossOriginPipeline.unhandledFailedRequests, [crossOriginFailure],
    "跨 origin 的 SSE recovery 不得处理 chunked failed request");
  assert.ok(crossOriginPipeline.unhandledConsoleErrors.includes(unmatchedChunkedText),
    "跨 origin 的 SSE recovery 不得处理对应 chunked console error");
  const duplicateForkError = { ...forkConsoleError, index: 1 };
  const duplicateFork = classifyPipeline({ forkErrors: [forkConsoleError, duplicateForkError] });
  assert.equal(duplicateFork.handledForkAttachmentDeletionConflicts.length, 1,
    "单个 response/race 证据只能处理一条 fork 409 console error");
  assert.ok(duplicateFork.unhandledConsoleErrors.includes(forkConsoleError.text),
    "重复 fork 409 console error 必须保留一条未处理记录");

  const pageRouteSessionId = "ses_page_route";
  const testRestartWindow = ({ operationId, phase, sourceSessionId, sourceThreadId, targetSessionId, pageIds, index }) => ({
    operationId, captureOperationId: operationId, phase, sourceSessionId, sourceThreadId, targetSessionId,
    activeSessionId: "ses_active", workspaceId: "workspace-a", origin: "http://127.0.0.1:19001",
    targetName: "a", gatewayPort: 19001,
    backendRestart: { targetName: "a", port: 19000, beforePid: 1000 + index, afterPid: 2000 + index,
      stopRequestedAt: 130, stoppedAt: 135, startRequestedAt: 170, startedAt: 180 },
    pageBindings: [
      { pageId: pageIds[0], role: "copy-request", documentIds: [1], sessionIds: [pageRouteSessionId] },
      { pageId: pageIds[1], role: "foreground-before-restart", documentIds: [1], sessionIds: [pageRouteSessionId] },
      { pageId: pageIds[2], role: "foreground-after-restart", documentIds: [1], sessionIds: [pageRouteSessionId] },
    ],
    startedAt: 100, endedAt: 200,
  });
  const restartWindows = [
    testRestartWindow({ operationId: "fork-op-partial", phase: "copy.source.partial", sourceSessionId: "ses_source_partial",
      sourceThreadId: "thr_source_partial", targetSessionId: "ses_target_partial", pageIds: [9, 8, 10], index: 1 }),
    testRestartWindow({ operationId: "fork-op-before-cas", phase: "copy.source.before_captured_cas", sourceSessionId: "ses_source_cas",
      sourceThreadId: "thr_source_cas", targetSessionId: "ses_target_cas", pageIds: [11, 10, 12], index: 2 }),
    testRestartWindow({ operationId: "fork-op-marker", phase: "copy.source.before_captured_cas", sourceSessionId: "ses_source_marker",
      sourceThreadId: "thr_source_marker", targetSessionId: "ses_target_marker", pageIds: [13, 12, 14], index: 3 }),
  ];
  const restartConsoleFacts = [];
  const restartConsoleErrors = [];
  const restartRequests = [];
  const restartResponses = [];
  const restartFailures = [];
  const status502Text = "Failed to load resource: the server responded with a status of 502 (Bad Gateway)";
  const restartTargets = [
    { windowIndex: 0, pageRole: "copy-request", paths: ["/api/v1/workspace/files?scope=workspace",
      `/api/v1/sessions/${pageRouteSessionId}/main-thread`, `/api/v1/session-catalog/breadcrumb/${pageRouteSessionId}`,
      `/api/v1/sessions/${pageRouteSessionId}/bootstrap`, "/api/v1/session-catalog/events?after=0&limit=1000",
      `/api/v1/sessions/${pageRouteSessionId}/file-tree-settings`, `/api/v1/sessions/${pageRouteSessionId}/goal`,
      "/api/v1/session-catalog/children?limit=100", "/api/v1/workspace", "/api/v1/session-catalog/snapshot"] },
    { windowIndex: 1, pageRole: "foreground-before-restart", paths: ["/api/v1/workspace/observations/stream"] },
    { windowIndex: 2, pageRole: "foreground-after-restart",
      paths: [`/api/v1/sessions/${pageRouteSessionId}/history`, "/api/v1/workspace/observations/stream"] },
  ];
  for (const { windowIndex, pageRole, paths } of restartTargets) {
    const window = restartWindows[windowIndex];
    for (const path of paths) {
      const errorIndex = restartConsoleFacts.length;
      const targetUrl = `${window.origin}${path}`;
      const method = path.endsWith("/history") || path === OBSERVATION_STREAM_PATH ? "POST" : "GET";
      const pageBinding = window.pageBindings.find((binding) => binding.role === pageRole);
      const requestFactId = `browser-request-${errorIndex}`;
      const requestId = `request-${errorIndex}`;
      const requestBody = path === OBSERVATION_STREAM_PATH
        ? { subscriptions: [{ subscription_id: `sub-${errorIndex}`, kind: "workspace_files" }] }
        : method === "POST" ? { direction: "tail", turns: 20 } : null;
      restartConsoleFacts.push({ index: errorIndex, text: status502Text, url: targetUrl, cdpRequestId: `cdp-${errorIndex}`,
        pageId: pageBinding.pageId, documentId: 1, pageRole: pageBinding.role,
        receivedAt: 150, restartWindowId: window.operationId });
      restartConsoleErrors.push(status502Text);
      restartRequests.push({ requestFactId, restartWindowId: window.operationId, url: targetUrl, method,
        pageId: pageBinding.pageId, documentId: 1, pageRole: pageBinding.role,
        requestedAt: 140, workspaceHeader: window.workspaceId, body: requestBody,
        bodyText: requestBody === null ? null : JSON.stringify(requestBody) });
      restartResponses.push({ restartWindowId: window.operationId, url: targetUrl, method, status: 502, cdpRequestId: `cdp-${errorIndex}`,
        requestFactId, pageId: pageBinding.pageId, documentId: 1, pageRole: pageBinding.role,
        requestedAt: 140, receivedAt: 150, requestId, bodyRequestId: requestId,
        body: { detail: `无法连接工作区后端 http://127.0.0.1:19000${new URL(targetUrl).pathname}: backend restart`, request_id: requestId },
        workspaceHeader: window.workspaceId });
    }
  }
  const observationsUrl = "http://127.0.0.1:19001/api/v1/workspace/observations/stream";
  const observationErrorIndex = restartConsoleFacts.length;
  const observationPageBinding = restartWindows[0].pageBindings.find((binding) => binding.role === "copy-request");
  restartConsoleFacts.push({ index: observationErrorIndex,
    text: "Failed to load resource: net::ERR_INCOMPLETE_CHUNKED_ENCODING", url: observationsUrl,
    pageId: observationPageBinding.pageId, documentId: 1, pageRole: observationPageBinding.role, receivedAt: 150,
    restartWindowId: restartWindows[0].operationId });
  restartConsoleErrors.push(restartConsoleFacts.at(-1).text);
  const observationRequestFactId = "browser-request-observation-stream-interrupted";
  const observationSubscriptionId = "sub-observation-restart";
  restartFailures.push({ requestFactId: observationRequestFactId, url: observationsUrl, method: "POST",
    error: "net::ERR_INCOMPLETE_CHUNKED_ENCODING", pageId: observationPageBinding.pageId,
    documentId: 1, pageRole: observationPageBinding.role, requestedAt: 90, failedAt: 150,
    restartWindowId: restartWindows[0].operationId });
  restartRequests.push({ requestFactId: observationRequestFactId, restartWindowId: null, url: observationsUrl,
    method: "POST", pageId: observationPageBinding.pageId, documentId: 1, pageRole: observationPageBinding.role,
    requestedAt: 90, workspaceHeader: restartWindows[0].workspaceId,
    body: { subscriptions: [{ subscription_id: observationSubscriptionId, kind: "workspace_files" }] } });
  const recoveryRequestId = "request-observation-stream-recovered";
  restartResponses.push({ requestFactId: "browser-request-observation-stream-recovery",
    restartWindowId: restartWindows[0].operationId, url: observationsUrl, method: "POST", status: 200,
    pageId: observationPageBinding.pageId, documentId: 1, pageRole: observationPageBinding.role,
    requestedAt: 181, receivedAt: 183, workspaceHeader: restartWindows[0].workspaceId,
    requestId: recoveryRequestId, contentType: "text/event-stream", subscriptions: [
      { subscription_id: observationSubscriptionId, kind: "workspace_files" },
    ], connectedSubscriptions: [observationSubscriptionId] });
  const classifiedRestart = classifyControlledRestartErrors(restartConsoleFacts, restartConsoleErrors,
    restartFailures, restartRequests, restartResponses, restartWindows);
  assert.equal(classifiedRestart.handled.length, 14,
    "三次受控 capture 重启应逐条绑定十三条 502 与一条 observations stream chunked failure");
  assert.deepEqual(classifiedRestart.unhandledConsoleErrors, []);
  assert.deepEqual(classifiedRestart.unhandledFailedRequests, []);
  assert.deepEqual(new Set(classifiedRestart.handled.filter((fact) => fact.responseRequestId)
    .map((fact) => fact.pageRole)), new Set(["copy-request", "foreground-before-restart", "foreground-after-restart"]),
  "502 必须能分别绑定实际 copy 请求页、重启前页面和恢复页面身份");
  const routedSessionErrors = classifiedRestart.handled.filter((fact) => fact.responseRequestId
    && (fact.url.includes("/api/v1/sessions/") || fact.url.includes("/api/v1/session-catalog/breadcrumb/")));
  assert.equal(routedSessionErrors.length, 6);
  assert.ok(routedSessionErrors.every((fact) => fact.url.includes(pageRouteSessionId)
    && !fact.url.includes("ses_active")),
  "Session endpoint 应按重启前页面实际观测的路由分类，不得替换为 activeSessionId");
  assert.ok(classifiedRestart.handled.every((fact) => fact.activeSessionId === "ses_active"
    && fact.sourceSessionId !== fact.activeSessionId && fact.sourceThreadId.startsWith("thr_source_")),
  "重启分类必须保留 active browser Session 与本次 fork source session/thread 的独立身份");
  const classifiedHistory = classifiedRestart.handled.find((fact) => fact.endpoint === "history");
  assert.ok(classifiedHistory && classifiedHistory.url.includes(pageRouteSessionId)
    && !classifiedHistory.url.includes("ses_active") && classifiedHistory.requestBody.direction === "tail",
  "history 分类按页面实际路由和同请求 POST 正文，不能用 window.activeSessionId 代替");

  const baseRestartError = restartConsoleFacts[0];
  const baseRestartRequest = restartRequests[0];
  const baseRestartResponse = restartResponses[0];
  const assertRestartRemainsUnhandled = ({ error = baseRestartError, request = baseRestartRequest,
    response = baseRestartResponse, window = restartWindows[0], failure = null, windows = [window] } = {}, message) => {
    const classified = classifyControlledRestartErrors([error], [error.text], failure ? [failure] : [],
      request ? [request] : [], response ? [response] : [], windows);
    assert.equal(classified.handled.length, 0, message);
    assert.deepEqual(classified.unhandledConsoleErrors, [error.text], `${message}；console error 必须保留`);
    assert.deepEqual(classified.unhandledFailedRequests, failure ? [failure] : [], `${message}；request failure 必须保留`);
  };
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, receivedAt: 201 } },
    "重启窗口外的相同 URL 不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, pageId: 99 },
    request: { ...baseRestartRequest, pageId: 99 }, response: { ...baseRestartResponse, pageId: 99 } },
  "窗口内错 page 不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, documentId: 2 },
    request: { ...baseRestartRequest, documentId: 2 }, response: { ...baseRestartResponse, documentId: 2 } },
  "窗口内错 document 不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, pageRole: "foreground-after-restart" },
    request: { ...baseRestartRequest, pageRole: "foreground-after-restart" },
    response: { ...baseRestartResponse, pageRole: "foreground-after-restart" } },
  "错 page role 不能分类");
  const nonAllowlistedUrl = `${restartWindows[0].origin}/api/v1/sessions/${pageRouteSessionId}/messages`;
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, url: nonAllowlistedUrl },
    request: { ...baseRestartRequest, url: nonAllowlistedUrl }, response: { ...baseRestartResponse, url: nonAllowlistedUrl } },
  "受控窗口内非 allowlist endpoint 不能分类");
  const queriedRestartUrl = `${baseRestartError.url}?after_seq=0`;
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, url: queriedRestartUrl },
    request: { ...baseRestartRequest, url: queriedRestartUrl }, response: { ...baseRestartResponse, url: queriedRestartUrl } },
  "受控 endpoint 带 query 的不同 URL 不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, restartWindowId: null },
    request: { ...baseRestartRequest, restartWindowId: null }, response: { ...baseRestartResponse, restartWindowId: null },
    window: { ...restartWindows[0], operationId: null } },
  "缺少 fork operation ID 的窗口不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, restartWindowId: "fork-op-other" } },
  "console 错误的 operation ID 必须精确匹配受控窗口");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse, restartWindowId: "fork-op-other" } },
  "真实 response fact 的 operation ID 必须精确匹配受控窗口");
  assertRestartRemainsUnhandled({ window: { ...restartWindows[0], captureOperationId: "fork-op-other" } },
  "受控窗口 operation ID 必须与 capture receipt 一致");
  assertRestartRemainsUnhandled({ windows: [restartWindows[0],
    { ...restartWindows[0], sourceSessionId: "ses_source_ambiguous" }] },
  "相同 operation ID 的重复窗口会造成归属歧义，必须保留错误");
  const historyIndex = restartConsoleFacts.findIndex((fact) => fact.url.endsWith(`/${pageRouteSessionId}/history`));
  const historyError = restartConsoleFacts[historyIndex];
  const historyRequest = restartRequests.find((request) => request.requestFactId === restartResponses[historyIndex].requestFactId);
  const historyResponse = restartResponses[historyIndex];
  assertRestartRemainsUnhandled({ error: historyError, request: { ...historyRequest, method: "GET" },
    response: { ...historyResponse, method: "GET" } },
  "history 502 必须绑定实际 POST 请求");
  const sourceUrl = `${restartWindows[0].origin}/api/v1/sessions/${restartWindows[0].sourceSessionId}/main-thread`;
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, url: sourceUrl },
    request: { ...baseRestartRequest, url: sourceUrl }, response: { ...baseRestartResponse, url: sourceUrl } },
  "Session endpoint 不得根据 fork sourceSessionId 替代页面实际路由");
  const activeUrl = `${restartWindows[0].origin}/api/v1/sessions/${restartWindows[0].activeSessionId}/main-thread`;
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, url: activeUrl },
    request: { ...baseRestartRequest, url: activeUrl }, response: { ...baseRestartResponse, url: activeUrl } },
  "Session endpoint 不得以 window.activeSessionId 替代页面实际路由");
  const routeError = restartConsoleFacts[1];
  const routeRequest = restartRequests[1];
  const routeResponse = restartResponses[1];
  const routeNotObservedWindow = { ...restartWindows[0], pageBindings: restartWindows[0].pageBindings.map((binding) =>
    binding.pageId === routeError.pageId ? { ...binding, sessionIds: [] } : binding) };
  assertRestartRemainsUnhandled({ error: routeError, request: routeRequest, response: routeResponse,
    window: routeNotObservedWindow },
  "页面未在窗口前观察到该 session 路由时不能分类");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, text: "Failed to load resource: net::ERR_CONNECTION_RESET" } },
  "未列入 allowlist 的错误文本不能分类");
  assertRestartRemainsUnhandled({ response: null, request: null },
  "缺少同一请求的真实 502 response fact 时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse, requestId: null } },
  "缺少 response header request ID 时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse, bodyRequestId: "request-other" } },
  "response body 的 request ID 不一致时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse,
    body: { ...baseRestartResponse.body, request_id: "request-other" } } },
  "解析后的 response JSON request_id 不一致时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse, bodyError: "SyntaxError" } },
  "response JSON 读取或解析失败时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse,
    body: { ...baseRestartResponse.body, detail: baseRestartResponse.body.detail.replace(":19000/", ":19002/") } } },
  "502 正文指向其它 backend port 时不能分类");
  assertRestartRemainsUnhandled({ response: { ...baseRestartResponse,
    body: { ...baseRestartResponse.body, detail: "工作区初始化业务失败" } } },
  "502 业务错误不能伪装成受控后端断连");
  for (const changedUrl of [`${restartWindows[0].origin}/api/v1/workspace?scope=workspace`,
    `${restartWindows[0].origin}/api/v1/session-catalog/children?limit=101`]) {
    assertRestartRemainsUnhandled({ error: { ...baseRestartError, url: changedUrl },
      request: { ...baseRestartRequest, url: changedUrl }, response: { ...baseRestartResponse, url: changedUrl } },
    "初始化 endpoint 的不同查询不能借用受控 502 归属");
  }
  assertRestartRemainsUnhandled({ request: { ...baseRestartRequest, requestFactId: "browser-request-other" } },
  "request fact 与 response fact 未按唯一请求身份配对时不能分类");
  assertRestartRemainsUnhandled({ request: { ...baseRestartRequest, bodyError: "SyntaxError" } },
  "请求正文解析失败时不能分类");
  assertRestartRemainsUnhandled({ request: { ...baseRestartRequest, workspaceHeader: "workspace-other" },
    response: { ...baseRestartResponse, workspaceHeader: "workspace-other" } },
  "错 workspace 请求和响应不能分类");
  assertRestartRemainsUnhandled({ window: { ...restartWindows[0], backendRestart: {
    ...restartWindows[0].backendRestart, beforePid: restartWindows[0].backendRestart.afterPid,
  } } }, "旧后端 PID 与恢复 PID 不同时变成相同值时不能分类");
  assertRestartRemainsUnhandled({ window: { ...restartWindows[0], backendRestart: {
    ...restartWindows[0].backendRestart, port: restartWindows[0].gatewayPort,
  } } }, "受控重启 backend port 与 gateway port 冲突时不能分类");
  assertRestartRemainsUnhandled({ window: { ...restartWindows[0], gatewayPort: restartWindows[0].gatewayPort + 1 } },
  "受控重启 gateway origin 与登记端口不一致时不能分类");
  assertRestartRemainsUnhandled({ error: baseRestartError,
    response: { ...baseRestartResponse, requestedAt: 5_190, receivedAt: 5_200 },
    window: { ...restartWindows[0], endedAt: 6_000 } },
  "超出有界关联时间的真实 response fact 不能分类");
  const competingResponse = { ...baseRestartResponse, requestedAt: 149, receivedAt: 155,
    requestId: "request-competing-response" };
  const competingResponses = classifyControlledRestartErrors([baseRestartError], [baseRestartError.text], [],
    [baseRestartRequest, { ...baseRestartRequest, requestFactId: "browser-request-competing" }],
    [baseRestartResponse, { ...competingResponse, requestFactId: "browser-request-competing",
      bodyRequestId: "request-competing-response", body: { ...baseRestartResponse.body, request_id: "request-competing-response" } }],
    [restartWindows[0]]);
  assert.equal(competingResponses.handled.length, 0,
    "同一 console 错误附近有多个 response 时不得猜测匹配对象");
  assert.deepEqual(competingResponses.unhandledConsoleErrors, [baseRestartError.text],
    "response 配对歧义时原 console error 必须保留");
  const competingConsoleError = { ...baseRestartError, index: 1, receivedAt: 151 };
  const competingConsoleErrors = classifyControlledRestartErrors(
    [baseRestartError, competingConsoleError], [baseRestartError.text, competingConsoleError.text], [],
    [baseRestartRequest], [baseRestartResponse], [restartWindows[0]]);
  assert.equal(competingConsoleErrors.handled.length, 0,
    "同一 response 附近有多个 console error 时不得猜测匹配对象");
  assert.deepEqual(competingConsoleErrors.unhandledConsoleErrors,
    [baseRestartError.text, competingConsoleError.text], "console 配对歧义时两个原错误都必须保留");
  const observationConsoleError = restartConsoleFacts.at(-1);
  const observationFailure = restartFailures[0];
  assertRestartRemainsUnhandled({ error: observationConsoleError,
    failure: { ...observationFailure, restartWindowId: "fork-op-other" } },
  "观察流失败事实不能借用另一个重启窗口");
  assertRestartRemainsUnhandled({ error: observationConsoleError,
    failure: { ...observationFailure, pageRole: "foreground-after-restart" } },
  "观察流失败事实必须保留实际页面角色");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, cdpRequestId: "cdp-other" } },
  "网络日志必须与 HTTP 响应的 CDP 请求身份相同");
  assertRestartRemainsUnhandled({ error: { ...baseRestartError, cdpRequestId: null } },
  "没有网络请求身份不能按相近时间猜测错误归属");
  const anotherRequestId = "browser-request-distinct-cdp";
  const anotherResponseId = "request-distinct-cdp";
  const distinctErrors = classifyControlledRestartErrors([baseRestartError,
    { ...baseRestartError, index: 1, cdpRequestId: "cdp-distinct", receivedAt: 151 }],
    [baseRestartError.text, baseRestartError.text], [],
    [baseRestartRequest, { ...baseRestartRequest, requestFactId: anotherRequestId }],
    [baseRestartResponse, { ...baseRestartResponse, cdpRequestId: "cdp-distinct",
      requestFactId: anotherRequestId, requestId: anotherResponseId, bodyRequestId: anotherResponseId,
      body: { ...baseRestartResponse.body, request_id: anotherResponseId } }], [restartWindows[0]]);
  assert.equal(distinctErrors.handled.length, 2, "连续重试即使 URL 与时间相近，也必须按各自网络请求身份分别处理");
  assertRestartRemainsUnhandled({ error: observationConsoleError,
    failure: { ...observationFailure, error: "net::ERR_CONNECTION_RESET" } },
  "受控 observations stream 不得豁免其它网络错误类型");
  assertRestartRemainsUnhandled({ error: observationConsoleError,
    failure: { ...observationFailure, restartWindowId: "fork-op-other" } },
  "observations stream failure 的 operation ID 必须精确匹配受控窗口");
  const assertObservationRemainsUnhandled = (response, request = restartRequests.at(-1), error = observationConsoleError) => {
    const responses = response === null ? [] : [response];
    const classified = classifyControlledRestartErrors([error], [error.text], [observationFailure],
      [request], responses, [restartWindows[0]]);
    assert.equal(classified.handled.length, 0, "observations stream 恢复证据不足时不能分类");
    assert.deepEqual(classified.unhandledConsoleErrors, [error.text]);
    assert.deepEqual(classified.unhandledFailedRequests, [observationFailure]);
  };
  const recoveredObservation = restartResponses.at(-1);
  const reRegisteredObservation = { ...recoveredObservation,
    subscriptions: [{ ...recoveredObservation.subscriptions[0], subscription_id: "sub-new-registration" }],
    connectedSubscriptions: ["sub-new-registration"] };
  const reRegistered = classifyControlledRestartErrors([observationConsoleError], [observationConsoleError.text],
    [observationFailure], [restartRequests.at(-1)], [reRegisteredObservation], [restartWindows[0]]);
  assert.equal(reRegistered.handled.length, 1, "同一观察目标重新登记后应按新 ID 的真实 connected 回执证明恢复");
  assertObservationRemainsUnhandled({ ...reRegisteredObservation, connectedSubscriptions: [observationSubscriptionId] });
  assertObservationRemainsUnhandled({ ...reRegisteredObservation,
    subscriptions: [{ ...reRegisteredObservation.subscriptions[0], kind: "session_activity" }] });
  assertObservationRemainsUnhandled({ ...reRegisteredObservation,
    subscriptions: [{ ...reRegisteredObservation.subscriptions[0], paths: ["other/path"] }] });
  const traceRequest = { ...restartRequests.at(-1), body: { subscriptions: [
    { subscription_id: "trace-before", kind: "session_trace", session_id: "ses_trace_owner" },
  ] } };
  assertObservationRemainsUnhandled({ ...reRegisteredObservation, subscriptions: [
    { subscription_id: "sub-new-registration", kind: "session_trace", session_id: "ses_other" },
  ] }, traceRequest);
  assertObservationRemainsUnhandled({ ...reRegisteredObservation,
    subscriptions: [...reRegisteredObservation.subscriptions, ...reRegisteredObservation.subscriptions] });
  const consecutiveRecoveries = classifyControlledRestartErrors([observationConsoleError], [observationConsoleError.text],
    [observationFailure], [restartRequests.at(-1)], [reRegisteredObservation, {
      ...reRegisteredObservation, requestId: "request-later-reconfiguration", requestedAt: 185, receivedAt: 187,
    }], [restartWindows[0]]);
  assert.equal(consecutiveRecoveries.handled.length, 1, "后续合法连接重配不应抹去首次恢复事实");
  assert.equal(consecutiveRecoveries.handled[0].recoveryRequestId, reRegisteredObservation.requestId);
  assertObservationRemainsUnhandled({ ...recoveredObservation, connectedSubscriptions: [] });
  assertObservationRemainsUnhandled({ ...recoveredObservation, connectedSubscriptions: ["sub-other"] });
  assertObservationRemainsUnhandled({ ...recoveredObservation, pageId: recoveredObservation.pageId + 1 });
  assertObservationRemainsUnhandled({ ...recoveredObservation, documentId: recoveredObservation.documentId + 1 });
  assertObservationRemainsUnhandled({ ...recoveredObservation, workspaceHeader: "workspace-other" });
  assertObservationRemainsUnhandled({ ...recoveredObservation, restartWindowId: "fork-op-other" });
  assertObservationRemainsUnhandled({ ...recoveredObservation, requestId: null });
  assertObservationRemainsUnhandled(null);
  const duplicateObservationErrors = classifyControlledRestartErrors(
    [observationConsoleError, { ...observationConsoleError, index: observationConsoleError.index + 1 }],
    [observationConsoleError.text, observationConsoleError.text], [observationFailure],
    [restartRequests.at(-1)], [recoveredObservation], [restartWindows[0]]);
  assert.equal(duplicateObservationErrors.handled.length, 0,
    "重复 observations console error 必须保持歧义并全部未处理");
  assert.deepEqual(duplicateObservationErrors.unhandledFailedRequests, [observationFailure]);

  const controlledStreamWindow = { ...restartWindows[0], origin: "http://127.0.0.1:19000",
    pageId: 1, documentIds: [2], startedAt: 60, endedAt: 140 };
  const afterTwentyConsoleErrors = [chunkedConsoleError, secondChunkedConsoleError].map((error) => ({
    ...error, restartWindowId: controlledStreamWindow.operationId,
  }));
  const afterTwentyPipeline = classifyConsoleErrors({
    consoleErrorFacts: afterTwentyConsoleErrors,
    consoleErrors: afterTwentyConsoleErrors.map((error) => error.text),
    failedRequests: [failedRequest, reconnectFailure],
    messageStreamResponses: [interruptedResponse, failedReconnect],
    jobOwnerResponses: [owner],
    controlledRestartWindows: [controlledStreamWindow],
    controlledRestartResponses: [],
    conflictResponses: [], historyErrorResponses: [], lifecycleErrorResponses: [],
    lifecycleDeletionProofs: [], turnNavigationMissingTurnProofs: [], historyLiveProofs: [],
    historyRecoveryResponses: [], section8Facts: { forks: { attachmentDeletionRace: [] } },
  });
  assert.equal(afterTwentyPipeline.handledControlledRestartErrors.length, 0,
    "message-stream endpoint 不属于受控 restart allowlist");
  assert.equal(afterTwentyPipeline.handledIncompleteMessageStreamErrors.length, 0,
    "连续 after_seq=20 的两个 HTTP 200 chunked failure 没有成功 recovery，均不得豁免");
  assert.deepEqual(afterTwentyPipeline.unhandledConsoleErrors, afterTwentyConsoleErrors.map((error) => error.text),
    "两个 after_seq=20 chunked console errors 必须保留为未处理错误");
  assert.deepEqual(afterTwentyPipeline.unhandledFailedRequests, [failedRequest, reconnectFailure],
    "两个 after_seq=20 chunked request failures 必须保留为未处理失败");
}

function isExpectedBrowserAbort(request) {
  // 页面刷新或重建 SSE 连接时，浏览器会取消尚未完成的 fetch；这不是服务端请求失败。
  return request.failure()?.errorText === "net::ERR_ABORTED";
}

function basicStreamConnections(owner, observations = controlledRestartResponses, messages = messageStreamResponses) {
  const traceByDocument = new Map();
  for (const response of observations) {
    if (response.pageId !== owner.page.diagnosticId || response.status !== 200
        || response.workspaceHeader !== owner.gatewayWorkspaceId
        || response.contentType?.split(";", 1)[0] !== "text/event-stream"
        || !response.requestId || !response.cdpRequestId) continue;
    const subscriptions = response.subscriptions?.filter((subscription) => subscription.kind === "session_trace"
      && subscription.session_id === owner.sessionId
      && response.connectedSubscriptions.includes(subscription.subscription_id)) ?? [];
    // 同页增减其它 observer 会重建物理连接；每个 document 只计已确认连接的会话逻辑订阅。
    if (subscriptions.length === 1 && !traceByDocument.has(response.documentId)) {
      traceByDocument.set(response.documentId, { ...response, subscriptionId: subscriptions[0].subscription_id });
    }
  }
  return {
    trace: [...traceByDocument.values()],
    message: messages.filter((response) => response.pageId === owner.page.diagnosticId
      && response.sessionId === owner.sessionId && response.threadId === owner.threadId
      && response.workspaceHeader === owner.gatewayWorkspaceId
      && response.status === 200 && isEventStreamResponse(response)),
  };
}

async function waitForStreamCount(owner, kind, expectedCount) {
  await waitUntil(
    async () => basicStreamConnections(owner)[kind].length >= expectedCount,
    `${kind} SSE 第 ${expectedCount} 次连接`,
    60_000,
  );
}

async function waitForJobStatus(owner, jobId, expectedJobStatus = "completed") {
  assert.ok(["completed", "failed"].includes(expectedJobStatus), "必须明确指定成功或失败的 Job 合同");
  const expectedStatuses = expectedJobStatus === "completed" ? ["completed", "succeeded"] : ["failed"];
  let latestJob = null;
  await waitUntil(
    async () => {
      const payload = await ownerApi(owner, `/api/v1/jobs/${jobId}`);
      latestJob = payload?.data ?? null;
      assert.equal(latestJob?.job_id, jobId);
      assert.equal(latestJob?.session_id, owner.sessionId);
      assert.equal(latestJob?.thread_id, owner.threadId);
      if (["completed", "succeeded", "failed", "cancelled", "timed_out"].includes(latestJob?.status)) {
        assert.ok(expectedStatuses.includes(latestJob.status), `Job 未满足 ${expectedJobStatus} 合同: ${JSON.stringify(latestJob)}`);
        return true;
      }
      return false;
    },
    `Job ${jobId} 提交 ${expectedJobStatus} 终态`,
    120_000,
  );
  return latestJob;
}

async function createSessionThroughGateway(page, workspaceId) {
  const payload = await api(page, "/api/v1/sessions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-BoxTeam-Workspace-Id": workspaceId,
    },
    body: JSON.stringify({ title: "Web 工具循环完整流程", agent_id: "default" }),
  });
  const sessionId = payload?.data?.session_id;
  assert.equal(typeof sessionId, "string", "Gateway 新建会话响应缺少 session_id");
  const main = await api(page, `/api/v1/sessions/${sessionId}/main-thread`, {
    headers: { "X-BoxTeam-Workspace-ID": workspaceId },
  });
  const owner = { name: "a", page, sessionId, threadId: main.data.thread_id,
    gatewayWorkspaceId: workspaceId, workspaceIdentity: payload.data.workspace_id, child: false };
  const catalog = await sessionCatalogNode(owner);
  await threadRolloutLocator(owner, catalog);
  await planGoldenLoop(sessionId, main.data.thread_id);
  return owner;
}

async function selectSession(page, sessionId) {
  const sessionButton = page.locator(`button[data-session-id="${sessionId}"]`);
  await sessionButton.waitFor({ state: "visible", timeout: 30_000 });
  await sessionButton.click();
  // 输入框跨会话复用；等目标 owner 提交后再输入，避免把草稿写进刚切走的会话。
  await page.locator(`.chat-stream-virtual-shell[data-owner-session-id="${sessionId}"]`)
    .waitFor({ state: "visible", timeout: 30_000 });
  await page.locator("#input").waitFor({ state: "visible", timeout: 30_000 });
}

async function sendPrompt(owner, prompt) {
  const { page, sessionId } = owner;
  const sendRequestPromise = page.waitForRequest(isSessionMessageRequest, { timeout: 30_000 });
  const sendResponsePromise = page.waitForResponse(
    (response) => isSessionMessageRequest(response.request()) && response.status() === 200,
    { timeout: 30_000 },
  );
  await page.locator("#input").fill(prompt);
  await page.locator("#sendButton").click();
  const [request, response] = await Promise.all([sendRequestPromise, sendResponsePromise]);
  assert.match(
    new URL(request.url()).pathname,
    new RegExp(`/api/v1/sessions/${sessionId}/messages$`),
    "Composer 消息请求未发往当前 Session",
  );
  const payload = request.postDataJSON();
  assert.equal(payload?.message?.content, prompt, "Composer 发送内容不一致");
  assert.equal(request.headers()["x-boxteam-workspace-id"], owner.gatewayWorkspaceId);
  assert.equal(response.status(), 200, "Composer 发送失败");
  const responseBody = await response.json();
  const jobId = responseBody?.data?.job_id;
  assert.equal(typeof jobId, "string", "Composer 响应缺少 job_id");
  const turnId = responseBody?.data?.turn_id;
  canonicalUuid7(turnId, "turn");
  assert.notEqual(turnId, jobId, "Composer 不能以 Job 身份代替 Turn");
  assert.equal(typeof responseBody.data.message_id, "string");
  assert.ok(responseBody.data.message_id.length > 0);
  return { payload, jobId, turnId, accepted: responseBody.data };
}

function expectedActivityPreview(durationMs, itemCount, boundary = null) {
  assert.ok(durationMs == null || (Number.isInteger(durationMs) && durationMs >= 0), "Turn duration_ms 必须是非负整数或 null");
  assert.ok(Number.isInteger(itemCount) && itemCount >= 0, "Turn item_count 必须是非负整数");
  const duration = durationMs == null ? "—" : durationMs < 1000
    ? `${durationMs}ms`
    : `${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 0 : 1)}s`;
  return `耗时 ${duration} · Item ${itemCount} 项${boundary ? ` · ${boundary.title}` : ""}`;
}

function expectedTurnBoundary(turn) {
  const tools = turn.response_parts.filter((part) => part.kind === "tool_call");
  const states = [
    [turn.response_parts.some((part) => part.partial && part.completion_reason === "user_interrupt"), "user-interrupted", "已由用户中断"],
    [turn.status === "cancelled", "cancelled", "任务已取消"],
    [tools.some((part) => part.partial || part.status === "cancelled"), "tool-incomplete", "工具调用未完成"],
    [tools.some((part) => part.outcome_unknown), "tool-outcome-unknown", "工具执行结果未知"],
    [tools.some((part) => part.status === "failed"), "tool-failed", "工具执行失败"],
    [turn.status === "failed", "turn-failed", "本轮执行失败"],
    [turn.status === "timed_out", "turn-timed-out", "本轮执行超时"],
  ];
  const state = states.find(([applies]) => applies);
  return state ? { kind: state[1], title: state[2] } : null;
}

async function waitForCompletedTurn(page, prompt, finalText, expectedReasoning) {
  const userMessage = page.locator(".chat-user-text").filter({ hasText: prompt }).last();
  await userMessage.waitFor({ state: "visible", timeout: 60_000 });
  const turn = userMessage.locator("xpath=ancestor::article[contains(@class, 'chat-turn')]");
  const finalMessage = turn.locator(".chat-markdown").filter({ hasText: finalText }).last();
  await finalMessage.waitFor({ state: "visible", timeout: 120_000 });
  await page.waitForFunction(
    () => document.querySelectorAll("#interruptButton, .chat-thinking.is-active").length === 0,
    undefined,
    { timeout: 30_000 },
  );
  const thinking = turn.locator(".chat-thinking").last();
  await thinking.waitFor({ state: "visible", timeout: 30_000 });
  const thinkingToggle = thinking.locator(".chat-thinking-toggle");
  await waitUntil(
    async () => thinking.getAttribute("data-item-count").then((value) => value !== null),
    "terminal 历史投影提供 Item 统计",
    30_000,
  );
  const durationMs = Number(await thinking.getAttribute("data-duration-ms"));
  const itemCount = Number(await thinking.getAttribute("data-item-count"));
  const activityPreview = expectedActivityPreview(durationMs, itemCount);
  const activityPreviewElement = thinking.locator(".chat-thinking-preview");
  await activityPreviewElement.waitFor({ state: "visible", timeout: 30_000 });
  const originalViewport = page.viewportSize();
  await page.setViewportSize({ width: 640, height: 900 });
  assert.equal(
    await activityPreviewElement.isVisible(),
    true,
    "窄窗口也必须显示耗时和 Item 数量",
  );
  if (originalViewport) await page.setViewportSize(originalViewport);
  assert.equal(
    await activityPreviewElement.innerText(),
    activityPreview,
    "可见标题必须精确匹配 Turn duration_ms 和中间 item_count",
  );
  assert.equal(itemCount, 4, "固定工具循环每轮必须投影 2 个 reasoning、1 个 tool call 和 1 个 tool result");
  assert.equal(
    await thinkingToggle.getAttribute("aria-label"),
    `展开 Turn 中间消息：${activityPreview}`,
    "折叠按钮的可访问名称必须包含精确耗时和 Item 数量",
  );
  assert.equal(await thinkingToggle.getAttribute("aria-expanded"), "false");
  await thinkingToggle.click();
  await waitUntil(
    async () => await thinkingToggle.getAttribute("aria-expanded") === "true",
    "Turn 中间 Item 展开",
    30_000,
  );
  await activityPreviewElement.waitFor({ state: "visible", timeout: 30_000 });
  assert.equal(await activityPreviewElement.innerText(), activityPreview);
  const completedTool = turn.locator(".chat-tool-row.is-complete").filter({ hasText: "已运行 read_file" });
  await completedTool.waitFor({ state: "visible", timeout: 30_000 });
  const activityOrder = await turn.locator(
    ".chat-thinking-body > .chat-activity-item > .chat-markdown, .chat-thinking-body > .chat-activity-item > .chat-tool-row",
  ).evaluateAll((elements) =>
    elements.map((element) => element.textContent?.trim() ?? ""),
  );
  assert.equal(activityOrder.length, 3, "展开后应为 reasoning → 聚合工具 → reasoning 三个展示块");
  assert.match(activityOrder[0], new RegExp(expectedReasoning[0]));
  assert.match(activityOrder[1], /已运行 read_file/);
  assert.match(activityOrder[2], new RegExp(expectedReasoning[1]));
  assert.equal(
    activityOrder.filter((text) => text.includes(expectedReasoning[1])).length,
    1,
    "工具后的 reasoning summary 不得重复投影",
  );
  assert.equal(
    await turn.locator(".chat-tool-row.is-failed, .chat-tool-row.is-unknown, .chat-tool-row.is-incomplete").count(),
    0,
    "工具调用出现失败、未知或未完成状态",
  );
  return {
    activityPreview,
    durationMs,
    itemCount,
    activityOrder,
    expanded: await thinkingToggle.getAttribute("aria-expanded") === "true",
    finalText: await finalMessage.innerText(),
    toolText: await completedTool.innerText(),
    userCount: await page.locator(".chat-user-text").filter({ hasText: prompt }).count(),
  };
}

async function runGoalOwner(surface) {
  const owner = await createProductSession(surface, "Goal main Thread 身份");
  const path = `/api/v1/sessions/${owner.sessionId}/goal`;
  const expectedOwner = { session_id: owner.sessionId, thread_id: owner.threadId };
  const assertOwner = (goal) => {
    assert.deepEqual({ session_id: goal.session_id, thread_id: goal.thread_id }, expectedOwner);
  };
  const initial = (await ownerApi(owner, path, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ objective: "编辑前的暂停目标", status: "paused" }),
  })).data;
  assertOwner(initial);
  await openOwner(owner);
  const input = owner.page.getByTestId("composer-input");
  const command = async (text) => {
    await input.fill(text);
    await input.press("Enter");
  };
  const responseFor = (method) => owner.page.waitForResponse((response) =>
    new URL(response.url()).pathname === path && response.request().method() === method);
  const readResponse = async (pending) => {
    const response = await pending;
    assert.equal(response.status(), 200);
    const envelope = await response.json();
    assert.equal(envelope.request_id, response.headers()["x-request-id"]);
    assertOwner(envelope.data);
    return envelope.data;
  };
  const refresh = responseFor("GET");
  await command("/goal");
  await readResponse(refresh);
  await waitUntil(async () => (await owner.page.locator(".composer-attachment-notice").innerText())
    .includes(initial.objective), "主窗口显示暂停 Goal");
  await command("/goal edit");
  const dialog = owner.page.getByRole("dialog", { name: "编辑 Goal", exact: true });
  await dialog.getByRole("textbox").fill("浏览器已编辑的暂停目标");
  const update = responseFor("PUT");
  await dialog.getByRole("button", { name: "保存 Goal", exact: true }).click();
  const edited = await readResponse(update);
  assert.equal(edited.goal_id, initial.goal_id);
  assert.equal(edited.status, "paused");
  assert.equal(edited.objective, "浏览器已编辑的暂停目标");
  await dialog.waitFor({ state: "hidden" });
  await drainPageResponseBodies(owner.page);
  await owner.page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(owner.page, owner.sessionId);
  const reread = responseFor("GET");
  await command("/goal");
  assert.deepEqual(await readResponse(reread), edited);
  await waitUntil(async () => (await owner.page.locator(".composer-attachment-notice").innerText())
    .includes(edited.objective), "刷新后显示后端 Goal");
  const clear = responseFor("DELETE");
  await command("/goal clear");
  const cleared = await readResponse(clear);
  assert.equal(cleared.cleared, true);
  assert.equal((await ownerApi(owner, path)).data, null);
  await waitUntil(async () => (await owner.page.locator(".composer-attachment-notice").innerText())
    .includes("Goal 已清除"), "主窗口清除 Goal");
  return { ...expectedOwner, initial, edited, cleared };
}

async function runBasicChat(surface) {
  const { page } = surface;
  const rootOwner = await createSessionThroughGateway(page, surface.gatewayWorkspaceId);
  result.threadId = rootOwner.threadId;
  result.sessionId = rootOwner.sessionId;
  await drainPageResponseBodies(page);
  await page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(page, result.sessionId);
  await waitForStreamCount(rootOwner, "trace", 1);
  const firstSend = await sendOwnerPrompt(rootOwner, firstPrompt);
  await waitForStreamCount(rootOwner, "message", 1);
  result.firstTurn = await waitForCompletedTurn(
    page,
    firstPrompt,
    firstFinalText,
    ["先读取 README.md", "工具已经返回，我先核对 README.md"],
  );
  result.firstJob = await waitForJobStatus(rootOwner, firstSend.jobId);

  await drainPageResponseBodies(page);
  await page.reload({ waitUntil: "domcontentloaded" });
  await selectSession(page, result.sessionId);
  await waitForStreamCount(rootOwner, "trace", 2);
  result.restoredHistory = await waitForCompletedTurn(
    page,
    firstPrompt,
    firstFinalText,
    ["先读取 README.md", "工具已经返回，我先核对 README.md"],
  );

  const secondSend = await sendOwnerPrompt(rootOwner, secondPrompt);
  await waitForStreamCount(rootOwner, "message", 2);
  result.secondTurn = await waitForCompletedTurn(
    page,
    secondPrompt,
    secondFinalText,
    ["复用历史上下文", "工具已经返回，我先确认历史上下文"],
  );
  result.secondJob = await waitForJobStatus(rootOwner, secondSend.jobId);
  result.persisted = await loadPersistedEvidence(page, result.sessionId, surface.gatewayWorkspaceId, rootOwner.threadId);
  result.basicStreamConnections = basicStreamConnections(rootOwner);
  result.streams = Object.fromEntries(Object.entries(result.basicStreamConnections)
    .map(([kind, connections]) => [kind, connections.length]));

  assert.equal(result.persisted.llmRequestCount, 4, "两轮工具循环没有产生四次上游模型请求");
  assert.deepEqual(
    result.persisted.canonicalActivity.map((turn) => turn.itemCount),
    [4, 4],
    "两轮 history 均应由后端投影四个逻辑 Item",
  );
  assert.deepEqual(result.persisted.userMessages, [firstPrompt, secondPrompt]);
  assert.equal(result.persisted.assistantMessages.at(-1), secondFinalText);
  assert.deepEqual(result.persisted.upstreamMessageRoles, [
    ["system", "user", "user", "user", "user", "user"],
    ["system", "user", "user", "user", "user", "user", "assistant", "tool"],
    ["system", "user", "user", "user", "user", "user", "assistant", "tool", "assistant", "user"],
    ["system", "user", "user", "user", "user", "user", "assistant", "tool", "assistant", "user", "assistant", "tool"],
  ]);
  assert.match(result.persisted.agentStateJsonl, /先读取 README\.md/);
  return { sessionId: rootOwner.sessionId, threadId: rootOwner.threadId, streams: result.streams };
}

async function loadPersistedEvidence(page, sessionId, workspaceId, threadId) {
  const headers = { "X-BoxTeam-Workspace-Id": workspaceId };
  const messages = await api(page, `/api/v1/sessions/${sessionId}/messages`, { headers });
  const logs = await api(page, `/api/v1/sessions/${sessionId}/llm-request-logs`, { headers });
  const agentState = await api(page, `/api/v1/sessions/${sessionId}/agent-state/messages`, { headers });
  const history = await api(page, `/api/v1/sessions/${sessionId}/history`, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/json" },
    body: JSON.stringify({
      direction: "tail",
      turns: 2,
      include: ["user", "reasoning_detail", "tool_call", "tool_result", "final_response"],
    }),
  });
  const items = messages?.data?.items ?? [];
  for (const message of items) assert.equal(message.thread_id, threadId, "MessageDTO 必须保留请求选定的 main owner");
  const logItems = Array.isArray(logs?.data) ? logs.data : logs?.data?.items ?? [];
  const historyTurns = history?.data?.items ?? [];
  const canonicalActivity = historyTurns.map((turn) => {
    const parts = (turn.response_parts ?? []).filter((part) => part.kind !== "final_text");
    assert.equal(parts.length, turn.activity_stats?.item_count, "history Item 数量与 response_parts 不一致");
    assert.ok(parts.every((part) => typeof part.source?.item_id === "string"));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.item_sequence)));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.part_ordinal)));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.logical_item_ordinal)));
    assert.ok(parts.every((part) => typeof part.source?.created_at === "string"));
    assert.ok(parts.every((part) => Number.isInteger(part.source?.elapsed_ms)));
    const coordinates = parts.map((part) => [
      part.source.logical_item_ordinal,
      part.source.part_ordinal,
    ]);
    assert.deepEqual(
      coordinates,
      [...coordinates].sort((left, right) => left[0] - right[0] || left[1] - right[1]),
      "history response_parts 未使用后端 canonical 顺序",
    );
    return {
      turnId: turn.turn_id,
      itemCount: turn.activity_stats.item_count,
      firstItemSequence: turn.activity_stats.first_item_sequence,
      lastItemSequence: turn.activity_stats.last_item_sequence,
      coordinates,
    };
  });
  const upstreamAttempts = logItems
    .map((item) => item?.upstream?.attempts?.[0])
    .filter((attempt) => attempt);
  return {
    messageCount: items.length,
    userMessages: items.filter((item) => item.role === "user").map((item) => item.content),
    assistantMessages: items.filter((item) => item.role === "assistant").map((item) => item.content),
    llmRequestCount: logItems.length,
    upstreamMessageRoles: upstreamAttempts.map((attempt) =>
      (attempt.request?.messages ?? []).map((message) => message.role)),
    agentStateJsonl: agentState?.data?.jsonl ?? "",
    canonicalActivity,
  };
}

const HISTORY_INCLUDE = ["user", "text", "reasoning_detail", "tool_call", "tool_result", "final_response", "internal"];

function canonicalUuid7(value, prefix) {
  assert.match(value, new RegExp(`^${prefix}_[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$`));
}

function toolResponse(label, name, args, reasoning = `${label}：执行真实产品工具。`) {
  return { label, reasoning, calls: [{ id: label, name, arguments: args }] };
}

function assertBoundToolResult(owner, sent, turn, modelCallId, providerCallId, toolName) {
  assert.deepEqual([turn.session_id, turn.job_id, turn.turn_id], [owner.sessionId, sent.jobId, sent.turnId]);
  assert.equal(sent.accepted.thread_id, owner.threadId);
  const toolCallId = `${modelCallId}:tool-call:${providerCallId}`;
  const calls = turn.response_parts.filter((part) => part.kind === "tool_call" && part.tool_call_id === toolCallId);
  const results = turn.response_parts.filter((part) => part.kind === "tool_result" && part.tool_call_id === toolCallId);
  assert.equal(calls.length, 1, "原 emitting ModelCall/raw ToolCall 必须只有一个可见调用");
  assert.equal(results.length, 1, "原 emitting ModelCall/raw ToolCall 必须只有一个可见结果");
  for (const part of [...calls, ...results]) {
    assert.equal(part.tool_name, toolName);
    assert.ok(part.source.item_id, "拒绝事实必须绑定真实 canonical Item");
    assert.equal(part.part_id, `${part.source.item_id}:part:${part.source.part_ordinal}`);
  }
  assert.ok(calls[0].source.logical_item_ordinal < results[0].source.logical_item_ordinal);
  assert.equal(results[0].truncated, false);
  assert.equal(results[0].partial, false);
  return results[0];
}

function assertDiscoveryRejection(result, targetSessionId) {
  const prefix = "Gateway 通信失败: path=/api/gateway/federation/discovery, status=409, detail=";
  assert.ok(result.result.startsWith(prefix), "裸 Session 寻址拒绝必须来自原 discovery HTTP 409");
  const body = JSON.parse(result.result.slice(prefix.length));
  assert.deepEqual(body.detail, { code: "target_not_resolvable",
    message: `目标 Session 在指定 Gateway/workspace 范围内不可解析: ${targetSessionId}`, detail: {} });
  assert.equal(typeof body.request_id, "string");
  assert.ok(body.request_id);
  return body;
}

function assertSessionDeletionPending(body, sessionId) {
  assert.equal(typeof body.detail, "string");
  assert.equal(body.detail.split(":", 1)[0], "session_deletion_pending");
  assert.ok(body.detail.includes(sessionId), "删除拒绝必须携带本次 API 的精确 Session owner");
}

function finalResponse(label, text) {
  return { label, reasoning: `${label}：核对已提交结果。`, text };
}

function freezeInternalExecutionOwners(executions) {
  const owners = Object.create(null);
  for (const execution of executions) {
    assert.equal(Object.hasOwn(owners, execution.execution_id), false, "冻结的 source history 不得重复 internal execution");
    owners[execution.execution_id] = Object.freeze({
      session_id: execution.session_id,
      thread_id: execution.thread_id,
      job_id: execution.job_id,
    });
  }
  return Object.freeze(owners);
}

function assertInternalExecutionOwner(execution, owner) {
  for (const field of ["execution_id", "session_id", "thread_id", "job_id"]) {
    assert.equal(typeof execution[field], "string", `internal execution 缺少 ${field}`);
    assert.ok(execution[field].length > 0, `internal execution 的 ${field} 不能为空`);
  }
  const inheritedExecutionOwners = owner.inheritedExecutionOwners;
  const inherited = inheritedExecutionOwners && Object.hasOwn(inheritedExecutionOwners, execution.execution_id)
    ? inheritedExecutionOwners[execution.execution_id] : undefined;
  if (inherited !== undefined) {
    assert.deepEqual(
      [execution.session_id, execution.thread_id, execution.job_id],
      [inherited.session_id, inherited.thread_id, inherited.job_id],
      `继承的 internal execution 必须保留冻结来源 owner 与 job: ${execution.execution_id}`,
    );
    return;
  }
  assert.deepEqual(
    [execution.session_id, execution.thread_id],
    [owner.sessionId, owner.threadId],
    `当前 internal execution 必须属于请求 owner: ${execution.execution_id}`,
  );
}

async function assertCurrentInternalExecutionJobs(executions, owner) {
  if (!owner.inheritedExecutionOwners) return;
  const current = executions.filter((execution) => !Object.hasOwn(owner.inheritedExecutionOwners, execution.execution_id));
  if (current.length === 0) return;
  const executionIds = current.map((execution) => execution.execution_id);
  const rows = await readRollout(owner,
    `SELECT execution_id,session_id,thread_id,job_id,execution_origin,turn_id FROM executions WHERE execution_id IN (${executionIds.map(() => "?").join(",")})`,
    executionIds);
  const rowsById = new Map(rows.map((row) => [row.execution_id, row]));
  assert.equal(rowsById.size, rows.length, "target 本地 execution owner 事实不得重复");
  assert.equal(rows.length, executionIds.length, "target 新增 internal execution 必须有唯一 durable execution/job 事实");
  for (const execution of current) {
    const durable = rowsById.get(execution.execution_id);
    assert.ok(durable, `target 新增 internal execution 缺少 durable owner: ${execution.execution_id}`);
    assert.deepEqual(
      [execution.execution_id, execution.session_id, execution.thread_id, execution.job_id],
      [durable.execution_id, durable.session_id, durable.thread_id, durable.job_id],
      `target 新增 internal execution 必须逐字段匹配本地 durable owner 与 job: ${execution.execution_id}`,
    );
    assert.equal(durable.execution_origin, "internal",
      `target 新增 internal execution 的 durable origin 必须为 internal: ${execution.execution_id}`);
    assert.equal(durable.turn_id, null,
      `target 新增 internal execution 必须不属于 Turn: ${execution.execution_id}`);
    assert.deepEqual([durable.session_id, durable.thread_id], [owner.sessionId, owner.threadId],
      `target 新增 internal execution 必须属于当前读取 owner: ${execution.execution_id}`);
  }
}

async function loadThreadHistory(owner, options = {}, timing = null) {
  const requestTiming = timing ? {} : null;
  const requestStartedAt = timing ? performance.now() : null;
  let requestOutcome = "completed";
  try {
    const value = await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": owner.gatewayWorkspaceId },
      body: JSON.stringify({ direction: "tail", turns: 256, include: HISTORY_INCLUDE, ...options }),
    }, undefined, requestTiming);
    return await validateThreadHistoryPage(owner, value, timing);
  } catch (error) {
    requestOutcome = "failed";
    throw error;
  } finally {
    if (timing) recordHistoryDuration(timing, "history.http_response_body_parse",
      requestTiming.requestToBodyParseMs ?? performance.now() - requestStartedAt, requestTiming.outcome ?? requestOutcome,
      requestTiming.responseBodyUtf8Bytes === undefined ? {} : { responseBodyUtf8Bytes: requestTiming.responseBodyUtf8Bytes });
  }
}

async function validateThreadHistoryPage(owner, value, timing) {
  const validationStartedAt = performance.now();
  let validationOutcome = "completed";
  try {
    assert.equal(value.data.thread_id, owner.threadId);
    assert.ok(value.data.items.every((turn) => turn.session_id === owner.sessionId));
    for (const turn of value.data.items) {
      canonicalUuid7(turn.turn_id, "turn");
      assert.deepEqual([turn.source_view_id, turn.source_view_revision],
        [value.data.source_view_id, value.data.source_view_revision], "Turn 坐标必须对应本页实际读取快照");
      assert.equal(typeof turn.job_id, "string");
      assert.ok(turn.job_id.length > 0);
      assert.notEqual(turn.turn_id, turn.job_id);
      assert.ok(turn.items.every((item) => item.session_id === owner.sessionId && item.job_id === turn.job_id));
    }
    const rootOrder = value.data.items.map((turn) => turn.root_logical_item_ordinal);
    assert.deepEqual(rootOrder, [...rootOrder].sort((left, right) => left - right));
    assert.equal(new Set(value.data.internal_executions.map((execution) => execution.execution_id)).size, value.data.internal_executions.length);
    for (const execution of value.data.internal_executions) assertInternalExecutionOwner(execution, owner);
    await assertCurrentInternalExecutionJobs(value.data.internal_executions, owner);
    assert.equal(new Set(value.data.items.map((turn) => turn.turn_id)).size, value.data.items.length);
    return value.data;
  } catch (error) {
    validationOutcome = "failed";
    throw error;
  } finally {
    recordHistoryTiming(timing, "history.single_page_validation", validationStartedAt, validationOutcome);
  }
}

function mergeThreadHistoryPages(pages) {
  const turns = new Map();
  const internals = new Map();
  for (const page of pages) {
    for (const turn of page.items) {
      assert.ok(!turns.has(turn.turn_id), "composite 续读不得重复 Turn");
      turns.set(turn.turn_id, turn);
    }
    for (const execution of page.internal_executions) {
      let merged = internals.get(execution.execution_id);
      if (!merged) {
        merged = { ...execution, display_parts: [], response_parts: [] };
        internals.set(execution.execution_id, merged);
      }
      assert.equal(merged.job_id, execution.job_id);
      for (const [field, identity] of [["display_parts", "item_id"], ["response_parts", "part_id"]]) {
        for (const part of execution[field]) {
          assert.ok(!merged[field].some((previous) => previous[identity] === part[identity]), "composite 续读不得重影 canonical 成员");
          merged[field].push(part);
        }
      }
    }
  }
  for (const execution of internals.values()) {
    execution.display_parts.sort((left, right) => left.logical_item_ordinal - right.logical_item_ordinal);
    execution.response_parts.sort((left, right) => left.source.logical_item_ordinal - right.source.logical_item_ordinal || left.source.part_ordinal - right.source.part_ordinal);
  }
  return { ...pages[0], items: [...turns.values()].sort((left, right) => left.root_logical_item_ordinal - right.root_logical_item_ordinal),
    internal_executions: [...internals.values()], has_more: false, next_cursor: null };
}

async function loadCompleteThreadHistory(owner, options = {}, timing = null) {
  const pages = [await loadThreadHistory(owner, options, timing)];
  const direction = ["head", "after"].includes(options.direction) ? "after" : "before";
  const continuationOptions = { ...options };
  delete continuationOptions.anchor_turn_id;
  delete continuationOptions.before_turns;
  delete continuationOptions.after_turns;
  while (pages.at(-1).has_more) {
    const page = pages.at(-1);
    assert.equal(typeof page.next_cursor, "string");
    pages.push(await loadThreadHistory(owner, { ...continuationOptions, direction, cursor: page.next_cursor }, timing));
  }
  return measureHistoryPhase(timing, "history.multi_page_merge_hydration", async () => mergeThreadHistoryPages(pages));
}

async function openOwner(owner) {
  await drainPageResponseBodies(owner.page);
  await owner.page.reload({ waitUntil: "domcontentloaded" });
  try {
    await openCatalogWorkspace(owner);
    const breadcrumb = (await ownerApi(owner, `/api/v1/session-catalog/breadcrumb/${owner.sessionId}`)).data;
    assert.equal(breadcrumb.items.at(-1).node_id, owner.sessionId, "打开会话的 breadcrumb 叶节点必须匹配目标会话");
    for (const node of breadcrumb.items.slice(0, -1)) {
      await expandCatalogRow(catalogRow(owner, node.node_id));
    }
    await selectSession(owner.page, owner.sessionId);
  } catch (error) {
    result.navigationFailure = {
      expected: { workspaceId: owner.gatewayWorkspaceId, sessionId: owner.sessionId },
      url: owner.page.url(),
      rows: await owner.page.locator(".session-resource-row").evaluateAll((elements) => elements.map((element) => ({
        testId: element.getAttribute("data-testid"), current: element.getAttribute("aria-current"), text: element.textContent,
      }))),
    };
    await writeFile(`${requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT")}/basic-chat-tool-loop-navigation-failure.html`, await owner.page.content());
    throw error;
  }
  if (!owner.child) return;
  await owner.page.getByRole("tab", { name: "运行与连接", exact: true }).click();
  await owner.page.locator(`.child-thread-item[data-thread-id="${owner.threadId}"] .child-thread-main`).click({ timeout: 30_000 });
  await owner.page.locator(`.child-thread-conversation[data-thread-id="${owner.threadId}"]`).waitFor({ state: "visible" });
}

async function createProductSession(surface, title) {
  const response = await ownerApi(surface, "/api/v1/sessions", {
    method: "POST", headers: { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": surface.gatewayWorkspaceId },
    body: JSON.stringify({ title, agent_id: "default" }),
  });
  const dto = response.data;
  assert.equal(dto.workspace_id, surface.workspaceIdentity, "业务 Session 身份必须来自后端 workspace identity");
  canonicalUuid7(dto.session_id, "ses");
  const main = await ownerApi(surface, `/api/v1/sessions/${dto.session_id}/main-thread`);
  assert.equal(main.data.session_id, dto.session_id);
  canonicalUuid7(main.data.thread_id, "thr");
  const owner = { ...surface, sessionId: dto.session_id, threadId: main.data.thread_id, child: false, title, dto,
    resource: { resource_identity: dto.resource_identity, uri: surface.uriTemplate.replace("__SESSION__", dto.session_id), scope: dto.scope } };
  const catalog = await sessionCatalogNode(owner);
  await threadRolloutLocator(owner, catalog);
  return owner;
}

async function acceptOwnerPrompt(owner, prompt) {
  let sent;
  if (!owner.child) {
    sent = await sendPrompt(owner, prompt);
    assert.equal(sent.accepted.thread_id, owner.threadId, "主聊天准入必须属于权威 main thread");
  } else {
    const pathname = `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/messages`;
    const container = owner.page.locator(`.child-thread-conversation[data-thread-id="${owner.threadId}"]`);
    const input = container.locator(`#child-thread-draft-${owner.threadId}`);
    try {
      await input.fill(prompt);
    } catch (error) {
      try {
        result.childInputFailure = { sessionId: owner.sessionId, threadId: owner.threadId, prompt,
          actionability: await input.evaluate((element) => {
            const rect = element.getBoundingClientRect();
            const style = getComputedStyle(element);
            return { disabled: element.disabled, readOnly: element.readOnly, connected: element.isConnected,
              hidden: element.hidden, display: style.display, visibility: style.visibility,
              rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
              value: element.value, documentVisibility: document.visibilityState };
          }) };
      } catch (diagnosticError) {
        result.childInputFailure = { sessionId: owner.sessionId, threadId: owner.threadId, diagnosticError: String(diagnosticError) };
      }
      throw error;
    }
    const [response, click] = await Promise.allSettled([
      owner.page.waitForResponse((entry) => new URL(entry.url()).pathname === pathname && entry.request().method() === "POST", { timeout: 30_000 }),
      container.getByRole("button", { name: "发送到子线程", exact: true }).click(),
    ]);
    if (click.status === "rejected") throw click.reason;
    if (response.status === "rejected") throw response.reason;
    const reply = response.value;
    assert.equal(reply.status(), 200, await reply.text());
    const body = await reply.json();
    assert.equal(body.data.thread_id, owner.threadId);
    canonicalUuid7(body.data.turn_id, "turn");
    assert.notEqual(body.data.turn_id, body.data.job_id);
    assert.equal(reply.request().postDataJSON().message.content, prompt);
    assert.equal(reply.request().headers()["x-boxteam-workspace-id"], owner.gatewayWorkspaceId);
    sent = { jobId: body.data.job_id, turnId: body.data.turn_id, accepted: body.data };
  }
  return { ...sent, prompt };
}

async function sendOwnerPrompt(owner, prompt) {
  const sent = await acceptOwnerPrompt(owner, prompt);
  await assertAcceptedTurn(owner, sent);
  return sent;
}

async function assertAcceptedTurn(owner, sent) {
  assert.equal(sent.accepted.thread_id, owner.threadId);
  assert.equal(typeof sent.accepted.message_id, "string");
  assert.ok(sent.accepted.message_id.length > 0);
  const history = await loadThreadHistory(owner, { turn_ids: [sent.turnId] });
  assert.equal(history.items.length, 1, "准入响应必须对应同一 owner 的权威历史 Turn");
  const turn = history.items[0];
  assert.equal(turn.turn_id, sent.turnId);
  assert.equal(turn.job_id, sent.jobId, "准入 Job 必须来自该 Turn 的真实 last execution");
  assert.deepEqual(turn.source_message_ids, [sent.accepted.message_id]);
  assert.deepEqual(turn.user_messages.map((message) => message.message_id), [sent.accepted.message_id]);
  assert.equal(turn.user_messages[0].content, sent.prompt);
  return turn;
}

function partCoordinate(part) {
  const source = part.source;
  assert.equal(typeof source?.item_id, "string");
  for (const key of ["item_sequence", "logical_item_ordinal", "part_ordinal", "elapsed_ms"]) assert.ok(Number.isInteger(source[key]), `${part.part_id} 缺少 ${key}`);
  assert.equal(typeof source.created_at, "string");
  return { part_id: part.part_id, kind: part.kind, item_id: source.item_id,
    item_sequence: source.item_sequence, logical_item_ordinal: source.logical_item_ordinal,
    part_ordinal: source.part_ordinal, created_at: source.created_at, elapsed_ms: source.elapsed_ms };
}

function expectedTranscript(history) {
  return [
    ...history.items.map((turn) => ({ kind: "turn", ordinal: turn.root_logical_item_ordinal,
      turnOrdinal: turn.ordinal, turnId: turn.turn_id })),
    ...history.internal_executions.flatMap((execution) => execution.display_parts.map((part) => ({ kind: "internal", ordinal: part.logical_item_ordinal, text: part.text }))),
  ].sort((left, right) => left.ordinal - right.ordinal);
}

async function locateAttachmentCard(owner, history, fileId, name, availability) {
  assert.equal(history.thread_id, owner.threadId, "附件历史必须属于当前目标 Thread");
  assert.ok(history.items.every((turn) => turn.session_id === owner.sessionId), "附件历史不能混入其它 Session 的 Turn");
  const matches = history.items.flatMap((turn) => turn.user_messages.flatMap((message) =>
    (message.attachments ?? [])
      .filter((attachment) => attachment.file_id === fileId
        && attachment.name === name
        && attachment.availability === availability)
      .map(() => turn)));
  assert.equal(matches.length, 1, `目标历史必须唯一包含 ${availability} 附件 ${fileId}`);
  const turn = matches[0];
  const transcript = expectedTranscript(history);
  const index = transcript.findIndex((entry) => entry.kind === "turn" && entry.turnId === turn.turn_id);
  assert.ok(index >= 0, `附件 Turn ${turn.turn_id} 必须存在于正式 transcript 索引`);
  const { cell, observed } = await visitTranscriptIndex(owner, index, transcript[index]);
  assert.deepEqual(observed, { kind: "turn", turnId: turn.turn_id }, "附件卡定位必须核对精确 owner 与 Turn");
  const article = cell.locator(`.chat-turn[data-turn-id="${turn.turn_id}"]`);
  await article.waitFor({ state: "visible" });
  assert.equal(await article.getAttribute("data-root-logical-item-ordinal"), String(turn.root_logical_item_ordinal));
  const card = article.locator(`.message-attachment[data-availability="${availability}"]`).filter({ hasText: name });
  await card.waitFor({ state: "visible" });
  assert.equal(await card.count(), 1, `目标 Turn 必须唯一显示 ${availability} 附件 ${name}`);
  return card;
}

async function readTurnViewportState(page, turnId) {
  return page.evaluate((targetTurnId) => {
    const shell = document.querySelector(".chat-stream-shell .chat-stream-virtual-shell");
    const scroller = document.querySelector(".chat-stream-shell .chat-virtual-list");
    if (!(shell instanceof HTMLElement) || !(scroller instanceof HTMLElement)) return null;
    const target = [...scroller.querySelectorAll(".chat-virtual-item[data-turn-id]")]
      .find((element) => element.getAttribute("data-turn-id") === targetTurnId);
    if (!target) return null;
    const turn = target.querySelector(".chat-turn");
    const viewport = scroller.getBoundingClientRect();
    const bounds = target.getBoundingClientRect();
    return {
      ownerSessionId: shell.getAttribute("data-owner-session-id"),
      ownerThreadId: shell.getAttribute("data-owner-thread-id"),
      turnId: target.getAttribute("data-turn-id"),
      rootLogicalItemOrdinal: turn?.getAttribute("data-root-logical-item-ordinal") ?? null,
      ariaCurrent: target.getAttribute("aria-current"),
      visible: bounds.bottom > viewport.top && bounds.top < viewport.bottom,
      scrollTop: scroller.scrollTop,
    };
  }, turnId);
}

async function assertLocatedTurn(owner, turn, timeoutMs = TRANSCRIPT_NAVIGATION_TIMEOUT_MS) {
  const form = owner.page.getByRole("form", { name: "跳到指定轮次", exact: true });
  const expectedStatus = `已定位第 ${turn.ordinal} 轮`;
  await waitUntil(async () => {
    const state = await readTurnViewportState(owner.page, turn.turn_id);
    const status = await form.getByRole("status").textContent().catch(() => null);
    return state?.ownerSessionId === owner.sessionId
      && state.ownerThreadId === owner.threadId
      && state.turnId === turn.turn_id
      && state.rootLogicalItemOrdinal === String(turn.root_logical_item_ordinal)
      && state.ariaCurrent === "location"
      && state.visible
      && status?.trim() === expectedStatus;
  }, `正式跳转控件定位 owner=${owner.sessionId}/${owner.threadId} Turn=${turn.turn_id}`, timeoutMs);
  const status = await form.getByRole("status").textContent();
  assert.equal(status?.trim(), expectedStatus);
  return await readTurnViewportState(owner.page, turn.turn_id);
}

async function visitTranscriptIndex(owner, index, expected, timeoutMs = TRANSCRIPT_NAVIGATION_TIMEOUT_MS, navigationEvidence = null) {
  if (expected.kind === "turn") {
    const form = owner.page.getByRole("form", { name: "跳到指定轮次", exact: true });
    const input = form.getByLabel("轮次 ID", { exact: true });
    await input.fill(expected.turnId);
    await form.getByRole("button", { name: "跳到指定轮次", exact: true }).click();
    if (navigationEvidence) navigationEvidence.counts[navigationEvidence.stage] += 1;
    const target = owner.page.locator(
      `.chat-stream-shell .chat-stream-virtual-shell .chat-virtual-item[data-turn-id="${expected.turnId}"]`,
    );
    await waitUntil(async () => {
      const state = await readTurnViewportState(owner.page, expected.turnId);
      const status = await form.getByRole("status").textContent().catch(() => null);
      return state?.ownerSessionId === owner.sessionId
        && state.ownerThreadId === owner.threadId
        && state.rootLogicalItemOrdinal === String(expected.ordinal)
        && state.ariaCurrent === "location"
        && state.visible
        && status?.trim() === `已定位第 ${expected.turnOrdinal} 轮`;
    }, `真实跳转控件定位 transcript Turn ${expected.turnId}`, timeoutMs);
    const observed = await readTurnViewportState(owner.page, expected.turnId);
    const status = await form.getByRole("status").textContent();
    assert.equal(observed?.ownerSessionId, owner.sessionId);
    assert.equal(observed?.ownerThreadId, owner.threadId);
    assert.equal(observed?.rootLogicalItemOrdinal, String(expected.ordinal));
    assert.equal(observed?.ariaCurrent, "location");
    assert.equal(status?.trim(), `已定位第 ${expected.turnOrdinal} 轮`);
    return { cell: target, observed: { kind: "turn", turnId: expected.turnId } };
  }
  const item = owner.page.locator(`.chat-stream-shell .chat-virtual-list [data-testid="virtuoso-item-list"] > [data-index="${index}"]`);
  const scroller = owner.page.locator(".chat-stream-shell .chat-virtual-list");
  let observed;
  await waitUntil(async () => {
    const snapshot = await scroller.evaluate((element, { desired, entry }) => {
      // 挂载与 prepend 会重排 index；可见性、身份及坐标必须在同一 DOM 快照核对。
      const viewport = element.getBoundingClientRect();
      const cells = [...element.querySelectorAll('[data-testid="virtuoso-item-list"] > [data-index]')];
      const cell = cells.find((candidate) => Number(candidate.getAttribute("data-index")) === desired);
      if (cell) {
        const bounds = cell.getBoundingClientRect();
        if (bounds.bottom > viewport.top && bounds.top < viewport.bottom) {
          const turns = cell.querySelectorAll(".chat-turn");
          const displays = cell.querySelectorAll(".chat-internal-execution-display");
          if (turns.length + displays.length === 1) {
            if (entry.kind === "turn") {
              const turn = turns[0];
              if (turn?.getAttribute("data-turn-id") === entry.turnId
                && turn.getAttribute("data-root-logical-item-ordinal") === String(entry.ordinal)) {
                return { observed: { kind: "turn", turnId: turn.getAttribute("data-turn-id") }, direction: 0, scrollAmount: 0 };
              }
            } else {
              const text = displays[0]?.querySelector(".chat-internal-execution-text")?.textContent;
              if (text === entry.text) return { observed: { kind: "internal", text }, direction: 0, scrollAmount: 0 };
            }
          }
        }
      }
      const visibleIndices = cells.flatMap((candidate) => {
        const bounds = candidate.getBoundingClientRect();
        return bounds.bottom > viewport.top && bounds.top < viewport.bottom
          ? [Number(candidate.getAttribute("data-index"))]
          : [];
      });
      if (visibleIndices.length === 0) return { observed: null, direction: 0, scrollAmount: 0 };
      const direction = desired < Math.min(...visibleIndices) ? -1
        : desired > Math.max(...visibleIndices) ? 1
          : desired < visibleIndices[0] ? -1 : 1;
      return {
        observed: null,
        direction,
        scrollAmount: Math.max(1, Math.floor(element.clientHeight * 0.7)),
      };
    }, { desired: index, entry: expected });
    observed = snapshot.observed;
    if (observed !== null) return true;
    if (snapshot.direction === 0) return false;

    const bounds = await scroller.boundingBox();
    if (!bounds) return false;
    await scroller.hover({
      position: {
        x: Math.min(Math.max(1, bounds.width / 2), Math.max(1, bounds.width - 1)),
        y: Math.min(Math.max(1, bounds.height / 2), Math.max(1, bounds.height - 1)),
      },
    });
    // 负 deltaY 是向上浏览更早历史，正 deltaY 是向下浏览更新历史；使用真实输入更新产品的 follow 状态。
    await owner.page.mouse.wheel(0, snapshot.direction * snapshot.scrollAmount);
    return false;
  }, `真实虚拟列表滚动到同一身份的 transcript ${index}`, timeoutMs);
  return { cell: item, observed };
}

async function leaveLatestFollowAtVisibleTurn(owner, index, expected) {
  assert.equal(expected.kind, "turn", "离开 latest-follow 必须绑定历史 Turn 身份");
  const scroller = owner.page.locator(".chat-stream-shell .chat-virtual-list");
  const jumpToLatest = owner.page.getByRole("button", { name: "跳到最新消息", exact: true });
  const deadline = Date.now() + TRANSCRIPT_NAVIGATION_TIMEOUT_MS;
  let latestState = null;
  let wheelPerformed = false;

  const readState = () => owner.page.evaluate(({ desired, entry }) => {
    const element = document.querySelector(".chat-stream-shell .chat-virtual-list");
    if (!(element instanceof HTMLElement)) return null;
    const viewport = element.getBoundingClientRect();
    const cell = [...element.querySelectorAll('[data-testid="virtuoso-item-list"] > [data-index]')]
      .find((candidate) => Number(candidate.getAttribute("data-index")) === desired);
    if (!cell) return null;
    const turns = cell.querySelectorAll(".chat-turn");
    const displays = cell.querySelectorAll(".chat-internal-execution-display");
    const turn = turns.length === 1 && displays.length === 0 ? turns[0] : null;
    const bounds = cell.getBoundingClientRect();
    const maxScrollTop = Math.max(0, element.scrollHeight - element.clientHeight);
    return {
      turnCount: turns.length,
      internalDisplayCount: displays.length,
      turnId: turn?.getAttribute("data-turn-id") ?? null,
      rootLogicalItemOrdinal: turn?.getAttribute("data-root-logical-item-ordinal") ?? null,
      identityMatches: turn?.getAttribute("data-turn-id") === entry.turnId
        && turn.getAttribute("data-root-logical-item-ordinal") === String(entry.ordinal),
      visible: bounds.bottom > viewport.top && bounds.top < viewport.bottom,
      safeUpwardWheel: Math.max(0, Math.floor(Math.min(
        element.scrollTop,
        viewport.bottom - bounds.top - 1,
      ))),
      scrollTop: element.scrollTop,
      distanceToBottom: Math.max(0, maxScrollTop - element.scrollTop),
      viewport: { top: viewport.top, bottom: viewport.bottom },
      target: { top: bounds.top, bottom: bounds.bottom },
    };
  }, { desired: index, entry: expected });

  for (let attempt = 0; attempt < 4 && Date.now() < deadline; attempt += 1) {
    let state = await readState();
    if (!state?.identityMatches || !state.visible) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      const { observed } = await visitTranscriptIndex(owner, index, expected, remaining);
      assert.deepEqual(observed, { kind: "turn", turnId: expected.turnId },
        "off-follow 重定位必须核对同一历史 Turn");
      state = await readState();
    }
    if (!state?.identityMatches || !state.visible) {
      latestState = state;
      continue;
    }

    const jumpVisible = await jumpToLatest.isVisible();
    if (wheelPerformed
      && state.distanceToBottom > VIRTUOSO_AT_BOTTOM_THRESHOLD_PX
      && jumpVisible) {
      return state;
    }

    // ChatPanel 未覆盖 Virtuoso 默认 4px 底部阈值；多滚 1px 后才算离底。
    const wheelAmount = Math.max(
      1,
      Math.floor(VIRTUOSO_AT_BOTTOM_THRESHOLD_PX - state.distanceToBottom) + 1,
    );

    const bounds = await scroller.boundingBox();
    if (!bounds) throw new Error("历史 Turn 离底前无法取得真实虚拟列表边界");
    await scroller.hover({
      position: {
        x: Math.min(Math.max(1, bounds.width / 2), Math.max(1, bounds.width - 1)),
        y: Math.min(Math.max(1, bounds.height / 2), Math.max(1, bounds.height - 1)),
      },
    });
    await owner.page.mouse.wheel(0, -wheelAmount);
    wheelPerformed = true;
    await owner.page.evaluate(() => new Promise((resolve) => requestAnimationFrame(
      () => requestAnimationFrame(resolve),
    )));

    // 若几何空间不足，真实滚轮可能暂时让目标离开视口；下一轮会用同一身份重新导航。
    latestState = await readState();
    if (latestState?.identityMatches
      && latestState.visible
      && latestState.distanceToBottom > VIRTUOSO_AT_BOTTOM_THRESHOLD_PX
      && await jumpToLatest.isVisible()) {
      return latestState;
    }
  }

  throw new Error(`未能让同一历史 Turn 在离开 latest-follow 后保持可见: ${JSON.stringify({
    index, turnId: expected.turnId, ordinal: expected.ordinal, latestState,
  })}`);
}

async function assertTranscriptDom(owner, history, stage, { onTurn = null, navigationCounts = null, historyTiming = null } = {}) {
  const expected = expectedTranscript(history);
  const turnsById = new Map(history.items.map((turn) => [turn.turn_id, turn]));
  let turnAnchorJumpCount = 0;
  let inspectedIndex = null;
  try {
  if (owner.child) {
    const turns = owner.page.locator(`.child-thread-conversation[data-thread-id="${owner.threadId}"] .chat-turn`);
    await waitUntil(async () => await turns.count() === history.items.length, `${stage}: child Turn 数量`);
    assert.deepEqual(await turns.evaluateAll((elements) => elements.map((element) => element.getAttribute("data-turn-id"))),
      history.items.map((turn) => turn.turn_id), "可见 Turn 顺序必须由 root logical ordinal 决定");
    if (onTurn) for (const turn of history.items) await onTurn(turn);
    return { turnAnchorJumpCount };
  }
  const shell = owner.page.locator(".chat-stream-shell .chat-stream-virtual-shell");
  await waitUntil(async () => {
    if (Number(await shell.getAttribute("data-turn-count")) === history.items.length
      && Number(await shell.getAttribute("data-transcript-item-count")) === expected.length) return true;
    const scroller = owner.page.locator(".chat-stream-shell .chat-virtual-list");
    await scroller.hover();
    await owner.page.mouse.wheel(0, -await scroller.evaluate((element) => Math.max(1, element.clientHeight)));
    return false;
  }, `${stage}: 正常向上滚动加载全部 Turn 与 standalone internal transcript`);
  const actual = [];
  for (const [index, entry] of expected.entries()) {
    inspectedIndex = index;
    const navigationEvidence = navigationCounts ? { counts: navigationCounts, stage } : null;
    const locateTarget = entry.kind === "turn" && onTurn && stage === "live"
      && historyTiming?.targetTurnId === entry.turnId ? historyTiming : null;
    const { observed } = await measureHistoryPhase(locateTarget, `transcript.old_turn_virtual_location.${stage}`,
      () => visitTranscriptIndex(owner, index, entry, TRANSCRIPT_NAVIGATION_TIMEOUT_MS, navigationEvidence));
    assert.deepEqual(observed, entry.kind === "turn" ? { kind: "turn", turnId: entry.turnId } : { kind: "internal", text: entry.text });
    actual.push(observed);
    if (entry.kind === "turn") {
      turnAnchorJumpCount += 1;
      if (onTurn) {
        const turn = turnsById.get(entry.turnId);
        assert.ok(turn, `${stage}: transcript Turn 必须属于同一份 owner history`);
        await onTurn(turn);
      }
    }
  }
  assert.deepEqual(actual.filter((entry) => entry.kind === "turn").map((entry) => entry.turnId), history.items.map((turn) => turn.turn_id));
  if (onTurn) assert.equal(turnAnchorJumpCount, history.items.length, `${stage}: 每个 owner Turn 必须只在 transcript 遍历中跳转一次`);
  return { turnAnchorJumpCount };
  } catch (error) {
    const expectedOffset = Math.max(0, (inspectedIndex ?? 0) - 8);
    const failure = {
      stage, error: String(error), inspectedIndex,
      target: { pageId: owner.page.diagnosticId, documentId: owner.page.diagnosticDocumentId,
        workspaceId: owner.gatewayWorkspaceId, sessionId: owner.sessionId, threadId: owner.threadId,
        child: Boolean(owner.child), url: owner.page.url() },
      expectedCount: expected.length, expectedOffset,
      expected: expected.slice(expectedOffset, expectedOffset + 64).map((entry) => entry.kind === "turn"
        ? { kind: entry.kind, turnId: entry.turnId, rootLogicalItemOrdinal: entry.ordinal,
          turnOrdinal: history.items.find((turn) => turn.turn_id === entry.turnId)?.ordinal }
        : { kind: entry.kind, logicalItemOrdinal: entry.ordinal, text: entry.text?.slice(0, 300) }),
    };
    try {
      // 只记录同次失败的有界 DOM 坐标；诊断不能改变原断言、滚动或业务事实。
      failure.dom = await owner.page.evaluate(() => {
        const shell = document.querySelector(".chat-stream-shell .chat-stream-virtual-shell");
        const scroller = document.querySelector(".chat-stream-shell .chat-virtual-list");
        const viewport = scroller?.getBoundingClientRect();
        const cells = [...document.querySelectorAll('.chat-stream-shell [data-testid="virtuoso-item-list"] > [data-index]')];
        return {
          activeSessionIds: [...document.querySelectorAll('.session-item.active[data-session-id]')]
            .slice(0, 8).map((element) => element.getAttribute("data-session-id")),
          shell: shell ? { sessionId: shell.getAttribute("data-owner-session-id"), firstItemIndex: shell.getAttribute("data-first-item-index"),
            firstTurnId: shell.getAttribute("data-first-turn-id"), turnCount: shell.getAttribute("data-turn-count"),
            transcriptItemCount: shell.getAttribute("data-transcript-item-count") } : null,
          renderedCount: cells.length,
          cells: cells.slice(0, 64).map((cell) => {
            const turn = cell.querySelector(".chat-turn");
            const display = cell.querySelector(".chat-internal-execution-display");
            const bounds = cell.getBoundingClientRect();
            return { index: cell.getAttribute("data-index"), itemIndex: cell.getAttribute("data-item-index"),
              visible: Boolean(viewport && bounds.bottom > viewport.top && bounds.top < viewport.bottom),
              turnId: turn?.getAttribute("data-turn-id") ?? null, rootLogicalItemOrdinal: turn?.getAttribute("data-root-logical-item-ordinal") ?? null,
              internalText: display?.querySelector(".chat-internal-execution-text")?.textContent?.slice(0, 300) ?? null };
          }),
          childTurns: [...document.querySelectorAll(".child-thread-conversation .chat-turn")].slice(0, 64)
            .map((turn) => ({ threadId: turn.closest(".child-thread-conversation")?.getAttribute("data-thread-id"),
              turnId: turn.getAttribute("data-turn-id"), rootLogicalItemOrdinal: turn.getAttribute("data-root-logical-item-ordinal") })),
        };
      });
    } catch (captureError) {
      failure.captureError = String(captureError);
    }
    transcriptDomFailures.push(failure);
    throw error;
  }
}

async function assertTurnDom(owner, turn, stage, history, timing = null,
  { navigationCounts = null, transcriptLocated = false } = {}) {
  let phase = transcriptLocated || owner.child ? "Turn article visibility and root identity" : "transcript navigation";
  let expected = [];
  let latestActual = null;
  const transcript = owner.page.locator(owner.child
    ? `.child-thread-conversation[data-thread-id="${owner.threadId}"]` : ".chat-stream-shell");
  const article = transcript.locator(`.chat-turn[data-turn-id="${turn.turn_id}"]`);
  try {
    if (!owner.child && !transcriptLocated) {
      const transcriptItems = expectedTranscript(history);
      const index = transcriptItems.findIndex((entry) => entry.turnId === turn.turn_id);
      phase = "reconfirm transcript item identity";
      const locateTarget = stage === "live" && timing?.targetTurnId === turn.turn_id ? timing : null;
      const navigationEvidence = navigationCounts ? { counts: navigationCounts, stage } : null;
      const { observed } = await measureHistoryPhase(locateTarget, `transcript.old_turn_virtual_location.${stage}`,
        () => visitTranscriptIndex(owner, index, transcriptItems[index], TRANSCRIPT_NAVIGATION_TIMEOUT_MS, navigationEvidence));
      assert.deepEqual(observed, { kind: "turn", turnId: turn.turn_id }, `${stage}: 回看 Turn 必须核对同一身份`);
    }

    phase = "Turn article visibility and root identity";
    await article.waitFor({ state: "visible", timeout: 60_000 });
    assert.equal(await article.getAttribute("data-root-logical-item-ordinal"), String(turn.root_logical_item_ordinal));
    expected = turn.response_parts.map(partCoordinate);
    const logical = expected.map((part) => [part.logical_item_ordinal, part.part_ordinal]);
    assert.deepEqual(logical, [...logical].sort((left, right) => left[0] - right[0] || left[1] - right[1]), `${stage}: history 必须按 view logical ordinal 排序`);
    assert.equal(new Set(expected.map((part) => part.part_id)).size, expected.length);
    const activity = article.locator(".chat-thinking");
    await activity.waitFor({ state: "visible" });
    phase = "backend activity statistics";
    await waitUntil(async () => await activity.getAttribute("data-item-count") === String(turn.activity_stats.item_count)
      && await activity.getAttribute("data-duration-ms") === (turn.activity_stats.duration_ms == null ? null : String(turn.activity_stats.duration_ms)), `${stage} 后端活动统计进入 DOM`);
    const toggle = activity.locator(".chat-thinking-toggle");
    phase = "activity summary toggle contract";
    if (turn.activity_stats.item_count > 0) {
      assert.equal(await toggle.evaluate((element) => element.tagName), "BUTTON");
      if (await toggle.getAttribute("aria-expanded") !== "true") await toggle.click();
    } else {
      assert.equal(await toggle.getAttribute("role"), "status", `${stage}: 零活动 Turn 仍显示静态统计摘要`);
      assert.equal(await toggle.getAttribute("aria-expanded"), null);
      assert.equal(await activity.locator("button").count(), 0, `${stage}: 零活动 Turn 不得添加展开按钮`);
    }
    if (!owner.child) {
      phase = "reconfirm transcript location after activity expansion";
      const located = await assertLocatedTurn(owner, turn);
      assert.equal(located.rootLogicalItemOrdinal, String(turn.root_logical_item_ordinal),
        `${stage}: 活动详情读取后必须仍定位同一 Turn`);
    }
    phase = "historical boundary and preview";
    const boundary = expectedTurnBoundary(turn);
    assert.equal(await activity.getAttribute("data-status-kind"), boundary?.kind ?? null, `${stage}: 终态提示必须对应后端 Turn/tool 事实`);
    assert.equal(await activity.locator(".chat-thinking-preview").innerText(), expectedActivityPreview(turn.activity_stats.duration_ms, turn.activity_stats.item_count, boundary));
    phase = "canonical response part count";
    const countTarget = stage === "live" && timing?.targetTurnId === turn.turn_id ? timing : null;
    const pageDomNodeCount = countTarget
      ? await owner.page.evaluate(() => document.querySelectorAll("*").length)
      : null;
    await measureHistoryPhase(countTarget, `transcript.canonical_response_part_count.${stage}`, () => waitUntil(async () => {
      latestActual = await article.locator("[data-response-sources]").evaluateAll((elements) => elements.flatMap((element) => JSON.parse(element.getAttribute("data-response-sources"))));
      return latestActual.length === expected.length;
    }, `${stage} 全部 canonical response part 展示`), () => ({
      expectedPartCount: expected.length,
      livePartCount: latestActual?.length ?? null,
      pageDomNodeCount,
    }));
    phase = "canonical response part identity/order/elapsed";
    assert.deepEqual(latestActual.map(partCoordinate), expected, `${stage}: actual DOM 的 Item identity/order/elapsed 与 history 不一致`);
    return { turnId: turn.turn_id, rootOrdinal: turn.root_logical_item_ordinal, activity: turn.activity_stats, parts: expected,
      userMessages: turn.user_messages.map((message) => ({ messageId: message.message_id, content: message.content })) };
  } catch (error) {
    const summarize = (parts) => {
      const coordinate = (part) => {
        const source = part?.source ?? part;
        const text = (value) => typeof value === "string" ? value.slice(0, 160) : null;
        const strings = [part?.part_id, part?.kind, source?.item_id, source?.created_at];
        return {
          part_id: text(part?.part_id),
          kind: text(part?.kind),
          item_id: text(source?.item_id),
          item_sequence: source?.item_sequence ?? null,
          logical_item_ordinal: source?.logical_item_ordinal ?? null,
          part_ordinal: source?.part_ordinal ?? null,
          created_at: text(source?.created_at),
          elapsed_ms: source?.elapsed_ms ?? null,
          coordinateValuesTruncated: strings.some((value) => typeof value === "string" && value.length > 160),
        };
      };
      const limit = 64;
      return {
        count: parts.length,
        countKnown: true,
        coordinates: parts.length <= limit
          ? parts.map(coordinate)
          : [...parts.slice(0, limit / 2), ...parts.slice(-limit / 2)].map(coordinate),
        coordinatesTruncated: parts.length > limit,
      };
    };
    const failure = {
      stage,
      phase,
      error: String(error).slice(0, 2_000),
      target: {
        pageId: owner.page.diagnosticId,
        documentId: owner.page.diagnosticDocumentId,
        workspaceId: owner.gatewayWorkspaceId,
        sessionId: owner.sessionId,
        threadId: owner.threadId,
        turnId: turn.turn_id,
        child: Boolean(owner.child),
        url: owner.page.url().slice(0, 512),
      },
      expected: summarize(expected.length > 0 ? expected : turn.response_parts ?? []),
    };
    failure.actual = latestActual === null
      ? { count: null, countKnown: false, coordinates: [], coordinatesAvailable: false }
      : summarize(latestActual);
    try {
      failure.dom = await owner.page.evaluate(({ threadId, turnId, child }) => {
        const shell = document.querySelector(".chat-stream-shell .chat-stream-virtual-shell");
        const scroller = document.querySelector(".chat-stream-shell .chat-virtual-list");
        const viewport = scroller?.getBoundingClientRect();
        const cellNodes = document.querySelectorAll('.chat-stream-shell [data-testid="virtuoso-item-list"] > [data-index]');
        const cellLimit = 64;
        const cells = [];
        for (let index = 0; index < Math.min(cellNodes.length, cellLimit); index += 1) cells.push(cellNodes[index]);
        const activeSessionNodes = document.querySelectorAll('.session-item.active[data-session-id]');
        const activeSessionLimit = 8;
        const activeSessionIds = [];
        for (let index = 0; index < Math.min(activeSessionNodes.length, activeSessionLimit); index += 1) {
          activeSessionIds.push(activeSessionNodes[index].getAttribute("data-session-id")?.slice(0, 160) ?? null);
        }
        const articleSelector = `.chat-turn[data-turn-id="${CSS.escape(turnId)}"]`;
        const article = child
          ? document.querySelector(`.child-thread-conversation[data-thread-id="${CSS.escape(threadId)}"] ${articleSelector}`)
          : document.querySelector(`.chat-stream-shell ${articleSelector}`);
        const activity = article?.querySelector(".chat-thinking");
        const toggle = activity?.querySelector(".chat-thinking-toggle");
        const articleBounds = article?.getBoundingClientRect();
        const responseSourceNodes = article?.querySelectorAll("[data-response-sources]") ?? null;
        const responseSourceNodeLimit = 32;
        const attributeByteLimit = 16 * 1024;
        const aggregateAttributeByteLimit = 64 * 1024;
        const partSampleLimit = 64;
        const coordinateTextLimit = 160;
        const responseSourceSamples = [];
        const responsePartCoordinates = [];
        let inspectedAttributeBytes = 0;
        let totalParts = 0;
        let totalPartsKnown = Boolean(article);
        let responsePartsTruncated = false;
        const sourceNodeCount = responseSourceNodes?.length ?? null;
        const sampledSourceNodeCount = Math.min(sourceNodeCount ?? 0, responseSourceNodeLimit);
        if (sourceNodeCount !== null && sourceNodeCount > responseSourceNodeLimit) {
          totalPartsKnown = false;
          responsePartsTruncated = true;
        }
        for (let nodeIndex = 0; nodeIndex < sampledSourceNodeCount; nodeIndex += 1) {
          const raw = responseSourceNodes[nodeIndex].getAttribute("data-response-sources");
          let attributeBytes = null;
          let attributeByteLimitExceeded = raw !== null && raw.length > attributeByteLimit;
          let aggregateAttributeByteLimitExceeded = false;
          let partCount = null;
          let partCountKnown = false;
          let parseError = null;
          if (raw !== null && !attributeByteLimitExceeded) {
            attributeBytes = new TextEncoder().encode(raw).byteLength;
            attributeByteLimitExceeded = attributeBytes > attributeByteLimit;
            aggregateAttributeByteLimitExceeded = !attributeByteLimitExceeded
              && inspectedAttributeBytes + attributeBytes > aggregateAttributeByteLimit;
          }
          if (raw === null || attributeByteLimitExceeded || aggregateAttributeByteLimitExceeded) {
            totalPartsKnown = false;
            responsePartsTruncated = true;
          } else {
            inspectedAttributeBytes += attributeBytes;
            try {
              const parts = JSON.parse(raw);
              if (!Array.isArray(parts)) throw new Error("data-response-sources 不是数组");
              partCount = parts.length;
              partCountKnown = true;
              totalParts += parts.length;
              const sampleCount = Math.min(parts.length, partSampleLimit - responsePartCoordinates.length);
              if (parts.length > sampleCount) responsePartsTruncated = true;
              for (let partIndex = 0; partIndex < sampleCount; partIndex += 1) {
                const part = parts[partIndex];
                const source = part?.source ?? part;
                const strings = [part?.part_id, part?.kind, source?.item_id, source?.created_at];
                const clipped = (value) => typeof value === "string" ? value.slice(0, coordinateTextLimit) : null;
                responsePartCoordinates.push({ nodeIndex, partIndex,
                  part_id: clipped(part?.part_id), kind: clipped(part?.kind), item_id: clipped(source?.item_id),
                  item_sequence: source?.item_sequence ?? null, logical_item_ordinal: source?.logical_item_ordinal ?? null,
                  part_ordinal: source?.part_ordinal ?? null, created_at: clipped(source?.created_at),
                  elapsed_ms: source?.elapsed_ms ?? null,
                  coordinateValuesTruncated: strings.some((value) => typeof value === "string" && value.length > coordinateTextLimit) });
              }
            } catch (error) {
              totalPartsKnown = false;
              responsePartsTruncated = true;
              parseError = String(error).slice(0, 160);
            }
          }
          responseSourceSamples.push({ nodeIndex,
            attributeCodeUnits: raw?.length ?? null, attributeBytes,
            attributeByteLimitExceeded, aggregateAttributeByteLimitExceeded,
            attributeMissing: raw === null, partCount, partCountKnown, parseError });
        }
        return {
          activeSessionIds,
          activeSessionIdsTruncated: activeSessionNodes.length > activeSessionLimit,
          shell: shell ? {
            sessionId: shell.getAttribute("data-owner-session-id")?.slice(0, 160) ?? null,
            firstItemIndex: shell.getAttribute("data-first-item-index")?.slice(0, 80) ?? null,
            firstTurnId: shell.getAttribute("data-first-turn-id")?.slice(0, 160) ?? null,
            turnCount: shell.getAttribute("data-turn-count")?.slice(0, 80) ?? null,
            transcriptItemCount: shell.getAttribute("data-transcript-item-count")?.slice(0, 80) ?? null,
          } : null,
          scroll: scroller ? {
            top: scroller.scrollTop,
            height: scroller.scrollHeight,
            clientHeight: scroller.clientHeight,
            viewport: viewport ? { top: viewport.top, bottom: viewport.bottom, left: viewport.left, right: viewport.right } : null,
          } : null,
          targetTurn: article ? {
            mounted: article.isConnected,
            visible: Boolean(viewport && articleBounds && articleBounds.bottom > viewport.top && articleBounds.top < viewport.bottom),
            rootLogicalItemOrdinal: article.getAttribute("data-root-logical-item-ordinal")?.slice(0, 80) ?? null,
            cellIndex: article.closest("[data-index]")?.getAttribute("data-index")?.slice(0, 80) ?? null,
            expanded: toggle?.getAttribute("aria-expanded") ?? null,
            loading: activity?.querySelector('[role="status"]')?.textContent?.slice(0, 160) ?? null,
            error: activity?.querySelector('[role="alert"]')?.textContent?.slice(0, 160) ?? null,
          } : { mounted: false, visible: false },
          renderedCount: cellNodes.length,
          renderedCountKnown: true,
          cellsTruncated: cellNodes.length > cellLimit,
          cells: cells.map((cell) => {
            const turn = cell.querySelector(".chat-turn");
            const bounds = cell.getBoundingClientRect();
            return {
              index: cell.getAttribute("data-index")?.slice(0, 80) ?? null,
              itemIndex: cell.getAttribute("data-item-index")?.slice(0, 80) ?? null,
              visible: Boolean(viewport && bounds.bottom > viewport.top && bounds.top < viewport.bottom),
              turnId: turn?.getAttribute("data-turn-id")?.slice(0, 160) ?? null,
              rootLogicalItemOrdinal: turn?.getAttribute("data-root-logical-item-ordinal")?.slice(0, 80) ?? null,
            };
          }),
          responseSources: {
            nodeCount: sourceNodeCount,
            nodeCountKnown: sourceNodeCount !== null,
            sampledNodeCount: responseSourceSamples.length,
            nodesTruncated: sourceNodeCount !== null && sourceNodeCount > responseSourceNodeLimit,
            attributeBytesInspected: inspectedAttributeBytes,
            attributeBytesLimit: aggregateAttributeByteLimit,
            samples: responseSourceSamples,
            totalPartCount: totalPartsKnown ? totalParts : null,
            totalPartCountKnown: totalPartsKnown,
            sampledPartCount: responsePartCoordinates.length,
            partSampleLimit,
            partsTruncated: responsePartsTruncated,
            coordinates: responsePartCoordinates,
          },
        };
      }, { threadId: owner.threadId, turnId: turn.turn_id, child: Boolean(owner.child) });
    } catch (captureError) {
      failure.captureError = String(captureError).slice(0, 500);
    }
    transcriptDomFailures.push(failure);
    throw error;
  }
}

async function assertInternalHistory(owner, history, stage) {
  const facts = [];
  for (const execution of history.internal_executions) {
    assertInternalExecutionOwner(execution, owner);
    assert.ok(!history.items.some((turn) => turn.job_id === execution.job_id), "internal execution 不得伪造用户 Turn");
    const parts = execution.response_parts.map(partCoordinate);
    assert.deepEqual(parts.map((part) => [part.logical_item_ordinal, part.part_ordinal]),
      parts.map((part) => [part.logical_item_ordinal, part.part_ordinal]).sort((left, right) => left[0] - right[0] || left[1] - right[1]));
    if (owner.child) {
      const report = owner.page.locator(`.child-thread-conversation[data-thread-id="${owner.threadId}"] .child-thread-report[data-execution-id="${execution.execution_id}"]`);
      await report.waitFor({ state: "visible" });
      for (const part of execution.response_parts.filter((entry) => entry.kind === "text" || entry.kind === "final_text")) {
        if (part.text) {
          const rendered = await report.locator(".chat-markdown").allTextContents();
          assert.ok(rendered.some((text) => text.includes(part.text)), `${stage}: internal report 正文丢失: ${JSON.stringify({ expected: part.text, rendered })}`);
        }
      }
    }
    facts.push({ executionId: execution.execution_id, jobId: execution.job_id, parts, display: execution.display_parts });
  }
  if (!owner.child) {
    for (const [index, part] of expectedTranscript(history).entries()) {
      if (part.kind !== "internal") continue;
      const { cell } = await visitTranscriptIndex(owner, index, part);
      const display = cell.locator(".chat-internal-execution-display");
      assert.equal(await display.locator(".chat-internal-execution-text").textContent(), part.text);
      assert.equal(await display.locator(".chat-internal-execution-label").textContent(), "会话生成");
      const html = await display.evaluate((element) => element.outerHTML);
      assert.ok(!html.includes("data-turn-id"), "安全 internal notice 不能伪造 Turn");
      for (const execution of history.internal_executions) {
        for (const identity of [execution.execution_id, execution.job_id,
          ...execution.display_parts.flatMap((part) => Object.values(part.provenance).filter((value) => typeof value === "string"))]) {
          assert.ok(!html.includes(identity), "安全 internal notice DOM 不能暴露内部来源 identity");
        }
      }
    }
  }
  return facts;
}

async function verifyOwnerTurns(owner, { reload = true, historyTiming = null } = {}) {
  const page = await loadCompleteThreadHistory(owner, {}, historyTiming);
  const navigationCounts = { live: 0, refresh: 0 };
  const verifyStage = async (stage) => {
    const factsByTurnId = new Map();
    const transcript = await assertTranscriptDom(owner, page, stage, {
      navigationCounts,
      historyTiming: stage === "live" ? historyTiming : null,
      onTurn: async (turn) => {
        factsByTurnId.set(turn.turn_id, await assertTurnDom(owner, turn, stage, page,
          stage === "live" ? historyTiming : null, { navigationCounts, transcriptLocated: true }));
      },
    });
    const expectedAnchorJumps = owner.child ? 0 : page.items.length;
    assert.equal(navigationCounts[stage], expectedAnchorJumps,
      `${stage}: 每个 owner Turn 必须恰好触发一次 DOM 锚点跳转`);
    assert.equal(transcript.turnAnchorJumpCount, expectedAnchorJumps,
      `${stage}: transcript 完整性遍历必须覆盖全部 Turn 锚点跳转`);
    assert.equal(factsByTurnId.size, page.items.length, `${stage}: 每个 owner Turn 都必须完成详情校验`);
    return {
      facts: page.items.map((turn) => factsByTurnId.get(turn.turn_id)),
      turnAnchorJumpCount: navigationCounts[stage],
    };
  };
  const live = await verifyStage("live");
  const facts = live.facts;
  const internal = await assertInternalHistory(owner, page, "live");
  // 必须在测试驱动主动刷新之前证明当前页面已完成真实历史校准。
  historyLiveProofs.push({ pageId: owner.page.diagnosticId, documentId: owner.page.diagnosticDocumentId,
    sessionId: owner.sessionId, threadId: owner.threadId, workspaceId: owner.gatewayWorkspaceId,
    turnIds: page.items.map((turn) => turn.turn_id),
    internalExecutionIds: page.internal_executions.map((execution) => execution.execution_id), verifiedAt: Date.now() });
  const turnNavigationProof = {
    pageId: owner.page.diagnosticId, documentId: owner.page.diagnosticDocumentId,
    sessionId: owner.sessionId, threadId: owner.threadId, child: Boolean(owner.child),
    expectedTurnCount: page.items.length,
    liveAnchorJumps: live.turnAnchorJumpCount,
    refreshAnchorJumps: null,
  };
  historyTurnNavigationProofs.push(turnNavigationProof);
  if (reload) {
    await openOwner(owner);
    const refresh = await verifyStage("refresh");
    for (let index = 0; index < page.items.length; index += 1) assert.deepEqual(refresh.facts[index], facts[index]);
    turnNavigationProof.refreshAnchorJumps = refresh.turnAnchorJumpCount;
    assert.deepEqual(await assertInternalHistory(owner, page, "refresh"), internal);
  }
  return { turns: facts, internal: page.internal_executions,
    turnNavigation: { live: turnNavigationProof.liveAnchorJumps, refresh: turnNavigationProof.refreshAnchorJumps } };
}

async function waitForPlannedExecutions(owner, calls, expectedExecutionOutcome = "completed", failedAdmissionIdempotencyKey = null) {
  assert.ok(["completed", "failed"].includes(expectedExecutionOutcome), "必须明确指定 execution 的预期终态");
  const expectedOutcomes = expectedExecutionOutcome === "completed" ? ["completed", "completed_empty"] : ["failed"];
  let rows;
  await waitUntil(async () => {
    rows = await readRollout(owner, `SELECT m.model_call_id,e.execution_id,e.outcome FROM model_calls m JOIN executions e ON e.execution_id=m.execution_id WHERE m.model_call_id IN (${calls.map(() => "?").join(",")})`, calls);
    if (failedAdmissionIdempotencyKey !== null) {
      const admissions = await readRollout(owner,
        "SELECT execution_id,job_id,outcome FROM executions WHERE session_id=? AND thread_id=? AND admission_idempotency_key=?",
        [owner.sessionId, owner.threadId, failedAdmissionIdempotencyKey]);
      const failedAdmission = admissions.find((execution) => execution.outcome === "failed");
      if (failedAdmission) {
        const job = (await ownerApi(owner, `/api/v1/jobs/${failedAdmission.job_id}`)).data;
        assert.equal(job?.job_id, failedAdmission.job_id);
        assert.equal(job?.session_id, owner.sessionId);
        assert.equal(job?.thread_id, owner.threadId);
        assert.ok(typeof job?.error_message === "string" && job.error_message.length > 0,
          `精确 report execution 已失败，但对应 Job 未提供 error_message: ${JSON.stringify(job)}`);
        throw new Error(`精确 report execution ${failedAdmission.execution_id} failed: ${job.error_message}`);
      }
    }
    for (const row of rows) {
      if (["completed", "completed_empty", "failed", "interrupted", "cancelled", "execution_lost"].includes(row.outcome)) {
        assert.ok(expectedOutcomes.includes(row.outcome), `execution 未满足 ${expectedExecutionOutcome} 合同: ${JSON.stringify(row)}`);
      }
    }
    return rows.length === calls.length && rows.every((row) => expectedOutcomes.includes(row.outcome));
  }, `已计划的精确 owner ModelCall 全部提交 ${expectedExecutionOutcome} execution 终态`, 120_000);
  assert.deepEqual(rows.map((row) => row.model_call_id).sort(), [...calls].sort());
  return rows;
}

async function completeOwnerTurn(owner, prompt, responses, attachments = [], {
  expectedJobStatus = "completed",
  expectedExecutionOutcome = "completed",
  expectedTurnStatus = "completed",
  historyTiming = null,
  verifyHistory = true,
  openBeforeSend = true,
} = {}) {
  const calls = await planChatCalls(owner.sessionId, owner.threadId, responses);
  if (openBeforeSend) await openOwner(owner);
  if (attachments.length) await owner.page.locator(".composer-file-input").setInputFiles(attachments);
  const sent = await sendOwnerPrompt(owner, prompt);
  const job = await waitForJobStatus(owner, sent.jobId, expectedJobStatus);
  const executions = await waitForPlannedExecutions(owner, calls, expectedExecutionOutcome);
  const turn = await assertAcceptedTurn(owner, sent);
  assert.equal(turn.status, expectedTurnStatus);
  const evidence = verifyHistory ? await verifyOwnerTurns(owner, { historyTiming }) : null;
  return { calls, sent, job, executions, turn, evidence };
}

async function readCatalog(surface, query, parameters = []) {
  return testControl({ action: "read_sql", name: surface.name,
    relative_path: ".boxteam/navigation/session-catalog.sqlite", query, parameters });
}

async function sessionCatalogNode(owner) {
  const rows = await readCatalog(owner,
    "SELECT node_id,kind,state,workspace_id,parent_node_id,revision,created_at,storage_relative_locator,main_thread_id FROM nodes WHERE node_id=?", [owner.sessionId]);
  assert.equal(rows.length, 1, "Session 定位只能来自唯一权威 catalog 节点");
  const node = rows[0];
  assert.equal(node.kind, "session");
  assert.equal(node.state, "active");
  assert.equal(node.workspace_id, owner.workspaceIdentity);
  canonicalUuid7(node.node_id, "ses");
  canonicalUuid7(node.main_thread_id, "thr");
  const createdDate = new Date(node.created_at).toISOString().slice(0, 10);
  const embeddedDate = new Date(Number.parseInt(node.node_id.slice(4, 16), 16)).toISOString().slice(0, 10);
  assert.equal(createdDate, embeddedDate);
  assert.equal(node.storage_relative_locator, `sessions/${createdDate.replaceAll("-", "/")}/${owner.sessionId}`);
  const exported = (await ownerApi(owner, "/api/v1/session-catalog/export")).data;
  const publicNodes = exported.items.filter((item) => item.node_id === owner.sessionId);
  assert.equal(publicNodes.length, 1, "公开目录必须投影同一个权威 Session 节点");
  const publicNode = publicNodes[0];
  assert.equal(publicNode.kind, node.kind);
  assert.equal(publicNode.state, node.state);
  assert.equal(publicNode.parent_node_id, node.parent_node_id);
  assert.equal(publicNode.node_revision, node.revision);
  assert.equal(Object.hasOwn(publicNode, "storage_relative_path"), false);
  assert.equal(JSON.stringify(publicNode).includes(node.storage_relative_locator), false);
  assert.equal(publicNode.session.session_id, node.node_id);
  assert.equal(publicNode.session.workspace_id, node.workspace_id);
  assert.equal(publicNode.session.thread_id, node.main_thread_id);
  assert.equal(Date.parse(publicNode.created_at), Date.parse(node.created_at));
  const main = (await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/main-thread`)).data;
  assert.deepEqual(main, { session_id: owner.sessionId, thread_id: node.main_thread_id });
  return node;
}

function sessionLocator(node) {
  return `.boxteam/${node.storage_relative_locator}`;
}

async function readControl(owner, query, parameters = []) {
  const node = await sessionCatalogNode(owner);
  return readFrozenControl(owner, node, query, parameters);
}

async function readFrozenControl(owner, node, query, parameters = []) {
  assert.equal(node.node_id, owner.sessionId);
  assert.equal(node.workspace_id, owner.workspaceIdentity);
  return testControl({ action: "read_sql", name: owner.name, relative_path: `${sessionLocator(node)}/session-control.sqlite`, query, parameters });
}

async function threadRolloutLocator(owner, node, sessionPath = sessionLocator(node)) {
  const threads = await testControl({ action: "read_sql", name: owner.name,
    relative_path: `${sessionPath}/session-control.sqlite`,
    query: "SELECT tc.thread_id,tc.kind,tc.created_at,(SELECT COUNT(*) FROM thread_catalog WHERE kind='main') AS main_count,(SELECT thread_id FROM thread_catalog WHERE kind='main') AS main_thread_id,tcr.state AS creation_state,tcr.final_relative_locator,tcr.child_created_at FROM thread_catalog tc LEFT JOIN thread_creation_records tcr ON tcr.child_thread_id=tc.thread_id WHERE tc.thread_id=?",
    parameters: [owner.threadId] });
  assert.equal(threads.length, 1, "thread 归属必须来自 owner 的唯一权威 thread catalog");
  const thread = threads[0];
  assert.equal(thread.main_count, 1, "Session 本地 thread catalog 必须只有一个 main row");
  assert.equal(thread.main_thread_id, node.main_thread_id);
  if (thread.kind === "main") {
    assert.equal(owner.child, false);
    assert.equal(thread.thread_id, node.main_thread_id, "main rollout 必须属于 Session 权威 main pointer");
    return `${sessionPath}/threads/${thread.thread_id}/rollout/index.sqlite`;
  }
  assert.equal(thread.kind, "child");
  assert.equal(owner.child, true);
  assert.notEqual(thread.thread_id, node.main_thread_id);
  assert.equal(thread.creation_state, "published", "child 的物理路径只能来自已发布 creation record");
  assert.equal(thread.created_at, thread.child_created_at);
  const createdDate = new Date(thread.child_created_at).toISOString().slice(0, 10);
  const embeddedDate = new Date(Number.parseInt(owner.threadId.slice(4, 16), 16)).toISOString().slice(0, 10);
  assert.equal(createdDate, embeddedDate);
  const locator = thread.final_relative_locator;
  assert.equal(locator, `threads/${createdDate.replaceAll("-", "/")}/${owner.threadId}`);
  return `${sessionPath}/${locator}/rollout/index.sqlite`;
}

async function readRollout(owner, query, parameters = []) {
  const node = await sessionCatalogNode(owner);
  return readFrozenRollout(owner, node, query, parameters);
}

async function readFrozenRollout(owner, node, query, parameters = []) {
  return testControl({ action: "read_sql", name: owner.name, relative_path: await threadRolloutLocator(owner, node), query, parameters });
}

async function readRolloutDirectory(owner) {
  const node = await sessionCatalogNode(owner);
  const index = await threadRolloutLocator(owner, node);
  return testControl({ action: "read_directory", name: owner.name,
    relative_path: index.slice(0, -"/index.sqlite".length) });
}

async function waitForIdleChildRuntime(owner) {
  let runtime;
  await waitUntil(async () => {
    const { items } = (await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/child-threads`)).data;
    const child = items.find((entry) => entry.thread_id === owner.threadId);
    assert.ok(child, "idle 前置必须观察真实 child owner");
    runtime = child.runtime;
    return runtime.residency === "resident" && runtime.execution_state === "idle"
      && runtime.blockers.length === 0 && runtime.idle_deadline !== null;
  }, "child lease 与 blocker 释放并建立 idle 锚点");
  return runtime;
}

async function graphBinding(owner) {
  const rows = await readControl(owner, "SELECT graph_id,graph_revision,graph_schema_hash,capability_profile_hash FROM thread_catalog WHERE thread_id=?", [owner.threadId]);
  assert.equal(rows.length, 1);
  assert.equal(typeof rows[0].graph_id, "string");
  assert.ok(rows[0].graph_revision >= 1);
  assert.match(rows[0].graph_schema_hash, /^sha256:[0-9a-f]{64}$/);
  assert.match(rows[0].capability_profile_hash, /^sha256:[0-9a-f]{64}$/);
  return rows[0];
}

const GRAPH_PHASES = new Set(["agent.graph.compiled", "thread.runtime.built", "thread.runtime.acquired", "thread.runtime.closed"]);

async function graphLifecycle(owner) {
  const events = (await testControl({ action: "events" })).filter((event) => GRAPH_PHASES.has(event.phase)
    && event.details?.owns_thread !== false && event.details?.session_id === owner.sessionId
    && event.details?.thread_id === owner.threadId);
  const phase = (name) => events.filter((event) => event.phase === name);
  const compiled = phase("agent.graph.compiled");
  const built = phase("thread.runtime.built");
  const acquired = phase("thread.runtime.acquired");
  const closed = phase("thread.runtime.closed");
  assert.equal(compiled.length, built.length, "每次真实建图只允许编译一个 compiled Agent graph");
  for (const [index, build] of built.entries()) {
    const compile = compiled[index];
    assert.match(compile.details.compiled_graph_type, /\.CompiledStateGraph$/, "必须观察真实 compiled graph 对象");
    assert.equal(compile.pid, build.pid);
    assert.ok(compile.at <= build.at);
    assert.equal(compile.details.agent_object_id, build.details.agent_object_id);
    assert.equal(compile.details.scope_object_id, build.details.scope_object_id);
    assert.deepEqual(build.details.graph_binding, await graphBinding(owner));
    const nextBuild = built[index + 1];
    const uses = acquired.filter((event) => event.at >= build.at && (!nextBuild || event.at < nextBuild.at));
    assert.ok(uses.length > 0, "已物化的 graph 必须来自实际可执行 admission");
    for (const use of uses) {
      assert.equal(use.pid, build.pid);
      assert.equal(use.details.runtime_object_id, build.details.runtime_object_id);
      assert.equal(use.details.agent_object_id, build.details.agent_object_id);
      assert.equal(use.details.generation, uses[0].details.generation);
      assert.equal(use.operation_id, use.details.execution_id);
    }
    const releases = closed.filter((event) => event.pid === build.pid
      && event.details.generation === uses[0].details.generation);
    assert.ok(releases.length <= 1, "同一 generation 只能实际释放一次");
    for (const release of releases) {
      assert.equal(release.details.runtime_object_id, build.details.runtime_object_id);
      assert.equal(release.details.scope_object_id, build.details.scope_object_id);
      assert.equal(release.details.scope_state, "closed");
      assert.ok(release.at >= uses.at(-1).at);
      if (nextBuild) assert.ok(release.at < nextBuild.at, "旧 scope 未实际关闭前不得重建");
    }
  }
  return { compiled, built, acquired, closed };
}

function graphAdmission(lifecycle, jobId) {
  const matches = lifecycle.acquired.filter((event) => event.details.job_id === jobId);
  assert.equal(matches.length, 1, "每个执行 Job 必须观察到一次实际 runtime lease");
  return matches[0];
}

async function modelRequest(callId) {
  return JSON.parse(await readFile(`${requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT")}/model-stream/${callId}.json`, "utf8"));
}

async function assertToolResultContinuation(owner, calls, providerCallId, result) {
  const rows = await readRollout(owner,
    "SELECT model_call_id,outcome,dispatch_state FROM model_calls WHERE session_id=? AND thread_id=? AND execution_id=(SELECT execution_id FROM model_calls WHERE model_call_id=?) ORDER BY attempt",
    [owner.sessionId, owner.threadId, calls[0]]);
  assert.deepEqual(rows.map((row) => [row.model_call_id, row.outcome, row.dispatch_state]),
    calls.map((call) => [call, "completed", "completed"]), "普通工具拒绝只消费原调用和已规划后继，不重发原 ModelCall");
  const request = await modelRequest(calls.at(-1));
  const messages = request.request.messages.filter((message) => message.role === "tool" && message.tool_call_id === providerCallId);
  assert.deepEqual(messages.map((message) => message.content), [result.result], "后继 Provider 只消费一次原拒绝正文");
  return { modelCalls: rows, toolMessages: messages };
}

async function prepareNavigationIntent(surface, fields) {
  const snapshot = (await ownerApi(surface, "/api/v1/session-catalog/snapshot")).data;
  const operationId = `op_${Bun.randomUUIDv7().replaceAll("-", "")}`;
  const intent = { client_operation_id: operationId, client_sequence: 1,
    base_catalog_revision: snapshot.catalog_revision, ...fields };
  return { operationId, intent };
}

async function enqueueNavigationIntent(surface, operation) {
  const accepted = await ownerApi(surface, "/api/v1/session-catalog/operations:enqueue", {
    method: "POST", headers: { "Content-Type": "application/json", "X-BoxTeam-Workspace-Id": surface.gatewayWorkspaceId }, body: JSON.stringify({ intents: [operation.intent] }),
  });
  assert.equal(accepted.data.accepted_count, 1);
  return { ...operation, accepted: accepted.data };
}

async function navigationIntent(surface, fields) {
  return enqueueNavigationIntent(surface, await prepareNavigationIntent(surface, fields));
}

async function navigationTerminal(surface, operation, expected = "committed") {
  let receipt;
  let terminalRequestId;
  await waitUntil(async () => {
    const value = await ownerApi(surface, `/api/v1/session-catalog/operations?operation_id=${operation.operationId}`);
    terminalRequestId = value.request_id;
    receipt = value.data.items.find((item) => item.operation_id === operation.operationId);
    assert.notEqual(receipt?.settlement, "repair_required", `删除排空需修复: ${receipt?.error_detail}`);
    return ["committed", "rejected", "cancelled", "dependency_failed"].includes(receipt?.state)
      && (receipt.settlement === null || receipt.settlement === "settled");
  }, `导航 operation ${operation.operationId} 完整终态`, 120_000);
  assert.equal(receipt.state, expected, JSON.stringify(receipt));
  if (expected === "committed" && ["delete_session", "delete_folder"].includes(operation.intent.kind)) {
    const journals = await readCatalog(surface,
      "SELECT subtree_delete_idempotency_key,root_node_id,state,frozen_node_ids,frozen_session_locators FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [operation.operationId]);
    assert.equal(journals.length, 1);
    const journal = journals[0];
    assert.equal(journal.state, "completed");
    const nodeIds = JSON.parse(journal.frozen_node_ids).map((node) => {
      assert.deepEqual(Object.keys(node).sort(), ["node_id", "revision"]);
      assert.equal(typeof node.node_id, "string");
      assert.ok(Number.isInteger(node.revision) && node.revision >= 1);
      return node.node_id;
    });
    assert.ok(nodeIds.length > 0);
    const sessionIds = JSON.parse(journal.frozen_session_locators).map((node) => node.session_id);
    const remaining = await readCatalog(surface,
      `SELECT node_id FROM nodes WHERE node_id IN (${nodeIds.map(() => "?").join(",")})`, nodeIds);
    assert.deepEqual(remaining, []);
    await waitUntil(async () => {
      const counts = await Promise.all(nodeIds.map((nodeId) => catalogRow(surface, nodeId).count()));
      const chats = await Promise.all(sessionIds.map((sessionId) =>
        surface.page.locator(`.chat-stream-virtual-shell[data-owner-session-id="${sessionId}"]`).count()));
      return [...counts, ...chats].every((count) => count === 0);
    }, "原删除 journal 与目录 DOM 同时收敛");
    lifecycleDeletionProofs.push({ operationId: operation.operationId, terminalRequestId,
      origin: new URL(surface.page.url()).origin, workspaceHeader: surface.gatewayWorkspaceId,
      journal, nodeIds, sessionIds, remaining,
      absentDomNodeIds: nodeIds, absentChatSessionIds: sessionIds, verifiedAt: Date.now() });
  }
  return receipt;
}

async function deleteOwner(owner, expected = "committed") {
  return navigationTerminal(owner, await navigationIntent(owner, { kind: "delete_session", target_node_id: owner.sessionId }), expected);
}

async function readDeletedControl(owner, deletion, query, parameters = []) {
  const records = await readCatalog(owner, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]);
  assert.equal(records.length, 1, "隔离 trace 必须定点绑定原删除 journal");
  const record = records[0];
  assert.ok(JSON.parse(record.frozen_session_locators).some((node) => node.session_id === owner.sessionId));
  return testControl({ action: "read_sql", name: owner.name,
    relative_path: `.boxteam/sessions/.deleting/${record.subtree_delete_idempotency_key}/${owner.sessionId}/session-control.sqlite`, query, parameters });
}

async function runObservationDeletion(surfaces) {
  const facts = [];
  for (const operation of ["read", "wait"]) {
    for (const observationFirst of [true, false]) {
      const label = `delete-${operation}-${observationFirst}`;
      const source = await createProductSession(surfaces.b, `${label} source`);
      const target = await createProductSession(surfaces.c, `${label} target`);
      const targetNode = await sessionCatalogNode(target);
      if (operation === "read") await completeOwnerTurn(source, "载入真实上下文读取能力。", [
        toolResponse(`${label}-load`, "skill_load", { name: "gateway-context" }),
        finalResponse(`${label}-loaded`, "上下文读取能力已载入。"),
      ]);
      const held = [];
      const arm = async (barrier) => { await testControl({ action: "arm", ...barrier }); held.push(barrier); return barrier; };
      try {
        let targetJob;
        if (operation === "wait") {
          const targetCalls = await planChatCalls(target.sessionId, target.threadId, [
            toolResponse(`${label}-target-tool`, "read_file", { path: "README.md" }),
            finalResponse(`${label}-target-final`, "目标任务完成。"),
          ]);
          const targetBarrier = await arm({ operation_id: targetCalls[0], phase: "model.response_ready" });
          await openOwner(target);
          targetJob = await sendOwnerPrompt(target, "保持真实未终态任务，供最大 300 秒 wait 建立 baseline。");
          await testControl({ action: "acknowledged", ...targetBarrier });
        }
        const query = { ...target.resource, view: "messages", recent_rounds: 50, limit: 10, max_chars: 16384 };
        const calls = await planChatCalls(source.sessionId, source.threadId, [
          operation === "read"
            ? toolResponse(label, "invoke_extension_tool", { tool_name: "read_context", arguments: query })
            : toolResponse(label, "wait_for_session", { target_session_id: target.sessionId, until: "terminal", timeout_seconds: 300 }),
          ...(operation === "read"
            ? [finalResponse(`${label}-done`, "目标寻址已拒绝，没有建立新观察。")] : []),
        ]);
        const sourceCallId = softwareOperation(operation, source, calls[0], label);
        const deletion = await prepareNavigationIntent(target, { kind: "delete_session", target_node_id: target.sessionId });
        const deleting = await arm({ operation_id: deletion.operationId, phase: "deletion.catalog_deleting" });
        const barrier = observationFirst
          ? await arm({ operation_id: sourceCallId, phase: operation === "wait" ? "observation.target.baseline_frozen" : "observation.target.admitted" })
          : deleting;
        const canonical = observationFirst
          ? await arm({ operation_id: `${calls[0]}:tool-call:${label}`, phase: "canonical.tool_result.before_commit" }) : null;
        const clockBefore = await testControl({ action: "clock" });
        if (!observationFirst) {
          await enqueueNavigationIntent(target, deletion);
          await testControl({ action: "acknowledged", ...barrier });
        }
        await openOwner(source);
        const sent = await sendOwnerPrompt(source, `${operation} ${target.resource.uri}，删除后不得建立新观察。`);
        let original;
        if (observationFirst) {
          await testControl({ action: "acknowledged", ...barrier });
          original = await observationPage(target, sourceCallId, "target");
          const envelope = observationEnvelope(original);
          assert.equal(envelope.terminal_replay, false);
          const leases = await readFrozenControl(target, targetNode,
            "SELECT * FROM session_operation_leases WHERE operation_kind='remote_observation' AND operation_identity IN (?,?) AND state NOT IN ('completed','cancelled','failed')",
            [`target-observation|${original.observation_id}`, `target-page|${original.page_identity}`]);
          assert.equal(leases.length, 2, "原观察和原页各自捕获唯一 lease");
          assert.ok(leases.every((lease) => lease.captured_lifecycle_generation === 1 && lease.recovery_ref === lease.operation_identity));
          if (operation === "wait") {
            assert.ok(original.snapshot_json.includes(targetJob.jobId), "300 秒 wait 必须冻结真实 active Job");
            const deadline = envelope.query.deadline;
            assert.equal(deadline.timeout_seconds, 300);
            assert.equal(Date.parse(deadline.deadline_at_utc) - Date.parse(deadline.admitted_at_utc), 300_000);
            assert.equal(deadline.monotonic_origin_id, clockBefore.origin);
            assert.equal(deadline.monotonic_deadline_ns - clockBefore.wait_ns, 300_000_000_000);
          } else assert.equal(original.snapshot_json, null, "read 准入屏障在 snapshot 创建之前");
          await enqueueNavigationIntent(target, deletion);
          await testControl({ action: "acknowledged", ...deleting });
        }
        const pending = await readCatalog(target, "SELECT state FROM nodes WHERE node_id=?", [target.sessionId]);
        assert.deepEqual(pending, [{ state: "deleting" }], "必须由 catalog 的实际 gate 决定删除顺序");
        await testControl({ action: "release", ...deleting });
        const terminal = await navigationTerminal(target, deletion);
        if (observationFirst) await testControl({ action: "release", ...barrier });
        const inaccessible = await ownerApiResponse(target, `/api/v1/sessions/${target.sessionId}/main-thread`);
        assert.equal(inaccessible.status, 404);
        assert.equal(inaccessible.body.detail, `Session ${target.sessionId} not found`);
        await navigationTerminal(target, deletion);
        if (observationFirst) {
          await testControl({ action: "acknowledged", ...canonical });
          const saved = await observationPage(source, sourceCallId, "source");
          assert.equal(observationEnvelope(saved).terminal_replay, false, "本次授权模式不能改写原业务 envelope");
          const targetRows = await readDeletedControl(target, deletion,
            "SELECT p.*,o.snapshot_json,o.expires_at FROM remote_observation_pages p JOIN remote_observations o USING(observation_id,side) WHERE p.page_identity=? AND p.side='target'", [original.page_identity]);
          assert.equal(targetRows.length, 1);
          assert.deepEqual(targetRows[0].request_json, original.request_json);
          assert.deepEqual(targetRows[0].snapshot_json, original.snapshot_json);
          assert.deepEqual(JSON.parse(targetRows[0].result_json).error,
            { code: "target-deleted", message: "target-deleted", detail: {} });
          const proof = await executionProof(source, sent.jobId);
          const reopen = await replaceNodePage(surfaces.b, [source]);
          const killed = await testControl({ action: "process", name: "b", operation: "kill" });
          const restarted = await testControl({ action: "process", name: "b", operation: "start" });
          await reopen();
          await testControl({ action: "release", ...canonical });
          const expectedStatus = operation === "read" ? "completed" : "failed";
          const recovered = await assertRecoveredExecution(source, sent, proof, calls,
            { expectedJobStatus: expectedStatus, expectedExecutionOutcome: expectedStatus, expectedTurnStatus: expectedStatus });
          const results = recovered.turn.response_parts.filter((part) => part.kind === "tool_result");
          assert.equal(results.length, 1, "同 source call 的新授权重放只提交一份 ToolResult");
          const result = assertBoundToolResult(source, sent, recovered.turn, calls[0], label,
            operation === "read" ? "invoke_extension_tool" : "wait_for_session");
          assert.equal(result.result, "target-deleted");
          if (operation === "read") {
            assert.deepEqual(recovered.turn.response_parts.filter((part) => part.kind === "final_text").map((part) => part.text),
              ["目标寻址已拒绝，没有建立新观察。"]);
            await assertToolResultContinuation(source, calls, label, result);
          } else {
            assert.equal(recovered.job.error_message, "target-deleted");
            assert.equal(recovered.turn.response_parts.filter((part) => part.kind === "final_text").length, 0);
          }
          assert.deepEqual(await observationPage(source, sourceCallId, "source"), saved);
          assert.deepEqual(await readDeletedControl(target, deletion,
            "SELECT p.*,o.snapshot_json,o.expires_at FROM remote_observation_pages p JOIN remote_observations o USING(observation_id,side) WHERE p.page_identity=? AND p.side='target'", [original.page_identity]), targetRows);
          facts.push({ operation, observationFirst, sourceCallId, deletion, original, saved, targetRows, proof, killed, restarted, recovered, terminal });
        } else {
          const expectedJobStatus = operation === "read" ? "completed" : "failed";
          const expectedExecutionOutcome = operation === "read" ? "completed" : "failed";
          const expectedTurnStatus = operation === "read" ? "completed" : "failed";
          const job = await waitForJobStatus(source, sent.jobId, expectedJobStatus);
          await waitForPlannedExecutions(source, calls, expectedExecutionOutcome);
          const displayed = await verifyOwnerTurns(source);
          const turn = (await loadThreadHistory(source, { turn_ids: [sent.turnId] })).items[0];
          assert.equal(turn.response_parts.filter((part) => part.kind === "tool_result").length, 1);
          const result = assertBoundToolResult(source, sent, turn, calls[0], label,
            operation === "read" ? "invoke_extension_tool" : "wait_for_session");
          const discoveryRejection = assertDiscoveryRejection(result, target.sessionId);
          assert.equal(turn.status, expectedTurnStatus);
          let continuation;
          if (operation === "read") {
            assert.deepEqual(turn.response_parts.filter((part) => part.kind === "final_text").map((part) => part.text),
              ["目标寻址已拒绝，没有建立新观察。"]);
            continuation = await assertToolResultContinuation(source, calls, label, result);
          } else {
            assert.equal(job.error_message, result.result, "builtin wait 沿原访问异常结束，不能由后继 Provider 错误代替");
            assert.equal(turn.response_parts.filter((part) => part.kind === "final_text").length, 0);
            const emitted = await readRollout(source,
              "SELECT model_call_id FROM model_calls WHERE execution_id=(SELECT execution_id FROM model_calls WHERE model_call_id=?) ORDER BY attempt", [calls[0]]);
            assert.deepEqual(emitted.map((row) => row.model_call_id), calls, "终止的 builtin wait 不得重发或发起后继调用");
          }
          const rows = await readDeletedControl(target, deletion,
            "SELECT (SELECT COUNT(*) FROM remote_observations) AS observations,(SELECT COUNT(*) FROM remote_observation_pages) AS pages,(SELECT COUNT(*) FROM communication_inbox) AS inbox");
          assert.deepEqual(rows, [{ observations: 0, pages: 0, inbox: 0 }]);
          facts.push({ operation, observationFirst, sourceCallId, deletion, terminal, job, turn, displayed, rows, discoveryRejection, continuation });
        }
        const clockAfter = await testControl({ action: "clock" });
        assert.equal(clockAfter.wait_ns, clockBefore.wait_ns, "删除取消真实 wait，不用推进 clock 冒充超时");
        assert.equal(clockAfter.wait_utc, clockBefore.wait_utc);
      } finally {
        for (const barrier of held) await testControl({ action: "release", ...barrier });
      }
    }
  }
  return facts;
}

async function runLifecycleAdmissionDeletion(surfaces) {
  const facts = { child: [], control: [], team: [], inbox: [] };
  const held = [];
  const matches = [];
  const arm = async (operation_id, phase) => {
    const barrier = { operation_id, phase };
    await testControl({ action: "arm", ...barrier });
    held.push(barrier);
    return barrier;
  };
  const armMatch = async (owner, phase, details) => {
    const barrier = { barrier_id: `I-${Bun.randomUUIDv7()}`, phase,
      details: { session_id: owner.sessionId, ...details } };
    await testControl({ action: "arm_match", ...barrier });
    matches.push(barrier);
    return barrier;
  };
  const ack = (barrier) => testControl({ action: barrier.barrier_id ? "acknowledged_match" : "acknowledged", ...barrier });
  const release = (barrier) => testControl({ action: barrier.barrier_id ? "release_match" : "release", ...barrier });
  const prepareDelete = async (owner) => {
    const operation = await prepareNavigationIntent(owner, { kind: "delete_session", target_node_id: owner.sessionId });
    const barrier = await arm(operation.operationId, "deletion.catalog_deleting");
    return { operation, barrier };
  };
  const pendingCatalog = async (owner) => {
    assert.deepEqual(await readCatalog(owner, "SELECT state FROM nodes WHERE node_id=?", [owner.sessionId]), [{ state: "deleting" }]);
  };
  const activeLease = (rows, kind) => {
    assert.equal(rows.length, 1, `${kind} 必须持久捕获一个原 owner lease`);
    const lease = rows[0];
    assert.equal(lease.operation_kind, kind);
    assert.equal(lease.captured_lifecycle_generation, 1);
    assert.ok(["active", "settling"].includes(lease.state));
    assert.ok(lease.preimage_hash && lease.fencing_token >= 1);
    return lease;
  };
  const deletedLease = async (owner, deletion, lease) => {
    const rows = await readDeletedControl(owner, deletion, "SELECT * FROM session_operation_leases WHERE lease_id=?", [lease.lease_id]);
    assert.equal(rows.length, 1);
    for (const key of ["operation_identity", "preimage_hash", "recovery_ref", "captured_lifecycle_generation"]) assert.equal(rows[0][key], lease[key]);
    assert.ok(["completed", "cancelled", "failed"].includes(rows[0].state), "删除必须等原 lease 终结后才物理隔离");
    return rows[0];
  };
  const controlCounts = (owner, node) => readFrozenControl(owner, node,
    "SELECT (SELECT COUNT(*) FROM thread_catalog) AS threads,(SELECT COUNT(*) FROM thread_creation_records) AS creations,(SELECT COUNT(*) FROM communication_inbox) AS inbox,(SELECT COUNT(*) FROM thread_execution_intents) AS intents,(SELECT COUNT(*) FROM collaboration_fanouts) AS fanouts");
  const rolloutCounts = async (owner, node) => {
    const index = await threadRolloutLocator(owner, node);
    const directory = await testControl({ action: "read_directory", name: owner.name, relative_path: index.slice(0, -"/index.sqlite".length) });
    if (directory === null) return null;
    return readFrozenRollout(owner, node,
      "SELECT (SELECT COUNT(*) FROM executions) AS executions,(SELECT COUNT(*) FROM model_calls) AS calls,(SELECT COUNT(*) FROM context_assemblies) AS assemblies,(SELECT COUNT(*) FROM turn_records) AS roots");
  };
  const deletedRolloutCounts = async (owner, deletion, node) => {
    const rows = await readCatalog(owner, "SELECT subtree_delete_idempotency_key,frozen_session_locators FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]);
    assert.equal(rows.length, 1);
    const frozen = JSON.parse(rows[0].frozen_session_locators).find((entry) => entry.session_id === owner.sessionId);
    assert.ok(frozen?.storage_relative_locator);
    assert.equal(frozen.storage_relative_locator, node.storage_relative_locator);
    const isolatedSession = `.boxteam/sessions/.deleting/${rows[0].subtree_delete_idempotency_key}/${owner.sessionId}`;
    const index = await threadRolloutLocator(owner, node, isolatedSession);
    const directory = index.slice(0, -"/index.sqlite".length);
    if (await testControl({ action: "read_directory", name: owner.name, relative_path: directory }) === null) return null;
    return testControl({ action: "read_sql", name: owner.name,
      relative_path: index,
      query: "SELECT (SELECT COUNT(*) FROM executions) AS executions,(SELECT COUNT(*) FROM model_calls) AS calls,(SELECT COUNT(*) FROM context_assemblies) AS assemblies,(SELECT COUNT(*) FROM turn_records) AS roots" });
  };
  const deniedHistory = async (owner) => {
    const response = await ownerApiResponse(owner, `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction: "tail", turns: 1, include: HISTORY_INCLUDE }),
    });
    assert.equal(response.status, 409);
    assertSessionDeletionPending(response.body, owner.sessionId);
    return response.body;
  };
  try {
    for (const stage of ["prepared", "published", "before_admission"]) {
      const owner = await createProductSession(surfaces.a, `I child ${stage}`);
      const node = await sessionCatalogNode(owner);
      const deletion = await prepareDelete(owner);
      const threadId = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
      const parentJob = `job_${await testControl({ action: "next_uuid", namespace: "job" })}`;
      const label = `I-child-${stage}`;
      const calls = await planChatCalls(owner.sessionId, owner.threadId, [
        toolResponse(label, "task", { description: "只读取 README.md，不修改文件。", subagent_type: "general-purpose" }),
        finalResponse(`${label}-done`, "持久 child 创建请求已结束。"), finalResponse(`${label}-report`, "child 报告已处理。"),
      ]);
      await planChatCalls(owner.sessionId, threadId, [finalResponse(`${label}-child`, "原 child 初始化完成。")]);
      // delegation 使用 ToolNode 的 Provider 原始调用 ID，不使用 canonical carrier 的 scoped ID。
      const key = `del_${createHash("sha256").update(`session-subagent-delegation\0${owner.sessionId}\0${parentJob}\0${label}\0general-purpose`).digest("hex").slice(0, 32)}`;
      const creation = await arm(key, `thread.creation.${stage}`);
      await openOwner(owner);
      const sent = await acceptOwnerPrompt(owner, `创建 child 并与删除竞争：${stage}。`);
      assert.equal(sent.jobId, parentJob);
      await ack(creation);
      const before = await controlCounts(owner, node);
      const records = await readFrozenControl(owner, node, "SELECT * FROM thread_creation_records WHERE thread_creation_idempotency_key=?", [key]);
      let lease;
      if (stage !== "before_admission") {
        assert.equal(records.length, 1);
        assert.equal(records[0].child_thread_id, threadId);
        assert.equal(records[0].owner_session_lifecycle_generation, 1);
        assert.equal(records[0].state, stage === "published" ? "published" : "preparing");
        // ThreadCreationRecord 自身就是本创建的恢复 lease，不凭空要求另一套通用记录。
        lease = records[0];
      } else assert.deepEqual(records, []);
      const enqueuing = enqueueNavigationIntent(owner, deletion.operation);
      if (stage === "published") {
        // publish 的真实 topology shared 仍被屏障持有；先释放才能进入删除唯一提交点。
        assert.deepEqual(await readCatalog(owner, "SELECT state FROM nodes WHERE node_id=?", [owner.sessionId]), [{ state: "active" }]);
        await release(creation);
      }
      await enqueuing;
      await ack(deletion.barrier);
      await pendingCatalog(owner);
      const denied = await deniedHistory(owner);
      if (stage !== "published") await release(creation);
      await release(deletion.barrier);
      const terminal = await navigationTerminal(owner, deletion.operation);
      const after = await readDeletedControl(owner, deletion.operation, "SELECT * FROM thread_creation_records WHERE thread_creation_idempotency_key=?", [key]);
      if (stage === "before_admission") {
        assert.deepEqual(after, [], "删除先行不能创建 thread record、初始 intent 或 lease");
        const counts = await readDeletedControl(owner, deletion.operation, "SELECT (SELECT COUNT(*) FROM thread_catalog) AS threads,(SELECT COUNT(*) FROM thread_execution_intents) AS intents");
        assert.deepEqual(counts, [{ threads: before[0].threads, intents: before[0].intents }]);
      } else {
        assert.equal(after.length, 1);
        assert.equal(after[0].child_thread_id, threadId);
        assert.equal(after[0].preimage_hash, records[0].preimage_hash);
        assert.equal(after[0].state, stage === "published" ? "published" : "aborted");
      }
      const leaseAfter = lease ? after[0] : null;
      facts.child.push({ stage, key, sent, before, records, after, lease, leaseAfter, denied, terminal });
    }

    for (const kind of ["skill_load", "tool_set"]) {
      for (const operationFirst of [true, false]) {
        const owner = await createProductSession(surfaces.a, `I ${kind} ${operationFirst}`);
        const node = await sessionCatalogNode(owner);
        const deletion = await prepareDelete(owner);
        const drain = operationFirst ? await arm(deletion.operation.operationId, `deletion.drain_waiting/${owner.sessionId}`) : null;
        const label = `I-${kind}-${operationFirst}`;
        const calls = await planChatCalls(owner.sessionId, owner.threadId, kind === "skill_load" ? [
          toolResponse(label, "skill_load", { name: "gateway-context" }), finalResponse(`${label}-done`, "独立 Skill 控制已结束。"),
        ] : [finalResponse(label, "实际 ToolSet 编译已结束。")]);
        const control = await armMatch(owner, operationFirst ? "context.control.admitted" : "context.control.before_admission", { thread_id: owner.threadId, kind });
        await openOwner(owner);
        const sent = await acceptOwnerPrompt(owner, `真实 ${kind} 与 Session 删除竞争。`);
        const admitted = await ack(control);
        const before = await rolloutCounts(owner, node);
        const baseline = await controlCounts(owner, node);
        const leaseRows = await readFrozenControl(owner, node, "SELECT * FROM session_operation_leases WHERE operation_identity=?", [`context-control:${admitted.operation_id}`]);
        const lease = operationFirst ? activeLease(leaseRows, "context_control") : null;
        if (lease) assert.ok(lease.recovery_ref, "独立控制 owner 必须持久保存原恢复引用");
        if (!operationFirst) assert.deepEqual(leaseRows, []);
        await enqueueNavigationIntent(owner, deletion.operation);
        await ack(deletion.barrier);
        await pendingCatalog(owner);
        assert.deepEqual(await readFrozenControl(owner, node, "SELECT state,generation FROM lifecycle_fence WHERE id=1"), [{ state: "active", generation: 1 }]);
        await release(deletion.barrier);
        if (operationFirst) {
          await ack(drain);
          assert.ok(!(await testControl({ action: "events" })).some((event) => event.operation_id === deletion.operation.operationId && event.phase === `deletion.fence_closed/${owner.sessionId}`));
          await release(drain);
        }
        await release(control);
        const terminal = await navigationTerminal(owner, deletion.operation);
        const after = await deletedRolloutCounts(owner, deletion.operation, node);
        const controlAfter = await readDeletedControl(owner, deletion.operation, "SELECT (SELECT COUNT(*) FROM thread_catalog) AS threads,(SELECT COUNT(*) FROM thread_creation_records) AS creations,(SELECT COUNT(*) FROM communication_inbox) AS inbox,(SELECT COUNT(*) FROM thread_execution_intents) AS intents,(SELECT COUNT(*) FROM collaboration_fanouts) AS fanouts");
        assert.deepEqual(controlAfter, baseline, "独立控制失败不能旁生 thread/inbox/intent/fanout");
        const events = (await testControl({ action: "events" })).filter((event) => event.operation_id === admitted.operation_id);
        assert.equal(admitted.phase, control.phase);
        assert.equal(admitted.details.session_id, owner.sessionId);
        assert.equal(admitted.details.thread_id, owner.threadId);
        assert.equal(admitted.details.kind, kind);
        await writeFile(`${requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT")}/lifecycle-control-${kind}-${operationFirst}.json`, JSON.stringify({
          owner: { sessionId: owner.sessionId, threadId: owner.threadId }, barrier: control, admitted, events,
          deletion: deletion.operation, before, after, leaseRows, terminal,
        }, null, 2));
        let leaseAfter;
        if (operationFirst) {
          leaseAfter = await deletedLease(owner, deletion.operation, lease);
          const settled = events.find((event) => event.phase === "context.control.settled");
          assert.ok(settled, "已许可的旧 control 必须能在排空期间结算原 lease");
          const fence = (await testControl({ action: "events" })).find((event) => event.operation_id === deletion.operation.operationId && event.phase === `deletion.fence_closed/${owner.sessionId}`);
          assert.ok(settled.at < fence.at);
        } else {
          assert.deepEqual(after, before, "删除先行不能创建新的 context assembly、ModelCall、Job 或 root");
          assert.ok(!events.some((event) => event.phase === "context.control.admitted"));
          assert.deepEqual(await readDeletedControl(owner, deletion.operation, "SELECT lease_id FROM session_operation_leases WHERE operation_identity=?", [`context-control:${admitted.operation_id}`]), []);
        }
        facts.control.push({ kind, operationFirst, calls, sent, admitted, before, after, lease, leaseAfter, events, terminal });
      }
    }

    for (const boundary of ["mutation", "fanout", "deletion_first"]) {
      const owner = await createProductSession(surfaces.a, `I team ${boundary}`);
      const teamId = `team_${await testControl({ action: "next_uuid", namespace: "team" })}`;
      await completeOwnerTurn(owner, `建立 I 团队 ${boundary}。`, [
        toolResponse(`I-${boundary}-team`, "create_team", { name: `I 团队 ${boundary}` }), finalResponse(`I-${boundary}-created`, "原团队已创建。"),
      ]);
      const childId = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
      await planChatCalls(owner.sessionId, childId, [finalResponse(`I-${boundary}-child`, "真实成员初始化结束。")]);
      await completeOwnerTurn(owner, "创建用于原 fanout 的成员。", [
        toolResponse(`I-${boundary}-member`, "create_team_member", { team_id: teamId, role: "reviewer", startup_prompt: "只确认角色，不修改文件。", work_mode: "read_only" }),
        finalResponse(`I-${boundary}-member-created`, "成员创建已结束。"), finalResponse(`I-${boundary}-report`, "成员初始化报告已处理。"),
      ]);
      await waitUntil(async () => {
        const board = await teamBoard(owner, teamId);
        return ["completed", "completed_empty"].includes(board.children.find((child) => child.thread_id === childId)?.report_status)
          && (await readControl(owner, "SELECT COUNT(*) AS pending FROM collaboration_fanouts WHERE state='pending'"))[0].pending === 0;
      }, "真实团队初始化与既有 fanout 全部结算", 120_000);
      const node = await sessionCatalogNode(owner);
      const revision = (await readFrozenControl(owner, node, "SELECT revision FROM collaboration_ledger WHERE id=1"))[0].revision;
      const deletion = await prepareDelete(owner);
      const label = `I-${boundary}-assign`;
      const calls = await planChatCalls(owner.sessionId, owner.threadId, [
        toolResponse(label, "assign_team_task", { team_id: teamId, assignee_thread_id: childId, title: label, description: "登记原任务，不启动额外 Job。", phase: "review", start_assignee: false }),
        finalResponse(`${label}-done`, "原团队任务调用已结束。"),
      ]);
      const mutation = boundary === "deletion_first" ? await arm(calls[0], "model.response_ready")
        : await armMatch(owner, boundary === "mutation" ? "team.mutation.committed" : `team.fanout.before_recipient/${childId}`, { ledger_revision: revision + 1 });
      await openOwner(owner);
      const sent = await acceptOwnerPrompt(owner, `团队 mutation/fanout 与删除竞争：${boundary}。`);
      const observed = await ack(mutation);
      const baseline = await controlCounts(owner, node);
      const leases = boundary === "deletion_first" ? [] : await readFrozenControl(owner, node, "SELECT l.* FROM collaboration_fanouts f JOIN session_operation_leases l ON l.lease_id=f.lease_id WHERE f.ledger_revision=?", [revision + 1]);
      const lease = boundary === "deletion_first" ? null : activeLease(leases, "collaboration_fanout");
      if (lease) assert.ok(lease.recovery_ref, "fanout owner 必须持久保存原恢复引用");
      const enqueuing = enqueueNavigationIntent(owner, deletion.operation);
      if (boundary === "mutation") await release(mutation);
      await enqueuing;
      await ack(deletion.barrier);
      await pendingCatalog(owner);
      if (boundary !== "mutation") await release(mutation);
      await release(deletion.barrier);
      const terminal = await navigationTerminal(owner, deletion.operation);
      const ledgerAfter = (await readDeletedControl(owner, deletion.operation, "SELECT revision FROM collaboration_ledger WHERE id=1"))[0].revision;
      assert.equal(ledgerAfter, revision + (boundary === "deletion_first" ? 0 : 1));
      const fanouts = await readDeletedControl(owner, deletion.operation, "SELECT f.*,d.recipient_thread_id,d.state AS delivery_state FROM collaboration_fanouts f JOIN collaboration_deliveries d USING(ledger_revision) WHERE f.ledger_revision=?", [revision + 1]);
      if (boundary === "deletion_first") {
        assert.deepEqual(fanouts, []);
        assert.equal((await readDeletedControl(owner, deletion.operation, "SELECT COUNT(*) AS threads FROM thread_catalog"))[0].threads, baseline[0].threads);
      } else {
        assert.ok(fanouts.length > 0 && fanouts.every((row) => row.state !== "pending" && row.delivery_state !== "pending"));
        assert.ok(fanouts.every((row) => row.wakeup_json === null), "start_assignee=false 不能创建新 wakeup 或 Job");
      }
      const leaseAfter = lease ? await deletedLease(owner, deletion.operation, lease) : null;
      facts.team.push({ boundary, revision, sent, observed, lease, leaseAfter, ledgerAfter, fanouts, terminal });
    }

    for (const boundary of ["target.accepted", "inbox.claimed", "inbox.job_created", "deletion_first"]) {
      const source = await createProductSession(surfaces.b, `I send ${boundary}`);
      const target = await createProductSession(surfaces.c, `I target ${boundary}`);
      const node = await sessionCatalogNode(target);
      const deletion = await prepareDelete(target);
      const label = `I-inbox-${boundary}`;
      await planChatCalls(target.sessionId, target.threadId, [finalResponse(`${label}-target`, "原 inbox 只绑定一个 execution。")]);
      const calls = await planChatCalls(source.sessionId, source.threadId, [
        toolResponse(label, "send_message_to_session", { target_session_id: target.sessionId, content: label, kind: "result" }),
        ...(boundary === "deletion_first" ? [] : [finalResponse(`${label}-done`, "原 send 调用已结束。")]),
      ]);
      const operationId = softwareOperation("send", source, calls[0], label);
      const communicationId = `comm_${createHash("sha256").update(`communication\0${operationId}`).digest("hex").slice(0, 32)}`;
      const barrier = boundary === "deletion_first" ? await arm(calls[0], "model.response_ready") : await arm(communicationId, `communication.${boundary}`);
      await openOwner(source);
      const sent = await sendOwnerPrompt(source, `向 ${target.sessionId} 发送真实通信并与目标删除竞争。`);
      await ack(barrier);
      const before = await rolloutCounts(target, node);
      const inbox = await readFrozenControl(target, node, "SELECT * FROM communication_inbox WHERE communication_id=?", [communicationId]);
      const lease = boundary === "deletion_first" ? null : activeLease(await readFrozenControl(target, node, "SELECT * FROM session_operation_leases WHERE operation_identity=?", [`communication-target|${communicationId}`]), "communication_target");
      assert.equal(inbox.length, boundary === "deletion_first" ? 0 : 1);
      if (inbox.length) {
        assert.equal(lease.preimage_hash, inbox[0].payload_hash, "通信 owner 沿原通信身份和持久 inbox 恢复");
        assert.equal(inbox[0].target_thread_id, target.threadId);
        assert.equal(inbox[0].state, "target_accepted");
        assert.equal(inbox[0].job_id, null);
      }
      if (["target.accepted", "inbox.claimed", "deletion_first"].includes(boundary)) assert.equal(before, null, "fresh target 尚未有 execution admission，rollout 必须未物化");
      else assert.equal(before[0].executions, 1);
      await enqueueNavigationIntent(target, deletion.operation);
      await ack(deletion.barrier);
      await pendingCatalog(target);
      await release(barrier);
      await release(deletion.barrier);
      const terminal = await navigationTerminal(target, deletion.operation);
      const expectedJobStatus = boundary === "deletion_first" ? "failed" : "completed";
      const expectedExecutionOutcome = boundary === "deletion_first" ? "failed" : "completed";
      const expectedTurnStatus = boundary === "deletion_first" ? "failed" : "completed";
      const job = await waitForJobStatus(source, sent.jobId, expectedJobStatus);
      await waitForPlannedExecutions(source, calls, expectedExecutionOutcome);
      const history = (await loadThreadHistory(source, { turn_ids: [sent.turnId] })).items[0];
      assert.equal(history.status, expectedTurnStatus);
      const sourceEvidence = await verifyOwnerTurns(source);
      const results = history.response_parts.filter((part) => part.kind === "tool_result");
      assert.equal(results.length, 1, "同一通信调用只能提交一个 ToolResult");
      const after = await readDeletedControl(target, deletion.operation, "SELECT * FROM communication_inbox WHERE communication_id=?", [communicationId]);
      const afterRollout = await deletedRolloutCounts(target, deletion.operation, node);
      if (boundary === "deletion_first") {
        assert.deepEqual(after, []);
        assert.deepEqual(afterRollout, before, "删除先行不能注入 inbox/ambient/context/Job/root");
        // 裸 ID 在 tombstone 后不再是 discovery 候选；沿原公开错误拒绝，不暴露被删除目标。
        const result = assertBoundToolResult(source, sent, history, calls[0], label, "send_message_to_session");
        assertDiscoveryRejection(result, target.sessionId);
        assert.equal(job.error_message, result.result, "builtin send 必须保留原 discovery owner 异常");
        assert.equal(history.response_parts.filter((part) => part.kind === "final_text").length, 0);
        const emitted = await readRollout(source,
          "SELECT model_call_id FROM model_calls WHERE execution_id=(SELECT execution_id FROM model_calls WHERE model_call_id=?) ORDER BY attempt", [calls[0]]);
        assert.deepEqual(emitted.map((row) => row.model_call_id), calls, "终止的 builtin send 不得重发或发起后继调用");
      } else {
        assert.equal(after.length, 1);
        for (const key of ["communication_id", "admission_id", "payload_hash", "wakeup_key", "target_thread_id"]) assert.equal(after[0][key], inbox[0][key]);
        assert.ok(["terminal", "cancelled", "failed"].includes(after[0].state));
        if (before === null) assert.equal(afterRollout, null, "catalog deleting 后不能由迟到 binder 物化 fresh rollout");
        else assert.equal(afterRollout[0].executions, before[0].executions, "catalog deleting 后不得让迟到 binder 创建额外 execution");
      }
      const leaseAfter = lease ? await deletedLease(target, deletion.operation, lease) : null;
      facts.inbox.push({ boundary, communicationId, sent, before, inbox, lease, after, afterRollout, leaseAfter, terminal, job, history, sourceEvidence });
    }

    const reader = await createProductSession(surfaces.a, "I cold history shared guard");
    const readerNode = await sessionCatalogNode(reader);
    const beforeGraph = await graphLifecycle(reader);
    assert.equal(beforeGraph.compiled.length, 0);
    const deletion = await prepareDelete(reader);
    const guard = await armMatch(reader, "history.read.guarded", { thread_id: reader.threadId, kind: "history" });
    const reading = loadThreadHistory(reader);
    await ack(guard);
    await enqueueNavigationIntent(reader, deletion.operation);
    assert.deepEqual(await readCatalog(reader, "SELECT state FROM nodes WHERE node_id=?", [reader.sessionId]), [{ state: "active" }], "cold reader shared guard 必须阻止 catalog deleting 线性化");
    assert.ok(!(await testControl({ action: "events" })).some((event) => event.operation_id === deletion.operation.operationId && event.phase === "deletion.catalog_deleting"));
    await release(guard);
    const history = await reading;
    await ack(deletion.barrier);
    await pendingCatalog(reader);
    assert.equal((await testControl({ action: "events" })).filter((event) => event.phase === "agent.graph.compiled" && event.details?.session_id === reader.sessionId).length, 0, "cold history 不得构建 Agent graph");
    const pending = await deniedHistory(reader);
    const metadata = await readCatalog(reader, "SELECT * FROM nodes WHERE node_id=?", [reader.sessionId]);
    assert.equal(metadata.length, 1);
    assert.equal(metadata[0].state, "deleting");
    assert.deepEqual(await readFrozenControl(reader, readerNode, "SELECT state,generation FROM lifecycle_fence WHERE id=1"), [{ state: "active", generation: 1 }]);
    const reopen = await replaceNodePage(surfaces.a, [reader]);
    const killed = await testControl({ action: "process", name: "a", operation: "kill" });
    await release(deletion.barrier);
    const resumedBarrier = await arm(deletion.operation.operationId, "deletion.catalog_deleting");
    const starting = testControl({ action: "process", name: "a", operation: "start" });
    await ack(resumedBarrier);
    await pendingCatalog(reader);
    assert.deepEqual(await readFrozenControl(reader, readerNode, "SELECT state,generation FROM lifecycle_fence WHERE id=1"), [{ state: "active", generation: 1 }], "重启不得把 local active fence 误当作 catalog active");
    // 启动恢复早于 ready；该屏障只能读真实 SQL，释放后才使用正式 HTTP。
    await release(resumedBarrier);
    const restarted = await starting;
    await reopen();
    const terminal = await navigationTerminal(reader, deletion.operation);
    const journal = (await readCatalog(reader, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operation.operationId]))[0];
    assert.equal(journal.state, "completed");
    assert.equal((await readCatalog(reader, "SELECT COUNT(*) AS count FROM navigation_mutation_records WHERE operation_id=?", [deletion.operation.operationId]))[0].count, 1);
    facts.cold = { readerNode, history, pending, metadata, killed, restarted, terminal, journal };
    return facts;
  } finally {
    for (const barrier of matches) await release(barrier);
    for (const barrier of held) await release(barrier);
  }
}

async function catalogMutation(surface, fields, expected = "committed") {
  if (["rename_node", "move_node"].includes(fields.kind)) {
    const rows = await readCatalog(surface, "SELECT revision FROM nodes WHERE node_id=?", [fields.target_node_id]);
    assert.equal(rows.length, 1);
    fields = { ...fields, expected_revision: rows[0].revision };
  }
  const operation = await navigationIntent(surface, fields);
  const receipt = await navigationTerminal(surface, operation, expected);
  return { operation, receipt };
}

function catalogRow(surface, nodeId) {
  return surface.page.getByTestId(`catalog-node-${surface.gatewayWorkspaceId}-${nodeId}`);
}

function namedCatalogRow(surface, name) {
  return surface.page.locator(".session-resource-row").filter({ has: surface.page.getByText(name, { exact: true }) });
}

async function expandCatalogRow(row) {
  await row.waitFor({ state: "visible" });
  if (await row.locator("..").getAttribute("aria-expanded") === "false") {
    await row.locator(".session-resource-chevron").click();
  }
}

async function openCatalogWorkspace(owner) {
  // 重载重建树展开态；Gateway 当前激活项也可能已由另一客户端改变。
  const row = owner.page.getByTestId(`workspace-node-${owner.gatewayWorkspaceId}`);
  const label = row.locator(".workspace-label");
  const [response] = await Promise.all([
    owner.page.waitForResponse((entry) => entry.request().method() === "POST"
      && new URL(entry.url()).pathname === `/api/gateway/workspaces/${owner.gatewayWorkspaceId}/activate`, { timeout: 30_000 }),
    label.click(),
  ]);
  assert.equal(response.status(), 200, await response.text());
  assert.equal((await response.json()).data.active_workspace_id, owner.gatewayWorkspaceId);
  // POST 完成后仍须等待正式刷新落入镜像；切换中选择 Session 会启动另一轮后台激活。
  await waitUntil(async () => await label.isEnabled()
    && await row.getAttribute("aria-current") === "location", "目标工作区激活及页面刷新完成");
  await expandCatalogRow(row);
}

async function browserCatalogOutbox(surface) {
  let records;
  await waitUntil(async () => {
    records = await surface.page.evaluate(async (workspaceId) => {
      // 观察器不能抢先创建空库；schema 只能由产品的持久层初始化。
      const databases = await indexedDB.databases();
      if (!databases.some((database) => database.name === "boxteam-session-catalog-outbox")) return null;
      const database = await new Promise((resolve, reject) => {
        const request = indexedDB.open("boxteam-session-catalog-outbox");
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      try {
        const transaction = database.transaction("operations", "readonly");
        const records = await new Promise((resolve, reject) => {
          const request = transaction.objectStore("operations").getAll();
          request.onsuccess = () => resolve(request.result);
          request.onerror = () => reject(request.error);
        });
        return records.filter((record) => record.partition_key.split("\u0000")[1] === workspaceId)
          .sort((left, right) => left.client_sequence - right.client_sequence);
      } finally {
        database.close();
      }
    }, surface.workspaceIdentity);
    return records !== null;
  }, "产品创建原会话目录 outbox");
  return records;
}

async function catalogFolderDialog(surface, { name, parentName = null, renameName = null, deleteName = null }) {
  const row = parentName || renameName || deleteName
    ? namedCatalogRow(surface, parentName || renameName || deleteName)
    : surface.page.getByTestId(`workspace-node-${surface.gatewayWorkspaceId}`);
  await row.click({ button: "right" });
  const item = deleteName ? "删除文件夹及内容" : renameName ? "重命名" : parentName ? "新建子文件夹" : "新建会话文件夹";
  await surface.page.getByRole("menuitem", { name: item, exact: true }).click();
  const title = deleteName ? "永久删除会话文件夹" : renameName ? "重命名会话文件夹" : parentName ? "新建子文件夹" : "新建会话文件夹";
  const dialog = surface.page.getByRole("dialog", { name: title, exact: true });
  await dialog.waitFor({ state: "visible" });
  if (!deleteName) await dialog.getByRole("textbox", { name: "文件夹名称", exact: true }).fill(name);
  await dialog.getByRole("button", { name: deleteName ? "删除" : parentName || renameName ? "保存" : "创建", exact: true }).click();
  return dialog;
}

async function runNavigationOptimism(surfaces) {
  const surface = surfaces.a;
  const label = `Q-${Bun.randomUUIDv7()}`;
  const facts = { requests: [], receipts: [], local: [] };
  result.section8.navigation_optimism = facts;
  const held = [];
  const exactHeld = [];
  const wireReads = new Set();
  const wirePages = [];
  let peerContext;
  let restoreDirectoryPermissions;
  let eventTransport;
  const transportReads = new Set();
  let scenarioError;
  const settledDeletionDom = async (deletedIds) => {
    const catalog = (await ownerApi(surface, "/api/v1/session-catalog/export")).data;
    const deleted = new Set(deletedIds);
    assert.ok(!catalog.items.some((node) => deleted.has(node.node_id)));
    let currentSessionId;
    let title;
    await waitUntil(async () => {
      for (const nodeId of deleted) {
        if (await catalogRow(surface, nodeId).count()) return false;
      }
      const current = surface.page.locator(".session-resource-row.current-session");
      const count = await current.count();
      title = (await surface.page.locator(".command-center-title").innerText()).trim();
      if (count === 0) {
        currentSessionId = null;
        return title === "新会话" && await surface.page.locator(".chat-user-text").count() === 0;
      }
      if (count !== 1) return false;
      currentSessionId = await current.locator("button[data-session-id]").getAttribute("data-session-id");
      const canonical = catalog.items.find((node) => node.node_id === currentSessionId);
      return canonical?.kind === "session" && canonical.state === "active"
        && !deleted.has(currentSessionId) && title === canonical.name;
    }, "settled 删除重载后隐藏冻结子树并清空或切换到权威剩余会话");
    return { deletedIds: [...deleted], currentSessionId, title };
  };
  const attachWire = (page) => {
    const request = (entry) => {
      if (entry.method() !== "POST" || new URL(entry.url()).pathname !== "/api/v1/session-catalog/operations:enqueue") return;
      facts.requests.push({ page: page.url(), at: Date.now(), workspaceId: entry.headers()["x-boxteam-workspace-id"], ...entry.postDataJSON() });
    };
    const response = (entry) => {
      if (entry.request().method() !== "POST" || new URL(entry.url()).pathname !== "/api/v1/session-catalog/operations:enqueue") return;
      const fact = { at: Date.now(), status: entry.status(), requestId: entry.headers()["x-request-id"], operationIds: entry.request().postDataJSON().intents.map((intent) => intent.client_operation_id) };
      facts.receipts.push(fact);
      const read = entry.json().then((body) => { fact.body = body; }, (error) => { fact.aborted = String(error); });
      wireReads.add(read);
      void read.then(() => wireReads.delete(read));
    };
    page.on("request", request);
    page.on("response", response);
    wirePages.push({ page, request, response });
  };
  const match = async (phase, details, tag) => {
    const barrier = { barrier_id: `${label}-${tag}`, phase, details: { workspace_id: surface.workspaceIdentity, ...details } };
    await testControl({ action: "arm_match", ...barrier });
    held.push(barrier);
    return barrier;
  };
  const acknowledge = (barrier) => testControl({ action: "acknowledged_match", barrier_id: barrier.barrier_id });
  const release = (barrier) => testControl({ action: "release_match", barrier_id: barrier.barrier_id });
  const localOperation = async (owner, action, predicate, description) => {
    const previous = new Set((await browserCatalogOutbox(owner)).map((record) => record.operation.client_operation_id));
    const dialog = await action();
    if (dialog) await dialog.waitFor({ state: "hidden", timeout: 5000 });
    let stored;
    await waitUntil(async () => {
      stored = (await browserCatalogOutbox(owner)).find((record) => !previous.has(record.operation.client_operation_id) && predicate(record.operation));
      return stored !== undefined;
    }, `${description}的原 IndexedDB 意图`, 5000);
    assert.equal(stored.operation.client_sequence, stored.client_sequence);
    assert.ok(stored.client_sequence > 0);
    facts.local.push(stored);
    return { operationId: stored.operation.client_operation_id, intent: stored.operation, stored };
  };
  const createCanonicalFolder = async (name) => {
    const barrier = await match("navigation.before_commit", { kind: "create_folder", name }, name);
    const operation = await localOperation(surface, () => catalogFolderDialog(surface, { name }), (intent) => intent.kind === "create_folder" && intent.name === name, name);
    assert.equal((await acknowledge(barrier)).operation_id, operation.operationId);
    await release(barrier);
    const receipt = await navigationTerminal(surface, operation);
    await catalogRow(surface, receipt.created_node_id).waitFor({ state: "visible" });
    return { name, nodeId: receipt.created_node_id, operation, receipt };
  };
  const assertDomParent = async (row, parent) => {
    const parentTestId = await row.evaluate((element) => element.closest('[role="treeitem"]').parentElement.closest('[role="treeitem"]')
      ?.querySelector(":scope > .session-resource-row")?.getAttribute("data-testid"));
    assert.equal(parentTestId, await parent.getAttribute("data-testid"), "真实资源树必须同步展示 pending 父链");
  };
  attachWire(surface.page);
  try {
    await openCatalogWorkspace(surface);
    const first = await createProductSession(surface, `${label} 会话一`);
    const second = await createProductSession(surface, `${label} 会话二`);
    await completeOwnerTurn(first, "为真实 pinned fork 留下已完成的 source assembly。", [finalResponse(`${label}-source-ready`, "原会话上下文已完成。")]);
    await openOwner(first);
    const folderOne = await createCanonicalFolder(`${label} 文件夹一`);
    const folderTwo = await createCanonicalFolder(`${label} 文件夹二`);
    const independent = await createCanonicalFolder(`${label} 独立文件夹`);
    const host = await createCanonicalFolder(`${label} 冲突宿主`);
    const sourceRows = await readCatalog(surface, "SELECT node_id,parent_node_id,revision FROM nodes WHERE node_id IN (?,?,?,?) ORDER BY node_id", [first.sessionId, second.sessionId, folderOne.nodeId, folderTwo.nodeId]);
    const firstPage = (await ownerApi(surface, "/api/v1/session-catalog/children?limit=1")).data;
    assert.equal(typeof firstPage.cursor, "string");
    const rootName = `${label} pending 父文件夹`;
    const childName = `${label} pending 子文件夹`;
    const enqueueHold = await match("navigation.enqueued", { kind: "create_folder", name: rootName }, "enqueue");
    const commitHold = await match("navigation.before_commit", { kind: "create_folder", name: rootName }, "commit");
    const root = await localOperation(surface, () => catalogFolderDialog(surface, { name: rootName }), (intent) => intent.name === rootName, "父 Folder");
    const enqueued = await acknowledge(enqueueHold);
    assert.equal(enqueued.operation_id, root.operationId);
    assert.equal((await acknowledge(commitHold)).operation_id, root.operationId);
    await namedCatalogRow(surface, rootName).locator(".session-resource-pending-state").waitFor({ state: "visible" });
    assert.ok(!facts.receipts.some((receipt) => receipt.operationIds.includes(root.operationId)), "HTTP 回执尚被 owner phase 阻塞时，对话框应已关闭且 DOM 已更新");
    const reserved = (await readCatalog(surface, "SELECT * FROM navigation_mutation_records WHERE operation_id=?", [root.operationId]))[0];
    assert.equal(reserved.state, "queued");
    assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [reserved.reserved_node_id]), []);
    await expandCatalogRow(namedCatalogRow(surface, rootName));
    const child = await localOperation(surface, () => catalogFolderDialog(surface, { name: childName, parentName: rootName }), (intent) => intent.name === childName, "子 Folder");
    assert.equal(child.stored.operation.created_by_operation_id, root.operationId);
    await expandCatalogRow(namedCatalogRow(surface, childName));
    const moves = [];
    for (const nodeId of [first.sessionId, second.sessionId, folderOne.nodeId, folderTwo.nodeId]) {
      const moved = await localOperation(surface, async () => {
        await catalogRow(surface, nodeId).dragTo(namedCatalogRow(surface, rootName));
      }, (intent) => intent.kind === "move_node" && intent.target_node_id === nodeId, `移动 ${nodeId}`);
      assert.equal(moved.stored.operation.created_by_operation_id, root.operationId);
      await assertDomParent(catalogRow(surface, nodeId), namedCatalogRow(surface, rootName));
      moves.push(moved);
    }
    const twice = await localOperation(surface, async () => {
      await catalogRow(surface, first.sessionId).dragTo(namedCatalogRow(surface, childName));
    }, (intent) => intent.kind === "move_node" && intent.target_node_id === first.sessionId, "同 Session 二次移动");
    assert.equal(twice.stored.operation.created_by_operation_id, child.operationId);
    assert.ok(twice.stored.operation.depends_on.includes(moves[0].operationId));
    assert.ok(twice.stored.operation.depends_on.includes(child.operationId));
    await assertDomParent(catalogRow(surface, first.sessionId), namedCatalogRow(surface, childName));
    const pending = [root, child, ...moves, twice];
    const diskBeforeReload = await browserCatalogOutbox(surface);
    const sequences = pending.map((operation) => operation.stored.client_sequence);
    assert.deepEqual(sequences, [...sequences].sort((left, right) => left - right));
    assert.equal(new Set(sequences).size, pending.length);
    assert.deepEqual(await readCatalog(surface, "SELECT node_id,parent_node_id,revision FROM nodes WHERE node_id IN (?,?,?,?) ORDER BY node_id", sourceRows.map((node) => node.node_id)), sourceRows, "pending 投影不能预先改写 SQLite 父链");
    const beforeExport = (await ownerApi(surface, "/api/v1/session-catalog/export")).data;
    assert.ok(!beforeExport.items.some((node) => node.name === rootName || node.name === childName), "Gateway 不得将 client_ref 投影当作 CSM 权威节点");
    await drainPageResponseBodies(surface.page);
    await surface.page.reload({ waitUntil: "domcontentloaded" });
    await openCatalogWorkspace(surface);
    const diskAfterReload = await browserCatalogOutbox(surface);
    const originalPending = diskBeforeReload.filter((record) => pending.some((operation) =>
      operation.operationId === record.operation.client_operation_id));
    const restoredPending = diskAfterReload.filter((record) => pending.some((operation) =>
      operation.operationId === record.operation.client_operation_id));
    const frozenCommand = (record) => {
      const operation = { ...record.operation };
      // 回执镜像可按权威版本推进；所有命令字段及外层分区/序号逐字段保持原值。
      for (const field of ["state", "server_state", "client_ref", "created_node_id",
        "committed_catalog_revision", "queue_seq", "receipt_revision", "settlement",
        "error_code", "error_detail"]) delete operation[field];
      return { ...record, operation };
    };
    assert.deepEqual(restoredPending.map(frozenCommand), originalPending.map(frozenCommand),
      "丢失 202 后刷新必须保留逐字段原意图、分区与序号");
    const restoredReceipts = (await ownerApi(surface, `/api/v1/session-catalog/operations?${new URLSearchParams(
      pending.map((operation) => ["operation_id", operation.operationId]),
    )}`)).data;
    facts.pendingReload = { diskBeforeReload, diskAfterReload, restoredReceipts };
    for (const record of restoredPending) {
      const operation = record.operation;
      const original = originalPending.find((item) => item.operation.client_operation_id === operation.client_operation_id);
      if (operation.receipt_revision === null) {
        assert.equal(operation.state, "pending_local");
        assert.deepEqual(record, original, "未取得回执的原持久记录不得改变");
        continue;
      }
      const receipt = restoredReceipts.items.find((item) => item.operation_id === operation.client_operation_id);
      assert.ok(receipt, "已落盘回执必须能按原 operation ID 从后端读取");
      assert.equal(operation.state, "accepted", "worker 未提交时不得把 durable acceptance 当作 committed");
      assert.equal(operation.client_sequence, receipt.client_sequence);
      assert.equal(operation.server_state, receipt.state);
      for (const field of ["created_node_id", "committed_catalog_revision", "queue_seq",
        "receipt_revision", "settlement", "error_code", "error_detail"]) {
        assert.deepEqual(operation[field], receipt[field], `刷新后的持久 ${field} 必须来自真实回执`);
      }
      assert.equal(operation.client_ref, receipt.created_node_id ? null : original.operation.client_ref);
    }
    await release(enqueueHold);
    const reopen = await replaceNodePage(surface, [first, second]);
    const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await release(commitHold);
    const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    assert.notEqual(killed.before_pid, restarted.after_pid);
    await reopen();
    await openCatalogWorkspace(surface);
    attachWire(surface.page);
    const terminals = [];
    for (const operation of pending) terminals.push(await navigationTerminal(surface, operation));
    const canonicalRoot = terminals[0].created_node_id;
    const canonicalChild = terminals[1].created_node_id;
    const queueAfter = await readCatalog(surface, `SELECT operation_id,queue_seq,preimage_hash,client_sequence,state FROM navigation_mutation_records WHERE operation_id IN (${pending.map(() => "?").join(",")}) ORDER BY client_sequence`, pending.map((operation) => operation.operationId));
    assert.equal(queueAfter.length, pending.length, "丢回执和 backend 重启不能重新分配 operation ID");
    assert.ok(queueAfter.every((record) => record.state === "committed"));
    const original = queueAfter.find((record) => record.operation_id === root.operationId);
    assert.equal(original.preimage_hash, reserved.preimage_hash);
    assert.equal(original.queue_seq, reserved.queue_seq);
    assert.equal(original.client_sequence, reserved.client_sequence);
    const parents = await readCatalog(surface, "SELECT node_id,parent_node_id FROM nodes WHERE node_id IN (?,?,?,?,?) ORDER BY node_id", [canonicalChild, first.sessionId, second.sessionId, folderOne.nodeId, folderTwo.nodeId]);
    const expectedParents = [[canonicalChild, canonicalRoot], [first.sessionId, canonicalChild], [second.sessionId, canonicalRoot], [folderOne.nodeId, canonicalRoot], [folderTwo.nodeId, canonicalRoot]].map(([node_id, parent_node_id]) => ({ node_id, parent_node_id })).sort((left, right) => left.node_id.localeCompare(right.node_id));
    assert.deepEqual(parents, expectedParents);
    const breadcrumb = (await ownerApi(surface, `/api/v1/session-catalog/breadcrumb/${first.sessionId}`)).data;
    assert.deepEqual(breadcrumb.items.map((node) => node.node_id), [canonicalRoot, canonicalChild, first.sessionId]);
    const rootChildren = (await ownerApi(surface, `/api/v1/session-catalog/children?parent_node_id=${canonicalRoot}&limit=100`)).data;
    assert.equal(rootChildren.total, 4);
    assert.equal(new Set(rootChildren.items.map((node) => node.node_id)).size, 4);
    const stalePage = await ownerApiResponse(surface,
      `/api/v1/session-catalog/children?limit=1&cursor=${encodeURIComponent(firstPage.cursor)}`);
    assert.equal(stalePage.status, 409);
    assert.match(JSON.stringify(stalePage.body), /已更新|revision_changed/);
    const staleBody = stalePage.body;
    const paginated = [];
    let cursor;
    do {
      const path = `/api/v1/session-catalog/children?parent_node_id=${canonicalRoot}&limit=1${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
      const next = (await ownerApi(surface, path)).data;
      assert.equal(next.catalog_revision, rootChildren.catalog_revision);
      paginated.push(...next.items.map((node) => node.node_id));
      cursor = next.cursor;
    } while (cursor);
    assert.equal(paginated.length, 4);
    assert.equal(new Set(paginated).size, 4, "重新发牌后的真实分页必须无重影、无丢失");
    facts.pagination = { staleBody, paginated };
    await waitUntil(async () => !(await browserCatalogOutbox(surface)).some((record) => pending.some((operation) => operation.operationId === record.operation.client_operation_id)), "真实 confirmed 投影后清理原 outbox");
    await openOwner(first);
    await expandCatalogRow(namedCatalogRow(surface, rootName));
    await expandCatalogRow(namedCatalogRow(surface, childName));
    facts.pendingRecovery = { pending, reserved, enqueued, diskBeforeReload, diskAfterReload, restoredReceipts,
      killed, restarted, terminals, queueAfter, parents, breadcrumb, rootChildren };

    const peerStorage = await surface.page.context().storageState();
    peerContext = await browserStartup("navigation_peer_context", () => browser.newContext({ storageState: peerStorage, viewport: { width: 1920, height: 1100 } }));
    const peerPage = await browserStartup("navigation_peer_page", () => peerContext.newPage());
    installPageDiagnostics(peerPage);
    attachWire(peerPage);
    await ensureGuest(peerPage, JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"))[surface.name].base_url);
    const peer = { ...surface, page: peerPage };
    await openCatalogWorkspace(peer);
    const conflicting = await match("navigation.before_commit", { kind: "delete_folder", target_node_id: host.nodeId }, "conflicting-delete");
    const hostDelete = await localOperation(peer, () => catalogFolderDialog(peer, { deleteName: host.name }), (intent) => intent.kind === "delete_folder" && intent.target_node_id === host.nodeId, "另一客户端先删除宿主");
    assert.equal((await acknowledge(conflicting)).operation_id, hostDelete.operationId);
    await expandCatalogRow(namedCatalogRow(surface, host.name));
    const failedName = `${label} 失败依赖父 Folder`;
    const failedParent = await localOperation(surface, () => catalogFolderDialog(surface, { name: failedName, parentName: host.name }), (intent) => intent.name === failedName, "已排队删除宿主下的父 Folder");
    await expandCatalogRow(namedCatalogRow(surface, failedName));
    const failedMove = await localOperation(surface, async () => {
      await catalogRow(surface, second.sessionId).dragTo(namedCatalogRow(surface, failedName));
    }, (intent) => intent.kind === "move_node" && intent.target_node_id === second.sessionId, "失败 Folder 的 move 依赖");
    assert.equal(failedMove.stored.operation.created_by_operation_id, failedParent.operationId);
    const independentName = `${label} 独立命令成功`;
    const independentRename = await localOperation(surface, () => catalogFolderDialog(surface, { name: independentName, renameName: independent.name }), (intent) => intent.name === independentName, "独立重命名");
    await expandCatalogRow(namedCatalogRow(peer, rootName));
    const peerName = `${label} 另一客户端成功`;
    const peerRename = await localOperation(peer, () => catalogFolderDialog(peer, { name: peerName, renameName: folderTwo.name }), (intent) => intent.name === peerName, "另一客户端独立重命名");
    await release(conflicting);
    const failureReceipts = [];
    for (const [owner, operation, expected] of [[peer, hostDelete, "committed"], [surface, failedParent, "rejected"], [surface, failedMove, "dependency_failed"], [surface, independentRename, "committed"], [peer, peerRename, "committed"]]) {
      failureReceipts.push(await navigationTerminal(owner, operation, expected));
    }
    await namedCatalogRow(surface, independentName).waitFor({ state: "visible" });
    await namedCatalogRow(surface, peerName).waitFor({ state: "visible" });
    await waitUntil(async () => (await namedCatalogRow(surface, failedName).count()) === 0, "失败父 Folder 回退");
    await assertDomParent(catalogRow(surface, second.sessionId), namedCatalogRow(surface, rootName));
    assert.equal((await readCatalog(surface, "SELECT parent_node_id FROM nodes WHERE node_id=?", [second.sessionId]))[0].parent_node_id, canonicalRoot);
    const failedDiskIds = [failedParent.operationId, failedMove.operationId];
    await waitUntil(async () => !(await browserCatalogOutbox(surface)).some((record) => failedDiskIds.includes(record.operation.client_operation_id)), "失败依赖只在最新 confirmed 对账后持久清除");
    facts.conflict = { hostDelete, failedParent, failedMove, independentRename, peerRename, failureReceipts };

    const failedLocalName = `${label} IndexedDB abort`;
    await surface.page.evaluate((operationName) => {
      const original = IDBObjectStore.prototype.add;
      IDBObjectStore.prototype.add = function (value, ...args) {
        const request = original.call(this, value, ...args);
        if (this.name === "operations" && this.transaction.db.name === "boxteam-session-catalog-outbox"
          && value?.operation?.name === operationName) {
          IDBObjectStore.prototype.add = original;
          const transaction = this.transaction;
          window.__section8OutboxAbort = {
            operationId: value.operation.client_operation_id,
            name: value.operation.name,
            stores: Array.from(transaction.objectStoreNames),
          };
          queueMicrotask(() => transaction.abort());
        }
        return request;
      };
    }, failedLocalName);
    const localFailureDialog = await catalogFolderDialog(surface, { name: failedLocalName });
    await localFailureDialog.getByRole("alert").waitFor({ state: "visible" });
    assert.match(await localFailureDialog.getByRole("alert").innerText(), /持久化|中止|abort|Abort/i);
    const localAbort = await surface.page.evaluate(() => window.__section8OutboxAbort);
    assert.equal(localAbort.name, failedLocalName, "必须真实 abort 本次创建命令的原子写入");
    canonicalUuid7(localAbort.operationId, "op");
    assert.deepEqual(localAbort.stores, ["operations", "partition-sequences"]);
    assert.deepEqual(await readCatalog(surface, "SELECT operation_id FROM navigation_mutation_records WHERE json_extract(params_json,'$.name')=?", [failedLocalName]), [], "本地写失败不能入队后端");
    assert.ok(!(await browserCatalogOutbox(surface)).some((record) => record.operation.name === failedLocalName));
    assert.equal(await namedCatalogRow(surface, failedLocalName).count(), 0);
    await localFailureDialog.getByRole("button", { name: "取消", exact: true }).click();
    facts.localAbort = { ...localAbort, backendEnqueued: false };

    const eventPageUrl = peerPage.url();
    // 先结束旧文档的请求，再开启拦截并走真实页面初始化，避免 reload 撤销已暂停的旧请求。
    await drainPageResponseBodies(peerPage);
    await peerPage.goto("about:blank", { waitUntil: "load" });
    const eventWire = { activitySseDropped: [], pages: [], transportErrors: [] };
    facts.eventRecovery = { eventWire };
    let holdEventPage = false;
    let heldEventPage;
    let queryAfter;
    let transportError;
    eventTransport = await peerPage.context().newCDPSession(peerPage);
    eventTransport.on("Fetch.requestPaused", (entry) => {
      const pending = (async () => {
        const url = new URL(entry.request.url);
        const workspaceId = Object.entries(entry.request.headers).find(([name]) => name.toLowerCase() === "x-boxteam-workspace-id")?.[1];
        // 三工作区仍正常运行；传输实验只针对本场景原 A 目标。
        if (workspaceId !== surface.gatewayWorkspaceId) {
          await eventTransport.send(entry.responseStatusCode === undefined ? "Fetch.continueRequest" : "Fetch.continueResponse", { requestId: entry.requestId });
          return;
        }
        if (entry.responseStatusCode === undefined) {
          if (url.pathname === "/api/v1/workspace/observations/stream") {
            assert.equal(entry.request.method, "POST");
            const { subscriptions } = JSON.parse(entry.request.postData);
            assert.ok(Array.isArray(subscriptions));
            if (subscriptions.some((subscription) => subscription.kind === "session_activity")) {
              // 活动观察者已合并到唯一工作区传输；只取消包含它的真实目标请求。
              await eventTransport.send("Fetch.failRequest", { requestId: entry.requestId, errorReason: "Aborted" });
              eventWire.activitySseDropped.push({ url: entry.request.url, workspaceId, subscriptions, at: Date.now() });
              return;
            }
          }
          if (url.pathname === "/api/v1/session-catalog/navigation-events" && queryAfter !== undefined) {
            const originalUrl = url.href;
            url.searchParams.delete("cursor");
            url.searchParams.set("after", String(queryAfter));
            eventWire.cursorQuery = { originalUrl, forwardedUrl: url.href, after: queryAfter };
            queryAfter = undefined;
            // 只让真实只读请求携带失效 cursor，响应仍由原 Gateway/backend 产生。
            await eventTransport.send("Fetch.continueRequest", { requestId: entry.requestId, url: url.href });
            return;
          }
          await eventTransport.send("Fetch.continueRequest", { requestId: entry.requestId });
          return;
        }
        if (entry.responseStatusCode === 200) {
          const body = await eventTransport.send("Fetch.getResponseBody", { requestId: entry.requestId });
          const bytes = body.base64Encoded ? Buffer.from(body.body, "base64") : Buffer.from(body.body);
          const payload = JSON.parse(bytes.toString("utf8"));
          const httpRequestId = entry.responseHeaders.find((header) => header.name.toLowerCase() === "x-request-id")?.value;
          assert.equal(payload.request_id, httpRequestId);
          const captured = { url: entry.request.url, httpRequestId, bytes: bytes.length,
            sha256: createHash("sha256").update(bytes).digest("hex"), data: payload.data };
          if (url.pathname === "/api/v1/session-catalog/navigation-events") {
            eventWire.pages.push(captured);
            if (payload.data.cursor_gone) eventWire.cursorGone = captured;
            if (holdEventPage) {
              holdEventPage = false;
              heldEventPage = { requestId: entry.requestId, captured };
              return;
            }
          } else if (eventWire.cursorGone && url.pathname === "/api/v1/session-catalog/snapshot") {
            eventWire.recoverySnapshot = captured;
          }
        }
        // 继续传送原响应；不替换任何 status、header 或 body 字节。
        await eventTransport.send("Fetch.continueResponse", { requestId: entry.requestId });
      })();
      transportReads.add(pending);
      void pending.then(() => transportReads.delete(pending), (error) => {
        eventWire.transportErrors.push({ requestId: entry.requestId, networkId: entry.networkId,
          url: entry.request.url, method: entry.request.method, responseStatusCode: entry.responseStatusCode,
          error: String(error) });
        transportError = error;
        transportReads.delete(pending);
      });
    });
    await eventTransport.send("Fetch.enable", { patterns: [
      { urlPattern: "*/api/v1/workspace/observations/stream*", requestStage: "Request" },
      { urlPattern: "*/api/v1/session-catalog/navigation-events*", requestStage: "Request" },
      { urlPattern: "*/api/v1/session-catalog/navigation-events*", requestStage: "Response" },
      { urlPattern: "*/api/v1/session-catalog/snapshot*", requestStage: "Response" },
    ] });
    await drainPageResponseBodies(peerPage);
    await peerPage.goto(eventPageUrl, { waitUntil: "domcontentloaded" });
    await openCatalogWorkspace(peer);
    await namedCatalogRow(peer, independentName).waitFor({ state: "visible" });
    await waitUntil(() => {
      assert.ifError(transportError);
      return eventWire.activitySseDropped.length > 0;
    }, "真实会话活动 SSE 传输被取消");
    const withoutSseName = `${label} 会话活动流不可用后成功`;
    const withoutSseHold = await match("navigation.before_commit", { kind: "rename_node", target_node_id: independent.nodeId, name: withoutSseName }, "activity-stream-unavailable");
    const withoutSse = await localOperation(peer, () => catalogFolderDialog(peer, { name: withoutSseName, renameName: independentName }), (intent) => intent.name === withoutSseName, "会话活动流不可用时重命名");
    assert.equal((await acknowledge(withoutSseHold)).operation_id, withoutSse.operationId);
    await release(withoutSseHold);
    const withoutSseReceipt = await navigationTerminal(peer, withoutSse);
    await namedCatalogRow(peer, withoutSseName).waitFor({ state: "visible" });
    await waitUntil(async () => !(await browserCatalogOutbox(peer)).some((record) => record.operation.client_operation_id === withoutSse.operationId), "会话活动流不可用时按原 ID 对账并清除 outbox");
    assert.equal((await readCatalog(surface, "SELECT COUNT(*) AS count FROM navigation_mutation_records WHERE operation_id=?", [withoutSse.operationId]))[0].count, 1);
    await namedCatalogRow(surface, withoutSseName).waitFor({ state: "visible" });
    holdEventPage = true;
    await waitUntil(() => {
      assert.ifError(transportError);
      return heldEventPage !== undefined;
    }, "真实旧 navigation 响应已冻结");
    const delayedPage = heldEventPage.captured;
    const newerName = `${label} 新 confirmed 先于旧事件页`;
    const newerHold = await match("navigation.before_commit", { kind: "rename_node", target_node_id: independent.nodeId, name: newerName }, "late-event-page");
    const newer = await localOperation(surface, () => catalogFolderDialog(surface, { name: newerName, renameName: withoutSseName }), (intent) => intent.name === newerName, "另一客户端更新 confirmed");
    assert.equal((await acknowledge(newerHold)).operation_id, newer.operationId);
    await release(newerHold);
    const newerReceipt = await navigationTerminal(surface, newer);
    await namedCatalogRow(surface, newerName).waitFor({ state: "visible" });
    await peerPage.getByRole("button", { name: "刷新资源树", exact: true }).click();
    await namedCatalogRow(peer, newerName).waitFor({ state: "visible" });
    const newerSnapshot = (await ownerApi(peer, "/api/v1/session-catalog/snapshot")).data;
    assert.ok(newerSnapshot.event_seq_watermark > delayedPage.data.event_seq_watermark);
    await eventTransport.send("Fetch.continueResponse", { requestId: heldEventPage.requestId });
    heldEventPage = undefined;
    await waitUntil(() => {
      assert.ifError(transportError);
      return eventWire.pages.some((page) => page.data.event_seq_watermark >= newerSnapshot.event_seq_watermark);
    }, "旧事件页迟到后继续消费真实新 revision");
    assert.equal(await namedCatalogRow(peer, withoutSseName).count(), 0);
    assert.equal(await catalogRow(peer, independent.nodeId).count(), 1);
    queryAfter = newerSnapshot.event_seq_watermark + 1;
    await waitUntil(() => {
      assert.ifError(transportError);
      return eventWire.cursorGone !== undefined && eventWire.recoverySnapshot !== undefined;
    }, "真实 cursor gone 后自动重读权威 snapshot");
    assert.equal(eventWire.cursorGone.data.cursor_gone, true);
    assert.equal(eventWire.cursorGone.data.workspace_id, surface.workspaceIdentity);
    assert.ok(eventWire.recoverySnapshot.data.catalog_revision >= newerSnapshot.catalog_revision);
    await namedCatalogRow(peer, newerName).waitFor({ state: "visible" });
    assert.equal(await catalogRow(peer, independent.nodeId).count(), 1);
    assert.ifError(transportError);
    facts.eventRecovery = { withoutSse, withoutSseReceipt, delayedPage, newer, newerReceipt, newerSnapshot, eventWire };
    // 先停止接收新拦截并排空已接收回调；Fetch 关闭会让尚在读正文的回调失败。
    eventTransport.removeAllListeners("Fetch.requestPaused");
    await Promise.all([...transportReads]);
    await eventTransport.send("Fetch.disable");
    await eventTransport.detach();
    eventTransport = null;
    await Promise.all(peerContext.pages().map(drainPageResponseBodies));
    await peerContext.close();
    peerContext = null;
    const pinned = await forkOwner(first, { mode: "context_fork", relationship: "pinned" });
    await openOwner(first);
    const blocked = await match("navigation.before_commit", { kind: "delete_folder", target_node_id: canonicalRoot }, "blocked-subtree-delete");
    const rejectedDelete = await localOperation(surface, () => catalogFolderDialog(surface, { deleteName: rootName }), (intent) => intent.kind === "delete_folder" && intent.target_node_id === canonicalRoot, "有 pinned blocker 的递归删除");
    assert.equal((await acknowledge(blocked)).operation_id, rejectedDelete.operationId);
    assert.equal(await catalogRow(surface, first.sessionId).count(), 0, "pending 递归删除应隐藏后代");
    assert.equal(await surface.page.locator("#input").isVisible(), true, "pending 不能关闭已打开聊天");
    await release(blocked);
    const rejectedReceipt = await navigationTerminal(surface, rejectedDelete, "rejected");
    assert.equal(rejectedReceipt.error_code, "source_retained_by_fork");
    await namedCatalogRow(surface, rootName).waitFor({ state: "visible" });
    await expandCatalogRow(namedCatalogRow(surface, rootName));
    await expandCatalogRow(namedCatalogRow(surface, childName));
    await catalogRow(surface, first.sessionId).waitFor({ state: "visible" });
    assert.equal(await surface.page.locator("#input").isVisible(), true);
    await deleteOwner(pinned.owner);
    const deletingHold = await match("navigation.before_commit", { kind: "delete_folder", target_node_id: canonicalRoot }, "subtree-delete");
    const deletion = await localOperation(surface, () => catalogFolderDialog(surface, { deleteName: rootName }), (intent) => intent.kind === "delete_folder" && intent.target_node_id === canonicalRoot, "递归删除真实提交");
    assert.equal((await acknowledge(deletingHold)).operation_id, deletion.operationId);
    const catalogDeleting = { operation_id: deletion.operationId, phase: "deletion.catalog_deleting" };
    await testControl({ action: "arm", ...catalogDeleting });
    exactHeld.push(catalogDeleting);
    await release(deletingHold);
    await testControl({ action: "acknowledged", ...catalogDeleting });
    const pendingReceipt = (await ownerApi(surface, `/api/v1/session-catalog/operations?operation_id=${deletion.operationId}`)).data.items[0];
    assert.equal(pendingReceipt.state, "committed");
    assert.equal(pendingReceipt.settlement, "pending");
    assert.ok(!("pending_settlement" in pendingReceipt));
    assert.equal(await surface.page.locator("#input").isVisible(), true, "committed/pending 排空期间仍保留已打开聊天");
    const journal = (await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]))[0];
    const frozenIds = JSON.parse(journal.frozen_node_ids).map((node) => node.node_id);
    const deletingNodes = await readCatalog(surface, `SELECT node_id,state FROM nodes WHERE node_id IN (${frozenIds.map(() => "?").join(",")})`, frozenIds);
    assert.ok(deletingNodes.length > 0 && deletingNodes.every((node) => node.state === "deleting"));
    const reopenDeleted = await replaceNodePage(surface, []);
    const deletionKilled = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await testControl({ action: "release", ...catalogDeleting });
    const deletionRestarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    await reopenDeleted();
    await openCatalogWorkspace(surface);
    attachWire(surface.page);
    const settledReceipt = await navigationTerminal(surface, deletion);
    assert.equal(settledReceipt.settlement, "settled");
    assert.ok(settledReceipt.receipt_revision > pendingReceipt.receipt_revision);
    assert.deepEqual(await readCatalog(surface, `SELECT node_id FROM nodes WHERE node_id IN (${frozenIds.map(() => "?").join(",")})`, frozenIds), []);
    const recoveredJournal = (await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]))[0];
    assert.equal(recoveredJournal.state, "completed");
    assert.equal(recoveredJournal.frozen_node_ids, journal.frozen_node_ids);
    assert.equal((await readCatalog(surface, "SELECT COUNT(*) AS count FROM navigation_mutation_records WHERE operation_id=?", [deletion.operationId]))[0].count, 1);
    const afterDelete = (await ownerApi(surface, "/api/v1/session-catalog/export")).data;
    assert.ok(!afterDelete.items.some((node) => frozenIds.includes(node.node_id)));
    const deletionDom = await settledDeletionDom(frozenIds);
    assert.equal(await surface.page.locator(".chat-user-text").filter({ hasText: "为真实 pinned fork 留下已完成的 source assembly。" }).count(), 0, "已删除当前会话的聊天不能在重载后继续显示");
    facts.deletion = { rejectedDelete, rejectedReceipt, deletion, pendingReceipt, settledReceipt, journal, deletingNodes, deletionKilled, deletionRestarted, recoveredJournal, deletionDom };

    const repairOwner = await createProductSession(surface, `${label} 排空权限故障会话`);
    const repairNode = await sessionCatalogNode(repairOwner);
    const repairFolder = await createCanonicalFolder(`${label} 排空故障文件夹`);
    const repairMove = await localOperation(surface, async () => {
      await catalogRow(surface, repairOwner.sessionId).dragTo(namedCatalogRow(surface, repairFolder.name));
    }, (intent) => intent.kind === "move_node" && intent.target_node_id === repairOwner.sessionId, "排空故障会话移入 Folder");
    await navigationTerminal(surface, repairMove);
    await openOwner(repairOwner);
    const repairHold = await match("navigation.before_commit", { kind: "delete_folder", target_node_id: repairFolder.nodeId }, "repair-subtree-delete");
    const repairDelete = await localOperation(surface, () => catalogFolderDialog(surface, { deleteName: repairFolder.name }),
      (intent) => intent.kind === "delete_folder" && intent.target_node_id === repairFolder.nodeId, "排空故障递归删除");
    assert.equal((await acknowledge(repairHold)).operation_id, repairDelete.operationId);
    const repairDeleting = { operation_id: repairDelete.operationId, phase: "deletion.catalog_deleting" };
    await testControl({ action: "arm", ...repairDeleting });
    exactHeld.push(repairDeleting);
    await release(repairHold);
    await testControl({ action: "acknowledged", ...repairDeleting });
    const repairPendingReceipt = (await ownerApi(surface, `/api/v1/session-catalog/operations?operation_id=${repairDelete.operationId}`)).data.items[0];
    assert.equal(repairPendingReceipt.settlement, "pending");
    const repairJournal = (await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [repairDelete.operationId]))[0];
    const dateDirectory = dirname(join(surface.workspace, ".boxteam", repairNode.storage_relative_locator));
    const originalMode = (await stat(dateDirectory)).mode & 0o7777;
    assert.ok(originalMode & 0o222, "真实日期桶在注入前必须可写");
    restoreDirectoryPermissions = () => chmod(dateDirectory, originalMode);
    // 只撤销真实 OS 目录权限；harness 不修改业务表、回执或删除状态。
    await chmod(dateDirectory, originalMode & ~0o222);
    await testControl({ action: "release", ...repairDeleting });
    let repairReceipt;
    await waitUntil(async () => {
      repairReceipt = (await ownerApi(surface, `/api/v1/session-catalog/operations?operation_id=${repairDelete.operationId}`)).data.items[0];
      return repairReceipt?.settlement === "repair_required";
    }, "真实目录隔离失败的 repair_required 回执");
    assert.equal(repairReceipt.state, "committed", "排空失败不能把已提交的删除伪装成拒绝");
    assert.match(repairReceipt.error_detail, /日期桶目录隔离失败|Permission denied/);
    assert.ok(repairReceipt.receipt_revision > repairPendingReceipt.receipt_revision);
    const repairStatus = surface.page.locator(".workbench-status-text").filter({ hasText: repairDelete.operationId });
    await repairStatus.filter({ hasText: /删除已提交但排空失败|需修复/ }).waitFor({ state: "visible", timeout: 30_000 });
    assert.match(await repairStatus.innerText(), /删除已提交但排空失败|需修复/);
    const repairDisk = (await browserCatalogOutbox(surface)).find((record) => record.operation.client_operation_id === repairDelete.operationId);
    assert.equal(repairDisk?.operation.state, "committed");
    assert.equal(repairDisk?.operation.settlement, "repair_required", "需要修复的原意图必须持久保留");
    assert.deepEqual(await readCatalog(surface, "SELECT state FROM nodes WHERE node_id=?", [repairOwner.sessionId]), [{ state: "deleting" }]);
    assert.ok(!(await ownerApi(surface, "/api/v1/session-catalog/export")).data.items.some((node) => node.node_id === repairOwner.sessionId));
    assert.equal(await catalogRow(surface, repairOwner.sessionId).count(), 0, "repair 不能将后代假回滚为可打开的 active 会话");
    await restoreDirectoryPermissions();
    restoreDirectoryPermissions = null;
    const repairReopen = await replaceNodePage(surface, []);
    const repairKilled = await testControl({ action: "process", name: surface.name, operation: "kill" });
    const repairRestarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    await repairReopen();
    await openCatalogWorkspace(surface);
    attachWire(surface.page);
    const repairedReceipt = await navigationTerminal(surface, repairDelete);
    assert.equal(repairedReceipt.settlement, "settled");
    assert.ok(repairedReceipt.receipt_revision > repairReceipt.receipt_revision);
    const repairedJournal = (await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [repairDelete.operationId]))[0];
    assert.equal(repairedJournal.state, "completed");
    assert.equal(repairedJournal.frozen_node_ids, repairJournal.frozen_node_ids);
    assert.equal((await readCatalog(surface, "SELECT COUNT(*) AS count FROM navigation_mutation_records WHERE operation_id=?", [repairDelete.operationId]))[0].count, 1);
    await waitUntil(async () => !(await browserCatalogOutbox(surface)).some((record) => record.operation.client_operation_id === repairDelete.operationId), "同一删除已修复并确认后清除 outbox");
    const repairDom = await settledDeletionDom(JSON.parse(repairJournal.frozen_node_ids).map((node) => node.node_id));
    facts.repair = { repairDelete, repairJournal, repairPendingReceipt, repairReceipt, repairDisk, repairKilled, repairRestarted, repairedReceipt, repairedJournal, repairDom };
    await Promise.all([...wireReads]);
    for (const receipt of facts.receipts.filter((entry) => entry.body)) {
      assert.equal(receipt.status, 202);
      assert.equal(receipt.requestId, receipt.body.request_id);
      assert.ok(receipt.body.data.receipts.every((entry) => receipt.operationIds.includes(entry.operation_id)));
    }
    assert.ok(facts.requests.every((request) => request.workspaceId === surface.gatewayWorkspaceId));
    return facts;
  } catch (error) {
    scenarioError = error;
    throw error;
  } finally {
    const cleanupErrors = [];
    const cleanup = async (releaseResource) => {
      try {
        await releaseResource();
      } catch (error) {
        cleanupErrors.push(error);
      }
    };
    if (restoreDirectoryPermissions) await cleanup(restoreDirectoryPermissions);
    if (eventTransport) {
      eventTransport.removeAllListeners("Fetch.requestPaused");
      await cleanup(() => Promise.all([...transportReads]));
      await cleanup(() => eventTransport.send("Fetch.disable"));
      await cleanup(() => eventTransport.detach());
    }
    for (const barrier of held) await cleanup(() => release(barrier));
    for (const barrier of exactHeld) await cleanup(() => testControl({ action: "release", ...barrier }));
    for (const { page, request, response } of wirePages) {
      page.off("request", request);
      page.off("response", response);
    }
    if (peerContext) {
      await cleanup(() => Promise.all(peerContext.pages().map(drainPageResponseBodies)));
      await cleanup(() => peerContext.close());
    }
    if (cleanupErrors.length > 0) {
      throw new AggregateError(scenarioError === undefined ? cleanupErrors : [scenarioError, ...cleanupErrors],
        "导航验收清理失败；保留场景和全部资源释放错误");
    }
  }
}

async function runNavigationDates(surfaces) {
  const surface = surfaces.a;
  const initialClock = await testControl({ action: "clock" });
  const held = [];
  let childPage;
  try {
    await testControl({ action: "identifier_date", utc: "2026-10-09T23:59:50+00:00" });
    const first = await createProductSession(surface, "跨日期目录中的活跃 Session");
    await testControl({ action: "identifier_date", utc: "2026-10-10T00:00:10+00:00" });
    const second = await createProductSession(surface, "下一 UTC 日期的 Session");
    const firstNode = await sessionCatalogNode(first);
    const secondNode = await sessionCatalogNode(second);
    assert.equal(firstNode.storage_relative_locator, `sessions/2026/10/09/${first.sessionId}`);
    assert.equal(secondNode.storage_relative_locator, `sessions/2026/10/10/${second.sessionId}`);
    assert.equal(Date.parse(first.dto.created_at), Date.parse(firstNode.created_at));
    assert.equal(Date.parse(second.dto.created_at), Date.parse(secondNode.created_at));
    const folders = [];
    for (const name of ["跨日期外层", "跨日期中层", "跨日期内层"]) {
      const created = await catalogMutation(surface, { kind: "create_folder", name, parent_node_id: folders.at(-1)?.nodeId ?? null });
      const nodeId = created.receipt.created_node_id;
      canonicalUuid7(nodeId, "ses");
      folders.push({ nodeId, name, ...created });
    }
    const [outer, middle, inner] = folders;
    const folderRows = await readCatalog(surface,
      "SELECT node_id,storage_relative_locator,created_at,main_thread_id FROM nodes WHERE kind='folder' AND node_id IN (?,?,?) ORDER BY node_id", folders.map((folder) => folder.nodeId));
    assert.equal(folderRows.length, 3);
    assert.ok(folderRows.every((row) => row.storage_relative_locator === null && row.created_at === null && row.main_thread_id === null));
    for (const folder of folders) {
      await assert.rejects(stat(`${surface.workspace}/.boxteam/sessions/${folder.nodeId}`), { code: "ENOENT" }, "Folder 只能是 SQLite 节点");
    }

    const childThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
    await planChatCalls(first.sessionId, childThread, [finalResponse("navigation-child-initial", "目录变化前的 child 初始化完成。")]);
    const creation = await completeOwnerTurn(first, "创建真实持久 child，用于活跃 main/child 目录移动。", [
      toolResponse("navigation-child-create", "task", { description: "读取 README.md 后等待用户直接对话。", subagent_type: "general-purpose" }),
      finalResponse("navigation-child-created", "持久 child 已创建。"),
      finalResponse("navigation-child-report", "初始化报告已处理。"),
    ]);
    await waitUntil(async () => {
      const summary = (await ownerApi(first, `/api/v1/sessions/${first.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === childThread);
      return ["completed", "completed_empty"].includes(summary?.report_status);
    }, "目录场景 child 初始化完成", 120_000);
    childPage = await browserStartup("navigation_child_page", () => surface.page.context().newPage());
    installPageDiagnostics(childPage);
    await ensureGuest(childPage, JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"))[surface.name].base_url);
    const child = { ...first, page: childPage, threadId: childThread, child: true };
    const fork = await forkOwner(first, { mode: "context_fork" });
    const forkNode = await sessionCatalogNode(fork.owner);
    const lineage = await readControl(first, "SELECT * FROM thread_creation_records WHERE child_thread_id=?", [childThread]);
    assert.equal(lineage.length, 1);
    const manifestPath = `${surface.workspace}/${sessionLocator(firstNode)}/session.json`;
    const manifestBefore = await readFile(manifestPath, "utf8");
    const active = [];
    for (const [owner, label] of [[first, "navigation-main"], [child, "navigation-child"]]) {
      const calls = await planChatCalls(owner.sessionId, owner.threadId, [
        toolResponse(`${label}-tool`, "read_file", { path: "README.md" }, "目录移动期间保持同一真实 execution。"),
        finalResponse(`${label}-final`, "目录移动后原工具循环完成。"),
      ]);
      const barrier = { operation_id: calls[0], phase: "model.response_ready" };
      await testControl({ action: "arm", ...barrier });
      held.push(barrier);
      await openOwner(owner);
      const sent = await sendOwnerPrompt(owner, `${label}：读取 README.md，移动目录不改变本轮身份。`);
      await testControl({ action: "acknowledged", ...barrier });
      const items = await readRollout(owner, "SELECT item_id,content_hash,item_sequence,execution_id,turn_id FROM item_catalog ORDER BY item_sequence");
      const assemblies = await readRollout(owner, "SELECT assembly_id,request_hash,plan_hash,status FROM context_assemblies ORDER BY assembly_id");
      active.push({ owner, calls, sent, barrier, items, assemblies, binding: await graphBinding(owner), request: await modelRequest(calls[0]) });
    }
    const mutations = [];
    mutations.push(await catalogMutation(surface, { kind: "move_node", target_node_id: first.sessionId, parent_node_id: inner.nodeId }));
    mutations.push(await catalogMutation(surface, { kind: "move_node", target_node_id: fork.owner.sessionId, parent_node_id: inner.nodeId }));
    mutations.push(await catalogMutation(surface, { kind: "rename_node", target_node_id: first.sessionId, name: "跨日期 Session 已改名" }));
    mutations.push(await catalogMutation(surface, { kind: "rename_node", target_node_id: middle.nodeId, name: "跨日期中层已改名" }));
    mutations.push(await catalogMutation(surface, { kind: "move_node", target_node_id: outer.nodeId, parent_node_id: second.sessionId }));
    const nested = (await ownerApi(first, `/api/v1/sessions/${first.sessionId}`)).data;
    assert.equal(nested.parent_session_id, second.sessionId, "parent_session_id 只投影最近的祖先 Session");
    const nestedBreadcrumb = (await ownerApi(first, `/api/v1/session-catalog/breadcrumb/${first.sessionId}`)).data;
    assert.deepEqual(nestedBreadcrumb.items.map((node) => node.node_id), [second.sessionId, outer.nodeId, middle.nodeId, inner.nodeId, first.sessionId]);
    mutations.push(await catalogMutation(surface, { kind: "move_node", target_node_id: outer.nodeId, parent_node_id: null }));
    const rooted = (await ownerApi(first, `/api/v1/sessions/${first.sessionId}`)).data;
    assert.equal(rooted.parent_session_id, null);
    const breadcrumb = (await ownerApi(first, `/api/v1/session-catalog/breadcrumb/${first.sessionId}`)).data;
    assert.deepEqual(breadcrumb.items.map((node) => node.node_id), [outer.nodeId, middle.nodeId, inner.nodeId, first.sessionId]);
    assert.equal(await readFile(manifestPath, "utf8"), manifestBefore, "导航变更不改 Session 物理 manifest");
    assert.deepEqual(await readControl(first, "SELECT * FROM thread_creation_records WHERE child_thread_id=?", [childThread]), lineage, "导航父链不改 delegation lineage");
    for (const [owner, frozen] of [[first, firstNode], [second, secondNode], [fork.owner, forkNode]]) {
      const current = await sessionCatalogNode(owner);
      for (const field of ["node_id", "workspace_id", "created_at", "storage_relative_locator", "main_thread_id"]) assert.equal(current[field], frozen[field]);
    }
    const forkLineage = (await ownerApi(fork.owner, `/api/v1/sessions/${fork.owner.sessionId}`)).data;
    assert.equal(forkLineage.kind, fork.owner.dto.kind);
    assert.equal(forkLineage.context_source_session_id, first.sessionId);
    for (const execution of active) {
      assert.deepEqual(await readRollout(execution.owner, "SELECT item_id,content_hash,item_sequence,execution_id,turn_id FROM item_catalog ORDER BY item_sequence"), execution.items);
      assert.deepEqual(await readRollout(execution.owner, "SELECT assembly_id,request_hash,plan_hash,status FROM context_assemblies ORDER BY assembly_id"), execution.assemblies);
      assert.deepEqual(await graphBinding(execution.owner), execution.binding);
      // 此时模型响应仍被 barrier 阻塞；历史跳转在下方 Turn 完成后由 verifyOwnerTurns 验证。
      const current = await assertAcceptedTurn(execution.owner, execution.sent);
      assert.equal(current.turn_id, execution.sent.turnId, "目录变化保留运行中的 Turn 身份");
    }
    for (const execution of active) await testControl({ action: "release", ...execution.barrier });
    const completed = [];
    for (const execution of active) {
      await waitForJobStatus(execution.owner, execution.sent.jobId);
      await waitForPlannedExecutions(execution.owner, execution.calls);
      const next = await modelRequest(execution.calls[1]);
      const prefix = (request) => JSON.stringify(request.request.messages.filter((message) => message.role === "system"));
      assert.equal(prefix(next), prefix(execution.request), "活跃 main/child 的导航变化不漂移 stable prefix bytes");
      completed.push({ sent: execution.sent, calls: execution.calls, turn: await assertAcceptedTurn(execution.owner, execution.sent), evidence: await verifyOwnerTurns(execution.owner) });
    }
    mutations.push(await catalogMutation(surface, { kind: "move_node", target_node_id: second.sessionId, parent_node_id: outer.nodeId }));
    const beforeReject = await readCatalog(surface, "SELECT * FROM nodes WHERE node_id IN (?,?,?,?,?,?) ORDER BY node_id", [...folders.map((folder) => folder.nodeId), first.sessionId, second.sessionId, fork.owner.sessionId]);
    const nonRecursive = await catalogMutation(surface, { kind: "delete_folder", target_node_id: outer.nodeId, recursive: false }, "rejected");
    assert.match(nonRecursive.receipt.error_detail, /非空|non.empty|包含/i);
    const cycle = await catalogMutation(surface, { kind: "move_node", target_node_id: outer.nodeId, parent_node_id: inner.nodeId }, "rejected");
    assert.match(cycle.receipt.error_detail, /循环/);
    const foreign = await createProductSession(surfaces.b, "跨工作区 parent 拒绝");
    const foreignBefore = await sessionCatalogNode(foreign);
    const crossWorkspace = await catalogMutation(surface, { kind: "move_node", target_node_id: first.sessionId, parent_node_id: foreign.sessionId }, "rejected");
    assert.equal(crossWorkspace.receipt.error_code, "node_not_found");
    assert.deepEqual(await sessionCatalogNode(foreign), foreignBefore);
    assert.deepEqual(await readCatalog(surface, "SELECT * FROM nodes WHERE node_id IN (?,?,?,?,?,?) ORDER BY node_id", beforeReject.map((node) => node.node_id)), beforeReject, "拒绝不得留下部分父链变更");

    const deletion = await prepareNavigationIntent(surface, { kind: "delete_folder", target_node_id: outer.nodeId, recursive: true });
    const deleting = { operation_id: deletion.operationId, phase: "deletion.catalog_deleting" };
    const renamed = { operation_id: deletion.operationId, phase: "deletion.renamed/" + first.sessionId };
    for (const barrier of [deleting, renamed]) {
      await testControl({ action: "arm", ...barrier });
      held.push(barrier);
    }
    await enqueueNavigationIntent(surface, deletion);
    await testControl({ action: "acknowledged", ...deleting });
    const pending = await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]);
    assert.equal(pending.length, 1);
    const frozen = pending[0];
    const frozenIds = JSON.parse(frozen.frozen_node_ids).map((node) => node.node_id);
    assert.deepEqual([...frozenIds].sort(), beforeReject.map((node) => node.node_id).sort());
    assert.deepEqual(JSON.parse(frozen.frozen_session_locators), [[first.sessionId, firstNode.storage_relative_locator], [second.sessionId, secondNode.storage_relative_locator], [fork.owner.sessionId, forkNode.storage_relative_locator]].sort(([left], [right]) => left.localeCompare(right)).map(([session_id, storage_relative_locator]) => ({ session_id, storage_relative_locator })));
    const deletingRows = await readCatalog(surface, "SELECT node_id,state FROM nodes WHERE node_id IN (?,?,?,?,?,?)", frozenIds);
    assert.equal(deletingRows.length, frozenIds.length);
    assert.ok(deletingRows.every((node) => node.state === "deleting"));
    for (const owner of [first, second, fork.owner]) {
      // 删除中只观察 catalog；Session 详情同样受业务读取准入约束。
      const metadata = await readCatalog(owner,
        "SELECT node_id,state,main_thread_id FROM nodes WHERE node_id=?", [owner.sessionId]);
      assert.deepEqual(metadata, [{ node_id: owner.sessionId, state: "deleting", main_thread_id: owner.threadId }],
        "deleting 的 catalog metadata 仍可定点观察");
      const refused = await ownerApiResponse(owner, `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction: "tail", turns: 1, include: HISTORY_INCLUDE }),
      });
      assert.equal(refused.status, 409);
      assertSessionDeletionPending(refused.body, owner.sessionId);
    }
    const exported = (await ownerApi(surface, "/api/v1/session-catalog/export")).data;
    assert.ok(!exported.items.some((node) => frozenIds.includes(node.node_id) && node.state === "active"));
    await waitUntil(async () => (await surface.page.locator(`[data-session-id="${first.sessionId}"]`).count()) === 0, "整个 deleting 子树从真实 DOM 隐藏");
    await testControl({ action: "release", ...deleting });
    await testControl({ action: "acknowledged", ...renamed });
    assert.deepEqual(await readDeletedControl(first, deletion, "SELECT state,generation FROM lifecycle_fence WHERE id=1"), [{ state: "deleting", generation: 2 }]);
    assert.deepEqual(await readFrozenControl(second, secondNode, "SELECT state,generation FROM lifecycle_fence WHERE id=1"), [{ state: "active", generation: 1 }]);
    await assert.rejects(stat(`${surface.workspace}/${sessionLocator(firstNode)}`), { code: "ENOENT" });
    assert.ok((await stat(`${surface.workspace}/${sessionLocator(secondNode)}`)).isDirectory());
    await drainPageResponseBodies(childPage);
    await childPage.close();
    const reopen = await replaceNodePage(surface, [first, second, fork.owner]);
    const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await testControl({ action: "release", ...renamed });
    const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    await reopen();
    const terminal = await navigationTerminal(surface, deletion);
    const recovered = (await readCatalog(surface, "SELECT * FROM subtree_delete_records WHERE subtree_delete_idempotency_key=?", [deletion.operationId]))[0];
    assert.equal(recovered.state, "completed");
    for (const field of ["subtree_delete_idempotency_key", "root_node_id", "frozen_node_ids", "frozen_session_locators", "record_created_at"]) assert.equal(recovered[field], frozen[field]);
    assert.deepEqual(JSON.parse(recovered.drained_session_ids).sort(), [first.sessionId, second.sessionId, fork.owner.sessionId].sort());
    assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id IN (?,?,?,?,?,?)", frozenIds), []);
    assert.equal((await readCatalog(surface, "SELECT COUNT(*) AS count FROM subtree_delete_records WHERE root_node_id=?", [outer.nodeId]))[0].count, 1, "重启必须恢复同一删除 batch");
    for (const owner of [first, second, fork.owner]) {
      const inaccessible = await ownerApiResponse(owner, `/api/v1/sessions/${owner.sessionId}/main-thread`);
      assert.equal(inaccessible.status, 404);
      assert.equal(inaccessible.body.detail, `Session ${owner.sessionId} not found`);
    }
    return { first: firstNode, second: secondNode, fork: forkNode, folderRows, creation, lineage, nestedBreadcrumb, breadcrumb, completed, mutations, nonRecursive, cycle, crossWorkspace, deletion, frozen, killed, restarted, terminal, recovered };
  } finally {
    for (const barrier of held) await testControl({ action: "release", ...barrier });
    if (childPage) {
      await drainPageResponseBodies(childPage);
      await childPage.close();
    }
    await testControl({ action: "identifier_date", utc: initialClock.wait_utc });
  }
}

function assertForkSourceOwner(uri, source, localGatewayId = source.gatewayId) {
  const match = /^boxteam:\/\/(?:(?<gateway>[^/]+)\/)?workspace\/(?<workspace>[^/]+)\/resources\/session\/(?<session>[^/]+)\/threads\/(?<thread>[^/]+)$/.exec(uri);
  assert.ok(match, "fork 来源必须是显式 Session/Thread URI");
  const { gateway, workspace, session, thread } = match.groups;
  assert.deepEqual([gateway ?? localGatewayId, workspace, session, thread],
    [source.gatewayId, source.workspaceIdentity, source.sessionId, source.threadId],
    "local URI 按请求 Gateway 解析后必须仍是冻结的真实 source owner");
}

function workspaceSessionInventoryUri(surface) {
  const sessionPlaceholder = "/__SESSION__";
  assert.ok(surface.uriTemplate.endsWith(sessionPlaceholder), "workspace Session URI 模板必须指向具体 Session");
  return surface.uriTemplate.slice(0, -sessionPlaceholder.length);
}

async function freezeForkRequest(source, {
  mode = "full_rollout_copy", relationship = "detached", creationKind = "new_session_main",
  anchorMode = "inclusive", turnId, resources = [],
} = {}, targetSurface = source) {
  const query = new URLSearchParams({ thread_id: source.threadId, mode, anchor_mode: anchorMode });
  if (turnId) {
    const history = await loadCompleteThreadHistory(source);
    const displayed = history.items.find((turn) => turn.turn_id === turnId);
    assert.ok(displayed, "Turn 点击必须来自实际 history 读取");
    query.set("turn_id", turnId);
    query.set("source_view_id", displayed.source_view_id);
    query.set("source_view_revision", String(displayed.source_view_revision));
  }
  const point = (await ownerApi(source, `/api/v1/sessions/${source.sessionId}/fork-source-point?${query}`)).data;
  assertForkSourceOwner(point.source_uri, source);
  assert.equal(point.resource_mode, mode);
  assert.equal(point.resource_anchor_mode, anchorMode);
  return {
    idempotency_key: await testControl({ action: "identifier", namespace: "fork/product-request" }),
    source_uri: `${source.resource.uri}/threads/${source.threadId}`,
    source_view_id: point.source_view_id, source_view_revision: point.source_view_revision,
    anchor_item_id: mode === "view_prefix_fork" ? point.anchor_item_id : null,
    anchor_mode: mode === "view_prefix_fork" ? anchorMode : "inclusive", mode,
    target_container_uri: creationKind === "new_child_thread" ? targetSurface.resource.uri
      : workspaceSessionInventoryUri(targetSurface),
    creation_kind: creationKind, relationship, resources,
  };
}

function forkOperationId(request) {
  return "fork-" + createHash("sha256").update(request.idempotency_key).digest("hex");
}

async function closeForkPeerContexts(contexts, primaryError, hasPrimaryError) {
  const closeErrors = [];
  for (const peerContext of [...contexts].reverse()) {
    for (const cleanup of [
      () => Promise.all(peerContext.pages().map(drainPageResponseBodies)),
      () => peerContext.close(),
    ]) {
      try {
        await cleanup();
      } catch (error) {
        closeErrors.push(error);
      }
    }
  }
  if (closeErrors.length === 0) return;

  const cleanupMessage = `fork peer context 关闭失败: ${closeErrors.map(String).join("; ")}`;
  if (hasPrimaryError && primaryError instanceof Error) {
    primaryError.message = `${primaryError.message}\n${cleanupMessage}`;
    return;
  }
  if (hasPrimaryError) {
    throw new AggregateError([primaryError, ...closeErrors], `${String(primaryError)}\n${cleanupMessage}`, {
      cause: primaryError,
    });
  }
  if (closeErrors.length === 1) throw closeErrors[0];
  throw new AggregateError(closeErrors, cleanupMessage);
}

async function runForkPeerContextCleanupSelfTest() {
  const primaryError = new Error("fork operation deadline exceeded");
  const closeErrors = [new Error("peer C already closed"), new Error("peer A close failed")];
  const closeOrder = [];
  const contexts = ["A", "B", "C"].map((name) => ({
    pages: () => [],
    async close() {
      closeOrder.push(name);
      if (name === "C") throw closeErrors[0];
      if (name === "A") throw closeErrors[1];
    },
  }));

  await assert.rejects(async () => {
    let capturedError;
    let hasPrimaryError = false;
    try {
      throw primaryError;
    } catch (error) {
      capturedError = error;
      hasPrimaryError = true;
      throw error;
    } finally {
      await closeForkPeerContexts(contexts, capturedError, hasPrimaryError);
    }
  }, (error) => error === primaryError
    && error.message.startsWith("fork operation deadline exceeded")
    && error.message.includes(String(closeErrors[0]))
    && error.message.includes(String(closeErrors[1])));
  assert.deepEqual(closeOrder, ["C", "B", "A"], "单个关闭失败后仍必须尝试关闭剩余 Context");

  const standaloneCloseError = new Error("peer context close failed");
  await assert.rejects(
    closeForkPeerContexts([{ pages: () => [], close: async () => { throw standaloneCloseError; } }], undefined, false),
    (error) => error === standaloneCloseError,
  );
}

async function forkDeadline(request, source, targetSurface, stage = "fork_context POST") {
  assert.equal(typeof request.idempotency_key, "string");
  let deadline = forkOperationDeadlines.get(request.idempotency_key);
  if (!deadline) {
    deadline = await beginForkOperationDeadline({
      target: `${targetSurface.gatewayId}/${targetSurface.workspaceIdentity}/${source.sessionId}`,
      operationKey: request.idempotency_key,
      timeoutMs: FORK_OPERATION_DEADLINE_MS,
    });
    deadline.sourceSessionId = source.sessionId;
    deadline.targetWorkspace = targetSurface.workspaceIdentity;
    forkOperationDeadlines.set(request.idempotency_key, deadline);
  }
  await deadline.setStage(stage);
  return deadline;
}

function forkDeadlineError(deadline) {
  return deadline.error();
}

function waitForForkBudget(awaitable, deadline, stage) {
  return waitForForkOperation(awaitable, deadline, stage);
}

function forkHistoryValidationBudget(timeoutMs = FORK_HISTORY_VALIDATION_TIMEOUT_MS) {
  const expiresAt = performance.now() + timeoutMs;
  return {
    remainingMs() {
      return Math.max(0, expiresAt - performance.now());
    },
    async wait(awaitable, stage) {
      const remainingMs = this.remainingMs();
      if (remainingMs <= 0) throw new Error(`fork durable history validation deadline exceeded: stage=${stage}`);
      let timer;
      const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(
          `fork durable history validation deadline exceeded: stage=${stage}, timeout_ms=${timeoutMs}`,
        )), remainingMs);
      });
      try {
        return await Promise.race([awaitable, timeout]);
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

async function readForkDurableHistory(targetSurface, payload, deadline) {
  const validationBudget = !deadline || deadline.backendCompleted ? forkHistoryValidationBudget() : null;
  const wait = validationBudget
    ? (awaitable, stage) => validationBudget.wait(awaitable, stage)
    : (awaitable, stage) => waitForForkBudget(awaitable, deadline, stage);
  const fork = payload?.data;
  const sessionId = fork?.session?.session_id;
  const threadId = fork?.thread_id;
  assert.equal(typeof sessionId, "string", "fork 响应必须返回持久目标 Session");
  assert.equal(typeof threadId, "string", "fork 响应必须返回持久目标 Thread");
  const token = await wait(localToken(targetSurface.page), "读取 fork Thread 持久历史凭据");
  const response = await wait(targetSurface.page.request.fetch(new URL(
    `/api/v1/sessions/${sessionId}/threads/${threadId}/history`, targetSurface.page.url()).toString(), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Local-Token": token,
      "X-BoxTeam-Workspace-Id": targetSurface.gatewayWorkspaceId },
    data: JSON.stringify({ direction: "tail", turns: 256 }),
    timeout: validationBudget ? validationBudget.remainingMs() : 0,
  }), "读取 fork Thread 持久历史");
  try {
    const text = await wait(response.text(), "读取 fork Thread 持久历史响应正文");
    if (!response.ok()) throw new Error(`fork Thread 持久历史读取失败 ${response.status()}: ${text}`);
    const history = text ? JSON.parse(text) : null;
    const requestId = response.headers()["x-request-id"];
    assert.ok(requestId, "fork Thread 历史响应缺少 X-Request-ID");
    assert.equal(history?.request_id, requestId, "fork Thread 历史响应体与 X-Request-ID 不一致");
    assert.equal(history.data.thread_id, threadId, "真实历史读取必须落在刚发布的 fork Thread owner");
    assert.equal(typeof history.data.source_view_id, "string", "fork Thread 历史必须返回实际读取 view");
    assert.ok(Number.isSafeInteger(history.data.source_view_revision), "fork Thread 历史必须返回实际读取 revision");
    const proof = { sessionId, threadId, sourceViewId: history.data.source_view_id,
      sourceViewRevision: history.data.source_view_revision, turnCount: history.data.items.length };
    result.forkDurabilityProofs.push(proof);
    return proof;
  } finally {
    await response.dispose();
  }
}

function liveForkResources(payload) {
  return (Array.isArray(payload?.data?.resources) ? payload.data.resources : [])
    .filter((resource) => resource.owner_reference && resource.action !== "omit");
}

function assertForkResponseRequestId(response, payload, description) {
  const requestId = response.headers()["x-request-id"];
  assert.ok(requestId, `${description} 缺少 X-Request-ID`);
  assert.equal(payload?.request_id, requestId, `${description} 响应体与 X-Request-ID 不一致`);
  return requestId;
}

function forkContainerUriIdentity(uri) {
  const match = typeof uri === "string"
    && /^boxteam:\/\/(?:(?<gateway>[^/]+)\/)?workspace\/(?<workspace>[^/]+)\/resources\/(?<kind>session|folder)(?:\/(?<container>[^/]+)(?:\/threads\/(?<thread>[^/]+))?)?$/.exec(uri);
  if (!match) return null;
  return { gateway: match.groups.gateway ?? null, workspace: match.groups.workspace,
    kind: match.groups.kind, container: match.groups.container ?? null, thread: match.groups.thread ?? null };
}

function forkUriGatewayIdentity(identity, localGatewayId) {
  return identity.gateway ?? localGatewayId ?? null;
}

function sameForkUriAuthority(identities, localGatewayId) {
  const [first, ...rest] = identities;
  return identities.every((identity) => identity)
    && rest.every((identity) => forkUriGatewayIdentity(identity, localGatewayId)
      === forkUriGatewayIdentity(first, localGatewayId));
}

function sameForkContainerUriIdentity(leftUri, rightUri, localGatewayId) {
  const left = forkContainerUriIdentity(leftUri);
  const right = forkContainerUriIdentity(rightUri);
  return sameForkUriAuthority([left, right], localGatewayId)
    && left.workspace === right.workspace && left.kind === right.kind
    && left.container === right.container && left.thread === right.thread;
}

function forkResponseAnchorMatchesFrozenRequest(fork, request) {
  // context_fork 的 null 代表由来源 owner 按冻结 view 解析安全截止点。
  if (request?.mode === "context_fork" && request.anchor_item_id == null) {
    return Object.hasOwn(fork, "anchor_item_id")
      && (fork.anchor_item_id === null
        || typeof fork.anchor_item_id === "string" && fork.anchor_item_id.length > 0);
  }
  return (fork?.anchor_item_id ?? null) === (request?.anchor_item_id ?? null);
}

function forkResponseTargetsFrozenRequest(payload, request, localGatewayId) {
  const fork = payload?.data;
  const requestedTarget = forkContainerUriIdentity(request?.target_container_uri);
  const sessionUri = forkContainerUriIdentity(fork?.session?.uri);
  const threadUri = forkContainerUriIdentity(fork?.thread_uri);
  if (!requestedTarget || !sessionUri || !threadUri
      || fork.creation_kind !== request.creation_kind || fork.mode !== request.mode
      || !sameForkContainerUriIdentity(request.source_uri, fork.source_uri, localGatewayId)
      || fork.source_view_id !== request.source_view_id
      || fork.source_view_revision !== request.source_view_revision
      || !forkResponseAnchorMatchesFrozenRequest(fork, request)
      || typeof fork.session?.session_id !== "string" || typeof fork.session?.workspace_id !== "string"
      || typeof fork.thread_id !== "string") return false;
  if (requestedTarget.workspace !== fork.session.workspace_id
      || requestedTarget.workspace !== sessionUri.workspace || requestedTarget.workspace !== threadUri.workspace
      || sessionUri.kind !== "session" || sessionUri.container !== fork.session.session_id || sessionUri.thread !== null
      || threadUri.kind !== "session" || threadUri.container !== fork.session.session_id
      || threadUri.thread !== fork.thread_id
      || threadUri.gateway !== (requestedTarget.gateway ?? localGatewayId ?? null)
      || (sessionUri.gateway !== null && sessionUri.gateway !== threadUri.gateway)) return false;
  if (request.creation_kind === "new_child_thread") {
    return requestedTarget.kind === "session" && requestedTarget.container !== null
      && fork.session.session_id === requestedTarget.container;
  }
  if (request.creation_kind !== "new_session_main") return false;
  if (requestedTarget.kind === "session" && requestedTarget.container !== null) {
    return requestedTarget.thread === null && fork.session.session_id !== requestedTarget.container
      && fork.session.parent_session_id === requestedTarget.container;
  }
  return true;
}

function sameForkOwnerReference(left, right) {
  return left?.kind === right?.kind && left?.resource_id === right?.resource_id
    && left?.workspace_id === right?.workspace_id && left?.expected_revision === right?.expected_revision;
}

function hasTerminalForkOwnerReceipt(payload, request, localGatewayId) {
  if (!forkResponseTargetsFrozenRequest(payload, request, localGatewayId)) return false;
  const requested = Array.isArray(request.resources) ? request.resources.filter((resource) => resource.owner_reference) : [];
  const receipts = Array.isArray(payload.data.resources)
    ? payload.data.resources.filter((resource) => resource.owner_reference) : [];
  if (receipts.length !== requested.length) return false;
  return requested.every((choice) => {
    const matches = receipts.filter((receipt) => receipt.action === choice.action
      && sameForkOwnerReference(receipt.owner_reference, choice.owner_reference));
    if (matches.length !== 1) return false;
    const [receipt] = matches;
    return choice.action === "omit" ? receipt.operation_state === "not_dispatched"
      : ["committed", "rejected"].includes(receipt.operation_state);
  });
}

async function finishForkDeadline(deadline) {
  await deadline.complete();
  deadline.backendCompleted = true;
  forkOperationDeadlines.delete(deadline.operationKey);
}

async function remainingForkDeadlineMs(deadline) {
  return deadline.backendCompleted ? null : deadline.remainingMs();
}

function isTerminalForkRejection(status, payload, request) {
  if (status < 400 || status >= 500) return false;
  const selected = Array.isArray(request?.resources)
    ? request.resources.filter((resource) => resource.owner_reference && resource.action !== "omit") : [];
  if (selected.length > 0) {
    return status === 400 && typeof payload?.detail === "string"
      && payload.detail.startsWith("fork_live_resource_rejected:");
  }
  return liveForkResources(payload).every((resource) => resource.operation_state === "not_dispatched");
}

async function classifyForkResponse(deadline, status, payload, request, localGatewayId) {
  if (status >= 500 || status < 200 || status >= 300 && status < 400) return "unknown";
  if (status >= 400) {
    if (isTerminalForkRejection(status, payload, request)) {
      await finishForkDeadline(deadline);
      return "completed";
    }
    return "known";
  }

  if (hasTerminalForkOwnerReceipt(payload, request, localGatewayId)) {
    await finishForkDeadline(deadline);
    return "completed";
  }
  return "pending";
}

async function runForkDeadlineClassifierSelfTest() {
  const localGatewayId = "gateway-local";
  const testDeadlines = [];
  await assertForkDeadlineCloseSemanticsForTest();

  const createDeadline = (operationKey) => {
    const deadline = { operationKey, completed: 0, remainingReads: 0,
      async complete() { this.completed += 1; },
      async remainingMs() { this.remainingReads += 1; return 12_345; } };
    forkOperationDeadlines.set(operationKey, deadline);
    testDeadlines.push(deadline);
    return deadline;
  };
  const request = { idempotency_key: "first-partial",
    source_uri: "boxteam://workspace/ws-source/resources/session/ses-source/threads/thr-source",
    source_view_id: "view-source", source_view_revision: 7, anchor_item_id: null, mode: "full_rollout_copy",
    creation_kind: "new_session_main", target_container_uri: "boxteam://workspace/ws-target/resources/session",
    resources: [] };
  const makePayload = (resources = [], mode = request.mode, anchorItemId = request.anchor_item_id) => ({ data: {
    session: { session_id: "ses-target", workspace_id: "ws-target",
      uri: "boxteam://workspace/ws-target/resources/session/ses-target", parent_session_id: null },
    thread_id: "thr-target", thread_uri: `boxteam://${localGatewayId}/workspace/ws-target/resources/session/ses-target/threads/thr-target`,
    creation_kind: request.creation_kind,
    source_uri: `boxteam://${localGatewayId}/workspace/ws-source/resources/session/ses-source/threads/thr-source`,
    source_view_id: request.source_view_id,
    source_view_revision: request.source_view_revision, anchor_item_id: anchorItemId, mode, resources,
  } });
  const rejectedResource = { owner_reference: { kind: "terminal", resource_id: "term-1",
    workspace_id: "ws-target", expected_revision: 4 },
    action: "reuse_reference", operation_state: "rejected", availability: "unavailable" };
  const committedResource = { owner_reference: { kind: "browser", resource_id: "browser-1",
    workspace_id: "ws-target", expected_revision: 9 },
    action: "reuse_reference", operation_state: "committed", availability: "available" };

  const rejected = createDeadline("rejected-4xx");
  assert.equal(await classifyForkResponse(rejected, 422, { request_id: "req-4xx", detail: "invalid request" }, null, localGatewayId), "completed");
  assert.equal(rejected.completed, 1, "无 live resource 的明确 4xx 必须结束预算");

  const resourceRequest = { ...request, resources: [
    { owner_reference: rejectedResource.owner_reference, action: "reuse_reference" },
  ] };
  const resourceRejection = createDeadline("selected-resource-rejection");
  assert.equal(await classifyForkResponse(resourceRejection, 400,
    { request_id: "req-resource-rejected", detail: "fork_live_resource_rejected: target mismatch" },
    resourceRequest, localGatewayId), "completed");
  assert.equal(resourceRejection.completed, 1, "后端明确拒绝已选 live resource 时必须结束原预算");
  assert.equal(await remainingForkDeadlineMs(resourceRejection), null,
    "明确的 live-resource 前置拒绝结束后不再读取 inactive deadline");
  assert.equal(forkOperationDeadlines.has(resourceRejection.operationKey), false,
    "明确的 live-resource 前置拒绝必须移除对应 watchdog token");

  const unclassifiedResourceFailure = createDeadline("unclassified-selected-resource-4xx");
  assert.equal(await classifyForkResponse(unclassifiedResourceFailure, 400,
    { request_id: "req-resource-unknown", detail: "fork_live_revision_conflict: revision changed" },
    resourceRequest, localGatewayId), "known");
  assert.equal(unclassifiedResourceFailure.completed, 0,
    "其它带资源的 4xx 不能仅凭状态码结束预算");
  assert.equal(await remainingForkDeadlineMs(unclassifiedResourceFailure), 12_345,
    "其它带资源的 4xx 仍保留活动 deadline");
  assert.equal(forkOperationDeadlines.has(unclassifiedResourceFailure.operationKey), true,
    "未识别的资源拒绝不能清除 watchdog token");

  const wrongStatusResourceRejection = createDeadline("wrong-status-resource-rejection");
  assert.equal(await classifyForkResponse(wrongStatusResourceRejection, 422,
    { request_id: "req-resource-wrong-status", detail: "fork_live_resource_rejected: target mismatch" },
    resourceRequest, localGatewayId), "known");
  assert.equal(wrongStatusResourceRejection.completed, 0,
    "只有后端约定的 HTTP 400 才能结算 live-resource 前置拒绝");
  assert.equal(await remainingForkDeadlineMs(wrongStatusResourceRejection), 12_345,
    "相同错误前缀但不是 HTTP 400 时仍保留活动 deadline");
  assert.equal(forkOperationDeadlines.has(wrongStatusResourceRejection.operationKey), true,
    "相同错误前缀但不是 HTTP 400 时不能清除 watchdog token");

  const serverFailure = createDeadline("server-5xx");
  assert.equal(await classifyForkResponse(serverFailure, 503, { request_id: "req-5xx" }, null, localGatewayId), "unknown");
  assert.equal(serverFailure.completed, 0, "5xx 不得结束预算");

  const firstPartial = createDeadline("first-partial");
  request.resources = [
    { owner_reference: rejectedResource.owner_reference, action: "reuse_reference" },
    { owner_reference: committedResource.owner_reference, action: "reuse_reference" },
  ];
  const firstPartialPayload = makePayload([rejectedResource, committedResource]);
  assert.equal(await classifyForkResponse(firstPartial, 200, firstPartialPayload, request, localGatewayId), "completed");
  assert.equal(firstPartial.completed, 1, "目标匹配且所有 live owner receipt 已提交或拒绝时结束首次 deadline");
  assert.equal(await remainingForkDeadlineMs(firstPartial), null,
    "终态回执结束原预算后，不再向 watchdog 查询 inactive token");
  assert.equal(firstPartial.remainingReads, 0, "终态预算不再读取 active 剩余时间");
  assert.equal(forkOperationDeadlines.has(request.idempotency_key), false);

  const contextForkRequest = { ...request, idempotency_key: "context-fork-safe-anchor",
    mode: "context_fork", anchor_item_id: null, resources: [] };
  const contextForkPayload = makePayload([], "context_fork", "item-safe-context-cutoff");
  const resolvedSafeAnchor = createDeadline(contextForkRequest.idempotency_key);
  assert.equal(await classifyForkResponse(resolvedSafeAnchor, 200, contextForkPayload,
    contextForkRequest, localGatewayId), "completed",
  "context_fork 未指定锚点时允许来源 owner 返回冻结 view 的规范安全截止 item");
  assert.equal(resolvedSafeAnchor.completed, 1,
    "规范安全截止 item 与其它冻结来源/目标身份匹配后结束原 deadline");

  const emptyContextAnchor = createDeadline("context-fork-empty-source-anchor");
  assert.equal(await classifyForkResponse(emptyContextAnchor, 200,
    makePayload([], "context_fork", null), contextForkRequest, localGatewayId), "completed",
  "来源 owner 对没有安全截止 item 的冻结 view 可以返回显式 null");
  assert.equal(emptyContextAnchor.completed, 1);

  const missingResolvedAnchor = createDeadline("context-fork-missing-safe-anchor");
  const missingResolvedAnchorPayload = structuredClone(contextForkPayload);
  delete missingResolvedAnchorPayload.data.anchor_item_id;
  assert.equal(await classifyForkResponse(missingResolvedAnchor, 200, missingResolvedAnchorPayload,
    contextForkRequest, localGatewayId), "pending",
  "未返回 context_fork anchor 字段不能冒充来源 owner 已解析安全截止点");
  assert.equal(missingResolvedAnchor.completed, 0);

  const invalidResolvedAnchor = createDeadline("context-fork-empty-safe-anchor");
  const invalidResolvedAnchorPayload = structuredClone(contextForkPayload);
  invalidResolvedAnchorPayload.data.anchor_item_id = "";
  assert.equal(await classifyForkResponse(invalidResolvedAnchor, 200, invalidResolvedAnchorPayload,
    contextForkRequest, localGatewayId), "pending",
  "空字符串不能作为来源 owner 解析出的安全截止 item");
  assert.equal(invalidResolvedAnchor.completed, 0);

  const explicitContextAnchorRequest = { ...contextForkRequest,
    idempotency_key: "context-fork-explicit-anchor", anchor_item_id: "item-context-requested-anchor" };
  const explicitContextAnchorMismatch = createDeadline(explicitContextAnchorRequest.idempotency_key);
  assert.equal(await classifyForkResponse(explicitContextAnchorMismatch, 200,
    makePayload([], "context_fork", "item-context-other-anchor"),
    explicitContextAnchorRequest, localGatewayId), "pending",
  "context_fork 显式请求 anchor 时，响应中的不同 ID 仍必须保持 pending");
  assert.equal(explicitContextAnchorMismatch.completed, 0);

  const explicitContextAnchorMatch = createDeadline("context-fork-explicit-anchor-match");
  assert.equal(await classifyForkResponse(explicitContextAnchorMatch, 200,
    makePayload([], "context_fork", "item-context-requested-anchor"),
    explicitContextAnchorRequest, localGatewayId), "completed");
  assert.equal(explicitContextAnchorMatch.completed, 1);

  const contextSourceViewMismatch = createDeadline("context-fork-source-view-mismatch");
  const contextSourceViewMismatchPayload = structuredClone(contextForkPayload);
  contextSourceViewMismatchPayload.data.source_view_id = "view-drifted";
  assert.equal(await classifyForkResponse(contextSourceViewMismatch, 200, contextSourceViewMismatchPayload,
    contextForkRequest, localGatewayId), "pending",
  "解析安全 anchor 不能掩盖来源冻结 view 身份漂移");
  assert.equal(contextSourceViewMismatch.completed, 0);

  const contextTargetMismatch = createDeadline("context-fork-target-mismatch");
  const contextTargetMismatchPayload = structuredClone(contextForkPayload);
  contextTargetMismatchPayload.data.thread_uri =
    "boxteam://gateway/workspace/ws-other/resources/session/ses-target/threads/thr-target";
  assert.equal(await classifyForkResponse(contextTargetMismatch, 200, contextTargetMismatchPayload,
    contextForkRequest, localGatewayId), "pending",
  "解析安全 anchor 不能掩盖冻结目标 Gateway/workspace 漂移");
  assert.equal(contextTargetMismatch.completed, 0);

  const explicitAnchorMismatch = createDeadline("explicit-anchor-mismatch");
  const explicitAnchorRequest = { ...request, idempotency_key: "explicit-anchor-request",
    mode: "view_prefix_fork", anchor_item_id: "item-requested-anchor", resources: [] };
  const explicitAnchorPayload = makePayload([], "view_prefix_fork", "item-other-anchor");
  assert.equal(await classifyForkResponse(explicitAnchorMismatch, 200, explicitAnchorPayload,
    explicitAnchorRequest, localGatewayId), "pending",
  "显式请求 anchor 仍必须与服务端返回 anchor 完全一致");
  assert.equal(explicitAnchorMismatch.completed, 0);

  const retry = createDeadline(request.idempotency_key);
  const replayable = makePayload([
    { ...rejectedResource, operation_state: "dispatching" }, committedResource,
  ]);
  assert.equal(await classifyForkResponse(retry, 202, replayable, request, localGatewayId), "pending");
  assert.equal(retry.completed, 0, "HTTP 202 与 dispatching owner receipt 不得结束新重试的 deadline");
  assert.equal(await remainingForkDeadlineMs(retry), 12_345,
    "HTTP 202 pending receipt 仍必须读取且保留 active 操作预算");
  assert.equal(retry.remainingReads, 1);
  assert.equal(forkOperationDeadlines.has(retry.operationKey), true,
    "HTTP 202 pending receipt 必须保留 watchdog token");
  const notDispatched = makePayload([
    { ...rejectedResource, operation_state: "not_dispatched" }, committedResource,
  ]);
  assert.equal(await classifyForkResponse(retry, 200, notDispatched, request, localGatewayId), "pending");
  assert.equal(retry.completed, 0, "not_dispatched owner receipt 不得结束新重试的 deadline");
  assert.equal(await classifyForkResponse(retry, 503, firstPartialPayload, request, localGatewayId), "unknown");
  assert.equal(retry.completed, 0, "partial retry 的 5xx 不得结束预算");

  assert.equal(await classifyForkResponse(retry, 200, firstPartialPayload, request, localGatewayId), "completed");
  assert.equal(retry.completed, 1, "后续同 key 重试使用新 deadline，并凭 rejected terminal receipt 结束");
  assert.equal(await remainingForkDeadlineMs(retry), null,
    "后续同 key 重试收到终态回执后也不再查询 inactive deadline");
  assert.equal(retry.remainingReads, 1, "重试的终态完成不额外读取 active 剩余时间");
  assert.equal(forkOperationDeadlines.has(retry.operationKey), false);

  const wrongTarget = createDeadline("wrong-target");
  const wrongTargetPayload = makePayload([rejectedResource, committedResource]);
  wrongTargetPayload.data.thread_uri = "boxteam://gateway/workspace/ws-other/resources/session/ses-target/threads/thr-target";
  assert.equal(await classifyForkResponse(wrongTarget, 200, wrongTargetPayload, request, localGatewayId), "pending");
  assert.equal(wrongTarget.completed, 0, "不属于冻结目标 workspace 的响应必须继续受 operation deadline 约束");

  const sourceAuthorityMismatch = createDeadline("source-authority-mismatch");
  const qualifiedSourceRequest = { ...request,
    source_uri: "boxteam://gateway-request/workspace/ws-source/resources/session/ses-source/threads/thr-source" };
  const sourceAuthorityMismatchPayload = makePayload([rejectedResource, committedResource]);
  sourceAuthorityMismatchPayload.data.source_uri =
    "boxteam://gateway-response/workspace/ws-source/resources/session/ses-source/threads/thr-source";
  assert.equal(await classifyForkResponse(sourceAuthorityMismatch, 200, sourceAuthorityMismatchPayload,
    qualifiedSourceRequest, localGatewayId), "pending");
  assert.equal(sourceAuthorityMismatch.completed, 0, "显式来源 Gateway authority 不一致必须继续等待");

  const remoteTarget = createDeadline("remote-target");
  const remoteTargetRequest = { ...request,
    target_container_uri: "boxteam://gateway-target/workspace/ws-target/resources/session" };
  const remoteTargetPayload = makePayload([rejectedResource, committedResource]);
  remoteTargetPayload.data.thread_uri =
    "boxteam://gateway-target/workspace/ws-target/resources/session/ses-target/threads/thr-target";
  assert.equal(remoteTargetPayload.data.session.uri,
    "boxteam://workspace/ws-target/resources/session/ses-target", "remote Session URI 可省略 Gateway authority");
  assert.equal(await classifyForkResponse(remoteTarget, 200, remoteTargetPayload,
    remoteTargetRequest, localGatewayId), "completed");
  assert.equal(remoteTarget.completed, 1, "remote Thread URI 与显式目标 Gateway 匹配时终态 owner 回执必须结束预算");

  const targetAuthorityMismatch = createDeadline("target-authority-mismatch");
  const qualifiedTargetRequest = { ...request,
    target_container_uri: "boxteam://gateway-request/workspace/ws-target/resources/session" };
  assert.equal(await classifyForkResponse(targetAuthorityMismatch, 200,
    makePayload([rejectedResource, committedResource]), qualifiedTargetRequest, localGatewayId), "pending");
  assert.equal(targetAuthorityMismatch.completed, 0, "显式目标 Gateway authority 不一致必须继续等待");

  const missingReceipt = createDeadline("missing-receipt");
  assert.equal(await classifyForkResponse(missingReceipt, 200, makePayload([]), request, localGatewayId), "pending");
  assert.equal(missingReceipt.completed, 0, "缺少已选 live owner receipt 必须继续等待");

  for (const [field, value] of [["kind", "browser"], ["resource_id", "term-other"],
    ["workspace_id", "ws-other"], ["expected_revision", 5]]) {
    const identityMismatch = createDeadline(`owner-identity-mismatch-${field}`);
    const mismatchPayload = makePayload([
      { ...rejectedResource, owner_reference: { ...rejectedResource.owner_reference, [field]: value } },
      committedResource,
    ]);
    assert.equal(await classifyForkResponse(identityMismatch, 200, mismatchPayload, request, localGatewayId), "pending",
      `owner receipt 的 ${field} 不匹配必须继续等待`);
    assert.equal(identityMismatch.completed, 0, `owner receipt 的 ${field} 不匹配不得结束预算`);
  }

  const resourceConflict = createDeadline("resource-4xx");
  assert.equal(await classifyForkResponse(resourceConflict, 409,
    { data: { resources: [rejectedResource] } }, null, localGatewayId), "known");
  assert.equal(resourceConflict.completed, 0, "资源相关 4xx 不能冒充无资源副作用的明确拒绝");

  for (const deadline of testDeadlines) forkOperationDeadlines.delete(deadline.operationKey);
}

async function beginForkUiSubmission(page, source, targetSurface, sessionId, submit) {
  const pathname = `/api/v1/sessions/${sessionId}/fork-context`;
  const matches = (request) => request.method() === "POST" && new URL(request.url()).pathname === pathname;
  const deadline = await beginForkSubmissionDeadline({
    target: `${targetSurface.gatewayId}/${targetSurface.workspaceIdentity}/${source.sessionId}`,
    timeoutMs: FORK_OPERATION_DEADLINE_MS,
  });
  deadline.sourceSessionId = source.sessionId;
  deadline.targetWorkspace = targetSurface.workspaceIdentity;
  const responsePending = page.waitForResponse((response) => matches(response.request()), { timeout: 0 });
  const requestPending = page.waitForRequest(matches, { timeout: 0 }).then(async (request) => {
    const body = request.postDataJSON();
    const boundDeadline = await bindForkOperationDeadline(deadline, body.idempotency_key);
    boundDeadline.sourceSessionId = source.sessionId;
    boundDeadline.targetWorkspace = targetSurface.workspaceIdentity;
    await boundDeadline.setStage("确认按钮提交后的 fork POST 已发出");
    forkOperationDeadlines.set(body.idempotency_key, boundDeadline);
    return { request, body, deadline: boundDeadline };
  });
  const clickPending = submit({ timeout: 0 });
  const [{ request, body, deadline: boundDeadline }] = await waitForForkOperation(
    Promise.all([requestPending, clickPending]), deadline, "确认按钮提交并等待 fork POST request",
  );
  const waitForResponse = async () => {
    let response;
    try {
      response = await waitForForkBudget(responsePending, boundDeadline, "fork POST 响应头");
    } catch (error) {
      const remaining = await boundDeadline.remainingMs();
      if (remaining <= 1) throw forkDeadlineError(boundDeadline);
      throw new Error(`fork response wait failed: source_session_id=${boundDeadline.sourceSessionId}, `
        + `target_workspace=${boundDeadline.targetWorkspace}, idempotency_key=${boundDeadline.operationKey}, `
        + `last_phase=${boundDeadline.lastStage}, backend_state=unknown; ${String(error)}`);
    }
    assert.equal(response.request(), request, "UI fork 响应必须属于本次确认后的 POST");
    const text = await waitForForkBudget(response.text(), boundDeadline, "fork POST 响应正文");
    const payload = text ? JSON.parse(text) : null;
    assertForkResponseRequestId(response, payload, "UI fork API");
    const backendState = await classifyForkResponse(boundDeadline, response.status(), payload, body, targetSurface.gatewayId);
    const durableHistory = response.status() >= 200 && response.status() < 300
      ? await readForkDurableHistory(targetSurface, payload, boundDeadline)
      : null;
    const remainingMs = await remainingForkDeadlineMs(boundDeadline);
    return { response, request: body, payload, backendState, durableHistory, remainingMs,
      deadline: boundDeadline, deadlineToken: boundDeadline.token, deadlineNs: boundDeadline.deadlineNs };
  };
  return { request: body, waitForResponse };
}

function requestFork(source, request, expectedStatus, targetSurface = source) {
  return requestForkResponse(source, request, targetSurface).then((response) => {
    const statusMatches = expectedStatus === undefined ? response.status >= 200 && response.status < 300
      : response.status === expectedStatus;
    if (!statusMatches) throw new Error(`API ${response.status}, backend_state=${response.backendState}: ${JSON.stringify(response.body)}`);
    return response.body;
  });
}

async function requestForkResponse(source, request, targetSurface = source) {
  const { response, deadline } = await requestForkHttpResponse(source, request, targetSurface);
  const durableHistory = response.status >= 200 && response.status < 300
    ? await readForkDurableHistory(targetSurface, response.body, deadline)
    : null;
  return { ...response, durableHistory };
}

async function requestForkHttpResponse(source, request, targetSurface) {
  result.forkRequests.push(structuredClone(request));
  const deadline = await forkDeadline(request, source, targetSurface);
  const token = await waitForForkBudget(
    localToken(targetSurface.page), deadline, "读取 fork 本地凭据",
  );
  const remaining = await deadline.remainingMs();
  if (remaining <= 0) throw forkDeadlineError(deadline);
  let response;
  try {
    response = await targetSurface.page.request.fetch(new URL(
      `/api/v1/sessions/${source.sessionId}/fork-context`, targetSurface.page.url()).toString(), {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Local-Token": token,
        "X-BoxTeam-Workspace-Id": targetSurface.gatewayWorkspaceId },
      data: JSON.stringify(request),
      timeout: 0,
    });
  } catch (error) {
    if (await deadline.remainingMs() <= 1) throw forkDeadlineError(deadline);
    throw new Error(`fork POST failed: source_session_id=${deadline.sourceSessionId}, `
      + `target_workspace=${deadline.targetWorkspace}, idempotency_key=${deadline.operationKey}, `
      + `last_phase=${deadline.lastStage}, backend_state=unknown; ${String(error)}`);
  }
  try {
    const text = await waitForForkBudget(response.text(), deadline, "fork_context POST 响应正文");
    const body = text ? JSON.parse(text) : null;
    const requestId = assertForkResponseRequestId(response, body,
      `API /api/v1/sessions/${source.sessionId}/fork-context`);
    const backendState = await classifyForkResponse(deadline, response.status(), body, request, targetSurface.gatewayId);
    const remainingMs = await remainingForkDeadlineMs(deadline);
    return { response: { status: response.status(), requestId, body, backendState,
      remainingMs, deadlineToken: deadline.token, deadlineNs: deadline.deadlineNs }, deadline };
  } finally {
    await response.dispose();
  }
}

async function forkHistoryEvidence(owner) {
  const viewQuery = "SELECT d.rollout_id,d.active_branch_id,d.projection_epoch,d.history_view_revision,b.head_view_id AS view_id FROM database_meta d JOIN branches b ON b.branch_id=d.active_branch_id WHERE d.singleton_id=1";
  const frozenView = await readRollout(owner, viewQuery);
  assert.equal(frozenView.length, 1);
  assert.equal(typeof frozenView[0].view_id, "string");
  const history = await loadCompleteThreadHistory(owner);
  assert.deepEqual(await readRollout(owner, viewQuery), frozenView, "完整 history 与 SQL 对账必须消费同一 active view/revision");
  assert.equal(history.projection_epoch, frozenView[0].projection_epoch);
  const members = await readRollout(owner,
    "SELECT i.item_id,i.item_sequence,i.turn_id,i.created_at,m.logical_item_ordinal,m.context_selected,m.context_ordinal,m.source_kind,i.execution_id,e.execution_origin,e.turn_id AS execution_turn_id FROM context_view_items m JOIN item_catalog i USING(item_id) LEFT JOIN executions e ON e.execution_id=i.execution_id WHERE m.view_id=? AND m.visible=1 ORDER BY m.logical_item_ordinal", [frozenView[0].view_id]);
  assert.deepEqual(history.internal_executions.map((execution) => execution.execution_id).sort(),
    internalExecutionIdsFromMembers(members),
    "冻结 history execution 集合必须与 reader 谓词命中的 visible view members 一致");
  const roots = await readRollout(owner,
    "SELECT t.turn_id,t.root_input_item_id,m.logical_item_ordinal FROM context_view_turns t JOIN context_view_items m ON m.view_id=t.view_id AND m.item_id=t.root_input_item_id WHERE t.view_id=? AND m.source_kind='canonical'", [frozenView[0].view_id]);
  const turns = await readRollout(owner,
    "SELECT tr.turn_id,tr.root_input_item_id,tr.initial_execution_id,tr.last_execution_id,tr.status,tr.final_item_id,tr.created_at,tr.updated_at,t.created_at AS projection_start,t.updated_at AS projection_end FROM turn_records tr JOIN turns t USING(turn_id) ORDER BY tr.turn_ordinal");
  const inheritedExecutionOwners = freezeInternalExecutionOwners(history.internal_executions);
  const byItem = new Map(members.map((member) => [member.item_id, member]));
  for (const turn of history.items) {
    assert.deepEqual([turn.source_view_id, turn.source_view_revision],
      [frozenView[0].view_id, frozenView[0].history_view_revision], "真实 history 必须给每个 Turn 绑定读取快照");
    const root = roots.find((row) => row.turn_id === turn.turn_id);
    assert.ok(root, "fork 源/目标 Turn 必须有本地显式 root membership");
    assert.equal(root.logical_item_ordinal, turn.root_logical_item_ordinal);
    for (const part of turn.response_parts) {
      const row = byItem.get(part.source.item_id);
      assert.ok(row, "fork history part 必须属于本地冻结 view");
      assert.deepEqual([row.turn_id, row.item_sequence, row.logical_item_ordinal, row.created_at.replace(/\+00:00$/, "Z")],
        [turn.turn_id, part.source.item_sequence, part.source.logical_item_ordinal, part.source.created_at.replace(/\+00:00$/, "Z")]);
    }
    const activity = turn.response_parts.filter((part) => part.kind !== "final_text");
    assert.equal(activity.length, turn.activity_stats.item_count);
    const itemSequences = activity.map((part) => part.source.item_sequence);
    assert.equal(turn.activity_stats.first_item_sequence, itemSequences.length ? Math.min(...itemSequences) : null);
    assert.equal(turn.activity_stats.last_item_sequence, itemSequences.length ? Math.max(...itemSequences) : null);
  }
  const completedTurn = [...history.items].reverse().find((turn) => turn.status === "completed"
    && byItem.get(turns.find((row) => row.turn_id === turn.turn_id)?.final_item_id)?.context_selected === 1);
  let completedAnchorItemId = completedTurn ? turns.find((row) => row.turn_id === completedTurn.turn_id).final_item_id : null;
  if (history.items.length === 0 && members.length > 0) {
    const internal = await readRollout(owner,
      "SELECT i.item_id FROM context_view_items m JOIN item_catalog i USING(item_id) JOIN executions e USING(execution_id) WHERE m.view_id=? AND m.visible=1 AND m.context_selected=1 AND e.execution_origin='internal' AND e.outcome IN ('completed','completed_empty') ORDER BY m.context_ordinal DESC LIMIT 1", [frozenView[0].view_id]);
    completedAnchorItemId = internal[0]?.item_id ?? null;
  }
  return { history, members, turns, view: frozenView[0], completedAnchorItemId, inheritedExecutionOwners };
}

function turnsWithoutHistorySnapshot(items) {
  return items.map((turn) => {
    const stable = { ...turn };
    delete stable.source_view_id;
    delete stable.source_view_revision;
    return stable;
  });
}

function internalExecutionIdsFromMembers(members) {
  const executionIds = new Set();
  for (const member of members) {
    if (member.source_kind !== "display_only" || member.turn_id !== null
      || member.execution_origin !== "internal" || member.execution_turn_id !== null) continue;
    assert.equal(typeof member.execution_id, "string",
      "reader 可见的 display-only internal member 必须绑定 execution");
    executionIds.add(member.execution_id);
  }
  return [...executionIds].sort();
}

function assertForkInternalExecutionSet(sourceEvidence, targetHistory, selectedItemIds, mode) {
  const sourceIds = sourceEvidence.history.internal_executions.map((execution) => execution.execution_id).sort();
  assert.deepEqual(sourceIds, internalExecutionIdsFromMembers(sourceEvidence.members),
    "冻结 source history execution 集合必须由 visible SQL members 证明");
  const selectedMembers = mode === "full_rollout_copy" ? sourceEvidence.members
    : sourceEvidence.members.filter((member) => selectedItemIds.has(member.item_id));
  const expected = internalExecutionIdsFromMembers(selectedMembers);
  const actual = targetHistory.internal_executions.map((execution) => execution.execution_id).sort();
  assert.deepEqual(actual, expected,
    "fork 初始 target 只能包含冻结 source history 中落入复制范围的 internal execution，不能凭空新增或带入截点外 execution");
}

async function startForkMenuObservation(page, article, turnId) {
  const key = "__r2058ForkMenuObservation";
  await article.evaluate((initialArticle, { targetTurnId, observationKey }) => {
    if (globalThis[observationKey]) throw new Error("fork menu observer already installed");
    const maximumRecords = 128;
    const timeline = [];
    const identities = new WeakMap();
    const articleSelector = `.chat-turn[data-turn-id="${CSS.escape(targetTurnId)}"]`;
    const anchorSelector = ".chat-user-action-menu-anchor";
    const triggerSelector = ".chat-user-action-menu-trigger";
    const menuSelector = '.chat-user-action-menu[role="menu"][aria-label="用户消息操作"]';
    let nextIdentity = 1;
    let stopped = false;
    let observer;
    let targetArticle = initialArticle;
    let targetAnchor = null;
    let targetTrigger = null;
    let targetMenu = null;
    let targetPositioner = null;
    let truncated = false;

    const identity = (element) => {
      if (!element) return null;
      let id = identities.get(element);
      if (!id) {
        id = nextIdentity++;
        identities.set(element, id);
      }
      return id;
    };
    const elementInfo = (element) => {
      if (!element) return null;
      return {
        id: identity(element),
        tag: element.tagName.toLowerCase(),
        className: typeof element.className === "string" ? element.className.slice(0, 120) : "",
        connected: element.isConnected,
      };
    };
    const rect = (element) => {
      if (!element) return null;
      const value = element.getBoundingClientRect();
      return { x: Math.round(value.x * 100) / 100, y: Math.round(value.y * 100) / 100,
        width: Math.round(value.width * 100) / 100, height: Math.round(value.height * 100) / 100 };
    };
    const visibility = (element) => {
      if (!element) return null;
      const style = getComputedStyle(element);
      return {
        inline: element.style.visibility,
        computed: style.visibility,
        display: style.display,
        opacity: style.opacity,
        rect: rect(element),
      };
    };
    const currentState = () => {
      targetArticle = targetArticle?.isConnected ? targetArticle : document.querySelector(articleSelector);
      targetAnchor = targetArticle?.querySelector(anchorSelector) ?? null;
      targetTrigger = targetAnchor?.querySelector(triggerSelector) ?? null;
      targetMenu = targetTrigger?.getAttribute("aria-expanded") === "true"
        ? document.querySelector(menuSelector)
        : null;
      targetPositioner = targetMenu?.closest(".anchored-overlay-positioner") ?? null;
      return {
        turnId: targetTurnId,
        article: elementInfo(targetArticle),
        initialArticleConnected: initialArticle.isConnected,
        anchor: targetAnchor ? { ...elementInfo(targetAnchor), rect: rect(targetAnchor) } : null,
        trigger: targetTrigger ? { ...elementInfo(targetTrigger), rect: rect(targetTrigger),
          expanded: targetTrigger.getAttribute("aria-expanded") } : null,
        menu: targetMenu ? {
          ...elementInfo(targetMenu),
          visibility: visibility(targetMenu),
          itemCount: targetMenu.querySelectorAll('[role="menuitem"]').length,
          itemLabels: Array.from(targetMenu.querySelectorAll('[role="menuitem"]'))
            .map((item) => item.getAttribute("aria-label")),
        } : null,
        positioner: targetPositioner ? { ...elementInfo(targetPositioner), visibility: visibility(targetPositioner) } : null,
      };
    };
    const cleanup = () => {
      if (stopped) return;
      stopped = true;
      observer.disconnect();
      document.removeEventListener("pointerdown", pointerListener, true);
      document.removeEventListener("click", clickListener, true);
      document.removeEventListener("keydown", keyListener, true);
    };
    const append = (entry) => {
      if (stopped) return;
      timeline.push({ atMs: Math.round(performance.now() * 100) / 100, ...entry });
      if (timeline.length === maximumRecords) {
        truncated = true;
        cleanup();
      }
    };
    const isTargetNode = (node) => node instanceof Element && (
      node === initialArticle || node === targetArticle || node === targetAnchor || node === targetTrigger
      || node === targetMenu || node === targetPositioner
      || node.matches(articleSelector)
      || node.matches(anchorSelector) && node.closest(articleSelector) !== null
      || node.matches(triggerSelector) && node.closest(articleSelector) !== null
      || node.matches(menuSelector)
      || node.matches(".anchored-overlay-positioner") && node.contains(targetMenu)
    );
    const isWithinTargetControl = (node) => node instanceof Element && (
      targetAnchor?.contains(node) || targetTrigger?.contains(node)
      || targetMenu?.contains(node) || targetPositioner?.contains(node)
    );
    const recordMutations = (records) => {
      if (stopped) return;
      for (const mutation of records) {
        const target = mutation.target instanceof Element ? mutation.target : mutation.target.parentElement;
        const changedNodes = mutation.type === "childList"
          ? [...mutation.addedNodes, ...mutation.removedNodes].filter(isTargetNode).map(elementInfo)
          : [];
        const replacedTarget = [targetArticle, targetAnchor, targetTrigger, targetMenu, targetPositioner]
          .some((element) => element && !element.isConnected);
        if (!replacedTarget && !isTargetNode(target) && !isWithinTargetControl(target) && changedNodes.length === 0) continue;
        currentState();
        append({ kind: "mutation", change: mutation.type, attributeName: mutation.attributeName ?? null,
          oldValue: mutation.oldValue, currentValue: mutation.type === "attributes" ? target?.getAttribute(mutation.attributeName) ?? null : null,
          target: elementInfo(target), changedNodes });
        if (stopped) return;
      }
    };
    const recordEvent = (event) => {
      if (stopped) return;
      currentState();
      const path = event.composedPath().filter((node) => node instanceof Element);
      const related = path.find((element) => targetArticle?.contains(element) || targetMenu?.contains(element));
      const expanded = targetTrigger?.getAttribute("aria-expanded") === "true";
      const escape = event.type === "keydown" && event.key === "Escape";
      if (!related && !(expanded && (event.type === "pointerdown" || escape))) return;
      append({ kind: "event", eventType: event.type, key: event.type === "keydown" ? event.key : null,
        relatedToTarget: Boolean(related), expanded,
        target: elementInfo(event.target instanceof Element ? event.target : null) });
    };
    const pointerListener = (event) => recordEvent(event);
    const clickListener = (event) => recordEvent(event);
    const keyListener = (event) => recordEvent(event);
    observer = new MutationObserver(recordMutations);
    observer.observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeOldValue: true,
      attributeFilter: ["aria-expanded", "style"],
    });
    currentState();
    document.addEventListener("pointerdown", pointerListener, true);
    document.addEventListener("click", clickListener, true);
    document.addEventListener("keydown", keyListener, true);
    const stop = () => {
      cleanup();
      return { timeline, truncated };
    };
    globalThis[observationKey] = {
      capture(label) {
        const state = currentState();
        append({ kind: "snapshot", label, state });
        return { state, timelineLength: timeline.length, truncated };
      },
      stop,
    };
  }, { targetTurnId: turnId, observationKey: key });

  return {
    capture: (label) => page.evaluate(({ observationKey, snapshotLabel }) => {
      const observation = globalThis[observationKey];
      if (!observation) throw new Error("fork menu observer disappeared before snapshot");
      return observation.capture(snapshotLabel);
    }, { observationKey: key, snapshotLabel: label }),
    stop: () => page.evaluate((observationKey) => {
      const observation = globalThis[observationKey];
      if (!observation) return { missing: true };
      const report = observation.stop();
      delete globalThis[observationKey];
      return report;
    }, key),
  };
}

async function publishedForkOwner(source, request, fork, sourceEvidence, targetSurface = source) {
  const before = sourceEvidence.history;
  const inheritedExecutionOwners = sourceEvidence.inheritedExecutionOwners;
  assert.ok(inheritedExecutionOwners && Object.isFrozen(inheritedExecutionOwners), "fork 必须使用请求前冻结的 source history owner 事实");
  const dto = fork.session;
  assertForkSourceOwner(request.source_uri, source, targetSurface.gatewayId);
  assertForkSourceOwner(fork.source_uri, source, targetSurface.gatewayId);
  assert.deepEqual([request.source_view_id, request.source_view_revision],
    [sourceEvidence.view.view_id, sourceEvidence.view.history_view_revision]);
  assert.deepEqual([fork.mode, fork.creation_kind, fork.source_view_id, fork.source_view_revision],
    [request.mode, request.creation_kind, request.source_view_id, request.source_view_revision],
    "发布结果必须绑定原冻结请求，不能在恢复或重试时替换复制范围与来源");
  const anchorItemId = request.mode === "context_fork" ? sourceEvidence.completedAnchorItemId : request.anchor_item_id;
  if (request.mode === "context_fork" && request.anchor_item_id !== null) assert.equal(request.anchor_item_id, anchorItemId);
  assert.equal(fork.anchor_item_id, anchorItemId, "context 自动截止点必须来自源端最新完成且可见的事实");
  assert.equal(dto.workspace_id, targetSurface.workspaceIdentity);
  assert.equal(dto.scope, "workspace");
  const main = (await ownerApi(targetSurface, `/api/v1/sessions/${dto.session_id}/main-thread`)).data;
  assert.equal(main.session_id, dto.session_id);
  assert.equal(dto.thread_id, main.thread_id, "SessionDTO 的 thread_id 始终指向 catalog main owner");
  if (request.creation_kind === "new_session_main") {
    assert.notEqual(dto.resource_identity, source.resource.resource_identity);
    assert.equal(fork.thread_id, main.thread_id);
  } else assert.notEqual(fork.thread_id, main.thread_id);
  const target = { ...targetSurface, sessionId: dto.session_id, threadId: fork.thread_id,
    child: request.creation_kind === "new_child_thread", title: dto.title, dto, inheritedExecutionOwners,
    resource: { resource_identity: dto.resource_identity, uri: targetSurface.uriTemplate.replace("__SESSION__", dto.session_id), scope: dto.scope } };
  assert.equal(fork.thread_uri, `${target.resource.uri}/threads/${target.threadId}`);
  canonicalUuid7(target.sessionId, "ses");
  canonicalUuid7(target.threadId, "thr");
  const catalog = await sessionCatalogNode(target);
  await threadRolloutLocator(target, catalog);
  const capture = await readCopyCapture(targetSurface, forkOperationId(request));
  assert.equal(capture.state, "materialized");
  assert.deepEqual([capture.target_session_id, capture.target_thread_id], [target.sessionId, target.threadId]);
  const frozen = JSON.parse(capture.request_json);
  assertForkSourceOwner(frozen.source_uri, source, targetSurface.gatewayId);
  assert.equal(fork.source_uri, frozen.source_uri, "结果的来源 URI 必须逐字来自原 committed capture");
  assert.deepEqual([frozen.mode, frozen.creation_kind, frozen.source_view_id, frozen.source_view_revision, frozen.anchor_item_id, frozen.anchor_mode, frozen.relationship, frozen.target_main_thread_id],
    [request.mode, request.creation_kind, request.source_view_id, request.source_view_revision, anchorItemId, request.anchor_mode, request.relationship, main.thread_id]);
  const requestedDebugPrefix = request.source_uri + "/debug/node/configurations/";
  assert.deepEqual(frozen.resources.map((choice) => [choice.resource_uri, choice.action]),
    request.resources.filter((choice) => !choice.owner_reference).map((choice) => [choice.resource_uri.startsWith(requestedDebugPrefix)
      ? frozen.source_uri + choice.resource_uri.slice(request.source_uri.length) : choice.resource_uri, choice.action]));
  const mappings = await readAttachmentSql(targetSurface,
    "SELECT m.source_file_id,m.target_session_id,m.target_thread_id,m.snapshot_json,r.owner_session_id,r.owner_thread_id,r.attachment_id,r.state AS owner_state,r.blob_id AS owner_blob_id,r.digest AS owner_digest,r.variant AS owner_variant,r.item_ref AS owner_item_ref,b.availability AS blob_availability,b.digest AS blob_digest,b.length AS blob_length,c.claim_id,c.state AS claim_state,c.target_owner_ref_id AS claim_owner_ref FROM fork_attachment_mappings m LEFT JOIN attachment_owner_refs r ON r.owner_ref_id=json_extract(m.snapshot_json,'$.target_owner_ref_id') LEFT JOIN attachment_blobs b ON b.blob_id=r.blob_id LEFT JOIN fork_attachment_claims c ON c.claim_id=json_extract(m.snapshot_json,'$.claim_id') WHERE m.copy_operation_id=? AND m.target_session_id=? AND m.target_thread_id=? ORDER BY m.source_file_id",
    [capture.copy_operation_id, target.sessionId, target.threadId]);
  const attachmentSnapshots = [];
  const resources = [];
  for (const row of mappings) {
    const snapshot = JSON.parse(row.snapshot_json);
    assert.deepEqual([snapshot.target_session_id, snapshot.target_thread_id], [target.sessionId, target.threadId]);
    if (row.source_file_id !== snapshot.source_file_id) continue;
    attachmentSnapshots.push(snapshot);
    if (snapshot.availability === "available") {
      assert.deepEqual([row.owner_session_id, row.owner_thread_id, row.attachment_id, row.owner_state, row.owner_blob_id,
        row.owner_digest, row.owner_variant, row.owner_item_ref, row.blob_availability, row.blob_digest, row.blob_length],
      [target.sessionId, snapshot.target_owner_thread_id, snapshot.target_attachment_id, "active", snapshot.blob_id,
        snapshot.digest, snapshot.variant, snapshot.target_item_ref, "available", snapshot.digest, snapshot.length],
      "可用附件必须由目标消费 scope 的真实保活 owner 与 blob 发布，复用 main 引用时保活 Thread 可以不同");
      if (snapshot.claim_id !== null) assert.deepEqual([row.claim_id, row.claim_state, row.claim_owner_ref],
        [snapshot.claim_id, "committed", snapshot.target_owner_ref_id]);
    } else assert.equal(snapshot.availability, "unavailable");
    resources.push({ resource_uri: snapshot.source_file_id, action: snapshot.target_action, availability: snapshot.availability,
      target_uri: snapshot.availability === "available" ? snapshot.target_file_id : null,
      reason: snapshot.availability === "available" ? null : snapshot.unavailable_reason });
  }
  const debug = await readFrozenRollout(target, catalog,
    "SELECT state,proof_json FROM fork_debug_snapshots WHERE materialization_id=? AND target_session_id=? AND target_thread_id=?",
    [capture.materialization_id, target.sessionId, target.threadId]);
  assert.ok(debug.length <= 1);
  if (debug.length) assert.equal(debug[0].state, "published");
  const configurationMap = debug.length ? JSON.parse(debug[0].proof_json).target_configuration_id_map : {};
  const debugPrefix = frozen.source_uri + "/debug/node/configurations/";
  for (const choice of frozen.resources.filter((choice) => choice.resource_uri.startsWith(debugPrefix))) {
    const targetId = configurationMap[choice.resource_uri.slice(debugPrefix.length)];
    if (choice.action === "copy_snapshot") assert.equal(typeof targetId, "string");
    resources.push({ resource_uri: choice.resource_uri, action: choice.action, availability: targetId ? "available" : "unavailable",
      target_uri: targetId ? `${fork.thread_uri}/debug/node/configurations/${targetId}` : null,
      reason: targetId ? null : "用户未继承该静态方案" });
  }
  assert.deepEqual(fork.resources.filter((resource) => !resource.owner_reference), resources,
    "完整静态资源结果只能来自目标 owner 已发布的映射，不得省略默认继承附件或回读来源");
  const liveChoices = request.resources.filter((choice) => choice.owner_reference);
  const liveResults = fork.resources.filter((resource) => resource.owner_reference);
  assert.equal(fork.resources.length, resources.length + liveChoices.length, "静态与运行资源必须逐项结算且不能混入 checkpoint copy");
  assert.deepEqual(liveResults.map((resource) => [resource.owner_reference, resource.action]),
    liveChoices.map((choice) => [choice.owner_reference, choice.action]));
  for (const resource of liveResults) {
    assert.equal(Object.hasOwn(resource, "resource_uri"), false, "运行资源只能使用原 owner 的 typed 引用");
    assert.equal(resource.target_uri, null);
    if (resource.action === "omit") {
      assert.equal(resource.operation_state, "not_dispatched");
      assert.equal(resource.availability, "unavailable");
      continue;
    }
    const current = (await ownerApi(target, `/api/v1/resources/${resource.owner_reference.resource_id}`)).data;
    assert.ok(resource.owner_resource, "已结算运行资源必须返回原 owner 的完整当前投影");
    for (const field of ["resource_id", "workspace_id", "kind", "retention_scope", "source_thread", "thread_associations", "owner_revision", "status", "reconciliation_status", "available_actions"])
      assert.deepEqual(resource.owner_resource[field], current[field]);
    assert.ok(["dispatching", "committed", "rejected"].includes(resource.operation_state));
    if (resource.availability === "available") {
      assert.equal(resource.operation_state, "committed");
      assert.equal(resource.committed_revision, resource.owner_reference.expected_revision + 1);
      assert.equal(current.reconciliation_status, "verified");
      assert.ok(current.thread_associations.some((ref) => ref.session_id === target.sessionId && ref.thread_id === target.threadId));
    } else assert.equal(typeof resource.reason, "string");
  }
  await openOwner(target);
  const displayed = await verifyOwnerTurns(target);
  const targetEvidence = await forkHistoryEvidence(target);
  const after = targetEvidence.history;
  const sourceMembers = new Map(sourceEvidence.members.map((row) => [row.item_id, row]));
  const targetMembers = new Map(targetEvidence.members.map((row) => [row.item_id, row]));
  let selectedMembers = sourceEvidence.members;
  if (request.mode !== "full_rollout_copy") {
    const anchor = sourceMembers.get(anchorItemId);
    if (anchorItemId !== null) assert.ok(anchor, "精确截止 item 必须属于源冻结 view");
    if (anchor) assert.equal(anchor.context_selected, 1, "显式截止 item 必须属于有效 selection");
    selectedMembers = anchor ? sourceEvidence.members.filter((row) => row.context_selected === 1 && (request.anchor_mode === "before"
      ? row.context_ordinal < anchor.context_ordinal : row.context_ordinal <= anchor.context_ordinal)) : [];
  }
  const visibleMemberProjection = ({ item_id, item_sequence, turn_id, created_at, logical_item_ordinal }) =>
    ({ item_id, item_sequence, turn_id, created_at, logical_item_ordinal });
  assert.deepEqual(targetEvidence.members.map(visibleMemberProjection), selectedMembers.map(visibleMemberProjection),
    "复制的 visible 成员必须精确等于原冻结范围，backing 不能扩大展示");
  const sourceSelected = selectedMembers.filter((member) => member.context_selected === 1)
    .sort((left, right) => left.context_ordinal - right.context_ordinal);
  for (const member of sourceSelected) assert.ok(Number.isInteger(member.context_ordinal) && member.context_ordinal >= 0,
    `selected context member 必须有非负整数 ordinal: ${member.item_id}`);
  assert.equal(new Set(sourceSelected.map((member) => member.context_ordinal)).size, sourceSelected.length,
    "source context ordinal 必须唯一，才能冻结 selection 顺序");
  const denseContextOrdinalByItem = new Map(sourceSelected.map((member, ordinal) => [member.item_id, ordinal]));
  const expectedSelection = selectedMembers.map((member) => [member.item_id, member.context_selected,
    request.mode === "full_rollout_copy" ? member.context_ordinal
      : member.context_selected === 1 ? denseContextOrdinalByItem.get(member.item_id) : null]);
  assert.deepEqual(targetEvidence.members.map((member) => [member.item_id, member.context_selected, member.context_ordinal]),
    expectedSelection,
    request.mode === "full_rollout_copy"
      ? "完整复制必须逐项保留源 context selection 与 ordinal"
      : "前缀复制必须保留截点内的 context selection，并按源顺序连续重编 ordinal");
  const selectedIds = new Set(selectedMembers.map((row) => row.item_id));
  assertForkInternalExecutionSet(sourceEvidence, after, selectedIds, request.mode);
  assert.deepEqual(after.items.map((turn) => turn.turn_id), before.items.filter((turn) =>
    selectedMembers.some((member) => member.turn_id === turn.turn_id)).map((turn) => turn.turn_id));
  if (request.mode === "full_rollout_copy") {
    assert.deepEqual(targetEvidence.turns, sourceEvidence.turns, "完整复制保留不可变 Turn 与投影时间事实");
  }
  for (const copied of after.items) {
    const original = before.items.find((turn) => turn.turn_id === copied.turn_id);
    assert.ok(original, "复制历史不得重建或替换原 Turn ID");
    const rootSelected = selectedIds.has(sourceEvidence.turns.find((row) => row.turn_id === original.turn_id).root_input_item_id);
    assert.deepEqual(copied.user_messages.map((message) => message.content), rootSelected
      ? original.user_messages.map((message) => message.content) : []);
    const selectedParts = original.response_parts.filter((part) => targetMembers.has(part.source.item_id));
    assert.deepEqual(copied.response_parts.map((part) => [part.part_id, part.kind, part.text ?? part.result ?? part.tool_name]),
      selectedParts.map((part) => [part.part_id, part.kind, part.text ?? part.result ?? part.tool_name]));
    if (request.mode === "full_rollout_copy") assert.deepEqual(copied.activity_stats, original.activity_stats);
    assert.deepEqual(copied.response_parts.map((part) => [part.source.item_id, part.source.part_ordinal, part.source.created_at, part.source.elapsed_ms]),
      selectedParts.map((part) => [part.source.item_id, part.source.part_ordinal, part.source.created_at, part.source.elapsed_ms]));
    for (const part of copied.response_parts) {
      assert.equal(targetMembers.get(part.source.item_id).created_at,
        sourceMembers.get(part.source.item_id).created_at, "fork canonical 创建时间必须逐字保留源端值");
    }
    assert.equal(copied.status, original.status, "历史状态来自canonical事实，截点外final正文仍只按visible成员展示");
    assert.deepEqual([copied.created_at, copied.updated_at, copied.completed_at], [original.created_at, original.updated_at, original.completed_at]);
    const sourceTurn = sourceEvidence.turns.find((row) => row.turn_id === original.turn_id);
    const targetTurn = targetEvidence.turns.find((row) => row.turn_id === copied.turn_id);
    assert.deepEqual(targetTurn, sourceTurn);
  }
  return { owner: target, result: fork, sourceHistory: before, targetHistory: after, displayed, sourceEvidence, targetEvidence,
    publication: { capture, frozen, attachmentSnapshots, debug } };
}

async function forkOwner(source, options = {}, targetSurface = source) {
  const before = await forkHistoryEvidence(source);
  const request = await freezeForkRequest(source, options, targetSurface);
  const response = await requestForkResponse(source, request, targetSurface);
  assert.equal(response.backendState, "completed", `真实 fork 首次提交必须是可核验终态: ${response.status}`);
  assert.ok(response.status >= 200 && response.status < 300,
    `API ${response.status}, backend_state=${response.backendState}: ${JSON.stringify(response.body)}`);
  return { ...await publishedForkOwner(source, request, response.body.data, before, targetSurface), request,
    deadlineToken: response.deadlineToken, deadlineNs: response.deadlineNs };
}

async function runEmptyForkUi(surface) {
  const owners = [];
  const empty = await createProductSession(surface, "空来源的完整历史分支");
  owners.push(empty);
  await openOwner(empty);
  assert.deepEqual((await loadCompleteThreadHistory(empty)).items, []);
  assert.equal(await readRolloutDirectory(empty), null, "打开全新空会话及读取历史不能物化 rollout");
  const emptyMain = (await ownerApi(empty, `/api/v1/sessions/${empty.sessionId}/main-thread`)).data;
  const emptyPointPath = `/api/v1/sessions/${empty.sessionId}/fork-source-point`;
  const waitEmptyPoint = (mode) => empty.page.waitForResponse((response) => {
    const url = new URL(response.url());
    return response.request().method() === "GET" && url.pathname === emptyPointPath
      && (mode === undefined || url.searchParams.get("mode") === mode);
  }, { timeout: 30_000 });
  await catalogRow(empty, empty.sessionId).click({ button: "right" });
  const [emptyPointResponse] = await Promise.all([
    waitEmptyPoint(), empty.page.getByRole("menuitem", { name: "从上下文创建分支", exact: true }).click(),
  ]);
  assert.equal(emptyPointResponse.status(), 200, await emptyPointResponse.text());
  const emptyPoint = (await emptyPointResponse.json()).data;
  assert.equal(emptyPoint.anchor_item_id, null);
  assert.equal(emptyPoint.resource_mode, "view_prefix_fork");
  const emptyBefore = await forkHistoryEvidence(empty);
  assert.deepEqual([emptyPoint.source_view_id, emptyPoint.source_view_revision],
    [emptyBefore.view.view_id, emptyBefore.view.history_view_revision], "来源选择必须冻结真实物化的空 view");
  const emptyDialog = empty.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await emptyDialog.getByLabel("复制范围", { exact: true }).waitFor({ state: "visible" });
  assert.equal(await emptyDialog.getByLabel("复制范围", { exact: true }).inputValue(), "view_prefix_fork");
  assert.equal(await emptyDialog.getByRole("button", { name: "创建分支", exact: true }).isDisabled(), true);
  const [fullPointResponse] = await Promise.all([
    waitEmptyPoint("full_rollout_copy"), emptyDialog.getByLabel("复制范围", { exact: true }).selectOption("full_rollout_copy"),
  ]);
  assert.equal(fullPointResponse.status(), 200, await fullPointResponse.text());
  const fullPoint = (await fullPointResponse.json()).data;
  assert.deepEqual([fullPoint.source_uri, fullPoint.source_view_id, fullPoint.source_view_revision, fullPoint.anchor_item_id],
    [emptyPoint.source_uri, emptyPoint.source_view_id, emptyPoint.source_view_revision, null]);
  await waitUntil(async () => await emptyDialog.getByRole("button", { name: "创建分支", exact: true }).isEnabled(), "空来源显式完整历史候选收敛");
  const emptySubmission = await beginForkUiSubmission(empty.page, empty, empty, empty.sessionId,
    (options) => emptyDialog.getByRole("button", { name: "创建分支", exact: true }).click(options));
  const { response: emptyForkResponse, request: emptyRequest, payload: emptyPayload } = await emptySubmission.waitForResponse();
  assert.equal(emptyForkResponse.status(), 200, JSON.stringify(emptyPayload));
  const emptyFork = emptyPayload.data;
  assert.deepEqual([emptyRequest.mode, emptyRequest.creation_kind, emptyRequest.anchor_item_id, emptyRequest.resources],
    ["full_rollout_copy", "new_child_thread", null, []]);
  assert.deepEqual([emptyRequest.source_uri, emptyRequest.source_view_id, emptyRequest.source_view_revision],
    [emptyPoint.source_uri, emptyPoint.source_view_id, emptyPoint.source_view_revision]);
  assert.equal(emptyFork.session.session_id, empty.sessionId);
  const emptyChild = { ...empty, threadId: emptyFork.thread_id, child: true };
  await empty.page.locator(`.child-thread-conversation[data-thread-id="${emptyChild.threadId}"]`).waitFor({ state: "visible" });
  const emptySummary = (await ownerApi(empty, `/api/v1/sessions/${empty.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === emptyChild.threadId);
  assert.ok(emptySummary);
  assert.equal(emptySummary.status, "idle");
  assert.equal(emptySummary.runtime.execution_state, "idle");
  assert.deepEqual(await graphLifecycle(emptyChild), { compiled: [], built: [], acquired: [], closed: [] });
  assert.deepEqual(await readRollout(emptyChild, "SELECT execution_id FROM executions"), []);
  assert.deepEqual((await ownerApi(empty, `/api/v1/sessions/${empty.sessionId}/main-thread`)).data, emptyMain);
  assert.equal(await catalogRow(empty, empty.sessionId).getAttribute("aria-current"), "true");
  assert.equal(await empty.page.locator("#input").isVisible(), true);
  const emptyPublished = await publishedForkOwner(empty, emptyRequest, emptyFork, emptyBefore);
  assert.deepEqual(emptyFork.resources, []);
  const successor = await completeOwnerTurn(emptyChild, "从空分支开始第一轮独立对话。", [
    finalResponse("empty-child-fork-successor", "空分支已开始独立对话。"),
  ]);
  assert.deepEqual((await loadCompleteThreadHistory(empty)).items, [], "空分支首次续聊不能给 main 创建历史");
  return { source: emptyPoint, fullPoint, request: emptyRequest, fork: emptyFork, summary: emptySummary,
    publication: emptyPublished.publication, successor };
}

async function runDefaultChildForkUi(surface) {
  const owners = [];
  const { source, material, attachment } = await createAttachmentSource(surface, "页面默认子线程分支来源", owners);
  const before = await forkHistoryEvidence(source);
  const displayedTurn = before.history.items.find((turn) => turn.turn_id === material.turn.turn_id);
  assert.ok(displayedTurn);
  const main = (await ownerApi(source, `/api/v1/sessions/${source.sessionId}/main-thread`)).data;
  const callsBefore = await readRollout(source, "SELECT model_call_id,session_id,thread_id FROM model_calls ORDER BY model_call_id");
  const sourcePath = `/api/v1/sessions/${source.sessionId}/fork-source-point`;
  const waitPoint = () => source.page.waitForResponse((response) => response.request().method() === "GET"
    && new URL(response.url()).pathname === sourcePath, { timeout: 30_000 });
  const targetArticle = source.page.locator(`.chat-stream-shell .chat-turn[data-turn-id="${material.turn.turn_id}"]`);
  assert.equal(await targetArticle.count(), 1, "菜单观测必须绑定唯一目标 Turn article");
  const menuObservation = { turnId: material.turn.turn_id, afterTriggerClick: null, failureFinal: null,
    timeline: [], observer: null, pointResponseOutcome: null, observationErrors: [] };
  (result.forkMenuObservations ??= []).push(menuObservation);
  const observer = await startForkMenuObservation(source.page, targetArticle, material.turn.turn_id);
  const pointPending = waitPoint().then(
    (response) => ({ response }),
    (error) => ({ error }),
  );
  let pointResponse;
  let actionError;
  try {
    try {
      await targetArticle.getByRole("button", { name: "消息操作", exact: true }).click();
      menuObservation.afterTriggerClick = await observer.capture("after-trigger-click");
      await source.page.getByRole("menuitem", { name: "从此消息创建分支", exact: true }).click();
    } catch (error) {
      actionError = error;
    }

    const pointOutcome = await pointPending;
    if (pointOutcome.error) {
      menuObservation.pointResponseOutcome = { status: "rejected", error: String(pointOutcome.error) };
      if (!actionError) actionError = pointOutcome.error;
    } else {
      pointResponse = pointOutcome.response;
      menuObservation.pointResponseOutcome = { status: "received", httpStatus: pointResponse.status(), url: pointResponse.url() };
    }
    if (actionError) {
      try {
        menuObservation.failureFinal = await observer.capture("failure-final");
      } catch (error) {
        menuObservation.observationErrors.push(`failure snapshot: ${String(error)}`);
      }
    }
  } finally {
    try {
      const observerReport = await observer.stop();
      menuObservation.timeline = observerReport.timeline;
      menuObservation.observer = Object.fromEntries(Object.entries(observerReport).filter(([key]) => key !== "timeline"));
    } catch (error) {
      menuObservation.observationErrors.push(`observer cleanup: ${String(error)}`);
    }
  }
  if (actionError) throw actionError;
  assert.equal(pointResponse.status(), 200, await pointResponse.text());
  assert.equal(new URL(pointResponse.url()).searchParams.get("turn_id"), material.turn.turn_id);
  assert.deepEqual([new URL(pointResponse.url()).searchParams.get("thread_id"),
    new URL(pointResponse.url()).searchParams.get("source_view_id"),
    new URL(pointResponse.url()).searchParams.get("source_view_revision")],
  [source.threadId, displayedTurn.source_view_id, String(displayedTurn.source_view_revision)],
  "初次页面点击必须携带实际读取的 Thread/view/revision");
  const point = (await pointResponse.json()).data;
  assertForkSourceOwner(point.source_uri, source);
  assert.equal(point.source_view_id, before.view.view_id);
  assert.equal(point.source_view_revision, before.view.history_view_revision);
  assert.equal(point.anchor_item_id, before.turns.find((turn) => turn.turn_id === material.turn.turn_id).root_input_item_id);
  assert.deepEqual(point.resources.map((resource) => [resource.resource_uri, resource.name, resource.content_type, resource.availability, resource.required_for_context]),
    [[attachment.file_id, attachment.name, "text/plain", "available", true]], "附件候选必须从真实页面GET取得真实ingress附件");
  const dialog = source.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await dialog.waitFor({ state: "visible" });
  assert.ok((await dialog.innerText()).includes(`${source.title} · ${source.sessionId}`));
  assert.equal(await dialog.getByLabel("复制范围", { exact: true }).inputValue(), "view_prefix_fork");
  assert.equal(await dialog.getByLabel("截止位置", { exact: true }).inputValue(), "inclusive");
  assert.equal(await dialog.getByLabel("创建位置", { exact: true }).inputValue(), "new_child_thread");
  assert.equal(await dialog.getByLabel("目标容器", { exact: true }).inputValue(), point.child_container_uri);
  const name = "用户编辑的默认分支名称";
  await dialog.getByLabel("目标名称", { exact: true }).fill(name);
  for (const anchorMode of ["before", "inclusive"]) {
    const [response] = await Promise.all([waitPoint(), dialog.getByLabel("截止位置", { exact: true }).selectOption(anchorMode)]);
    assert.equal(response.status(), 200, await response.text());
    const refreshed = (await response.json()).data;
    assert.deepEqual([refreshed.source_view_id, refreshed.source_view_revision, refreshed.anchor_item_id],
      [point.source_view_id, point.source_view_revision, point.anchor_item_id], "范围切换必须绑定原点击点");
    assert.equal(refreshed.resource_anchor_mode, anchorMode);
    assert.equal(refreshed.resources.length, anchorMode === "before" ? 0 : 1);
    await waitUntil(async () => await dialog.getByRole("button", { name: "创建分支", exact: true }).isEnabled(), "真实候选回执收敛后允许创建");
    assert.equal(await dialog.getByLabel("目标名称", { exact: true }).inputValue(), name, "范围补读不能覆盖用户名称");
  }
  const resource = dialog.locator("fieldset label").filter({ hasText: attachment.name });
  assert.equal(await resource.count(), 1);
  assert.equal(await resource.getByRole("combobox").inputValue(), "default");
  // isDisabled 会沿隐式 label 重定位到 select；这里直接核选项的原生禁用状态。
  const omitOption = await resource.getByRole("option", { name: "不继承", exact: true })
    .evaluate((option) => ({ disabled: option.disabled, matchesDisabled: option.matches(":disabled") }));
  assert.deepEqual(omitOption, { disabled: true, matchesDisabled: true }, "上下文必需附件不能选择不继承");
  const viewport = source.page.viewportSize();
  assert.ok(viewport);
  for (const control of [dialog.getByRole("heading", { name: "创建上下文分支", exact: true }),
    dialog.getByRole("button", { name: "创建分支", exact: true })]) {
    const box = await control.boundingBox();
    assert.ok(box && box.y >= 0 && box.y + box.height <= viewport.height, "弹窗标题和创建操作必须保持在可见视口内");
  }
  const submission = await beginForkUiSubmission(source.page, source, source, source.sessionId,
    (options) => dialog.getByRole("button", { name: "创建分支", exact: true }).click(options));
  const { response: forkResponse, request, payload } = await submission.waitForResponse();
  assert.equal(forkResponse.status(), 200, JSON.stringify(payload));
  const fork = payload.data;
  assert.deepEqual([request.source_uri, request.source_view_id, request.source_view_revision, request.anchor_item_id],
    [point.source_uri, point.source_view_id, point.source_view_revision, point.anchor_item_id]);
  assert.deepEqual([request.mode, request.anchor_mode, request.creation_kind, request.title, request.relationship, request.resources],
    ["view_prefix_fork", "inclusive", "new_child_thread", name, "detached", []]);
  assert.equal(fork.session.session_id, source.sessionId);
  assert.equal(fork.creation_kind, "new_child_thread");
  assert.notEqual(fork.thread_id, source.threadId);
  assert.equal(fork.thread_uri, `${source.resource.uri}/threads/${fork.thread_id}`);
  assertForkSourceOwner(fork.source_uri, source);
  const child = { ...source, threadId: fork.thread_id, child: true };
  await source.page.locator(`.child-thread-conversation[data-thread-id="${child.threadId}"]`).waitFor({ state: "visible" });
  assert.equal(await catalogRow(source, source.sessionId).getAttribute("aria-current"), "true", "默认分支保持主窗口的会话选择");
  assert.equal(await source.page.locator("#input").isVisible(), true);
  assert.deepEqual((await ownerApi(source, `/api/v1/sessions/${source.sessionId}/main-thread`)).data, main);
  const summary = (await ownerApi(source, `/api/v1/sessions/${source.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId);
  assert.ok(summary);
  assert.equal(summary.status, "idle");
  assert.equal(summary.runtime.execution_state, "idle");
  assert.deepEqual(await graphLifecycle(child), { compiled: [], built: [], acquired: [], closed: [] }, "创建等待消息的分支不能主动建图或执行");
  assert.deepEqual(await readRollout(source, "SELECT model_call_id,session_id,thread_id FROM model_calls ORDER BY model_call_id"), callsBefore);
  assert.deepEqual(await readRollout(child, "SELECT model_call_id FROM model_calls WHERE session_id=? AND thread_id=?", [child.sessionId, child.threadId]), []);
  const published = await publishedForkOwner(source, request, fork, before);
  assert.deepEqual(fork.resources, [{ resource_uri: attachment.file_id, action: "reuse_reference", availability: "available",
    target_uri: attachment.file_id, reason: null }], "同会话默认 child 必须发布 main 已保活附件的复用结果");
  const coldHistory = published.targetHistory;
  assert.deepEqual(coldHistory.items.map((turn) => turn.turn_id), [material.turn.turn_id]);
  assert.deepEqual(coldHistory.items[0].response_parts, [], "点击用户root只发布可见root，不扩张backing响应");
  assert.equal(coldHistory.items[0].status, material.turn.status);
  const displayed = published.displayed;
  assert.deepEqual((await loadCompleteThreadHistory(source)).items, before.history.items, "同会话分支不得改写main历史");
  const successor = await completeOwnerTurn(child, "在默认子线程中继续使用刚才的附件上下文。", [finalResponse("default-child-fork-successor", "子线程已在本地独立继续。")]);
  const wire = (await modelRequest(successor.calls[0])).request.messages;
  assert.ok(JSON.stringify(wire).includes(material.turn.user_messages[0].content));
  assert.ok(JSON.stringify(wire).includes("在默认子线程中继续使用刚才的附件上下文。"));
  assert.deepEqual((await loadCompleteThreadHistory(source)).items, before.history.items);
  const childBefore = await forkHistoryEvidence(child);
  const childContainer = child.page.locator(`.child-thread-conversation[data-thread-id="${child.threadId}"]`);
  const childSuccessorTurn = childBefore.history.items.find((turn) => turn.turn_id === successor.turn.turn_id);
  assert.ok(childSuccessorTurn, "后继完整 Turn 必须来自实际读取的 child history");
  assert.equal(childSuccessorTurn.status, "completed");
  assert.deepEqual([childSuccessorTurn.source_view_id, childSuccessorTurn.source_view_revision],
    [childBefore.view.view_id, childBefore.view.history_view_revision], "按钮所属 Turn 必须保留实际展示的冻结快照");
  const childFinalPart = childSuccessorTurn.response_parts.find((part) => part.kind === "final_text" && part.final);
  assert.ok(childFinalPart && typeof childFinalPart.source.item_id === "string", "后继最终回答必须有可分支的稳定 item 身份");
  assert.equal(childFinalPart.status, "completed", "item 锚点必须来自已闭合最终回答");
  const childTurnElement = childContainer.locator(`.child-thread-turn[data-turn-id="${childSuccessorTurn.turn_id}"]`);
  await childTurnElement.waitFor({ state: "visible" });
  const childUserForkButton = childTurnElement.locator(".child-thread-user button");
  const childFinalForkButton = childTurnElement.locator(`[data-fork-item-id="${childFinalPart.source.item_id}"]`);
  assert.equal(await childUserForkButton.count(), 1, "必须定位到后继 Turn 的用户消息按钮");
  assert.equal(await childFinalForkButton.count(), 1, "必须唯一定位到实际展示的最终回答 item 按钮");
  const [childTurnPointResponse] = await Promise.all([
    child.page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === sourcePath
        && url.searchParams.get("thread_id") === child.threadId
        && url.searchParams.get("turn_id") === childSuccessorTurn.turn_id;
    }, { timeout: 30_000 }),
    childUserForkButton.click(),
  ]);
  assert.equal(childTurnPointResponse.status(), 200, await childTurnPointResponse.text());
  const childTurnQuery = new URL(childTurnPointResponse.url()).searchParams;
  assert.deepEqual([childTurnQuery.get("thread_id"), childTurnQuery.get("turn_id"), childTurnQuery.get("item_id"),
    childTurnQuery.get("source_view_id"), childTurnQuery.get("source_view_revision")],
  [child.threadId, childSuccessorTurn.turn_id, null, childSuccessorTurn.source_view_id,
    String(childSuccessorTurn.source_view_revision)], "Turn 按钮必须发送自身实际展示快照");
  const childTurnPoint = (await childTurnPointResponse.json()).data;
  const childSuccessorEvidence = childBefore.turns.find((turn) => turn.turn_id === childSuccessorTurn.turn_id);
  assert.ok(childSuccessorEvidence);
  assert.deepEqual([childTurnPoint.source_view_id, childTurnPoint.source_view_revision, childTurnPoint.anchor_item_id],
    [childSuccessorTurn.source_view_id, childSuccessorTurn.source_view_revision, childSuccessorEvidence.root_input_item_id]);
  const childTurnDialog = child.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await childTurnDialog.waitFor({ state: "visible" });
  await childTurnDialog.getByRole("button", { name: "取消", exact: true }).click();
  const [childItemPointResponse] = await Promise.all([
    child.page.waitForResponse((response) => {
      const url = new URL(response.url());
      return response.request().method() === "GET" && url.pathname === sourcePath
        && url.searchParams.get("thread_id") === child.threadId
        && url.searchParams.get("item_id") === childFinalPart.source.item_id;
    }, { timeout: 30_000 }),
    childFinalForkButton.click(),
  ]);
  assert.equal(childItemPointResponse.status(), 200, await childItemPointResponse.text());
  const childItemQuery = new URL(childItemPointResponse.url()).searchParams;
  assert.deepEqual([childItemQuery.get("thread_id"), childItemQuery.get("turn_id"), childItemQuery.get("item_id"),
    childItemQuery.get("source_view_id"), childItemQuery.get("source_view_revision")],
  [child.threadId, null, childFinalPart.source.item_id, childSuccessorTurn.source_view_id,
    String(childSuccessorTurn.source_view_revision)], "item 按钮必须发送所属 Turn 的实际展示快照");
  const childItemPoint = (await childItemPointResponse.json()).data;
  assert.deepEqual([childItemPoint.source_view_id, childItemPoint.source_view_revision, childItemPoint.anchor_item_id],
    [childSuccessorTurn.source_view_id, childSuccessorTurn.source_view_revision, childFinalPart.source.item_id]);
  const childItemDialog = child.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await childItemDialog.waitFor({ state: "visible" });
  await childItemDialog.getByRole("button", { name: "取消", exact: true }).click();
  const [childPointResponse] = await Promise.all([
    child.page.waitForResponse((response) => response.request().method() === "GET"
      && new URL(response.url()).pathname === sourcePath
      && new URL(response.url()).searchParams.get("thread_id") === child.threadId, { timeout: 30_000 }),
    childContainer.getByRole("button", { name: "从此线程创建分支", exact: true }).click(),
  ]);
  assert.equal(childPointResponse.status(), 200, await childPointResponse.text());
  assert.equal(new URL(childPointResponse.url()).searchParams.get("thread_id"), child.threadId);
  const childThreadQuery = new URL(childPointResponse.url()).searchParams;
  assert.deepEqual([childThreadQuery.get("turn_id"), childThreadQuery.get("item_id"),
    childThreadQuery.get("source_view_id"), childThreadQuery.get("source_view_revision")], [null, null, null, null],
  "线程标题按钮继续以空选择读取当前 child 快照");
  const childPoint = (await childPointResponse.json()).data;
  assertForkSourceOwner(childPoint.source_uri, child);
  assert.equal(childPoint.child_container_uri, point.child_container_uri);
  assert.deepEqual([childPoint.source_view_id, childPoint.source_view_revision, childPoint.anchor_item_id,
    childPoint.resource_mode, childPoint.resource_anchor_mode],
  [childBefore.view.view_id, childBefore.view.history_view_revision, childBefore.completedAnchorItemId,
    "view_prefix_fork", "inclusive"], "右侧线程按钮必须读取精确 child owner 的真实候选");
  const childDialog = child.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await childDialog.waitFor({ state: "visible" });
  assert.deepEqual(await Promise.all(["复制范围", "截止位置", "创建位置", "目标容器"].map((label) =>
    childDialog.getByLabel(label, { exact: true }).inputValue())),
  ["view_prefix_fork", "inclusive", "new_child_thread", childPoint.child_container_uri]);
  await waitUntil(async () => await childDialog.getByRole("button", { name: "创建分支", exact: true }).isEnabled(), "右侧 child 的真实候选允许默认创建");
  const childSubmission = await beginForkUiSubmission(child.page, child, child, source.sessionId,
    (options) => childDialog.getByRole("button", { name: "创建分支", exact: true }).click(options));
  const { response: childForkResponse, request: childRequest, payload: childPayload } = await childSubmission.waitForResponse();
  assert.equal(childForkResponse.status(), 200, JSON.stringify(childPayload));
  const childFork = childPayload.data;
  assert.deepEqual([childRequest.source_uri, childRequest.source_view_id, childRequest.source_view_revision, childRequest.anchor_item_id,
    childRequest.mode, childRequest.anchor_mode, childRequest.creation_kind, childRequest.target_container_uri, childRequest.relationship, childRequest.resources],
  [childPoint.source_uri, childPoint.source_view_id, childPoint.source_view_revision, childPoint.anchor_item_id,
    "view_prefix_fork", "inclusive", "new_child_thread", childPoint.child_container_uri, "detached", []]);
  assert.equal(childFork.session.session_id, source.sessionId);
  assert.equal(childFork.session.thread_id, main.thread_id);
  assert.notEqual(childFork.thread_id, child.threadId);
  assert.notEqual(childFork.thread_id, main.thread_id);
  assert.equal(childFork.thread_uri, `${source.resource.uri}/threads/${childFork.thread_id}`);
  await child.page.locator(`.child-thread-conversation[data-thread-id="${childFork.thread_id}"]`).waitFor({ state: "visible" });
  assert.equal(await catalogRow(source, source.sessionId).getAttribute("aria-current"), "true", "右侧 child 再次分支保持当前主会话选择");
  assert.equal(await child.page.locator("#input").isVisible(), true);
  assert.deepEqual((await ownerApi(source, `/api/v1/sessions/${source.sessionId}/main-thread`)).data, main);
  const childPublished = await publishedForkOwner(child, childRequest, childFork, childBefore);
  assert.deepEqual(childFork.resources, fork.resources, "右侧 child 二次分支仍由同会话的真实 main 附件 owner 保活");
  assert.deepEqual(await graphLifecycle(childPublished.owner), { compiled: [], built: [], acquired: [], closed: [] });
  assert.deepEqual(await readRollout(childPublished.owner, "SELECT execution_id FROM executions WHERE session_id=? AND thread_id=?",
    [childPublished.owner.sessionId, childPublished.owner.threadId]), [], "右侧二次分支不得自动生成新的执行");
  assert.deepEqual((await loadCompleteThreadHistory(source)).items, before.history.items);

  // 已有子线程的关闭、重开和刷新只是展示操作，不得再次创建或启动任务。
  const existing = [source, child, childPublished.owner];
  const controlQuery = "SELECT (SELECT COUNT(*) FROM thread_catalog) AS threads,(SELECT COUNT(*) FROM thread_creation_records) AS creations,(SELECT COUNT(*) FROM thread_execution_intents) AS intents,(SELECT COUNT(*) FROM collaboration_fanouts) AS fanouts";
  const readExisting = async () => ({
    control: await readControl(source, controlQuery),
    owners: await Promise.all(existing.map(async (owner) => {
      const history = await loadCompleteThreadHistory(owner);
      return { threadId: owner.threadId, items: history.items, internal: history.internal_executions,
        executions: await readRollout(owner, "SELECT * FROM executions ORDER BY execution_id"),
        calls: await readRollout(owner, "SELECT * FROM model_calls ORDER BY model_call_id"),
        graph: await graphLifecycle(owner) };
    })),
  });
  const existingBefore = await readExisting();
  const unexpectedRequests = [];
  const recordMutation = (request) => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && /\/sessions\/[^/]+\/(fork-context|(?:threads\/[^/]+\/)?messages)$/.test(path)) {
      unexpectedRequests.push({ method: request.method(), path, body: request.postDataJSON() });
    }
  };
  source.page.on("request", recordMutation);
  try {
    for (const owner of [childPublished.owner, child]) {
      await source.page.getByRole("button", { name: "关闭子线程对话", exact: true }).click();
      await source.page.locator(".child-thread-conversation").waitFor({ state: "hidden" });
      await source.page.locator(`.child-thread-item[data-thread-id="${owner.threadId}"] .child-thread-main`).click();
      const conversation = source.page.locator(`.child-thread-conversation[data-thread-id="${owner.threadId}"]`);
      await conversation.waitFor({ state: "visible" });
      await conversation.locator(`.child-thread-turn[data-turn-id="${successor.turn.turn_id}"]`).waitFor({ state: "visible" });
      assert.equal(await catalogRow(source, source.sessionId).getAttribute("aria-current"), "true");
      assert.equal(await source.page.locator("#input").isVisible(), true);
    }
    await openOwner(childPublished.owner);
    await source.page.locator(`.child-thread-conversation[data-thread-id="${childFork.thread_id}"] .child-thread-turn[data-turn-id="${successor.turn.turn_id}"]`).waitFor({ state: "visible" });
    assert.deepEqual(await readExisting(), existingBefore, "重开和刷新不能改变历史、执行、模型调用、线程或回报准入");
    assert.deepEqual(unexpectedRequests, [], "重开已有子线程不能提交fork或消息");
  } finally {
    source.page.off("request", recordMutation);
  }

  // 左侧会话菜单与消息入口共用同一弹窗，来源仍是该 Session 的 main。
  await catalogRow(source, source.sessionId).click({ button: "right" });
  const [sidebarResponse] = await Promise.all([
    waitPoint(), source.page.getByRole("menuitem", { name: "从上下文创建分支", exact: true }).click(),
  ]);
  assert.equal(sidebarResponse.status(), 200, await sidebarResponse.text());
  const sidebarPoint = (await sidebarResponse.json()).data;
  assertForkSourceOwner(sidebarPoint.source_uri, source);
  assert.deepEqual([sidebarPoint.source_view_id, sidebarPoint.source_view_revision, sidebarPoint.anchor_item_id],
    [before.view.view_id, before.view.history_view_revision, before.completedAnchorItemId]);
  const sidebarDialog = source.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
  await sidebarDialog.waitFor({ state: "visible" });
  assert.deepEqual(await Promise.all(["复制范围", "截止位置", "创建位置", "目标容器"].map((label) =>
    sidebarDialog.getByLabel(label, { exact: true }).inputValue())),
    ["view_prefix_fork", "inclusive", "new_child_thread", sidebarPoint.child_container_uri]);
  await waitUntil(async () => await sidebarDialog.getByRole("button", { name: "创建分支", exact: true }).isEnabled(), "侧边菜单取得真实候选后允许默认创建");
  const sidebarSubmission = await beginForkUiSubmission(source.page, source, source, source.sessionId,
    (options) => sidebarDialog.getByRole("button", { name: "创建分支", exact: true }).click(options));
  const { response: sidebarForkResponse, request: sidebarRequest, payload: sidebarPayload } = await sidebarSubmission.waitForResponse();
  assert.equal(sidebarForkResponse.status(), 200, JSON.stringify(sidebarPayload));
  const sidebarFork = sidebarPayload.data;
  assert.deepEqual([sidebarRequest.source_uri, sidebarRequest.source_view_id, sidebarRequest.source_view_revision,
    sidebarRequest.anchor_item_id, sidebarRequest.mode, sidebarRequest.anchor_mode, sidebarRequest.creation_kind,
    sidebarRequest.target_container_uri, sidebarRequest.relationship, sidebarRequest.resources],
    [sidebarPoint.source_uri, sidebarPoint.source_view_id, sidebarPoint.source_view_revision, sidebarPoint.anchor_item_id,
      "view_prefix_fork", "inclusive", "new_child_thread", sidebarPoint.child_container_uri, "detached", []]);
  assert.equal(sidebarFork.session.session_id, source.sessionId);
  assert.ok(!existing.some((owner) => owner.threadId === sidebarFork.thread_id), "侧边新建必须发布全新thread");
  await source.page.locator(`.child-thread-conversation[data-thread-id="${sidebarFork.thread_id}"]`).waitFor({ state: "visible" });
  assert.equal(await catalogRow(source, source.sessionId).getAttribute("aria-current"), "true");
  assert.deepEqual((await ownerApi(source, `/api/v1/sessions/${source.sessionId}/main-thread`)).data, main);
  const sidebarPublished = await publishedForkOwner(source, sidebarRequest, sidebarFork, before);
  assert.deepEqual(await graphLifecycle(sidebarPublished.owner), { compiled: [], built: [], acquired: [], closed: [] });
  assert.deepEqual(await readRollout(sidebarPublished.owner, "SELECT execution_id FROM executions WHERE session_id=? AND thread_id=?",
    [source.sessionId, sidebarFork.thread_id]), [], "侧边新建不得自动执行或回报");
  const afterSidebar = await readExisting();
  assert.deepEqual(afterSidebar.owners, existingBefore.owners, "侧边新建不得修改已有main或child事实");
  assert.deepEqual(afterSidebar.control, [{ ...existingBefore.control[0],
    threads: existingBefore.control[0].threads + 1, creations: existingBefore.control[0].creations + 1 }],
    "侧边新建只增加一个thread及其创建记录，不派生执行或回报准入");
  return { source: { sessionId: source.sessionId, threadId: source.threadId },
    point, omitOption, request, fork, summary, coldHistory, displayed, successor, publication: published.publication,
    reopen: { before: existingBefore, unexpectedRequests },
    sidebar: { point: sidebarPoint, request: sidebarRequest, fork: sidebarFork, publication: sidebarPublished.publication },
    childBranch: { source: childPoint, request: childRequest, fork: childFork, publication: childPublished.publication,
      history: childPublished.targetHistory, displayed: childPublished.displayed } };
}

async function readCopyCapture(surface, operationId) {
  const rows = await readCatalog(surface, "SELECT * FROM fork_source_captures WHERE copy_operation_id=?", [operationId]);
  assert.equal(rows.length, 1);
  return rows[0];
}

async function readCopyTargetControl(surface, capture, query, parameters = []) {
  const rows = await readCatalog(surface, "SELECT * FROM session_creation_records WHERE session_creation_idempotency_key=?", [capture.creation_key]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].session_id, capture.target_session_id);
  return testControl({ action: "read_sql", name: surface.name,
    relative_path: `.boxteam/${rows[0].storage_relative_locator}/session-control.sqlite`, query, parameters });
}

async function copyRequestSurface(source) {
  const page = await browserStartup("copy_source_page", () => source.page.context().newPage());
  installPageDiagnostics(page);
  await ensureGuest(page, JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"))[source.name].base_url);
  await waitUntil(() => [...page.diagnosticObservationRequests.values()].some((request) =>
    request.body?.subscriptions?.length && controlledRestartResponses.some((response) =>
      response.requestFactId === request.requestFactId && response.status === 200
      && hasConnectedObservationTargets(response, request.body.subscriptions))),
  "copy 请求页面必须先建立真实观察订阅，才能验证后端重启恢复");
  return { ...source, page };
}

function bindControlledRestartPage(window, page, role) {
  assert.equal(page.controlledRestartWindow, null, "页面不能同时属于两个受控重启窗口");
  const binding = { pageId: page.diagnosticId, role,
    documentIds: page.diagnosticDocumentId > 0 ? [page.diagnosticDocumentId] : [],
    sessionIds: [...page.diagnosticSessionIds],
    observationRequests: role === "copy-request"
      ? structuredClone([...page.diagnosticObservationRequests.values()].filter((request) =>
        request.body?.subscriptions?.length && controlledRestartResponses.some((response) =>
          response.requestFactId === request.requestFactId && response.status === 200
          && hasConnectedObservationTargets(response, request.body.subscriptions)))) : [] };
  window.pageBindings.push(binding);
  page.controlledRestartWindow = window;
  return binding;
}

function beginControlledRestartWindow(surface, { operationId, phase, captured, activeSessionId, requestPage }) {
  assert.equal(captured.copy_operation_id, operationId, "重启窗口必须绑定真实 capture operation ID");
  assert.ok(requestPage, "受控重启必须绑定实际发起 copy 请求的产品页面");
  const node = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"))[surface.name];
  const gatewayUrl = new URL(node.base_url);
  const window = { operationId, phase, sourceSessionId: captured.source_session_id,
    captureOperationId: captured.copy_operation_id,
    sourceThreadId: captured.source_thread_id, targetSessionId: captured.target_session_id,
    activeSessionId, workspaceId: surface.gatewayWorkspaceId, origin: gatewayUrl.origin,
    targetName: surface.name, gatewayPort: Number(gatewayUrl.port),
    backendRestart: { targetName: surface.name, port: null, beforePid: null, afterPid: null,
      stopRequestedAt: null, stoppedAt: null, startRequestedAt: null, startedAt: null },
    startedAt: Date.now(), endedAt: null, pageBindings: [] };
  controlledRestartWindows.push(window);
  bindControlledRestartPage(window, requestPage, "copy-request");
  bindControlledRestartPage(window, surface.page, "foreground-before-restart");
  return window;
}

function recordControlledRestartStopped(window, targetName, stopped, requestedAt) {
  assert.equal(targetName, window.targetName, "受控重启目标必须与窗口注册目标一致");
  assert.equal(Number.isInteger(stopped.before_pid), true, "受控重启必须保存被停止后端的真实 PID");
  assert.equal(stopped.after_pid, null, "kill 回执不得谎报仍运行的后端 PID");
  assert.equal(Number.isInteger(stopped.port), true, "受控重启必须保存后端真实监听端口");
  Object.assign(window.backendRestart, { port: stopped.port, beforePid: stopped.before_pid,
    stopRequestedAt: requestedAt, stoppedAt: Date.now() });
}

function recordControlledRestartStarted(window, targetName, started, requestedAt) {
  assert.equal(targetName, window.targetName, "受控恢复目标必须与窗口注册目标一致");
  assert.equal(started.before_pid, window.backendRestart.beforePid, "恢复回执必须属于被停止的同一旧 PID");
  assert.equal(started.port, window.backendRestart.port, "恢复必须复用原后端监听端口");
  assert.equal(Number.isInteger(started.after_pid), true, "受控恢复必须保存新后端的真实 PID");
  Object.assign(window.backendRestart, { afterPid: started.after_pid,
    startRequestedAt: requestedAt, startedAt: Date.now() });
}

async function runCopyCaptureLifecycle(surface, owners, activeSessionId) {
  const recoveries = [];
  const heldSource = await createProductSession(surface, "copy 持有 source guard 时删除等待 capture");
  owners.push(heldSource);
  await completeOwnerTurn(heldSource, "正常 capture 必须完整冻结后才允许删除关闭 source fence。", [
    toolResponse("capture-held-read", "read_file", { path: "README.md" }),
    finalResponse("capture-held-final", "这份冻结正文在 source 删除后仍只进入原 target。"),
  ]);
  const heldHistory = await forkHistoryEvidence(heldSource);
  const heldNode = await sessionCatalogNode(heldSource);
  const heldRequest = await freezeForkRequest(heldSource);
  const heldOperationId = forkOperationId(heldRequest);
  const heldPartial = { operation_id: heldOperationId, phase: "copy.source.partial" };
  const heldMarker = { operation_id: heldOperationId, phase: "copy.source.marker_published" };
  const heldCas = { operation_id: heldOperationId, phase: "copy.source.before_captured_cas" };
  const heldDeletion = await prepareNavigationIntent(heldSource, { kind: "delete_session", target_node_id: heldSource.sessionId });
  const deleteExecuting = { operation_id: heldDeletion.operationId, phase: "navigation.before_commit" };
  for (const barrier of [heldPartial, heldMarker, heldCas, deleteExecuting]) await testControl({ action: "arm", ...barrier });
  const heldRequestSurface = await copyRequestSurface(heldSource);
  const heldPending = requestFork(heldSource, heldRequest, undefined, heldRequestSurface).then((value) => ({ value }), (error) => ({ error: String(error) }));
  await testControl({ action: "acknowledged", ...heldPartial });
  const heldCapture = await readCopyCapture(surface, heldOperationId);
  assert.equal(heldCapture.state, "preparing");
  await enqueueNavigationIntent(heldSource, heldDeletion);
  await testControl({ action: "acknowledged", ...deleteExecuting });
  await testControl({ action: "release", ...deleteExecuting });
  let pendingDeletion;
  await waitUntil(async () => {
    pendingDeletion = (await ownerApi(heldSource, `/api/v1/session-catalog/operations?operation_id=${heldDeletion.operationId}`)).data.items.find((item) => item.operation_id === heldDeletion.operationId);
    return pendingDeletion?.state === "running";
  }, "已接受删除实际进入原 gate 等待");
  const heldFence = await readFrozenControl(heldSource, heldNode, "SELECT state,generation FROM lifecycle_fence");
  assert.equal(heldFence[0].state, "active", "capture guard 未释放时删除不能关闭 source fence");
  await testControl({ action: "release", ...heldPartial });
  await testControl({ action: "acknowledged", ...heldMarker });
  const heldMarkerPath = `${heldSource.workspace}/.boxteam/navigation/${heldCapture.snapshot_relative_locator}/captured.json`;
  const heldMarkerBytes = await readFile(heldMarkerPath);
  const heldSnapshot = JSON.parse(heldMarkerBytes);
  assert.equal(heldSnapshot.copy_operation_id, heldOperationId);
  assert.equal(heldSnapshot.source_snapshot_id, heldCapture.snapshot_id);
  assert.equal(heldSnapshot.preimage_hash, heldCapture.preimage_hash);
  assert.equal(heldSnapshot.rollout.source_session_id, heldSource.sessionId);
  assert.equal(heldSnapshot.rollout.source_thread_id, heldSource.threadId);
  assert.deepEqual(await readFrozenControl(heldSource, heldNode, "SELECT state,generation FROM lifecycle_fence"), heldFence,
    "captured marker 已 durable 且 guard 尚持有时 source fence 必须保持原 generation");
  assert.ok(!(await testControl({ action: "events" })).some((event) => event.operation_id === heldDeletion.operationId && event.phase === "deletion.fence_closed/" + heldSource.sessionId));
  await testControl({ action: "release", ...heldMarker });
  await testControl({ action: "acknowledged", ...heldCas });
  const heldDeleted = await navigationTerminal(heldSource, heldDeletion);
  assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [heldSource.sessionId]), []);
  assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [heldCapture.target_session_id]), [], "source 删除完成时 target 尚未开始物化");
  assert.deepEqual(await readFile(heldMarkerPath), heldMarkerBytes, "删除不能改变原 durable capture");
  await testControl({ action: "release", ...heldCas });
  const heldReply = await heldPending;
  assert.equal(heldReply.error, undefined, heldReply.error);
  await drainPageResponseBodies(heldRequestSurface.page);
  await heldRequestSurface.page.close();
  const heldTarget = await publishedForkOwner(heldSource, heldRequest, heldReply.value.data, heldHistory);
  owners.push(heldTarget.owner);
  const heldAfter = await readCopyCapture(surface, heldOperationId);
  assert.equal(heldAfter.state, "materialized");
  assert.equal(heldAfter.marker_sha256, createHash("sha256").update(heldMarkerBytes).digest("hex"));
  assert.equal(heldAfter.snapshot_sha256, heldSnapshot.snapshot_sha256);
  assert.equal((await testControl({ action: "events" })).filter((event) => event.operation_id === heldOperationId && event.phase === "copy.source.partial").length, 1,
    "target 只能消费原 snapshot，不能再次读取已删除 source");
  recoveries.push({ phase: "copy-held-source-delete", operationId: heldOperationId, captured: heldCapture, pendingDeletion, heldFence,
    marker: { sha256: heldAfter.marker_sha256, snapshot: heldSnapshot }, sourceDelete: heldDeleted, after: heldAfter,
    target: { sessionId: heldTarget.owner.sessionId, history: heldTarget.targetHistory, displayed: heldTarget.displayed } });
  for (const phase of ["copy.source.partial", "copy.source.before_captured_cas"]) {
    const source = await createProductSession(surface, `公开 copy capture ${phase}`);
    owners.push(source);
    await completeOwnerTurn(source, "建立本轮被冻结的完整 source revision。", [
      toolResponse(`capture-${phase}-read`, "read_file", { path: "README.md" }),
      finalResponse(`capture-${phase}-final`, "冻结时的正文只能进入本轮 target。"),
    ]);
    const before = await forkHistoryEvidence(source);
    const request = await freezeForkRequest(source);
    const operationId = forkOperationId(request);
    const barrier = { operation_id: operationId, phase };
    await testControl({ action: "arm", ...barrier });
    const requestSurface = await copyRequestSurface(source);
    const requestStartedAt = performance.now();
    const pending = requestForkResponse(source, request, requestSurface)
      .then((response) => ({ response }), (error) => ({ error: String(error) }));
    await testControl({ action: "acknowledged", ...barrier });
    const captured = await readCopyCapture(surface, operationId);
    assert.equal(captured.state, "preparing");
    assert.equal(captured.snapshot_sha256, null);
    assert.equal(captured.marker_sha256, null);
    assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [captured.target_session_id]), [], "capture 完成前不能公开 target");
    let marker;
    if (phase === "copy.source.before_captured_cas") {
      const markerPath = `${source.workspace}/.boxteam/navigation/${captured.snapshot_relative_locator}/captured.json`;
      const markerBytes = await readFile(markerPath);
      const frozen = JSON.parse(markerBytes);
      assert.equal(frozen.copy_operation_id, operationId);
      assert.equal(frozen.source_snapshot_id, captured.snapshot_id);
      assert.equal(frozen.preimage_hash, captured.preimage_hash);
      assert.equal(frozen.rollout.source_session_id, source.sessionId);
      assert.equal(frozen.rollout.source_thread_id, source.threadId);
      marker = { sha256: createHash("sha256").update(markerBytes).digest("hex"), snapshot: frozen };
    }
    const sourceDeletion = marker
      ? await enqueueNavigationIntent(source, await prepareNavigationIntent(source, {
        kind: "delete_session", target_node_id: source.sessionId,
      }))
      : null;
    let firstHttp;
    const restartWindow = beginControlledRestartWindow(surface, { operationId, phase,
      captured, activeSessionId, requestPage: requestSurface.page });
    const reopen = await replaceNodePage(surface, owners, restartWindow);
    const stopRequestedAt = Date.now();
    const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
    recordControlledRestartStopped(restartWindow, surface.name, killed, stopRequestedAt);
    await testControl({ action: "release", ...barrier });
    firstHttp ??= await pending;
    const firstHttpElapsedMs = performance.now() - requestStartedAt;
    if (firstHttp.error) {
      assert.match(firstHttp.error, /backend_state=unknown/,
        "客户端本地绝对期限到达必须明确报告后端终态未知");
      assert.ok(firstHttpElapsedMs >= FORK_OPERATION_DEADLINE_MS - 1_000,
        "本地 deadline 错误必须发生在原始预算边界");
    } else if (firstHttp.response.status === 502) {
      assert.match(firstHttp.response.body.detail, /^无法连接工作区后端 /,
        "进程立即退出时原 HTTP 必须收到 Gateway 的明确上游连接失败");
      assert.ok(firstHttpElapsedMs < FORK_OPERATION_DEADLINE_MS, "502 必须发生在单次 fork 总期限内");
    } else {
      assert.equal(firstHttp.response.status, 504,
        "只有精确 Gateway 响应头超时可解释长屏障期间的失败");
      assert.match(firstHttp.response.body.detail, /timeout_seconds=60(?:\.0)?$/,
        "长屏障超时必须明确绑定 Gateway 60 秒期限");
      assert.ok(firstHttpElapsedMs >= FORK_OPERATION_DEADLINE_MS - 1_000, "504 必须发生在 fork 总期限边界附近");
    }
    const startRequestedAt = Date.now();
    const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    recordControlledRestartStarted(restartWindow, surface.name, restarted, startRequestedAt);
    assert.notEqual(killed.before_pid, restarted.after_pid);
    await closeControlledRestartRequestPage(requestSurface.page, restartWindow);
    await reopen();
    const after = await readCopyCapture(surface, operationId);
    assert.deepEqual({ ...after, state: captured.state, snapshot_sha256: captured.snapshot_sha256, marker_sha256: captured.marker_sha256, abort_reason: captured.abort_reason }, captured,
      "恢复不得替换 copy/source/target/claim preimage 与 identity");
    let target;
    if (marker) {
      assert.equal(after.state, "materialized");
      assert.equal(after.marker_sha256, marker.sha256);
      assert.equal(after.snapshot_sha256, marker.snapshot.snapshot_sha256);
      assert.ok(sourceDeletion, "durable marker recovery 必须保留崩溃前已接受的 source DELETE");
      const sourceDelete = await navigationTerminal(source, sourceDeletion);
      assert.equal(sourceDelete.state, "committed");
      assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [source.sessionId]), [],
        "source DELETE 必须在同 key replay 前真实提交");
      const captureAndDeleteEvents = (await testControl({ action: "events" })).filter((event) =>
        event.operation_id === operationId && event.phase === "copy.source.captured"
        || event.operation_id === sourceDeletion.operationId && event.phase === `deletion.fence_closed/${source.sessionId}`);
      const capturedEvent = captureAndDeleteEvents.filter((event) => event.operation_id === operationId);
      const deletedFenceEvent = captureAndDeleteEvents.filter((event) => event.operation_id === sourceDeletion.operationId);
      assert.equal(capturedEvent.length, 1, "startup recovery 必须恰好从原 marker 捕获一次 source");
      assert.equal(deletedFenceEvent.length, 1, "source DELETE 必须经过真实 lifecycle fence close");
      assert.ok(capturedEvent[0].at < deletedFenceEvent[0].at,
        "startup recovery 消费 durable marker 后，source DELETE 才能关闭 source fence");
      const targetReceipt = await readCatalog(surface,
        "SELECT session_id,main_thread_id,state FROM session_creation_records WHERE session_creation_idempotency_key=?",
        [after.creation_key]);
      assert.deepEqual(targetReceipt, [{ session_id: after.target_session_id,
        main_thread_id: after.target_thread_id, state: "published" }],
      "startup recovery 必须发布同一个 target owner receipt");
      assert.deepEqual(await readCatalog(surface, "SELECT node_id,state FROM nodes WHERE node_id=?", [after.target_session_id]),
        [{ node_id: after.target_session_id, state: "active" }],
      "source 删除后 target catalog owner 必须保持 active");
      const fork = (await requestFork(source, request, undefined, surface)).data;
      const totalElapsedMs = performance.now() - requestStartedAt;
      assert.ok(totalElapsedMs < FORK_OPERATION_DEADLINE_MS,
        `marker 崩溃后同 key 恢复必须沿用首次 POST 的原始期限，elapsed_ms=${totalElapsedMs.toFixed(3)}`);
      target = await publishedForkOwner(source, request, fork, before);
      owners.push(target.owner);
      assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [source.sessionId]), [], "恢复只能消费 SourceCopySnapshot，不能复活 source");
      const sourceCaptureEvents = (await testControl({ action: "events" })).filter((event) => event.operation_id === operationId && event.phase === "copy.source.partial");
      assert.equal(sourceCaptureEvents.length, 1, "完整 marker 恢复不得再次读取当前 source capture");
    } else {
      assert.equal(after.state, "aborted");
      assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [after.target_session_id]), []);
      assert.deepEqual(await readCatalog(surface,
        "SELECT state,session_id,main_thread_id FROM session_creation_records WHERE session_creation_idempotency_key=?", [after.creation_key]),
      [{ state: "aborted", session_id: after.target_session_id, main_thread_id: after.target_thread_id }]);
      assert.deepEqual(await copyAttachmentClaims(surface, operationId), []);
      assert.deepEqual((await loadCompleteThreadHistory(source)).items, before.history.items, "partial 失败不能修改 source");
      await finishForkDeadline(forkOperationDeadlines.get(request.idempotency_key));
      await deleteOwner(source);
    }
    recoveries.push({ phase, operationId, captured, marker, killed,
      firstHttp: firstHttp.response ? { ...firstHttp.response, elapsed_ms: firstHttpElapsedMs }
        : { error: firstHttp.error, elapsed_ms: firstHttpElapsedMs }, restarted, after,
      target: target ? { sessionId: target.owner.sessionId, history: target.targetHistory, displayed: target.displayed } : null });
  }
  const markerCrash = await runCopyMarkerCrashRecovery(surface, owners, activeSessionId);
  return [...recoveries, markerCrash];
}

async function runCopyMarkerCrashRecovery(surface, owners, activeSessionId) {
  const source = await createProductSession(surface, "marker durable 后立即崩溃");
  owners.push(source);
  await completeOwnerTurn(source, "为 marker 崩溃恢复建立不可变 source 正文。", [
    finalResponse("copy-marker-crash-source", "marker 崩溃后仍应消费冻结的 source。"),
  ]);
  const before = await forkHistoryEvidence(source);
  const request = await freezeForkRequest(source);
  const operationId = forkOperationId(request);
  const barrier = { operation_id: operationId, phase: "copy.source.before_captured_cas" };
  await testControl({ action: "arm", ...barrier });
  const requestSurface = await copyRequestSurface(source);
  const requestStartedAt = performance.now();
  const pending = requestForkResponse(source, request, requestSurface)
    .then((response) => ({ response }), (error) => ({ error: String(error) }));
  await testControl({ action: "acknowledged", ...barrier });
  const captured = await readCopyCapture(surface, operationId);
  assert.equal(captured.state, "preparing");
  assert.equal(captured.snapshot_sha256, null);
  assert.equal(captured.marker_sha256, null);
  const markerPath = `${source.workspace}/.boxteam/navigation/${captured.snapshot_relative_locator}/captured.json`;
  const markerBytes = await readFile(markerPath);
  const marker = JSON.parse(markerBytes);
  assert.equal(marker.copy_operation_id, operationId);
  assert.equal(marker.source_snapshot_id, captured.snapshot_id);
  assert.equal(marker.preimage_hash, captured.preimage_hash);
  const restartWindow = beginControlledRestartWindow(surface, { operationId, phase: barrier.phase,
    captured, activeSessionId, requestPage: requestSurface.page });
  const reopen = await replaceNodePage(surface, owners, restartWindow);
  const stopRequestedAt = Date.now();
  const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
  recordControlledRestartStopped(restartWindow, surface.name, killed, stopRequestedAt);
  await testControl({ action: "release", ...barrier });
  const firstHttp = await pending;
  assert.equal(firstHttp.error, undefined, firstHttp.error);
  assert.equal(firstHttp.response.status, 502, "marker durable 后立即崩溃必须使原 fork HTTP 收到真实上游失败");
  assert.match(firstHttp.response.body.detail, /^无法连接工作区后端 /);
  const firstHttpElapsedMs = performance.now() - requestStartedAt;
  assert.ok(firstHttpElapsedMs < 60_000, "原 HTTP 失败必须发生在 Gateway 60 秒期限前");
  const startRequestedAt = Date.now();
  const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
  recordControlledRestartStarted(restartWindow, surface.name, restarted, startRequestedAt);
  assert.notEqual(killed.before_pid, restarted.after_pid);
  await closeControlledRestartRequestPage(requestSurface.page, restartWindow);
  await reopen();
  const after = await readCopyCapture(surface, operationId);
  assert.equal(after.state, "materialized");
  assert.equal(after.marker_sha256, createHash("sha256").update(markerBytes).digest("hex"));
  assert.equal(await testControl({ action: "read_directory", name: surface.name,
    relative_path: `.boxteam/navigation/${after.snapshot_relative_locator}` }), null,
  "恢复完成并 materialize 后必须清理原 captured snapshot");
  const fork = (await requestFork(source, request)).data;
  const totalElapsedMs = performance.now() - requestStartedAt;
  assert.ok(totalElapsedMs < FORK_OPERATION_DEADLINE_MS,
    `marker durable 崩溃后的同 key 恢复必须沿用首次 POST 的期限，elapsed_ms=${totalElapsedMs.toFixed(3)}`);
  const target = await publishedForkOwner(source, request, fork, before);
  owners.push(target.owner);
  assert.deepEqual((await loadCompleteThreadHistory(source)).items, before.history.items,
    "marker 崩溃重放不能改变 source history");
  assert.equal((await readCopyCapture(surface, operationId)).state, "materialized");
  await deleteOwner(target.owner);
  const sourceDelete = await deleteOwner(source);
  return { phase: barrier.phase, operationId, captured, marker, killed, restarted,
    firstHttp: { ...firstHttp.response, elapsed_ms: firstHttpElapsedMs }, total_elapsed_ms: totalElapsedMs, after,
    targetSessionId: target.owner.sessionId, sourceDelete };
}

async function readAttachmentContent(owner, fileId, expectedStatus = 200) {
  const token = await localToken(owner.page);
  const response = await owner.page.evaluate(async ({ path, token, workspaceId }) => {
    const reply = await fetch(path, { headers: { "X-Local-Token": token, "X-BoxTeam-Workspace-Id": workspaceId }, credentials: "include" });
    return { status: reply.status, requestId: reply.headers.get("X-Request-ID"), contentType: reply.headers.get("Content-Type"), bytes: [...new Uint8Array(await reply.arrayBuffer())] };
  }, { path: `/api/v1/sessions/${owner.sessionId}/attachments/content?${new URLSearchParams({ file_id: fileId, variant: "original" })}`, token, workspaceId: owner.gatewayWorkspaceId });
  assert.equal(response.status, expectedStatus, Buffer.from(response.bytes).toString("utf8"));
  assert.equal(typeof response.requestId, "string");
  assert.ok(response.requestId.length > 0);
  return response;
}

async function readAttachmentSql(surface, query, parameters = []) {
  return testControl({ action: "read_sql", name: surface.name, relative_path: ".boxteam/attachments/catalog.sqlite", query, parameters });
}

async function copyAttachmentClaims(surface, operationId) {
  return readAttachmentSql(surface, "SELECT c.*,r.state AS target_owner_state,r.blob_id AS target_owner_blob_id FROM fork_attachment_claims c LEFT JOIN attachment_owner_refs r ON r.owner_ref_id=c.target_owner_ref_id WHERE c.copy_operation_id=? ORDER BY c.claim_id", [operationId]);
}

async function createAttachmentSource(surface, title, owners, { prefixTurns = 0, turnOptions = {} } = {}) {
  const source = await createProductSession(surface, title);
  owners.push(source);
  const prefix = [];
  for (let index = 0; index < prefixTurns; index += 1) prefix.push(await completeOwnerTurn(source,
    `无附件的稳定前缀 ${index}。`, [finalResponse(`attachment-prefix-${index}`, `第 ${index} 轮稳定前缀已完成。`)], [], {
      ...turnOptions, openBeforeSend: turnOptions.openBeforeSend ?? true,
    }));
  const buffer = Buffer.concat([await readFile(`${source.workspace}/README.md`), Buffer.from(`\n独占附件事实：${source.sessionId}\n`)]);
  const file = { name: "source-attachment.txt", mimeType: "text/plain", buffer };
  const material = await completeOwnerTurn(source, "这份已上传的附件属于本轮 source Turn，请保留其独占事实。", [finalResponse(`attachment-${source.sessionId}`, "附件已成为本轮 source 的可用输入。")], [file], {
    ...turnOptions, openBeforeSend: turnOptions.openBeforeSend ?? prefixTurns === 0,
  });
  const refs = material.turn.user_messages.flatMap((message) => message.attachments);
  assert.equal(refs.length, 1);
  const attachment = refs[0];
  assert.match(attachment.file_id, new RegExp(`^boxteam://(?:${source.gatewayId}/)?workspace/${source.workspaceIdentity}/resources/session/${source.sessionId}/attachments/att_[0-9a-f]{32}$`));
  const content = await readAttachmentContent(source, attachment.file_id);
  assert.deepEqual(Buffer.from(content.bytes), buffer);
  const blob = (await readAttachmentSql(source, "SELECT b.* FROM attachment_blobs b JOIN attachment_owner_refs r USING(blob_id) WHERE r.owner_session_id=? AND r.attachment_id=?", [source.sessionId, attachment.file_id.split("/").at(-1)]))[0];
  assert.equal(blob.availability, "available");
  assert.equal(blob.length, buffer.length);
  assert.equal(blob.digest, "sha256:" + createHash("sha256").update(buffer).digest("hex"));
  return { source, prefix, material, attachment, blob, buffer };
}

async function enableAttachmentCapabilityTools(surface) {
  const catalog = (await ownerApi(surface, "/api/v1/tools?agent_id=default")).data;
  const toolIds = ["read_file", "write_file", "edit_file"].map((name) => {
    const tool = catalog.find((entry) => entry.name === name);
    assert.ok(tool, `附件 capability 场景缺少正常工具 ${name}`);
    return tool.tool_id;
  });
  await ownerApi(surface, "/api/v1/tools/selection", { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "default", changes: toolIds.map((tool_id) => ({ tool_id, execution_enabled: true, model_visible: true })) }) });
}

async function runMessageStreamAfter20(surface) {
  await enableAttachmentCapabilityTools(surface);
  const owners = [];
  const { source, attachment, buffer } = await createAttachmentSource(surface, "消息流游标 20 的真实工具失败", owners);
  const before = await forkHistoryEvidence(source);
  const request = await freezeForkRequest(source);
  const fork = (await requestFork(source, request)).data;
  const published = await publishedForkOwner(source, request, fork, before);
  const targetFile = published.targetHistory.items.flatMap((turn) => turn.user_messages)
    .flatMap((message) => message.attachments).find((item) => item.name === attachment.name)?.file_id;
  assert.ok(targetFile?.startsWith(`boxteam://${published.owner.gatewayId}/workspace/${published.owner.workspaceIdentity}/resources/session/${published.owner.sessionId}/attachments/`));
  const capabilityBoundary = await runAttachmentCapabilityBoundary(published.owner, source,
    targetFile, attachment.file_id, "message-stream-after20");
  const response = messageStreamResponses.findLast((fact) => fact.status === 200
    && fact.sessionId === published.owner.sessionId && fact.turnId === capabilityBoundary.sent.turnId);
  assert.ok(response?.messageStreamId, "真实页面必须先订阅同一 Turn 的消息流");
  const query = new URLSearchParams({ thread_id: published.owner.threadId,
    turn_stream_id: response.messageStreamId, after_seq: "20" });
  const token = await localToken(published.owner.page);
  // 浏览器从同一 Gateway 续读真实持久流；不构造或替换任何产品事件。
  const replay = await published.owner.page.evaluate(async ({ path, token, workspaceId }) => {
    const response = await fetch(path, { headers: { "X-Local-Token": token,
      "X-BoxTeam-Workspace-Id": workspaceId, Accept: "text/event-stream", "Last-Event-ID": "20" } });
    const fact = { status: response.status, requestId: response.headers.get("X-Request-ID"),
      streamId: response.headers.get("X-Message-Stream-ID"), chunks: [], eof: false, error: null };
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) { fact.eof = true; break; }
        fact.chunks.push(decoder.decode(value, { stream: true }));
      }
      fact.chunks.push(decoder.decode());
    } catch (error) {
      fact.error = String(error);
    } finally {
      reader.releaseLock();
    }
    return fact;
  }, { path: `/api/v1/sessions/${published.owner.sessionId}/turns/${capabilityBoundary.sent.turnId}/message-stream?${query}`,
    token, workspaceId: published.owner.gatewayWorkspaceId });
  const facts = { sourceSessionId: source.sessionId, sessionId: published.owner.sessionId,
    threadId: published.owner.threadId, turnId: capabilityBoundary.sent.turnId,
    capabilityBoundary, originalSubscription: response, replay };
  result.section8.message_stream_after20 = facts;
  assert.equal(replay.status, 200);
  assert.equal(replay.streamId, response.messageStreamId);
  assert.ok(replay.requestId);
  assert.equal(replay.error, null, `after_seq=20 必须完成真实 body: ${JSON.stringify(replay)}`);
  assert.equal(replay.eof, true);
  const events = replay.chunks.join("").split("\n\n").filter((frame) => frame.includes("\ndata:"))
    .map((frame) => JSON.parse(frame.split("\n").find((line) => line.startsWith("data:")).slice(5)));
  assert.ok(events.length > 0);
  assert.equal(events[0].event_seq, 21, "续读必须从真实 seq21 开始");
  assert.ok(events.every((event, index) => event.event_seq === index + 21
    && event.session_id === published.owner.sessionId && event.thread_id === published.owner.threadId
    && event.turn_stream_id === response.messageStreamId));
  assert.equal(events.at(-1).type, "stream.completed");
  facts.replayedEventSeqs = events.map((event) => event.event_seq);
  assert.deepEqual(Buffer.from((await readAttachmentContent(published.owner, targetFile)).bytes), buffer);
  return facts;
}

async function runAttachmentCapabilityBoundary(owner, source, targetFile, sourceFile, label) {
  const alias = targetFile.replace(`boxteam://${owner.gatewayId}/`, "boxteam://");
  const completed = await completeOwnerTurn(owner, "读取附件的规范路径，并确认其它会话引用及两种路径的写入都被明确拒绝。", [
    { label: `attachment-capability-boundary-${label}`, calls: [
      { id: "attachment-alias-read", name: "read_file", arguments: { path: alias } },
      { id: "attachment-source-read", name: "read_file", arguments: { path: sourceFile } },
      { id: "attachment-uri-write", name: "write_file", arguments: { file_path: targetFile, content: "forbidden rewrite", expected_version: "absent" } },
      { id: "attachment-alias-write", name: "write_file", arguments: { file_path: alias, content: "forbidden rewrite", expected_version: "absent" } },
      { id: "attachment-alias-edit", name: "edit_file", arguments: { file_path: alias, expected_version: `sha256:${"0".repeat(64)}`, old_string: "独占附件事实", new_string: "forbidden rewrite" } },
    ] }, finalResponse(`attachment-capability-boundary-final-${label}`, "附件规范路径可读，跨会话与写入能力已明确拒绝。"),
  ]);
  assert.equal(completed.turn.response_parts.filter((part) => part.kind === "tool_result").length, 5);
  const boundaryResult = (callId) => {
    const toolName = callId.endsWith("read") ? "read_file" : callId.endsWith("write") ? "write_file" : "edit_file";
    return String(assertBoundToolResult(owner, completed.sent, completed.turn, completed.calls[0], callId, toolName).result);
  };
  assert.ok(boundaryResult("attachment-alias-read").includes(source.sessionId), "规范 alias 必须消费同一受信附件 capability");
  assert.match(boundaryResult("attachment-source-read"), /产物 URI 不属于当前 Agent SessionThread/);
  for (const callId of ["attachment-uri-write", "attachment-alias-write", "attachment-alias-edit"]) {
    assert.equal(JSON.parse(boundaryResult(callId)).code, "capability_denied");
  }
  return completed;
}

async function runCopyAttachmentLifecycle(surface, owners) {
  await enableAttachmentCapabilityTools(surface);
  const facts = [];
  for (const phase of ["copy.attachments.preparing", "copy.attachments.owner_reserved", "copy.attachments.before_finalize"]) {
    const input = await createAttachmentSource(surface, `公开 copy 附件恢复 ${phase}`, owners);
    const { source, attachment, blob, buffer } = input;
    const before = await forkHistoryEvidence(source);
    const request = await freezeForkRequest(source);
    const operationId = forkOperationId(request);
    const barrier = { operation_id: operationId, phase };
    await testControl({ action: "arm", ...barrier });
    const requestSurface = await copyRequestSurface(source);
    const pending = requestFork(source, request, undefined, requestSurface).then((value) => ({ value }), (error) => ({ error: String(error) }));
    await testControl({ action: "acknowledged", ...barrier });
    const captured = await readCopyCapture(surface, operationId);
    const claims = await copyAttachmentClaims(surface, operationId);
    assert.equal(claims.length, 1);
    const claim = claims[0], snapshot = JSON.parse(claim.snapshot_json);
    assert.equal(snapshot.required, true);
    assert.equal(snapshot.availability, "available");
    assert.equal(snapshot.source_file_id, attachment.file_id);
    assert.equal(snapshot.blob_id, blob.blob_id);
    assert.equal(claim.state, phase.endsWith(".preparing") ? "preparing" : "owner_reserved");
    assert.equal(claim.target_owner_state, phase.endsWith(".preparing") ? null : "active");
    const target = { ...source, sessionId: captured.target_session_id, threadId: captured.target_thread_id };
    const targetFile = snapshot.target_file_id;
    let sourceDelete;
    if (!phase.endsWith(".preparing")) {
      sourceDelete = await deleteOwner(source);
      const gc = (await testControl({ action: "events" })).findLast((event) => event.operation_id === source.sessionId && event.phase === "attachments.gc.completed");
      assert.ok(gc);
      assert.ok(!gc.details.collected_blob_ids.includes(blob.blob_id), "已提交 claim 必须阻止 source 删除后的正文 GC");
      assert.deepEqual(await readFile(`${source.workspace}/.boxteam/attachments/${blob.relative_locator}`), buffer);
    }
    if (phase === "copy.attachments.before_finalize") {
      const publishedSession = await ownerApiResponse(surface, `/api/v1/sessions/${target.sessionId}`);
      assert.equal(publishedSession.status, 200);
      assert.equal(publishedSession.body.data.session_id, target.sessionId,
        "附件 finalize 前 target Session 必须已发布");
    } else {
      const missingSession = await ownerApiResponse(surface, `/api/v1/sessions/${target.sessionId}`);
      assert.equal(missingSession.status, 404);
      assert.equal(missingSession.body.detail, `Session ${target.sessionId} not found`);
    }
    const missingAttachment = await ownerApiResponse(target,
      `/api/v1/sessions/${target.sessionId}/attachments/content?${new URLSearchParams({ file_id: targetFile, variant: "original" })}`);
    assert.equal(missingAttachment.status, 404);
    assert.equal(missingAttachment.body.detail, phase === "copy.attachments.before_finalize"
      ? `附件引用尚未完成发布: ${targetFile}`
      : `Session ${target.sessionId} not found`);
    await drainPageResponseBodies(requestSurface.page);
    await requestSurface.page.close();
    const reopen = await replaceNodePage(surface, owners);
    const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await testControl({ action: "release", ...barrier });
    const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    assert.notEqual(killed.before_pid, restarted.after_pid);
    assert.ok((await pending).error);
    await reopen();
    const settledClaims = await copyAttachmentClaims(surface, operationId);
    assert.equal(settledClaims.length, 1, "恢复只按原 claim ID 收敛，不能借用其它 copy 或添加第二个 claim");
    assert.equal(settledClaims[0].claim_id, claim.claim_id);
    assert.equal(settledClaims[0].snapshot_json, claim.snapshot_json);
    assert.equal(settledClaims[0].preimage_hash, claim.preimage_hash);
    let published, settlement, tool, capabilityBoundary;
    if (phase.endsWith(".preparing")) {
      assert.equal(settledClaims[0].state, "aborted");
      assert.equal(settledClaims[0].target_owner_state, null);
      assert.equal((await readCopyCapture(surface, operationId)).state, "aborted");
      assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [target.sessionId]), []);
      assert.deepEqual(await readCatalog(surface,
        "SELECT state,session_id,main_thread_id FROM session_creation_records WHERE session_creation_idempotency_key=?", [operationId]),
      [{ state: "aborted", session_id: target.sessionId, main_thread_id: target.threadId }]);
      assert.deepEqual(Buffer.from((await readAttachmentContent(source, attachment.file_id)).bytes), buffer);
      await finishForkDeadline(forkOperationDeadlines.get(request.idempotency_key));
      sourceDelete = await deleteOwner(source);
    } else {
      assert.equal(settledClaims[0].state, "committed");
      assert.equal(settledClaims[0].target_owner_state, "active");
      const fork = (await requestFork(source, request, undefined, surface)).data;
      published = await publishedForkOwner(source, request, fork, before);
      owners.push(published.owner);
      settlement = await readCopyTargetControl(surface, captured, "SELECT * FROM copy_attachment_settlements WHERE copy_operation_id=?", [operationId]);
      assert.equal(settlement.length, 1);
      assert.equal(settlement[0].state, "committed");
      assert.deepEqual(JSON.parse(settlement[0].claim_ids_json), [claim.claim_id]);
      assert.equal((await readCopyTargetControl(surface, captured, "SELECT state FROM session_operation_leases WHERE lease_id=?", [settlement[0].lease_id]))[0].state, "completed");
      assert.deepEqual(Buffer.from((await readAttachmentContent(published.owner, targetFile)).bytes), buffer);
      const card = await locateAttachmentCard(published.owner, published.targetHistory,
        targetFile, attachment.name, "available");
      assert.equal(await card.getByRole("button", { name: `在右侧打开附件：${attachment.name}`, exact: true }).isEnabled(), true);
      tool = await completeOwnerTurn(published.owner, `用 read_file 读取本会话附件 ${targetFile}，确认独占事实。`, [
        toolResponse(`copy-attachment-read-${phase}`, "read_file", { path: targetFile }), finalResponse(`copy-attachment-read-final-${phase}`, "副本附件 capability 已实际读取。"),
      ]);
      const resultPart = tool.turn.response_parts.find((part) => part.kind === "tool_result");
      assert.ok(JSON.stringify(resultPart).includes(source.sessionId), "正常工具 capability 必须读到原正文独占事实");
      await assertToolResultContinuation(published.owner, tool.calls, `copy-attachment-read-${phase}`, resultPart);
      capabilityBoundary = await runAttachmentCapabilityBoundary(published.owner, source,
        targetFile, attachment.file_id, phase);
      assert.deepEqual(Buffer.from((await readAttachmentContent(published.owner, targetFile)).bytes), buffer, "全部被拒写入后正文必须保持逐字节一致");
      await deleteOwner(published.owner);
    }
    const finalClaims = await copyAttachmentClaims(surface, operationId);
    assert.equal(finalClaims[0].state, phase.endsWith(".preparing") ? "aborted" : "target_deleted");
    const blobs = await readAttachmentSql(surface, "SELECT * FROM attachment_blobs WHERE digest=?", [blob.digest]);
    assert.equal(blobs.length, 1, "同一正文全程只能保留一个 blob-id");
    assert.equal(blobs[0].blob_id, blob.blob_id);
    assert.equal(blobs[0].availability, "tombstoned", "所有 owner/claim 终结后不得留下可用正文泄漏");
    facts.push({ phase, source: source.sessionId, captured, claims, sourceDelete, killed, restarted, settledClaims, settlement,
      published: published ? { sessionId: published.owner.sessionId, history: published.targetHistory, displayed: published.displayed } : null, tool, capabilityBoundary, finalClaims, blobs });
  }
  return facts;
}

async function runCopyAttachmentDeletionRace(surface, owners) {
  const facts = [];
  for (const finalizerFirst of [true, false]) {
    const { source, attachment, blob, buffer } = await createAttachmentSource(surface, `copy finalizer/delete ${finalizerFirst}`, owners);
    const request = await freezeForkRequest(source);
    const operationId = forkOperationId(request);
    const initial = { operation_id: operationId, phase: finalizerFirst ? "copy.attachments.published_pending" : "copy.attachments.before_finalize" };
    const committing = { operation_id: operationId, phase: "copy.attachments.claims_committed" };
    await testControl({ action: "arm", ...initial });
    if (finalizerFirst) await testControl({ action: "arm", ...committing });
    const requestSurface = await copyRequestSurface(source);
    const requestPageId = requestSurface.page.diagnosticId;
    const requestDocumentId = requestSurface.page.diagnosticDocumentId;
    const requestStartedAt = Date.now();
    let settled = false;
    // 本场景故意关闭目标读取准入；以原 POST 与 owner 提交凭据核验 fork 终态。
    const pending = requestForkHttpResponse(source, request, requestSurface).then(
      ({ response }) => { settled = true; return { value: response }; },
      (error) => { settled = true; return { error: String(error) }; },
    );
    await testControl({ action: "acknowledged", ...initial });
    assert.equal(settled, false, "原 POST 必须等待仍持有屏障的附件 finalizer");
    const capture = await readCopyCapture(surface, operationId);
    const claims = await copyAttachmentClaims(surface, operationId);
    assert.equal(claims.length, 1);
    assert.equal(claims[0].state, "owner_reserved");
    const snapshot = JSON.parse(claims[0].snapshot_json);
    const target = { ...source, sessionId: capture.target_session_id, threadId: capture.target_thread_id };
    owners.push(target);
    assert.equal((await readCatalog(surface, "SELECT state FROM nodes WHERE node_id=?", [target.sessionId]))[0].state, "active");
    const publishedSettlement = await readCopyTargetControl(surface, capture, "SELECT * FROM copy_attachment_settlements WHERE copy_operation_id=?", [operationId]);
    assert.equal(publishedSettlement.length, 1);
    assert.equal(publishedSettlement[0].state, "published_pending_attachment_commit");
    const deletion = await prepareNavigationIntent(target, { kind: "delete_session", target_node_id: target.sessionId });
    const catalogDeleting = { operation_id: deletion.operationId, phase: "deletion.catalog_deleting" };
    const drainWaiting = { operation_id: deletion.operationId, phase: "deletion.drain_waiting/" + target.sessionId };
    const fenceClosed = { operation_id: deletion.operationId, phase: "deletion.fence_closed/" + target.sessionId };
    let deletionCatalogAt = null;
    let initialReleasedAt = null;
    let responseReceivedAt = null;
    let deletionDrainReleasedAt = null;
    let rejection = null;
    let successResponse = null;
    await testControl({ action: "arm", ...fenceClosed });
    if (!finalizerFirst) {
      await testControl({ action: "arm", ...catalogDeleting });
      await testControl({ action: "arm", ...drainWaiting });
    }
    await enqueueNavigationIntent(target, deletion);
    let claimsBeforeRelease, settlementBeforeRelease;
    if (finalizerFirst) {
      await testControl({ action: "release", ...initial });
      await testControl({ action: "acknowledged", ...committing });
      assert.equal(settled, false, "原 POST 必须等待已 committed 的 finalizer 退出 gate");
      claimsBeforeRelease = await copyAttachmentClaims(surface, operationId);
      assert.equal(claimsBeforeRelease[0].state, "committed");
      settlementBeforeRelease = await readCopyTargetControl(surface, capture, "SELECT * FROM copy_attachment_settlements WHERE copy_operation_id=?", [operationId]);
      assert.equal(settlementBeforeRelease[0].state, "committed");
      assert.deepEqual(await readCopyTargetControl(surface, capture, "SELECT state FROM session_operation_leases WHERE lease_id=?", [settlementBeforeRelease[0].lease_id]), [{ state: "completed" }]);
      assert.equal(claimsBeforeRelease[0].target_owner_state, "active", "finalizer 持有 target gate 时删除不得提前释放附件 owner");
      assert.deepEqual(await readCopyTargetControl(surface, capture, "SELECT state FROM lifecycle_fence"), [{ state: "active" }], "删除 drain 必须等原 finalizer 成功终态退出 gate");
      await testControl({ action: "release", ...committing });
      const reply = await pending;
      assert.equal(settled, true);
      assert.equal(reply.error, undefined, reply.error);
      assert.equal(reply.value.status, 200);
      assert.equal(reply.value.backendState, "completed");
      assert.equal(reply.value.body.data.session.session_id, target.sessionId);
      assert.equal(reply.value.body.data.thread_id, target.threadId);
      successResponse = reply.value;
    } else {
      await testControl({ action: "acknowledged", ...catalogDeleting });
      assert.equal((await readCatalog(surface, "SELECT state FROM nodes WHERE node_id=?", [target.sessionId]))[0].state, "deleting");
      deletionCatalogAt = Date.now();
      assert.equal(settled, false);
      await testControl({ action: "release", ...catalogDeleting });
      await testControl({ action: "acknowledged", ...drainWaiting });
      assert.equal(settled, false);
      // 删除尚未持排空 gate；先让原 POST 实际经过 deleting 校验，再核删除终态和重启。
      await testControl({ action: "release", ...initial });
      initialReleasedAt = Date.now();
      const reply = await pending;
      responseReceivedAt = Date.now();
      assert.equal(settled, true);
      assert.equal(reply.error, undefined, reply.error);
      assert.equal(reply.value.status, 409, "删除先行后的原 POST 必须收到真实业务冲突");
      assert.equal(reply.value.body.detail,
        `Session catalog 节点非 active，拒绝附件准入/发布: session_id=${target.sessionId}, state='deleting'`);
      assert.equal(reply.value.body.request_id, reply.value.requestId);
      rejection = reply.value;
      const observedResponses = conflictResponses.filter((response) => response.requestId === rejection.requestId);
      assert.ok(observedResponses.length <= 1, "同一 fork 409 request ID 不得重复登记");
      if (observedResponses.length === 0) {
        conflictResponses.push({
          url: new URL(`/api/v1/sessions/${source.sessionId}/fork-context`, requestSurface.page.url()).toString(),
          method: "POST", status: rejection.status, pageId: requestPageId, documentId: requestDocumentId,
          requestedAt: requestStartedAt, receivedAt: responseReceivedAt, kind: "fork_context",
          sessionId: source.sessionId, workspaceHeader: requestSurface.gatewayWorkspaceId,
          requestId: rejection.requestId, body: rejection.body, forkRequest: structuredClone(request),
        });
      } else {
        assert.deepEqual(observedResponses[0].forkRequest, request, "fork 409 response 必须绑定真实请求正文");
      }
      await testControl({ action: "release", ...drainWaiting });
      deletionDrainReleasedAt = Date.now();
    }
    await testControl({ action: "acknowledged", ...fenceClosed });
    const deletedClaims = await copyAttachmentClaims(surface, operationId);
    assert.deepEqual(deletedClaims.map((claim) => [claim.claim_id, claim.state, claim.target_owner_state]), [[claims[0].claim_id, "target_deleted", "released"]]);
    const deletedSettlement = await readCopyTargetControl(surface, capture, "SELECT * FROM copy_attachment_settlements WHERE copy_operation_id=?", [operationId]);
    assert.deepEqual(deletedSettlement, [{ ...publishedSettlement[0], state: "target_deleted" }]);
    const lease = await readCopyTargetControl(surface, capture, "SELECT state FROM session_operation_leases WHERE lease_id=?", [publishedSettlement[0].lease_id]);
    assert.deepEqual(lease, [{ state: finalizerFirst ? "completed" : "cancelled" }], "删除必须消费 finalizer 成功终态，或取消原 reserved owner lease");
    await drainPageResponseBodies(requestSurface.page);
    await requestSurface.page.close();
    const reopen = await replaceNodePage(surface, owners);
    const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await testControl({ action: "release", ...fenceClosed });
    const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
    assert.notEqual(killed.before_pid, restarted.after_pid);
    await reopen();
    const terminal = await navigationTerminal(target, deletion);
    assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [target.sessionId]), []);
    assert.deepEqual(await copyAttachmentClaims(surface, operationId), deletedClaims, "重启不得复活已释放 reserved/committed target owner");
    assert.deepEqual(Buffer.from((await readAttachmentContent(source, attachment.file_id)).bytes), buffer);
    const sourceDelete = await deleteOwner(source);
    const blobs = await readAttachmentSql(surface, "SELECT * FROM attachment_blobs WHERE digest=?", [blob.digest]);
    assert.equal(blobs.length, 1);
    assert.equal(blobs[0].availability, "tombstoned");
    assert.equal((await readAttachmentSql(surface, "SELECT COUNT(*) AS count FROM attachment_owner_refs WHERE blob_id=? AND state='active'", [blob.blob_id]))[0].count, 0);
    facts.push({ finalizerFirst, sourceSessionId: source.sessionId, sourceThreadId: source.threadId,
      targetSessionId: target.sessionId, targetThreadId: target.threadId, workspaceId: requestSurface.gatewayWorkspaceId,
      operationId, request: structuredClone(request), requestPageId, requestDocumentId, requestStartedAt,
      response: rejection, successResponse, responseReceivedAt, initialBarrier: initial,
      deletionCatalogBarrier: finalizerFirst ? null : catalogDeleting, deletionCatalogAt,
      initialReleasedAt, deletionDrainBarrier: finalizerFirst ? null : drainWaiting, deletionDrainReleasedAt,
      capture, claims, publishedSettlement, deletion, claimsBeforeRelease, settlementBeforeRelease, deletedClaims,
      deletedSettlement, lease, killed, restarted, terminal, sourceDelete, blobs });
  }
  return facts;
}

async function runCopyMissingAttachment(surface, owners) {
  const historyTiming = { targetTurnId: null, events: [] };
  const progress = { scenario: result.runningScenario, historyTiming };
  (result.copyMissingAttachmentProgress ??= []).push(progress);
  const recordPhase = (phase, operation) => measureHistoryPhase(historyTiming, phase, operation);
  const input = await createAttachmentSource(surface, "公开 copy required 与 optional 正文缺失", owners, {
    turnOptions: { verifyHistory: false, openBeforeSend: true },
  });
  const { source, material, attachment, blob } = input;
  const blobPath = `${source.workspace}/.boxteam/attachments/${blob.relative_locator}`;
  const requiredRequest = await freezeForkRequest(source);
  await unlink(blobPath);
  const requiredOperation = forkOperationId(requiredRequest);
  const missing = await requestForkResponse(source, requiredRequest);
  progress.requiredMissingResponse = {
    status: missing.status, requestId: missing.requestId, body: missing.body,
  };
  assert.equal(missing.status, 500, "required 正文真实缺失必须使整个公开 copy 明确失败");
  assert.equal(missing.body.code, 500);
  assert.equal(missing.body.data, null);
  assert.equal(missing.body.message, `FileNotFoundError: [Errno 2] No such file or directory: '${blobPath}'`);
  const failedCapture = await readCopyCapture(surface, requiredOperation);
  assert.equal(failedCapture.state, "aborted");
  assert.deepEqual(await readCatalog(surface, "SELECT node_id FROM nodes WHERE node_id=?", [failedCapture.target_session_id]), []);
  assert.deepEqual(await copyAttachmentClaims(surface, requiredOperation), []);
  assert.equal((await readCatalog(surface, "SELECT state FROM session_creation_records WHERE session_creation_idempotency_key=?", [requiredOperation]))[0].state, "aborted");
  assert.equal((await readAttachmentSql(source, "SELECT availability FROM attachment_blobs WHERE blob_id=?", [blob.blob_id]))[0].availability, "available", "真实字节缺失输入不得改写 catalog 业务事实");
  await finishForkDeadline(forkOperationDeadlines.get(requiredRequest.idempotency_key));
  const tools = (await ownerApi(surface, "/api/v1/tools?agent_id=default")).data;
  const compactTool = tools.find((tool) => tool.name === "compact_conversation");
  assert.ok(compactTool);
  await ownerApi(surface, "/api/v1/tools/selection", { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "default", changes: [{ tool_id: compactTool.tool_id, execution_enabled: true, model_visible: true }] }) });
  // 附件放在稳定缓存前缀之后，确保真实压缩归纳的是附件所在中段。
  const optionalInput = await recordPhase("copy_missing_attachment.setup_prefix_and_attachment", () =>
    createAttachmentSource(surface, "optional source 保留历史与真实 compaction", owners, {
      prefixTurns: 4, turnOptions: { verifyHistory: false, openBeforeSend: true },
    }));
  const earlier = optionalInput.source, optionalTurn = optionalInput.material, optionalAttachment = optionalInput.attachment, optionalBlob = optionalInput.blob;
  const seeds = [];
  for (let index = 0; index < 3; index += 1) seeds.push(await completeOwnerTurn(earlier,
    `附件后的上下文素材 ${index}：${"这段详细过程必须由真实压缩归纳。".repeat(160)}`, [
      toolResponse(`optional-seed-${index}`, "read_file", { path: "README.md" }),
      finalResponse(`optional-seed-final-${index}`, `第 ${index} 轮素材已核对。${"详细过程只作为旧上下文。".repeat(160)}`),
    ], [], { verifyHistory: false, openBeforeSend: false }));
  const compact = await completeOwnerTurn(earlier, "压缩旧上下文，历史附件保留在用户历史中。", [
    { ...toolResponse("optional-compact-real", "compact_conversation", {}), usage: { input: 100000, output: 16 } },
    { label: "optional-compact-summary", text: "之前已上传独占附件并核对 README，详细过程已完成。" },
    finalResponse("optional-compact-final", "旧上下文已经真实压缩，用户历史仍保留附件引用。"),
  ], [], { historyTiming, openBeforeSend: false });
  historyTiming.targetTurnId = compact.turn.turn_id;
  assert.ok(compact.turn.response_parts.some((part) => part.kind === "compaction_summary"));
  const visible = (await ownerApi(earlier, `/api/v1/sessions/${earlier.sessionId}/messages`)).data.items;
  assert.ok(visible.some((message) => message.attachments.some((ref) => ref.file_id === optionalAttachment.file_id)), "压缩不得从完整可见消息中删掉历史附件");
  const before = await forkHistoryEvidence(earlier);
  const attachmentRoot = before.turns.find((turn) => turn.turn_id === optionalTurn.turn.turn_id)?.root_input_item_id;
  assert.ok(attachmentRoot);
  const attachmentMember = before.members.find((member) => member.item_id === attachmentRoot);
  assert.ok(attachmentMember, "历史附件 root 必须仍是冻结 view 的可见成员");
  assert.deepEqual([attachmentMember.context_selected, attachmentMember.context_ordinal], [0, null], "被压缩的附件 root 必须退出当前模型 selection");
  const compactRequest = await modelRequest(compact.calls.at(-1));
  assert.ok(!JSON.stringify(compactRequest.request.messages).includes(optionalAttachment.file_id), "真实压缩后首次 Provider 输入不得再带历史附件");
  assert.deepEqual(await readRollout(earlier,
    "SELECT selection.ref_id FROM context_assembly_selections selection JOIN model_calls call ON call.assembly_id=selection.assembly_id WHERE call.model_call_id=? AND selection.included=1 AND selection.ref_type='canonical_item' AND selection.ref_id=?",
    [compact.calls.at(-1), attachmentRoot]), [], "首次后继 Provider 的封存 selection 必须排除旧附件 root");
  const optionalRequest = await freezeForkRequest(earlier);
  const optionalOperation = forkOperationId(optionalRequest);
  const optionalPoint = (await ownerApi(earlier, `/api/v1/sessions/${earlier.sessionId}/fork-source-point?${new URLSearchParams({
    thread_id: earlier.threadId, mode: "full_rollout_copy", source_view_id: optionalRequest.source_view_id,
    source_view_revision: String(optionalRequest.source_view_revision),
  })}`)).data;
  assert.deepEqual(optionalPoint.resources.filter((resource) => resource.resource_uri === optionalAttachment.file_id)
    .map((resource) => [resource.availability, resource.required_for_context]), [["available", false]], "真实 fork 候选必须按冻结 selection 将历史附件列为 optional");
  assert.ok(before.history.items.some((turn) => turn.user_messages.some((message) => message.attachments.some((ref) => ref.file_id === optionalAttachment.file_id))), "历史仍必须有实际附件引用");
  await unlink(`${earlier.workspace}/.boxteam/attachments/${optionalBlob.relative_locator}`);
  const fork = (await requestFork(earlier, optionalRequest)).data;
  const target = await publishedForkOwner(earlier, optionalRequest, fork, before);
  owners.push(target.owner);
  const mappings = await readAttachmentSql(surface,
    "SELECT source_file_id,snapshot_json FROM fork_attachment_mappings WHERE copy_operation_id=? AND target_session_id=? AND target_thread_id=? ORDER BY source_file_id",
    [optionalOperation, target.owner.sessionId, target.owner.threadId]);
  const unavailable = mappings.map((row) => ({ source_file_id: row.source_file_id, snapshot: JSON.parse(row.snapshot_json) }))
    .filter(({ source_file_id, snapshot }) => source_file_id === snapshot.source_file_id
      && snapshot.source_file_id === optionalAttachment.file_id)
    .map(({ snapshot }) => snapshot);
  assert.equal(unavailable.length, 1);
  assert.equal(unavailable[0].required, false);
  assert.equal(unavailable[0].availability, "unavailable");
  assert.equal(unavailable[0].claim_id, null);
  assert.deepEqual(await copyAttachmentClaims(surface, optionalOperation), []);
  const history = await loadCompleteThreadHistory(target.owner);
  const targetFile = unavailable[0].target_file_id;
  const card = await locateAttachmentCard(target.owner, history, targetFile, optionalAttachment.name, "unavailable");
  assert.equal(await card.getByRole("status").innerText(), "附件不可用");
  assert.ok((await card.getByRole("button").evaluateAll((buttons) => buttons.map((button) => button.disabled))).every(Boolean));
  await openOwner(target.owner);
  const reopenedHistory = await loadCompleteThreadHistory(target.owner);
  const reopenedCard = await locateAttachmentCard(target.owner, reopenedHistory,
    targetFile, optionalAttachment.name, "unavailable");
  assert.equal(await reopenedCard.getByRole("status").innerText(), "附件不可用");
  const successorTurnTiming = {
    phase: "copy_missing_attachment.successor_turn",
    outcome: "started",
    started_at: new Date().toISOString(),
  };
  progress.successorTurnTiming = successorTurnTiming;
  const successorPhaseIdentity = {
    pid: process.pid, scenario: result.runningScenario,
    copy_operation_id: optionalOperation, idempotency_key: optionalRequest.idempotency_key,
    source_uri: optionalRequest.source_uri, target_uri: fork.thread_uri,
  };
  await writeFile(phasePath,
    `${successorTurnTiming.started_at} copy_missing_attachment_successor_begin ${JSON.stringify(successorPhaseIdentity)}\n`,
    { flag: "a" });
  const successorTurnStartedAt = performance.now();
  let successor;
  try {
    successor = await completeOwnerTurn(target.owner, "继续使用压缩后的上下文，不读取历史附件。", [
      finalResponse("optional-copy-successor", "副本只使用当前有效上下文完成了新一轮。"),
    ]);
    successorTurnTiming.outcome = "completed";
    successorTurnTiming.completed_at = new Date().toISOString();
    successorTurnTiming.duration_ms = Number((performance.now() - successorTurnStartedAt).toFixed(3));
  } catch (error) {
    successorTurnTiming.outcome = "failed";
    successorTurnTiming.failed_at = new Date().toISOString();
    successorTurnTiming.duration_ms = Number((performance.now() - successorTurnStartedAt).toFixed(3));
    throw error;
  } finally {
    await writeFile(phasePath,
      `${new Date().toISOString()} copy_missing_attachment_successor_end ${JSON.stringify({ ...successorPhaseIdentity,
        ...successorTurnTiming, turn_id: successor?.turn.turn_id, model_call_ids: successor?.calls })}\n`,
      { flag: "a" });
  }
  const successorRequest = await modelRequest(successor.calls[0]);
  assert.ok(!JSON.stringify(successorRequest.request.messages).includes(optionalAttachment.file_id), "副本首次 Provider 不得复活历史不可用附件");
  assert.ok(!JSON.stringify(successorRequest.request.messages).includes(targetFile), "副本首次 Provider 不得发送目标侧不可用附件引用");
  return { source: source.sessionId, material, missing, failedCapture, optionalSource: earlier.sessionId,
    optionalPrefix: optionalInput.prefix, optionalTurn, seeds, compact, attachmentMember, compactRequest, optionalPoint, unavailable, successor, successorRequest, successorTurnTiming,
    target: { sessionId: target.owner.sessionId, history: target.targetHistory, displayed: target.displayed }, history };
}

async function runLiveResourceForks(surface, owners) {
  const source = await createProductSession(surface, "真实运行资源的显式上下文分支");
  owners.push(source);
  await completeOwnerTurn(source, "建立运行资源 fork 的冻结来源。", [finalResponse("live-fork-source", "运行资源来源已完成。")]);
  const sourceRef = { session_id: source.sessionId, thread_id: source.threadId };
  const created = [];
  const get = async (resourceId) => (await ownerApi(source, `/api/v1/resources/${resourceId}`)).data;
  const command = async (resourceId, action, thread) => {
    const current = await get(resourceId);
    return (await ownerApi(source, `/api/v1/resources/${resourceId}/${action}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: current.owner_revision, ...(thread ? { thread } : {}) }),
    })).data;
  };
  for (const kind of ["terminal", "browser"]) {
    for (const retention_scope of ["workspace", "thread"]) {
      const resource = (await ownerApi(source, `/api/v1/resources/${kind === "terminal" ? "terminals" : "browsers"}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...sourceRef, retention_scope, title: `${kind} ${retention_scope} fork` }),
      })).data;
      assert.equal(resource.kind, kind);
      assert.equal(resource.workspace_id, source.workspaceIdentity);
      assert.deepEqual(resource.source_thread, sourceRef);
      assert.deepEqual(resource.thread_associations, [sourceRef]);
      assert.equal(resource.reconciliation_status, "verified");
      created.push(resource);
    }
  }
  const reusable = created.filter((resource) => resource.retention_scope === "workspace");
  const terminal = reusable.find((resource) => resource.kind === "terminal");
  const browser = reusable.find((resource) => resource.kind === "browser");
  assert.ok(Number.isInteger(terminal.metadata.os_pid) && terminal.metadata.os_pid > 0, "真实 Terminal 必须已启动 PTY");
  assert.ok(browser.metadata.runtime_generation && browser.metadata.active_page_id, "真实 Browser 必须已创建运行实例和页面");
  const browserRun = async (code, thread) => {
    const current = await get(browser.resource_id);
    return (await ownerApi(source, `/api/v1/resources/${browser.resource_id}/browser-operation`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: current.owner_revision, action: "run", payload: { code }, ...(thread ? { thread } : {}) }),
    })).data;
  };
  const marker = `fork-instance:${source.sessionId}`;
  assert.equal((await browserRun(`return await page.evaluate((value) => { window.__forkInstance = value; return value; }, ${JSON.stringify(marker)});`)).result, marker);
  const instance = (resource) => resource.kind === "terminal"
    ? [resource.metadata.os_pid, resource.metadata.process_start_time, resource.metadata.pty_worker_pid]
    : [resource.metadata.runtime_generation, resource.metadata.active_page_id];
  const instances = new Map(reusable.map((resource) => [resource.resource_id, instance(resource)]));
  const choice = (resource) => ({ owner_reference: { kind: resource.kind, resource_id: resource.resource_id,
    workspace_id: resource.workspace_id, expected_revision: resource.owner_revision }, action: "reuse_reference" });
  const countsQuery = "SELECT (SELECT COUNT(*) FROM nodes) AS nodes,(SELECT COUNT(*) FROM session_creation_records) AS sessions,(SELECT COUNT(*) FROM fork_source_captures) AS captures";
  const invalid = [];
  for (const resource of created.filter((resource) => resource.retention_scope === "thread")) {
    const request = await freezeForkRequest(source, { resources: [choice(resource)] });
    const before = await readCatalog(source, countsQuery);
    const rejected = await requestForkResponse(source, request);
    assert.equal(rejected.status, 400);
    assert.equal(rejected.backendState, "completed", "已返回明确资源前置拒绝后必须结束该 fork deadline");
    assert.equal(rejected.remainingMs, null, "明确资源前置拒绝完成后不能继续向 watchdog 暴露剩余时间");
    assert.match(rejected.body.detail, /^fork_live_resource_rejected:/);
    assert.deepEqual(await readCatalog(source, countsQuery), before, "thread 范围资源拒绝必须先于目标发布");
    assert.equal((await get(resource.resource_id)).retention_scope, "thread", "fork 不得自动 promote 原资源");
    invalid.push({ request, rejected: rejected.body });
  }
  const defaults = [];
  for (const creationKind of ["new_session_main", "new_child_thread"]) {
    const before = await Promise.all(reusable.map((resource) => get(resource.resource_id)));
    const fork = await forkOwner(source, { creationKind });
    owners.push(fork.owner);
    assert.deepEqual(fork.result.resources, [], "默认 fork 不得隐式关联任何 live 资源");
    for (const resource of before) {
      const current = await get(resource.resource_id);
      assert.equal(current.owner_revision, resource.owner_revision);
      assert.deepEqual(current.thread_associations, resource.thread_associations);
      assert.deepEqual(instance(current), instances.get(resource.resource_id));
    }
    defaults.push({ request: fork.request, result: fork.result });
  }
  const explicit = [];
  for (const [creationKind, partial] of [["new_session_main", false], ["new_child_thread", false], ["new_session_main", true]]) {
    await openOwner(source);
    const evidence = await forkHistoryEvidence(source);
    const pointPending = source.page.waitForResponse((response) => response.request().method() === "GET"
      && new URL(response.url()).pathname === `/api/v1/sessions/${source.sessionId}/fork-source-point`);
    await catalogRow(source, source.sessionId).click({ button: "right" });
    await source.page.getByRole("menuitem", { name: "从上下文创建分支", exact: true }).click();
    const pointReply = await pointPending;
    assert.equal(pointReply.status(), 200, await pointReply.text());
    const point = (await pointReply.json()).data;
    const dialog = source.page.getByRole("dialog", { name: "创建上下文分支", exact: true });
    await dialog.waitFor({ state: "visible" });
    for (const resource of created) {
      const candidate = point.live_resources.find((item) => item.resource_id === resource.resource_id);
      assert.ok(candidate?.metadata && Array.isArray(candidate.available_actions), "picker 必须消费真实 source-point 的完整资源事实");
      assert.deepEqual(candidate.source_thread, sourceRef);
      const checkbox = dialog.getByRole("checkbox", { name: `关联${resource.kind === "terminal" ? "终端" : "浏览器"} ${resource.resource_id}`, exact: true });
      assert.equal(await checkbox.isChecked(), false, "每次新请求默认不选运行资源");
      assert.equal(await checkbox.isDisabled(), resource.retention_scope === "thread");
      if (resource.retention_scope === "workspace") await checkbox.check();
    }
    await dialog.getByLabel("创建位置", { exact: true }).selectOption(creationKind);
    await waitUntil(async () => await dialog.getByRole("button", { name: "创建分支", exact: true }).isEnabled(), "live picker 的真实候选与目标容器收敛");
    const barrierId = `live-fork-partial:${source.sessionId}`;
    if (partial) await testControl({ action: "arm_match", barrier_id: barrierId,
      phase: "fork.live.before_attach.terminal", details: { resource_id: terminal.resource_id } });
    const submission = await beginForkUiSubmission(source.page, source, source, source.sessionId,
      (options) => dialog.getByRole("button", { name: "创建分支", exact: true }).click(options));
    if (partial) {
      await testControl({ action: "acknowledged_match", barrier_id: barrierId });
      await command(terminal.resource_id, "attach", sourceRef);
      await testControl({ action: "release_match", barrier_id: barrierId });
    }
    const { response: reply, request, payload, deadline: submissionDeadline } = await submission.waitForResponse();
    assert.equal(reply.status(), 200, JSON.stringify(payload));
    const fork = payload.data;
    result.forkRequests.push(structuredClone(request));
    assert.deepEqual(request.resources, point.live_resources.filter((resource) => reusable.some((item) => item.resource_id === resource.resource_id)).map(choice),
      "真实 Dialog 必须提交冻结 owner_revision 与 typed identity，不创建伪 URI");
    const live = fork.resources.filter((resource) => resource.owner_reference);
    assert.equal(live.length, 2);
    assert.deepEqual(live.map((resource) => [resource.owner_reference.kind, resource.operation_state, resource.availability]),
      [["terminal", partial ? "rejected" : "committed", partial ? "unavailable" : "available"], ["browser", "committed", "available"]]);
    const counts = await readCatalog(source, countsQuery);
    const revisions = await Promise.all(reusable.map(async (resource) => (await get(resource.resource_id)).owner_revision));
    if (partial) {
      await dialog.getByRole("alert").waitFor({ state: "visible" });
      assert.ok((await dialog.getByRole("alert").innerText()).includes(`目标已创建（${fork.thread_id}）`));
      assert.equal(submissionDeadline.backendCompleted, true,
        "终态 rejected receipt 必须结束首次 operation deadline");
      assert.equal(forkOperationDeadlines.has(request.idempotency_key), false,
        "终态 rejected receipt 完成后不得保留首个 deadline");
      const retrySubmission = await beginForkUiSubmission(source.page, source, source, source.sessionId,
        (options) => dialog.getByRole("button", { name: "重试原请求", exact: true }).click(options));
      const { response: retried, request: retriedRequest, payload: retriedPayload,
        backendState: retryBackendState, durableHistory: retryDurableHistory,
        deadline: retryDeadline } = await retrySubmission.waitForResponse();
      assert.equal(retried.status(), 200, JSON.stringify(retriedPayload));
      assert.equal(retryBackendState, "completed", "原 key 的 partial retry 必须返回可判定的终态");
      assert.deepEqual(retriedRequest, request, "部分失败重试必须使用原 key 与原 expected_revision");
      const retry = retriedPayload.data;
      assert.deepEqual([retry.session.session_id, retry.thread_id], [fork.session.session_id, fork.thread_id]);
      assert.deepEqual(retry.resources.map((resource) => [resource.owner_reference, resource.operation_state, resource.availability]),
        fork.resources.map((resource) => [resource.owner_reference, resource.operation_state, resource.availability]));
      assert.ok(retryDurableHistory, "partial retry 必须读回目标 Thread 的持久历史");
      assert.deepEqual([retryDurableHistory.sessionId, retryDurableHistory.threadId],
        [retry.session.session_id, retry.thread_id], "partial retry 必须读回同一目标 Thread 的持久历史");
      assert.equal(typeof retryDurableHistory.sourceViewId, "string", "持久目标历史必须返回实际读取 view");
      assert.ok(Number.isSafeInteger(retryDurableHistory.sourceViewRevision), "持久目标历史必须有可核验 view revision");
      assert.ok(retryDurableHistory.turnCount > 0, "持久目标历史必须包含来源 Turn");
      const retryTerminal = retry.resources.find((resource) => resource.owner_reference?.kind === "terminal");
      assert.ok(retryTerminal, "partial retry 必须返回原 Terminal 操作终态");
      assert.deepEqual([retryTerminal.operation_state, retryTerminal.availability], ["rejected", "unavailable"],
        "partial retry 只能在 live resource 的 rejected 终态得到确认后结束 deadline");
      await dialog.getByRole("alert").waitFor({ state: "visible" });
      assert.notEqual(retryDeadline, submissionDeadline, "终态拒绝之后的用户重试必须使用新 deadline");
      assert.equal(retryDeadline.backendCompleted, true, "重试响应的 terminal receipt 必须结束新 deadline");
      assert.equal(forkOperationDeadlines.has(request.idempotency_key), false,
        "用户重试完成后不得保留 active deadline");
      await dialog.getByRole("button", { name: "取消", exact: true }).click();
      await dialog.waitFor({ state: "hidden" });
    } else {
      await dialog.waitFor({ state: "hidden" });
      const retry = (await requestFork(source, request)).data;
      assert.deepEqual([retry.session.session_id, retry.thread_id], [fork.session.session_id, fork.thread_id]);
      assert.deepEqual(retry.resources.map((resource) => [resource.owner_reference, resource.operation_state, resource.committed_revision]),
        live.map((resource) => [resource.owner_reference, resource.operation_state, resource.committed_revision]));
    }
    assert.deepEqual(await readCatalog(source, countsQuery), counts, "重试不得创建第二个目标或 capture");
    assert.deepEqual(await Promise.all(reusable.map(async (resource) => (await get(resource.resource_id)).owner_revision)), revisions,
      "committed/rejected 原 operation 重试不得再 attach 或推进 owner revision");
    const published = await publishedForkOwner(source, request, fork, evidence);
    owners.push(published.owner);
    for (const resource of reusable) assert.deepEqual(instance(await get(resource.resource_id)), instances.get(resource.resource_id));
    if (!partial) assert.equal((await browserRun("return await page.evaluate(() => window.__forkInstance);",
      { session_id: published.owner.sessionId, thread_id: published.owner.threadId })).result, marker,
    "显选关联必须继续使用原 Playwright 页面的内存状态");
    explicit.push({ request, result: fork, target: published.owner });
  }
  const main = explicit[0];
  const child = explicit[1];
  await command(terminal.resource_id, "detach", { session_id: child.target.sessionId, thread_id: child.target.threadId });
  const detachedRevision = (await get(terminal.resource_id)).owner_revision;
  const detachedRetry = (await requestFork(source, child.request)).data;
  const detachedTerminal = detachedRetry.resources.find((resource) => resource.owner_reference?.kind === "terminal");
  assert.deepEqual([detachedTerminal.operation_state, detachedTerminal.availability, detachedTerminal.owner_resource.owner_revision],
    ["committed", "unavailable", detachedRevision], "后来 detach 不能令原 fork operation 再次 attach");
  const sourceDelete = await deleteOwner(source);
  for (const resource of reusable) {
    const current = await get(resource.resource_id);
    assert.deepEqual(current.source_thread, sourceRef);
    assert.equal(current.retention_scope, "workspace");
    assert.equal(current.reconciliation_status, "verified");
    assert.deepEqual(instance(current), instances.get(resource.resource_id), "来源删除后工作区资源仍使用原实例");
    assert.ok(current.thread_associations.some((ref) => ref.session_id !== source.sessionId));
  }
  assert.equal((await browserRun("return await page.evaluate(() => window.__forkInstance);",
    { session_id: main.target.sessionId, thread_id: main.target.threadId })).result, marker);
  const afterDeleteRetry = (await requestFork(source, main.request)).data;
  assert.deepEqual([afterDeleteRetry.session.session_id, afterDeleteRetry.thread_id], [main.target.sessionId, main.target.threadId]);
  // 验收结束后通过原 owner 显式停止实例；避免影响后续已有的冷恢复场景。
  const stopped = [];
  for (const resource of reusable) stopped.push(await command(resource.resource_id, "stop"));
  return { source: source.sessionId, created, instances: [...instances], invalid, defaults,
    explicit: explicit.map(({ request, result }) => ({ request, result })), detachedRetry, sourceDelete, afterDeleteRetry, stopped };
}

async function runPublicForks(surfaces) {
  const surface = surfaces.a;
  const source = await createProductSession(surface, "公开 copy 与 pinned retention");
  const material = await completeOwnerTurn(source, "读取 README.md，为独立副本建立已完成的上下文。", [
    toolResponse("copy-source-read", "read_file", { path: "README.md" }), finalResponse("copy-source-final", "公开 copy 的源上下文已经完成。"),
  ]);
  const owners = [source];
  const liveResources = await runLiveResourceForks(surface, owners);
  const remoteForks = [];
  const catalogCounts = "SELECT (SELECT COUNT(*) FROM nodes) AS nodes,(SELECT COUNT(*) FROM session_creation_records) AS creations,"
    + "(SELECT COUNT(*) FROM fork_source_captures) AS captures,(SELECT COUNT(*) FROM fork_retention_claims) AS claims";
  const contextCounts = "SELECT (SELECT COUNT(*) FROM item_catalog) AS items,(SELECT COUNT(*) FROM executions) AS executions,"
    + "(SELECT COUNT(*) FROM context_assemblies) AS assemblies,(SELECT COUNT(*) FROM retention_refs) AS retention_refs";
  for (const remoteSurface of [surfaces.b, surfaces.c]) {
    const remote = await createProductSession(remoteSurface, "真实跨工作区上下文分支");
    await completeOwnerTurn(remote, "建立可正常读取的已完成源上下文。", [
      finalResponse(`remote-fork-${remote.name}-final`, "本工作区的源上下文已完成。"),
    ]);
    const remoteHistory = await forkHistoryEvidence(remote);
    const copiedModes = [];
    for (const mode of ["context_fork", "view_prefix_fork", "full_rollout_copy"]) {
      const fork = await forkOwner(remote, { mode }, surface);
      owners.push(fork.owner);
      assert.equal(fork.result.export_release.state, "confirmed");
      assert.deepEqual((await forkHistoryEvidence(remote)).history.items, remoteHistory.history.items, "导出不得改写源历史");
      const operationId = forkOperationId(fork.request);
      assertForkSourceOwner(fork.request.source_uri, remote, surface.gatewayId);
      assertForkSourceOwner(fork.result.source_uri, remote, surface.gatewayId);
      assertForkSourceOwner(fork.result.thread_uri, fork.owner, surface.gatewayId);
      const catalogBeforeRetry = await readCatalog(surface, catalogCounts);
      const captureBeforeRetry = await readCopyCapture(surface, operationId);
      assert.deepEqual([captureBeforeRetry.target_session_id, captureBeforeRetry.target_thread_id],
        [fork.owner.sessionId, fork.owner.threadId]);
      const creationBeforeRetry = await readCatalog(surface,
        "SELECT session_creation_idempotency_key,session_id FROM session_creation_records WHERE session_creation_idempotency_key=?", [operationId]);
      assert.equal(creationBeforeRetry.length, 1, "首次远端 copy 必须只创建一个目标 session");
      assert.equal(creationBeforeRetry[0].session_id, fork.owner.sessionId);
      const retry = await requestFork(remote, fork.request, undefined, surface);
      assert.deepEqual(retry.data, fork.result, "跨 Gateway 用同一冻结请求重试必须返回完整且相同的 fork 结果");
      assertForkSourceOwner(retry.data.source_uri, remote, surface.gatewayId);
      assertForkSourceOwner(retry.data.thread_uri, fork.owner, surface.gatewayId);
      assert.deepEqual(await readCatalog(surface, catalogCounts), catalogBeforeRetry,
        "同请求重试不得重复创建目标、capture 或 retention claim");
      assert.deepEqual(await readCopyCapture(surface, operationId), captureBeforeRetry,
        "同请求重试必须复用唯一且未改变目标身份的 capture");
      assert.deepEqual(await readCatalog(surface,
        "SELECT session_creation_idempotency_key,session_id FROM session_creation_records WHERE session_creation_idempotency_key=?", [operationId]),
      creationBeforeRetry, "同请求重试不得重复创建 session");
      const successor = await completeOwnerTurn(fork.owner, `跨端 ${mode} 的首次新输入。`, [finalResponse(`remote-${remote.name}-${mode}-successor`, "目标使用已导入的本地上下文继续执行。")]);
      const wire = (await modelRequest(successor.calls[0])).request.messages;
      assert.ok(JSON.stringify(wire).includes("建立可正常读取的已完成源上下文。"));
      copiedModes.push({ mode, request: fork.request, result: fork.result, successor, history: fork.targetHistory });
    }
    const pinnedRequest = await freezeForkRequest(remote, { mode: "context_fork", relationship: "pinned" }, surface);
    const localCatalogBefore = await readCatalog(surface, catalogCounts);
    const remoteCatalogBefore = await readCatalog(remote, catalogCounts);
    const remoteContextBefore = await readRollout(remote, contextCounts);
    const rejected = await requestForkResponse(remote, pinnedRequest, surface);
    assert.equal(rejected.status, 400);
    assert.match(rejected.body.detail, /^fork_remote_retention_unsupported:/);
    assert.deepEqual(await readCatalog(surface, catalogCounts), localCatalogBefore, "远端 pinned 拒绝不能创建目标或claim");
    assert.deepEqual(await readCatalog(remote, catalogCounts), remoteCatalogBefore, "远端 pinned 拒绝不能修改来源catalog");
    assert.deepEqual(await readRollout(remote, contextCounts), remoteContextBefore, "远端 pinned 拒绝不能修改来源上下文");
    remoteForks.push({ source: remote.resource, copiedModes, pinnedRequest, rejected: rejected.body, localCatalogBefore, remoteCatalogBefore, remoteContextBefore });
  }
  const detached = await forkOwner(source);
  const copyOfCopy = await forkOwner(detached.owner);
  owners.push(detached.owner, copyOfCopy.owner);
  const deletingRequests = [];
  for (const options of [{ mode: "context_fork", relationship: "pinned" }, { mode: "full_rollout_copy" }]) {
    deletingRequests.push({ options, request: await freezeForkRequest(detached.owner, options) });
  }
  const deleteFirst = await prepareNavigationIntent(detached.owner, { kind: "delete_session", target_node_id: detached.owner.sessionId });
  const deleteFirstBarrier = { operation_id: deleteFirst.operationId, phase: "deletion.catalog_deleting" };
  await testControl({ action: "arm", ...deleteFirstBarrier });
  await enqueueNavigationIntent(detached.owner, deleteFirst);
  await testControl({ action: "acknowledged", ...deleteFirstBarrier });
  const admissionCounts = "SELECT (SELECT COUNT(*) FROM session_creation_records WHERE json_extract(session_metadata_json,'$.context_source_session_id')=?) AS creations,"
    + "(SELECT COUNT(*) FROM fork_source_captures WHERE source_session_id=?) AS captures,(SELECT COUNT(*) FROM fork_retention_claims WHERE source_session_id=?) AS claims";
  const countOwner = [detached.owner.sessionId, detached.owner.sessionId, detached.owner.sessionId];
  const rejectedCounts = await readCatalog(surface, admissionCounts, countOwner);
  const deletingForks = [];
  for (const { options, request } of deletingRequests) {
    const rejected = await requestForkResponse(detached.owner, request);
    assert.equal(rejected.status, 409);
    assertSessionDeletionPending(rejected.body, detached.owner.sessionId);
    assert.deepEqual(await readCatalog(surface, admissionCounts, countOwner), rejectedCounts, "删除先行不能创建 target/capture/claim");
    deletingForks.push({ options, rejected: rejected.body });
  }
  await testControl({ action: "release", ...deleteFirstBarrier });
  const deletedBeforeFork = await navigationTerminal(detached.owner, deleteFirst);
  const before = await forkHistoryEvidence(source);
  const pinnedRequest = await freezeForkRequest(source, { mode: "context_fork", relationship: "pinned" });
  const operationId = forkOperationId(pinnedRequest);
  const forkId = await testControl({ action: "next_uuid", namespace: "fork/identity" });
  const claimBarrier = { operation_id: operationId, phase: "fork.retention.claimed" };
  await testControl({ action: "arm", ...claimBarrier });
  const pending = requestFork(source, pinnedRequest).then((value) => ({ value }), (error) => ({ error: String(error) }));
  await testControl({ action: "acknowledged", ...claimBarrier });
  const capture = (await readCatalog(source, "SELECT * FROM fork_source_captures WHERE copy_operation_id=?", [operationId]))[0];
  assert.equal(capture.fork_id, forkId);
  assert.equal(capture.state, "preparing", "pinned claim 必须先于 source capture 提交");
  const claim = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE fork_retention_claim_id=?", [forkId]))[0];
  assert.equal(claim.state, "preparing");
  assert.equal(claim.source_session_id, source.sessionId);
  assert.equal(claim.target_session_id, capture.target_session_id);
  const sourceLease = await readControl(source, "SELECT * FROM session_operation_leases WHERE operation_identity=?", ["fork-retention:" + forkId]);
  assert.equal(sourceLease.length, 1);
  assert.equal(sourceLease[0].operation_kind, "fork_retention");
  assert.equal(sourceLease[0].state, "active");
  assert.equal(sourceLease[0].captured_lifecycle_generation, claim.source_lifecycle_generation);
  assert.equal(sourceLease[0].preimage_hash, capture.preimage_hash);
  assert.equal(sourceLease[0].recovery_ref, capture.copy_operation_id);
  const blocker = await deleteOwner(source, "rejected");
  assert.ok(JSON.stringify(blocker).includes(forkId), "preparing pin 必须在整树 deleting 前返回具体 claim blocker");
  const active = (await ownerApi(source, `/api/v1/sessions/${source.sessionId}`)).data;
  assert.equal(active.session_id, source.sessionId);
  assert.equal((await sessionCatalogNode(source)).state, "active");
  await testControl({ action: "release", ...claimBarrier });
  const firstReply = await pending;
  assert.equal(firstReply.error, undefined, firstReply.error);
  const pinned = await publishedForkOwner(source, pinnedRequest, firstReply.value.data, before);
  owners.push(pinned.owner);
  const pinnedDelete = await deleteOwner(pinned.owner);

  const recoveringRequest = await freezeForkRequest(source, { mode: "context_fork", relationship: "pinned" });
  const recoveringOperation = forkOperationId(recoveringRequest);
  const commitBarrier = { operation_id: recoveringOperation, phase: "fork.target_committed" };
  await testControl({ action: "arm", ...commitBarrier });
  const interruptedFork = requestFork(source, recoveringRequest).then((value) => ({ value }), (error) => ({ error: String(error) }));
  await testControl({ action: "acknowledged", ...commitBarrier });
  const recoveringCapture = (await readCatalog(source, "SELECT * FROM fork_source_captures WHERE copy_operation_id=?", [recoveringOperation]))[0];
  const originalClaim = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE fork_retention_claim_id=?", [recoveringCapture.fork_id]))[0];
  assert.equal(originalClaim.state, "preparing");
  const committedEvent = (await testControl({ action: "events" })).find((event) => event.operation_id === recoveringOperation && event.phase === commitBarrier.phase);
  const targetJournal = await testControl({ action: "read_sql", name: surface.name,
    relative_path: `.boxteam/${committedEvent.details.target_index_relative_locator}`,
    query: "SELECT * FROM fork_materializations WHERE materialization_id=?", parameters: [recoveringCapture.materialization_id] });
  assert.equal(targetJournal.length, 1);
  assert.equal(targetJournal[0].status, "target_committed");
  assert.equal(targetJournal[0].fork_id, originalClaim.fork_retention_claim_id);
  const reopen = await replaceNodePage(surface, owners);
  const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
  await testControl({ action: "release", ...commitBarrier });
  const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
  assert.notEqual(killed.before_pid, restarted.after_pid);
  assert.ok((await interruptedFork).error, "target publication 前退出必须使原 HTTP 明确失败");
  await reopen();
  const recoveredResult = (await requestFork(source, recoveringRequest)).data;
  const recovered = await publishedForkOwner(source, recoveringRequest, recoveredResult, before);
  owners.push(recovered.owner);
  const activatedClaim = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE fork_retention_claim_id=?", [recoveringCapture.fork_id]))[0];
  assert.deepEqual({ ...activatedClaim, state: originalClaim.state, record_updated_at: originalClaim.record_updated_at }, originalClaim,
    "target_committed 恢复只能激活原 claim，不能替换 frozen owner/generation");
  assert.equal(activatedClaim.state, "active");
  assert.equal((await readCatalog(source, "SELECT COUNT(*) AS count FROM fork_source_captures WHERE target_session_id=?", [recovered.owner.sessionId]))[0].count, 1);
  await deleteOwner(recovered.owner);

  const releaseRecoveries = [];
  const sourceNode = await sessionCatalogNode(source);
  for (const phase of ["fork.retention.release.prepared", "fork.retention.release.source_ref_released", "fork.retention.release.source_released"]) {
    const target = await forkOwner(source, { mode: "context_fork", relationship: "pinned" });
    owners.push(target.owner);
    const targetNode = await sessionCatalogNode(target.owner);
    const retention = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE target_session_id=?", [target.owner.sessionId]))[0];
    const deletion = await prepareNavigationIntent(target.owner, { kind: "delete_session", target_node_id: target.owner.sessionId });
    const barrier = { operation_id: retention.fork_retention_claim_id, phase };
    const fenceBarrier = { operation_id: deletion.operationId, phase: "deletion.fence_closed/" + target.owner.sessionId };
    await testControl({ action: "arm", ...barrier });
    await testControl({ action: "arm", ...fenceBarrier });
    await enqueueNavigationIntent(target.owner, deletion);
    await testControl({ action: "acknowledged", ...barrier });
    assert.equal((await readCatalog(source, "SELECT state FROM nodes WHERE node_id=?", [target.owner.sessionId]))[0].state, "deleting");
    const preparing = await readFrozenControl(target.owner, targetNode, "SELECT * FROM fork_retention_releases");
    assert.equal(preparing.length, 1);
    assert.equal(preparing[0].state, "preparing");
    assert.equal(preparing[0].fork_retention_claim_id, retention.fork_retention_claim_id);
    assert.equal(preparing[0].source_thread_id, source.threadId);
    const pausedClaim = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE fork_retention_claim_id=?", [retention.fork_retention_claim_id]))[0];
    assert.equal(pausedClaim.state, phase.endsWith(".source_released") ? "released" : "active");
    const reopenRelease = await replaceNodePage(surface, owners);
    const releaseKilled = await testControl({ action: "process", name: surface.name, operation: "kill" });
    await testControl({ action: "release", ...barrier });
    const restartingRelease = testControl({ action: "process", name: surface.name, operation: "start" });
    await testControl({ action: "acknowledged", ...fenceBarrier });
    const committed = await readFrozenControl(target.owner, targetNode, "SELECT * FROM fork_retention_releases");
    assert.deepEqual(committed, [{ ...preparing[0], state: "committed" }], "恢复必须收敛同一冻结 release record");
    const finalClaim = (await readCatalog(source, "SELECT * FROM fork_retention_claims WHERE fork_retention_claim_id=?", [retention.fork_retention_claim_id]))[0];
    assert.equal(finalClaim.state, "released");
    assert.equal(finalClaim.release_reason, preparing[0].release_reason);
    const finalLease = await readFrozenControl(source, sourceNode,
      "SELECT operation_kind,preimage_hash,captured_lifecycle_generation,recovery_ref,state FROM session_operation_leases WHERE operation_identity=?", ["fork-retention:" + retention.fork_retention_claim_id]);
    assert.deepEqual(finalLease, [{ operation_kind: "fork_retention", preimage_hash: preparing[0].capture_preimage_hash,
      captured_lifecycle_generation: retention.source_lifecycle_generation, recovery_ref: preparing[0].copy_operation_id, state: "completed" }]);
    assert.deepEqual(await readFrozenRollout(source, sourceNode,
      "SELECT owner_session_id,status FROM retention_refs WHERE reference_kind='fork' AND reference_id=?", [retention.fork_retention_claim_id]),
    [{ owner_session_id: target.owner.sessionId, status: "released" }]);
    await testControl({ action: "release", ...fenceBarrier });
    const releaseRestarted = await restartingRelease;
    assert.notEqual(releaseKilled.before_pid, releaseRestarted.after_pid);
    await reopenRelease();
    const terminal = await navigationTerminal(target.owner, deletion);
    assert.deepEqual(await readCatalog(source, "SELECT node_id FROM nodes WHERE node_id=?", [target.owner.sessionId]), []);
    releaseRecoveries.push({ phase, deletion, preparing, pausedClaim, committed, finalClaim, finalLease, releaseKilled, releaseRestarted, terminal });
  }
  const captureLifecycle = await runCopyCaptureLifecycle(surface, owners, source.sessionId);
  const attachmentLifecycle = await runCopyAttachmentLifecycle(surface, owners);
  const attachmentDeletionRace = await runCopyAttachmentDeletionRace(surface, owners);
  const missingAttachment = await runCopyMissingAttachment(surface, owners);
  const sourceDelete = await deleteOwner(source);
  await openOwner(copyOfCopy.owner);
  assert.deepEqual((await loadThreadHistory(copyOfCopy.owner)).items, copyOfCopy.targetHistory.items, "detached copy 不得回读已删除 source");
  const successor = await completeOwnerTurn(copyOfCopy.owner, "来源已删除，请继续使用副本中导入的上下文。", [
    finalResponse("deleted-source-copy-successor", "副本已在来源删除后独立继续。"),
  ]);
  const successorRequest = await modelRequest(successor.calls[0]);
  const successorMessages = successorRequest.request.messages;
  for (const text of [material.turn.user_messages[0].content, "公开 copy 的源上下文已经完成。", successor.sent.prompt]) {
    assert.ok(JSON.stringify(successorMessages).includes(text), "删除来源后的真实 Provider 请求必须消费导入正文和目标新输入");
  }
  const sourceToolMessages = (await modelRequest(material.calls.at(-1))).request.messages
    .filter((message) => message.role === "tool" && message.tool_call_id === "copy-source-read");
  assert.equal(sourceToolMessages.length, 1);
  assert.deepEqual(successorMessages.filter((message) => message.role === "tool" && message.tool_call_id === "copy-source-read"),
    sourceToolMessages, "删除来源后的副本必须独立读取原工具结果正文");
  const successorOwner = await readRollout(copyOfCopy.owner,
    "SELECT model_call_id,session_id,thread_id FROM model_calls WHERE model_call_id=?", [successor.calls[0]]);
  assert.deepEqual(successorOwner, [{ model_call_id: successor.calls[0], session_id: copyOfCopy.owner.sessionId,
    thread_id: copyOfCopy.owner.threadId }], "删除来源后的新 ModelCall 必须归属于副本当前 owner");
  return { source: source.sessionId, material, remoteForks, detached: { sessionId: detached.owner.sessionId, history: detached.targetHistory },
    copyOfCopy: { sessionId: copyOfCopy.owner.sessionId, history: copyOfCopy.targetHistory,
      successor, successorRequest, successorOwner },
    pinned: { sessionId: pinned.owner.sessionId, history: pinned.targetHistory }, deleteFirst: { deleteFirst, rejectedCounts, deletingForks, deletedBeforeFork },
    claim, sourceLease, capture, blocker, pinnedDelete,
    liveResources, targetCommittedRecovery: { recoveringCapture, originalClaim, activatedClaim, targetJournal, killed, restarted,
      target: recovered.owner.sessionId, history: recovered.targetHistory }, releaseRecoveries, captureLifecycle, attachmentLifecycle, attachmentDeletionRace, missingAttachment, sourceDelete };
}

async function runCopyAttachmentFocus(surface) {
  const owners = [];
  const lifecycle = await runCopyAttachmentLifecycle(surface, owners);
  const missing = await runCopyMissingAttachment(surface, owners);
  return { lifecycle, missing };
}

async function runSessionPageSelection(surface) {
  const owners = [];
  for (let index = 0; index < 22; index += 1) {
    owners.push(await createProductSession(surface, `会话分页选择 ${index}`));
  }
  const firstPage = (await ownerApi(surface, "/api/v1/sessions")).data;
  assert.equal(firstPage.has_more, true);
  assert.equal(firstPage.items.length, 20);
  const visibleIds = new Set(firstPage.items.map((session) => session.session_id));
  const owner = owners.find((entry) => !visibleIds.has(entry.sessionId));
  assert.ok(owner, "必须选择真实首页以外的会话");
  await openOwner(owner);
  await waitUntil(async () => await catalogRow(owner, owner.sessionId).getAttribute("aria-current") === "true",
    "首页外会话成为当前选择");
  const refreshed = owner.page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/v1/sessions"
      && response.request().method() === "GET" && response.status() === 200, { timeout: 30_000 });
  const first = await completeOwnerTurn(owner, "在首页外的会话完成首轮。", [
    finalResponse("off-page-first", "首轮仍属于选中的会话。"),
  ], [], { openBeforeSend: false, verifyHistory: false });
  const refreshedPage = (await (await refreshed).json()).data;
  assert.equal(refreshedPage.has_more, true);
  assert.ok(!refreshedPage.items.some((session) => session.session_id === owner.sessionId));
  assert.equal(await catalogRow(owner, owner.sessionId).getAttribute("aria-current"), "true",
    "活动刷新收到首页后不得把首页外会话当作已删除");
  const second = await completeOwnerTurn(owner, "不重新选择会话，继续发送第二轮。", [
    finalResponse("off-page-second", "第二轮仍属于原会话。"),
  ], [], { openBeforeSend: false, verifyHistory: false });
  assert.equal(first.turn.ordinal, 1);
  assert.equal(second.turn.ordinal, 2);
  const history = await loadCompleteThreadHistory(owner);
  assert.deepEqual(history.items.map((turn) => turn.turn_id), [first.turn.turn_id, second.turn.turn_id]);
  const cleanup = [];
  for (const entry of owners) cleanup.push(await deleteOwner(entry));
  return { sessionId: owner.sessionId, firstPage: firstPage.items.map((session) => session.session_id),
    first, refreshedPage: refreshedPage.items.map((session) => session.session_id), second, history, cleanup };
}

async function runOptionalSeedRevisit(surface) {
  const owners = [];
  const input = await createAttachmentSource(surface, "optional seed history revisit", owners, { prefixTurns: 4 });
  const seeds = [];
  let measurement = null;
  for (let index = 0; index < 3; index += 1) {
    let historyTiming = null;
    if (index === 2) {
      const target = seeds[0].turn;
      assert.equal(target.ordinal, 6, "optional-seed-0 必须是第六轮回访目标");
      assert.equal(seeds[1].turn.ordinal, 7, "第三个 seed 前必须已完成第七轮");
      historyTiming = { targetTurnId: target.turn_id, events: [] };
      measurement = {
        owner: { sessionId: input.source.sessionId, threadId: input.source.threadId, workspaceId: input.source.gatewayWorkspaceId },
        targetTurnId: target.turn_id,
        targetRootLogicalItemOrdinal: target.root_logical_item_ordinal,
        historyTiming,
      };
      // 先挂到结果上，第三轮 live/reload 校准失败时也保留已采集计时。
      result.section8.optional_seed_revisit = measurement;
    }
    const seed = await completeOwnerTurn(input.source,
      `附件后的上下文素材 ${index}：${"这段详细过程必须由真实压缩归纳。".repeat(160)}`, [
        toolResponse(`optional-seed-${index}`, "read_file", { path: "README.md" }),
        finalResponse(`optional-seed-final-${index}`, `第 ${index} 轮素材已核对。${"详细过程只作为旧上下文。".repeat(160)}`),
      ], [], { historyTiming });
    seeds.push(seed);
    if (index === 2) assert.equal(seed.turn.ordinal, 8, "第三个 seed 必须完成第八轮");
  }
  return { ...measurement, source: input.source.sessionId, prefix: input.prefix, material: input.material, seeds };
}

async function runRealCompaction(surface) {
  const tools = (await ownerApi(surface, "/api/v1/tools?agent_id=default")).data;
  const compact = tools.find((tool) => tool.name === "compact_conversation");
  assert.ok(compact, "正常 ToolSet catalog 必须提供 compact_conversation");
  await ownerApi(surface, "/api/v1/tools/selection", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "default", changes: [{ tool_id: compact.tool_id, execution_enabled: true, model_visible: true }] }),
  });
  const historyTool = tools.find((tool) => tool.name === "read_compaction_history");
  assert.ok(historyTool, "正常 ToolSet catalog 必须提供压缩历史读取工具");
  await ownerApi(surface, "/api/v1/tools/selection", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "default", changes: [{ tool_id: historyTool.tool_id, execution_enabled: true, model_visible: true }] }),
  });
  const owner = await createProductSession(surface, "真实 compact_conversation");
  const prepared = [];
  for (let index = 0; index < 3; index += 1) prepared.push(await completeOwnerTurn(owner,
    `压缩素材 ${index}：${"保留具体源事实，后续摘要只需一段。".repeat(160)}`, [
      toolResponse(`compact-seed-${index}`, "read_file", { path: "README.md" }),
      finalResponse(`compact-seed-final-${index}`, `第 ${index} 轮已确认 README。${"这是待压缩的详细过程。".repeat(160)}`),
    ]));
  const activationQuery = "SELECT a.assembly_id,a.turn_id,b.activation_snapshot_id,b.plan_hash,b.request_hash,b.bindings_hash,b.activation_provenance_hash,a.snapshot_json FROM context_assemblies a JOIN resource_activation_assembly_bindings b ON b.assembly_id=a.assembly_id WHERE a.status IN ('sealed','terminal') ORDER BY a.assembly_id";
  const frozenBefore = await readRollout(owner, activationQuery);
  assert.ok(frozenBefore.length >= prepared.length, "压缩前每个真实 Turn 必须已有封存资源绑定");
  const compactCall = { ...toolResponse("compact-real-tool", "compact_conversation", {}), usage: { input: 100000, output: 16 } };
  const actual = await completeOwnerTurn(owner, "压缩前文的详细过程，然后保留简短结果。", [
    compactCall, { label: "compact-summary-provider", text: "前面三轮均通过 read_file 核对 README，事实一致。" },
    toolResponse("compact-read-history", "read_compaction_history", { line_offset: 1, limit: 2000 }),
    finalResponse("compact-after-final", "真实压缩已完成，已通过归档工具读回原始消息。"),
  ]);
  const history = await loadThreadHistory(owner);
  const turn = history.items.at(-1);
  const summaries = turn.response_parts.filter((part) => part.kind === "compaction_summary");
  assert.equal(summaries.length, 1, "真实压缩只能添加一个独立 compaction_summary");
  assert.ok(summaries[0].text.includes("read_compaction_history(line_offset=1, limit=200)"),
    "摘要必须指向模型可执行的精确 thread 归档读取工具");
  assert.ok(!summaries[0].text.includes("session-artifacts/") && !summaries[0].text.includes("history.md"),
    "模型与持久 history 摘要不得携带可推导的归档位置");
  const archiveRead = turn.response_parts.find((part) => part.kind === "tool_result"
    && part.tool_name === "read_compaction_history");
  assert.ok(archiveRead, "模型必须实际调用工具取回归档原文");
  const archiveText = JSON.stringify(archiveRead);
  assert.ok(archiveText.includes("压缩素材 1") && archiveText.includes("第 1 轮已确认 README"),
    "工具结果必须含已摘要的原始用户消息与回答");
  // 第 0 轮属于保留的缓存前缀，归档只收录实际移出的中段。
  const compactedRequest = await modelRequest(actual.calls[2]);
  const compactedMessages = JSON.stringify(compactedRequest.request.messages);
  assert.ok(compactedMessages.includes("压缩素材 0") && compactedMessages.includes("第 0 轮已确认 README"),
    "压缩后的模型请求必须保留第 0 轮缓存前缀");
  assert.ok(!compactedMessages.includes("压缩素材 1") && !compactedMessages.includes("第 1 轮已确认 README"),
    "归档读取必须取回已从模型请求移出的中段原文");
  assert.ok(!archiveText.includes("压缩素材 0"), "缓存前缀不得被当作已压缩中段重复归档");
  const expandable = turn.response_parts.filter((part) => part.kind !== "final_text");
  assert.equal(turn.activity_stats.item_count, expandable.length);
  assert.equal(turn.activity_stats.item_count, expandable.filter((part) => part.kind !== "compaction_summary").length + 1);
  assert.ok(turn.response_parts.some((part) => part.kind === "tool_call" && part.tool_name === "compact_conversation"));
  assert.ok(turn.response_parts.some((part) => part.kind === "tool_result"));
  const display = owner.page.locator(`.chat-turn[data-turn-id="${turn.turn_id}"] details.chat-compaction-summary`);
  assert.equal(await display.count(), 1);
  await display.locator("summary").click();
  const sources = JSON.parse(await display.getAttribute("data-response-sources"));
  assert.deepEqual(sources.map(partCoordinate), summaries.map(partCoordinate));
  assert.ok((await display.innerText()).includes("README"));
  const controls = await readRollout(owner,
    "SELECT event.checkpoint_id,event.payload_json FROM control_events event JOIN checkpoints checkpoint ON checkpoint.checkpoint_id=event.checkpoint_id WHERE event.control_kind='checkpoint_created' AND checkpoint.checkpoint_kind='compaction' ORDER BY event.control_sequence");
  assert.equal(controls.length, 1, "一次真实压缩必须提交唯一 compaction checkpoint control event");
  const [control] = controls;
  const refs = JSON.parse(control.payload_json).resource_activation_refs;
  assert.ok(Array.isArray(refs) && refs.length >= frozenBefore.length);
  assert.equal(new Set(refs.map((ref) => ref.assembly_id)).size, refs.length);
  const frozenAtRestart = await readRollout(owner, activationQuery);
  const frozenById = new Map(frozenAtRestart.map((row) => [row.assembly_id, row]));
  for (const ref of refs) {
    const { snapshot_json: _snapshot, ...binding } = frozenById.get(ref.assembly_id);
    assert.deepEqual(ref, binding, "control 引用必须逐字段对应原封存 assembly");
  }
  for (const row of frozenBefore) {
    assert.deepEqual(frozenById.get(row.assembly_id), row, "压缩不得改写旧 assembly bytes");
    assert.ok(refs.some((ref) => ref.assembly_id === row.assembly_id), "被压缩的原资源引用不能丢失");
  }
  const reopen = await replaceNodePage(surface, [owner]);
  const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
  const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
  assert.notEqual(killed.before_pid, restarted.after_pid);
  await reopen();
  await openOwner(owner);
  const frozenAfter = await readRollout(owner, activationQuery);
  assert.deepEqual(frozenAfter, frozenAtRestart, "硬重启只能恢复原封存 activation/assembly");
  const restoredHistory = await loadThreadHistory(owner);
  assert.deepEqual(restoredHistory.items, history.items, "压缩后的历史在实际 backend 重启后保持原身份/顺序/统计");
  const evidence = await verifyOwnerTurns(owner);
  return { sessionId: owner.sessionId, threadId: owner.threadId, prepared, actual, summary: summaries[0], archiveRead,
    compactedRequest: compactedRequest.identity, activity: turn.activity_stats,
    activationRecovery: { frozenBefore, frozenAtRestart, frozenAfter, control, refs, killed, restarted, evidence } };
}

async function runBackgroundMailboxRecovery(surface) {
  const main = await createProductSession(surface, "后台任务与子线程邮箱恢复");
  const childThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  await planChatCalls(main.sessionId, childThread, [finalResponse("background-child-ready", "后台任务子线程已就绪。")]);
  await completeOwnerTurn(main, "创建负责后台时间消息的持久子线程。", [
    toolResponse("background-delegate", "task", { description: "初始化后等待直接指令。", subagent_type: "general-purpose" }),
    finalResponse("background-delegated", "子线程已经创建。"),
    finalResponse("background-report", "子线程初始化报告已处理。"),
  ]);
  const child = { ...main, threadId: childThread, child: true };
  const childRuntime = async () => (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`))
    .data.items.find((item) => item.thread_id === child.threadId).runtime;
  await waitUntil(async () => {
    const items = (await ownerApi(main, `/api/v1/sessions/${main.sessionId}/child-threads`)).data.items;
    return items.some((item) => item.thread_id === childThread && ["completed", "completed_empty"].includes(item.report_status));
  }, "后台任务 child 初始 execution 完成", 120_000);
  const bindingBefore = await graphBinding(child);
  const sourceId = `background:${childThread}`;
  // 首条立即发送；第二条前杀死实际 backend，以真实 checkpoint 恢复而不改业务时钟。
  const emitted = await completeOwnerTurn(child, "每隔30秒发送两条系统时间消息。", [
    toolResponse("background-emit", "emit_system_time_messages", { source_id: sourceId, interval_seconds: 30, message_count: 2 }),
    finalResponse("background-emitting", "后台时间任务已启动。"),
  ], [], { verifyHistory: false });
  const emitResult = assertBoundToolResult(child, emitted.sent, emitted.turn, emitted.calls[0], "background-emit", "emit_system_time_messages");
  const taskId = JSON.parse(emitResult.result).task_id;
  const node = await sessionCatalogNode(child);
  const taskRows = () => readFrozenControl(child, node,
    "SELECT * FROM background_tasks WHERE task_id=?", [taskId]);
  const mailboxRows = () => readFrozenControl(child, node,
    "SELECT message_id,session_id,thread_id,agent_id,source_id,message_json FROM background_mailbox ORDER BY sequence");
  let beforeTask;
  await waitUntil(async () => {
    [beforeTask] = await taskRows();
    return beforeTask?.status === "running" && JSON.parse(beforeTask.checkpoint_json).next_index === 1;
  }, "真实 emitting tool 已提交首条消息和 checkpoint");
  const firstMailbox = await mailboxRows();
  assert.deepEqual(firstMailbox.map((item) => item.message_id), [`${taskId}:message:0`]);
  await testControl({ action: "advance_residency", seconds: 1800 });
  const blockedRuntime = await childRuntime();
  assert.equal(blockedRuntime.residency, "resident");
  assert.ok(blockedRuntime.blockers.some((blocker) => blocker.kind === "background_task"));
  const blockerDisplay = child.page.locator(`.child-thread-item[data-thread-id="${childThread}"] .child-thread-runtime`);
  await waitUntil(async () => {
    const text = await blockerDisplay.innerText();
    return blockedRuntime.blockers.every((blocker) => text.includes(blocker.reason));
  }, "右侧 child 列表展示真实非空后台任务 blocker");
  const blockerText = await blockerDisplay.innerText();
  assert.ok(!blockerText.includes(taskId) && !blockerText.includes(sourceId), "blocker 不得泄露内部任务或来源身份");
  const blockerEvidence = { text: blockerText, reasons: blockedRuntime.blockers.map((blocker) => blocker.reason) };
  const [activeLease] = await readFrozenControl(child, node,
    "SELECT lease_id,state,operation_identity FROM session_operation_leases WHERE lease_id=?", [beforeTask.lease_id]);
  assert.equal(activeLease.state, "active");
  assert.equal(activeLease.operation_identity, taskId);
  const graphBefore = await graphLifecycle(child);
  const reopen = await replaceNodePage(surface, [main, child]);
  const killed = await testControl({ action: "process", name: surface.name, operation: "kill" });
  const restarted = await testControl({ action: "process", name: surface.name, operation: "start" });
  assert.notEqual(killed.before_pid, restarted.after_pid);
  await reopen();
  let afterTask;
  await waitUntil(async () => {
    [afterTask] = await taskRows();
    return afterTask?.status === "completed";
  }, "backend 重启沿原任务 checkpoint 完成第二条消息", 120_000);
  assert.equal(afterTask.task_id, beforeTask.task_id);
  assert.equal(afterTask.lease_id, beforeTask.lease_id);
  assert.equal(afterTask.descriptor_json, beforeTask.descriptor_json);
  assert.ok(afterTask.fencing_token > beforeTask.fencing_token, "实际恢复必须领取新 token");
  assert.equal(JSON.parse(afterTask.checkpoint_json).next_index, 2);
  const mailbox = await mailboxRows();
  const messageIds = [0, 1].map((index) => `${taskId}:message:${index}`);
  assert.deepEqual(mailbox.map((item) => item.message_id), messageIds, "跨进程恢复不能重复逻辑消息");
  for (const message of mailbox) {
    assert.equal(message.session_id, child.sessionId);
    assert.equal(message.thread_id, child.threadId, "后台输出不得落入 main thread");
    assert.equal(message.source_id, sourceId);
  }
  const [settledLease] = await readFrozenControl(child, node,
    "SELECT lease_id,state,operation_identity FROM session_operation_leases WHERE lease_id=?", [afterTask.lease_id]);
  assert.equal(settledLease.state, "completed");
  const resources = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/resources`)).data.items;
  const resource = resources.find((item) => item.resource_id === taskId);
  assert.equal(resource.status, "completed");
  assert.equal(resource.metadata.thread_id, child.threadId);
  const coldRuntime = await childRuntime();
  assert.equal(coldRuntime.residency, "cold");
  const graphCold = await graphLifecycle(child);
  assert.equal(graphCold.compiled.length, graphBefore.compiled.length, "冷任务/资源读取不得构建 graph");
  const bindingAfter = await graphBinding(child);
  assert.deepEqual(bindingAfter, bindingBefore);

  const label = "background-collect";
  const calls = await planChatCalls(child.sessionId, child.threadId, [
    toolResponse(label, "collect_background_messages", { source_id: sourceId, timeout_seconds: 1, poll_interval_seconds: 0.01 }),
    finalResponse("background-collected", "恢复后的两条时间消息已收集。"),
  ]);
  const consumptionId = softwareOperation("background_collect", child, calls[0], label);
  const barrier = { operation_id: `${calls[0]}:tool-call:${label}`, phase: "canonical.tool_result.before_commit" };
  await testControl({ action: "arm", ...barrier });
  await openOwner(child);
  const sent = await sendOwnerPrompt(child, "收集本子线程的后台时间消息。");
  await testControl({ action: "acknowledged", ...barrier });
  const consumptionRows = () => readFrozenControl(child, node,
    "SELECT * FROM background_consumptions WHERE consumption_id=?", [consumptionId]);
  const [consumptionBefore] = await consumptionRows();
  assert.equal(consumptionBefore.acknowledged, 0, "工具返回但 canonical 未提交时禁止 ack");
  const batch = JSON.parse(consumptionBefore.batch_json);
  assert.equal(batch.thread_id, child.threadId);
  assert.deepEqual(batch.messages.map((item) => item.message_id), messageIds);
  const canonicalResults = () => readRollout(child,
    "SELECT item_id,semantic_kind,status,tool_call_id,projection_manifest_json FROM item_catalog WHERE semantic_kind='tool_result' AND json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=?", [calls[0]]);
  const canonicalBefore = await canonicalResults();
  assert.deepEqual(canonicalBefore, []);
  await testControl({ action: "release", ...barrier });
  await waitForJobStatus(child, sent.jobId);
  await waitForPlannedExecutions(child, calls);
  const turn = await assertAcceptedTurn(child, sent);
  const collected = assertBoundToolResult(child, sent, turn, calls[0], label, "collect_background_messages");
  assert.deepEqual(JSON.parse(collected.result), batch, "canonical ToolResult 必须保留原冻结 batch");
  const [consumptionAfter] = await consumptionRows();
  assert.equal(consumptionAfter.acknowledged, 1);
  assert.equal(consumptionAfter.batch_json, consumptionBefore.batch_json);
  const canonicalAfter = await canonicalResults();
  // stream attempt 与 checkpoint ToolMessage 是既有的两类载体；同类不能重复，公开投影只选一条。
  const carrierKinds = canonicalAfter.map((item) => {
    const identity = JSON.parse(item.projection_manifest_json).projection_identity;
    assert.equal(identity.model_call_id, calls[0]);
    assert.equal(item.status, "completed");
    assert.ok([label, barrier.operation_id].includes(item.tool_call_id));
    return identity.projection_message_id ? "checkpoint" : "attempt";
  });
  assert.equal(new Set(carrierKinds).size, canonicalAfter.length, "同一精确 tool call 的同类 canonical 载体不能重复");
  assert.equal(canonicalAfter.filter((item) => item.item_id === collected.source.item_id).length, 1,
    "唯一公开 ToolResult 必须精确关联已提交 canonical identity");
  const history = await verifyOwnerTurns(child);
  const mainHistory = await loadThreadHistory(main);
  assert.ok(mainHistory.items.every((item) => item.response_parts.every((part) =>
    !["emit_system_time_messages", "collect_background_messages"].includes(part.tool_name))), "main history 不得混入 child 工具");
  const graphCollected = await graphLifecycle(child);
  assert.equal(graphCollected.compiled.length, graphBefore.compiled.length + 1, "真实消费才重新构建 child graph");
  assert.deepEqual(await graphBinding(child), bindingBefore);
  return { sessionId: main.sessionId, mainThreadId: main.threadId, threadId: child.threadId, taskId,
    beforeTask, afterTask, firstMailbox, mailbox, activeLease, settledLease, blockedRuntime, blockerEvidence, coldRuntime,
    resource, killed, restarted, bindingBefore, bindingAfter, consumptionBefore, consumptionAfter,
    canonicalBefore, canonicalAfter, projectedResultItemId: collected.source.item_id, batch, collectedBatch: JSON.parse(collected.result),
    graphCounts: { before: graphBefore.compiled.length, cold: graphCold.compiled.length, collected: graphCollected.compiled.length },
    emittingTurnId: emitted.sent.turnId, collectingTurnId: sent.turnId, history };
}

async function runRuntimeSharedResources(surface) {
  const parent = await createProductSession(surface, "卸载 scope 按保留范围释放资源并保持共享来源");
  const threadId = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  const initialCalls = await planChatCalls(parent.sessionId, threadId, [
    toolResponse("scope-child-read", "read_file", { path: "README.md" }),
    finalResponse("scope-child-ready", "子线程已读取 README.md。"),
  ]);
  await completeOwnerTurn(parent, "创建独立子线程读取 README.md。", [
    toolResponse("scope-delegate", "task", { description: "读取 README.md 后等待。", subagent_type: "general-purpose" }),
    finalResponse("scope-created", "子线程已创建。"),
    finalResponse("scope-report", "子线程报告已收到。"),
  ]);
  const child = { ...parent, threadId, child: true };
  await waitForIdleChildRuntime(child);
  const tracked = await completeOwnerTurn(parent, "持续跟踪 gateway-context Skill。", [
    toolResponse("scope-track", "skill_load", { name: "gateway-context", mode: "tracked" }),
    finalResponse("scope-tracked", "共享来源已跟踪。"),
  ]);
  const tracking = await readRollout(parent, "SELECT tracking_status FROM context_source_control_states WHERE session_id=? AND thread_id=? AND source_kind='skill' AND name='gateway-context'", [parent.sessionId, parent.threadId]);
  assert.deepEqual(tracking, [{ tracking_status: "tracked" }]);
  const thread = { session_id: child.sessionId, thread_id: child.threadId };
  const getResource = async (id) => (await ownerApi(child, `/api/v1/resources/${id}`)).data;
  const resources = [];
  for (const [kind, retentionScope] of [
    ["terminal", "thread"], ["browser", "thread"],
    ["terminal", "workspace"], ["browser", "workspace"],
  ]) {
    const resource = (await ownerApi(child, `/api/v1/resources/${kind === "terminal" ? "terminals" : "browsers"}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...thread, retention_scope: retentionScope, title: `scope-${retentionScope}-${kind}` }),
    })).data;
    assert.equal(resource.kind, kind);
    assert.deepEqual(resource.source_thread, thread);
    assert.equal(resource.retention_scope, retentionScope);
    assert.equal(resource.status, "running");
    assert.deepEqual(resource.thread_associations, [thread]);
    if (kind === "terminal") {
      assert.ok(Number.isInteger(resource.metadata.os_pid) && resource.metadata.os_pid > 0);
      assert.ok(resource.metadata.process_start_time, "临时与持久终端都必须真实启动PTY");
    } else {
      assert.ok(resource.metadata.runtime_generation && resource.metadata.active_page_id, "临时与持久浏览器都必须真实启动页面");
    }
    resources.push(resource);
  }
  const terminal = resources.find((entry) => entry.kind === "terminal" && entry.retention_scope === "workspace");
  const browserResource = resources.find((entry) => entry.kind === "browser" && entry.retention_scope === "workspace");
  assert.ok(Number.isInteger(terminal.metadata.os_pid) && terminal.metadata.os_pid > 0);
  assert.ok(browserResource.metadata.runtime_generation && browserResource.metadata.active_page_id);
  const browserRun = async (code) => {
    const current = await getResource(browserResource.resource_id);
    return (await ownerApi(child, `/api/v1/resources/${current.resource_id}/browser-operation`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: current.owner_revision, action: "run", payload: { code } }),
    })).data;
  };
  const marker = `scope:${child.threadId}`;
  assert.equal((await browserRun(`return await page.evaluate((value) => { window.__scopeMarker = value; return value; }, ${JSON.stringify(marker)});`)).result, marker);
  const heldCalls = await planChatCalls(parent.sessionId, parent.threadId, [
    toolResponse("scope-consumer-held", "read_file", { path: "README.md" }),
    finalResponse("scope-consumer-final", "另一个来源消费者仍在原 graph 中。"),
  ]);
  const hold = { operation_id: `${heldCalls[0]}:tool-call:scope-consumer-held`, phase: "canonical.tool_result.before_commit" };
  const skillPath = join(parent.workspace, ".boxteam/skills/gateway-context/SKILL.md");
  const skillBytes = await readFile(skillPath);
  await testControl({ action: "arm", ...hold });
  let observation;
  let held;
  try {
    await openOwner(parent);
    held = await sendOwnerPrompt(parent, "保持本次读取活跃，核对另一个线程卸载后的来源更新。");
    await testControl({ action: "acknowledged", ...hold });
    const historyBefore = await loadThreadHistory(child);
    const bindingBefore = await graphBinding(child);
    const itemsBefore = await readRollout(child, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence");
    const epochsBefore = await readRollout(child, "SELECT * FROM context_epoch_transitions ORDER BY transition_id");
    const idle = await waitForIdleChildRuntime(child);
    await testControl({ action: "advance_residency", seconds: 1799 });
    assert.equal((await graphLifecycle(child)).closed.length, 0);
    await testControl({ action: "advance_residency", seconds: 1 });
    await waitUntil(async () => (await graphLifecycle(child)).closed.length === 1, "仅关闭到期 child 的 scope", residencyScanObservationTimeoutMs);
    const closed = await graphLifecycle(child);
    const consumerBefore = await graphLifecycle(parent);
    assert.equal(consumerBefore.compiled.length, 1);
    assert.equal(consumerBefore.closed.length, 0, "活跃消费者不得随另一个 scope 关闭");
    assert.equal(closed.closed[0].details.scope_state, "closed");
    await openOwner(child);
    const coldHistory = await loadThreadHistory(child);
    assert.deepEqual(coldHistory.items, historyBefore.items, "刷新及 cold history 不改写持久历史");
    assert.deepEqual(coldHistory.internal_executions, historyBefore.internal_executions);
    assert.deepEqual(await readRollout(child, "SELECT item_id,content_hash FROM item_catalog ORDER BY item_sequence"), itemsBefore);
    assert.deepEqual(await readRollout(child, "SELECT * FROM context_epoch_transitions ORDER BY transition_id"), epochsBefore, "卸载不能创建上下文 epoch");
    assert.deepEqual(await graphBinding(child), bindingBefore);
    assert.equal((await browserRun("return await page.evaluate(() => window.__scopeMarker);")).result, marker, "workspace 浏览器的原页面内存必须跨 scope 关闭保留");
    const externalAfter = await Promise.all(resources.map((resource) => getResource(resource.resource_id)));
    for (const [index, current] of externalAfter.entries()) {
      const original = resources[index];
      assert.equal(current.retention_scope, original.retention_scope);
      assert.equal(current.status, original.retention_scope === "thread" ? "deleted" : "running");
      assert.equal(current.reconciliation_status, "verified", "资源状态必须由实际 owner 核实");
      assert.ok(current.owner_revision > original.owner_revision, "释放关联必须推进 owner revision");
      assert.deepEqual(current.thread_associations, []);
      if (original.retention_scope === "workspace") {
        const fields = current.kind === "terminal" ? ["os_pid", "process_start_time", "pty_worker_pid"] : ["runtime_generation", "active_page_id"];
        for (const field of fields) assert.equal(current.metadata[field], original.metadata[field], "卸载不能停止或重建 workspace 外部实例");
      }
    }
    assert.equal((await graphLifecycle(child)).compiled.length, 1, "冷读取及外部资源操作不得物化 child graph");
    const changedMarker = `SCOPE_WATCH_${parent.threadId}`;
    const changedBytes = Buffer.concat([skillBytes, Buffer.from(`\n${changedMarker}\n`)]);
    const changedRevision = `sha256:${createHash("sha256").update(changedBytes).digest("hex")}`;
    await writeFile(skillPath, changedBytes);
    let watchPublished;
    await waitUntil(async () => {
      watchPublished = (await testControl({ action: "events" })).find((event) => event.phase === "workspace.source.watch.published"
        && event.pid === closed.closed[0].pid && event.at > closed.closed[0].at
        && event.details?.revision === changedRevision);
      return Boolean(watchPublished);
    }, "未发起后续模型请求前，共享 watcher 已发布精确新版来源");
    assert.ok(watchPublished.operation_id.endsWith("/gateway-context/SKILL.md"));
    assert.equal(watchPublished.details.available, true);
    assert.ok(watchPublished.details.subscriber_ids.length > 0, "共享来源通道仍有消费者；parent接收由后续原graph请求核实");
    assert.equal((await graphLifecycle(parent)).acquired.length, consumerBefore.acquired.length, "watch 发布必须先于下一次模型准入");
    await testControl({ action: "release", ...hold });
    await waitForJobStatus(parent, held.jobId);
    const parentThread = { session_id: parent.sessionId, thread_id: parent.threadId };
    const terminalBeforeUse = await getResource(terminal.resource_id);
    const attached = (await ownerApi(parent, `/api/v1/resources/${terminal.resource_id}/attach`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: terminalBeforeUse.owner_revision, thread: parentThread }),
    })).data;
    assert.deepEqual(attached.thread_associations, [parentThread]);
    const observed = await completeOwnerTurn(parent, "核对已跟踪 Skill 的当前正文，并向保留的 workspace 终端写入后读取输出。", [
      toolResponse("scope-pty-write", "write_stdin", { session_id: terminal.resource_id, chars: "printf 'SCOPE_%s_%s\\n' 'PTY' 'ALIVE'\n" }),
      toolResponse("scope-pty-read", "write_stdin", { session_id: terminal.resource_id }),
      finalResponse("scope-watch-observed", "共享监听已发布新正文，原 PTY 仍可操作。"),
    ]);
    const observedRequest = await modelRequest(observed.calls[0]);
    const terminalResultRequest = await modelRequest(observed.calls[2]);
    const terminalOutput = terminalResultRequest.request.messages.filter((message) => message.role === "tool");
    assert.ok(terminalOutput.some((message) => message.content.includes(terminal.resource_id) && message.content.includes("SCOPE_PTY_ALIVE")), "卸载后原 PTY 必须执行命令并返回真实输出，不能只核命令回显");
    const terminalAfterUse = await getResource(terminal.resource_id);
    assert.equal(terminalAfterUse.metadata.os_pid, terminal.metadata.os_pid);
    assert.equal(terminalAfterUse.metadata.process_start_time, terminal.metadata.process_start_time);
    const detached = (await ownerApi(parent, `/api/v1/resources/${terminal.resource_id}/detach`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: terminalAfterUse.owner_revision, thread: parentThread }),
    })).data;
    assert.deepEqual(detached.thread_associations, []);
    assert.ok(JSON.stringify(observedRequest.request.messages).includes(changedMarker), "另一个已订阅消费者必须收到卸载后的真实文件变化");
    const consumerAfter = await graphLifecycle(parent);
    assert.equal(consumerAfter.compiled.length, 1, "来源更新必须由原 resident consumer 接收");
    assert.equal(consumerAfter.closed.length, 0);
    observation = { idle, historyBefore, coldHistory, bindingBefore, itemsBefore, epochsBefore, closed,
      consumerBefore, consumerAfter, externalAfter, observed, observedRequest, terminalResultRequest, terminalAfterUse, changedMarker, changedRevision, watchPublished };
  } finally {
    await testControl({ action: "release", ...hold });
    await writeFile(skillPath, skillBytes);
  }
  const coldBeforeResume = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((entry) => entry.thread_id === child.threadId).runtime;
  assert.equal(coldBeforeResume.residency, "cold", "资源关联、PTY读写及解除关联不得唤醒child");
  assert.equal((await graphLifecycle(child)).compiled.length, 1, "只有后续真实消息准入才能重建child graph");
  const resumed = await completeOwnerTurn(child, "重新加载原 child 并读取 README.md。", [
    toolResponse("scope-resumed-read", "read_file", { path: "README.md" }),
    finalResponse("scope-resumed-final", "workspace 外部实例与原 GraphBinding 保留。"),
  ]);
  const prefix = (request) => JSON.stringify(request.request.messages.filter((message) => message.role === "system"));
  const prefixBefore = prefix(await modelRequest(initialCalls[0]));
  const prefixAfter = prefix(await modelRequest(resumed.calls[0]));
  assert.equal(prefixAfter, prefixBefore, "仅卸载和刷新不能漂移 child stable-prefix bytes");
  assert.deepEqual(await graphBinding(child), observation.bindingBefore);
  const rebuilt = await graphLifecycle(child);
  assert.equal(rebuilt.compiled.length, 2);
  assert.ok(graphAdmission(rebuilt, resumed.sent.jobId).details.generation > observation.closed.acquired[0].details.generation);
  for (const resource of resources.filter((entry) => entry.retention_scope === "workspace")) {
    const current = await getResource(resource.resource_id);
    await ownerApi(child, `/api/v1/resources/${current.resource_id}/stop`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expected_revision: current.owner_revision }),
    });
  }
  return { sessionId: child.sessionId, threadId: child.threadId, parentThreadId: parent.threadId,
    initialCalls, tracked, held, heldCalls, resources, ...observation, coldBeforeResume, prefixBefore, prefixAfter, resumed, rebuilt };
}

async function runChildFifoAndResidency(surface) {
  const owner = await createProductSession(surface, "子线程 FIFO 与驻留");
  const startupHistory = await loadThreadHistory(owner);
  assert.deepEqual(startupHistory.items, []);
  assert.deepEqual(await graphLifecycle(owner), { compiled: [], built: [], acquired: [], closed: [] }, "启动和 cold history 不能物化 thread graph");
  const predictedThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  await planChatCalls(owner.sessionId, predictedThread, [finalResponse("child-initial", "子线程初始化已完成。")]);
  const creation = await completeOwnerTurn(owner, "创建只读取 README.md 的持久子线程。", [
    toolResponse("child-delegate", "task", { description: "独立核对 README.md，完成后等待直接对话。", subagent_type: "general-purpose" }),
    finalResponse("child-delegated", "子线程已经创建。"),
    finalResponse("child-report-processed", "子线程报告已经处理。"),
  ]);
  let summary;
  await waitUntil(async () => {
    const children = await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/child-threads`);
    summary = children.data.items.find((item) => item.thread_id === predictedThread);
    return summary?.report_status === "completed" || summary?.report_status === "completed_empty";
  }, "真实 child 初始 internal execution 完成", 120_000);
  canonicalUuid7(summary.thread_id, "thr");
  const child = { ...owner, threadId: summary.thread_id, child: true };
  const initialHistory = await loadThreadHistory(child);
  assert.equal(initialHistory.items.length, 0, "初始 internal execution 不得伪造用户 Turn");
  assert.equal(initialHistory.internal_executions.length, 1);
  const bindingBefore = await graphBinding(child);
  const initiallyResident = await graphLifecycle(child);
  assert.equal(initiallyResident.compiled.length, 1, "首次 initial admission 必须只建图一次");
  assert.equal(initiallyResident.acquired[0].details.execution_origin, "internal");
  assert.equal(initiallyResident.acquired[0].details.turn_id, null);
  const siblingThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  await planChatCalls(owner.sessionId, siblingThread, [finalResponse("sibling-initial", "独立 sibling 初始化已完成。")]);
  const siblingCreation = await completeOwnerTurn(owner, "创建第二个独立子线程，用于核对 sibling graph 归属。", [
    toolResponse("sibling-delegate", "task", { description: "独立核对 README.md，完成后等待直接对话。", subagent_type: "general-purpose" }),
    finalResponse("sibling-created", "第二个子线程已经创建。"),
    finalResponse("sibling-report-processed", "第二个子线程报告已经处理。"),
  ]);
  let siblingSummary;
  await waitUntil(async () => {
    siblingSummary = (await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === siblingThread);
    return ["completed", "completed_empty"].includes(siblingSummary?.report_status);
  }, "sibling 初始工作完成", 120_000);
  const sibling = { ...owner, threadId: siblingThread, child: true };
  const siblingHistory = await loadThreadHistory(sibling);
  assert.equal(siblingHistory.internal_executions.length, 1);
  const siblingGraph = await graphLifecycle(sibling);
  assert.equal(siblingGraph.compiled.length, 1);
  assert.equal(siblingGraph.compiled[0].pid, initiallyResident.compiled[0].pid);
  assert.notEqual(siblingGraph.compiled[0].details.compiled_graph_object_id, initiallyResident.compiled[0].details.compiled_graph_object_id, "同时 resident 的 sibling 必须持有独立 compiled graph");
  assert.notEqual(siblingGraph.acquired[0].details.runtime_object_id, initiallyResident.acquired[0].details.runtime_object_id);
  const mainGraph = await graphLifecycle(owner);
  assert.equal(mainGraph.compiled.length, 1, "main thread 后续 Turn 必须复用首次 compiled graph");
  assert.equal(graphAdmission(mainGraph, creation.sent.jobId).details.agent_object_id, graphAdmission(mainGraph, siblingCreation.sent.jobId).details.agent_object_id);
  await openOwner(owner);
  const mainHistory = await loadThreadHistory(owner);
  const reportNotice = mainHistory.internal_executions.flatMap((execution) => execution.display_parts)
    .find((part) => part.provenance.source_thread_id === child.threadId);
  assert.ok(reportNotice, "main 必须持有真实 child 汇报来源，不能由测试补造");
  assert.equal(reportNotice.provenance.source_session_id, owner.sessionId);
  assert.equal(reportNotice.provenance.source_execution_id, initialHistory.internal_executions[0].execution_id);
  const mainTranscript = expectedTranscript(mainHistory);
  const reportIndex = mainTranscript.findIndex((entry) => entry.kind === "internal" && entry.ordinal === reportNotice.logical_item_ordinal);
  assert.ok(reportIndex >= 0, "汇报通知必须位于真实 main transcript");
  const { cell: reportCell } = await visitTranscriptIndex(owner, reportIndex, mainTranscript[reportIndex]);
  const [sourceReportResponse] = await Promise.all([
    owner.page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/sessions/${child.sessionId}/threads/${child.threadId}/history`
      && response.request().method() === "POST" && response.request().postDataJSON().execution_id === reportNotice.provenance.source_execution_id,
    { timeout: 30_000 }),
    reportCell.getByRole("button", { name: "查看来源报告", exact: true }).click(),
  ]);
  assert.equal(sourceReportResponse.status(), 200, await sourceReportResponse.text());
  const sourceReport = (await sourceReportResponse.json()).data;
  assert.equal(sourceReport.thread_id, child.threadId);
  assert.deepEqual(sourceReport.internal_executions.map((execution) => execution.execution_id), [reportNotice.provenance.source_execution_id]);
  const childPanel = owner.page.locator(`.child-thread-conversation[data-thread-id="${child.threadId}"]`);
  await childPanel.locator(`.child-thread-report[data-execution-id="${reportNotice.provenance.source_execution_id}"]`).waitFor({ state: "visible" });
  assert.equal(await catalogRow(owner, owner.sessionId).getAttribute("aria-current"), "true");
  assert.equal(await owner.page.locator("#input").isVisible(), true, "打开来源报告必须保留 main 输入入口");
  for (const threadId of [child.threadId, sibling.threadId]) {
    assert.equal(await owner.page.locator(`button[data-session-id="${threadId}"]`).count(), 0, "child 不得进入左侧 Session 导航");
  }
  const childRow = owner.page.locator(`.child-thread-item[data-thread-id="${child.threadId}"]`);
  const siblingRow = owner.page.locator(`.child-thread-item[data-thread-id="${sibling.threadId}"]`);
  await waitUntil(async () => !(await childRow.locator(".child-thread-report-link").innerText()).includes("新报告"), "来源报告读取后取消精确 child 未读");
  assert.ok((await siblingRow.locator(".child-thread-report-link").innerText()).includes("新报告"), "读取一个 child 不得确认 sibling 报告");
  await childPanel.locator("textarea").fill("child 独立草稿");
  await siblingRow.locator(".child-thread-main").click();
  const siblingPanel = owner.page.locator(`.child-thread-conversation[data-thread-id="${sibling.threadId}"]`);
  assert.equal(await siblingPanel.locator("textarea").inputValue(), "");
  await siblingPanel.locator("textarea").fill("sibling 独立草稿");
  await childRow.locator(".child-thread-main").click();
  assert.equal(await childPanel.locator("textarea").inputValue(), "child 独立草稿");
  await siblingRow.locator(".child-thread-main").click();
  assert.equal(await siblingPanel.locator("textarea").inputValue(), "sibling 独立草稿");
  const panelEvidence = { sourceReport, reportNotice, mainTurnIds: mainHistory.items.map((turn) => turn.turn_id),
    selectedSessionId: owner.sessionId, childDraft: "child 独立草稿", siblingDraft: "sibling 独立草稿" };
  const firstPrompt = "FIFO 第一条：读取 README.md，queued 第二条不得进入本轮。";
  const secondPrompt = "FIFO 第二条：独立核对 README.md，保持根消息因果顺序。";
  const calls = await planChatCalls(child.sessionId, child.threadId, [
    toolResponse("child-fifo-first-tool", "read_file", { path: "README.md" }, "第一条开始读取 README.md。"),
    finalResponse("child-fifo-first-final", "FIFO 第一条完成。"),
    toolResponse("child-fifo-second-tool", "read_file", { path: "README.md" }, "第二条现在才开始读取 README.md。"),
    finalResponse("child-fifo-second-final", "FIFO 第二条完成。"),
  ]);
  const toolCallId = `${calls[0]}:tool-call:child-fifo-first-tool`;
  await testControl({ action: "arm", operation_id: toolCallId, phase: "canonical.tool_result.before_commit" });
  await openOwner(child);
  const first = await sendOwnerPrompt(child, firstPrompt);
  await testControl({ action: "acknowledged", operation_id: toolCallId, phase: "canonical.tool_result.before_commit" });
  const second = await sendOwnerPrompt(child, secondPrompt);
  const pending = await loadThreadHistory(child);
  assert.equal(pending.items.length, 2);
  assert.equal(pending.items[1].status, "accepted");
  assert.deepEqual(pending.items[1].user_messages.map((message) => message.content), [secondPrompt], "queued history 正文必须等于已接受的第二条 root");
  assert.ok(pending.items[0].root_logical_item_ordinal < pending.items[1].root_logical_item_ordinal);
  const queuedDom = [];
  for (const stage of ["queued live", "queued refresh"]) {
    if (stage === "queued refresh") await openOwner(child);
    await assertTranscriptDom(child, pending, stage);
    const queued = child.page.locator(`.child-thread-conversation[data-thread-id="${child.threadId}"] .chat-turn[data-turn-id="${second.turnId}"]`);
    await queued.waitFor({ state: "visible", timeout: 60_000 });
    const rootOrdinal = await queued.getAttribute("data-root-logical-item-ordinal");
    assert.equal(rootOrdinal, String(pending.items[1].root_logical_item_ordinal));
    const queuedContent = queued.locator(".child-thread-user .chat-markdown");
    assert.equal(await queuedContent.count(), 1);
    const content = await queuedContent.textContent();
    assert.equal(content, pending.items[1].user_messages[0].content, `${stage}: queued 正文必须来自已接受 root`);
    queuedDom.push({ stage, turnId: second.turnId, rootOrdinal, content });
  }
  const withBlocker = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId);
  assert.equal(withBlocker.runtime.residency, "resident");
  assert.equal(withBlocker.runtime.execution_state, "active", "runtime lease 必须由权威 active 状态表达");
  assert.equal(withBlocker.runtime.cold_eligible, false, "active/queued lease 必须阻止卸载");
  await waitUntil(async () => {
    const text = await childRow.locator(".child-thread-runtime").innerText();
    return withBlocker.runtime.blockers.every((blocker) => text.includes(blocker.reason));
  }, "右侧列表展示后端脱敏 blocker");
  await testControl({ action: "advance_residency", seconds: 1800 });
  const stillActive = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId);
  assert.equal(stillActive.runtime.residency, "resident");
  await testControl({ action: "release", operation_id: toolCallId, phase: "canonical.tool_result.before_commit" });
  await waitForJobStatus(child, first.jobId);
  await waitForJobStatus(child, second.jobId);
  const interleaved = await verifyOwnerTurns(child);
  const request = await modelRequest(calls[1]);
  assert.ok(JSON.stringify(request.request.messages).includes(firstPrompt));
  assert.ok(!JSON.stringify(request.request.messages).includes(secondPrompt), "旧 execution fence 泄露 queued root");
  const successors = [];
  for (const call of calls.slice(2)) {
    const successor = await modelRequest(call);
    successors.push(successor);
    assert.ok(JSON.stringify(successor.request.messages).includes(secondPrompt), "后继 execution 必须消费同一冻结 queued 正文");
  }
  const terminalHistory = await loadThreadHistory(child);
  const firstParts = terminalHistory.items[0].response_parts.map(partCoordinate);
  const rootRows = await readRollout(child, "SELECT item_sequence FROM item_catalog WHERE turn_id=? AND turn_scope='turn_root'", [terminalHistory.items[1].turn_id]);
  assert.equal(rootRows.length, 1);
  const secondRoot = rootRows[0].item_sequence;
  assert.ok(firstParts.some((part) => part.item_sequence < secondRoot) && firstParts.some((part) => part.item_sequence > secondRoot), "夹具必须真的造成两个 Turn 物理 Item 交错");
  assert.ok(!firstParts.some((part) => part.item_sequence === secondRoot));
  assert.equal(terminalHistory.items[0].activity_stats.item_count, 4);
  assert.equal(terminalHistory.items[1].activity_stats.item_count, 4);
  const reused = await graphLifecycle(child);
  assert.equal(reused.compiled.length, 1, "同一 durable child 的初始工作与后续 Turn 必须复用 compiled graph");
  assert.equal(graphAdmission(reused, first.jobId).details.generation, initiallyResident.acquired[0].details.generation);
  assert.equal(graphAdmission(reused, second.jobId).details.agent_object_id, initiallyResident.acquired[0].details.agent_object_id);
  const idleRuntime = await waitForIdleChildRuntime(child);
  await testControl({ action: "advance_residency", seconds: 1799 });
  const beforeDeadline = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId);
  assert.equal(beforeDeadline.runtime.residency, "resident", "29:59 仍必须驻留");
  assert.equal((await graphLifecycle(child)).closed.length, 0, "29:59 不能实际释放 graph generation");
  await testControl({ action: "advance_residency", seconds: 1 });
  await waitUntil(async () => (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId).runtime.residency === "cold", "30:00 到期后后台扫描释放 runtime", residencyScanObservationTimeoutMs);
  const coldHistory = await loadThreadHistory(child);
  assert.deepEqual(coldHistory.items, terminalHistory.items);
  const coldDetail = await loadThreadHistory(child, { turn_ids: [first.turnId] });
  assert.deepEqual(coldDetail.items, [terminalHistory.items[0]], "cold detail 必须沿权威 history 读取同一 Turn");
  const traceIdentity = await assertColdThreadTraceIdentity(child, owner.threadId);
  const coldRuntime = (await ownerApi(child, `/api/v1/sessions/${child.sessionId}/child-threads`)).data.items.find((item) => item.thread_id === child.threadId).runtime;
  assert.equal(coldRuntime.residency, "cold", "cold history 不能唤醒 graph");
  assert.deepEqual(await graphBinding(child), bindingBefore);
  const released = await graphLifecycle(child);
  assert.equal(released.compiled.length, 1, "cold history/detail 不得再次编译 graph");
  assert.equal(released.closed.length, 1, "到期后的后台扫描必须真实关闭 scope");
  assert.equal(released.closed[0].details.generation, reused.acquired[0].details.generation);
  await waitUntil(async () => (await childRow.locator(".child-thread-runtime").innerText()).includes("运行时已释放"), "列表镜像 cold residency");
  await childRow.locator(".child-thread-report-link").click();
  await childPanel.locator(`.child-thread-report[data-execution-id="${reportNotice.provenance.source_execution_id}"]`).waitFor({ state: "visible" });
  assert.equal((await graphLifecycle(child)).compiled.length, 1, "真实点击 cold 报告不得重新建图");
  assert.deepEqual((await loadThreadHistory(owner)).items, mainHistory.items, "child 直接对话不得修改 main Turn 历史");
  const rehydrated = await completeOwnerTurn(child, "cold 后再次读取 README.md。", [
    toolResponse("child-rehydrated-tool", "read_file", { path: "README.md" }), finalResponse("child-rehydrated-final", "原 GraphBinding 已重新驻留。"),
  ]);
  const bindingAfter = await graphBinding(child);
  assert.deepEqual(bindingAfter, bindingBefore);
  const rebuilt = await graphLifecycle(child);
  assert.equal(rebuilt.compiled.length, 2, "cold 后首次可执行 admission 必须沿原 binding 重建一次");
  assert.ok(graphAdmission(rebuilt, rehydrated.sent.jobId).details.generation > reused.acquired[0].details.generation);
  return { sessionId: child.sessionId, mainThreadId: owner.threadId, threadId: child.threadId, creation, startupHistory, initialHistory, first, second,
    pending, queuedDom, terminalHistory, secondRoot, requests: { firstContinuation: request, successors },
    runtime: { withBlocker: withBlocker.runtime, stillActive: stillActive.runtime, beforeDeadline: beforeDeadline.runtime, coldRuntime },
    interleaved, bindingBefore, bindingAfter, coldHistory, coldDetail, traceIdentity, rehydrated,
    graph: { initiallyResident, reused, siblingGraph, mainGraph, released, rebuilt, idleRuntime }, siblingCreation, siblingHistory, panelEvidence };
}

async function assertColdThreadTraceIdentity(owner, mainThreadId) {
  const base = `/api/v1/sessions/${owner.sessionId}/traces`;
  async function pages(threadId) {
    const events = [];
    let cursor = null;
    let firstCursor = null;
    let emptyPages = 0;
    let pageCount = 0;
    const seenCursors = new Set();
    do {
      const query = new URLSearchParams({ limit: "11" });
      if (threadId) query.set("thread_id", threadId);
      if (cursor) query.set("cursor", cursor);
      const page = (await ownerApi(owner, `${base}?${query}`)).data;
      for (const event of page.items) {
        assert.equal(event.session_id, owner.sessionId);
        canonicalUuid7(event.thread_id, "thr");
        if (threadId) assert.equal(event.thread_id, threadId);
        events.push({ event_id: event.event_id, job_id: event.job_id, thread_id: event.thread_id, type: event.type });
      }
      pageCount += 1;
      if (page.items.length === 0) emptyPages += 1;
      cursor = page.next_cursor;
      assert.equal(page.has_more, Boolean(cursor));
      if (cursor) {
        assert.ok(!seenCursors.has(cursor), "稀疏 Trace 页必须推进游标");
        seenCursors.add(cursor);
        firstCursor ??= cursor;
      }
    } while (cursor);
    assert.equal(new Set(events.map((event) => event.event_id)).size, events.length);
    return { events, firstCursor, emptyPages, pageCount };
  }
  const aggregate = await pages(null);
  const child = await pages(owner.threadId);
  const main = await pages(mainThreadId);
  assert.ok(child.events.length > 0 && main.events.length > 0, "真实产品流程必须生成 main 和 child Trace");
  for (const [threadId, filtered] of [[owner.threadId, child], [mainThreadId, main]]) {
    assert.deepEqual(filtered.events.map((event) => event.event_id).sort(),
      aggregate.events.filter((event) => event.thread_id === threadId).map((event) => event.event_id).sort());
  }
  assert.ok(child.firstCursor, "真实历史应跨越 Trace 分页");
  async function rejectedCursor(path) {
    const response = await ownerApiResponse(owner, path);
    assert.equal(response.status, 410);
    assert.equal(response.body.detail.code, "trace_cursor_gone");
    return response.body.detail;
  }
  const wrongThread = await rejectedCursor(`${base}?${new URLSearchParams({ thread_id: mainThreadId, cursor: child.firstCursor })}`);
  const wrongScope = await rejectedCursor(`${base}?${new URLSearchParams({ cursor: child.firstCursor })}`);
  const wrongPurpose = await rejectedCursor(`${base}/stream?${new URLSearchParams({ thread_id: owner.threadId, after_event_id: child.firstCursor })}`);
  return { aggregate, child, main, wrongThread, wrongScope, wrongPurpose };
}

function softwareOperation(operation, owner, modelCallId, providerCallId) {
  return `cop_${createHash("sha256").update(JSON.stringify([operation, owner.sessionId, owner.threadId, modelCallId, providerCallId])).digest("hex")}`;
}

async function executionProof(owner, jobId) {
  const rows = await readRollout(owner, "SELECT e.execution_id,e.turn_id,e.job_id,e.admission_idempotency_key,e.outcome,e.last_model_call_id,m.model_call_id,m.dispatch_state,m.outcome AS model_outcome,m.assembly_id,a.status AS assembly_status,a.outcome AS assembly_outcome,a.request_hash,a.plan_hash FROM executions e JOIN model_calls m ON m.execution_id=e.execution_id AND m.model_call_id=e.last_model_call_id JOIN context_assemblies a ON a.assembly_id=m.assembly_id WHERE e.job_id=?", [jobId]);
  assert.equal(rows.length, 1);
  const proof = rows[0];
  assert.equal(proof.outcome, "unknown");
  assert.equal(proof.dispatch_state, "dispatched");
  assert.equal(proof.model_outcome, "unknown");
  assert.equal(proof.assembly_status, "sealed");
  assert.ok(proof.request_hash && proof.plan_hash);
  const terminal = await readRollout(owner, "SELECT commit_id FROM storage_commits WHERE commit_kind='terminal_convergence' AND subject_id IN (?,?)", [proof.execution_id, proof.turn_id]);
  assert.equal(terminal.length, 0);
  const carrier = await readRollout(owner, "SELECT item_id,semantic_kind,status,execution_id,turn_id,content_hash,projection_manifest_json FROM item_catalog WHERE json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? AND semantic_kind IN ('reasoning','assistant_output','tool_call') ORDER BY item_sequence", [proof.model_call_id]);
  assert.ok(carrier.length > 0 && carrier.some((item) => item.semantic_kind === "tool_call"));
  assert.ok(carrier.every((item) => item.status === "completed" && item.execution_id === proof.execution_id && item.turn_id === proof.turn_id));
  return { proof, carrier, binding: await graphBinding(owner) };
}

async function replaceNodePage(surface, owners, restartWindow = null) {
  const peerContext = surface.page.context();
  const replacesPrimaryPage = surface.page === page;
  if (restartWindow) {
    assert.equal(controlledRestartPageBinding(restartWindow, surface.page.diagnosticId)?.role,
      "foreground-before-restart", "受控重启必须保留被替换页面的原始身份");
  }
  await drainPageResponseBodies(surface.page);
  await surface.page.close();
  return async () => {
    surface.page = await browserStartup("recovery_peer_page", () => peerContext.newPage());
    if (replacesPrimaryPage) page = surface.page;
    installPageDiagnostics(surface.page);
    if (restartWindow) {
      bindControlledRestartPage(restartWindow, surface.page, "foreground-after-restart");
    }
    try {
      await ensureGuest(surface.page, JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"))[surface.name].base_url);
      for (const owner of owners) owner.page = surface.page;
    } finally {
      if (restartWindow) {
        restartWindow.endedAt = Date.now();
        surface.page.controlledRestartWindow = null;
      }
    }
  };
}

async function assertRecoveredExecution(owner, sent, before, calls, { expectedJobStatus, expectedExecutionOutcome, expectedTurnStatus }) {
  await openOwner(owner);
  const job = await waitForJobStatus(owner, sent.jobId, expectedJobStatus);
  assert.deepEqual(await graphBinding(owner), before.binding, "backend 重建须保留逐字段 GraphBinding");
  const rows = await readRollout(owner, "SELECT execution_id,turn_id,job_id,admission_idempotency_key,outcome FROM executions WHERE job_id=?", [sent.jobId]);
  assert.deepEqual(rows, [{ execution_id: before.proof.execution_id, turn_id: sent.turnId, job_id: sent.jobId, admission_idempotency_key: before.proof.admission_idempotency_key, outcome: expectedExecutionOutcome }]);
  const modelCalls = await readRollout(owner, "SELECT model_call_id FROM model_calls WHERE execution_id=? ORDER BY attempt", [before.proof.execution_id]);
  assert.deepEqual(modelCalls.map((item) => item.model_call_id), calls, "恢复不能重发原 Provider 或另建原 ModelCall");
  const original = await readRollout(owner, "SELECT item_id,content_hash FROM item_catalog WHERE json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? AND semantic_kind IN ('reasoning','assistant_output','tool_call') ORDER BY item_sequence", [before.proof.model_call_id]);
  assert.deepEqual(original, before.carrier.map(({ item_id, content_hash }) => ({ item_id, content_hash })), "恢复不得改写原 sealed output bytes");
  const requests = await Promise.all(calls.map(modelRequest));
  const prefixes = requests.map((entry) => entry.request.messages.filter((message) => message.role === "system"));
  assert.ok(prefixes.every((prefix) => JSON.stringify(prefix) === JSON.stringify(prefixes[0])), "重启不能改变 stable prefix bytes");
  const evidence = await verifyOwnerTurns(owner);
  const turn = await assertAcceptedTurn(owner, sent);
  assert.equal(turn.status, expectedTurnStatus);
  assert.equal(turn.user_messages.length, 1, "原 Turn 只能保留一个真实 root");
  return { job, rows, modelCalls, evidence, turn };
}

async function runCommunicationRecovery(surfaces) {
  const facts = {};
  for (const boundary of ["source_receipt", "target_binding"]) {
    const source = await createProductSession(surfaces.b, `send 恢复 ${boundary}`);
    const target = await createProductSession(surfaces.c, `send 目标 ${boundary}`);
    await planChatCalls(target.sessionId, target.threadId, [finalResponse(`${boundary}-target`, "通信只注入一次。")]);
    const label = `${boundary}-send`;
    const calls = await planChatCalls(source.sessionId, source.threadId, [
      toolResponse(label, "send_message_to_session", { target_session_id: target.sessionId, content: "恢复同一通信调用。", kind: "result" }),
      finalResponse(`${boundary}-final`, "原 send 调用已恢复。"),
    ]);
    const operationId = softwareOperation("send", source, calls[0], label);
    const communicationId = `comm_${createHash("sha256").update(`communication\0${operationId}`).digest("hex").slice(0, 32)}`;
    const barrier = boundary === "source_receipt" ? { operation_id: operationId, phase: "communication.source.receipt_received" }
      : { operation_id: communicationId, phase: "communication.inbox.job_created" };
    await testControl({ action: "arm", ...barrier });
    await openOwner(source);
    const sent = await sendOwnerPrompt(source, `请向 ${target.sessionId} 发送恢复检查。`);
    await testControl({ action: "acknowledged", ...barrier });
    const inboxBefore = await readControl(target, "SELECT * FROM communication_inbox WHERE communication_id=?", [communicationId]);
    assert.equal(inboxBefore.length, 1);
    let targetExecutionsBefore;
    await waitUntil(async () => {
      targetExecutionsBefore = await readRollout(target, "SELECT execution_id,job_id FROM executions ORDER BY execution_id");
      return targetExecutionsBefore.length === 1;
    }, "原 inbox 创建唯一 durable admission");
    const restarting = boundary === "source_receipt" ? source : target;
    const before = boundary === "source_receipt" ? await executionProof(source, sent.jobId) : null;
    const targetBinding = await graphBinding(target);
    const reopen = await replaceNodePage(surfaces[restarting.name], [restarting]);
    const killed = await testControl({ action: "process", name: restarting.name, operation: "kill" });
    await testControl({ action: "release", ...barrier });
    const restarted = await testControl({ action: "process", name: restarting.name, operation: "start" });
    assert.notEqual(killed.before_pid, restarted.after_pid);
    await reopen();
    const sourceResult = before ? await assertRecoveredExecution(source, sent, before, calls,
      { expectedJobStatus: "completed", expectedExecutionOutcome: "completed", expectedTurnStatus: "completed" }) : { job: await waitForJobStatus(source, sent.jobId), evidence: await verifyOwnerTurns(source) };
    let inbox;
    await waitUntil(async () => {
      inbox = await readControl(target, "SELECT * FROM communication_inbox WHERE communication_id=?", [communicationId]);
      return inbox.length === 1 && inbox[0].state === "execution_bound";
    }, `${boundary} inbox 唯一 binding`, 120_000);
    assert.equal(inbox[0].turn_id, null, "ambient send 不创建用户 Turn");
    const targetExecutions = await readRollout(target, "SELECT execution_id,job_id FROM executions ORDER BY execution_id");
    assert.deepEqual(targetExecutions, targetExecutionsBefore, "target restart 不得创建第二份 Job/execution");
    assert.equal(targetExecutions.length, 1);
    await waitForJobStatus(target, targetExecutions[0].job_id);
    const outbox = await readControl(source, "SELECT * FROM communication_outbox WHERE send_operation_id=?", [operationId]);
    assert.equal(outbox.length, 1);
    assert.equal(outbox[0].communication_id, communicationId);
    assert.deepEqual(await graphBinding(target), targetBinding);
    await openOwner(target);
    const targetHistory = await verifyOwnerTurns(target);
    assert.equal(targetHistory.turns.length, 0);
    assert.equal(targetHistory.internal.length, 1);
    const notices = await readRollout(target, "SELECT item_id FROM item_catalog WHERE semantic_kind='runtime_notice' ORDER BY item_sequence");
    assert.equal(notices.length, 1, "communication 只能注入一份 ambient item");
    facts[boundary] = { operationId, communicationId, before, killed, restarted, sourceResult, inboxBefore, inbox, outbox, targetExecutions, targetHistory, notices };
  }
  return facts;
}

async function observationPage(owner, sourceCallId, side) {
  const callPath = side === "source" ? "$.envelope.source_call_id" : "$.source_call_id";
  const rows = await readControl(owner, `SELECT p.*,o.snapshot_json,o.expires_at FROM remote_observation_pages p JOIN remote_observations o USING(observation_id,side) WHERE p.side=? AND json_extract(p.request_json,'${callPath}')=? ORDER BY p.page_ordinal`, [side, sourceCallId]);
  assert.equal(rows.length, 1, "每个 source call 只能拥有一份原 PageRecord");
  return rows[0];
}

function observationEnvelope(page) {
  const request = JSON.parse(page.request_json);
  return page.side === "source" ? request.envelope : request;
}

async function runReadRecovery(surfaces, reportOwners) {
  const source = reportOwners?.source ?? await createProductSession(surfaces.b, "多页读取冻结快照与同源恢复");
  const target = reportOwners?.target ?? await createProductSession(surfaces.c, "read 冻结上界目标");
  for (const text of ["冻结素材一", "冻结素材二"]) await completeOwnerTurn(target, text, [finalResponse(text, text)]);
  if (!reportOwners) await completeOwnerTurn(source, "载入上下文读取能力。", [toolResponse("recovery-load-context", "skill_load", { name: "gateway-context" }), finalResponse("recovery-context-ready", "上下文工具已载入。")]);
  const args = { ...target.resource, view: "messages", recent_rounds: 50, limit: 2, max_chars: 16384 };
  const label = "recovery-read-first";
  const calls = await planChatCalls(source.sessionId, source.threadId, [toolResponse(label, "invoke_extension_tool", { tool_name: "read_context", arguments: args })]);
  const sourceCallId = softwareOperation("read", source, calls[0], label);
  const toolCallId = `${calls[0]}:tool-call:${label}`;
  const barrier = { operation_id: sourceCallId, phase: "observation.source.response_received" };
  await testControl({ action: "arm", ...barrier });
  await testControl({ action: "arm", operation_id: toolCallId, phase: "canonical.tool_result.before_commit" });
  await openOwner(source);
  const sent = await sendOwnerPrompt(source, `读取 ${target.resource.uri}，继续同一冻结快照的下一页。`);
  await testControl({ action: "acknowledged", ...barrier });
  const frozen = await observationPage(target, sourceCallId, "target");
  const before = await executionProof(source, sent.jobId);
  const reopen = await replaceNodePage(surfaces.b, [source]);
  const killed = await testControl({ action: "process", name: "b", operation: "kill" });
  await testControl({ action: "release", ...barrier });
  const appended = await completeOwnerTurn(target, "冻结之后的新素材不得混入。", [finalResponse("recovery-late-target", "冻结之后的新素材不得混入。")]);
  const viewBefore = await readRollout(target, "SELECT active_branch_id,history_view_revision FROM database_meta");
  await planChatCalls(target.sessionId, target.threadId, [finalResponse("recovery-view-replaced", "独立 view 的替换正文不得进入原周报。")]);
  const viewChange = (await ownerApi(target, `/api/v1/sessions/${target.sessionId}/turns/${appended.sent.turnId}/replay`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "regenerate", acknowledge_context_only: true }),
  })).data;
  await waitForJobStatus(target, viewChange.job_id);
  const viewAfter = await readRollout(target, "SELECT active_branch_id,history_view_revision FROM database_meta");
  assert.notDeepEqual(viewAfter, viewBefore, "页间必须发生正常 replay 的独立 view 变化");
  await verifyOwnerTurns(target);
  const restarted = await testControl({ action: "process", name: "b", operation: "start" });
  await reopen();
  await testControl({ action: "acknowledged", operation_id: toolCallId, phase: "canonical.tool_result.before_commit" });
  const saved = await observationPage(source, sourceCallId, "source");
  const firstPage = JSON.parse(saved.result_json);
  assert.equal(typeof firstPage.next_cursor, "string", "真实冻结读取必须有第二页");
  assert.deepEqual(await observationPage(target, sourceCallId, "target"), frozen, "目标新增 Turn 后原 PageRecord 不能变动");
  const pages = [];
  let nextCursor = firstPage.next_cursor;
  let previousToolCallId = toolCallId;
  // limit 计算投影 message 条数；沿原 cursor 读完冻结上界后才计划报告。
  do {
    const pageOrdinal = pages.length + 1;
    const pageLabel = pageOrdinal === 1 ? "recovery-read-second" : `recovery-read-page-${pageOrdinal + 1}`;
    const [call] = await planChatCalls(source.sessionId, source.threadId, [
      toolResponse(pageLabel, "invoke_extension_tool", { tool_name: "read_context", arguments: { ...args, cursor: nextCursor } }),
    ]);
    calls.push(call);
    const currentToolCallId = `${call}:tool-call:${pageLabel}`;
    await testControl({ action: "arm", operation_id: currentToolCallId, phase: "canonical.tool_result.before_commit" });
    await testControl({ action: "release", operation_id: previousToolCallId, phase: "canonical.tool_result.before_commit" });
    await testControl({ action: "acknowledged", operation_id: currentToolCallId, phase: "canonical.tool_result.before_commit" });
    const sourceCallId = softwareOperation("read", source, call, pageLabel);
    const page = await observationPage(source, sourceCallId, "source");
    assert.equal(page.observation_id, saved.observation_id);
    assert.equal(page.page_ordinal, pageOrdinal);
    assert.notEqual(page.page_identity, saved.page_identity);
    const result = JSON.parse(page.result_json);
    assert.equal(result.revision, firstPage.revision);
    assert.equal(result.has_more, result.next_cursor !== null);
    pages.push({ call, sourceCallId, page, result });
    nextCursor = result.next_cursor;
    previousToolCallId = currentToolCallId;
  } while (nextCursor !== null);
  calls.push(...await planChatCalls(source.sessionId, source.threadId, [
    finalResponse("recovery-read-final", "本周补充周报：冻结素材一与冻结素材二，只使用首次冻结的上界。"),
  ]));
  await testControl({ action: "release", operation_id: previousToolCallId, phase: "canonical.tool_result.before_commit" });
  const recovered = await assertRecoveredExecution(source, sent, before, calls,
    { expectedJobStatus: "completed", expectedExecutionOutcome: "completed", expectedTurnStatus: "completed" });
  const readResults = recovered.turn.response_parts.filter((part) => part.kind === "tool_result");
  assert.equal(readResults.length, pages.length + 1, "每页只提交原 source call 的一个 ToolResult");
  assert.ok(readResults.every((part) => !String(part.result).includes("冻结之后的新素材")));
  const { sourceCallId: nextCallId, page: secondPage } = pages[0];
  assert.equal(secondPage.observation_id, saved.observation_id);
  assert.notEqual(secondPage.page_identity, saved.page_identity);
  assert.equal(secondPage.page_ordinal, 1);
  const targetEffects = "SELECT (SELECT COUNT(*) FROM remote_observations) AS observations,(SELECT COUNT(*) FROM remote_observation_pages) AS pages,(SELECT COUNT(*) FROM communication_inbox) AS inbox";
  const effectsBefore = await readControl(target, targetEffects);
  const cursor = firstPage.next_cursor;
  assert.match(cursor, /^rc1\./);
  const tokenOffset = Math.floor(cursor.length / 2);
  const tampered = cursor.slice(0, tokenOffset) + (cursor[tokenOffset] === "A" ? "B" : "A") + cursor.slice(tokenOffset + 1);
  const negative = {};
  // read_context 的 ToolException 返回错误 ToolMessage；后继模型只确认原拒绝，不重试读取。
  for (const [label, code, arguments_] of [
    ["read-conflicting-parameters", "read-snapshot-mismatch", { ...args, cursor, limit: args.limit + 1 }],
    ["read-tampered-token", "read-snapshot-invalid", { ...args, cursor: tampered }],
    ["read-conflicting-revision", "read-snapshot-mismatch", { ...args, cursor, revision: "sha256:" + "0".repeat(64) }],
  ]) {
    const result = await completeOwnerTurn(source, `拒绝无效分页请求 ${label}。`, [
      toolResponse(label, "invoke_extension_tool", { tool_name: "read_context", arguments: arguments_ }),
      finalResponse(`${label}-done`, `读取已拒绝：${code}。`),
    ]);
    const errors = result.turn.response_parts.filter((part) => part.kind === "tool_result");
    assert.equal(errors.length, 1, "每项业务失败只提交原工具调用的一个完整错误结果");
    assert.equal(errors[0].tool_name, "invoke_extension_tool");
    assert.equal(errors[0].status, "completed");
    const errorResult = assertBoundToolResult(source, result.sent, result.turn, result.calls[0], label,
      "invoke_extension_tool");
    assert.equal(errorResult.result, code);
    await assertToolResultContinuation(source, result.calls, label, errorResult);
    assert.ok(!String(errors[0].result).includes("冻结素材"), "读取失败不能泄露冻结正文");
    assert.deepEqual(result.turn.response_parts.filter((part) => part.kind === "final_text").map((part) => part.text),
      [`读取已拒绝：${code}。`]);
    assert.deepEqual(await readControl(target, targetEffects), effectsBefore, "无效读取不能创建目标 observation/page/inbox");
    assert.deepEqual(await observationPage(target, sourceCallId, "target"), frozen);
    negative[label] = result;
  }
  assert.ok(readResults.every((part) => !String(part.result).includes("独立 view 的替换正文")));
  const finalRequest = await modelRequest(calls.at(-1));
  const requestBytes = JSON.stringify(finalRequest.request.messages);
  assert.ok(requestBytes.includes("冻结素材一") && requestBytes.includes("冻结素材二"), "实际周报 Provider 请求必须消费两页冻结素材");
  assert.ok(!requestBytes.includes("冻结之后的新素材") && !requestBytes.includes("独立 view 的替换正文"), "周报请求不能混入冻结后的追加与独立 view");
  return { source: { sessionId: source.sessionId, threadId: source.threadId }, target: { sessionId: target.sessionId, threadId: target.threadId },
    args, cursor, sourceCallId, nextCallId, frozen, saved, secondPage, pages, before, killed, restarted, appended, viewChange, viewBefore, viewAfter, negative, recovered, finalRequest };
}

async function runReadCursorExpiry(surfaces, previous) {
  assert.ok(previous, "cursor expiry 必须消费本轮已验证的原多页 observation");
  const source = { ...surfaces.b, ...previous.source, child: false };
  const target = { ...surfaces.c, ...previous.target, child: false };
  const targetEffects = "SELECT 'observation' AS kind,observation_id AS identity,side FROM remote_observations UNION ALL SELECT 'page' AS kind,page_identity AS identity,side FROM remote_observation_pages UNION ALL SELECT 'inbox' AS kind,communication_id AS identity,'' AS side FROM communication_inbox ORDER BY kind,identity,side";
  const before = await readControl(target, targetEffects);
  await testControl({ action: "advance_wait", seconds: 86_401 });
  const clock = await testControl({ action: "clock" });
  assert.ok(Date.parse(JSON.parse(previous.saved.request_json).envelope.expires_at) <= Date.parse(clock.wait_utc));
  // 新 operation 的创建时刻来自 ModelCall UUIDv7；只推进新 ID 工厂，不改原 cursor 与调用。
  await testControl({ action: "identifier_date", utc: clock.wait_utc });
  const result = await completeOwnerTurn(source, "原周报 cursor 过期后不得重新开始观察。", [
    toolResponse("read-expired-cursor", "invoke_extension_tool", { tool_name: "read_context", arguments: { ...previous.args, cursor: previous.cursor } }),
    finalResponse("read-expired-cursor-done", "原 cursor 已过期，没有重新读取。"),
  ]);
  const operationCreatedAt = Number.parseInt(result.calls[0].replaceAll("-", "").slice(0, 12), 16);
  assert.equal(operationCreatedAt, Date.parse(clock.wait_utc), "新 source operation 必须仍在原恢复窗口内，由旧 cursor 过期拒绝");
  const errors = result.turn.response_parts.filter((part) => part.kind === "tool_result");
  assert.equal(errors.length, 1);
  assert.equal(errors[0].tool_name, "invoke_extension_tool");
  assert.equal(errors[0].status, "completed");
  const errorResult = assertBoundToolResult(source, result.sent, result.turn, result.calls[0], "read-expired-cursor",
    "invoke_extension_tool");
  assert.equal(errorResult.result, "read-snapshot-expired");
  await assertToolResultContinuation(source, result.calls, "read-expired-cursor", errorResult);
  assert.ok(!String(errors[0].result).includes("冻结素材"));
  assert.deepEqual(result.turn.response_parts.filter((part) => part.kind === "final_text").map((part) => part.text),
    ["原 cursor 已过期，没有重新读取。"]);
  const after = await readControl(target, targetEffects);
  const original = new Set(before.map((row) => JSON.stringify(row)));
  assert.ok(after.every((row) => original.has(JSON.stringify(row))), "过期重试不能创建目标 observation/page/inbox；原事实可按到期规则回收");
  return { result, before, after, clock, operationCreatedAt };
}

async function runWaitRecovery(surfaces) {
  const facts = {};
  for (const changeOrigin of [false, true]) {
    const source = await createProductSession(surfaces.b, `wait deadline 恢复 ${changeOrigin}`);
    const target = await createProductSession(surfaces.c, `wait baseline 冻结 ${changeOrigin}`);
    const targetLabel = `wait-target-${changeOrigin}`;
    const targetCalls = await planChatCalls(target.sessionId, target.threadId, [
      toolResponse(targetLabel, "read_file", { path: "README.md" }),
      finalResponse(`${targetLabel}-final`, "旧目标任务完成。"),
      finalResponse(`wait-target-future-${changeOrigin}`, "冻结后的新任务独立完成。"),
    ]);
    const targetToolCallId = `${targetCalls[0]}:tool-call:${targetLabel}`;
    await testControl({ action: "arm", operation_id: targetToolCallId, phase: "canonical.tool_result.before_commit" });
    await openOwner(target);
    const targetSent = await sendOwnerPrompt(target, "保持旧目标任务，验证 frozen baseline。");
    await testControl({ action: "acknowledged", operation_id: targetToolCallId, phase: "canonical.tool_result.before_commit" });
    const label = `wait-source-${changeOrigin}`;
    const calls = await planChatCalls(source.sessionId, source.threadId, [toolResponse(label, "wait_for_session", { target_session_id: target.sessionId, until: "terminal", timeout_seconds: changeOrigin ? 300 : 60 }), finalResponse(`${label}-final`, "等待沿原预算结束。")]);
    const sourceCallId = softwareOperation("wait", source, calls[0], label);
    const barrier = { operation_id: sourceCallId, phase: "observation.target.baseline_frozen" };
    await testControl({ action: "arm", ...barrier });
    await openOwner(source);
    const sent = await sendOwnerPrompt(source, `等待 ${target.sessionId}，不得延长原预算。`);
    await testControl({ action: "acknowledged", ...barrier });
    const frozen = await observationPage(target, sourceCallId, "target");
    const future = await sendOwnerPrompt(target, "冻结后排队的新目标任务不得进入旧 wait baseline。");
    assert.equal((await loadThreadHistory(target)).items.at(-1).status, "accepted");
    assert.ok(!JSON.stringify(JSON.parse(frozen.snapshot_json)).includes(future.jobId));
    const before = await executionProof(source, sent.jobId);
    const reopen = await replaceNodePage(surfaces.b, [source]);
    const killed = await testControl({ action: "process", name: "b", operation: "kill" });
    await testControl({ action: "advance_wait", seconds: changeOrigin ? 300 : 60, change_origin: changeOrigin });
    await testControl({ action: "release", ...barrier });
    const restarted = await testControl({ action: "process", name: "b", operation: "start" });
    await reopen();
    const recovered = await assertRecoveredExecution(source, sent, before, calls,
      { expectedJobStatus: "completed", expectedExecutionOutcome: "completed", expectedTurnStatus: "completed" });
    const results = recovered.turn.response_parts.filter((part) => part.kind === "tool_result");
    assert.equal(results.length, 1);
    assert.match(String(results[0].result), /timed_out/);
    const saved = await observationPage(source, sourceCallId, "source");
    assert.equal(JSON.parse(saved.result_json).status, "timed_out", "两种 origin 都按原 deadline 返回超时结果，不改变 Job/Turn 的成功终态");
    const requestBefore = observationEnvelope(frozen);
    const requestAfter = observationEnvelope(saved);
    assert.deepEqual(requestAfter.query.deadline, requestBefore.query.deadline, "恢复不得重新计算 deadline");
    assert.deepEqual(JSON.parse((await observationPage(target, sourceCallId, "target")).snapshot_json), JSON.parse(frozen.snapshot_json));
    await testControl({ action: "release", operation_id: targetToolCallId, phase: "canonical.tool_result.before_commit" });
    await waitForJobStatus(target, targetSent.jobId);
    await waitForJobStatus(target, future.jobId);
    await waitForPlannedExecutions(target, targetCalls);
    const targetEvidence = await verifyOwnerTurns(target);
    facts[changeOrigin ? "changed_origin" : "same_origin"] = { sourceCallId, before, frozen, saved, future, killed, restarted, recovered, targetEvidence };
  }
  return facts;
}

async function runInitialAdmissionRecovery(surfaces) {
  const owner = await createProductSession(surfaces.a, "initial admission 先持久再绑定");
  const node = await sessionCatalogNode(owner);
  const childThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  const parentJob = `job_${await testControl({ action: "next_uuid", namespace: "job" })}`;
  const label = "initial-cas-delegate";
  const parentCalls = await planChatCalls(owner.sessionId, owner.threadId, [
    toolResponse(label, "task", { description: "读取 README.md 后提交固定初始化结果。", subagent_type: "general-purpose" }),
  ]);
  const admissionKey = `del_${createHash("sha256").update(`session-subagent-delegation\0${owner.sessionId}\0${parentJob}\0${label}\0general-purpose`).digest("hex").slice(0, 32)}`;
  const barrier = { operation_id: admissionKey, phase: "thread.initial.admitted_before_bound" };
  await testControl({ action: "arm", ...barrier });
  const childCalls = await planChatCalls(owner.sessionId, childThread, [finalResponse("initial-cas-child-result", "初始化报告的私有正文。")]);
  await openOwner(owner);
  // 同步 binder barrier 会暂停事件循环；准入回执后不再发起后端 API。
  const sent = await acceptOwnerPrompt(owner, "创建持久 child，验证 admission 后 CAS 前重启。");
  assert.equal(sent.jobId, parentJob);
  await testControl({ action: "acknowledged", ...barrier });
  const parentTurn = await readFrozenRollout(owner, node, "SELECT t.turn_id,e.execution_id,e.job_id,e.session_id,e.thread_id,e.accepted_ingress_id FROM turn_records t JOIN executions e ON e.execution_id=t.last_execution_id WHERE t.turn_id=?", [sent.turnId]);
  assert.equal(parentTurn.length, 1);
  assert.equal(parentTurn[0].job_id, sent.jobId);
  assert.equal(parentTurn[0].session_id, owner.sessionId);
  assert.equal(parentTurn[0].thread_id, owner.threadId);
  const emittingModelCalls = await readFrozenRollout(owner, node,
    "SELECT e.execution_id,e.job_id,e.session_id AS execution_session_id,e.thread_id AS execution_thread_id,e.turn_id,m.model_call_id,m.session_id AS model_session_id,m.thread_id AS model_thread_id,m.execution_id AS model_execution_id FROM model_calls m JOIN executions e ON e.execution_id=m.execution_id WHERE m.model_call_id=? AND e.execution_id=? ORDER BY m.attempt",
    [parentCalls[0], parentTurn[0].execution_id]);
  assert.deepEqual(emittingModelCalls, [{ execution_id: parentTurn[0].execution_id, job_id: sent.jobId,
    execution_session_id: owner.sessionId, execution_thread_id: owner.threadId, turn_id: sent.turnId,
    model_call_id: parentCalls[0], model_session_id: owner.sessionId, model_thread_id: owner.threadId,
    model_execution_id: parentTurn[0].execution_id }],
  "emitting ModelCall 必须由持久 model_calls/executions 证明属于同一父 Session/Thread/Turn/Execution");
  let canonicalToolCarriers = [];
  await waitUntil(async () => {
    canonicalToolCarriers = await readFrozenRollout(owner, node,
      "SELECT item_sequence,item_id,semantic_kind,payload_kind,status,execution_id,turn_id,turn_scope,message_group_id,wire_role,tool_call_id,projection_manifest_json FROM item_catalog WHERE json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? AND semantic_kind='tool_call' ORDER BY item_sequence",
      [parentCalls[0]]);
    return canonicalToolCarriers.length >= 2;
  }, "emitting ModelCall 的 stream 与 checkpoint canonical carrier 持久化");
  assert.equal(canonicalToolCarriers.length, 2, "emitting ModelCall 必须恰有 stream 与 checkpoint 两个 canonical carrier");
  const streamCarriers = canonicalToolCarriers.filter((carrier) => {
    const identity = JSON.parse(carrier.projection_manifest_json).projection_identity;
    return identity?.block_id !== undefined && identity?.block_index !== undefined
      && identity?.projection_message_id === undefined;
  });
  const checkpointCarriers = canonicalToolCarriers.filter((carrier) => {
    const identity = JSON.parse(carrier.projection_manifest_json).projection_identity;
    return identity?.projection_message_id !== undefined
      && identity?.block_id === undefined && identity?.block_index === undefined;
  });
  assert.equal(streamCarriers.length, 1, "emitting ModelCall 必须恰有一个 typed stream carrier");
  assert.equal(checkpointCarriers.length, 1, "emitting ModelCall 必须恰有一个 checkpoint carrier");
  assert.notEqual(streamCarriers[0].item_id, checkpointCarriers[0].item_id,
    "stream 与 checkpoint 必须由两个独立 canonical item 承载");
  const streamCarrier = streamCarriers[0];
  const checkpointCarrier = checkpointCarriers[0];
  const streamManifest = JSON.parse(streamCarrier.projection_manifest_json);
  const checkpointManifest = JSON.parse(checkpointCarrier.projection_manifest_json);
  const streamIdentity = streamManifest.projection_identity;
  const checkpointIdentity = checkpointManifest.projection_identity;
  for (const carrier of canonicalToolCarriers) {
    const identity = JSON.parse(carrier.projection_manifest_json).projection_identity;
    assert.equal(carrier.semantic_kind, "tool_call");
    assert.equal(carrier.payload_kind, "tool_call");
    assert.equal(carrier.status, "completed");
    assert.equal(carrier.execution_id, parentTurn[0].execution_id);
    assert.equal(carrier.turn_id, sent.turnId);
    assert.equal(carrier.turn_scope, "turn_member");
    assert.equal(carrier.wire_role, "assistant");
    assert.equal(identity.model_call_id, parentCalls[0]);
  }
  assert.equal(streamIdentity.block_id, `${parentCalls[0]}:tool-call:${label}`);
  assert.equal(streamIdentity.block_index, 0);
  assert.deepEqual(streamIdentity.content_part_refs, []);
  assert.equal(streamCarrier.tool_call_id, `${parentCalls[0]}:tool-call:${label}`);
  assert.equal(streamManifest.projection_group, null);
  assert.equal(typeof checkpointIdentity.projection_message_id, "string");
  assert.ok(checkpointIdentity.projection_message_id.length > 0);
  assert.equal(checkpointCarrier.tool_call_id, label);
  assert.deepEqual(checkpointManifest.projection_group,
    { content_form: "list", ordinal: 1, size: 2 });
  assert.ok(checkpointCarrier.message_group_id);
  assert.ok(streamCarrier.item_sequence < checkpointCarrier.item_sequence,
    "stream carrier 必须先于 checkpoint projection 持久化");
  const intents = await readFrozenControl(owner, node, "SELECT * FROM thread_execution_intents WHERE admission_idempotency_key=?", [admissionKey]);
  assert.equal(intents.length, 1);
  const intent = intents[0];
  assert.equal(intent.thread_id, childThread);
  assert.equal(intent.state, "pending", "barrier 必须处于 durable admission 之后、pending→bound CAS 之前");
  const child = { ...owner, threadId: childThread, child: true };
  const admissions = await readFrozenRollout(child, node, "SELECT e.*,c.commit_kind,c.status AS commit_status,c.metadata_json FROM executions e JOIN storage_commits c ON c.subject_id=e.execution_id AND c.commit_kind='execution_admission' WHERE e.admission_idempotency_key=?", [admissionKey]);
  assert.equal(admissions.length, 1);
  const admission = admissions[0];
  assert.equal(admission.execution_id, intent.execution_binding_id);
  assert.equal(admission.job_id, intent.job_id);
  assert.equal(admission.turn_id, null);
  assert.equal(admission.accepted_ingress_id, null);
  assert.equal(admission.execution_origin, "internal");
  assert.equal(admission.commit_status, "committed");
  assert.match(intent.binding_preimage_hash, /^[0-9a-f]{64}$/);
  assert.match(admission.admission_payload_hash, /^sha256:jcs:v1:[0-9a-f]{64}$/);
  const metadata = JSON.parse(admission.metadata_json);
  assert.equal(metadata.execution_id, intent.execution_binding_id);
  assert.equal(metadata.job_id, intent.job_id);
  assert.equal(metadata.request_input.kind, "seed_reference");
  const creationRows = await readFrozenControl(owner, node,
    "SELECT thread_creation_idempotency_key,state,child_thread_id,task_seed,task_reference FROM thread_creation_records WHERE child_thread_id=?",
    [childThread]);
  assert.equal(creationRows.length, 1, "initial admission 必须指向唯一冻结的 child creation record");
  const creation = creationRows[0];
  assert.equal(creation.state, "published");
  assert.equal(creation.child_thread_id, childThread);
  assert.equal(creation.thread_creation_idempotency_key, intent.creation_idempotency_key);
  const taskSeed = JSON.parse(creation.task_seed);
  const taskReference = JSON.parse(creation.task_reference);
  assert.equal(taskSeed.description, "读取 README.md 后提交固定初始化结果。");
  assert.equal(taskSeed.subagent_type, "general-purpose");
  assert.equal(taskSeed.parent_job_id, parentJob);
  assert.equal(taskSeed.parent_tool_call_id, label);
  assert.deepEqual(taskSeed.trusted_context, {});
  assert.equal(Object.hasOwn(taskSeed, "untrusted_instructions"), false);
  assert.deepEqual(taskReference, {
    parent_session_id: owner.sessionId,
    parent_thread_id: owner.threadId,
    delegation_id: admissionKey,
  });
  assert.deepEqual(metadata.request_input.reference, {
    creation_idempotency_key: creation.thread_creation_idempotency_key,
    task_seed_hash: `sha256:jcs:v1:${identityPreimageHash(taskSeed)}`,
    reference_binding_hash: `sha256:jcs:v1:${identityPreimageHash(taskReference)}`,
  }, "durable seed reference 必须绑定 barrier 前冻结的完整 task seed/reference");
  const seedControlContext = { ...taskReference, ...taskSeed.trusted_context };
  const encodedControlContext = JSON.stringify(seedControlContext, null, 2)
    .replaceAll("&", "\\u0026").replaceAll("<", "\\u003c").replaceAll(">", "\\u003e");
  const escapedTaskDescription = taskSeed.description
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
  const expectedSeedContent = [
    "<system_reminder>",
    "在当前 child thread 中独立完成已冻结的委派任务；结果由后端按持久 parent thread 地址自动回报。",
    `<control_context encoding="json" trust="control">\n${encodedControlContext}\n</control_context>`,
    `<delegated_task encoding="text" trust="untrusted_data">\n${escapedTaskDescription}\n</delegated_task>`,
    "</system_reminder>",
  ].join("\n");
  const bindingPreimage = Object.fromEntries(["admission_idempotency_key", "session_id", "thread_id", "creation_idempotency_key", "initial_state", "execution_binding_id", "job_id"].map((field) => [field, intent[field]]));
  assert.equal(intent.binding_preimage_hash, identityPreimageHash(bindingPreimage));
  assert.equal(intent.execution_binding_id, `tbind_${createHash("sha256").update(`initial-execution-binding\0${admissionKey}`).digest("hex").slice(0, 32)}`);
  assert.equal(intent.job_id, `job_${createHash("sha256").update(`initial-execution-job\0${admissionKey}`).digest("hex").slice(0, 32)}`);
  const requestPreimage = { session_id: owner.sessionId, thread_id: childThread, execution_id: intent.execution_binding_id,
    job_id: intent.job_id, admission_idempotency_key: admissionKey, input: metadata.request_input };
  assert.equal(admission.admission_payload_hash, `sha256:jcs:v1:${identityPreimageHash(requestPreimage)}`);
  assert.notEqual(admission.admission_payload_hash.split(":").at(-1), intent.binding_preimage_hash, "binding 前像与请求 payload 各自覆盖不同职责");
  const childBinding = (await readFrozenControl(owner, node, "SELECT graph_id,graph_revision,graph_schema_hash,capability_profile_hash FROM thread_catalog WHERE thread_id=?", [childThread]))[0];
  assert.equal((await readFrozenRollout(child, node, "SELECT COUNT(*) AS roots FROM turn_records"))[0].roots, 0);
  const reopen = await replaceNodePage(surfaces.a, [owner, child]);
  const killed = await testControl({ action: "process", name: "a", operation: "kill" });
  await testControl({ action: "release", ...barrier });
  const reportCalls = await planChatCalls(owner.sessionId, owner.threadId, [finalResponse("initial-cas-parent-report", "恢复的 child 报告已处理。")]);
  const reportSourceIdempotencyKey = `subagent-result:${admissionKey}`;
  const reportDigest = createHash("sha256")
    .update(`runtime-source\0${owner.sessionId}\0${owner.threadId}\0${reportSourceIdempotencyKey}`)
    .digest("hex");
  const reportAdmissionKey = `radm_${reportDigest}`;
  const expectedReportExecutionId = `rexec_${reportDigest}`;
  const expectedReportJobId = `rjob_${reportDigest}`;
  // 原父 execution 待执行 task 并非幂等 send/read/wait，恢复必须明确终结，不能盲重放。
  const restarted = await testControl({ action: "process", name: "a", operation: "start" });
  await reopen();
  let bound;
  await waitUntil(async () => {
    bound = (await readControl(owner, "SELECT * FROM thread_execution_intents WHERE admission_idempotency_key=?", [admissionKey]))[0];
    return bound?.state === "bound";
  }, "同 initial key 恢复 bound");
  for (const field of ["admission_idempotency_key", "execution_binding_id", "job_id", "binding_preimage_hash", "thread_id", "creation_idempotency_key"]) assert.equal(bound[field], intent[field]);
  await waitForJobStatus(child, intent.job_id);
  await waitForPlannedExecutions(child, childCalls);
  await waitForPlannedExecutions(owner, reportCalls, "completed", reportAdmissionKey);
  const reportAdmissions = await readRollout(owner,
    "SELECT session_id,thread_id,execution_id,job_id,execution_origin,turn_id,accepted_ingress_id,admission_idempotency_key,outcome FROM executions WHERE session_id=? AND thread_id=? AND admission_idempotency_key=?",
    [owner.sessionId, owner.threadId, reportAdmissionKey]);
  assert.equal(reportAdmissions.length, 1, "精确 report-back admission key 必须只绑定一个 owner execution");
  const reportAdmission = reportAdmissions[0];
  assert.equal(reportAdmission.execution_id, expectedReportExecutionId,
    "report-back execution identity 必须由固定 runtime source 前像派生");
  assert.equal(reportAdmission.job_id, expectedReportJobId,
    "report-back job identity 必须由固定 runtime source 前像派生");
  assert.deepEqual({ session_id: reportAdmission.session_id, thread_id: reportAdmission.thread_id,
    execution_origin: reportAdmission.execution_origin, turn_id: reportAdmission.turn_id,
    accepted_ingress_id: reportAdmission.accepted_ingress_id, admission_idempotency_key: reportAdmission.admission_idempotency_key,
    outcome: reportAdmission.outcome },
  { session_id: owner.sessionId, thread_id: owner.threadId, execution_origin: "internal", turn_id: null,
    accepted_ingress_id: null, admission_idempotency_key: reportAdmissionKey, outcome: "completed" },
  "report-back 必须由精确 key 的成功 internal execution 承载，不能创建用户 Turn");
  const reportJobs = await readRollout(owner,
    "SELECT e.execution_id,e.job_id,e.session_id,e.thread_id,e.execution_origin,e.turn_id,e.accepted_ingress_id,e.admission_idempotency_key,e.outcome,m.model_call_id,m.session_id AS model_session_id,m.thread_id AS model_thread_id,m.execution_id AS model_execution_id,m.outcome AS model_outcome,m.dispatch_state,m.assembly_id FROM executions e JOIN model_calls m ON m.execution_id=e.execution_id WHERE e.execution_id=? AND m.model_call_id IN (" + reportCalls.map(() => "?").join(",") + ") ORDER BY m.attempt",
    [reportAdmission.execution_id, ...reportCalls]);
  assert.deepEqual(reportJobs.map((row) => row.model_call_id).sort(), [...reportCalls].sort(),
    "每个计划 report-back ModelCall 都必须属于精确 admission execution");
  for (const row of reportJobs) {
    assert.deepEqual({ execution_id: row.execution_id, job_id: row.job_id,
      session_id: row.session_id, thread_id: row.thread_id, execution_origin: row.execution_origin,
      turn_id: row.turn_id, accepted_ingress_id: row.accepted_ingress_id,
      admission_idempotency_key: row.admission_idempotency_key, execution_outcome: row.outcome,
      model_session_id: row.model_session_id, model_thread_id: row.model_thread_id,
      model_execution_id: row.model_execution_id, model_outcome: row.model_outcome,
      dispatch_state: row.dispatch_state },
    { execution_id: reportAdmission.execution_id, job_id: reportAdmission.job_id,
      session_id: owner.sessionId, thread_id: owner.threadId, execution_origin: "internal",
      turn_id: null, accepted_ingress_id: null, admission_idempotency_key: reportAdmissionKey,
      execution_outcome: "completed", model_session_id: owner.sessionId, model_thread_id: owner.threadId,
      model_execution_id: reportAdmission.execution_id, model_outcome: "completed", dispatch_state: "completed" });
  }
  const reportAssemblies = await readRollout(owner,
    "SELECT assembly_id,session_id,thread_id,turn_id,execution_id,model_call_id,status,outcome,terminal_at FROM context_assemblies WHERE execution_id=? AND model_call_id IN (" + reportCalls.map(() => "?").join(",") + ") ORDER BY model_call_id",
    [reportAdmission.execution_id, ...reportCalls]);
  assert.deepEqual(reportAssemblies.map((assembly) => assembly.model_call_id).sort(), [...reportCalls].sort(),
    "精确 report-back ModelCall 必须有对应的 owner assembly");
  for (const assembly of reportAssemblies) {
    assert.deepEqual({ session_id: assembly.session_id, thread_id: assembly.thread_id,
      turn_id: assembly.turn_id, execution_id: assembly.execution_id, status: assembly.status,
      outcome: assembly.outcome },
    { session_id: owner.sessionId, thread_id: owner.threadId, turn_id: null,
      execution_id: reportAdmission.execution_id, status: "terminal", outcome: "completed" });
    assert.equal(typeof assembly.terminal_at, "string");
    assert.ok(assembly.terminal_at.length > 0);
  }
  const reportJob = await waitForJobStatus(owner, reportAdmission.job_id);
  assert.ok(["completed", "succeeded"].includes(reportJob.status));
  let oldParentTerminal;
  let oldParentModelCalls;
  await waitUntil(async () => {
    oldParentTerminal = await readRollout(owner,
      "SELECT t.turn_id,t.session_id AS turn_session_id,t.thread_id AS turn_thread_id,t.last_execution_id,t.status AS turn_status,e.execution_id,e.job_id,e.session_id AS execution_session_id,e.thread_id AS execution_thread_id,e.turn_id AS execution_turn_id,e.outcome FROM turn_records t JOIN executions e ON e.execution_id=t.last_execution_id AND e.turn_id=t.turn_id WHERE t.session_id=? AND t.thread_id=? AND t.turn_id=? AND t.last_execution_id=? AND e.execution_id=? AND e.job_id=? AND e.session_id=? AND e.thread_id=? AND e.turn_id=?",
      [owner.sessionId, owner.threadId, sent.turnId, parentTurn[0].execution_id,
        parentTurn[0].execution_id, sent.jobId, owner.sessionId, owner.threadId, sent.turnId]);
    oldParentModelCalls = await readRollout(owner,
      "SELECT model_call_id,execution_id,session_id,thread_id,outcome FROM model_calls WHERE execution_id=? AND model_call_id IN (" + parentCalls.map(() => "?").join(",") + ") ORDER BY model_call_id",
      [parentTurn[0].execution_id, ...parentCalls]);
    return oldParentTerminal.length === 1 && oldParentTerminal[0].turn_status === "unknown"
      && oldParentTerminal[0].outcome === "execution_lost"
      && oldParentModelCalls.length === parentCalls.length
      && oldParentModelCalls.every((call) => call.outcome === "unknown");
  }, "重启后的精确父 Turn/execution 与 emitting ModelCall 明确终结为 unknown/lost");
  assert.deepEqual(oldParentTerminal, [{ turn_id: sent.turnId, turn_session_id: owner.sessionId,
    turn_thread_id: owner.threadId, last_execution_id: parentTurn[0].execution_id, turn_status: "unknown",
    execution_id: parentTurn[0].execution_id, job_id: sent.jobId, execution_session_id: owner.sessionId,
    execution_thread_id: owner.threadId, execution_turn_id: sent.turnId, outcome: "execution_lost" }]);
  assert.deepEqual(oldParentModelCalls, parentCalls.map((modelCallId) => ({ model_call_id: modelCallId,
    execution_id: parentTurn[0].execution_id, session_id: owner.sessionId, thread_id: owner.threadId, outcome: "unknown" })));
  const parentEventsPath = `/api/v1/sessions/${owner.sessionId}/turns/${sent.turnId}/message-stream/events`;
  const readParentEvents = async (afterSeq = 0) => (await ownerApi(owner,
    `${parentEventsPath}?thread_id=${encodeURIComponent(owner.threadId)}&after_seq=${afterSeq}&limit=5000`)).data;
  const startedEvents = (await readParentEvents()).filter((event) => event.type === "tool.started"
    && event.model_call_id === parentCalls[0]
    && event.payload?.tool_call_id === streamCarrier.tool_call_id);
  assert.equal(startedEvents.length, 1, "真实父 Turn 消息流必须唯一记录原 ModelCall 的 tool.started");
  const parentToolStart = startedEvents[0];
  assert.deepEqual([parentToolStart.session_id, parentToolStart.thread_id, parentToolStart.turn_id,
    parentToolStart.job_id, parentToolStart.model_call_id],
  [owner.sessionId, owner.threadId, sent.turnId, sent.jobId, parentCalls[0]]);
  for (const field of ["tool_execution_id", "tool_call_id", "tool_invocation_id", "tool_attempt_id"]) {
    assert.equal(typeof parentToolStart.payload[field], "string", `原 tool.started 缺少真实 ${field}`);
    assert.ok(parentToolStart.payload[field].length > 0);
    assert.equal(parentToolStart[field], parentToolStart.payload[field], `tool.started 信封与 payload 的 ${field} 必须一致`);
  }
  assert.equal(parentToolStart.payload.tool_call_id, streamCarrier.tool_call_id);
  assert.equal(parentToolStart.payload.tool_execution_id, parentToolStart.payload.tool_attempt_id);
  assert.equal(typeof parentToolStart.payload.tool_name, "string");
  assert.ok(parentToolStart.payload.tool_name.length > 0);
  const completedEvents = (await readParentEvents(parentToolStart.event_seq - 1)).filter((event) => event.type === "tool.completed"
    && event.tool_attempt_id === parentToolStart.payload.tool_attempt_id
    && event.payload?.tool_attempt_id === parentToolStart.payload.tool_attempt_id);
  assert.equal(completedEvents.length, 1, "原 tool attempt 必须由真实父消息流唯一收口");
  const parentToolCompletion = completedEvents[0];
  assert.ok(parentToolStart.event_seq < parentToolCompletion.event_seq,
    "同一父 Turn 消息流中 tool.started 必须先于 tool.completed");
  assert.deepEqual([parentToolCompletion.session_id, parentToolCompletion.thread_id, parentToolCompletion.turn_id,
    parentToolCompletion.job_id, parentToolCompletion.model_call_id],
  [owner.sessionId, owner.threadId, sent.turnId, sent.jobId, parentCalls[0]]);
  for (const field of ["tool_execution_id", "tool_call_id", "tool_invocation_id", "tool_attempt_id"]) {
    assert.equal(parentToolCompletion.payload[field], parentToolStart.payload[field], `tool.completed 必须沿用原 ${field}`);
    assert.equal(parentToolCompletion[field], parentToolStart.payload[field], `tool.completed 信封必须沿用原 ${field}`);
  }
  assert.equal(parentToolCompletion.payload.status, "completed");
  assert.equal(parentToolCompletion.payload.outcome, "outcome_unknown");
  assert.equal(parentToolCompletion.payload.completion_reason, "execution_lost");
  assert.equal(typeof parentToolCompletion.payload.result, "string");
  const canonicalToolResults = await readRollout(owner,
    "SELECT item_id,semantic_kind,payload_kind,status,execution_id,turn_id,tool_call_id,content_hash,projection_manifest_json FROM item_catalog WHERE turn_id=? AND execution_id=? AND semantic_kind='tool_result' AND json_extract(projection_manifest_json,'$.projection_identity.model_call_id')=? ORDER BY item_sequence",
    [sent.turnId, parentTurn[0].execution_id, parentCalls[0]]);
  assert.equal(canonicalToolResults.length, 1, "initial admission 崩溃前没有 task ToolMessage，恢复必须只提交原 attempt 的 A result");
  const canonicalToolAttemptId = parentToolStart.payload.tool_attempt_id;
  const canonicalToolCallId = parentToolStart.payload.tool_call_id;
  const attemptCarrierId = `item-${canonicalToolAttemptId}-result-${canonicalToolCallId}`;
  const canonicalToolResult = canonicalToolResults[0];
  assert.equal(canonicalToolResult.item_id, attemptCarrierId,
    "A 必须按原 tool attempt 与 scoped call identity 精确定位，不能按 catalog 首行猜测");
  assert.deepEqual({ semantic_kind: canonicalToolResult.semantic_kind, payload_kind: canonicalToolResult.payload_kind,
    status: canonicalToolResult.status, execution_id: canonicalToolResult.execution_id,
    turn_id: canonicalToolResult.turn_id, tool_call_id: canonicalToolResult.tool_call_id },
  { semantic_kind: "tool_result", payload_kind: "tool_result", status: "completed",
    execution_id: parentTurn[0].execution_id, turn_id: sent.turnId,
    tool_call_id: parentToolStart.payload.tool_call_id });
  assert.equal(canonicalToolResult.item_id,
    `item-${parentToolStart.payload.tool_attempt_id}-result-${parentToolStart.payload.tool_call_id}`);
  const resultManifest = JSON.parse(canonicalToolResult.projection_manifest_json);
  assert.equal(resultManifest.projection_identity.model_call_id, parentCalls[0]);
  assert.equal(Object.hasOwn(resultManifest.projection_identity, "projection_message_id"), false,
    "A 由原 live tool attempt 承载，不得冒充 checkpoint projection");
  const parentHistory = await loadThreadHistory(owner, { turn_ids: [sent.turnId] });
  assert.equal(parentHistory.items.length, 1);
  const recoveredTurn = parentHistory.items[0];
  const canonicalResultParts = recoveredTurn.response_parts.filter((part) => part.kind === "tool_result"
    && part.tool_call_id === canonicalToolResult.tool_call_id);
  assert.equal(canonicalResultParts.length, 1, "公开 HTTP 历史必须暴露唯一的 typed unknown result");
  const canonicalResultPart = canonicalResultParts[0];
  assert.equal(canonicalResultPart.source.item_id, canonicalToolResult.item_id,
    "HTTP history 必须通过 response part source identity 精确联结真实 A row");
  assert.equal(canonicalResultPart.truncated, false, "校验 canonical 与 Provider 正文前必须读取完整工具结果");
  assert.equal(canonicalResultPart.result, parentToolCompletion.payload.result);
  assert.equal(canonicalResultPart.tool_call_id, parentToolStart.payload.tool_call_id);
  assert.equal(typeof canonicalResultPart.tool_name, "string");
  assert.ok(canonicalResultPart.tool_name.length > 0);
  const canonicalResultPayload = {
    tool_call_id: parentToolStart.payload.tool_call_id,
    tool_invocation_id: parentToolStart.payload.tool_invocation_id,
    tool_attempt_id: parentToolStart.payload.tool_attempt_id,
    result_id: parentToolStart.payload.tool_attempt_id,
    name: canonicalResultPart.tool_name,
    content: canonicalResultPart.result,
    tool_outcome: "unknown",
    execution_confirmed: false,
  };
  const orderedResultPayload = Object.fromEntries(Object.keys(canonicalResultPayload).sort()
    .map((field) => [field, canonicalResultPayload[field]]));
  const canonicalResultHashInput = { payload: orderedResultPayload, payload_kind: "tool_result" };
  assert.equal(canonicalToolResult.content_hash,
    `sha256:jcs:v1:${createHash("sha256").update(JSON.stringify(canonicalResultHashInput)).digest("hex")}`,
    "A content hash 必须绑定原 typed invocation/attempt、完整正文及 unknown/unconfirmed 语义");
  const reportRequest = await modelRequest(reportCalls[0]);
  const providerMessages = reportRequest.request.messages;
  const providerToolMessageIndexes = providerMessages.flatMap((message, index) =>
    message.role === "tool" && message.tool_call_id === checkpointCarrier.tool_call_id ? [index] : []);
  const providerToolMessages = providerToolMessageIndexes.map((index) => providerMessages[index]);
  assert.equal(providerToolMessages.length, 1, "report Provider 必须接收一次与原调用身份配对的 ToolMessage");
  assert.equal(providerToolMessages[0].content, canonicalResultPart.result,
    "report Provider 必须实际消费 HTTP canonical history 返回的完整 unknown result 正文");
  const providerDeclarationIndexes = providerMessages.flatMap((message, index) =>
    message.role === "assistant" && Array.isArray(message.tool_calls)
      && message.tool_calls.some((call) => call.id === checkpointCarrier.tool_call_id) ? [index] : []);
  assert.equal(providerDeclarationIndexes.length, 1,
    "report Provider 必须保留同一 typed emitting ModelCall 的唯一原始 call declaration");
  assert.ok(providerDeclarationIndexes[0] < providerToolMessageIndexes[0],
    "Provider ToolMessage 必须位于同一原始 call id 的 assistant declaration 之后");
  await assertAcceptedTurn(owner, sent);
  const recovered = await readRollout(child, "SELECT execution_id,job_id,turn_id,accepted_ingress_id,admission_idempotency_key,admission_payload_hash,outcome FROM executions");
  assert.deepEqual(recovered, [{ execution_id: intent.execution_binding_id, job_id: intent.job_id, turn_id: null, accepted_ingress_id: null,
    admission_idempotency_key: admissionKey, admission_payload_hash: admission.admission_payload_hash, outcome: "completed" }]);
  const commits = await readRollout(child, "SELECT metadata_json FROM storage_commits WHERE commit_kind='execution_admission'");
  assert.deepEqual(commits, [{ metadata_json: admission.metadata_json }], "恢复只能找回同一 durable admission");
  const modelCalls = await readRollout(child,
    "SELECT e.execution_id,e.job_id,e.session_id,e.thread_id,e.execution_origin,e.turn_id,e.accepted_ingress_id,e.admission_idempotency_key,e.admission_payload_hash,e.outcome,e.last_model_call_id,m.model_call_id,m.session_id AS model_session_id,m.thread_id AS model_thread_id,m.execution_id AS model_execution_id,m.outcome AS model_outcome,m.dispatch_state FROM executions e JOIN model_calls m ON m.execution_id=e.execution_id WHERE e.execution_id=? ORDER BY m.attempt",
    [intent.execution_binding_id]);
  assert.deepEqual(modelCalls, [{ execution_id: intent.execution_binding_id, job_id: intent.job_id,
    session_id: owner.sessionId, thread_id: childThread, execution_origin: "internal", turn_id: null,
    accepted_ingress_id: null, admission_idempotency_key: admissionKey,
    admission_payload_hash: admission.admission_payload_hash, outcome: "completed",
    last_model_call_id: childCalls[0], model_call_id: childCalls[0], model_session_id: owner.sessionId,
    model_thread_id: childThread, model_execution_id: intent.execution_binding_id,
    model_outcome: "completed", dispatch_state: "completed" }],
  "恢复后的 ModelCall 必须沿同一 admitted execution/job/owner 完成且不生成用户 Turn");
  const childRequest = await modelRequest(childCalls[0]);
  const seedMessages = childRequest.request.messages.filter((message) => message.role === "user" && message.content === expectedSeedContent);
  assert.deepEqual(seedMessages.map(({ role, content }) => ({ role, content })),
    [{ role: "user", content: expectedSeedContent }], "恢复后的真实 Provider request 必须发送完整、精确的冻结 seed 正文");
  assert.deepEqual(await graphBinding(child), childBinding);
  await openOwner(child);
  const displayed = await verifyOwnerTurns(child);
  assert.equal(displayed.turns.length, 0);
  assert.equal(displayed.internal.length, 1, "恢复的初始执行必须只投影为一个 typed internal history entry");
  const internal = displayed.internal[0];
  assert.equal(internal.execution_id, modelCalls[0].execution_id);
  assert.equal(internal.job_id, modelCalls[0].job_id);
  assert.equal(internal.session_id, modelCalls[0].session_id);
  assert.equal(internal.thread_id, modelCalls[0].thread_id);
  assert.equal(internal.status, "completed");
  assert.deepEqual(internal.response_parts.filter((part) => part.kind === "text" || part.kind === "final_text").map((part) => part.text),
    ["初始化报告的私有正文。"], "typed child history 必须保留同一初始 ModelCall 的精确响应正文");
  return { admissionKey, parentTurn, intent, bindingPreimage, requestPreimage, admission, bound, recovered, commits, childBinding, killed, restarted, parentTerminal: oldParentTerminal[0], displayed };
}

function identityPreimageHash(value) {
  // admission 前像与冻结 seed/reference 都只含字符串和对象；排序键后的 JSON.stringify 字节与 JCS 一致。
  const canonical = (entry) => {
    if (typeof entry === "string") return entry;
    assert.ok(entry && typeof entry === "object" && !Array.isArray(entry), "身份前像只能包含字符串或对象");
    return Object.fromEntries(Object.keys(entry).sort().map((key) => [key, canonical(entry[key])]));
  };
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

async function createGeneratedReport(target, label, responses) {
  // 生成分支和回报都通过公开 Gateway API；不预写 notice、Turn 或投影。
  const prompt = `生成分支 ${label} 的私有任务正文，不允许在主会话 notice DOM 中显示。`;
  const privateResult = `生成分支 ${label} 的私有结果正文，不允许在主会话 notice DOM 中显示。`;
  const displayText = "生成分支已结束，主会话正在处理返回结果。";
  const generatedId = `ses_${await testControl({ action: "next_uuid", namespace: "ses" })}`;
  const generatedThread = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
  const branchCalls = await planChatCalls(generatedId, generatedThread, [finalResponse(`generated-${label}-private-final`, privateResult)]);
  const reportCalls = await planChatCalls(target.sessionId, target.threadId, responses);
  const definition = (await ownerApi(target, "/api/gateway/session-generators", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      name: `固定会话生成 ${label}`, generator_type: { type_id: "builtin.agent_prompt", version: "1" }, enabled: true,
      trigger: { type: "manual", timezone: "UTC" }, placement: { kind: "workspace", workspace_id: target.gatewayWorkspaceId },
      execution_workspace_id: target.gatewayWorkspaceId, context_source: { kind: "fresh" }, naming: { title_template: "生成结果验收", path_template: [] },
      session_strategy: { mode: "fork_new_and_report_back", target: { workspace_id: target.gatewayWorkspaceId, session_id: target.sessionId, thread_id: target.threadId }, concurrency: "queue", report_back: "continue_agent" },
      config: { prompt, session_title: "生成结果验收" },
    }),
  })).data;
  assert.equal(definition.status, "ready");
  const manualKey = `section8-generated-${target.sessionId}`;
  const run = (await ownerApi(target, `/api/gateway/session-generators/${definition.generator_id}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: manualKey }),
  })).data;
  assert.equal(run.outputs.length, 1);
  assert.equal(run.outputs[0].session_id, generatedId);
  const generated = { ...target, sessionId: generatedId, threadId: generatedThread, child: false };
  await sessionCatalogNode(generated);
  await waitForJobStatus(generated, run.job_id);
  let terminal;
  await waitUntil(async () => {
    terminal = (await ownerApi(target, `/api/gateway/session-generators/${definition.generator_id}/runs`)).data.items.find((item) => item.run_id === run.run_id);
    if (terminal?.status === "failed") throw new Error(`生成会话失败: ${JSON.stringify(terminal)}`);
    return terminal?.status === "completed";
  }, "真实生成分支与主会话 internal 回报结束", 180_000);
  assert.equal(typeof terminal.report_back_job_id, "string");
  await waitForJobStatus(target, terminal.report_back_job_id);
  await waitForPlannedExecutions(target, reportCalls);
  await waitForPlannedExecutions(generated, branchCalls);
  assert.ok(branchCalls.length > 0, "生成分支必须有真实 Provider 调用");
  assert.ok(reportCalls.length > 0, "主会话回报必须有真实 Provider 调用");
  const [branchRequests, reportRequests] = await Promise.all([
    Promise.all(branchCalls.map(modelRequest)),
    Promise.all(reportCalls.map(modelRequest)),
  ]);
  const generatedFirstInput = JSON.stringify(branchRequests[0].request.messages);
  const reportFirstInput = JSON.stringify(reportRequests[0].request.messages);
  const generatedFirstContainsPrompt = generatedFirstInput.includes(prompt);
  const reportFirstContainsPrivateResult = reportFirstInput.includes(privateResult);
  const safeDisplayExcludedFromEveryRequest = [...branchRequests, ...reportRequests].every(
    (entry) => !JSON.stringify(entry.request.messages).includes(displayText),
  );
  assert.ok(generatedFirstContainsPrompt, "生成分支首个真实 Provider 请求必须包含生成 prompt");
  assert.ok(reportFirstContainsPrivateResult, "回报首个真实 Provider 请求必须包含私有生成结果");
  assert.ok(safeDisplayExcludedFromEveryRequest, "安全展示文案不得进入生成分支或回报 Provider 请求");
  const providerWireEvidence = {
    branchCallIds: branchCalls,
    reportCallIds: reportCalls,
    generatedFirstContainsPrompt,
    reportFirstContainsPrivateResult,
    safeDisplayExcludedFromEveryRequest,
  };
  return { prompt, privateResult, displayText, generatedId, generatedThread, branchCalls, reportCalls, providerWireEvidence, definition, manualKey, run, generated, terminal };
}

async function runGeneratedInternalHistory(surfaces) {
  const target = await createProductSession(surfaces.c, "会话生成安全展示与 composite cursor");
  const baseline = await completeOwnerTurn(target, "保留原用户根及活动统计。", [finalResponse("generation-baseline", "原用户轮已完成。")]);
  const before = (await forkHistoryEvidence(target)).history;
  const { prompt, privateResult, displayText, generatedId, generatedThread, reportCalls, providerWireEvidence, definition, manualKey, run, generated, terminal }
    = await createGeneratedReport(target, "composite", [
      { label: "generated-internal-many", reasoning: "在精确 internal execution 中核对工具。", calls: Array.from({ length: 130 }, (_, index) => ({ id: `generated-read-${index}`, name: "read_file", arguments: { path: "README.md" } })) },
      finalResponse("generated-internal-final", "主会话已处理生成结果。"),
    ]);
  const roots = await readRollout(target, "SELECT (SELECT COUNT(*) FROM turn_records) AS turns,(SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='user_input') AS roots");
  assert.deepEqual(roots, [{ turns: before.items.length, roots: before.items.length }], "生成回报不能增加用户根或 Turn");
  const admissions = await readRollout(target,
    "SELECT e.session_id,e.thread_id,e.execution_id,e.job_id,e.execution_origin,e.turn_id,e.accepted_ingress_id,e.admission_idempotency_key,e.admission_payload_hash,c.commit_id,c.status AS commit_status,c.commit_mode,c.jsonl_record_count,c.metadata_json FROM executions e JOIN storage_commits c ON c.subject_id=e.execution_id AND c.commit_kind='execution_admission' AND c.idempotency_key='internal-admission:'||e.admission_idempotency_key WHERE e.job_id=?",
    [terminal.report_back_job_id]);
  assert.equal(admissions.length, 1);
  const admission = admissions[0];
  assert.equal(admission.turn_id, null);
  assert.equal(admission.accepted_ingress_id, null);
  assert.equal(admission.execution_origin, "internal");
  assert.deepEqual([admission.session_id, admission.thread_id, admission.job_id],
    [target.sessionId, target.threadId, terminal.report_back_job_id]);
  const reportCallRows = await readRollout(target,
    `SELECT m.model_call_id,m.session_id AS model_session_id,m.thread_id AS model_thread_id,m.execution_id AS model_execution_id,m.outcome AS model_outcome,m.dispatch_state,e.execution_id,e.job_id,e.session_id AS execution_session_id,e.thread_id AS execution_thread_id,e.execution_origin,e.turn_id,e.accepted_ingress_id,e.outcome AS execution_outcome FROM model_calls m JOIN executions e ON e.execution_id=m.execution_id WHERE m.model_call_id IN (${reportCalls.map(() => "?").join(",")}) AND e.job_id=? ORDER BY m.attempt`,
    [...reportCalls, terminal.report_back_job_id]);
  assert.deepEqual(reportCallRows.map((row) => row.model_call_id).sort(), [...reportCalls].sort(),
    "每个计划的 report-back ModelCall 都必须属于唯一 admission 的 job");
  for (const row of reportCallRows) {
    assert.deepEqual({
      model_session_id: row.model_session_id,
      model_thread_id: row.model_thread_id,
      model_execution_id: row.model_execution_id,
      model_outcome: row.model_outcome,
      dispatch_state: row.dispatch_state,
      execution_id: row.execution_id,
      job_id: row.job_id,
      execution_session_id: row.execution_session_id,
      execution_thread_id: row.execution_thread_id,
      execution_origin: row.execution_origin,
      turn_id: row.turn_id,
      accepted_ingress_id: row.accepted_ingress_id,
      execution_outcome: row.execution_outcome,
    }, {
      model_session_id: target.sessionId,
      model_thread_id: target.threadId,
      model_execution_id: admission.execution_id,
      model_outcome: "completed",
      dispatch_state: "completed",
      execution_id: admission.execution_id,
      job_id: admission.job_id,
      execution_session_id: admission.session_id,
      execution_thread_id: admission.thread_id,
      execution_origin: "internal",
      turn_id: null,
      accepted_ingress_id: null,
      execution_outcome: "completed",
    }, "每个 report-back ModelCall 必须沿同一 internal admission 和 owner 完成");
  }
  assert.equal(admission.commit_status, "committed");
  assert.equal(admission.commit_mode, "item_bearing");
  assert.equal(admission.jsonl_record_count, 1);
  assert.match(admission.admission_payload_hash, /^sha256:jcs:v1:[0-9a-f]{64}$/);
  const admissionMetadata = JSON.parse(admission.metadata_json);
  assert.equal(admissionMetadata.execution_id, admission.execution_id);
  assert.equal(admissionMetadata.job_id, admission.job_id);
  assert.equal(admissionMetadata.request_input.kind, "runtime_notice");
  const notices = await readRollout(target,
    "SELECT item_id,item_sequence,turn_id,turn_scope,semantic_kind,payload_kind,content_hash,commit_id,jsonl_offset,jsonl_length FROM item_catalog WHERE execution_id=? AND semantic_kind='runtime_notice'",
    [admission.execution_id]);
  assert.equal(notices.length, 1);
  assert.equal(notices[0].turn_id, null);
  assert.equal(notices[0].turn_scope, "pending_next_turn");
  assert.equal(notices[0].item_id, admissionMetadata.notice_item_id);
  assert.equal(notices[0].commit_id, admission.commit_id);
  const targetCatalog = await sessionCatalogNode(target);
  const rolloutIndex = await threadRolloutLocator(target, targetCatalog);
  const jsonlBytes = await readFile(join(target.workspace, rolloutIndex.replace(/index\.sqlite$/, "rollout.jsonl")));
  const canonicalNotice = JSON.parse(jsonlBytes.subarray(notices[0].jsonl_offset,
    notices[0].jsonl_offset + notices[0].jsonl_length).toString("utf8"));
  assert.deepEqual([canonicalNotice.item_id, canonicalNotice.item_sequence, canonicalNotice.semantic_kind,
    canonicalNotice.turn_scope, canonicalNotice.payload_kind, canonicalNotice.content_hash],
  [notices[0].item_id, notices[0].item_sequence, "runtime_notice", "pending_next_turn", notices[0].payload_kind, notices[0].content_hash]);
  assert.equal(canonicalNotice.turn_id ?? null, null);
  assert.deepEqual(admissionMetadata.request_input, { kind: "runtime_notice", payload: canonicalNotice.payload },
    "唯一 durable admission 必须冻结实际 canonical runtime_notice 的完整 payload");
  const admissionPreimage = { session_id: target.sessionId, thread_id: target.threadId,
    execution_id: admission.execution_id, job_id: admission.job_id,
    admission_idempotency_key: admission.admission_idempotency_key, input: admissionMetadata.request_input };
  assert.equal(admission.admission_payload_hash, `sha256:jcs:v1:${identityPreimageHash(admissionPreimage)}`,
    "RuntimeNotice admission hash 必须逐字覆盖原 owner、execution/job、key 与实际 request_input");
  assert.equal(canonicalNotice.content_hash,
    `sha256:jcs:v1:${identityPreimageHash({ payload_kind: canonicalNotice.payload_kind, payload: canonicalNotice.payload })}`);
  assert.ok(canonicalNotice.payload.prompt.includes(privateResult));
  const reportInput = (await modelRequest(reportCalls[0])).request.messages;
  assert.ok(reportInput.some((message) => message.role === "user" && message.content === canonicalNotice.payload.prompt),
    "真实 report-back 首个 Provider 请求必须消费同一 notice 的私有 prompt");
  const pages = [];
  let cursor;
  const partIds = new Set();
  const internal = { ...admission, session_id: target.sessionId, thread_id: target.threadId, display_parts: [], response_parts: [] };
  do {
    const page = await loadThreadHistory(target, { turns: undefined, execution_id: admission.execution_id, direction: cursor ? "before" : "tail", cursor });
    assert.equal(page.items.length, 0, "execution selector 不能返回既有用户 Turn");
    const entries = page.internal_executions;
    assert.equal(entries.length, 1);
    const entry = entries[0];
    assert.equal(entry.execution_id, admission.execution_id);
    assert.equal(entry.status, "completed");
    const pageRecords = new Set([...entry.display_parts.map((part) => part.item_id), ...entry.response_parts.map((part) => part.source.item_id)]);
    assert.ok(pageRecords.size <= 256, "每页 internal record 上限为 256");
    for (const part of entry.response_parts) {
      assert.ok(!partIds.has(part.part_id), "复合 cursor 不得重复 part");
      partIds.add(part.part_id);
    }
    internal.display_parts.push(...entry.display_parts);
    internal.response_parts.push(...entry.response_parts);
    pages.push(page);
    cursor = page.has_more ? page.next_cursor : null;
    if (page.has_more) assert.equal(typeof cursor, "string");
  } while (cursor);
  assert.ok(pages.length > 1, "真实 internal execution 必须超过单页 256 record");
  internal.response_parts.sort((left, right) => left.source.logical_item_ordinal - right.source.logical_item_ordinal || left.source.part_ordinal - right.source.part_ordinal);
  internal.display_parts.sort((left, right) => left.logical_item_ordinal - right.logical_item_ordinal);
  assert.equal(internal.display_parts.length, 1);
  const safe = internal.display_parts[0];
  assert.deepEqual([safe.item_id, safe.item_sequence, safe.kind, safe.text],
    [canonicalNotice.item_id, canonicalNotice.item_sequence, canonicalNotice.payload.display_part.kind, canonicalNotice.payload.display_part.text]);
  assert.deepEqual(Object.fromEntries(Object.entries(safe.provenance).filter(([, value]) => value !== null)),
    canonicalNotice.payload.display_part.provenance, "后继 history API 的 typed safe display 必须直接对应持久 notice");
  assert.equal(safe.kind, "generated_session_result");
  assert.equal(safe.text, displayText);
  assert.equal(safe.provenance.source_session_id, generatedId);
  assert.equal(safe.provenance.source_thread_id, generatedThread);
  for (const field of ["source_execution_id", "source_item_id"]) assert.equal(typeof safe.provenance[field], "string");
  const full = (await forkHistoryEvidence(target)).history;
  assert.deepEqual(turnsWithoutHistorySnapshot(full.items), turnsWithoutHistorySnapshot(before.items),
    "notice 与 internal response 不得改变原 Turn 身份、顺序、状态、正文或活动统计");
  assert.equal(full.internal_executions.length, 1);
  assert.equal(full.internal_executions[0].execution_id, admission.execution_id);
  assert.equal(full.internal_executions[0].status, "completed");
  assert.deepEqual(full.internal_executions[0].display_parts, internal.display_parts);
  assert.deepEqual(full.internal_executions[0].response_parts, internal.response_parts, "独立 execution 与 composite 双 lane 读取不得静默丢失输出");
  const membership = await readRollout(target,
    "SELECT m.item_id,m.logical_item_ordinal,m.visible,m.source_kind,m.context_selected,i.semantic_kind,i.payload_kind,i.projection_manifest_json FROM context_view_items m JOIN item_catalog i USING(item_id) JOIN branches b ON b.head_view_id=m.view_id JOIN database_meta d ON d.active_branch_id=b.branch_id WHERE m.visible=1 AND m.source_kind='display_only' AND i.execution_id=? ORDER BY m.logical_item_ordinal",
    [admission.execution_id]);
  assert.ok(membership.length > 256);
  const typedMembership = expectedTypedInternalHistoryMembership(membership);
  const finalResponseContent = finalResponse("generated-internal-final", "主会话已处理生成结果。");
  const finalParts = internal.response_parts.filter((part) => part.source.item_id === typedMembership.finalCarrier.item_id);
  assert.deepEqual(finalParts.map((part) => [part.kind, part.text, part.source.part_ordinal]), [
    ["reasoning", finalResponseContent.reasoning, 0],
    ["text", finalResponseContent.text, 1],
  ], "canonical final 的 reasoning 与 text 必须各保留一次并落在原 typed part 坐标");
  const visibleRecords = new Map();
  const recordVisibleItem = (itemId, ordinal) => {
    const existingOrdinal = visibleRecords.get(itemId);
    if (existingOrdinal !== undefined) {
      assert.equal(existingOrdinal, ordinal, `同一 public item 不得映射到多个逻辑 ordinal: ${itemId}`);
      return;
    }
    visibleRecords.set(itemId, ordinal);
  };
  for (const part of internal.display_parts) recordVisibleItem(part.item_id, part.logical_item_ordinal);
  for (const part of internal.response_parts) recordVisibleItem(part.source.item_id, part.source.logical_item_ordinal);
  assert.deepEqual([...visibleRecords].sort((left, right) => left[1] - right[1]),
    typedMembership.visibleMembership,
    "public history 必须逐项保留所有非 shadow membership 的 identity 和顺序；shadow 只能来自 final typed refs/replaces");
  const history = full;
  await openOwner(target);
  await assertTranscriptDom(target, history, "generated live");
  await assertInternalHistory(target, history, "generated live");
  const html = await target.page.locator(".chat-internal-execution-display").evaluateAll((elements) => elements.map((element) => element.outerHTML).join(""));
  for (const hidden of [prompt, privateResult, generatedId, generatedThread, run.run_id, definition.generator_id, admission.execution_id, admission.job_id]) assert.ok(!html.includes(hidden), "notice DOM 只允许安全文本和固定标签");
  const defaultInclude = await loadCompleteThreadHistory(target, { include: undefined });
  assert.ok(defaultInclude.internal_executions.flatMap((entry) => entry.display_parts).some((part) => part.item_id === safe.item_id), "默认 include 必须保留 internal 安全 notice");
  const assemblies = await readRollout(target, "SELECT session_id,thread_id,execution_id,turn_id,assembly_id,model_call_id,status,outcome FROM context_assemblies WHERE execution_id=?", [admission.execution_id]);
  assert.equal(assemblies.length, reportCalls.length);
  assert.deepEqual(assemblies.map((row) => row.model_call_id).sort(), [...reportCalls].sort(),
    "每个 report-back ModelCall 必须关联唯一 terminal assembly");
  assert.ok(assemblies.every((row) => row.session_id === target.sessionId && row.thread_id === target.threadId
    && row.execution_id === admission.execution_id && row.turn_id === null
    && row.status === "terminal" && row.outcome === "completed"));
  const crossOwner = await ownerApiResponse(generated, `/api/v1/sessions/${generatedId}/threads/${generatedThread}/history`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ direction: "before", include: HISTORY_INCLUDE, execution_id: admission.execution_id, cursor: pages[0].next_cursor }),
  });
  assert.equal(crossOwner.status, 400);
  assert.match(JSON.stringify(crossOwner.body), /历史游标不属于当前 rollout/);
  await openOwner(target);
  await assertTranscriptDom(target, history, "generated refresh");
  await assertInternalHistory(target, history, "generated refresh");
  const duplicate = (await ownerApi(target, `/api/gateway/session-generators/${definition.generator_id}/run`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ idempotency_key: manualKey }),
  })).data;
  assert.equal(duplicate.run_id, run.run_id);
  assert.deepEqual(duplicate.outputs, terminal.outputs);
  assert.equal((await readRollout(target, "SELECT COUNT(*) AS count FROM executions WHERE job_id=?", [admission.job_id]))[0].count, 1);
  // 同一 revision 下按首次上界读完双 lane；Turn lane 先耗尽不能重读 root。
  const frozenFirst = await loadThreadHistory(target, { direction: "tail", turns: 1 });
  assert.equal(frozenFirst.has_more, true);
  assert.deepEqual(frozenFirst.items.map((turn) => turn.turn_id), [baseline.sent.turnId]);
  const frozenPages = [frozenFirst];
  while (frozenPages.at(-1).has_more) {
    const previous = frozenPages.at(-1);
    assert.equal(typeof previous.next_cursor, "string");
    const page = await loadThreadHistory(target, { direction: "before", turns: 1, cursor: previous.next_cursor });
    assert.equal(page.items.length, 0, "已耗尽的 Turn lane 不得重复 root");
    frozenPages.push(page);
  }
  const frozenParts = frozenPages.flatMap((page) => page.internal_executions.flatMap((entry) => entry.response_parts));
  const frozenDisplays = frozenPages.flatMap((page) => page.internal_executions.flatMap((entry) => entry.display_parts));
  assert.equal(new Set(frozenParts.map((part) => part.part_id)).size, frozenParts.length);
  assert.equal(new Set(frozenDisplays.map((part) => part.item_id)).size, frozenDisplays.length);
  assert.deepEqual(frozenParts.map((part) => part.part_id).sort(), internal.response_parts.map((part) => part.part_id).sort(), "冻结上界下的 internal 输出必须完整且只出现一次");
  assert.deepEqual(frozenDisplays.map((part) => part.item_id).sort(), internal.display_parts.map((part) => part.item_id).sort());
  for (const page of frozenPages) {
    const records = new Set(page.internal_executions.flatMap((entry) => [...entry.display_parts.map((part) => part.item_id), ...entry.response_parts.map((part) => part.source.item_id)]));
    assert.ok(records.size <= 256);
  }
  const appended = await completeOwnerTurn(target, "首读之后追加新用户轮，旧 history cursor 必须明确 stale。", [
    finalResponse("generated-snapshot-later-turn", "只属于首读之后的新用户轮。"),
  ]);
  const invalidDirection = await ownerApiResponse(target, `/api/v1/sessions/${target.sessionId}/threads/${target.threadId}/history`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ direction: "after", turns: 1, include: HISTORY_INCLUDE, cursor: frozenFirst.next_cursor }),
  });
  assert.equal(invalidDirection.status, 400);
  assert.match(JSON.stringify(invalidDirection.body), /direction 与请求不一致/);
  const stale = await ownerApiResponse(target, `/api/v1/sessions/${target.sessionId}/threads/${target.threadId}/history`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ direction: "before", turns: 1, include: HISTORY_INCLUDE, cursor: frozenFirst.next_cursor }),
  });
  assert.equal(stale.status, 409);
  assert.equal(stale.body.detail.code, "stale_turn_cursor");
  assert.equal(stale.body.detail.reason, "history_view_revision");
  assert.ok(stale.body.detail.current_history_view_revision > stale.body.detail.cursor_history_view_revision);
  const aroundOptions = { direction: "around", anchor_turn_id: baseline.sent.turnId, turns: undefined, before_turns: 0, after_turns: 1 };
  const aroundPages = [await loadThreadHistory(target, aroundOptions)];
  assert.equal(aroundPages[0].has_after, true, "密集 internal 窗口必须提供 after_cursor");
  while (aroundPages.at(-1).has_after) {
    const previous = aroundPages.at(-1);
    assert.equal(typeof previous.after_cursor, "string");
    const page = await loadThreadHistory(target, { direction: "after", turns: undefined, cursor: previous.after_cursor });
    assert.equal(page.items.length, 0, "around 窗口已返回的 root 不得在 after 续读重影");
    aroundPages.push(page);
  }
  const aroundDense = mergeThreadHistoryPages(aroundPages);
  assert.deepEqual(aroundDense.items.map((turn) => turn.turn_id), [baseline.sent.turnId, appended.sent.turnId]);
  assert.deepEqual(aroundDense.internal_executions, full.internal_executions, "两个 root 之间超过 256 个 display-only 成员不能在 around 分页中丢失或重影");
  await openOwner(target);
  await assertTranscriptDom(target, aroundDense, "generated dense gap live");
  await assertInternalHistory(target, aroundDense, "generated dense gap live");
  await openOwner(target);
  await assertTranscriptDom(target, aroundDense, "generated dense gap refresh");
  await assertInternalHistory(target, aroundDense, "generated dense gap refresh");
  const around = await loadCompleteThreadHistory(target, { direction: "around", anchor_turn_id: baseline.sent.turnId, turns: undefined, before_turns: 0, after_turns: 0 });
  assert.deepEqual(around.items.map((turn) => turn.turn_id), [baseline.sent.turnId]);
  const fork = await forkOwner(target);
  const forkDisplay = fork.targetHistory.internal_executions.flatMap((entry) => entry.display_parts);
  assert.deepEqual(forkDisplay.map((part) => [part.text, part.logical_item_ordinal]), internal.display_parts.map((part) => [part.text, part.logical_item_ordinal]), "fork 必须从既有 membership 重映射保真顺序");
  // 零 Turn 的首部 notice 与随后第一个用户 root 共用 view 坐标，不能制造用户活动。
  const head = await createProductSession(surfaces.c, "零 Turn 的首部生成 notice");
  const headReport = await createGeneratedReport(head, "head", [
    finalResponse("generated-head-internal-final", "首部生成回报已处理。"),
  ]);
  const zero = await loadCompleteThreadHistory(head);
  assert.deepEqual(zero.items, []);
  assert.equal(zero.internal_executions.length, 1);
  const headNotice = zero.internal_executions[0].display_parts[0];
  assert.equal(zero.internal_executions[0].display_parts.length, 1);
  assert.equal(headNotice.kind, "generated_session_result");
  assert.equal(headNotice.text, headReport.displayText);
  assert.equal(headNotice.provenance.source_session_id, headReport.generatedId);
  assert.deepEqual(await readRollout(head, "SELECT (SELECT COUNT(*) FROM turn_records) AS turns,(SELECT COUNT(*) FROM item_catalog WHERE semantic_kind='user_input') AS roots"), [{ turns: 0, roots: 0 }]);
  await openOwner(head);
  await assertTranscriptDom(head, zero, "generated zero Turn");
  await assertInternalHistory(head, zero, "generated zero Turn");
  const headHtml = await head.page.locator(".chat-internal-execution-display").evaluateAll((elements) => elements.map((element) => element.outerHTML).join(""));
  for (const hidden of [headReport.prompt, headReport.privateResult, headReport.generatedId, headReport.generatedThread, headReport.run.run_id, headReport.definition.generator_id]) assert.ok(!headHtml.includes(hidden));
  const firstHeadTurn = await completeOwnerTurn(head, "首部 notice 之后的第一条真实用户消息。", [
    finalResponse("generated-head-first-user", "第一条用户轮独立完成。"),
  ]);
  const headHistory = await loadCompleteThreadHistory(head);
  assert.deepEqual(headHistory.items.map((turn) => turn.turn_id), [firstHeadTurn.sent.turnId]);
  assert.deepEqual(headHistory.items[0].activity_stats, firstHeadTurn.turn.activity_stats);
  assert.ok(headNotice.logical_item_ordinal < headHistory.items[0].root_logical_item_ordinal);
  assert.deepEqual(headHistory.internal_executions, zero.internal_executions);
  const firstHeadRequest = await modelRequest(firstHeadTurn.calls[0]);
  const firstHeadInput = JSON.stringify(firstHeadRequest.request.messages);
  for (const privateText of [headReport.prompt, headReport.privateResult]) assert.ok(!firstHeadInput.includes(privateText), "新用户 execution 不得全局补入已消费的生成私有输入");
  await openOwner(head);
  await assertTranscriptDom(head, headHistory, "generated head live");
  await assertInternalHistory(head, headHistory, "generated head live");
  await openOwner(head);
  await assertTranscriptDom(head, headHistory, "generated head refresh");
  await assertInternalHistory(head, headHistory, "generated head refresh");
  return { target: target.sessionId, generated: generatedId, definition, run, terminal, providerWireEvidence, baseline, roots,
    admission, admissionMetadata, admissionPreimage, canonicalNotice, notices, pages, membership, internal, assemblies, crossOwner,
    snapshot: { appended, invalidDirection, stale, pageSizes: frozenPages.map((page) => ({ turns: page.items.length, records: page.internal_executions.reduce((sum, entry) => sum + entry.display_parts.length + entry.response_parts.length, 0) })),
      partIds: frozenParts.map((part) => part.part_id), displayIds: frozenDisplays.map((part) => part.item_id) },
    around, aroundPages, aroundDense, head: { sessionId: head.sessionId,
      report: { generatedId: headReport.generatedId, generatedThread: headReport.generatedThread,
        definition: headReport.definition, run: headReport.run, terminal: headReport.terminal,
        providerWireEvidence: headReport.providerWireEvidence },
      zero, first: firstHeadTurn, history: headHistory },
    fork: { sessionId: fork.owner.sessionId, history: fork.targetHistory } };
}

async function runWeeklyReport(surfaces) {
  const git = await createProductSession(surfaces.b, "Git 周报素材");
  const analysis = await createProductSession(surfaces.a, "分析周报素材");
  const implementation = await createProductSession(surfaces.c, "实施周报素材");
  const inputs = [];
  for (const [owner, text] of [[git, "Git：分支保持干净，README 已核对。"], [analysis, "分析：线程按逻辑顺序投影。"], [implementation, "实施：持久 owner 支持冷历史。"]]) {
    inputs.push(await completeOwnerTurn(owner, `本周工作：${text}`, [toolResponse(`${owner.name}-weekly-material-tool`, "read_file", { path: "README.md" }), finalResponse(`${owner.name}-weekly-material-final`, text)]));
  }
  const weekly = await createProductSession(surfaces.b, "汇总周报");
  const next = uuidText(await testControl({ action: "next_uuid", namespace: `model_call/${weekly.sessionId}/${weekly.threadId}` }));
  const operationId = softwareOperation("send", weekly, next, "weekly-send-remote");
  const communicationId = `comm_${createHash("sha256").update(`communication\0${operationId}`).digest("hex").slice(0, 32)}`;
  await planChatCalls(implementation.sessionId, implementation.threadId, [finalResponse("weekly-target-accepted", "实施：周报回执已核对。")]);
  const responses = [
    toolResponse("weekly-send-remote", "send_message_to_session", { target_session_id: implementation.sessionId, content: "请确认实施周报素材。", kind: "result" }),
    toolResponse("weekly-wait-remote", "wait_for_session", { target_session_id: implementation.sessionId, communication_id: communicationId, until: "terminal", timeout_seconds: 60 }),
    toolResponse("weekly-load-context-skill", "skill_load", { name: "gateway-context" }),
    toolResponse("weekly-read-local", "invoke_extension_tool", { tool_name: "read_context", arguments: { ...git.resource, view: "messages", recent_rounds: 50, limit: 20, max_chars: 16384 } }),
    toolResponse("weekly-read-hub", "invoke_extension_tool", { tool_name: "read_context", arguments: { ...analysis.resource, view: "messages", recent_rounds: 50, limit: 20, max_chars: 16384 } }),
    toolResponse("weekly-read-spoke", "invoke_extension_tool", { tool_name: "read_context", arguments: { ...implementation.resource, view: "messages", recent_rounds: 50, limit: 20, max_chars: 16384 } }),
    finalResponse("weekly-report", "本周周报\n\nGit：分支保持干净，README 已核对。\n分析：线程按逻辑顺序投影。\n实施：持久 owner 支持冷历史。"),
  ];
  const report = await completeOwnerTurn(weekly, `请把本地 ${git.sessionId}、远端裸 ID ${analysis.sessionId} 和链接 ${implementation.resource.uri} 汇总为周报；先发送并等待实施确认。`, responses);
  assert.equal(report.calls[0], next);
  const outbox = await readControl(weekly, "SELECT * FROM communication_outbox WHERE send_operation_id=?", [operationId]);
  const inbox = await readControl(implementation, "SELECT * FROM communication_inbox WHERE communication_id=?", [communicationId]);
  assert.equal(outbox.length, 1);
  assert.equal(inbox.length, 1);
  assert.equal(outbox[0].communication_id, communicationId);
  assert.equal(outbox[0].target_gateway_id, surfaces.c.gatewayId);
  assert.equal(outbox[0].target_thread_id, implementation.threadId);
  assert.equal(inbox[0].source_gateway_id, surfaces.b.gatewayId);
  assert.equal(inbox[0].state, "execution_bound");
  const history = await loadThreadHistory(weekly);
  const toolResults = history.items[0].response_parts.filter((part) => part.kind === "tool_result");
  assert.equal(toolResults.length, 6);
  for (const text of ["Git：", "分析：", "实施："]) assert.ok(toolResults.some((part) => String(part.result).includes(text)),
    `真实 read tool result 缺少 ${text}: ${JSON.stringify(toolResults.map((part) => ({
      tool_name: part.tool_name, result: String(part.result).slice(0, 2000),
    })))}`);
  const requests = await Promise.all(report.calls.map(modelRequest));
  const system = requests.map((entry) => entry.request.messages.filter((message) => message.role === "system"));
  for (const prefix of system) assert.deepEqual(prefix, system[0], "send/wait/read 不能漂移 stable prefix bytes");
  const targetHistory = await loadThreadHistory(implementation);
  assert.equal(targetHistory.items.length, 1, "ambient communication 不能伪造用户 Turn");
  assert.ok(targetHistory.internal_executions.length >= 1);
  const frozenRead = await runReadRecovery(surfaces, { source: weekly, target: implementation });
  return { git: git.sessionId, analysis: analysis.sessionId, implementation: implementation.sessionId, weekly: weekly.sessionId, inputs, report, outbox, inbox, targetHistory, frozenRead };
}

async function teamBoard(owner, teamId) {
  const reply = (await ownerApi(owner, `/api/v1/sessions/${owner.sessionId}/child-threads`)).data;
  assert.equal(reply.parent_session_id, owner.sessionId);
  const board = reply.teams.find((entry) => entry.team_id === teamId);
  assert.ok(board, "ChildThreadList 必须包含当前 Session 的团队");
  assert.equal(board.session_id, owner.sessionId);
  assert.equal(board.coordinator_thread_id, owner.threadId);
  return { board, children: reply.items };
}

async function assertTeamDom(owner, board) {
  await openOwner(owner);
  await owner.page.getByRole("tab", { name: "运行与连接", exact: true }).click();
  const element = owner.page.locator(`[data-team-id="${board.team_id}"]`);
  await waitUntil(async () => await element.getAttribute("data-team-version") === String(board.version), "右侧侧边栏更新团队版本");
  const text = await element.innerText();
  assert.ok(text.includes(board.name) && text.includes(board.coordinator_thread_id));
  assert.equal(await element.locator("[data-team-member-thread-id]").count(), board.members.length);
  for (const member of board.members) assert.ok((await element.locator(`[data-team-member-thread-id="${member.thread_id}"]`).innerText()).includes(member.role));
  assert.equal(await element.locator("[data-team-task-id]").count(), board.tasks.length);
  for (const task of board.tasks) {
    const row = element.locator(`[data-team-task-id="${task.task_id}"]`);
    assert.equal(await row.getAttribute("data-team-task-status"), task.status);
    const value = await row.innerText();
    assert.ok(value.includes(task.title) && value.includes(task.assignee_thread_id));
  }
}

async function runSkillUntrack(surface) {
  const owner = await createProductSession(surface, "人工停止 Skill 跟踪");
  const skillName = "gateway-context";
  const setup = await completeOwnerTurn(owner, "跟踪上下文 Skill，然后由用户从输入框停止跟踪。", [
    toolResponse("manual-untrack-load", "skill_load", { name: skillName, mode: "tracked" }),
    finalResponse("manual-untrack-ready", "Skill 已跟踪，等待用户控制。"),
  ]);
  const controlQuery = "SELECT * FROM context_source_control_states WHERE session_id=? AND thread_id=? AND source_kind='skill' AND name=?";
  const controlParameters = [owner.sessionId, owner.threadId, skillName];
  const tracked = await readRollout(owner, controlQuery, controlParameters);
  assert.equal(tracked.length, 1);
  assert.equal(tracked[0].tracking_status, "tracked");
  const historyQuery = "SELECT * FROM item_catalog ORDER BY item_sequence";
  const callsQuery = "SELECT * FROM model_calls ORDER BY model_call_id";
  const assembliesQuery = "SELECT * FROM context_assemblies ORDER BY assembly_id";
  const before = {
    items: await readRollout(owner, historyQuery), calls: await readRollout(owner, callsQuery),
    assemblies: await readRollout(owner, assembliesQuery),
  };
  const node = await sessionCatalogNode(owner);
  const jsonlPath = join(owner.workspace, (await threadRolloutLocator(owner, node)).replace(/index\.sqlite$/, "rollout.jsonl"));
  const bytes = await readFile(jsonlPath);
  const skillPath = join(owner.workspace, ".boxteam/skills", skillName, "SKILL.md");
  const skillBytes = await readFile(skillPath);
  const receipts = [];
  const skillLoadSummaries = {};
  try {
    await openOwner(owner);
    for (const [view, label, selector] of [
      ["agent", "上下文状态", ".agent-state-summary"],
      ["events", "事件视图", ".event-queue-key-trace"],
      ["requests", "请求视图", ".request-log-key-flow"],
    ]) {
      await owner.page.locator("#viewModeButton").click();
      await owner.page.getByRole("menuitemradio", { name: new RegExp(`^${label}`) }).click();
      const summary = owner.page.locator(selector);
      if (view === "requests") await summary.getByRole("button", { name: /请求链路/ }).click();
      await waitUntil(async () => (await summary.innerText()).includes(skillName)
        && (await summary.innerText()).includes("已接受的 Skill 加载"), `${label}读取真实Skill加载回执`);
      skillLoadSummaries[view] = await summary.innerText();
    }
    await owner.page.locator("#viewModeButton").click();
    await owner.page.getByRole("menuitemradio", { name: /^默认视图/ }).click();
    // 真实源已不存在时仍可人工控制；不恢复来源、不发送 LLM 消息。
    await unlink(skillPath);
    for (const explicitThread of [false, true]) {
      const responsePending = owner.page.waitForResponse((response) => response.request().method() === "POST"
        && new URL(response.url()).pathname === `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/skills/untrack`);
      const input = owner.page.getByTestId("composer-input");
      await input.fill(`/skill-untrack ${skillName}${explicitThread ? ` ${owner.threadId}` : ""}`);
      await input.press("Enter");
      const response = await responsePending;
      assert.equal(response.status(), 200);
      const payload = await response.json();
      assert.equal(payload.request_id, response.headers()["x-request-id"]);
      const receipt = payload.data;
      assert.equal(receipt.session_id, owner.sessionId);
      assert.equal(receipt.thread_id, owner.threadId);
      assert.equal(receipt.name, skillName);
      assert.equal(receipt.mode, "untrack");
      assert.equal(receipt.status, explicitThread ? "not_tracked" : "loaded");
      assert.equal(receipt.tracked, false);
      assert.equal(receipt.queued, false);
      assert.equal(receipt.error, null);
      await waitUntil(async () => (await owner.page.locator(".composer-attachment-notice").innerText())
        === (explicitThread ? `${skillName} 在该线程未被跟踪，已有上下文保留` : `已停止跟踪 ${skillName}，已有上下文保留`),
      "输入框显示完整后端停止跟踪结果");
      assert.equal(await input.inputValue(), "");
      receipts.push(payload);
    }
    const untracked = await readRollout(owner, controlQuery, controlParameters);
    assert.equal(untracked.length, 1);
    assert.equal(untracked[0].source_id, tracked[0].source_id);
    assert.equal(untracked[0].tracking_status, "untracked");
    assert.equal(untracked[0].state_revision, tracked[0].state_revision + 1);
    assert.deepEqual(await readRollout(owner, historyQuery), before.items);
    assert.deepEqual(await readRollout(owner, callsQuery), before.calls);
    assert.deepEqual(await readRollout(owner, assembliesQuery), before.assemblies);
    assert.deepEqual(await readFile(jsonlPath), bytes);
    return { sessionId: owner.sessionId, threadId: owner.threadId, setup, tracked, untracked, receipts, skillLoadSummaries,
      preservedJsonlSha256: createHash("sha256").update(bytes).digest("hex"), modelCallCount: before.calls.length };
  } finally {
    await writeFile(skillPath, skillBytes);
  }
}

async function runTeamActivation(surface, boundary, foreign) {
  const owner = await createProductSession(surface, `团队激活边界 ${boundary}`);
  const teamId = `team_${await testControl({ action: "next_uuid", namespace: "team" })}`;
  const setup = [await completeOwnerTurn(owner, "建立用于实际 child 协作的团队。", [
    toolResponse(`${boundary}-team-create`, "create_team", { name: `团队 ${boundary}` }), finalResponse(`${boundary}-team-created`, "团队面板已创建。"),
  ])];
  const createMember = async (coordinator, role) => {
    const threadId = `thr_${await testControl({ action: "next_uuid", namespace: "thr" })}`;
    const before = (await loadThreadHistory(owner)).internal_executions.length;
    await planChatCalls(owner.sessionId, threadId, [finalResponse(`${boundary}-${role}-initial`, `${role} 已确认角色，等待正式工作。`)]);
    const creation = await completeOwnerTurn(coordinator, `创建 ${role} 团队成员。`, [
      toolResponse(`${boundary}-${role}-create`, "create_team_member", { team_id: teamId, role, startup_prompt: "只确认团队角色与协议，不修改文件。", work_mode: "read_only" }),
      finalResponse(`${boundary}-${role}-created`, `${role} 已加入团队。`), finalResponse(`${boundary}-${role}-report`, `${role} 初始报告已确认。`),
    ]);
    await waitUntil(async () => {
      const { children } = await teamBoard(owner, teamId);
      const child = children.find((entry) => entry.thread_id === threadId);
      const history = await loadThreadHistory(owner);
      return ["completed", "completed_empty"].includes(child?.report_status) && history.internal_executions.length > before
        && ["completed", "completed_empty"].includes(history.internal_executions.at(-1).status);
    }, "成员及 coordinator 的真实报告结束", 120_000);
    const member = { ...owner, threadId, child: true, role };
    const { board } = await teamBoard(owner, teamId);
    assert.equal(board.members.find((entry) => entry.thread_id === threadId)?.role, role);
    return { member, creation };
  };
  const members = [];
  for (const role of ["reviewer", "tester"]) {
    const { member, creation } = await createMember(owner, role);
    setup.push(creation);
    members.push(member);
  }
  const [reviewer, tester] = members;
  const initial = await teamBoard(owner, teamId);
  assert.equal(initial.board.members.length, 3);
  await assertTeamDom(owner, initial.board);
  const residencyEvidence = { boundary, sessionId: owner.sessionId, threadId: tester.threadId };
  (result.teamResidencySnapshots ??= []).push(residencyEvidence);
  residencyEvidence.idle = await waitForIdleChildRuntime(tester);
  await testControl({ action: "advance_residency", seconds: 1800 });
  await waitUntil(async () => {
    residencyEvidence.afterAdvance = (await teamBoard(owner, teamId)).children.find((entry) => entry.thread_id === tester.threadId).runtime;
    return residencyEvidence.afterAdvance.residency === "cold";
  }, "无任务 tester 释放 runtime", residencyScanObservationTimeoutMs);
  const coldHistory = await loadThreadHistory(tester);
  const operatorPage = await browserStartup("team_operator_page", () => owner.page.context().newPage());
  installPageDiagnostics(operatorPage);
  try {
    await ensureGuest(operatorPage, new URL(owner.page.url()).origin);
    const coordinator = { ...owner, page: operatorPage };
    const calls = await planChatCalls(owner.sessionId, reviewer.threadId, [
      toolResponse(`${boundary}-first-read`, "read_file", { path: "README.md" }), finalResponse(`${boundary}-first-final`, "第一条工作完成。"),
      toolResponse(`${boundary}-queued-read`, "read_file", { path: "README.md" }), finalResponse(`${boundary}-queued-final`, "排队工作完成。"),
    ]);
    const firstTool = `${calls[0]}:tool-call:${boundary}-first-read`;
    const queuedTool = `${calls[2]}:tool-call:${boundary}-queued-read`;
    for (const operation_id of [firstTool, queuedTool]) await testControl({ action: "arm", operation_id, phase: "canonical.tool_result.before_commit" });
    await openOwner(reviewer);
    const first = await sendOwnerPrompt(reviewer, "第一条：读取 README，等待后续核对。");
    await testControl({ action: "acknowledged", operation_id: firstTool, phase: "canonical.tool_result.before_commit" });
    const queued = await sendOwnerPrompt(reviewer, "第二条：在 active slot 冻结最新团队事实。");
    assert.equal((await loadThreadHistory(reviewer)).items.at(-1).status, "accepted");
    const queuedTitle = `排队期间登记-${boundary}`;
    const activeTitle = `active-slot后登记-${boundary}`;
    const queuedRole = `queued-observer-${boundary}`;
    const activeRole = `active-observer-${boundary}`;
    const assign = (label, title) => completeOwnerTurn(coordinator, title, [
      toolResponse(label, "assign_team_task", { team_id: teamId, assignee_thread_id: reviewer.threadId, title, description: "核对 README 并保留团队冻结证据。", phase: "review", start_assignee: false }),
      finalResponse(`${label}-done`, `${title} 已登记，不启动额外 Job。`),
    ]);
    const delivered = () => waitUntil(async () => (await readControl(owner, "SELECT COUNT(*) AS pending FROM collaboration_fanouts WHERE state='pending'"))[0].pending === 0, "团队 revision 全部持久投递");
    const queuedMember = await createMember(coordinator, queuedRole);
    const queuedMutation = await assign(`${boundary}-queued-task`, queuedTitle);
    await delivered();
    const afterQueued = await teamBoard(coordinator, teamId);
    assert.equal(afterQueued.board.members.length, 4);
    assert.equal(afterQueued.board.members.find((entry) => entry.thread_id === queuedMember.member.threadId)?.role, queuedRole);
    await assertTeamDom(coordinator, afterQueued.board);
    await testControl({ action: "release", operation_id: firstTool, phase: "canonical.tool_result.before_commit" });
    await waitForJobStatus(reviewer, first.jobId);
    await testControl({ action: "acknowledged", operation_id: queuedTool, phase: "canonical.tool_result.before_commit" });
    const activeMember = await createMember(coordinator, activeRole);
    const activeMutation = await assign(`${boundary}-active-task`, activeTitle);
    await delivered();
    const afterActive = await teamBoard(coordinator, teamId);
    assert.equal(afterActive.board.members.length, 5);
    assert.equal(afterActive.board.members.find((entry) => entry.thread_id === activeMember.member.threadId)?.role, activeRole);
    await assertTeamDom(coordinator, afterActive.board);
    assert.equal((await teamBoard(coordinator, teamId)).children.find((entry) => entry.thread_id === tester.threadId).runtime.residency, "cold", "fanout 不能唤醒 cold child");
    assert.deepEqual(await loadThreadHistory(tester), coldHistory, "fanout 不能创建 cold child execution 或用户 Turn");
    await testControl({ action: "release", operation_id: queuedTool, phase: "canonical.tool_result.before_commit" });
    await waitForJobStatus(reviewer, queued.jobId);
    const displayed = await verifyOwnerTurns(reviewer);
    const requests = await Promise.all(calls.map(modelRequest));
    const messages = requests.map((request) => JSON.stringify(request.request.messages));
    assert.ok(!messages[0].includes(queuedTitle));
    assert.equal(messages[1].includes(queuedTitle), boundary === "model_call");
    assert.ok(messages[2].includes(queuedTitle) && !messages[2].includes(activeTitle));
    assert.equal(messages[3].includes(activeTitle), boundary === "model_call");
    for (const marker of [queuedRole, queuedMember.member.threadId]) {
      assert.ok(!messages[0].includes(marker), "首次请求不能看到后续新增成员");
      assert.equal(messages[1].includes(marker), boundary === "model_call", "排队期间新增成员遵守当前 Turn 的激活边界");
      assert.ok(messages[2].includes(marker), "排队执行取得 active slot 时必须冻结已发布成员和角色");
      assert.ok(messages[3].includes(marker));
    }
    for (const marker of [activeRole, activeMember.member.threadId]) {
      assert.ok(!messages[0].includes(marker) && !messages[1].includes(marker) && !messages[2].includes(marker));
      assert.equal(messages[3].includes(marker), boundary === "model_call", "active slot 后新增成员仅在显式 ModelCall 边界激活");
    }
    const snapshots = await readRollout(reviewer, "SELECT s.execution_id,s.turn_id,s.snapshot_kind,s.model_call_id,b.resource_id,b.revision,b.effective_boundary,b.activation_ordinal FROM resource_activation_snapshots s JOIN resource_activation_bindings b USING(session_id,thread_id,activation_snapshot_id) WHERE b.resource_kind='team_state' ORDER BY s.created_at,b.activation_ordinal");
    const current = snapshots.filter((row) => [first.turnId, queued.turnId].includes(row.turn_id));
    assert.ok(current.length >= 4 && current.every((row) => row.effective_boundary === boundary));
    const unique = current.map((row) => [row.execution_id, row.model_call_id, row.resource_id].join("/"));
    assert.equal(new Set(unique).size, unique.length, "冻结 snapshot 的 facet 不得重影");
    const executions = new Set(current.map((row) => row.execution_id));
    assert.equal((await testControl({ action: "events" })).filter((event) => event.phase === "team.ledger.read" && executions.has(event.operation_id)).length, 0, "child 请求只能消费已发布投影，禁止读团队 ledger");
    const ledgerBefore = await readControl(owner, "SELECT revision FROM collaboration_ledger WHERE id=1");
    const foreignQuery = "SELECT (SELECT COUNT(*) FROM communication_inbox) AS inbox,(SELECT revision FROM collaboration_ledger WHERE id=1) AS revision";
    const foreignBefore = await readControl(foreign, foreignQuery);
    // 新会话尚未执行；拒绝操作不得创建 rollout 或新增业务事实。
    assert.equal(await readRolloutDirectory(foreign), null, "foreign 新会话不得提前物化 rollout");
    const outboxBefore = await readControl(owner, "SELECT COUNT(*) AS count FROM communication_outbox");
    const assertNegativeIsolation = async () => {
      assert.deepEqual(await readControl(owner, "SELECT revision FROM collaboration_ledger WHERE id=1"), ledgerBefore);
      assert.deepEqual(await readControl(foreign, foreignQuery), foreignBefore);
      assert.equal(await readRolloutDirectory(foreign), null,
        "拒绝跨 Session 操作不得物化 foreign rollout 的 SQLite、WAL 或 JSONL");
      assert.deepEqual(await readControl(owner, "SELECT COUNT(*) AS count FROM communication_outbox"), outboxBefore);
    };
    // 非成员属于确定业务失败，必须收敛 failed Job/Turn，而不是继续向模型返回 schema 错误。
    const foreignMember = await completeOwnerTurn(coordinator, "拒绝跨 Session 团队成员。", [
      toolResponse(`${boundary}-foreign-member`, "assign_team_task", { team_id: teamId, assignee_thread_id: foreign.threadId, title: "非法跨 Session member", description: "不允许附着。", phase: "review", start_assignee: false }),
    ], [], { expectedJobStatus: "failed", expectedExecutionOutcome: "failed", expectedTurnStatus: "failed" });
    const membershipError = `Thread 不是团队成员: ${foreign.threadId}`;
    assert.equal(foreignMember.job.error_message, membershipError);
    const memberResults = foreignMember.turn.response_parts.filter((part) => part.kind === "tool_result");
    assert.equal(memberResults.length, 1, "业务失败只产生原工具调用的一个完整失败事实");
    assert.equal(memberResults[0].tool_name, "assign_team_task");
    assert.equal(memberResults[0].status, "completed", "完整错误结果的 Item 终态不冒充工具执行成功");
    assert.equal(memberResults[0].result, membershipError);
    assert.equal(foreignMember.turn.response_parts.filter((part) => part.kind === "final_text").length, 0);
    await assertNegativeIsolation();
    // extra=forbid 的三项参数错误返回错误 ToolMessage；模型仍可继续完成同一 Turn。
    const forbiddenFields = ["team_state", "team_task", "goal_state"];
    const statePayload = await completeOwnerTurn(coordinator, "拒绝携带业务状态的跨 Session send。", [
      ...forbiddenFields.map((field) => toolResponse(`${boundary}-foreign-${field}`, "send_message_to_session", { target_session_id: foreign.resource.uri, content: "禁止携带业务状态。", [field]: { status: "completed" } })),
      finalResponse(`${boundary}-negative-final`, "业务状态通信均已拒绝。"),
    ]);
    const errors = statePayload.turn.response_parts.filter((part) => part.kind === "tool_result");
    assert.equal(errors.length, forbiddenFields.length);
    for (const [index, part] of errors.entries()) {
      assert.equal(part.tool_name, "send_message_to_session");
      assert.ok(part.result.includes("Error invoking tool") && part.result.includes(forbiddenFields[index]), "每项非法参数必须明确返回原字段的 schema 错误");
    }
    await assertNegativeIsolation();
    const negative = { foreignMember, statePayload };
    return { sessionId: owner.sessionId, teamId, boundary, setup, initial, first, queued, queuedMember, activeMember, queuedMutation, activeMutation, afterQueued, afterActive, current, displayed, negative };
  } finally {
    await drainPageResponseBodies(operatorPage);
    await operatorPage.close();
  }
}

async function updateMcpCatalog(surface, server, tools, error = null) {
  const path = surface.mcpCatalogs[server];
  assert.equal(typeof path, "string", `正式 fixture 缺少 MCP 外部目录 ${surface.name}/${server}`);
  const previous = JSON.parse(await readFile(path, "utf8"));
  const value = { revision: previous.revision + 1, tools, error };
  await writeFile(`${path}.next`, JSON.stringify(value) + "\n");
  await rename(`${path}.next`, path);
  return value.revision;
}

function mcpEcho(description, schemaVersion = 1) {
  return { name: "echo", description, inputSchema: { type: "object",
    properties: { text: { type: "string" }, ...(schemaVersion === 2 ? { version: { type: "integer", const: 2 } } : {}) },
    required: schemaVersion === 2 ? ["text", "version"] : ["text"], additionalProperties: false } };
}

async function mcpAssemblies(owner, calls) {
  const node = await sessionCatalogNode(owner);
  const rows = await testControl({ action: "read_assemblies", name: owner.name,
    relative_path: await threadRolloutLocator(owner, node), session_id: owner.sessionId,
    thread_id: owner.threadId, model_call_ids: calls });
  assert.equal(rows.length, calls.length, "每次实际 Provider call 必须有唯一 sealed assembly");
  assert.deepEqual(rows.map((row) => row.call), calls);
  return rows;
}

async function mcpBinding(owner, call) {
  const node = await sessionCatalogNode(owner);
  const rolloutIndex = await threadRolloutLocator(owner, node);
  const rows = await testControl({ action: "read_sql", name: owner.name, relative_path: rolloutIndex,
    query: "SELECT b.revision,b.captured_registry_generation,b.effective_boundary,b.activation_ordinal,b.source_lineage_digest,s.lineage_detail_ref,s.lineage_manifest_digest,d.relative_path,d.content_hash AS detail_content_hash FROM context_assemblies a JOIN resource_activation_assembly_bindings r USING(session_id,thread_id,assembly_id) JOIN resource_activation_snapshots s USING(session_id,thread_id,activation_snapshot_id) JOIN resource_activation_bindings b USING(session_id,thread_id,activation_snapshot_id) JOIN context_plan_details d ON d.detail_ref=s.lineage_detail_ref AND d.session_id=s.session_id AND d.thread_id=s.thread_id WHERE a.model_call_id=? AND a.status IN ('sealed','terminal') AND b.resource_id='mcp-tool-catalog:v1'", parameters: [call] });
  assert.equal(rows.length, 1, "MCP 指引与 typed members 只能来自一个冻结父行");
  const row = rows[0];
  const ref = JSON.parse(row.lineage_detail_ref);
  assert.deepEqual([ref.session_id, ref.thread_id], [owner.sessionId, owner.threadId]);
  assert.equal(row.relative_path, `rollout/context-plan-details/${ref.assembly_id}/${ref.detail_id}`);
  const threadLocator = rolloutIndex.slice(0, -"/rollout/index.sqlite".length);
  const rawDetail = await readFile(join(owner.workspace, threadLocator, row.relative_path), "utf8");
  const detail = JSON.parse(rawDetail);
  assert.deepEqual(detail.detail_ref, ref);
  assert.equal(detail.detail_kind, "resource_activation_lineage");
  assert.equal(`sha256:jcs:v1:${createHash("sha256").update(rawDetail).digest("hex")}`, row.detail_content_hash);
  assert.equal(detail.source_revision, row.lineage_manifest_digest);
  assert.equal(detail.detail.schema, "resource-activation-lineage-manifest:v2");
  const parents = detail.detail.lineages.filter((lineage) => lineage.resource_id === "mcp-tool-catalog:v1");
  assert.equal(parents.length, 1);
  const parent = parents[0];
  assert.equal(parent.activation_ordinal, row.activation_ordinal);
  assert.equal(parent.source_lineage_digest, row.source_lineage_digest);
  assert.equal(parent.source_lineage_ref.digest, row.source_lineage_digest);
  const revisions = parent.source_lineage_ref.sources.filter(([identity]) => identity === "extension-catalog:v1");
  assert.equal(revisions.length, 1, "dispatch catalog revision 必须来自原冻结 typed lineage");
  assert.equal(typeof revisions[0][1], "string");
  assert.ok(revisions[0][1]);
  return { ...row, binding_id: `ext-catalog:v1:${row.captured_registry_generation}:${revisions[0][1]}` };
}

async function runMcpActivation(surface, boundary) {
  const owner = await createProductSession(surface, `真实 stdio MCP 激活 ${boundary}`);
  const turns = [];
  const finish = async (label, responses) => {
    const turn = await completeOwnerTurn(owner, `MCP ${boundary}：${label}`, responses);
    turns.push({ calls: turn.calls, sent: turn.sent });
    return turn;
  };
  const refresh = async () => (await ownerApi(surface, "/api/v1/mcp/servers")).data;
  const notify = async (tools) => {
    await updateMcpCatalog(surface, "notify", tools);
    await waitUntil(async () => {
      const server = (await refresh()).find((entry) => entry.server_id === "notify");
      return JSON.stringify(server.tools.map(({ remote_name, description }) => ({ remote_name, description })))
        === JSON.stringify(tools.map(({ name, description }) => ({ remote_name: name, description })));
    }, "真实 MCP 通知完成完整目录发布");
  };
  const permission = async (enabled) => ownerApi(surface, "/api/v1/tools/selection", {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: "default", changes: [{ tool_id: "mcp__notify__echo", execution_enabled: enabled, model_visible: enabled }] }),
  });
  const invoke = (label, text, version) => toolResponse(label, "invoke_extension_tool", {
    tool_name: "mcp__notify__echo", arguments: { text, ...(version === undefined ? {} : { version }) },
  });
  try {
    assert.ok((await refresh()).every((server) => server.tools.length === 0));
    const listCount = () => testControl({ action: "events" }).then((events) => events.filter((event) => event.phase === "mcp.list.after_capture" && event.operation_id.includes(`/mcp/${surface.name}/`)).length);
    const listsBefore = await listCount();
    const empty = await finish("空目录仍保留固定信封", [finalResponse(`${boundary}-mcp-empty`, "空目录工具面已确认。")]);
    assert.equal(await listCount(), listsBefore, "model-call preparation 不得访问 MCP tools/list");
    const emptyAssembly = (await mcpAssemblies(owner, empty.calls))[0];
    const beforeText = `目录原版-${boundary}`;
    const afterText = `目录新版-${boundary}`;
    await notify([mcpEcho(beforeText)]);
    await updateMcpCatalog(surface, "silent_a", [mcpEcho(`显式刷新-${boundary}`)]);
    const silent = (await refresh()).find((server) => server.server_id === "silent_a");
    assert.deepEqual(silent.tools.map((tool) => tool.tool_id), ["mcp__silent_a__echo"]);

    const inflightText = `${boundary}-mcp-inflight`;
    const inflight = { operation_id: `mcp:${inflightText}`, phase: "mcp.call.before_result" };
    const calls = await planChatCalls(owner.sessionId, owner.threadId, [
      invoke(`${boundary}-mcp-inflight-call`, inflightText), finalResponse(`${boundary}-mcp-inflight-final`, "旧调用按原目录结果完成。"),
    ]);
    await testControl({ action: "arm", ...inflight });
    await openOwner(owner);
    const sent = await sendOwnerPrompt(owner, "调用真实 MCP echo，然后核对后续 model call 的激活边界。");
    await testControl({ action: "acknowledged", ...inflight });
    const originalAssemblies = await mcpAssemblies(owner, [calls[0]]);
    await notify([mcpEcho(afterText, 2)]);
    await testControl({ action: "release", ...inflight });
    await waitForJobStatus(owner, sent.jobId);
    await waitForPlannedExecutions(owner, calls);
    turns.push({ calls, sent });
    await verifyOwnerTurns(owner);
    const requests = await Promise.all(calls.map(modelRequest));
    assert.ok(JSON.stringify(requests[0].request.messages).includes(beforeText));
    assert.equal(JSON.stringify(requests[1].request.messages).includes(afterText), boundary === "model_call");
    assert.ok(JSON.stringify(requests[1].request.messages).includes(`mini-mcp:echo:${inflightText}:revision=1`), "旧 in-flight 结果不能随新目录重写");
    assert.deepEqual((await mcpAssemblies(owner, [calls[0]])).map((row) => row.bytes), originalAssemblies.map((row) => row.bytes));
    const firstBinding = await mcpBinding(owner, calls[0]);
    const secondBinding = await mcpBinding(owner, calls[1]);
    assert.equal(firstBinding.effective_boundary, boundary);
    assert.equal(secondBinding.effective_boundary, boundary);
    assert.equal(secondBinding.revision === firstBinding.revision, boundary === "turn");

    const upgraded = await finish("新增 schema 参数通过真实固定信封调用", [
      invoke(`${boundary}-mcp-upgraded-call`, `${boundary}-mcp-upgraded`, 2), finalResponse(`${boundary}-mcp-upgraded-final`, "新版 schema 的执行 handle 已确认。"),
    ]);
    assert.ok(JSON.stringify((await modelRequest(upgraded.calls[1])).request.messages).includes(`mini-mcp:echo:${boundary}-mcp-upgraded:revision=2`));
    assert.equal((await graphLifecycle(owner)).built.length, 1, "目录增删改不得重建 resident Thread runtime");

    const revokeCalls = await planChatCalls(owner.sessionId, owner.threadId, [
      toolResponse(`${boundary}-mcp-revoke-wait`, "read_file", { path: "README.md" }),
      invoke(`${boundary}-mcp-revoked-call`, `${boundary}-mcp-denied`, 2), finalResponse(`${boundary}-mcp-revoked-final`, "当前权限已拒绝调用并保留成对结果。"),
    ]);
    const revokeBarrier = { operation_id: `${revokeCalls[0]}:tool-call:${boundary}-mcp-revoke-wait`, phase: "canonical.tool_result.before_commit" };
    await testControl({ action: "arm", ...revokeBarrier });
    await openOwner(owner);
    const revokedSend = await sendOwnerPrompt(owner, "先读取 README，再在同一 Turn 调用 MCP echo。");
    await testControl({ action: "acknowledged", ...revokeBarrier });
    await permission(false);
    await testControl({ action: "release", ...revokeBarrier });
    await waitForJobStatus(owner, revokedSend.jobId);
    await waitForPlannedExecutions(owner, revokeCalls);
    turns.push({ calls: revokeCalls, sent: revokedSend });
    assert.ok(JSON.stringify((await modelRequest(revokeCalls[2])).request.messages).includes("已被策略禁用"));
    assert.ok(!(await testControl({ action: "events" })).some((event) => event.operation_id === `mcp:${boundary}-mcp-denied`), "撤权后不能进入真实远端调用");
    await permission(true);
    await verifyOwnerTurns(owner);

    const valid = await mcpBinding(owner, revokeCalls.at(-1));
    await updateMcpCatalog(surface, "silent_a", [mcpEcho(`整批新增-${boundary}`)]);
    await updateMcpCatalog(surface, "silent_b", [], `整批拒绝-${boundary}`);
    const rejected = await ownerApiResponse(surface, "/api/v1/mcp/servers");
    assert.equal(rejected.status, 503);
    assert.ok(JSON.stringify(rejected.body).includes(`整批拒绝-${boundary}`));
    await updateMcpCatalog(surface, "silent_b", [mcpEcho(`整批完成-${boundary}`)]);
    await refresh();
    const recovered = await finish("失败 candidate 只在全部有效后一次发布", [finalResponse(`${boundary}-mcp-recovered-final`, "完整目录已恢复。")]);
    const recoveredBinding = await mcpBinding(owner, recovered.calls[0]);
    assert.equal(recoveredBinding.captured_registry_generation, valid.captured_registry_generation + 1, "失败刷新不能先发布部分目录或推进有效 generation");
    const recoveredRequest = JSON.stringify((await modelRequest(recovered.calls[0])).request.messages);
    assert.ok(recoveredRequest.includes(`整批新增-${boundary}`) && recoveredRequest.includes(`整批完成-${boundary}`));

    const reopen = await replaceNodePage(surface, [owner]);
    await testControl({ action: "process", name: surface.name, operation: "restart" });
    await reopen();
    const reconnected = await finish("backend 重启后重新连接真实 stdio", [
      invoke(`${boundary}-mcp-reconnected-call`, `${boundary}-mcp-reconnected`, 2), finalResponse(`${boundary}-mcp-reconnected-final`, "重连后调用完成。"),
    ]);
    assert.ok(JSON.stringify((await modelRequest(reconnected.calls[1])).request.messages).includes(`mini-mcp:echo:${boundary}-mcp-reconnected:revision=2`));
    assert.equal((await graphLifecycle(owner)).built.length, 2, "重启只重建一次该 Thread 的真实 graph");

    await notify([]);
    await updateMcpCatalog(surface, "silent_a", []);
    await updateMcpCatalog(surface, "silent_b", []);
    const deletedCatalog = await refresh();
    for (const serverId of ["notify", "silent_a", "silent_b"]) {
      const server = deletedCatalog.find((entry) => entry.server_id === serverId);
      assert.ok(server, `删除后缺少真实 MCP server ${serverId}`);
      assert.deepEqual(server.tools, [], `删除后 ${serverId} 必须发布空目录`);
    }
    const removedText = `${boundary}-mcp-removed`;
    const removed = await finish("目标删除后固定信封按新封存目录拒绝旧 target", [
      invoke(`${boundary}-mcp-removed-call`, removedText, 2),
      finalResponse(`${boundary}-mcp-removed-final`, "删除后的空目录与成对拒绝结果已确认。"),
    ]);
    const removedTurn = (await loadThreadHistory(owner, { turn_ids: [removed.sent.turnId] })).items[0];
    const removedResults = removedTurn.response_parts.filter((part) => part.kind === "tool_result");
    assert.equal(removedResults.length, 1, "删除后的真实工具调用仍须只有一个成对结果");
    const removedResult = assertBoundToolResult(owner, removed.sent, removedTurn, removed.calls[0],
      `${boundary}-mcp-removed-call`, "invoke_extension_tool");
    const removedBinding = await mcpBinding(owner, removed.calls[0]);
    assert.equal(removedResult.result,
      `sealed extension catalog binding 中不存在 target: tool_name='mcp__notify__echo' binding_id=${removedBinding.binding_id}`);
    await assertToolResultContinuation(owner, removed.calls, `${boundary}-mcp-removed-call`, removedResult);
    assert.ok(!(await testControl({ action: "events" })).some((event) => event.operation_id === `mcp:${removedText}`), "删除后不能进入旧远端 handle");
    for (const call of removed.calls) assert.equal((await mcpBinding(owner, call)).effective_boundary, boundary);
    const allCalls = turns.flatMap((turn) => turn.calls);
    const assemblies = await mcpAssemblies(owner, allCalls);
    const emptyRequest = await modelRequest(empty.calls[0]);
    const toolSet = emptyAssembly.snapshot.tool_set_refs.map(({ source_revision, content_hash, tools }) => ({ source_revision, content_hash, tools }));
    const epoch = emptyAssembly.snapshot.seal_binding.prefix.state.epoch;
    for (const row of assemblies) {
      assert.deepEqual((await modelRequest(row.call)).request.tools, emptyRequest.request.tools, "扩展目录变化不得改变 Provider 工具面");
      assert.deepEqual(row.snapshot.tool_set_refs.map(({ source_revision, content_hash, tools }) => ({ source_revision, content_hash, tools })), toolSet);
      assert.deepEqual(row.snapshot.seal_binding.prefix.state.epoch, epoch, "目录变化、撤权与重连不得制造 toolset_changed epoch");
      assert.ok(row.snapshot.seal_binding.prefix.state.frames.filter((frame) => frame.item_ref.source_identity === "mcp-tool-guidance:v1").every((frame) => frame.role === "user"));
      assert.ok(!(row.snapshot.seal_binding.root?.sources ?? []).some((source) => source.source_identity === "mcp-tool-guidance:v1"), "默认 tail_only MCP 指引不能混入 system root");
    }
    assert.equal((await graphLifecycle(owner)).built.length, 2);
    assert.deepEqual((await mcpAssemblies(owner, empty.calls)).map((row) => row.bytes), [emptyAssembly.bytes], "后续变化不能改写旧 sealed bytes");
    return { sessionId: owner.sessionId, boundary, turns, firstBinding, secondBinding, recoveredBinding, removedCalls: removed.calls, evidence: await verifyOwnerTurns(owner) };
  } finally {
    for (const server of Object.keys(surface.mcpCatalogs)) await updateMcpCatalog(surface, server, []);
    await refresh();
  }
}

async function runTurnNavigation(surface) {
  const owner = await createProductSession(surface, "真实轮次导航与历史定位");
  await openOwner(owner);
  const createdTurns = [];
  const createCompletedTurn = async (ordinal) => {
    const prompt = `创建第 ${ordinal} 条轮次导航验证记录。`;
    const label = `turn-navigation-${ordinal}`;
    const finalText = Array.from({ length: 8 }, (_, index) =>
      `第 ${ordinal} 轮的历史正文第 ${index + 1} 行，用于验证虚拟列表位置保真。`,
    ).join("\n");
    const calls = await planChatCalls(owner.sessionId, owner.threadId, [finalResponse(label, finalText)]);
    const sent = await acceptOwnerPrompt(owner, prompt);
    const job = await waitForJobStatus(owner, sent.jobId);
    const executions = await waitForPlannedExecutions(owner, calls);
    const turn = await assertAcceptedTurn(owner, sent);
    assert.equal(turn.status, "completed");
    assert.equal(turn.turn_id, sent.turnId);
    assert.ok(["completed", "succeeded"].includes(job.status));
    return { sent, turn, calls, executions };
  };

  for (let ordinal = 1; ordinal <= 6; ordinal += 1) {
    createdTurns.push(await createCompletedTurn(ordinal));
  }
  const initialHistory = await loadCompleteThreadHistory(owner);
  assert.equal(initialHistory.items.length, 6);
  assert.deepEqual(initialHistory.items.map((turn) => turn.turn_id), createdTurns.map((entry) => entry.turn.turn_id));
  const targetTurn = initialHistory.items[0];
  const sessionHistoryPath = `/api/v1/sessions/${owner.sessionId}/history`;
  const isInitialSessionTailRequest = (request) => request.method() === "POST"
    && new URL(request.url()).pathname === sessionHistoryPath
    && request.postDataJSON()?.direction === "tail";
  const historyPath = `/api/v1/sessions/${owner.sessionId}/threads/${owner.threadId}/history`;
  const isThreadHistoryRequest = (request) => request.method() === "POST"
    && new URL(request.url()).pathname === historyPath;

  const tailRequestPromise = owner.page.waitForRequest(isInitialSessionTailRequest, {
    timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS,
  });
  const tailResponsePromise = owner.page.waitForResponse((response) => {
    return isInitialSessionTailRequest(response.request());
  }, { timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS });
  await openOwner(owner);
  const [tailRequest, tailResponse] = await Promise.all([tailRequestPromise, tailResponsePromise]);
  assert.equal(tailRequest.frame(), owner.page.mainFrame(), "初始尾页请求必须来自当前用户页面");
  assert.equal(new URL(tailRequest.url()).origin, new URL(owner.page.url()).origin);
  assert.equal(tailRequest.headers()["x-boxteam-workspace-id"], owner.gatewayWorkspaceId);
  assert.deepEqual(tailRequest.postDataJSON(), { direction: "tail", turns: 5 });
  assert.equal(tailResponse.request(), tailRequest);
  assert.equal(tailResponse.url(), tailRequest.url());
  assert.equal(tailResponse.status(), 200, await tailResponse.text());
  const tailEnvelope = await tailResponse.json();
  const tailRequestId = tailResponse.headers()["x-request-id"];
  assert.equal(typeof tailRequestId, "string");
  assert.ok(tailRequestId);
  assert.equal(tailRequestId, tailEnvelope.request_id);
  const tailHistory = tailEnvelope.data;
  assert.equal(tailHistory.thread_id, owner.threadId);
  assert.deepEqual(tailHistory.items.map((turn) => turn.turn_id), initialHistory.items.slice(-5).map((turn) => turn.turn_id));
  assert.ok(!tailHistory.items.some((turn) => turn.turn_id === targetTurn.turn_id), "最旧目标 Turn 必须确实未加载到初始尾页");
  const initialShell = owner.page.locator(".chat-stream-shell .chat-stream-virtual-shell");
  assert.equal(await initialShell.getAttribute("data-owner-session-id"), owner.sessionId);
  assert.equal(await initialShell.getAttribute("data-owner-thread-id"), owner.threadId);
  assert.equal(await initialShell.getAttribute("data-turn-count"), "5");
  assert.equal(await owner.page.locator(`.chat-virtual-item[data-turn-id="${targetTurn.turn_id}"]`).count(), 0);

  const form = owner.page.getByRole("form", { name: "跳到指定轮次", exact: true });
  const input = form.getByLabel("轮次 ID", { exact: true });
  const submit = form.getByRole("button", { name: "跳到指定轮次", exact: true });
  const isAroundRequest = (request, anchorTurnId) => isThreadHistoryRequest(request)
    && request.postDataJSON()?.direction === "around"
    && request.postDataJSON()?.anchor_turn_id === anchorTurnId;

  await input.fill(targetTurn.turn_id);
  const aroundRequestPromise = owner.page.waitForRequest(
    (request) => isAroundRequest(request, targetTurn.turn_id),
    { timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS },
  );
  const aroundResponsePromise = owner.page.waitForResponse(
    (response) => isAroundRequest(response.request(), targetTurn.turn_id),
    { timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS },
  );
  await submit.click();
  const [aroundRequest, aroundResponse] = await Promise.all([aroundRequestPromise, aroundResponsePromise]);
  assert.equal(aroundRequest.frame(), owner.page.mainFrame(), "around 请求必须来自当前用户页面");
  assert.equal(new URL(aroundRequest.url()).origin, new URL(owner.page.url()).origin);
  assert.equal(aroundRequest.headers()["x-boxteam-workspace-id"], owner.gatewayWorkspaceId);
  assert.deepEqual(aroundRequest.postDataJSON(), {
    direction: "around",
    anchor_turn_id: targetTurn.turn_id,
    before_turns: 3,
    after_turns: 3,
  });
  assert.equal(aroundResponse.status(), 200, await aroundResponse.text());
  const aroundEnvelope = await aroundResponse.json();
  assert.equal(aroundResponse.headers()["x-request-id"], aroundEnvelope.request_id);
  assert.equal(aroundEnvelope.data.thread_id, owner.threadId);
  assert.ok(aroundEnvelope.data.items.some((turn) => turn.turn_id === targetTurn.turn_id));
  const firstLocation = await assertLocatedTurn(owner, targetTurn);
  assert.equal(firstLocation.rootLogicalItemOrdinal, String(targetTurn.root_logical_item_ordinal));

  const jumpToLatest = owner.page.getByRole("button", { name: "跳到最新消息", exact: true });
  await jumpToLatest.click();
  const latestInitialTurn = initialHistory.items.at(-1);
  await waitUntil(async () => {
    const latestState = await readTurnViewportState(owner.page, latestInitialTurn.turn_id);
    const targetState = await readTurnViewportState(owner.page, targetTurn.turn_id);
    return latestState?.visible
      && latestState.rootLogicalItemOrdinal === String(latestInitialTurn.root_logical_item_ordinal)
      && (!targetState || targetState.ariaCurrent !== "location")
      && !(await jumpToLatest.isVisible());
  }, "正式跳到最新消息并清除旧定位", TRANSCRIPT_NAVIGATION_TIMEOUT_MS);
  await waitUntil(async () => {
    const targetState = await readTurnViewportState(owner.page, targetTurn.turn_id);
    return !targetState?.visible;
  }, "最旧目标离开最新消息视口后再重复提交", TRANSCRIPT_NAVIGATION_TIMEOUT_MS);

  await input.fill(targetTurn.turn_id);
  await submit.click();
  const repeatedLocation = await assertLocatedTurn(owner, targetTurn);
  assert.equal(repeatedLocation.turnId, targetTurn.turn_id, "重复提交必须重新定位同一稳定 Turn ID");
  assert.equal(repeatedLocation.rootLogicalItemOrdinal, String(targetTurn.root_logical_item_ordinal));

  const newlyCreated = await createCompletedTurn(7);
  const newestHistory = await loadCompleteThreadHistory(owner);
  assert.equal(newestHistory.items.length, 7);
  assert.equal(newestHistory.items[0].turn_id, targetTurn.turn_id);
  assert.equal(newestHistory.items[0].root_logical_item_ordinal, targetTurn.root_logical_item_ordinal);
  const afterNewMessage = await assertLocatedTurn(owner, targetTurn);
  assert.equal(afterNewMessage.rootLogicalItemOrdinal, String(targetTurn.root_logical_item_ordinal), "新消息不得抢走已定位的历史 Turn");

  await jumpToLatest.click();
  const newestTurn = newestHistory.items.at(-1);
  await waitUntil(async () => {
    const state = await readTurnViewportState(owner.page, newestTurn.turn_id);
    return state?.visible
      && state.rootLogicalItemOrdinal === String(newestTurn.root_logical_item_ordinal)
      && !(await jumpToLatest.isVisible());
  }, "再次正式跳到新消息尾部", TRANSCRIPT_NAVIGATION_TIMEOUT_MS);
  const newestTranscript = expectedTranscript(newestHistory);
  const newestIndex = newestTranscript.findIndex((item) => item.kind === "turn" && item.turnId === newestTurn.turn_id);
  const offFollow = await leaveLatestFollowAtVisibleTurn(owner, newestIndex, {
    kind: "turn", turnId: newestTurn.turn_id, ordinal: newestTurn.root_logical_item_ordinal,
    turnOrdinal: newestTurn.ordinal,
  });
  assert.equal(offFollow.turnId, newestTurn.turn_id, "真实鼠标滚轮后必须保持同一 Turn 身份");
  assert.equal(offFollow.rootLogicalItemOrdinal, String(newestTurn.root_logical_item_ordinal), "真实鼠标滚轮后必须保持同一 root ordinal");
  assert.ok(offFollow.distanceToBottom > VIRTUOSO_AT_BOTTOM_THRESHOLD_PX);
  assert.equal(offFollow.identityMatches, true);

  const missingTurnId = "turn_00000000000070008000000000000000";
  canonicalUuid7(missingTurnId, "turn");
  const readMissingTurnViewport = () => owner.page.locator(".chat-stream-shell .chat-virtual-list").evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    const firstVisibleTurn = Array.from(element.querySelectorAll(".chat-virtual-item[data-turn-id]"))
      .find((turn) => {
        const turnBounds = turn.getBoundingClientRect();
        return turnBounds.bottom > bounds.top && turnBounds.top < bounds.bottom;
      });
    const shell = element.closest(".chat-stream-shell")?.querySelector(".chat-stream-virtual-shell");
    const navigationForm = document.querySelector('form[aria-label="跳到指定轮次"]');
    return {
      scrollTop: element.scrollTop,
      scrollHeight: element.scrollHeight,
      clientHeight: element.clientHeight,
      firstVisibleTurnId: firstVisibleTurn?.getAttribute("data-turn-id") ?? null,
      ownerSessionId: shell?.getAttribute("data-owner-session-id") ?? null,
      ownerThreadId: shell?.getAttribute("data-owner-thread-id") ?? null,
      formHeight: navigationForm?.getBoundingClientRect().height ?? null,
    };
  });
  const viewportBeforeInput = await readMissingTurnViewport();
  await input.fill(missingTurnId);
  const viewportBeforeSubmit = await readMissingTurnViewport();
  const missingRequestPromise = owner.page.waitForRequest(
    (request) => isAroundRequest(request, missingTurnId),
    { timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS },
  );
  const missingResponsePromise = owner.page.waitForResponse(
    (response) => isAroundRequest(response.request(), missingTurnId),
    { timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS },
  );
  await submit.click();
  const [missingRequest, missingResponse] = await Promise.all([missingRequestPromise, missingResponsePromise]);
  assert.equal(missingRequest.frame(), owner.page.mainFrame());
  assert.equal(missingRequest.method(), "POST");
  assert.equal(new URL(missingRequest.url()).origin, new URL(owner.page.url()).origin);
  assert.equal(missingResponse.request(), missingRequest);
  assert.equal(missingResponse.url(), missingRequest.url());
  assert.equal(missingRequest.headers()["x-boxteam-workspace-id"], owner.gatewayWorkspaceId);
  assert.deepEqual(missingRequest.postDataJSON(), {
    direction: "around",
    anchor_turn_id: missingTurnId,
    before_turns: 3,
    after_turns: 3,
  });
  const missingEnvelope = await missingResponse.json();
  assert.equal(missingResponse.status(), 404, "不存在的 Turn 必须保留精确 404 契约");
  const missingRequestId = missingResponse.headers()["x-request-id"];
  assert.equal(typeof missingRequestId, "string");
  assert.ok(missingRequestId.length > 0);
  assert.equal(missingEnvelope.request_id, missingRequestId, "预期 404 的响应体与响应头必须共享 request_id");
  assert.equal(missingEnvelope.detail, `rollout Turn 不存在: ${missingTurnId}`);
  const missingError = form.getByRole("alert");
  await missingError.waitFor({ state: "visible", timeout: TRANSCRIPT_NAVIGATION_TIMEOUT_MS });
  const missingErrorText = await missingError.textContent();
  assert.ok(missingErrorText?.includes(missingTurnId), "不存在的 Turn 必须报告其原始稳定 ID");
  assert.doesNotMatch(missingErrorText ?? "", /已定位第 \d+ 轮/);
  assert.equal(await form.getByRole("status").count(), 0, "错误状态不能保留成功文案");
  const viewportAfterError = await readMissingTurnViewport();
  const scrollBeforeMissingTurn = viewportBeforeInput.scrollTop;
  const scrollAfterMissingTurn = viewportAfterError.scrollTop;
  const missingTurnViewportObservation = {
    beforeInput: viewportBeforeInput,
    beforeSubmit: viewportBeforeSubmit,
    afterError: viewportAfterError,
  };
  assert.ok(Math.abs(scrollAfterMissingTurn - scrollBeforeMissingTurn) < 0.5,
    `不存在的 Turn 不能把视口伪装跳到会话尾部: ${JSON.stringify(missingTurnViewportObservation)}`);
  const newestAfterFailure = await readTurnViewportState(owner.page, newestTurn.turn_id);
  assert.equal(newestAfterFailure?.ownerSessionId, owner.sessionId);
  assert.equal(newestAfterFailure?.ownerThreadId, owner.threadId);
  assert.equal(newestAfterFailure?.ariaCurrent, null, "不存在的 Turn 不得保留或伪造定位成功标记");
  turnNavigationMissingTurnProofs.push({
    pageId: owner.page.diagnosticId,
    documentId: owner.page.diagnosticDocumentId,
    sessionId: owner.sessionId,
    threadId: owner.threadId,
    workspaceId: owner.gatewayWorkspaceId,
    anchorTurnId: missingTurnId,
    requestUrl: missingRequest.url(),
    responseUrl: missingResponse.url(),
    requestFrameIsMain: missingRequest.frame() === owner.page.mainFrame(),
    requestOrigin: new URL(missingRequest.url()).origin,
    pageOrigin: new URL(owner.page.url()).origin,
    requestMethod: missingRequest.method(),
    responseStatus: missingResponse.status(),
    requestBody: missingRequest.postDataJSON(),
    requestId: missingRequestId,
    bodyRequestId: missingEnvelope.request_id,
    detail: missingEnvelope.detail,
    alertText: missingErrorText,
    statusCount: 0,
    scrollBefore: scrollBeforeMissingTurn,
    scrollAfter: scrollAfterMissingTurn,
    scrollObservation: missingTurnViewportObservation,
    observedSessionId: newestAfterFailure.ownerSessionId,
    observedThreadId: newestAfterFailure.ownerThreadId,
    observedAriaCurrent: newestAfterFailure.ariaCurrent,
  });

  return {
    owner: { sessionId: owner.sessionId, threadId: owner.threadId, workspaceId: owner.gatewayWorkspaceId },
    initialTurnIds: initialHistory.items.map((turn) => turn.turn_id),
    initialTailTurnIds: tailHistory.items.map((turn) => turn.turn_id),
    targetTurnId: targetTurn.turn_id,
    targetRootLogicalItemOrdinal: targetTurn.root_logical_item_ordinal,
    aroundRequest: { direction: aroundRequest.postDataJSON().direction, anchorTurnId: aroundRequest.postDataJSON().anchor_turn_id,
      requestId: aroundEnvelope.request_id, returnedTurnIds: aroundEnvelope.data.items.map((turn) => turn.turn_id) },
    repeatedLocation,
    newlyCreatedTurnId: newlyCreated.turn.turn_id,
    offFollow,
    missingTurnId,
    missingStatus: missingResponse.status(),
    missingErrorText,
  };
}

async function runOwnerTurnsNavigationSmoke(surface) {
  const owner = await createProductSession(surface, "单 Turn 主窗口历史校验导航");
  const completed = await completeOwnerTurn(owner, "完成单 Turn 历史校验导航 smoke。", [
    finalResponse("owner-turns-navigation-smoke", "Gateway 页面与 owner history 已完成校验。"),
  ]);
  assert.equal(completed.evidence.turns.length, 1);
  assert.deepEqual(completed.evidence.turnNavigation, { live: 1, refresh: 1 });
  const proof = historyTurnNavigationProofs.at(-1);
  assert.ok(proof);
  assert.deepEqual({
    sessionId: proof.sessionId,
    threadId: proof.threadId,
    expectedTurnCount: proof.expectedTurnCount,
    liveAnchorJumps: proof.liveAnchorJumps,
    refreshAnchorJumps: proof.refreshAnchorJumps,
  }, {
    sessionId: owner.sessionId,
    threadId: owner.threadId,
    expectedTurnCount: 1,
    liveAnchorJumps: 1,
    refreshAnchorJumps: 1,
  });
  return { sessionId: owner.sessionId, threadId: owner.threadId, turnId: completed.sent.turnId, proof };
}

async function runSection8Product(browser, primaryPage) {
  const config = JSON.parse(requiredEnvironment("BOXTEAM_BROWSER_NODES"));
  const surfaces = {};
  const contexts = [];
  let primaryError;
  let hasPrimaryError = false;
  try {
    for (const [name, node] of Object.entries(config)) {
      const peerContext = name === "a" ? null : await browserStartup(`browser_context_${name}`, () => browser.newContext({ viewport: { width: 1920, height: 1100 } }));
      if (peerContext) contexts.push(peerContext);
      const page = peerContext ? await browserStartup(`browser_page_${name}`, () => peerContext.newPage()) : primaryPage;
      if (peerContext) installPageDiagnostics(page);
      await ensureGuest(page, node.base_url);
      const health = await api(page, "/api/gateway/health");
      assert.equal(health.data.gateway_id, node.gateway_id);
      const workspaces = await api(page, "/api/gateway/workspaces");
      const gatewayWorkspaceId = workspaces.data.active_workspace_id;
      assert.equal(typeof gatewayWorkspaceId, "string");
      const workspace = await api(page, "/api/v1/workspace");
      assert.equal(workspace.data.workspace_id, node.workspace_identity);
      surfaces[name] = { name, page, gatewayWorkspaceId, workspaceIdentity: node.workspace_identity, gatewayId: node.gateway_id,
        workspace: node.workspace, uriTemplate: node.uri_template, mcpCatalogs: node.mcp_catalogs };
    }
    const facts = {};
    result.section8 = facts;
    const scenarios = section8ScenarioRegistry(surfaces, facts);
    for (const name of section8Selection.selected) {
      assert.equal(typeof scenarios[name]?.run, "function", '未知 Section 8 场景：' + name);
      result.runningScenario = name;
      await browserPhase(`scenario_${name}`, "begin");
      facts[name] = await scenarios[name].run();
      await browserPhase(`scenario_${name}`, "end");
    }
    delete result.runningScenario;
    return facts;
  } catch (error) {
    primaryError = error;
    hasPrimaryError = true;
    throw error;
  } finally {
    await closeForkPeerContexts(contexts, primaryError, hasPrimaryError);
  }
}

const phasePath = standaloneSelfTest ? ""
  : `${requiredEnvironment("BOXTEAM_BROWSER_ARTIFACT_ROOT")}/basic-chat-tool-loop-phases.log`;
async function browserPhase(label, suffix) {
  const line = `${new Date().toISOString()} ${label}_${suffix} pid=${process.pid}\n`;
  await writeFile(phasePath, line, { flag: "a" });
  process.stdout.write(line);
}
async function browserStartup(label, create) {
  await browserPhase(label, "begin");
  let timer;
  try {
    const value = await Promise.race([
      create(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} 超过原 30000ms 浏览器操作期限`)), 30_000); }),
    ]);
    await browserPhase(label, "end");
    return value;
  } finally {
    clearTimeout(timer);
  }
}
let browser;
let context;
let page;
const pageErrors = [];
const consoleErrors = [];
const consoleErrorFacts = [];
const failedRequests = [];
const navigationResponses = [];
const historyConflictResponses = [];
const conflictResponses = [];
const historyRecoveryResponses = [];
const historyErrorResponses = [];
const lifecycleErrorResponses = [];
const jobOwnerResponses = [];
const lifecycleDeletionProofs = [];
const turnNavigationMissingTurnProofs = [];
const historyLiveProofs = [];
const historyTurnNavigationProofs = [];
const transcriptDomFailures = [];
const navigationReads = new Set();
const diagnosticPageTransitions = [];
function trackDiagnosticResponseBody(page, read, fact) {
  fact.bodyReadStartedAt = Date.now();
  navigationReads.add(read);
  page.diagnosticResponseBodies.add(read);
  void read.then(() => {
    fact.bodyReadCompletedAt = Date.now();
    navigationReads.delete(read);
    page.diagnosticResponseBodies.delete(read);
  });
}
async function drainPageResponseBodies(page) {
  // 正文属于原页面；主动刷新、导航或关闭前采完，不能用新请求补造旧响应。
  const transition = { pageId: page.diagnosticId, documentId: page.diagnosticDocumentId,
    startedAt: Date.now(), pendingBodies: page.diagnosticResponseBodies.size };
  diagnosticPageTransitions.push(transition);
  await waitUntil(() => page.diagnosticResponseBodies.size === 0, "原页面诊断响应正文采集");
  transition.completedAt = Date.now();
}
async function runDiagnosticResponseDrainSelfTest() {
  const page = { diagnosticId: "drain-self-test", diagnosticDocumentId: 1,
    diagnosticResponseBodies: new Set() };
  let finish;
  const fact = {};
  const read = new Promise((resolve) => { finish = resolve; });
  trackDiagnosticResponseBody(page, read, fact);
  let transitioned = false;
  const drain = drainPageResponseBodies(page).then(() => { transitioned = true; });
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(transitioned, false, "原正文未完成时不得开始页面转换");
  finish();
  await drain;
  assert.equal(transitioned, true);
  assert.equal(page.diagnosticResponseBodies.size, 0);
  assert.equal(navigationReads.has(read), false);
  assert.ok(fact.bodyReadCompletedAt >= fact.bodyReadStartedAt);
}
async function closeControlledRestartRequestPage(page, window) {
  // 保留发起请求的页面，直到被杀后端恢复，避免关闭页面截断错误正文或 SSE 恢复证据。
  const binding = controlledRestartPageBinding(window, page.diagnosticId);
  assert.equal(binding?.role, "copy-request");
  const interrupted = failedRequests.filter((failure) => failure.pageId === page.diagnosticId
    && failure.restartWindowId === window.operationId && failure.method === "POST"
    && failure.url === `${window.origin}/api/v1/workspace/observations/stream`
    && failure.error === "net::ERR_INCOMPLETE_CHUNKED_ENCODING");
  const originals = new Map(binding.observationRequests.map((request) => [request.requestFactId, request]));
  for (const failure of interrupted) {
    const request = controlledRestartRequests.find((request) => request.requestFactId === failure.requestFactId);
    assert.ok(request?.body?.subscriptions?.length, "中断的观察流必须保留原请求订阅");
    originals.set(request.requestFactId, request);
  }
  assert.ok(originals.size > 0, "重启前必须冻结原请求页的观察订阅，不能依赖中断事件是否已派发");
  for (const original of originals.values()) {
    assert.equal(original.pageId, page.diagnosticId);
    assert.ok(binding.documentIds.includes(original.documentId));
    assert.equal(original.workspaceHeader, window.workspaceId);
    assert.ok(original.body?.subscriptions?.length, "重启前冻结的订阅不能为空");
    await waitUntil(() => controlledRestartResponses.some((response) => response.pageId === original.pageId
      && response.documentId === original.documentId && response.url === original.url
      && response.restartWindowId === window.operationId && response.pageRole === "copy-request"
      && response.status === 200 && response.workspaceHeader === window.workspaceId
      && response.requestId && response.cdpRequestId
      && response.contentType?.split(";", 1)[0].trim().toLowerCase() === "text/event-stream"
      && response.requestedAt >= window.backendRestart.startedAt
      && hasConnectedObservationTargets(response, original.body.subscriptions)),
    "受控重启后，原请求页面必须重新连上全部原观察目标");
  }
  await drainPageResponseBodies(page);
  await page.close();
}

async function runControlledRestartPageCloseSelfTest() {
  const request = { requestFactId: "close-proof-original", pageId: -1, documentId: 1,
    url: "http://127.0.0.1:19001/api/v1/workspace/observations/stream", workspaceHeader: "workspace-close",
    body: { subscriptions: [{ subscription_id: "original", kind: "workspace_files" }] } };
  const window = { operationId: "close-proof-operation", workspaceId: request.workspaceHeader,
    origin: "http://127.0.0.1:19001", backendRestart: { startedAt: 100 },
    pageBindings: [{ pageId: -1, role: "copy-request", documentIds: [1], observationRequests: [request] }] };
  let closed = false;
  const page = { diagnosticId: -1, diagnosticResponseBodies: new Set(),
    close: async () => { closed = true; } };
  const responseCount = controlledRestartResponses.length;
  const failureCount = failedRequests.length;
  try {
    const closing = closeControlledRestartRequestPage(page, window);
    await Promise.resolve();
    assert.equal(closed, false, "中断事件尚未派发时，冻结的原订阅仍必须阻止提前关页");
    failedRequests.push({ ...request, method: "POST", restartWindowId: window.operationId,
      error: "net::ERR_INCOMPLETE_CHUNKED_ENCODING" });
    await Promise.resolve();
    assert.equal(closed, false, "稍晚派发的中断不能被关页绕过");
    controlledRestartResponses.push({ ...request, status: 200, requestedAt: 101,
      restartWindowId: window.operationId, pageRole: "copy-request", contentType: "text/event-stream",
      requestId: "recovered-request", cdpRequestId: "recovered-cdp-request",
      subscriptions: [{ subscription_id: "recovered", kind: "workspace_files" }],
      connectedSubscriptions: ["recovered"] });
    await closing;
    assert.equal(closed, true, "相同目标的真实 connected 回执到达后才能关页");
    window.pageBindings[0].observationRequests = [];
    failedRequests.splice(failureCount);
    await assert.rejects(closeControlledRestartRequestPage(page, window), /重启前必须冻结原请求页的观察订阅/);
  } finally {
    controlledRestartResponses.splice(responseCount);
    failedRequests.splice(failureCount);
  }
}
const messageStreamResponses = [];
const controlledRestartWindows = [];
const controlledRestartRequests = [];
const controlledRestartResponses = [];
const controlledRestartStreamDiagnostics = [];
const badGatewayConsoleAudit = [];
const BAD_GATEWAY_CONSOLE_TEXT = "Failed to load resource: the server responded with a status of 502 (Bad Gateway)";

function badGatewayConsoleAuditDifferences() {
  const counts = new Map();
  for (const fact of badGatewayConsoleAudit) {
    const key = JSON.stringify([fact.pageId, fact.documentId, fact.url, fact.text]);
    const count = counts.get(key) ?? { key, playwright: 0, cdp: 0 };
    count[fact.source] += 1;
    counts.set(key, count);
  }
  return [...counts.values()].filter((count) => count.playwright !== count.cdp);
}
const controlledRestartCdpSetups = new Set();
const OBSERVATION_STREAM_PATH = "/api/v1/workspace/observations/stream";
let diagnosticPageId = 0;
let controlledRestartRequestOrdinal = 0;

function classifyConsoleErrors(input = {}) {
  const {
    consoleErrorFacts: errorFacts = consoleErrorFacts,
    consoleErrors: errorTexts = consoleErrors,
    failedRequests: failures = failedRequests,
    messageStreamResponses: streamResponses = messageStreamResponses,
    jobOwnerResponses: ownerResponses = jobOwnerResponses,
    conflictResponses: conflictFacts = conflictResponses,
    historyErrorResponses: historyErrors = historyErrorResponses,
    lifecycleErrorResponses: lifecycleErrors = lifecycleErrorResponses,
    lifecycleDeletionProofs: deletionProofs = lifecycleDeletionProofs,
    turnNavigationMissingTurnProofs: missingTurnProofs = turnNavigationMissingTurnProofs,
    historyLiveProofs: liveProofs = historyLiveProofs,
    historyRecoveryResponses: recoveryFacts = historyRecoveryResponses,
    controlledRestartWindows: restartWindows = controlledRestartWindows,
    controlledRestartRequests: restartRequests = controlledRestartRequests,
    controlledRestartResponses: restartResponses = controlledRestartResponses,
    forkDeletionRaceFacts = input.section8Facts?.forks?.attachmentDeletionRace
      ?? result.section8?.forks?.attachmentDeletionRace ?? [],
  } = input;
  const handled = [];
  const handledLifecycle = [];
  const handledTurnNavigation = [];
  const messageStreamClassification = classifyMessageStreamErrors(
    errorFacts, errorTexts, streamResponses, ownerResponses,
  );
  const incompleteStreamClassification = classifyIncompleteMessageStreamErrors(
    errorFacts, failures, streamResponses, ownerResponses,
  );
  const controlledRestartClassification = classifyControlledRestartErrors(
    errorFacts, errorTexts, failures, restartRequests, restartResponses, restartWindows,
  );
  const handledForkDeletion = classifyForkAttachmentDeletionConflicts(errorFacts, conflictFacts, forkDeletionRaceFacts);
  const handledMessageStream = messageStreamClassification.handledMessageStreamErrors;
  const handledMessageStreamIndexes = new Set(handledMessageStream.map((fact) => fact.consoleIndex));
  const handledIncompleteStreamIndexes = incompleteStreamClassification.consumedConsoleIndexes;
  const handledForkDeletionIndexes = new Set(handledForkDeletion.map((fact) => fact.consoleIndex));
  const handledControlledRestartIndexes = controlledRestartClassification.consumedConsoleIndexes;
  const consumed = new Set();
  const consumedTurnNavigationRequests = new Set();
  for (const error of errorFacts) {
    if (handledMessageStreamIndexes.has(error.index) || handledIncompleteStreamIndexes.has(error.index)
        || handledForkDeletionIndexes.has(error.index) || handledControlledRestartIndexes.has(error.index)) continue;
    const status = error.text === "Failed to load resource: the server responded with a status of 409 (Conflict)" ? 409
      : error.text === "Failed to load resource: the server responded with a status of 404 (Not Found)" ? 404 : null;
    if (status === null || typeof error.url !== "string" || !error.url) continue;
    // 每条拒绝必须绑定同文档的实际响应；删除还需原 journal、tombstone 和目录 DOM 证据。
    const responses = status === 409 ? conflictFacts : [...historyErrors, ...lifecycleErrors];
    const candidates = responses.filter((response) => response.pageId === error.pageId
      && response.documentId === error.documentId && response.url === error.url);
    const deletionFacts = candidates.map((refusal) => {
      let nodeId = refusal.nodeId ?? refusal.sessionId;
      let jobOwnerRequestId;
      const detail = refusal.body?.detail;
      const expectedRead = ["bootstrap", "pending", "breadcrumb", "main_thread", "session", "file_tree_settings", "goal", "attachment_content", "job"].includes(refusal.kind) && refusal.method === "GET"
        || refusal.kind === "history" && refusal.method === "POST";
      if (!expectedRead || refusal.status !== status || !refusal.requestId || refusal.body?.request_id !== refusal.requestId
          || typeof detail !== "string") return null;
      if (status === 404 && (refusal.kind === "breadcrumb" && detail !== `会话目录节点不存在: ${nodeId}`
          || ["bootstrap", "main_thread", "session", "goal"].includes(refusal.kind) && detail !== `Session ${nodeId} not found`
          || refusal.kind === "file_tree_settings" && detail !== `会话不存在: ${nodeId}`
          || refusal.kind === "attachment_content" && (typeof refusal.attachmentFileId !== "string"
            || !refusal.attachmentFileId.includes(`/resources/session/${nodeId}/attachments/`)
            || detail !== `会话附件已释放: ${refusal.attachmentFileId}`))) return null;
      if (refusal.kind === "job") {
        if (status !== 404 || detail !== `Job ${refusal.jobId} not found`) return null;
        const responses = jobOwnerResponses.filter((response) => response.pageId === refusal.pageId
          && new URL(response.url).origin === new URL(refusal.url).origin
          && response.workspaceHeader === refusal.workspaceHeader
          && response.receivedAt <= refusal.requestedAt && response.body?.data?.job_id === refusal.jobId);
        const owners = responses.map((response) => {
          const data = response.body?.data;
          const owner = response.kind === "admission" ? data.dispatch : data;
          if (!response.requestId || response.body?.request_id !== response.requestId
              || !owner || owner.job_id !== refusal.jobId || typeof owner.session_id !== "string"
              || typeof owner.thread_id !== "string" || !owner.thread_id
              || response.kind === "job" && (response.status !== 200 || response.method !== "GET" || response.jobId !== refusal.jobId)
              || response.kind === "admission" && (response.status !== 200 || response.method !== "POST"
                || owner.session_id !== response.sessionId || owner.thread_id !== data.thread_id
                || response.threadId !== null && response.threadId !== owner.thread_id)) return null;
          return { sessionId: owner.session_id, threadId: owner.thread_id, requestId: response.requestId };
        });
        if (!owners.length || owners.some((owner) => owner === null)
            || !owners.every((owner) => owner.sessionId === owners[0].sessionId && owner.threadId === owners[0].threadId)) return null;
        nodeId = owners[0].sessionId;
        jobOwnerRequestId = owners[0].requestId;
      } else if (typeof nodeId !== "string" || !detail.includes(nodeId)) return null;
      if (status === 409 && (refusal.kind === "breadcrumb" || !detail.startsWith("session_deletion_pending:"))) return null;
      const lifecycleProofIndex = deletionProofs.findIndex((proof) => proof.origin === new URL(refusal.url).origin
        && proof.workspaceHeader === refusal.workspaceHeader && proof.terminalRequestId
        && proof.journal.state === "completed" && proof.remaining.length === 0
        && proof.verifiedAt >= refusal.receivedAt
        && (refusal.kind === "breadcrumb" ? proof.nodeIds : proof.sessionIds).includes(nodeId)
        && proof.absentDomNodeIds.includes(nodeId)
        && (refusal.kind === "breadcrumb" || proof.absentChatSessionIds.includes(nodeId)));
      if (lifecycleProofIndex < 0) return null;
      const proof = deletionProofs[lifecycleProofIndex];
      return { refusalRequestId: refusal.requestId, deletionOperationId: proof.operationId,
        terminalRequestId: proof.terminalRequestId, lifecycleProofIndex,
        ...(jobOwnerRequestId ? { jobOwnerRequestId } : {}) };
    });
    if (candidates.length && deletionFacts.every((fact) => fact !== null)) {
      const index = candidates.findIndex((refusal) => !consumed.has(refusal.requestId));
      if (index < 0) continue;
      consumed.add(candidates[index].requestId);
      handledLifecycle.push({ consoleIndex: error.index, ...deletionFacts[index] });
      continue;
    }
    if (status === 404 && typeof error.url === "string") {
      const turnNavigationFacts = candidates.map((response) => {
        if (response.kind !== "history" || response.method !== "POST" || response.status !== 404
            || response.url !== error.url || response.threadId === null
            || !response.requestId || response.body?.request_id !== response.requestId
            || !response.parameters || typeof response.parameters.anchor_turn_id !== "string"
            || response.parameters.direction !== "around" || response.parameters.before_turns !== 3
            || response.parameters.after_turns !== 3
            || Object.keys(response.parameters).length !== 4) return null;
        const proofIndex = missingTurnProofs.findIndex((proof) => proof.pageId === error.pageId
          && proof.documentId === error.documentId && proof.pageId === response.pageId
          && proof.documentId === response.documentId && proof.sessionId === response.sessionId
          && proof.threadId === response.threadId && proof.workspaceId === response.workspaceHeader
          && proof.anchorTurnId === response.parameters.anchor_turn_id
          && proof.requestUrl === response.url && proof.responseUrl === response.url
          && new URL(response.url).pathname === `/api/v1/sessions/${proof.sessionId}/threads/${proof.threadId}/history`
          && proof.requestMethod === response.method && proof.responseStatus === response.status
          && proof.requestFrameIsMain === true && proof.requestOrigin === proof.pageOrigin
          && proof.requestBody?.direction === response.parameters.direction
          && proof.requestBody?.anchor_turn_id === response.parameters.anchor_turn_id
          && proof.requestBody?.before_turns === response.parameters.before_turns
          && proof.requestBody?.after_turns === response.parameters.after_turns
          && Object.keys(proof.requestBody ?? {}).length === 4
          && proof.requestId === response.requestId && proof.bodyRequestId === response.body.request_id
          && proof.detail === response.body.detail && proof.detail === `rollout Turn 不存在: ${proof.anchorTurnId}`
          && proof.alertText?.includes(proof.anchorTurnId) && proof.statusCount === 0
          && Number.isFinite(proof.scrollBefore) && Number.isFinite(proof.scrollAfter)
          && Math.abs(proof.scrollAfter - proof.scrollBefore) < 0.5
          && proof.observedSessionId === proof.sessionId && proof.observedThreadId === proof.threadId
          && proof.observedAriaCurrent === null);
        if (proofIndex < 0) return null;
        return { responseRequestId: response.requestId, missingTurnProofIndex: proofIndex };
      });
      if (turnNavigationFacts.length === 1 && turnNavigationFacts[0] !== null) {
        const fact = turnNavigationFacts[0];
        if (!consumedTurnNavigationRequests.has(fact.responseRequestId)) {
          consumedTurnNavigationRequests.add(fact.responseRequestId);
          handledTurnNavigation.push({ consoleIndex: error.index, ...fact });
          continue;
        }
      }
    }
    if (status !== 409) continue;
    const recoveries = candidates.map((conflict) => {
      const detail = conflict.body?.detail;
      if (conflict.kind !== "history" || conflict.method !== "POST"
          || !["before", "after"].includes(conflict.parameters?.direction)
          || typeof conflict.parameters?.cursor !== "string" || !conflict.parameters.cursor
          || !Number.isInteger(conflict.parameters?.turns) || conflict.parameters.turns < 1 || conflict.parameters.turns > 256
          || !conflict.requestId || conflict.body?.request_id !== conflict.requestId
          || detail?.code !== "stale_turn_cursor" || detail.reason !== "history_view_revision"
          || detail.session_id !== conflict.sessionId || !Number.isInteger(detail.cursor_epoch)
          || detail.cursor_epoch < 1 || detail.cursor_epoch !== detail.current_epoch
          || !Number.isInteger(detail.cursor_history_view_revision) || detail.cursor_history_view_revision < 0
          || !Number.isInteger(detail.current_history_view_revision)
          || detail.current_history_view_revision <= detail.cursor_history_view_revision) return null;
      for (const [liveProofIndex, proof] of liveProofs.entries()) {
        if (proof.pageId !== conflict.pageId || proof.documentId !== conflict.documentId
            || proof.sessionId !== conflict.sessionId || proof.workspaceId !== conflict.workspaceHeader) continue;
        const responses = recoveryFacts.filter((response) => response.pageId === proof.pageId
          && response.documentId === proof.documentId && response.sessionId === proof.sessionId
          && response.workspaceHeader === proof.workspaceId && response.body?.request_id === response.requestId
          && response.requestId && response.body.threadId === proof.threadId
          && response.body.projectionEpoch === detail.current_epoch && response.receivedAt <= proof.verifiedAt);
        for (const bootstrap of responses.filter((response) => response.kind === "bootstrap"
          && response.method === "GET" && response.body.sessionId === proof.sessionId
          && response.requestedAt >= conflict.receivedAt)) {
          const tail = responses.find((response) => response.kind === "history" && response.method === "POST"
            && response.parameters?.direction === "tail" && response.parameters.cursor == null
            && Number.isInteger(response.parameters.turns) && response.parameters.turns >= 1 && response.parameters.turns <= 256
            && Object.keys(response.parameters).every((key) => ["direction", "turns"].includes(key))
            && response.requestedAt >= bootstrap.receivedAt);
          if (tail) return { conflictRequestId: conflict.requestId, bootstrapRequestId: bootstrap.requestId,
            freshTailRequestId: tail.requestId, liveProofIndex };
        }
      }
      return null;
    });
    if (!candidates.length || recoveries.some((recovery) => recovery === null)) continue;
    const index = candidates.findIndex((conflict) => !consumed.has(conflict.requestId));
    if (index < 0) continue;
    consumed.add(candidates[index].requestId);
    handled.push({ consoleIndex: error.index, ...recoveries[index] });
  }
  const handledConsoleIndexes = new Set([...handled, ...handledLifecycle, ...handledTurnNavigation,
    ...handledMessageStream, ...handledForkDeletion]
    .map((fact) => fact.consoleIndex));
  for (const index of handledIncompleteStreamIndexes) handledConsoleIndexes.add(index);
  for (const index of handledControlledRestartIndexes) handledConsoleIndexes.add(index);
  const handledFailureIndexes = new Set([...incompleteStreamClassification.consumedFailureIndexes,
    ...controlledRestartClassification.consumedFailureIndexes]);
  return { handledHistoryConflicts: handled, handledLifecycleErrors: handledLifecycle,
    handledTurnNavigationErrors: handledTurnNavigation,
    handledMessageStreamErrors: handledMessageStream,
    handledForkAttachmentDeletionConflicts: handledForkDeletion,
    handledIncompleteMessageStreamErrors: incompleteStreamClassification.handled,
    handledControlledRestartErrors: controlledRestartClassification.handled,
    unhandledConsoleErrors: errorTexts.filter((_error, index) => !handledConsoleIndexes.has(index)),
    unhandledFailedRequests: failures.filter((_failure, index) => !handledFailureIndexes.has(index)) };
}

function installPageDiagnostics(page) {
  page.controlledRestartWindow = null;
  page.diagnosticResponseBodies = new Set();
  page.diagnosticId = ++diagnosticPageId;
  page.diagnosticDocumentId = 0;
  page.diagnosticSessionIds = new Set();
  page.diagnosticObservationRequests = new Map();
  const requestDocuments = new WeakMap();
  const streamResponseByRequest = new WeakMap();
  const diagnosticResponseFacts = new WeakMap();
  const cdpStreamCaptures = new Map();
  const pendingCdpRequests = [];
  const pendingPlaywrightRequests = [];
  const pendingCdpResponses = new Map();
  const cdpNetworkRequests = new Map();
  const bindBadGatewayResponse = () => {
    for (const response of controlledRestartResponses) {
      if (response.pageId !== page.diagnosticId || response.status !== 502) continue;
      const candidates = [...pendingCdpResponses.entries()].filter(([, native]) => {
        const request = native.request;
        return request && native.status === 502 && native.serverRequestId === response.requestId
          && native.url === response.url && request.method === response.method
          && request.documentId === response.documentId && request.workspaceHeader === response.workspaceHeader
          && Math.abs(native.receivedAt - response.receivedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS;
      });
      if (candidates.length === 1) response.cdpRequestId = candidates[0][0];
    }
  };
  const pendingPlaywrightResponses = new Map();
  let cdpSession = null;
  const bindObservationRequest = () => {
    for (const cdpRequest of pendingCdpRequests) {
      if (cdpRequest.bound) continue;
      const candidates = pendingPlaywrightRequests.filter((item) => !item.bound
        && item.fact.pageId === page.diagnosticId && item.fact.documentId === cdpRequest.documentId
        && item.fact.url === cdpRequest.url && item.fact.method === "POST"
        && item.fact.workspaceHeader === cdpRequest.workspaceHeader
        && JSON.stringify(item.fact.body) === JSON.stringify(cdpRequest.body)
        && Math.abs(item.fact.requestedAt - cdpRequest.requestedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (candidates.length !== 1) continue;
      const [playwrightRequest] = candidates;
      const reverse = pendingCdpRequests.filter((candidate) => !candidate.bound
        && candidate.documentId === playwrightRequest.fact.documentId
        && candidate.url === playwrightRequest.fact.url
        && candidate.workspaceHeader === playwrightRequest.fact.workspaceHeader
        && JSON.stringify(candidate.body) === JSON.stringify(playwrightRequest.fact.body)
        && Math.abs(candidate.requestedAt - playwrightRequest.fact.requestedAt) <= CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS);
      if (reverse.length !== 1) continue;
      cdpRequest.bound = true;
      playwrightRequest.bound = true;
      playwrightRequest.fact.cdpRequestId = cdpRequest.requestId;
      cdpStreamCaptures.set(cdpRequest.requestId, { requestFactId: playwrightRequest.fact.requestFactId,
        subscriptions: new Set(cdpRequest.subscriptions.map((item) => item.subscription_id)), pending: "",
        bufferedReady: false, queuedChunks: [] });
      bindObservationResponse(cdpRequest.requestId);
    }
  };
  const bindObservationResponse = (requestId) => {
    const capture = cdpStreamCaptures.get(requestId);
    const cdpResponse = pendingCdpResponses.get(requestId);
    const playwrightResponse = pendingPlaywrightResponses.get(capture?.requestFactId);
    if (!capture || !cdpResponse || !playwrightResponse || cdpResponse.bound || playwrightResponse.bound) return;
    if (cdpResponse.status !== playwrightResponse.fact.status
        || cdpResponse.status !== 200
        || cdpResponse.contentType !== playwrightResponse.fact.contentType
        || Math.abs(cdpResponse.receivedAt - playwrightResponse.fact.receivedAt) > CONTROLLED_RESTART_EVENT_MATCH_WINDOW_MS) return;
    cdpResponse.bound = true;
    playwrightResponse.bound = true;
    const responseFact = playwrightResponse.fact;
    responseFact.cdpRequestId = requestId;
    responseFact.connectedSubscriptions = [];
    const parseChunk = (encoded) => {
      capture.pending += Buffer.from(encoded, "base64").toString("utf8");
      const blocks = capture.pending.split(/\r?\n\r?\n/);
      capture.pending = blocks.pop() ?? "";
      for (const block of blocks) {
        const data = block.split(/\r?\n/).filter((line) => line.startsWith("data:"))
          .map((line) => line.slice(5).trimStart()).join("\n");
        if (!data) continue;
        try {
          const envelope = JSON.parse(data);
          if (envelope.state === "connected" && capture.subscriptions.has(envelope.subscription_id)
              && !responseFact.connectedSubscriptions.includes(envelope.subscription_id)) {
            responseFact.connectedSubscriptions.push(envelope.subscription_id);
          }
        } catch (error) {
          controlledRestartStreamDiagnostics.push({ pageId: page.diagnosticId,
            requestFactId: capture.requestFactId, phase: "sse-envelope", error: String(error) });
        }
      }
    };
    const drain = () => {
      while (capture.queuedChunks.length) parseChunk(capture.queuedChunks.shift());
    };
    if (!cdpSession) {
      controlledRestartStreamDiagnostics.push({ pageId: page.diagnosticId,
        requestFactId: capture.requestFactId, phase: "cdp-session-unavailable" });
      return;
    }
    void cdpSession.send("Network.streamResourceContent", { requestId }).then((result) => {
      if (result.bufferedData) parseChunk(result.bufferedData);
      capture.bufferedReady = true;
      drain();
    }, (error) => {
      controlledRestartStreamDiagnostics.push({ pageId: page.diagnosticId,
        requestFactId: capture.requestFactId, phase: "streamResourceContent", error: String(error) });
    });
    capture.receiveChunk = (encoded) => {
      if (!capture.bufferedReady) capture.queuedChunks.push(encoded);
      else parseChunk(encoded);
    };
  };
  const cdpSetup = page.context().newCDPSession(page).then(async (cdp) => {
    cdpSession = cdp;
    await cdp.send("Network.enable");
    cdp.on("Log.entryAdded", ({ entry }) => {
      if (entry.source !== "network" || entry.level !== "error" || entry.text !== BAD_GATEWAY_CONSOLE_TEXT) return;
      const restartWindow = page.controlledRestartWindow;
      const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
      const fact = { text: entry.text, url: entry.url, pageId: page.diagnosticId,
        documentId: page.diagnosticDocumentId, receivedAt: Date.now(),
        restartWindowId: restartWindow?.operationId ?? null, pageRole: pageBinding?.role ?? null,
        cdpRequestId: entry.networkRequestId ?? null };
      badGatewayConsoleAudit.push({ ...fact, source: "cdp" });
      consoleErrorFacts.push({ ...fact, index: consoleErrors.length });
      consoleErrors.push(entry.text);
    });
    await cdp.send("Log.enable");
    cdp.on("Network.requestWillBeSent", (event) => {
      const workspaceHeader = Object.entries(event.request.headers ?? {}).find(([name]) =>
        name.toLowerCase() === "x-boxteam-workspace-id")?.[1] ?? null;
      cdpNetworkRequests.set(event.requestId, { method: event.request.method,
        documentId: page.diagnosticDocumentId, workspaceHeader });
      if (event.request.method !== "POST" || new URL(event.request.url).pathname !== OBSERVATION_STREAM_PATH) return;
      let body;
      try {
        body = JSON.parse(event.request.postData ?? "{}");
      } catch (error) {
        controlledRestartStreamDiagnostics.push({ pageId: page.diagnosticId, requestId: event.requestId,
          phase: "request-body", error: String(error) });
        return;
      }
      pendingCdpRequests.push({ requestId: event.requestId, url: event.request.url, body,
        subscriptions: body.subscriptions,
        workspaceHeader, documentId: page.diagnosticDocumentId, requestedAt: Date.now(), bound: false });
      bindObservationRequest();
    });
    cdp.on("Network.responseReceived", (event) => {
      pendingCdpResponses.set(event.requestId, { status: event.response.status,
        url: event.response.url, request: cdpNetworkRequests.get(event.requestId),
        serverRequestId: Object.entries(event.response.headers ?? {}).find(([name]) => name.toLowerCase() === "x-request-id")?.[1] ?? null,
        contentType: event.response.headers?.["content-type"] ?? event.response.headers?.["Content-Type"] ?? null,
        receivedAt: Date.now(), bound: false });
      bindObservationResponse(event.requestId);
      if (event.response.status === 502) bindBadGatewayResponse();
    });
    cdp.on("Network.dataReceived", (event) => {
      const capture = cdpStreamCaptures.get(event.requestId);
      if (capture?.receiveChunk && event.data) capture.receiveChunk(event.data);
    });
  }).catch((error) => {
    controlledRestartStreamDiagnostics.push({ pageId: page.diagnosticId, phase: "cdp-setup", error: String(error) });
    throw error;
  });
  controlledRestartCdpSetups.add(cdpSetup);
  void cdpSetup.then(() => controlledRestartCdpSetups.delete(cdpSetup),
    () => controlledRestartCdpSetups.delete(cdpSetup));
  page.controlledRestartCdpSetup = cdpSetup;
  page.on("framenavigated", (frame) => {
    if (frame !== page.mainFrame()) return;
    page.diagnosticDocumentId += 1;
    const restartWindow = page.controlledRestartWindow;
    const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
    if (pageBinding && !pageBinding.documentIds.includes(page.diagnosticDocumentId)) {
      pageBinding.documentIds.push(page.diagnosticDocumentId);
    }
  });
  let expectedUnauthorizedProbeCount = 0;
  page.on("pageerror", (error) => pageErrors.push(String(error)));
  page.on("console", (message) => {
  if (message.type() !== "error") return;
  const text = message.text();
  if (text === BAD_GATEWAY_CONSOLE_TEXT) {
    // 网络日志提供精确 request ID；另一采集源逐页/文档核数量，缺漏或重复都不能通过。
    badGatewayConsoleAudit.push({ source: "playwright", text, url: message.location().url,
      pageId: page.diagnosticId, documentId: page.diagnosticDocumentId });
    return;
  }
  if (
    expectedUnauthorizedProbeCount > 0
    && text === "Failed to load resource: the server responded with a status of 401 (Unauthorized)"
  ) {
    expectedUnauthorizedProbeCount -= 1;
    return;
  }
  const restartWindow = page.controlledRestartWindow;
  const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
  consoleErrorFacts.push({ index: consoleErrors.length, text, pageId: page.diagnosticId,
    documentId: page.diagnosticDocumentId, receivedAt: Date.now(),
    restartWindowId: restartWindow?.operationId ?? null, pageRole: pageBinding?.role ?? null,
    ...message.location() });
  consoleErrors.push(text);
});
page.on("request", (request) => {
  const requestUrl = request.url();
  const routeSessionId = sessionTargetFromUrl(requestUrl);
  if (routeSessionId) page.diagnosticSessionIds.add(routeSessionId);
  const restartWindow = page.controlledRestartWindow;
  const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
  const requestDocument = { requestFactId: `browser-request-${++controlledRestartRequestOrdinal}`,
    pageId: page.diagnosticId, documentId: page.diagnosticDocumentId, requestedAt: Date.now(),
    url: requestUrl, method: request.method(), workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null,
    restartWindowId: restartWindow?.operationId ?? null, pageRole: pageBinding?.role ?? null };
  requestDocuments.set(request, requestDocument);
  if (request.method() === "POST" && new URL(requestUrl).pathname === OBSERVATION_STREAM_PATH) {
    try {
      requestDocument.body = request.postDataJSON();
    } catch (error) {
      requestDocument.bodyError = String(error);
    }
    controlledRestartRequests.push(requestDocument);
    page.diagnosticObservationRequests.set(request, requestDocument);
    if (requestDocument.bodyError === undefined) {
      pendingPlaywrightRequests.push({ fact: requestDocument,
        subscriptions: requestDocument.body?.subscriptions ?? [], bound: false });
      bindObservationRequest();
    }
  }
  if (
    request.method() === "GET"
    && new URL(request.url()).pathname === "/api/gateway/users/current"
  ) {
    expectedUnauthorizedProbeCount += 1;
  }
});
page.on("requestfinished", (request) => {
  page.diagnosticObservationRequests.delete(request);
  const fact = diagnosticResponseFacts.get(request);
  if (fact) fact.requestFinishedAt = Date.now();
});
page.on("requestfailed", (request) => {
  page.diagnosticObservationRequests.delete(request);
  const fact = diagnosticResponseFacts.get(request);
  if (fact) fact.requestFailure = { error: request.failure()?.errorText, failedAt: Date.now() };
  if (isExpectedBrowserAbort(request)) return;
  const failedAt = Date.now();
  const requestDocument = requestDocuments.get(request);
  const streamResponse = streamResponseByRequest.get(request);
  const error = request.failure()?.errorText || "unknown";
  if (streamResponse) {
    streamResponse.failedAt = failedAt;
    streamResponse.failure = error;
  }
  failedRequests.push({ url: request.url(), error, pageId: page.diagnosticId,
    method: request.method(), documentId: requestDocument?.documentId ?? null,
    requestedAt: requestDocument?.requestedAt ?? null, failedAt,
    restartWindowId: requestDocument?.restartWindowId ?? page.controlledRestartWindow?.operationId ?? null,
    pageRole: requestDocument?.pageRole ?? (page.controlledRestartWindow
      ? controlledRestartPageBinding(page.controlledRestartWindow, page.diagnosticId)?.role ?? null : null),
    ...(requestDocument?.url === new URL(request.url()).origin + OBSERVATION_STREAM_PATH
      ? { requestFactId: requestDocument.requestFactId } : {}),
    ...(streamResponse ? { responseRequestId: streamResponse.requestId, status: streamResponse.status,
      sessionId: streamResponse.sessionId, turnId: streamResponse.turnId, threadId: streamResponse.threadId,
      turnStreamId: streamResponse.turnStreamId, afterSeq: streamResponse.afterSeq,
      workspaceHeader: streamResponse.workspaceHeader, messageStreamId: streamResponse.messageStreamId } : {}) });
});
page.on("response", (response) => {
  const url = new URL(response.url());
  if (response.request().method() === "POST" && url.pathname === OBSERVATION_STREAM_PATH && !url.search) {
    const request = response.request();
    const requestDocument = requestDocuments.get(request);
    const restartWindow = page.controlledRestartWindow;
    const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
    const subscriptions = requestDocument?.body?.subscriptions;
    controlledRestartResponses.push({ requestFactId: requestDocument?.requestFactId ?? null,
      restartWindowId: requestDocument?.restartWindowId ?? restartWindow?.operationId ?? null,
      url: response.url(), method: "POST", status: response.status(), pageId: page.diagnosticId,
      documentId: requestDocument?.documentId ?? null,
      pageRole: requestDocument?.pageRole ?? pageBinding?.role ?? null,
      requestedAt: requestDocument?.requestedAt ?? null, receivedAt: Date.now(),
      workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null,
      requestId: response.headers()["x-request-id"] ?? null,
      contentType: response.headers()["content-type"] ?? null,
      subscriptions: Array.isArray(subscriptions) ? subscriptions : null,
      connectedSubscriptions: [] });
    const responseFact = controlledRestartResponses.at(-1);
    if (responseFact.requestFactId !== null) {
      pendingPlaywrightResponses.set(responseFact.requestFactId, { fact: responseFact, bound: false });
      const capture = [...cdpStreamCaptures.values()].find((item) => item.requestFactId === responseFact.requestFactId);
      if (capture) {
        const cdpRequestId = [...cdpStreamCaptures.entries()].find(([, item]) => item === capture)?.[0];
        if (cdpRequestId) bindObservationResponse(cdpRequestId);
      }
    }
  }
  if (response.status() === 502) {
    const request = response.request();
    const requestDocument = requestDocuments.get(request);
    const restartWindow = page.controlledRestartWindow;
    const pageBinding = restartWindow && controlledRestartPageBinding(restartWindow, page.diagnosticId);
    const restartWindowId = requestDocument?.restartWindowId ?? restartWindow?.operationId ?? null;
    if (restartWindow && pageBinding && restartWindowId === restartWindow.operationId
        && requestDocument?.pageId === page.diagnosticId
        && requestDocument.documentId === page.diagnosticDocumentId) {
      const responseHeaders = response.headers();
      const requestBodyText = request.postData();
      let requestBody = null;
      let requestBodyError;
      if (requestBodyText) {
        try {
          requestBody = JSON.parse(requestBodyText);
        } catch (error) {
          requestBodyError = String(error);
        }
      }
      const requestFact = { requestFactId: requestDocument.requestFactId,
        restartWindowId, url: response.url(), method: request.method(), pageId: page.diagnosticId,
        documentId: requestDocument.documentId, pageRole: pageBinding.role,
        requestedAt: requestDocument.requestedAt, workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null,
        body: requestBody, bodyText: requestBodyText, ...(requestBodyError ? { bodyError: requestBodyError } : {}) };
      const responseFact = controlledRestartResponses.find((candidate) => candidate.requestFactId === requestDocument.requestFactId)
        ?? { requestFactId: requestDocument.requestFactId,
        restartWindowId, url: response.url(), method: request.method(), status: response.status(), pageId: page.diagnosticId,
        documentId: requestDocument.documentId, pageRole: pageBinding.role, requestedAt: requestDocument.requestedAt,
        receivedAt: Date.now(), requestId: responseHeaders["x-request-id"] ?? null,
        bodyRequestId: null, body: null, workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null };
      const existingRequest = controlledRestartRequests.find((candidate) => candidate.requestFactId === requestFact.requestFactId);
      if (existingRequest) {
        // 同一真实请求可能在窗口前发出；用已核对页面/文档的 502 响应补齐其中断归属。
        Object.assign(existingRequest, requestFact);
      } else {
        controlledRestartRequests.push(requestFact);
      }
      if (!controlledRestartResponses.some((candidate) => candidate.requestFactId === responseFact.requestFactId)) {
        controlledRestartResponses.push(responseFact);
      }
      bindBadGatewayResponse();
      diagnosticResponseFacts.set(request, responseFact);
      const read = response.text().then((bodyText) => {
        responseFact.bodyText = bodyText;
        try {
          responseFact.body = bodyText ? JSON.parse(bodyText) : null;
          responseFact.bodyRequestId = responseFact.body?.request_id ?? null;
        } catch (error) {
          responseFact.bodyError = String(error);
        }
      }, (error) => { responseFact.bodyError = String(error); });
      trackDiagnosticResponseBody(page, read, responseFact);
    }
  }
  const streamTarget = messageStreamTarget(response.url());
  if (streamTarget) {
    const request = response.request();
    const requestDocument = requestDocuments.get(request);
    const responseHeaders = response.headers();
    const fact = {
      url: response.url(),
      method: request.method(),
      status: response.status(),
      pageId: page.diagnosticId,
      documentId: requestDocument?.documentId ?? null,
      requestedAt: requestDocument?.requestedAt ?? null,
      receivedAt: Date.now(),
      sessionId: streamTarget.sessionId,
      turnId: streamTarget.turnId,
      threadId: streamTarget.threadId,
      turnStreamId: streamTarget.turnStreamId,
      afterSeq: streamTarget.afterSeq,
      workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null,
      requestId: responseHeaders["x-request-id"] ?? null,
      messageStreamId: responseHeaders["x-message-stream-id"] ?? null,
      headers: {
        requestId: responseHeaders["x-request-id"] ?? null,
        messageStreamId: responseHeaders["x-message-stream-id"] ?? null,
        contentType: responseHeaders["content-type"] ?? null,
        contentLength: responseHeaders["content-length"] ?? null,
      },
    };
    messageStreamResponses.push(fact);
    streamResponseByRequest.set(request, fact);
    if (fact.status === 404) {
      diagnosticResponseFacts.set(request, fact);
      const read = readBoundedMessageStreamErrorBody(response, fact)
        .catch((error) => { fact.bodyError = String(error); });
      trackDiagnosticResponseBody(page, read, fact);
    }
  }
  const history = /^\/api\/v1\/sessions\/([^/]+)\/(?:(bootstrap|history)|threads\/([^/]+)\/history)$/.exec(url.pathname);
  const historyKind = history?.[2] ?? (history?.[3] ? "history" : null);
  const pending = /^\/api\/v1\/sessions\/([^/]+)\/main-thread\/pending-requests$/.exec(url.pathname);
  const breadcrumb = /^\/api\/v1\/session-catalog\/breadcrumb\/([^/]+)$/.exec(url.pathname);
  const mainThread = /^\/api\/v1\/sessions\/([^/]+)\/main-thread$/.exec(url.pathname);
  const session = /^\/api\/v1\/sessions\/([^/]+)$/.exec(url.pathname);
  const fileTreeSettings = /^\/api\/v1\/sessions\/([^/]+)\/file-tree-settings$/.exec(url.pathname);
  const goal = /^\/api\/v1\/sessions\/([^/]+)\/goal$/.exec(url.pathname);
  const attachmentContent = /^\/api\/v1\/sessions\/([^/]+)\/attachments\/content$/.exec(url.pathname);
  const job = /^\/api\/v1\/jobs\/([^/]+)$/.exec(url.pathname);
  const admission = /^\/api\/v1\/sessions\/([^/]+)\/(?:threads\/([^/]+)\/)?messages$/.exec(url.pathname);
  const forkContext = /^\/api\/v1\/sessions\/([^/]+)\/fork-context$/.exec(url.pathname);
  if (response.status() === 409 || history && [200, 404].includes(response.status())
      || (pending || breadcrumb || mainThread) && response.status() === 404
      || (session || fileTreeSettings || goal || attachmentContent) && response.status() === 404
      || job && [200, 404].includes(response.status())
      || admission && response.request().method() === "POST" && response.status() === 200) {
    const request = response.request();
    const fact = { url: response.url(), method: request.method(), status: response.status(),
      pageId: page.diagnosticId, ...requestDocuments.get(request),
      kind: historyKind ?? (pending ? "pending" : breadcrumb ? "breadcrumb"
        : mainThread ? "main_thread" : session ? "session" : fileTreeSettings ? "file_tree_settings"
          : goal ? "goal" : attachmentContent ? "attachment_content" : job ? "job"
            : admission ? "admission" : forkContext ? "fork_context" : null),
      sessionId: history?.[1] ?? pending?.[1] ?? mainThread?.[1] ?? session?.[1] ?? fileTreeSettings?.[1]
        ?? goal?.[1] ?? attachmentContent?.[1] ?? admission?.[1] ?? forkContext?.[1] ?? null,
      nodeId: breadcrumb?.[1] ?? null, jobId: job?.[1] ?? null,
      attachmentFileId: attachmentContent ? url.searchParams.get("file_id") : null,
      threadId: history?.[3] ?? admission?.[2] ?? null,
      parameters: historyKind === "history" && request.method() === "POST" ? request.postDataJSON() : null,
      workspaceHeader: request.headers()["x-boxteam-workspace-id"] ?? null,
      requestId: response.headers()["x-request-id"] ?? null,
      receivedAt: Date.now() };
    if (fact.kind === "fork_context" && request.method() === "POST") {
      try {
        fact.forkRequest = request.postDataJSON();
      } catch (error) {
        fact.forkRequestBodyError = String(error);
      }
    }
    if (fact.status === 409) {
      conflictResponses.push(fact);
      if (fact.kind === "history") historyConflictResponses.push(fact);
    } else if (fact.status === 200 && (job || admission)) jobOwnerResponses.push(fact);
    else if (fact.status === 200) historyRecoveryResponses.push(fact);
    else if (history) historyErrorResponses.push(fact);
    else lifecycleErrorResponses.push(fact);
    diagnosticResponseFacts.set(request, fact);
    const read = response.json().then((body) => {
      fact.body = fact.status !== 200 || job || admission ? body : { request_id: body.request_id, sessionId: body.data?.session?.session_id,
        threadId: body.data?.thread_id, projectionEpoch: body.data?.projection_epoch };
    }, (error) => { fact.bodyError = String(error); });
    trackDiagnosticResponseBody(page, read, fact);
  }
  if (url.pathname === "/api/gateway/workspaces" || /^\/api\/gateway\/workspaces\/[^/]+\/activate$/.test(url.pathname)
      || url.pathname === "/api/v1/workspace" || url.pathname === "/api/v1/session-catalog/children") {
    const fact = { url: response.url(), method: response.request().method(), status: response.status(),
      workspaceHeader: response.request().headers()["x-boxteam-workspace-id"] ?? null,
      requestId: response.headers()["x-request-id"] ?? null, receivedAt: Date.now() };
    navigationResponses.push(fact);
    diagnosticResponseFacts.set(response.request(), fact);
    const read = response.json().then((body) => { fact.body = body; }, (error) => { fact.bodyError = String(error); });
    trackDiagnosticResponseBody(page, read, fact);
  }
  if (
    response.status() !== 401
    && new URL(response.url()).pathname === "/api/gateway/users/current"
    && expectedUnauthorizedProbeCount > 0
  ) {
    expectedUnauthorizedProbeCount -= 1;
  }
});
}
const result = {
  workspaceId: expectedWorkspaceId,
  sessionId: null,
  firstTurn: null,
  restoredHistory: null,
  secondTurn: null,
  persisted: null,
  streams: { trace: 0, message: 0 },
  diagnostics: { pageErrors, consoleErrors, consoleErrorFacts, failedRequests, diagnosticPageTransitions,
    controlledRestartWindows, controlledRestartRequests, controlledRestartResponses, controlledRestartStreamDiagnostics, badGatewayConsoleAudit },
  messageStreamResponses,
  navigationResponses,
  historyConflictResponses,
  conflictResponses,
  historyRecoveryResponses,
  historyErrorResponses,
  lifecycleErrorResponses,
  jobOwnerResponses,
  lifecycleDeletionProofs,
  turnNavigationMissingTurnProofs,
  historyLiveProofs,
  historyTurnNavigationProofs,
  transcriptDomFailures,
  forkRequests: [],
  forkDurabilityProofs: [],
};

function runDeletionReadClassifierSelfTest() {
  const sessionId = "ses_deletion_classifier";
  const url = `http://127.0.0.1:19000/api/v1/sessions/${sessionId}`;
  const text = "Failed to load resource: the server responded with a status of 409 (Conflict)";
  const refusal = { url, pageId: 7, documentId: 8, kind: "session", method: "GET", status: 409,
    sessionId, workspaceHeader: "workspace-a", requestId: "request-refused", receivedAt: 100,
    body: { request_id: "request-refused", detail: `session_deletion_pending: session_id='${sessionId}', state='deleting'` } };
  const proof = { origin: new URL(url).origin, workspaceHeader: "workspace-a", terminalRequestId: "request-terminal",
    journal: { state: "completed" }, remaining: [], verifiedAt: 200, sessionIds: [sessionId],
    nodeIds: [sessionId], absentDomNodeIds: [sessionId], absentChatSessionIds: [sessionId], operationId: "delete-op" };
  const classify = (response = refusal, proofs = [proof]) => classifyConsoleErrors({
    consoleErrorFacts: [{ index: 0, url, text, pageId: 7, documentId: 8 }], consoleErrors: [text],
    failedRequests: [], conflictResponses: [response], lifecycleDeletionProofs: proofs,
    historyErrorResponses: [], lifecycleErrorResponses: [], forkDeletionRaceFacts: [],
    messageStreamResponses: [], jobOwnerResponses: [], turnNavigationMissingTurnProofs: [],
    historyLiveProofs: [], historyRecoveryResponses: [], controlledRestartWindows: [],
    controlledRestartRequests: [], controlledRestartResponses: [],
  });
  assert.deepEqual(classify().unhandledConsoleErrors, []);
  assert.deepEqual(classify(refusal, []).unhandledConsoleErrors, [text], "缺少删除终态不得忽略409");
  assert.deepEqual(classify(refusal, [{ ...proof, remaining: [sessionId] }]).unhandledConsoleErrors, [text]);
  assert.deepEqual(classify(refusal, [{ ...proof, workspaceHeader: "workspace-other" }]).unhandledConsoleErrors, [text]);
  assert.deepEqual(classify({ ...refusal, body: { ...refusal.body, request_id: "request-other" } }).unhandledConsoleErrors, [text]);
  assert.deepEqual(classify({ ...refusal, body: undefined, bodyError: "Network.getResponseBody: No data found" }).unhandledConsoleErrors, [text], "缺少原响应正文不能由删除终态补造");
  assert.deepEqual(classify({ ...refusal, body: { ...refusal.body, detail: `会话物理目录缺失: ${sessionId}, path=/private/session` } }).unhandledConsoleErrors, [text]);
}

if (messageStreamDiagnosticsSelfTest) {
  runMessageStreamDiagnosticsSelfTest();
  runDeletionReadClassifierSelfTest();
  await runDiagnosticResponseDrainSelfTest();
  process.stdout.write("message stream diagnostics self-test passed\n");
} else if (forkDeadlineClassifierSelfTest) {
  await runForkDeadlineClassifierSelfTest();
  process.stdout.write("fork deadline classifier self-test passed\n");
} else if (forkPeerContextCleanupSelfTest) {
  await runForkPeerContextCleanupSelfTest();
  process.stdout.write("fork peer context cleanup self-test passed\n");
} else if (controlledRestartClassifierSelfTest) {
  runMessageStreamDiagnosticsSelfTest();
  await runControlledRestartPageCloseSelfTest();
  process.stdout.write("controlled restart classifier self-test passed\n");
} else {
try {
  browser = await browserStartup("browser_launch", () => chromium.launch({ executablePath, headless: true, timeout: 30_000 }));
  context = await browserStartup("browser_context", () => browser.newContext({ viewport: { width: 1920, height: 1100 } }));
  page = await browserStartup("browser_page", () => context.newPage());
  page.setDefaultTimeout(30_000);
  page.setDefaultNavigationTimeout(30_000);
  installPageDiagnostics(page);
  const startupEvents = await testControl({ action: "events" });
  assert.deepEqual(startupEvents.filter((event) => event.phase === "agent.graph.compiled" && event.details?.owns_thread === true), [], "backend 启动不能构建 durable thread compiled Agent graph");
  result.startupGraphEvents = startupEvents.filter((event) => GRAPH_PHASES.has(event.phase));
  await ensureGuest(page);
  const workspaces = await api(page, "/api/gateway/workspaces");
  assert.equal(workspaces?.data?.active_workspace_id, expectedWorkspaceId, "active workspace 不一致");


  result.section8Mode = section8Selection.mode;
  result.focusedScenarios = section8Selection.requested;
  result.section8 = await runSection8Product(browser, page);
  await Promise.all(navigationReads);
  await Promise.all(controlledRestartCdpSetups);
  Object.assign(result.diagnostics, classifyConsoleErrors());
  assert.deepEqual(pageErrors, [], "浏览器 pageerror 出现");
  assert.deepEqual(badGatewayConsoleAuditDifferences(), [], "502 的 Playwright/CDP 两份采集必须逐页/文档/URL 等量，禁止漏报错误");
  assert.deepEqual(result.diagnostics.unhandledConsoleErrors, [], "浏览器存在未处理的 console error");
  assert.deepEqual(result.diagnostics.unhandledFailedRequests, [], "浏览器请求失败缺少同一 stream 的后续恢复证据");
  await writeFile(resultPath, JSON.stringify(result, null, 2));
} catch (error) {
  // 先独立保存原始失败，完整证据序列化或截图失败不能覆盖业务错误。
  const failure = { scenario: result.runningScenario ?? "final-checks", error: String(error), stack: error.stack };
  process.stderr.write(`${failure.scenario}: ${failure.stack ?? failure.error}\n`);
  await writeFile(`${resultPath}.error.json`, JSON.stringify(failure, null, 2));
  let screenshotError = null;
  try {
    if (page) await page.screenshot({ path: screenshotPath, fullPage: true, timeout: 30_000 });
  } catch (captureError) {
    screenshotError = String(captureError);
  }
  await Promise.all(navigationReads);
  Object.assign(result.diagnostics, classifyConsoleErrors());
  // 长流程的完整诊断仍在内存中供分类；失败回执逐项写出，避免再复制整份历史掩盖原始错误。
  // TODO: 按完整default测量继续定位driver内存增长；释放响应尚未证明解决target detach时的OOM。
  const diagnostics = {};
  for (const [name, value] of Object.entries({ ...result.diagnostics,
    conflictResponses, historyErrorResponses, lifecycleErrorResponses, lifecycleDeletionProofs,
  })) {
    if (Array.isArray(value)) {
      const file = `${resultPath}.${name}.jsonl`;
      await writeFile(file, "");
      for (const record of value) await appendFile(file, `${JSON.stringify(record)}\n`);
      diagnostics[name] = { count: value.length, file };
    } else {
      diagnostics[name] = value;
    }
  }
  await writeFile(resultPath, JSON.stringify({ ...failure, screenshotError,
    section8Mode: result.section8Mode, focusedScenarios: result.focusedScenarios,
    completedScenarios: Object.keys(result.section8 ?? {}), diagnostics }, null, 2));
  throw error;
} finally {
  try {
    if (context) await browserStartup("browser_context_close", () => context.close());
  } finally {
    try {
      if (browser) await browserStartup("browser_close", () => browser.close());
    } finally {
      await closeForkDeadlineWatchdog();
    }
  }
}
}
