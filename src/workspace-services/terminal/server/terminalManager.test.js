import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE,
  MAX_RETAINED_TERMINAL_HISTORY,
  TerminalManager,
} from "./terminalManager.js";

test("工作区达到 64 个活动执行时淘汰最近 8 个之外最久未使用项", async () => {
  const manager = new TerminalManager({
    workspaceRoot: process.cwd(),
    workspaceId: "gw_terminal_limit_test",
  });
  const terminated = [];
  for (let index = 0; index < MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE; index += 1) {
    const id = `term_${String(index).padStart(2, "0")}`;
    manager.sessions.set(id, {
      id,
      status: "running",
      lastCommandStatus: "running",
      lastUsedAt: new Date(index * 1_000).toISOString(),
      async terminateForRelease(options) {
        this.status = options.status;
        terminated.push({ id, ...options });
      },
      toRecord() {
        return { terminal_id: id, status: this.status };
      },
    });
  }

  await manager.ensureExecutionCapacity();

  assert.deepEqual(terminated, [{
    id: "term_00",
    status: "terminated",
    commandStatus: "terminated",
    reason: "workspace_lru_eviction",
  }]);
});

test("已完成命令不占用工作区活动执行上限", async () => {
  const manager = new TerminalManager({
    workspaceRoot: process.cwd(),
    workspaceId: "gw_terminal_completed_test",
  });
  for (let index = 0; index < MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE; index += 1) {
    manager.sessions.set(`term_${index}`, {
      status: "running",
      lastCommandStatus: "completed",
      lastUsedAt: new Date(index * 1_000).toISOString(),
    });
  }

  await manager.ensureExecutionCapacity();
});

test("终端管理器只保留有界的近期终态历史", async () => {
  const manager = new TerminalManager({
    workspaceRoot: process.cwd(),
    workspaceId: "gw_terminal_history_test",
  });
  const disposed = [];
  for (let index = 0; index < MAX_RETAINED_TERMINAL_HISTORY + 2; index += 1) {
    const id = `term_${index}`;
    manager.sessions.set(id, {
      id,
      status: "deleted",
      updatedAt: new Date(index * 1_000).toISOString(),
      async dispose() {
        disposed.push(id);
      },
      toRecord() {
        return { terminal_id: id, status: this.status };
      },
    });
  }

  await manager.pruneTerminalHistory();

  assert.equal(manager.sessions.size, MAX_RETAINED_TERMINAL_HISTORY);
  assert.deepEqual(disposed, ["term_1", "term_0"]);
});

test("完成 steering 返回统一的终端信封", async () => {
  const manager = new TerminalManager({
    workspaceRoot: process.cwd(),
    workspaceId: "gw_terminal_steering_test",
  });
  const snapshot = {
    terminal_id: "term_steering",
    session_id: "session_steering",
    status: "completed",
  };
  const session = {
    finishSteering(options) {
      assert.deepEqual(options, { dispatched: true });
    },
    snapshot() {
      return snapshot;
    },
    toRecord() {
      return snapshot;
    },
  };
  manager.sessions.set("term_steering", session);

  const result = await manager.finishSteering("term_steering", {
    dispatched: true,
  });

  assert.deepEqual(result, { terminal: snapshot });
});
