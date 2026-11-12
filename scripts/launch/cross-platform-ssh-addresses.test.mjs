import { expect, test } from "bun:test";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { scpCommand, scpDownloadCommand, sshCommand } from "../../tools/cross-platform-development-targets/ssh-target.mjs";

const runId = randomUUID();
const output = path.resolve("out/tests/scripts/launch/cross-platform-ssh-addresses");
const entry = new URL("../../tools/cross-platform-development-targets/ssh-target.mjs", import.meta.url).href;

function fixture(label, host) {
  const workspace = path.join(output, "workspace", runId, label);
  const artifacts = path.join(output, "artifacts", runId, label);
  const bin = path.join(workspace, "bin");
  mkdirSync(bin, { recursive: true });
  mkdirSync(artifacts, { recursive: true });
  const capture = path.join(artifacts, "ssh-argv.json");
  const ssh = path.join(bin, "ssh");
  // 真实 Git/SCP 只运行此替身，不允许连接任何远端。
  writeFileSync(ssh, `#!${process.execPath}
import { writeFileSync } from "node:fs";
writeFileSync(${JSON.stringify(capture)}, JSON.stringify(process.argv.slice(2)));
process.exit(23);
`, { mode: 0o700 });
  const identityFile = path.join(workspace, "identity");
  writeFileSync(identityFile, "fixture-only", { mode: 0o600 });
  const target = {
    id: "address-fixture", platform: "linux",
    ssh: { host, port: 2222, user: "fixture", identityFile,
      knownHostsFile: path.join(workspace, "known_hosts") },
    paths: { repository: "/srv/中文 work%20/repo%2Fliteral\\name#x?" },
  };
  const environment = {
    ...process.env, PATH: `${bin}${path.delimiter}${process.env.PATH}`,
    GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
    GIT_SSH_VARIANT: "ssh", GIT_TERMINAL_PROMPT: "0",
  };
  let sequence = 0;
  return {
    workspace, target, ssh,
    run(command, overrides = {}) {
      const result = spawnSync(command[0], command.slice(1), {
        cwd: overrides.cwd ?? workspace, env: { ...environment, ...overrides.env }, encoding: "utf8", timeout: 10_000,
      });
      const prefix = path.join(artifacts, String(++sequence));
      writeFileSync(`${prefix}.stdout.raw`, result.stdout ?? "");
      writeFileSync(`${prefix}.stderr.raw`, result.stderr ?? "");
      writeFileSync(`${prefix}.result.json`, JSON.stringify({ command, cwd: overrides.cwd ?? workspace,
        status: result.status, signal: result.signal, error: result.error?.message ?? null }));
      expect(result.error).toBeUndefined();
      return result;
    },
    arguments() { return JSON.parse(readFileSync(capture, "utf8")); },
  };
}

for (const [label, host, expectedHost] of [
  ["dns", "fixture.invalid", "fixture.invalid"],
  ["ipv4", "192.0.2.42", "192.0.2.42"],
  ["ipv6", "2001:db8::42", "2001:db8::42"],
  ["bracketed-ipv6", "[2001:db8::42]", "2001:db8::42"],
]) {
  test.skipIf(process.platform === "win32")(`真实 Git 推送保留原生路径、端口及 ${label} 主机`, () => {
    const probe = fixture(`git-${label}-%k space`, host);
    expect(probe.run(["git", "init", "-q"]).status).toBe(0);
    expect(probe.run(["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
      "-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "fixture"]).status).toBe(0);
    const script = `import {pushSnapshot} from ${JSON.stringify(entry)};
pushSnapshot({target:${JSON.stringify(probe.target)},projectRoot:process.cwd(),
  snapshot:{commit:"HEAD",ref:"refs/heads/fixture"}});`;
    const result = probe.run([process.execPath, "--eval", script]);
    expect(result.status).toBe(1);
    expect(result.stderr).toContain("失败(exit=128)");
    const args = probe.arguments();
    expect(args.at(-2)).toBe(`fixture@${expectedHost}`);
    expect(args.at(-1)).toBe(`git-receive-pack '${probe.target.paths.repository}'`);
    expect(args.slice(args.indexOf("-p"), args.indexOf("-p") + 2)).toEqual(["-p", "2222"]);
    expect(args).toContain("StrictHostKeyChecking=yes");
    expect(args).toContain(`UserKnownHostsFile="${probe.target.ssh.knownHostsFile.replaceAll("%", "%%")}"`);
  });

  for (const direction of ["upload", "download"]) {
    test.skipIf(process.platform === "win32")(`真实 SCP ${direction} 正确解析 ${label} 主机及端口`, () => {
      const probe = fixture(`scp-${direction}-${label}`, host);
      const local = path.join(probe.workspace, "identity");
      const command = direction === "upload"
        ? scpCommand(probe.target, local, "/srv/remote-file")
        : scpDownloadCommand(probe.target, "/srv/remote-file", local);
      const result = probe.run([command[0], "-S", probe.ssh, ...command.slice(1)]);
      expect(result.status).not.toBe(0);
      const args = probe.arguments();
      expect(args).toContain(expectedHost);
      expect(args.slice(args.indexOf("-l"), args.indexOf("-l") + 2)).toEqual(["-l", "fixture"]);
      expect(args.slice(args.indexOf("-p"), args.indexOf("-p") + 2)).toEqual(["-p", "2222"]);
      expect(args).toContain("StrictHostKeyChecking=yes");
    });
  }
}

for (const owner of ["target", "gateway"]) {
  for (const name of ["中文 space", "percent-%k", 'quote-"-slash-\\']) {
    test.skipIf(process.platform !== "linux")(`真实 OpenSSH 保留 ${owner} 的 ${name} 路径`, () => {
      const probe = fixture(`paths-${owner}-${name}`, "fixture.invalid");
      const directory = path.join(probe.workspace, name);
      mkdirSync(directory);
      const key = path.join(probe.workspace, "generated-key");
      expect(probe.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key]).status).toBe(0);
      probe.target.ssh.identityFile = path.join(directory, "identity");
      probe.target.ssh.knownHostsFile = path.join(directory, "known_hosts");
      copyFileSync(key, probe.target.ssh.identityFile);
      let command = sshCommand(probe.target);
      if (owner === "gateway") {
        const script = `import json,sys
from app.gateway.ssh_command import build_ssh_command
print(json.dumps(build_ssh_command(host="fixture.invalid",port=2222,username="fixture",
    private_key_path=sys.argv[1],ssh_config_host=None)))`;
        const result = probe.run(["uv", "run", "--no-sync", "python", "-c", script,
          probe.target.ssh.identityFile], {
          cwd: process.cwd(), env: { BOXTEAM_GATEWAY_SSH_KNOWN_HOSTS_FILE: probe.target.ssh.knownHostsFile },
        });
        expect(result.status).toBe(0);
        command = JSON.parse(result.stdout);
      }
      // 使用真实客户端但在本地终止代理，不连接网络、不读取用户 SSH 配置。
      const options = command.slice(1);
      const nativeEnvironment = { env: { PATH: process.env.PATH } };
      const parsed = probe.run(["ssh", "-F", "/dev/null", "-G", ...options], nativeEnvironment);
      expect(parsed.status).toBe(0);
      expect(parsed.stdout).toContain(`identityfile ${probe.target.ssh.identityFile.replaceAll("%", "%%")}\n`);
      expect(parsed.stdout).toContain(`userknownhostsfile ${probe.target.ssh.knownHostsFile}\n`);
      const actual = probe.run(["ssh", "-F", "/dev/null", "-vvv", "-o", "ProxyCommand=/bin/false", ...options], nativeEnvironment);
      expect(actual.status).toBe(255);
      expect(actual.stderr).toMatch(/debug1: identity file .+identity type 3/);
      if (name.includes("%")) {
        const expansion = actual.stderr.split(/\r?\n/).find(line => line.includes("expanded UserKnownHostsFile"));
        expect(expansion).toEndWith(` -> '${probe.target.ssh.knownHostsFile}'`);
      }
    });
  }
}

test.skipIf(process.platform === "win32")("SSH 路径含环境变量语法时明确拒绝", () => {
  const probe = fixture("environment-expression", "fixture.invalid");
  probe.target.ssh.knownHostsFile = path.join(probe.workspace, "${HOME}", "known_hosts");
  expect(() => sshCommand(probe.target)).toThrow("UserKnownHostsFile 路径不支持 OpenSSH 环境变量语法");
});
