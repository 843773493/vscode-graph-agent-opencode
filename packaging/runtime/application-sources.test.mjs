import { expect, test } from "bun:test";
import { randomUUID } from "node:crypto";
import {
  mkdirSync,
  readdirSync,
  readFileSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

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
    ["src/workspace-services/terminal/client/index.html", Buffer.from("terminal attach")],
    ["src/workspace-services/terminal/server/终端 server.js", Buffer.from("terminal")],
    ["src/workspace-services/terminal/server/__pycache__/cached.pyc", Buffer.from("ignored")],
    ["src/workspace-services/terminal/protocol/终端 protocol.proto", Buffer.from("terminal protocol")],
    ["src/workspace-services/terminal/shared/终端 shared.js", Buffer.from("terminal shared")],
    ["src/workspace-services/browser/server/browser server.js", Buffer.from("插件专属服务端")],
    ["src/workspace-services/browser/client/index.html", Buffer.from("browser attach")],
    ["src/workspace-services/browser/client/main.js", Buffer.from("browser entry")],
    ["src/workspace-services/browser/client/browserInputEvents.js", Buffer.from("browser input")],
    ["src/workspace-services/browser/protocol/browser protocol.proto", Buffer.from("browser protocol")],
    ["src/workspace-services/protocol/shared protocol.proto", Buffer.from("shared protocol")],
    ["node_modules/@xterm/xterm/css/xterm.css", Buffer.from("static asset 0")],
    ["node_modules/@xterm/xterm/lib/xterm.js", Buffer.from("static asset 1")],
    ["node_modules/@xterm/addon-fit/lib/addon-fit.js", Buffer.from("static asset 2")],
    ["node_modules/@xterm/addon-search/lib/addon-search.js", Buffer.from("static asset 3")],
    ["src/clients/web/node_modules/@vscode/codicons/dist/codicon.css", Buffer.from("static asset 4")],
    ["src/clients/web/node_modules/@vscode/codicons/dist/codicon.ttf", Buffer.from("static asset 5")],
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
      !relative.split("/").includes("__pycache__") && !relative.endsWith(".pyc") &&
      !/^src\/workspace-services\/(terminal|browser)\/server\//.test(relative),
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
