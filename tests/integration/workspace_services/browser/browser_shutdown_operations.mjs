import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
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
  let closing;
  let restored;
  const order = [];
  try {
    await manager.init();
    await manager.create({ workspaceId, browserId, ownerRevision: 1, url: `http://127.0.0.1:${server.address().port}/` });
    const session = manager.get(browserId);
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
    const context = session.context;
    const page = cold ? context.pages()[0] : session.page;
    assert(context && page, "真实context和页面已经进入操作准备或执行阶段");
    assert.equal(session.pendingOperations, cold ? 1 : 0);
    assert.equal(session.inFlightOperations, cold ? 0 : 1);
    assert.equal(session.operationQueue.acceptedOperationExecutions.size, 1);

    if (phase === "prepare_timeout") {
      await assert.rejects(session.checkpointForManagerShutdown("integration_timeout", { operationDrainTimeoutMs: 20 }),
        { code: "browser_shutdown_operation_drain_timeout" });
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
    await operation;
    if (!closing) closing = manager.shutdown().then(() => { order.push("shutdown_done"); });
    await closing;
    assert.deepEqual(order, ["operation_done", "shutdown_done"]);
    assert.equal(page.isClosed(), true);
    assert.equal(session.context, null);
    assert.equal(manager.runtimePool.browser, null);
    assert.equal(session.operationQueue.acceptedOperationExecutions.size, 0);
    const checkpoint = await manager.stateStore.readCheckpoint(browserId);
    assert.equal(checkpoint.pages[0].controls.find((control) => control.selector === "#field").value, value);

    restored = new BrowserManager({ workspaceRoot, workspaceId });
    await restored.init();
    const recovered = restored.get(browserId);
    assert.equal(recovered.record.resource_state, "discarded");
    assert.equal(recovered.context, null);
    const response = await recovered.runInteractiveOperation({ actor: "user", action: "read_after_restart" },
      async () => ({ value: await recovered.page.locator("#field").inputValue() }));
    assert.equal(response.value, value);
    return { browserId, order, restoredValue: response.value, originalPageClosed: page.isClosed() };
  } finally {
    blockedNavigation = null;
    navigation.response.release();
    action.release();
    await Promise.allSettled([operation, closing].filter(Boolean));
    try {
      if (restored) await restored.shutdown();
    } finally {
      await manager.shutdown();
    }
  }
}

try {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  run.addCleanup("页面HTTP服务", () => new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    server.closeAllConnections();
  }));
  for (const phase of ["prepare", "executing", "prepare_timeout"]) {
    try {
      results.push({ phase, status: "passed", observations: await scenario(phase) });
    } catch (error) {
      results.push({ phase, status: "failed", error: error.stack ?? String(error) });
    }
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
