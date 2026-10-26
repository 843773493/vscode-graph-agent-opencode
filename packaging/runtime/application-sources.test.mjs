import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  cpSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";

import { copyApplicationSources } from "./application-sources.mjs";

const runId = randomUUID();
const outputRoot = path.resolve("out/tests/packaging/runtime/application-sources");
for (const kind of ["workspace", "artifacts"]) {
  mkdirSync(path.join(outputRoot, kind), { recursive: true });
  mkdirSync(path.join(outputRoot, kind, runId));
}

function createScenarioDirectories(scenario) {
  const directories = Object.fromEntries(["workspace", "artifacts"].map((kind) => {
    const directory = path.join(outputRoot, kind, runId, scenario);
    mkdirSync(directory);
    return [kind, directory];
  }));
  return directories;
}

function snapshotFileTree(root) {
  const files = [];
  function visit(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const absolutePath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(absolutePath);
      } else {
        files.push({
          // 文件系统使用宿主路径，快照键统一为 /，避免 Windows 与固定清单不一致。
          path: path.relative(root, absolutePath).split(path.sep).join("/"),
          contents: readFileSync(absolutePath).toString("base64"),
        });
      }
    }
  }
  visit(root);
  return files.sort((left, right) => left.path.localeCompare(right.path));
}

test("复制清单保留内容与源树，并过滤全部 Python 字节码缓存", () => {
  const directories = createScenarioDirectories("source-integrity-and-bytecode-filter");
  const projectRoot = path.join(directories.workspace, "源项目 space");
  const applicationRoot = path.join(directories.workspace, "应用 输出 space");
  const files = [
    ["app/agent.py", Buffer.from("Agent 中文正文\n")],
    ["app/nested/运行逻辑 文件.py", Buffer.from([0, 1, 254, 255])],
    ["app/nested/compiled.pyc", Buffer.from("ignored app bytecode")],
    ["app/nested/__pycache__/cached.pyc", Buffer.from("ignored app cache")],
    ["configs/工作区 config.jsonc", Buffer.from('{"enabled": true}\n')],
    ["configs/__pycache__/cached.pyc", Buffer.from("ignored config cache")],
    ["resources/模板目录/资源 文件.json", Buffer.from("资源 bytes")],
    ["resources/cache.pyc", Buffer.from("ignored resource bytecode")],
    ["src/shared/serverRuntimeLogging.mjs", Buffer.from("shared logging")],
    ["src/workspace-services/terminal/server/终端 server.js", Buffer.from("terminal")],
    ["src/workspace-services/terminal/server/__pycache__/cached.pyc", Buffer.from("ignored")],
    ["src/workspace-services/terminal/protocol/终端 protocol.proto", Buffer.from("terminal protocol")],
    ["src/workspace-services/terminal/shared/终端 shared.js", Buffer.from("terminal shared")],
    ["src/workspace-services/browser/server/browser server.js", Buffer.from("browser")],
    ["src/workspace-services/browser/protocol/browser protocol.proto", Buffer.from("browser protocol")],
    ["src/workspace-services/protocol/shared protocol.proto", Buffer.from("shared protocol")],
    ["pyproject.toml", Buffer.from("[project]\nname = 'fixture'\n")],
  ];
  mkdirSync(projectRoot, { recursive: true });
  for (const [relative, contents] of files) {
    const source = path.join(projectRoot, relative);
    mkdirSync(path.dirname(source), { recursive: true });
    writeFileSync(source, contents);
  }
  mkdirSync(applicationRoot, { recursive: true });
  const sourceBefore = snapshotFileTree(projectRoot);
  writeFileSync(path.join(directories.artifacts, "source-before.json"), `${JSON.stringify(sourceBefore, null, 2)}\n`, { flag: "wx" });

  copyApplicationSources(projectRoot, applicationRoot);

  const stagedFiles = snapshotFileTree(applicationRoot);
  const sourceAfter = snapshotFileTree(projectRoot);
  for (const [name, snapshot] of [["staged-files", stagedFiles], ["source-after", sourceAfter]]) {
    writeFileSync(path.join(directories.artifacts, `${name}.json`), `${JSON.stringify(snapshot, null, 2)}\n`, { flag: "wx" });
  }
  const expected = files
    .filter(([relative]) =>
      !relative.split("/").includes("__pycache__") && !relative.endsWith(".pyc"),
    )
    .map(([relative, contents]) => ({
      path: relative,
      contents: Buffer.from(contents).toString("base64"),
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  expect(stagedFiles).toEqual(expected);
  expect(sourceBefore).toEqual(sourceAfter);
  expect(
    stagedFiles.some(({ path: relative }) => relative.includes("__pycache__")),
  ).toBe(false);
});

test("staging复制后可加载终端会话模块的真实相对依赖", () => {
  const directories = createScenarioDirectories("terminal-import-closure");
  const repositoryRoot = process.cwd();
  const projectRoot = path.join(directories.workspace, "终端闭包源码");
  const applicationRoot = path.join(directories.workspace, "终端闭包staging", "application");
  for (const relative of [
    "app",
    "configs",
    "resources",
    "src/workspace-services/browser/server",
    "src/workspace-services/browser/protocol",
  ]) {
    const directory = path.join(projectRoot, relative);
    mkdirSync(directory, { recursive: true });
    writeFileSync(path.join(directory, "fixture.txt"), relative);
  }
  for (const relative of [
    "resources/logging",
    "src/shared",
    "src/workspace-services/terminal/server",
    "src/workspace-services/terminal/protocol",
    "src/workspace-services/terminal/shared",
    "src/workspace-services/protocol",
  ]) {
    cpSync(
      path.join(repositoryRoot, relative),
      path.join(projectRoot, relative),
      { recursive: true },
    );
  }
  writeFileSync(path.join(projectRoot, "pyproject.toml"), "fixture\n");
  mkdirSync(applicationRoot, { recursive: true });
  copyApplicationSources(projectRoot, applicationRoot);

  const terminalSessionUrl = pathToFileURL(path.join(
    applicationRoot,
    "src/workspace-services/terminal/server/terminalSession.js",
  )).href;
  const importScript = `
    const loaded = await import(${JSON.stringify(terminalSessionUrl)});
    const names = ["TerminalSession", "resolveTerminalCwd"];
    if (names.some((name) => typeof loaded[name] !== "function")) {
      throw new Error("terminalSession缺少预期导出");
    }
    console.log(names.join(","));
  `;
  const result = spawnSync(
    "node",
    ["--input-type=module", "--eval", importScript],
    { cwd: repositoryRoot, encoding: "utf8", timeout: 5000 },
  );
  // 先保存原始进程结果；解析或断言失败也保留现场。
  writeFileSync(path.join(directories.artifacts, "stdout.raw"), result.stdout ?? "", { flag: "wx" });
  writeFileSync(path.join(directories.artifacts, "stderr.raw"), result.stderr ?? "", { flag: "wx" });
  writeFileSync(path.join(directories.artifacts, "result.json"), `${JSON.stringify({
    command: ["node", "--input-type=module", "--eval", importScript], cwd: repositoryRoot,
    status: result.status, signal: result.signal, error: result.error?.message,
  }, null, 2)}\n`, { flag: "wx" });
  if (result.error || result.status !== 0) {
    throw new Error(`staging终端模块import失败: exit=${result.status} signal=${result.signal ?? "none"}\n${result.stderr ?? ""}`, {
      cause: result.error,
    });
  }
  expect(result.stdout.trim()).toBe("TerminalSession,resolveTerminalCwd");
});
