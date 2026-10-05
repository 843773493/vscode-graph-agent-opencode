import assert from "node:assert/strict";
import path from "node:path";
import { mkdirSync, symlinkSync, writeFileSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import test from "node:test";

import {
  MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE,
  MAX_RETAINED_TERMINAL_HISTORY,
  TerminalManager,
  matchesTerminalProcessIdentity,
  validateWorkspaceId,
  verifyWorkspaceIdentity,
} from "./terminalManager.js";
import { resolveTerminalCwd, TerminalSession, validateTerminalRecord } from "./terminalSession.js";

function probeWorkspaceRoot(name) {
  const root = path.join(
    process.cwd(),
    "out",
    "tests",
    "src",
    "workspace-services",
    "terminal",
    "server",
    "terminalManager",
    "workspace",
    name,
  );
  mkdirSync(root, { recursive: true });
  mkdirSync(path.join(root, ".boxteam"), { recursive: true });
  writeFileSync(path.join(root, ".boxteam", "workspace-identity.json"), JSON.stringify({
    workspace_id: "00000000-0000-4000-8000-000000000001",
  }));
  return root;
}

test("终端的持久相对路径覆盖相对子目录、越界绝对路径、工作区移动与符号链接四类场景", () => {
  const root = probeWorkspaceRoot(randomUUID());
  mkdirSync(path.join(root, "sub", "deep"), { recursive: true });
  symlinkSync(path.join(root, "sub"), path.join(root, "link"));

  // 用户传入相对子目录：落盘相对路径，restore 还原到同一绝对目录。
  const relativeCwd = resolveTerminalCwd(root, "sub/deep");
  assert.equal(relativeCwd, path.join(root, "sub", "deep"));
  assert.equal(resolveTerminalCwd(root, path.relative(root, relativeCwd)), relativeCwd);

  // 用户传入越界绝对路径：保留原语义（不施加 workspace 边界），且落盘为相对表达。
  const outside = resolveTerminalCwd(root, "/etc");
  assert.equal(outside, "/etc");
  assert.equal(resolveTerminalCwd(root, path.relative(root, outside)), "/etc");

  // 工作区被移动：相对路径按新根重新推导，不依赖旧绝对路径。
  const movedRoot = probeWorkspaceRoot(randomUUID());
  mkdirSync(path.join(movedRoot, "sub", "deep"), { recursive: true });
  assert.equal(
    resolveTerminalCwd(movedRoot, path.relative(root, relativeCwd)),
    path.join(movedRoot, "sub", "deep"),
  );

  // 符号链接：与改造前一致，path.resolve 只做规范化、不 realpath，链接段原样保留。
  assert.equal(resolveTerminalCwd(root, "link/deep"), path.join(root, "link", "deep"));

  // 不存在的目录显式失败，绝不静默回退进程 cwd。
  assert.throws(
    () => resolveTerminalCwd(root, "sub/missing"),
    /终端工作目录不存在或不是目录/,
  );
});

test("Terminal owner 拒绝旧记录格式且不迁移或覆盖原始数据", async () => {
  const root = probeWorkspaceRoot(randomUUID());
  const workspaceId = "00000000-0000-4000-8000-000000000001";
  const stateFile = path.join(root, ".boxteam", "terminal-manager", "terminals.json");
  mkdirSync(path.dirname(stateFile), { recursive: true });
  const original = `${JSON.stringify({
    workspace_id: workspaceId,
    terminals: [{ terminal_id: "term_legacy", cwd: "/old/workspace" }],
  })}\n`;
  await writeFile(stateFile, original, "utf8");

  const manager = new TerminalManager({ workspaceRoot: root, workspaceId });
  await assert.rejects(manager.init(), /终端状态文件版本不受支持/);
  assert.equal(await readFile(stateFile, "utf8"), original);
});

test("重启只接受完全匹配的 PID、进程组、会话和启动时间", () => {
  const record = {
    os_pid: 123,
    process_group_id: 456,
    process_session_id: 789,
    process_start_time: 1000,
  };
  const stat = {
    pid: 123,
    processGroupId: 456,
    processSessionId: 789,
    processStartTime: 1000,
  };

  assert.equal(matchesTerminalProcessIdentity(record, stat), true);
  assert.equal(matchesTerminalProcessIdentity(record, { ...stat, processStartTime: 1001 }), false);
  assert.equal(matchesTerminalProcessIdentity(record, null), false);
});

test("工作区达到 64 个 PTY 时拒绝创建且不隐式终止任何资源", async () => {
  const manager = new TerminalManager({
    workspaceRoot: probeWorkspaceRoot(randomUUID()),
    workspaceId: "00000000-0000-4000-8000-000000000001",
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

  await assert.rejects(manager.ensureExecutionCapacity(), /工作区终端进程已达到上限/);
  assert.deepEqual(terminated, []);
  assert.equal(manager.sessions.size, MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE);
});

test("已完成命令的存活 shell 仍占用 PTY 上限", async () => {
  const manager = new TerminalManager({
    workspaceRoot: probeWorkspaceRoot(randomUUID()),
    workspaceId: "00000000-0000-4000-8000-000000000001",
  });
  for (let index = 0; index < MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE; index += 1) {
    manager.sessions.set(`term_${index}`, {
      status: "running",
      lastCommandStatus: "completed",
      lastUsedAt: new Date(index * 1_000).toISOString(),
    });
  }

  await assert.rejects(manager.ensureExecutionCapacity(), /工作区终端进程已达到上限/);
});

test("终端管理器只保留有界的近期终态历史", async () => {
  const manager = new TerminalManager({
    workspaceRoot: probeWorkspaceRoot(randomUUID()),
    workspaceId: "00000000-0000-4000-8000-000000000001",
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
    workspaceRoot: probeWorkspaceRoot(randomUUID()),
    workspaceId: "00000000-0000-4000-8000-000000000001",
  });
  const snapshot = {
    terminal_id: "term_steering",
    transport_owner_revision: 1,
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


test("Terminal 显式 UUID 必填且与权威身份文件保持一致", async () => {
  for (const workspaceId of [undefined, "", "gw_legacy", "not-a-uuid"]) {
    assert.throws(() => validateWorkspaceId(workspaceId), /标准 UUID/);
  }
  const root = probeWorkspaceRoot(randomUUID());
  await verifyWorkspaceIdentity(root, "00000000-0000-4000-8000-000000000001");
  await assert.rejects(verifyWorkspaceIdentity(root, "00000000-0000-4000-8000-000000000002"), /权威身份文件不一致/);
  const manager = new TerminalManager({ workspaceRoot: root, workspaceId: "00000000-0000-4000-8000-000000000001" });
  await assert.rejects(manager.create({ workspaceId: "00000000-0000-4000-8000-000000000002" }), /身份不匹配/);
});

test("Terminal 持久记录拒绝另一工作区 owner 与绝对 cwd_relative", () => {
  const workspaceId = "00000000-0000-4000-8000-000000000001";
  assert.throws(() => validateTerminalRecord({ terminal_id: "term_wrong", workspace_id: "00000000-0000-4000-8000-000000000002" }, workspaceId), /工作区身份/);
  for (const cwd of ["/old/workspace", "C:\\old\\workspace"]) {
    assert.throws(() => validateTerminalRecord({ terminal_id: "term_absolute", workspace_id: workspaceId, cwd_relative: cwd }, workspaceId), /cwd_relative/);
  }
});

test("停止但未删除的 owner 资源不会被历史裁剪", async () => {
  const manager = new TerminalManager({ workspaceRoot: probeWorkspaceRoot(randomUUID()), workspaceId: "00000000-0000-4000-8000-000000000001" });
  for (let index = 0; index < MAX_RETAINED_TERMINAL_HISTORY + 10; index += 1) {
    const id = `term_stopped_${index}`;
    manager.sessions.set(id, { id, status: "terminated", toRecord: () => ({ terminal_id: id }) });
  }
  await manager.pruneTerminalHistory();
  await manager.ensureExecutionCapacity();
  assert.equal(manager.sessions.size, MAX_RETAINED_TERMINAL_HISTORY + 10);
});

test("grant 一次消费，过期或 owner 版本变化立即失效", async () => {
  const workspaceId = "00000000-0000-4000-8000-000000000001";
  const manager = new TerminalManager({ workspaceRoot: probeWorkspaceRoot(randomUUID()), workspaceId });
  const id = "term_00000000000070008000000000000001";
  const terminal = new TerminalSession({ manager, record: {
    terminal_id: id, workspace_id: workspaceId, transport_owner_revision: 1,
    reconciliation_status: "verified", cwd_relative: ".", status: "running",
  } });
  manager.sessions.set(id, terminal);
  const first = manager.issueTransportGrant(id, { ownerRevision: 1, association: "opaque-owner-ref" });
  assert.equal(manager.consumeTransportGrant(id, first.grant_token).association, "opaque-owner-ref");
  assert.throws(() => manager.consumeTransportGrant(id, first.grant_token), /已消费/);
  const wrongIdentity = manager.issueTransportGrant(id, { ownerRevision: 1, association: null });
  assert.throws(() => manager.consumeTransportGrant("other", wrongIdentity.grant_token), /无效/);
  assert.throws(() => manager.consumeTransportGrant(id, wrongIdentity.grant_token), /已消费/);
  const expired = manager.issueTransportGrant(id, { ownerRevision: 1, association: null });
  manager.transportGrants.get(expired.grant_token).expiresAt = Date.now() - 1;
  assert.throws(() => manager.consumeTransportGrant(id, expired.grant_token), /过期/);
  const old = manager.issueTransportGrant(id, { ownerRevision: 1, association: null });
  await manager.syncOwnerRevision(id, 2);
  assert.throws(() => manager.consumeTransportGrant(id, old.grant_token), /无效/);
  assert.throws(() => manager.issueTransportGrant(id, { ownerRevision: 1, association: null }), /revision 冲突/);
});
