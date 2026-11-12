import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const projectRoot = process.cwd();
const root = path.resolve("out/tests/packaging/runtime/windows-command-execution");
const runId = randomUUID();

async function observeCommands(projectRoot, mode) {
  const assert = await import("node:assert/strict");
  const fs = await import("node:fs");
  const path = await import("node:path");
  const vm = await import("node:vm");
  const { default: ts } = await import(`${projectRoot}/node_modules/typescript/lib/typescript.js`);
  const { default: childProcess } = await import("node:child_process");
  const workspace = process.cwd();
  const packagingSource = fs.readFileSync(path.join(projectRoot, "scripts/release/package-windows-x64.mjs"), "utf8");
  const packagingFile = ts.createSourceFile("package-windows-x64.mjs", packagingSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  const scriptRuntime = packagingFile.statements
    .filter(ts.isVariableStatement)
    .flatMap(statement => statement.declarationList.declarations)
    .find(declaration => declaration.name.getText(packagingFile) === "scriptRuntime");
  assert.ok(scriptRuntime?.initializer, "Windows发布wrapper缺少builder运行时");
  assert.equal(vm.runInNewContext(scriptRuntime.initializer.getText(packagingFile), {
    process: { platform: "win32", execPath: "C:\\tools\\bun.exe" },
  }), "C:\\tools\\bun.exe", "Windows builder必须使用启动wrapper的Bun运行时");
  const bin = path.join(workspace, "命令 bin");
  fs.mkdirSync(bin);
  const calls = [];
  const callsPath = path.join(workspace, "calls.jsonl");
  const fixtureScript = path.join(bin, "npm-fixture.mjs");
  fs.writeFileSync(fixtureScript, `import { appendFileSync } from 'node:fs';
    appendFileSync(process.env.BOXTEAM_NPM_CALLS, JSON.stringify(process.argv.slice(2)) + '\\n');
    console.log(process.argv[2] === 'pack' ? 'fixture.tgz' : 'installed');`);
  const environment = {
    ...process.env, PATH: bin, PATHEXT: ".CMD;.EXE",
    BOXTEAM_NPM_CALLS: callsPath, BOXTEAM_NODE_FIXTURE: process.execPath,
    BOXTEAM_NPM_FIXTURE: fixtureScript,
  };
  if (process.platform === "win32") {
    fs.writeFileSync(path.join(bin, "npm.cmd"), '@"%BOXTEAM_NODE_FIXTURE%" "%BOXTEAM_NPM_FIXTURE%" %*\r\n');
  } else {
    fs.writeFileSync(path.join(bin, "npm"), `#!${process.execPath}\n${fs.readFileSync(fixtureScript, "utf8")}`, { mode: 0o700 });
  }
  if (mode === "windows-parser") {
    Object.defineProperty(process, "platform", { value: "win32" });
    // 让真实cross-spawn解析Windows命令，仅替换最后的原生创建边界。
    childProcess.spawnSync = (command, args, options) => {
      calls.push({ command, args, options });
      return { status: 0, signal: null, stdout: args[3].includes('^"pack^"') ? "fixture.tgz\n" : "installed\n" };
    };
  }
  const { default: crossSpawn } = await import(`${projectRoot}/node_modules/cross-spawn/index.js`);
  const destination = path.join(workspace, "中文 release & (包) %value%");
  const packageRoot = path.join(workspace, "待发布 package");
  fs.mkdirSync(packageRoot);
  const tarball = path.join(destination, "release & assets.tgz");
  const prefix = path.join(workspace, "安装 prefix & (目录)");
  const expected = [
    ["pack", "--silent", "--pack-destination", destination],
    ["install", "--prefix", prefix, tarball],
  ];
  for (const [filename, names] of [
    ["packaging/runtime/build-windows-x64.mjs", ["run", "npmPack"]],
    ["packaging/runtime/verification/verify-windows-x64.mjs", ["run"]],
  ]) {
    const source = ts.createSourceFile(filename, fs.readFileSync(path.join(projectRoot, filename), "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
    const declarations = names.map(name => {
      const declaration = source.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name);
      assert.ok(declaration, `${filename}缺少${name}`);
      return declaration.getText(source);
    });
    const context = vm.createContext({ crossSpawn, path, projectRoot: workspace, process: { env: environment } });
    vm.runInContext(declarations.join("\n"), context);
    if (names.includes("npmPack")) assert.equal(context.npmPack(packageRoot, destination), path.join(destination, "fixture.tgz"));
    else assert.equal(context.run("npm", expected[1], { env: environment, capture: true }), "installed");
  }
  if (mode === "windows-parser") {
    assert.equal(calls.length, 2);
    for (let index = 0; index < calls.length; index++) {
      const call = calls[index];
      assert.match(call.command, /cmd\.exe$/i);
      assert.deepEqual(call.args.slice(0, 3), ["/d", "/s", "/c"]);
      assert.equal(call.options.windowsVerbatimArguments, true);
      assert.equal(call.options.shell, undefined);
      for (const argument of expected[index]) {
        for (const fragment of argument.split(/[/\\]/)) {
          const escaped = fragment.replace(/[()%!^"<>&|;, *?]/g, "^$&");
          assert.ok(call.args[3].includes(escaped), `${call.args[3]}缺少${escaped}`);
        }
      }
    }
  } else {
    assert.deepEqual(fs.readFileSync(callsPath, "utf8").trim().split("\n").map(JSON.parse), expected);
  }
  return { mode, calls: mode === "windows-parser" ? calls : expected };
}

const artifactsRoot = path.join(root, "artifacts", runId);
mkdirSync(artifactsRoot, { recursive: true });
const observer = path.join(artifactsRoot, "observer.mjs");
writeFileSync(observer, `${observeCommands.toString()}\nconsole.log(JSON.stringify(await observeCommands(...JSON.parse(process.argv[2]))));\n`, { flag: "wx" });

for (const mode of ["native-arguments", "windows-parser"]) {
  test(`${mode}通过真实cross-spawn保留打包和安装参数`, () => {
    const workspace = path.join(root, "workspace", runId, mode);
    const artifacts = path.join(artifactsRoot, mode);
    mkdirSync(workspace, { recursive: true });
    mkdirSync(artifacts);
    const args = [observer, JSON.stringify([projectRoot, mode])];
    const result = spawnSync("node", args, { cwd: workspace, timeout: 5000 });
    for (const stream of ["stdout", "stderr"]) writeFileSync(path.join(artifacts, `${stream}.raw`), result[stream] ?? "", { flag: "wx" });
    writeFileSync(path.join(artifacts, "result.json"), JSON.stringify({
      command: ["node", ...args], cwd: workspace, status: result.status, signal: result.signal,
      error: result.error?.message ?? null,
    }), { flag: "wx" });
    if (result.error || result.status !== 0) throw new Error(`命令执行观察失败: ${artifacts}\n${result.stderr}`, { cause: result.error });
    expect(JSON.parse(result.stdout).mode).toBe(mode);
  });
}
