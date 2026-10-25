import { afterAll, expect, spyOn, test } from "bun:test";
import { chmodSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { commandForCurrentPlatform } from "../../tools/cross-platform-development-targets/process.mjs";
import { pushSnapshot } from "../../tools/cross-platform-development-targets/ssh-target.mjs";
import {
  windowsGitSshArguments,
  windowsGitSshCommand,
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
  const originalSpawnSync = Bun.spawnSync.bind(Bun);
  const spawn = spyOn(Bun, "spawnSync").mockImplementation((command, options) => {
    const executable = Array.isArray(command)
      ? command[0]
      : typeof command === "string"
        ? command
        : Array.isArray(command?.cmd)
          ? command.cmd[0]
          : command?.cmd;
    if (executable !== commandForCurrentPlatform(["git"])[0]) {
      return originalSpawnSync(command, options);
    }
    invocation = { command, environment: options.env };
    return { exitCode: 0, signalCode: null, stdout: Buffer.alloc(0), stderr: Buffer.alloc(0) };
  });
  try {
    pushSnapshot({ target, projectRoot: process.cwd(),
      snapshot: { commit: "0123456789abcdef", ref: "refs/boxteam/snapshots/test" } });
  } finally { spawn.mockRestore(); }
  expect(invocation).toBeDefined();
  expect(invocation.command).toEqual([commandForCurrentPlatform(["git"])[0], "push", "--force",
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
  expect(windowsGitSshCommand([settings, destination, remoteCommand], "win32"))
    .toEqual(["ssh.exe", ...options, destination, `git-receive-pack "${repository}"`]);
  expect(() => windowsGitSshArguments([settings, "other.invalid", remoteCommand]))
    .toThrow("只接受当前目标");
  expect(() => windowsGitSshArguments([settings, destination, "git-receive-pack 'C:/other'"]))
    .toThrow("只接受当前目标");
  const expanded = "C:/%TEMP%/repository";
  expect(() => windowsGitSshArguments([JSON.stringify({ repository: expanded, destination, options }),
    destination, `git-receive-pack '${expanded}'`])).toThrow("百分号");
});
