import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { cp, mkdir, readFile, rmdir, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { BrowserManager } from "../../../../src/workspace-services/browser/server/browserManager.js";
import { TestRunContext } from "../../../harness/js/run-context.mjs";

const run = await TestRunContext.fromTestFile(fileURLToPath(import.meta.url)).prepare();
const runtimeDirectory = path.join(run.outputRoot, "runtime");
await mkdir(runtimeDirectory, { recursive: true });
for (const name of ["TMPDIR", "TMP", "TEMP"]) process.env[name] = runtimeDirectory;

function barrier() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release };
}

let blockedNavigation = null;
const server = createServer(async (_request, response) => {
  const blocked = blockedNavigation;
  blockedNavigation = null;
  if (blocked) {
    blocked.entered.release();
    await blocked.response.promise;
  }
  response.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  response.end('<!doctype html><html><head><title>关闭顺序</title></head><body><input id="field" value="initial"></body></html>');
});
const results = [];

async function scenario(phase) {
  const workspaceId = randomUUID();
  const workspaceRoot = path.join(run.workspaceRoot, `${phase}-${randomUUID()}`);
  await cp(path.join(run.projectRoot, "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  await mkdir(path.join(workspaceRoot, ".boxteam"), { recursive: true });
  await writeFile(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({ workspace_id: workspaceId }));
  const manager = new BrowserManager({ workspaceRoot, workspaceId });
  const browserId = `browser_${randomUUID().replaceAll("-", "")}`;
  const navigation = { entered: barrier(), response: barrier() };
  const action = barrier();
  const enteredAction = barrier();
  let operation;
  let peerOperation;
  let closingAttempt;
  let closing;
  let restored;
  const order = [];
  try {
    await manager.init();
    await manager.create({ workspaceId, browserId, ownerRevision: 1, url: `http://127.0.0.1:${server.address().port}/` });
    const session = manager.get(browserId);
    let peer = null;
    if (phase === "manager_batch_timeout") {
      const peerId = `browser_${randomUUID().replaceAll("-", "")}`;
      await manager.create({ workspaceId, browserId: peerId, ownerRevision: 1, url: `http://127.0.0.1:${server.address().port}/` });
      peer = manager.get(peerId);
    }
    const cold = phase !== "executing";
    if (cold) {
      await session.freeze({ allowSoftProtection: true });
      await session.discard({ allowSoftProtection: true });
      assert.equal(session.record.resource_state, "discarded");
      assert.equal(session.context, null);
      blockedNavigation = navigation;
    }
    const checkpointBefore = cold ? await manager.stateStore.readCheckpoint(browserId) : null;
    const stateBefore = await readFile(manager.stateStore.stateFile, "utf8");
    const value = `completed-${phase}`;
    operation = session.runInteractiveOperation({ actor: "user", action: phase }, async () => {
      enteredAction.release();
      if (!cold) await action.promise;
      await session.page.locator("#field").fill(value);
      order.push("operation_done");
    });
    operation.catch(() => undefined);
    await (cold ? navigation.entered.promise : enteredAction.promise);
    const peerEntered = barrier();
    const peerPage = peer?.page;
    if (peer) {
      peerOperation = peer.runInteractiveOperation({ actor: "user", action: "peer_blocked" }, async () => {
        peerEntered.release();
        await action.promise;
        await peer.page.locator("#field").fill("peer-completed");
      });
      peerOperation.catch(() => undefined);
      await peerEntered.promise;
    }
    const context = session.context;
    const page = cold ? context.pages()[0] : session.page;
    assert(context && page, "真实context和页面已经进入操作准备或执行阶段");
    assert.equal(session.pendingOperations, cold ? 1 : 0);
    assert.equal(session.inFlightOperations, cold ? 0 : 1);
    assert.equal(session.operationQueue.acceptedOperationExecutions.size, 1);

    let drainElapsedMs = null;
    if (["prepare_timeout", "manager_timeout", "manager_batch_timeout"].includes(phase)) {
      if (phase !== "prepare_timeout") {
        const started = performance.now();
        closingAttempt = manager.shutdown("integration_timeout");
        closingAttempt.catch(() => undefined);
        if (peer) {
          await new Promise((resolve) => setTimeout(resolve, 30));
          assert.equal(peer.closingRequested, true, "首个资源等待时其它资源也必须关闭准入");
          await assert.rejects(peer.runInteractiveOperation({ actor: "user", action: "peer_late" }, async () => {
            throw new Error("关闭准入后不能执行第二个Browser的新动作");
          }), { code: "browser_closing" });
        }
        await assert.rejects(closingAttempt, (error) => {
          assert(error instanceof AggregateError);
          assert.equal(error.errors.length, peer ? 2 : 1);
          for (const cause of error.errors) assert.equal(cause.code, "browser_shutdown_operation_drain_timeout");
          return true;
        });
        drainElapsedMs = performance.now() - started;
        assert(drainElapsedMs >= 4500 && drainElapsedMs < 8000, `整批排空只使用一次默认5秒预算: ${drainElapsedMs}`);
        assert.equal(manager.runtimePool.browser.isConnected(), true);
        assert.equal(session.record.status, "running");
        if (peer) {
          assert.equal(peer.record.status, "running");
          assert.equal(peer.context.pages()[0], peerPage);
          assert.equal(peerPage.isClosed(), false);
          assert.equal(peer.operationQueue.acceptedOperationExecutions.size, 1);
          assert.equal(peer.inFlightOperations, 1);
        }
      } else {
        await assert.rejects(session.checkpointForManagerShutdown("integration_timeout", { operationDrainTimeoutMs: 20 }),
          { code: "browser_shutdown_operation_drain_timeout" });
      }
      assert.deepEqual(await manager.stateStore.readCheckpoint(browserId), checkpointBefore);
      assert.equal(await readFile(manager.stateStore.stateFile, "utf8"), stateBefore);
      assert.equal(session.operationQueue.acceptedOperationExecutions.size, 1);
      assert.equal(session.pendingOperations, 1);
      assert.equal(page.isClosed(), false);
      assert.equal(session.context, context);
    } else {
      closing = manager.shutdown("integration_shutdown").then(() => { order.push("shutdown_done"); });
      closing.catch(() => undefined);
      await new Promise((resolve) => setTimeout(resolve, 30));
      assert.equal(session.closingRequested, true);
      assert.deepEqual(order, [], "原操作结束前不能关闭或生成新检查点");
      assert.equal(page.isClosed(), false);
      if (cold) assert.deepEqual(await manager.stateStore.readCheckpoint(browserId), checkpointBefore);
    }
    await assert.rejects(session.runInteractiveOperation({ actor: "user", action: "late" }, async () => {
      throw new Error("关闭准入后不应执行新操作");
    }), { code: "browser_closing" });
    navigation.response.release();
    action.release();
    await Promise.all([operation, peerOperation].filter(Boolean));
    if (!closing) closing = manager.shutdown().then(() => { order.push("shutdown_done"); });
    await closing;
    assert.deepEqual(order, ["operation_done", "shutdown_done"]);
    assert.equal(page.isClosed(), true);
    assert.equal(session.context, null);
    assert.equal(manager.runtimePool.browser, null);
    assert.equal(session.operationQueue.acceptedOperationExecutions.size, 0);
    const checkpoint = await manager.stateStore.readCheckpoint(browserId);
    assert.equal(checkpoint.pages[0].controls.find((control) => control.selector === "#field").value, value);
    if (peer) {
      assert.equal(peerPage.isClosed(), true);
      const peerCheckpoint = await manager.stateStore.readCheckpoint(peer.id);
      assert.equal(peerCheckpoint.pages[0].controls.find((control) => control.selector === "#field").value, "peer-completed");
    }

    restored = new BrowserManager({ workspaceRoot, workspaceId });
    await restored.init();
    const recovered = restored.get(browserId);
    assert.equal(recovered.record.resource_state, "discarded");
    assert.equal(recovered.context, null);
    const response = await recovered.runInteractiveOperation({ actor: "user", action: "read_after_restart" },
      async () => ({ value: await recovered.page.locator("#field").inputValue() }));
    assert.equal(response.value, value);
    if (peer) {
      const peerRecovered = restored.get(peer.id);
      const peerResponse = await peerRecovered.runInteractiveOperation({ actor: "user", action: "peer_after_restart" },
        async () => ({ value: await peerRecovered.page.locator("#field").inputValue() }));
      assert.equal(peerResponse.value, "peer-completed");
    }
    return { browserId, peerBrowserId: peer?.id ?? null, order, drainElapsedMs, restoredValue: response.value, originalPageClosed: page.isClosed() };
  } finally {
    blockedNavigation = null;
    navigation.response.release();
    action.release();
    await Promise.allSettled([operation, peerOperation, closingAttempt, closing].filter(Boolean));
    try {
      if (restored) await restored.shutdown();
    } finally {
      await manager.shutdown();
    }
  }
}

async function checkpointCleanupFailure() {
  const workspaceId = randomUUID();
  const workspaceRoot = path.join(run.workspaceRoot, `cleanup-failure-${randomUUID()}`);
  await cp(path.join(run.projectRoot, "tests/fixtures/workspaces/default_test_workspace"), workspaceRoot, { recursive: true });
  await mkdir(path.join(workspaceRoot, ".boxteam"), { recursive: true });
  await writeFile(path.join(workspaceRoot, ".boxteam/workspace-identity.json"), JSON.stringify({ workspace_id: workspaceId }));
  const manager = new BrowserManager({ workspaceRoot, workspaceId });
  const firstId = `browser_${randomUUID().replaceAll("-", "")}`;
  const secondId = `browser_${randomUUID().replaceAll("-", "")}`;
  const conflict = manager.stateStore.checkpointPath(firstId);
  let conflictCreated = false;
  let restored;
  try {
    await manager.init();
    for (const browserId of [firstId, secondId]) {
      await manager.create({ workspaceId, browserId, ownerRevision: 1, url: `http://127.0.0.1:${server.address().port}/` });
    }
    const first = manager.get(firstId);
    const second = manager.get(secondId);
    await second.runInteractiveOperation({ actor: "user", action: "edit_peer" },
      async () => { await second.page.locator("#field").fill("peer-survived-cleanup-failure"); });
    const firstPage = first.page;
    const secondPage = second.page;
    // 原生目录冲突同时使第一个检查点发布和随后的删除失败，不替换产品owner。
    await mkdir(conflict, { recursive: true });
    conflictCreated = true;
    let failure;
    try { await manager.shutdown("integration_cleanup_failure"); } catch (error) { failure = error; }
    assert(failure instanceof AggregateError);
    assert.equal(failure.errors.length, 1);
    const combined = failure.errors[0];
    assert(combined instanceof AggregateError);
    assert.equal(combined.cause, combined.errors[0]);
    assert.equal(combined.errors.length, 2);
    assert.match(combined.errors[0].message, /写入浏览器检查点失败/);
    assert.match(combined.errors[1].message, /删除浏览器检查点失败/);
    assert.equal(first.record.status, "running");
    assert.equal(first.record.reconciliation_status, "unknown");
    assert.equal(firstPage.isClosed(), true);
    assert.equal(secondPage.isClosed(), true, "首个资源收尾失败不能跳过第二个资源");
    assert.equal(second.record.resource_state, "discarded");
    assert.equal(manager.runtimePool.browser.isConnected(), true, "收尾失败不能关闭共享池");
    const stored = JSON.parse(await readFile(manager.stateStore.stateFile, "utf8"));
    assert.equal(stored.browsers.find((record) => record.browser_id === firstId).reconciliation_status, "unknown");
    const peerCheckpoint = await manager.stateStore.readCheckpoint(secondId);
    assert.equal(peerCheckpoint.pages[0].controls.find((control) => control.selector === "#field").value, "peer-survived-cleanup-failure");
    await rmdir(conflict);
    conflictCreated = false;
    await assert.rejects(manager.shutdown("integration_cleanup_retry"), (error) => {
      assert(error instanceof AggregateError);
      assert.equal(error.errors.length, 1);
      return true;
    });
    assert.equal(first.record.status, "lost");
    assert.equal(first.record.checkpoint, null);
    assert.equal(first.context, null);
    assert.equal(manager.runtimePool.browser, null);
    assert.deepEqual(await manager.stateStore.readCheckpoint(secondId), peerCheckpoint);
    await manager.shutdown();
    restored = new BrowserManager({ workspaceRoot, workspaceId });
    await restored.init();
    assert.equal(restored.get(firstId).record.status, "lost");
    const recovered = restored.get(secondId);
    const read = await recovered.runInteractiveOperation({ actor: "user", action: "read_peer_after_restart" },
      async () => ({ value: await recovered.page.locator("#field").inputValue() }));
    assert.equal(read.value, "peer-survived-cleanup-failure");
    return { firstId, secondId, failureCount: combined.errors.length, peerRestoredValue: read.value };
  } finally {
    if (conflictCreated) await rmdir(conflict);
    try { if (restored) await restored.shutdown(); } finally { await manager.shutdown(); }
  }
}

try {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  run.addCleanup("页面HTTP服务", () => new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    server.closeAllConnections();
  }));
  for (const phase of ["prepare", "executing", "prepare_timeout", "manager_timeout", "manager_batch_timeout"]) {
    try {
      results.push({ phase, status: "passed", observations: await scenario(phase) });
    } catch (error) {
      results.push({ phase, status: "failed", error: error.stack ?? String(error) });
    }
  }
  try {
    results.push({ phase: "checkpoint_cleanup_failure", status: "passed", observations: await checkpointCleanupFailure() });
  } catch (error) {
    results.push({ phase: "checkpoint_cleanup_failure", status: "failed", error: error.stack ?? String(error) });
  }
  const failed = results.filter((result) => result.status === "failed");
  await run.writeResult(failed.length ? "failed" : "passed", {
    scenarios: results,
    scope: "真实BrowserManager/BrowserSession、Chromium、HTTP页面、磁盘检查点和冷Manager恢复；受控HTTP响应及操作屏障，不含Gateway或主Web",
  });
  console.log(JSON.stringify({ scenarios: results, artifacts: run.artifactsDir }, null, 2));
  assert.equal(failed.length, 0, "Browser关闭操作集成失败");
} finally {
  await run.close();
}
