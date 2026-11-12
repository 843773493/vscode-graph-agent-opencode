import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const root = path.resolve("out/tests/packages/launcher/src/gateway/browser-open");
const runId = randomUUID();
const supervisorUrl = new URL("./supervisor.mjs", import.meta.url).href;

async function observeBrowser(supervisorUrl, platform, scenario) {
  const assert = await import("node:assert/strict");
  const { EventEmitter } = await import("node:events");
  const { default: childProcess } = await import("node:child_process");
  Object.defineProperty(process, "platform", { value: platform });
  const calls = [];
  childProcess.spawn = (command, args, options) => {
    calls.push({ command, args, options });
    const child = new EventEmitter();
    child.exitCode = null;
    child.signalCode = null;
    child.unref = () => {};
    queueMicrotask(() => {
      if (scenario === "spawn-error") {
        child.emit("error", Object.assign(new Error("fixture opener unavailable"), { code: "ENOENT" }));
        return;
      }
      child.emit("spawn");
      setImmediate(() => {
        child.exitCode = scenario === "nonzero" ? 17 : scenario === "signal" ? null : 0;
        child.signalCode = scenario === "signal" ? "SIGTERM" : null;
        child.emit("exit", child.exitCode, child.signalCode);
        child.emit("close", child.exitCode, child.signalCode);
      });
    });
    return child;
  };
  const { openGatewayBrowser } = await import(supervisorUrl);
  const warnings = [];
  const url = "http://127.0.0.1:8014/?name=中文&value=a(b)%20'\"";
  await openGatewayBrowser({ url, logger: { warning: (message) => warnings.push(message) } });
  assert.equal(calls.length, 1);
  if (scenario === "success") assert.deepEqual(warnings, []);
  else {
    assert.equal(warnings.length, 1);
    assert.ok(warnings[0].includes(url));
    assert.ok(warnings[0].includes(scenario === "spawn-error" ? "fixture opener unavailable"
      : scenario === "nonzero" ? "exit=17" : "signal=SIGTERM"));
  }
  const call = calls[0];
  if (platform === "win32") {
    assert.match(call.command, /powershell\.exe$/i);
    const script = Buffer.from(call.args.at(-1), "base64").toString("utf16le");
    assert.ok(script.includes("中文&value=a(b)%20"));
  } else {
    assert.equal(call.args.at(-1), url);
    if (platform === "darwin") assert.equal(call.command, "open");
    else assert.match(call.command, /xdg-open$/);
  }
  await assert.rejects(openGatewayBrowser({ url: "", logger: {} }), /非空字符串/);
  return { platform, scenario, calls, warnings };
}

const artifactsRoot = path.join(root, "artifacts", runId);
mkdirSync(artifactsRoot, { recursive: true });
const observer = path.join(artifactsRoot, "observer.mjs");
writeFileSync(observer, `${observeBrowser.toString()}\nconsole.log(JSON.stringify(await observeBrowser(...JSON.parse(process.argv[2]))));\n`, { flag: "wx" });

for (const platform of ["linux", "darwin", "win32"]) {
  for (const scenario of ["success", "spawn-error", "nonzero", "signal"]) {
    test(`${platform}/${scenario}通过真实open包打开并保留失败提示`, () => {
      const workspace = path.join(root, "workspace", runId, platform, scenario);
      const artifacts = path.join(artifactsRoot, platform, scenario);
      mkdirSync(workspace, { recursive: true });
      mkdirSync(artifacts, { recursive: true });
      const args = [observer, JSON.stringify([supervisorUrl, platform, scenario])];
      const result = spawnSync("node", args, { cwd: workspace, timeout: 5000 });
      writeFileSync(path.join(artifacts, "stdout.raw"), result.stdout ?? "", { flag: "wx" });
      writeFileSync(path.join(artifacts, "stderr.raw"), result.stderr ?? "", { flag: "wx" });
      writeFileSync(path.join(artifacts, "result.json"), JSON.stringify({
        command: ["node", ...args], cwd: workspace, status: result.status, signal: result.signal,
        error: result.error?.message ?? null,
      }), { flag: "wx" });
      if (result.error || result.status !== 0) throw new Error(`浏览器启动观察失败: ${artifacts}\n${result.stderr}`, { cause: result.error });
      expect(JSON.parse(result.stdout)).toMatchObject({ platform, scenario });
    });
  }
}
