import { afterEach, describe, expect, test } from "bun:test";
import {
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";

import {
  loadTargetConfiguration,
  selectTarget,
} from "../tools/cross-platform-development-targets/config.mjs";
import { parseTargetCliArgs } from "../tools/cross-platform-development-targets/cli-options.mjs";
import {
  assertCleanInitializedSubmodules,
  createWorkspaceSnapshot,
} from "../tools/cross-platform-development-targets/snapshot.mjs";
import {
  assertMatchingSha256,
  sshCommand,
} from "../tools/cross-platform-development-targets/ssh-target.mjs";

const projectRoot = process.cwd();
const temporaryRoots = [];

function temporaryRoot() {
  const root = mkdtempSync(path.join(os.tmpdir(), "boxteam-target-test-"));
  temporaryRoots.push(root);
  return root;
}

function command(cwd, args) {
  const result = Bun.spawnSync(args, { cwd, stdout: "pipe", stderr: "pipe" });
  if (result.exitCode !== 0) {
    throw new Error(new TextDecoder().decode(result.stderr));
  }
  return new TextDecoder().decode(result.stdout).trim();
}

function initializeRepository(root) {
  command(root, ["git", "init", "-q"]);
  command(root, ["git", "config", "user.name", "Target Test"]);
  command(root, ["git", "config", "user.email", "target-test@localhost"]);
  writeFileSync(path.join(root, ".gitignore"), ".env\nout/\n", "utf8");
  writeFileSync(path.join(root, "tracked.txt"), "before\n", "utf8");
  writeFileSync(path.join(root, "deleted.txt"), "remove\n", "utf8");
  command(root, ["git", "add", "."]);
  command(root, ["git", "commit", "-qm", "initial"]);
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

describe("跨平台开发目标配置", () => {
  test("加载示例并严格校验目标", () => {
    const configuration = loadTargetConfiguration({
      configPath: path.join(
        projectRoot,
        "tools/cross-platform-development-targets/targets.example.jsonc",
      ),
      projectRoot,
    });
    const target = selectTarget(configuration, "docker-debian");
    expect(target.platform).toBe("linux");
    expect(path.isAbsolute(target.ssh.identityFile)).toBe(true);
    expect(path.isAbsolute(target.docker.persistentRoot)).toBe(true);
  });

  test("schema 错误不回显配置中的秘密值", () => {
    const root = temporaryRoot();
    const configPath = path.join(root, "targets.jsonc");
    writeFileSync(
      configPath,
      JSON.stringify({ version: 1, targets: [], secret: "DO_NOT_PRINT_THIS" }),
      "utf8",
    );
    expect(() => loadTargetConfiguration({ configPath, projectRoot })).toThrow(
      /additional properties/,
    );
    try {
      loadTargetConfiguration({ configPath, projectRoot });
    } catch (error) {
      expect(String(error)).not.toContain("DO_NOT_PRINT_THIS");
    }
  });

  test("命令参数要求显式 target 并验证 profile", () => {
    expect(parseTargetCliArgs(["sync", "docker-debian", "--activate"])).toMatchObject({
      command: "sync",
      targetId: "docker-debian",
      options: { activate: true, profile: "development" },
    });
    expect(() =>
      parseTargetCliArgs(["start", "docker-debian", "--profile", "unknown"]),
    ).toThrow(/profile/);
  });
});

describe("Git 脏工作区快照", () => {
  test("包含修改删除和未跟踪文件且不改变真实 index", () => {
    const root = temporaryRoot();
    initializeRepository(root);
    writeFileSync(path.join(root, "tracked.txt"), "after\n", "utf8");
    rmSync(path.join(root, "deleted.txt"));
    writeFileSync(path.join(root, "new.txt"), "new\n", "utf8");
    writeFileSync(path.join(root, ".env"), "SECRET=not-in-git\n", "utf8");
    const statusBefore = command(root, ["git", "status", "--porcelain"]);
    const cachedBefore = command(root, ["git", "diff", "--cached"]);

    const snapshot = createWorkspaceSnapshot({ projectRoot: root });

    expect(command(root, ["git", "status", "--porcelain"])).toBe(statusBefore);
    expect(command(root, ["git", "diff", "--cached"])).toBe(cachedBefore);
    expect(command(root, ["git", "show", `${snapshot.commit}:tracked.txt`])).toBe("after");
    expect(command(root, ["git", "show", `${snapshot.commit}:new.txt`])).toBe("new");
    expect(
      Bun.spawnSync(["git", "cat-file", "-e", `${snapshot.commit}:deleted.txt`], {
        cwd: root,
      }).exitCode,
    ).not.toBe(0);
    expect(snapshot.files).not.toContain(".env");
  });

  test("拒绝包含未提交内容的已初始化子模块", () => {
    const child = temporaryRoot();
    initializeRepository(child);
    const parent = temporaryRoot();
    initializeRepository(parent);
    command(parent, [
      "git",
      "-c",
      "protocol.file.allow=always",
      "submodule",
      "add",
      "-q",
      child,
      "vendor/child",
    ]);
    command(parent, ["git", "commit", "-qam", "add submodule"]);
    writeFileSync(path.join(parent, "vendor/child/tracked.txt"), "dirty\n", "utf8");
    expect(() => assertCleanInitializedSubmodules(parent)).toThrow(/子模块/);
  });
});

describe("平台 adapter 边界", () => {
  test("SSH 命令强制 host key 校验且不包含密钥内容", () => {
    const target = {
      id: "linux",
      ssh: {
        host: "127.0.0.1",
        port: 22222,
        user: "boxteam",
        identityFile: path.join(
          projectRoot,
          "asset/gateway_ssh/boxteam_gateway_e2e_ed25519",
        ),
        knownHostsFile: "/tmp/known-hosts",
      },
    };
    const result = sshCommand(target, "echo ready");
    expect(result).toContain("StrictHostKeyChecking=yes");
    expect(result.join(" ")).not.toContain("PRIVATE KEY");
  });

  test(".env 哈希不一致时快速失败", () => {
    expect(() => assertMatchingSha256("local", "remote", "linux")).toThrow(
      /SHA-256 校验失败/,
    );
  });

  test("Windows adapter 保留真实 VMware 验证 TODO 且不使用 POSIX 生命周期命令", () => {
    const script = readFileSync(
      path.join(
        projectRoot,
        "tools/cross-platform-development-targets/windows/Manage-Target.ps1",
      ),
      "utf8",
    );
    expect(script).toContain("TODO");
    expect(script).not.toMatch(/\bnohup\b/);
    expect(script).not.toMatch(/\bkill\b/);
    expect(script).toContain("BOXTEAM_TARGET_ARGUMENTS_BASE64");
    expect(script).toContain("Assert-NativeCommand");
    expect(script).toContain(".venv\\Scripts\\python.exe");
    expect(script).toContain(".boxteams-dev");
    expect(script).toContain(".boxteams");
    expect(script).toContain("开发服务端口已被占用");
    expect(script).toContain("bun install --force --frozen-lockfile");
  });

  test("Linux adapter 快速暴露冲突并按环境版本重建目标 .venv", () => {
    const script = readFileSync(
      path.join(
        projectRoot,
        "tools/cross-platform-development-targets/linux/manage-target.sh",
      ),
      "utf8",
    );
    expect(script).toContain('set -eu');
    expect(script).toContain("开发服务端口已被占用");
    expect(script).toContain('"$venv_version" != "$system_version"');
    expect(script).toContain('rm -rf -- "$repository/.venv"');
    expect(script).toContain('bun install --force --frozen-lockfile');
    expect(script).toContain('$target_home/.boxteams-dev');
    expect(script).toContain('$target_home/.boxteams');
    expect(script).not.toContain('kill "$(cat');
  });
});
