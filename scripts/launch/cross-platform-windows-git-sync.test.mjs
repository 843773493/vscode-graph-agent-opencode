import { afterAll, expect, spyOn, test } from "bun:test";
import { chmodSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import crossSpawn from "cross-spawn";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { pushSnapshot } from "../../tools/cross-platform-development-targets/ssh-target.mjs";
import {
  windowsGitSshArguments,
} from "../../tools/cross-platform-development-targets/windows-git-ssh.mjs";

const root = path.resolve("out/tests/scripts/launch/cross-platform-windows-git-sync/workspace", String(process.pid));
mkdirSync(root, { recursive: true });
const identity = path.join(root, "identity");
writeFileSync(identity, "fixture-only");
if (process.platform !== "win32") chmodSync(identity, 0o600);
afterAll(() => rmSync(root, { recursive: true, force: true }));

test.each([
  ["fixture.invalid", "fixture.invalid"],
  ["2001:db8::1", "[2001:db8::1]"],
])("Windows Git 同步保留盘符和 SSH 设置，由专用入口转换远端命令 (%s)", (host, remoteHost) => {
  const target = {
    id: "windows-fixture", platform: "windows",
    paths: { repository: "C:\\Users\\fixture\\中文 work tree\\repository" },
    ssh: { host, port: 2222, user: "fixture", identityFile: identity,
      knownHostsFile: path.join(root, "known_hosts") },
  };
  let invocation;
  const spawn = spyOn(crossSpawn, "sync").mockImplementation((command, args, options) => {
    invocation = { command: [command, ...args], environment: options.env };
    return { status: 0, signal: null, stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) };
  });
  try {
    pushSnapshot({ target, projectRoot: process.cwd(),
      snapshot: { commit: "0123456789abcdef", ref: "refs/boxteam/snapshots/test" } });
  } finally { spawn.mockRestore(); }
  expect(invocation).toBeDefined();
  expect(invocation.command).toEqual(["git", "push", "--force",
    `fixture@${remoteHost}:C:/Users/fixture/中文 work tree/repository`,
    "0123456789abcdef:refs/boxteam/snapshots/test"]);
  expect(invocation.environment.GIT_SSH_VARIANT).toBe("simple");
  expect(invocation.environment.GIT_SSH_COMMAND).toContain("windows-git-ssh.mjs");
  expect(invocation.environment.GIT_SSH_COMMAND).toContain('"-p","2222"');
  expect(invocation.environment.GIT_SSH_COMMAND).toContain("StrictHostKeyChecking=yes");
});

test("Windows Git SSH 仅转换已固定目标的 receive-pack 路径且保留传输参数", () => {
  const repository = "C:/Users/fixture/中文 work tree/it's/repository";
  const destination = "fixture@fixture.invalid";
  const options = ["-p", "2222", "-o", "StrictHostKeyChecking=yes"];
  const settings = JSON.stringify({ repository, destination, options });
  const remoteCommand = "git-receive-pack 'C:/Users/fixture/中文 work tree/it'\\''s/repository'";
  expect(windowsGitSshArguments([settings, destination, remoteCommand]))
    .toEqual(["ssh", ...options, destination, `git-receive-pack "${repository}"`]);
  expect(() => windowsGitSshArguments([settings, "other.invalid", remoteCommand]))
    .toThrow("只接受当前目标");
  expect(() => windowsGitSshArguments([settings, destination, "git-receive-pack 'C:/other'"]))
    .toThrow("只接受当前目标");
  const expanded = "C:/%TEMP%/repository";
  expect(() => windowsGitSshArguments([JSON.stringify({ repository: expanded, destination, options }),
    destination, `git-receive-pack '${expanded}'`])).toThrow("百分号");
});

test("Git SSH真实入口原样继承二进制stdin、stdout和stderr", () => {
  const runId = randomUUID();
  const workspace = path.resolve("out/tests/scripts/launch/cross-platform-windows-git-sync/workspace", runId, "binary-stream");
  const artifacts = path.resolve("out/tests/scripts/launch/cross-platform-windows-git-sync/artifacts", runId, "binary-stream");
  const bin = path.join(workspace, "fixture bin");
  mkdirSync(bin, { recursive: true });
  mkdirSync(artifacts, { recursive: true });
  const program = "process.stdin.pipe(process.stdout); process.stderr.write(Buffer.from([255, 0, 128, 10]));";
  const script = path.join(bin, "ssh-fixture.mjs");
  writeFileSync(script, program);
  if (process.platform === "win32") {
    writeFileSync(path.join(bin, "ssh.cmd"), '@"%BOXTEAM_FIXTURE_JS%" "%BOXTEAM_FIXTURE_SSH%"\r\n');
  } else {
    writeFileSync(path.join(bin, "ssh"), `#!${process.execPath}\n${program}`, { mode: 0o700 });
  }
  const payload = Buffer.from([0, 255, 10, 13, 128, 240, 159, 146, 169]);
  const repository = "C:/fixture/repository";
  const destination = "fixture@fixture.invalid";
  const args = [path.resolve("tools/cross-platform-development-targets/windows-git-ssh.mjs"),
    JSON.stringify({ repository, destination, options: [] }), destination, `git-receive-pack '${repository}'`];
  const result = spawnSync(process.execPath, args, {
    cwd: workspace, input: payload, timeout: 5000,
    env: { ...process.env, PATH: bin, PATHEXT: ".CMD;.EXE",
      BOXTEAM_FIXTURE_JS: process.execPath, BOXTEAM_FIXTURE_SSH: script },
  });
  writeFileSync(path.join(artifacts, "stdin.raw"), payload);
  writeFileSync(path.join(artifacts, "stdout.raw"), result.stdout ?? "");
  writeFileSync(path.join(artifacts, "stderr.raw"), result.stderr ?? "");
  writeFileSync(path.join(artifacts, "result.json"), JSON.stringify({
    command: [process.execPath, ...args], cwd: workspace, status: result.status, signal: result.signal,
    error: result.error?.message ?? null,
  }));
  expect(result.error).toBeUndefined();
  expect(result.status).toBe(0);
  expect(readFileSync(path.join(artifacts, "stdout.raw"))).toEqual(payload);
  expect(result.stderr).toEqual(Buffer.from([255, 0, 128, 10]));
});
