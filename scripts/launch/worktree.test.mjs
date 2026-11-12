import { afterAll, expect, test } from "bun:test";
import { chmodSync, copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { main } from "./worktree.mjs";
import { git, gitText, readMetadata, repositoryContext } from "./worktree/repository.mjs";
import { setupWorktree } from "./worktree/configuration.mjs";
import { main as releasePolicy } from "../release/policy.mjs";
import { assertReleaseSource } from "../release/source-policy.mjs";
import { archiveWorktree } from "./worktree/lifecycle.mjs";

const project = process.cwd();
const runId = randomUUID();
const output = path.join(project, "out/tests/scripts/launch/worktree");
const workspace = path.join(output, "workspace", runId);
const artifacts = path.join(output, "artifacts", runId);
mkdirSync(workspace, { recursive: true });
mkdirSync(artifacts, { recursive: true });
const observations = [];
afterAll(() => writeFileSync(path.join(artifacts, "observations.json"), JSON.stringify(observations, null, 2)));

function fixture(name) {
  const root = path.join(workspace, name);
  mkdirSync(path.join(root, "configs"), { recursive: true });
  git(root, ["init", "-q"]);
  git(root, ["config", "user.email", "fixture@example.invalid"]);
  git(root, ["config", "user.name", "Fixture"]);
  for (const file of ["worktree_inline.jsonc", "worktree_schema.jsonc"]) copyFileSync(path.join(project, "configs", file), path.join(root, "configs", file));
  writeFileSync(path.join(root, ".gitignore"), "out/\n.env*\n*_local.jsonc\n");
  writeFileSync(path.join(root, "tracked.txt"), "base\n");
  writeFileSync(path.join(root, "delete.txt"), "delete\n");
  writeFileSync(path.join(root, "package.json"), '{"name":"fixture","version":"0.0.2"}\n');
  git(root, ["add", "."]); git(root, ["commit", "-qm", "initial"]);
  return root;
}

test("真实 Git CLI 创建、配置复制、增量同步和冲突保护保留源 index", async () => {
  const root = fixture("sync");
  writeFileSync(path.join(root, ".env"), "PRIVATE_TOKEN=fixture-do-not-log\n");
  writeFileSync(path.join(root, "configs/worktree_local.jsonc"), '{"runtime":{"worktrees":{"setup_scopes":[]}}}');
  mkdirSync(path.join(root, "out/development-runtime/boxteam-home"), { recursive: true });
  writeFileSync(path.join(root, "out/development-runtime/boxteam-home/private"), "not copied");
  const result = await main(["create", "dev", "HEAD", "--no-setup"], path.join(root, "configs"));
  const target = result.target;
  expect(readFileSync(path.join(target, ".env"), "utf8")).toContain("PRIVATE_TOKEN");
  expect(JSON.stringify(result)).not.toContain("fixture-do-not-log");
  expect(existsSync(path.join(target, "out/development-runtime/boxteam-home/private"))).toBe(false);
  writeFileSync(path.join(root, "tracked.txt"), "staged\n");
  git(root, ["add", "tracked.txt"]);
  writeFileSync(path.join(root, "tracked.txt"), "unstaged final\n");
  writeFileSync(path.join(root, "new.bin"), Buffer.from([0, 255, 1, 2]));
  rmSync(path.join(root, "delete.txt"));
  const index = readFileSync(path.join(root, ".git/index"));
  const first = await main(["sync", "dev", "tracked.txt", "new.bin", "delete.txt"], root);
  expect(first.changed).toBe(true);
  expect(readFileSync(path.join(target, "tracked.txt"), "utf8")).toBe("unstaged final\n");
  expect(readFileSync(path.join(target, "new.bin"))).toEqual(Buffer.from([0, 255, 1, 2]));
  expect(existsSync(path.join(target, "delete.txt"))).toBe(false);
  expect(readFileSync(path.join(root, ".git/index"))).toEqual(index);
  expect((await main(["sync", "dev", "tracked.txt", "new.bin", "delete.txt"], root)).changed).toBe(false);
  if (process.platform !== "win32") {
    chmodSync(path.join(root, "tracked.txt"), 0o755);
    await main(["sync", "dev", "tracked.txt"], root);
    expect(statSync(path.join(target, "tracked.txt")).mode & 0o100).toBe(0o100);
  }
  writeFileSync(path.join(target, "unrelated.txt"), "another agent\n");
  writeFileSync(path.join(root, "tracked.txt"), "next\n");
  await main(["sync", "dev", "tracked.txt"], root);
  expect(readFileSync(path.join(target, "unrelated.txt"), "utf8")).toBe("another agent\n");
  writeFileSync(path.join(target, "tracked.txt"), "conflict\n");
  writeFileSync(path.join(root, "new.txt"), "atomic check\n");
  await expect(main(["sync", "dev", "new.txt", "tracked.txt"], root)).rejects.toThrow("重叠修改");
  expect(existsSync(path.join(target, "new.txt"))).toBe(false);
  await expect(main(["sync", "dev", ".env"], root)).rejects.toThrow("忽略文件");
  const experiment = await main(["create", "experiment", "HEAD", "nested", "--no-setup"], target);
  expect(experiment.target).toStartWith(path.join(root, "out/worktrees"));
  expect(repositoryContext(experiment.target).root).toBe(root);
  expect(experiment.port_offset).not.toBe(result.port_offset);
  observations.push({ name: "sync", first, experiment, sourceIndexPreserved: true });
}, 30000);

test("创建互斥、失败初始化可重试、setup 顺序及未知范围拒绝", async () => {
  const root = fixture("locking");
  const results = await Promise.allSettled([main(["create", "dev", "HEAD", "--no-setup"], root), main(["create", "dev", "HEAD", "--no-setup"], root)]);
  expect(results.filter((result) => result.status === "fulfilled")).toHaveLength(1);
  expect(results.filter((result) => result.status === "rejected")).toHaveLength(1);
  const calls = [];
  setupWorktree(root, ["python", "root", "web", "workbench"], (command, args) => { calls.push([command, args]); return { status: 0 }; });
  expect(calls.map(([command]) => command)).toEqual(["git", "uv", "bun", "bun", "bun"]);
  expect(() => setupWorktree(root, ["python"], () => ({ status: 7 }))).toThrow("可重跑 setup");
  await main(["setup", "dev", "--scopes="], root);
  await expect(main(["setup", "dev", "--scopes=unknown"], root)).rejects.toThrow("未知依赖范围");
  observations.push({ name: "locking", calls });
}, 30000);

test("独立 CLI 进程从子目录创建并读取相同受管池", () => {
  const root = fixture("process");
  const command = [path.join(project, "scripts/launch/worktree.mjs"), "create", "experiment", "HEAD", "cli", "--no-setup"];
  const created = spawnSync(process.execPath, command, { cwd: path.join(root, "configs"), timeout: 15000, encoding: "utf8" });
  writeFileSync(path.join(artifacts, "cli-create.json"), JSON.stringify({ command, status: created.status, stdout: created.stdout, stderr: created.stderr }));
  expect(created.status).toBe(0);
  const target = JSON.parse(created.stdout).target;
  const listed = spawnSync(process.execPath, [path.join(project, "scripts/launch/worktree.mjs"), "list"], { cwd: target, timeout: 15000, encoding: "utf8" });
  expect(listed.status).toBe(0);
  expect(JSON.parse(listed.stdout).map((record) => record.target)).toEqual([root, target]);
}, 30000);

test("真实打包和安装 CLI 在写产物前拒绝根 checkout 正式发布", () => {
  const root = fixture("source-guard");
  for (const script of ["scripts/release/package-linux-x64.mjs", "scripts/release/package-windows-x64.mjs", "scripts/release/package-windows-x64-cross.mjs", "scripts/release/package-electron.mjs", "scripts/install/install-release.mjs"]) {
    const result = spawnSync(process.execPath, [path.join(project, script)], { cwd: root, env: { ...process.env, BOXTEAM_PROJECT_ROOT: root }, timeout: 15000, encoding: "utf8" });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("正式发布必须从受管");
    expect(existsSync(path.join(root, "out/packaging"))).toBe(false);
    observations.push({ name: "source-guard", script, status: result.status, stderr: result.stderr });
  }
}, 30000);

test("release 来源、基线证据及版本范围不会隐式认定验收", async () => {
  const root = fixture("release");
  expect((await releasePolicy(["compare", "HEAD"], root)).status).toBe("missing-release-baseline");
  await expect(releasePolicy(["record", "HEAD"], root)).rejects.toThrow("完整验收");
  const previous = gitText(root, ["rev-parse", "HEAD"]);
  await releasePolicy(["record", previous, "--accepted-by-user", "--evidence=fixture acceptance and deployment 1"], root);
  writeFileSync(path.join(root, "configs/change.jsonc"), "{}\n");
  git(root, ["add", "."]); git(root, ["commit", "-qm", "second"]);
  const current = gitText(root, ["rev-parse", "HEAD"]);
  await releasePolicy(["record", current, "--accepted-by-user", "--evidence=fixture acceptance and deployment 2"], root);
  const comparison = await releasePolicy(["compare"], root);
  expect(comparison.from).toBe(previous);
  expect(comparison.to).toBe(current);
  expect(comparison.files.join("\n")).toContain("configs/change.jsonc");
  expect(() => assertReleaseSource(root)).toThrow("正式发布");
  expect(assertReleaseSource(root, { development: true }).purpose).toBe("development-verification");
  const checkoutAlias = path.join(workspace, "release-alias");
  symlinkSync(root, checkoutAlias, process.platform === "win32" ? "junction" : "dir");
  expect(assertReleaseSource(checkoutAlias, { development: true }).purpose).toBe("development-verification");
  const release = await main(["create", "release", current, "--no-setup"], root);
  expect(assertReleaseSource(release.target).commit).toBe(current);
  writeFileSync(path.join(release.target, ".env.private"), "must not affect release\n");
  expect(() => assertReleaseSource(release.target)).toThrow("开发本地配置");
  rmSync(path.join(release.target, ".env.private"));
  writeFileSync(path.join(release.target, "tracked.txt"), "dirty");
  expect(() => assertReleaseSource(release.target)).toThrow("未提交源码");
  await expect(main(["sync", "release", "tracked.txt"], root)).rejects.toThrow("release 不接收");
  observations.push({ name: "release", comparison, release });
}, 30000);

test("配置复制拒绝覆盖目标且 tracked 模板只取 base-ref", async () => {
  const root = fixture("config-conflict");
  writeFileSync(path.join(root, ".env.example"), "committed template\n");
  git(root, ["add", "-f", ".env.example"]); git(root, ["commit", "-qm", "template"]);
  writeFileSync(path.join(root, ".env.example"), "uncommitted template\n");
  writeFileSync(path.join(root, ".env"), "source local\n");
  const { target } = await main(["create", "dev", "HEAD", "--no-setup"], root);
  expect(readFileSync(path.join(target, ".env.example"), "utf8")).toBe("committed template\n");
  writeFileSync(path.join(target, ".env"), "target local\n");
  await expect(main(["setup", "dev", "--scopes="], root)).rejects.toThrow("内容不同");
  expect(readFileSync(path.join(target, ".env"), "utf8")).toBe("target local\n");
}, 30000);

test("归档保留 staged/unstaged/untracked/ignored 全部数据，活动实例阻止归档", async () => {
  const root = fixture("archive");
  const { target } = await main(["create", "dev", "HEAD", "--no-setup"], root);
  writeFileSync(path.join(target, "tracked.txt"), "staged\n"); git(target, ["add", "tracked.txt"]);
  writeFileSync(path.join(target, "tracked.txt"), "unstaged\n");
  writeFileSync(path.join(target, "new.txt"), "new\n");
  writeFileSync(path.join(target, ".env"), "private\n");
  const state = gitText(target, ["status", "--porcelain"]);
  const runtime = path.join(target, "out/development-runtime"); mkdirSync(runtime, { recursive: true });
  const ready = path.join(runtime, "detached-ready.json"); writeFileSync(ready, JSON.stringify({ pid: process.pid }));
  await expect(main(["archive", "dev", "retained", "--checked-references"], root)).rejects.toThrow("活动实例");
  rmSync(ready);
  await expect(main(["archive", "dev", "retained"], root)).rejects.toThrow("checked-references");
  const context = repositoryContext(root);
  const metadata = readMetadata(target);
  expect(() => archiveWorktree(context, target, metadata, "retained", {
    assertIdle: () => { throw new Error("fixture systemd active"); },
  })).toThrow("fixture systemd active");
  const archived = archiveWorktree(context, target, metadata, "retained", {
    assertIdle: () => ({ unit: "fixture-unit", state: { activeState: "inactive" } }),
  });
  expect(existsSync(target)).toBe(false);
  expect(gitText(archived.target, ["status", "--porcelain"])).toBe(state);
  expect(readFileSync(path.join(archived.target, ".env"), "utf8")).toBe("private\n");
  expect(readMetadata(archived.target).archived).toBe(true);
  observations.push({ name: "archive", archived });
}, 30000);

test.skipIf(process.platform === "win32")("拒绝复制经过符号链接的本地配置", async () => {
  const root = fixture("symlink");
  writeFileSync(path.join(root, "outside"), "secret");
  symlinkSync("outside", path.join(root, ".env"));
  await expect(main(["create", "dev", "HEAD", "--no-setup"], root)).rejects.toThrow("符号链接");
  expect(existsSync(path.join(root, "out/worktrees/dev/.env"))).toBe(false);
});
