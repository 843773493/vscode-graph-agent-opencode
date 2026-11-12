import { describe, expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import { metadataPath } from "./worktree/repository.mjs";
import { main as workspaceMain, resolveWorkspaceTarget } from "./workspace.mjs";
import { assertSystemdUnitIdle } from "./worktree/lifecycle.mjs";
import { readUnitState } from "./dev-systemd.mjs";

const outputRoot = path.resolve("out/tests/scripts/launch/workspace");
const runId = randomUUID();
const fixtureRoot = path.join(outputRoot, "workspace", runId);
const artifacts = path.join(outputRoot, "artifacts", runId);
mkdirSync(fixtureRoot, { recursive: true });
mkdirSync(artifacts, { recursive: true });

function git(cwd, ...args) {
  const environment = { ...process.env };
  for (const name of ["GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"]) delete environment[name];
  if (args[0] === "commit") {
    environment.GIT_AUTHOR_NAME = "Workspace Fixture";
    environment.GIT_AUTHOR_EMAIL = "workspace-fixture@example.invalid";
    environment.GIT_COMMITTER_NAME = "Workspace Fixture";
    environment.GIT_COMMITTER_EMAIL = "workspace-fixture@example.invalid";
  }
  const result = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8", env: environment });
  if (result.error || (result.status !== 0 && !(args[0] === "commit" && result.status === 1)) || result.signal) {
    throw new Error(`git ${args.join(" ")} failed: status=${result.status} signal=${result.signal} stderr=${result.stderr} error=${result.error}`);
  }
}

function createRepository(role = "dev") {
  const root = path.join(fixtureRoot, randomUUID(), "primary");
  mkdirSync(root, { recursive: true });
  git(root, "init", "-q");
  git(root, "config", "user.name", "Workspace Fixture");
  git(root, "config", "user.email", "workspace-fixture@example.invalid");
  writeFileSync(path.join(root, "README.md"), "fixture\n");
  git(root, "add", "-f", "README.md");
  git(root, "commit", "-qm", "fixture");
  const dev = role === "dev" ? path.join(root, "out/worktrees/dev") : path.join(root, "out/worktrees/2026/11/05/experiment-case");
  mkdirSync(path.dirname(dev), { recursive: true });
  git(root, "worktree", "add", "-q", "-b", `fixture-${role}`, dev, "HEAD");
  const metadata = { schema_version: 1, role, primary_root: root, port_offset: role === "dev" ? 100 : 1000,
    base_commit: "fixture", created_at: "2026-11-05T00:00:00Z" };
  writeFileSync(metadataPath(dev), `${JSON.stringify(metadata)}\n`);
  return { root, dev };
}

describe("统一开发实例路由", () => {
  test("root 强制绑定主 worktree，dev 固定绑定 checkout/Home/offset", () => {
    const { root, dev } = createRepository();
    const rootTarget = resolveWorkspaceTarget({ target: "root", cwd: dev, environment: {} });
    const rootOffsetTarget = resolveWorkspaceTarget({ target: "root", cwd: dev,
      environment: { BOXTEAM_DEV_PORT_OFFSET: "16" } });
    const devTarget = resolveWorkspaceTarget({ target: "dev", cwd: dev, environment: {} });
    expect(rootTarget.checkout).toBe(root);
    expect(rootTarget.environment.BOXTEAM_PROJECT_ROOT).toBe(root);
    expect(rootOffsetTarget.portOffset).toBe(16);
    expect(rootOffsetTarget.environment.BOXTEAM_DEV_PORT_OFFSET).toBe("16");
    expect(devTarget.checkout).toBe(dev);
    expect(devTarget.environment.BOXTEAM_PROJECT_ROOT).toBe(dev);
    expect(devTarget.environment.BOXTEAM_FRONTEND_ROOT).toBe(root);
    expect(devTarget.environment.BOXTEAM_HOME).toBe(path.join(dev, "out/development-runtime/boxteam-home"));
    expect(devTarget.environment.BOXTEAM_DEV_PORT_OFFSET).toBe("100");
    writeFileSync(path.join(artifacts, "route.json"), `${JSON.stringify({ root: rootTarget.checkout,
      dev: devTarget.checkout, home: devTarget.environment.BOXTEAM_HOME, offset: devTarget.portOffset }, null, 2)}\n`);
  });

  test("固定 dev 拒绝外部 Home 与 workspace，并拒绝错误角色 metadata", () => {
    const { dev } = createRepository();
    expect(() => resolveWorkspaceTarget({ target: "dev", cwd: dev,
      environment: { BOXTEAM_HOME: "/tmp/foreign-home" } })).toThrow("拒绝外部 BOXTEAM_HOME");
    expect(() => resolveWorkspaceTarget({ target: "dev", cwd: dev,
      environment: { BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT: "/tmp/foreign-workspace" } })).toThrow("拒绝外部 BOXTEAM_DEFAULT_USER_WORKSPACE_ROOT");
    expect(() => resolveWorkspaceTarget({ target: "root", cwd: dev,
      environment: { BOXTEAM_HOME: "/tmp/foreign-home" } })).toThrow("拒绝外部 BOXTEAM_HOME");
  });

  test("experiment 默认 Home 隔离，外部路径只能通过 --home 显式给出", () => {
    const { root, dev } = createRepository("experiment");
    const isolated = resolveWorkspaceTarget({ target: "experiment", experimentPath: dev, cwd: root, environment: {} });
    expect(isolated.environment.BOXTEAM_HOME).toBe(path.join(dev, "out/development-runtime/boxteam-home"));
    expect(() => resolveWorkspaceTarget({ target: "experiment", experimentPath: dev, cwd: root,
      environment: { BOXTEAM_HOME: "/tmp/root-home" } })).toThrow("拒绝外部 BOXTEAM_HOME");
    const explicit = resolveWorkspaceTarget({ target: "experiment", experimentPath: dev, homeOverride: "/tmp/explicit-experiment-home",
      cwd: root, environment: {} });
    expect(explicit.environment.BOXTEAM_HOME).toBe("/tmp/explicit-experiment-home");
  });

  test("实际 workspace CLI 用根启动器连接 dev 后端，status 与 start 选择同一布局", () => {
    const { root, dev } = createRepository();
    let observed;
    const result = workspaceMain(["dev", "status"], dev, {}, (command, args, options) => {
      observed = { command, args, cwd: options.cwd, environment: options.env };
      return { status: 0, signal: null };
    });
    expect(observed.args).toEqual([path.join(root, "scripts/launch/dev-manager.mjs"), "status"]);
    expect(observed.cwd).toBe(root);
    expect(observed.environment.BOXTEAM_PROJECT_ROOT).toBe(dev);
    expect(observed.environment.BOXTEAM_FRONTEND_ROOT).toBe(root);
    expect(observed.environment.BOXTEAM_HOME).toBe(path.join(dev, "out/development-runtime/boxteam-home"));
    expect(result.exitCode).toBe(0);
    writeFileSync(path.join(artifacts, "cli-route.json"), `${JSON.stringify({ command: observed.command,
      args: observed.args, cwd: observed.cwd, home: observed.environment.BOXTEAM_HOME }, null, 2)}\n`);
  });

  test("显式后端源码可覆盖默认 dev，并将 Home 绑定到所选后端", () => {
    const { root, dev } = createRepository();
    let observed;
    const selected = workspaceMain(["dev", "start", "--backend-root", dev], root,
      { BOXTEAM_DEV_PORT_OFFSET: "200" }, (command, args, options) => {
        observed = options;
        return { status: 0, signal: null };
      });
    expect(selected.checkout).toBe(dev);
    expect(observed.env.BOXTEAM_FRONTEND_ROOT).toBe(root);
    expect(observed.env.BOXTEAM_PROJECT_ROOT).toBe(dev);
    expect(observed.env.BOXTEAM_HOME).toBe(path.join(dev, "out/development-runtime/boxteam-home"));
    expect(observed.env.BOXTEAM_DEV_PORT_OFFSET).toBe("200");
    expect(() => workspaceMain(["dev", "start", "--backend-root="], root, {})).toThrow("非空源码路径");
  });

  test("experiment 路由要求受管 checkout 与 experiment 角色", () => {
    const { root, dev } = createRepository();
    expect(() => resolveWorkspaceTarget({ target: "experiment", experimentPath: dev, cwd: root,
      environment: {} })).toThrow("不是实验 worktree");
  });

  test("归档 metadata 的实验 checkout 拒绝启动", () => {
    const { root, dev } = createRepository("experiment");
    const metadata = JSON.parse(readFileSync(metadataPath(dev), "utf8"));
    writeFileSync(metadataPath(dev), `${JSON.stringify({ ...metadata, archived: true })}\n`);
    expect(() => resolveWorkspaceTarget({ target: "experiment", experimentPath: dev, cwd: root,
      environment: {} })).toThrow("归档 worktree 不可启动");
  });

  test("归档复用 systemd 状态解析器并 fail-closed", () => {
    const { dev } = createRepository();
    const queries = [];
    const stateReader = (unit) => {
      queries.push(unit);
      return { loadState: "loaded", activeState: "inactive", subState: "dead" };
    };
    const observed = assertSystemdUnitIdle(dev, { port_offset: 100 }, { readState: stateReader });
    expect(observed.unit).toMatch(/^boxteam-dev-[0-9a-f]{16}$/);
    expect(observed.state.activeState).toBe("inactive");
    expect(queries).toEqual([observed.unit]);
    for (const activeState of ["active", "activating", "deactivating", "unknown"]) {
      expect(() => assertSystemdUnitIdle(dev, { port_offset: 100 }, {
        readState: () => ({ loadState: "loaded", activeState, subState: "fixture" }),
      })).toThrow("活动或状态未知");
    }
    expect(assertSystemdUnitIdle(dev, { port_offset: 100 }, {
      readState: () => ({ loadState: "loaded", activeState: "failed", subState: "failed" }),
    }).state.activeState).toBe("failed");
    expect(assertSystemdUnitIdle(dev, { port_offset: 100 }, {
      readState: () => ({ loadState: "not-found", activeState: "inactive", subState: "dead" }),
    }).state.loadState).toBe("not-found");
    expect(() => assertSystemdUnitIdle(dev, { port_offset: 100 }, {
      readState: () => ({ loadState: "unknown", activeState: "inactive", subState: "unknown" }),
    })).toThrow("活动或状态未知");
  });

  test("readUnitState 只对明确 not-found 返回缺省状态，保留 systemctl 失败", () => {
    const commands = [];
    const state = readUnitState("boxteam-dev-fixture", (command, args, options) => {
      commands.push({ command, args, options });
      return { exitCode: 1, signalCode: null, stdout: "LoadState=not-found\n", stderr: "" };
    });
    expect(state).toEqual({ loadState: "not-found", activeState: "inactive", subState: "dead" });
    expect(commands[0].command).toBe("systemctl");
    expect(commands[0].args).toContain("show");
    expect(commands[0].args).toContain("boxteam-dev-fixture");
    expect(() => readUnitState("boxteam-dev-fixture", () => ({
      exitCode: 1, signalCode: null, stdout: "", stderr: "fixture dbus unavailable",
    }))).toThrow("fixture dbus unavailable");
  });
});
