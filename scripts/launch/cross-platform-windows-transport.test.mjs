import { afterAll, expect, spyOn, test } from "bun:test";
import { chmodSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import path from "node:path";
import { runTargetAction } from "../../tools/cross-platform-development-targets/ssh-target.mjs";

const root = path.resolve("out/tests/scripts/launch/cross-platform-windows-transport/workspace");
mkdirSync(root, { recursive: true });
const identity = path.join(root, "identity");
writeFileSync(identity, "fixture-only");
chmodSync(identity, 0o600);
afterAll(() => rmSync(root, { recursive: true, force: true }));

test("Windows 动作使用单帧 UTF-8 脚本，参数不经过远端命令行转义", async () => {
  const scriptPath = "tools/cross-platform-development-targets/windows/Manage-Target.ps1";
  const target = {
    id: "windows-fixture", platform: "windows",
    ssh: { host: "fixture.invalid", port: 22, user: "fixture", identityFile: identity, knownHostsFile: path.join(root, "known_hosts") },
  };
  const args = ["C:/目录 含空格/引号'与$符号.txt"];
  let transport;
  const spawn = spyOn(Bun, "spawn").mockImplementation((command, options) => {
    transport = { command, input: options.stdin === "pipe" ? "" : options.stdin.toString("utf8") };
    return {
      exited: Promise.resolve(0), signalCode: null,
      stdin: { write(input) { transport.input += input; }, end() {} },
      stdout: new Response("中文结果\n").body, stderr: new Response("").body,
    };
  });
  try {
    expect(await runTargetAction({ target, projectRoot: process.cwd(), action: "hash-file", args })).toEqual({
      exitCode: 0, stdout: "中文结果", stderr: "",
    });
  } finally {
    spawn.mockRestore();
  }
  expect(transport.command).toContain("StrictHostKeyChecking=yes");
  const remote = transport.command.at(-1);
  expect(remote).not.toContain(args[0]);
  const bootstrap = Buffer.from(remote.split("-EncodedCommand ")[1], "base64").toString("utf16le");
  expect(bootstrap).toContain("[Console]::ReadLine()");
  expect(bootstrap).toContain("exit 1");
  expect(transport.input.trim().split("\n")).toHaveLength(1);
  const script = Buffer.from(transport.input.trim(), "base64").toString("utf8");
  const encodedArguments = script.match(/BOXTEAM_TARGET_ARGUMENTS_BASE64='([^']+)'/)[1];
  expect(JSON.parse(Buffer.from(encodedArguments, "base64").toString("utf8"))).toEqual(["hash-file", ...args]);
  expect(script.slice(script.indexOf("\n") + 1)).toBe(readFileSync(scriptPath, "utf8"));
});
