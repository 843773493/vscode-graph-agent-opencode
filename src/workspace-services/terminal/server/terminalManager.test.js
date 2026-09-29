import assert from "node:assert/strict";
import path from "node:path";
import { mkdirSync, symlinkSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import test from "node:test";

import {
  MAX_ACTIVE_EXECUTIONS_PER_WORKSPACE,
  MAX_RETAINED_TERMINAL_HISTORY,
  TerminalManager,
} from "./terminalManager.js";
import { deriveLegacyCwdRelative, resolveTerminalCwd } from "./terminalSession.js";

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

// 一次性迁移：旧记录只有绝对 cwd，须在 load 时由工作区根推导出 cwd_relative 后
// 物理写回，旧记录不得让 init 崩溃；推导失败必须 fail-closed 且带 terminal_id。
test("旧格式终端记录在 init 时迁移为相对路径并物理写回", async () => {
  const root = probeWorkspaceRoot(randomUUID());
  mkdirSync(path.join(root, "sub", "deep"), { recursive: true });
  const outsideRoot = probeWorkspaceRoot(randomUUID());
  const workspaceId = "gw_terminal_migration_test";
  const writeState = async (terminals) => {
    await writeFile(
      path.join(root, ".boxteam", "terminal-manager", "terminals.json"),
      `${JSON.stringify({ workspace_id: workspaceId, terminals }, null, 2)}\n`,
      "utf8",
    );
  };
  mkdirSync(path.join(root, ".boxteam", "terminal-manager"), { recursive: true });

  await writeState([
    {
      terminal_id: "term_legacy_in",
      workspace_id: workspaceId,
      session_id: "ses_legacy",
      cwd: path.join(root, "sub", "deep"),
      status: "deleted",
    },
    {
      terminal_id: "term_legacy_out",
      workspace_id: workspaceId,
      session_id: "ses_legacy",
      cwd: outsideRoot,
      status: "lost",
    },
  ]);

  const manager = new TerminalManager({ workspaceRoot: root, workspaceId });
  await manager.init();
  assert.equal(manager.sessions.get("term_legacy_in").cwd, path.join(root, "sub", "deep"));
  assert.equal(manager.sessions.get("term_legacy_out").cwd, outsideRoot);

  const persisted = JSON.parse(
    await readFile(path.join(root, ".boxteam", "terminal-manager", "terminals.json"), "utf8"),
  );
  assert.equal(persisted.terminals.every((record) => !("cwd" in record)), true);
  assert.equal(
    persisted.terminals.find((record) => record.terminal_id === "term_legacy_in").cwd_relative,
    path.join("sub", "deep"),
  );
  assert.equal("workspace_root" in persisted, false);

  // 幂等：二次 init 不改变相对路径，也不重新迁移。
  const manager2 = new TerminalManager({ workspaceRoot: root, workspaceId });
  await manager2.init();
  await manager2.persist();
  const persisted2 = JSON.parse(
    await readFile(path.join(root, ".boxteam", "terminal-manager", "terminals.json"), "utf8"),
  );
  assert.deepEqual(
    persisted2.terminals.map((record) => record.cwd_relative).sort(),
    persisted.terminals.map((record) => record.cwd_relative).sort(),
  );
});

test("旧格式终端记录迁移对不可推导 cwd 显式失败", async () => {
  const root = probeWorkspaceRoot(randomUUID());

  assert.throws(
    () => deriveLegacyCwdRelative(root, "/nonexistent/absolute/path", "term_bad"),
    /终端记录 cwd 不存在或不是目录.*terminal_id=term_bad/,
  );
  assert.throws(
    () => deriveLegacyCwdRelative(root, undefined, "term_non_string"),
    /终端记录 cwd 缺失或非字符串.*terminal_id=term_non_string/,
  );
  assert.throws(
    () => deriveLegacyCwdRelative(root, "relative/path", "term_relative"),
    /终端记录 cwd 不是绝对路径.*terminal_id=term_relative/,
  );
});

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
