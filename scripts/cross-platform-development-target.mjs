import { mkdirSync } from "node:fs";
import path from "node:path";

import {
  parseTargetCliArgs,
  targetCliUsage,
} from "../tools/cross-platform-development-targets/cli-options.mjs";
import {
  loadTargetConfiguration,
  selectTarget,
} from "../tools/cross-platform-development-targets/config.mjs";
import {
  provisionDockerTarget,
} from "../tools/cross-platform-development-targets/docker-target.mjs";
import { runChecked } from "../tools/cross-platform-development-targets/process.mjs";
import { createWorkspaceSnapshot } from "../tools/cross-platform-development-targets/snapshot.mjs";
import {
  pushSnapshot,
  runTargetAction,
  scpDownloadCommand,
  sshCommand,
  synchronizeEnvironmentFile,
} from "../tools/cross-platform-development-targets/ssh-target.mjs";

function actionArguments(target, options) {
  return [
    target.paths.repository,
    target.paths.home,
    target.paths.artifacts,
    options.profile,
    options.boxteamHome ?? "",
    options.workspace ?? "",
  ];
}

async function ensureRepository(target, configuration) {
  return runTargetAction({
    target,
    projectRoot: configuration.projectRoot,
    action: "init-repository",
    args: [target.paths.repository, target.paths.artifacts, target.paths.home],
  });
}

async function activateSnapshot(target, configuration, snapshotRef) {
  const selectedRef =
    snapshotRef ??
    (
      await runTargetAction({
        target,
        projectRoot: configuration.projectRoot,
        action: "latest-snapshot",
        args: [target.paths.repository],
      })
    ).stdout;
  const result = await runTargetAction({
    target,
    projectRoot: configuration.projectRoot,
    action: "activate",
    args: [target.paths.repository, selectedRef],
  });
  process.stdout.write(`[target=${target.id}] 已激活 ${selectedRef}: ${result.stdout}\n`);
}

async function synchronizeTarget(target, configuration, options) {
  await ensureRepository(target, configuration);
  const snapshot = createWorkspaceSnapshot({
    projectRoot: configuration.projectRoot,
    maxSnapshotBytes: target.maxSnapshotBytes,
  });
  process.stdout.write(
    `[target=${target.id}] 快照 ${snapshot.commit.slice(0, 12)}: ` +
      `${snapshot.files.length} files, ${snapshot.bytes} bytes\n`,
  );
  pushSnapshot({ target, projectRoot: configuration.projectRoot, snapshot });
  process.stdout.write(`[target=${target.id}] 已推送 ${snapshot.ref}\n`);
  if (options.activate) await activateSnapshot(target, configuration, snapshot.ref);
  if (options.copyEnv) {
    const envResult = await synchronizeEnvironmentFile({
      target,
      projectRoot: configuration.projectRoot,
    });
    process.stdout.write(
      `[target=${target.id}] .env 已校验: ${envResult.bytes} bytes, sha256=${envResult.sha256}\n`,
    );
  }
}

async function openTargetShell(target, options) {
  const remoteCommand = options.passthrough.length > 0 ? options.passthrough.join(" ") : undefined;
  const command = sshCommand(target, remoteCommand ?? "cd && exec \"$SHELL\" -l");
  command.splice(1, 0, "-t");
  runChecked(command, {
    stdout: "inherit",
    stderr: "inherit",
    label: `打开目标 ${target.id} shell`,
  });
}

async function collectTargetArtifacts(target, configuration, options) {
  const extension = target.platform === "windows" ? "zip" : "tar.gz";
  const remoteArchive = `${target.paths.artifacts}/boxteam-target-${target.id}.${extension}`;
  await runTargetAction({
    target,
    projectRoot: configuration.projectRoot,
    action: "prepare-collect",
    args: [target.paths.artifacts, remoteArchive],
  });
  const outputRoot = path.resolve(
    options.output ??
      path.join(
        configuration.projectRoot,
        "out",
        "cross-platform-dev-targets",
        target.id,
        "collected",
      ),
  );
  mkdirSync(outputRoot, { recursive: true });
  const localArchive = path.join(outputRoot, path.basename(remoteArchive));
  runChecked(scpDownloadCommand(target, remoteArchive, localArchive), {
    cwd: configuration.projectRoot,
    label: `收集目标 ${target.id} 产物`,
  });
  process.stdout.write(`[target=${target.id}] 产物已保存: ${localArchive}\n`);
}

export async function runTargetCommand(args, { projectRoot = process.cwd() } = {}) {
  const parsed = parseTargetCliArgs(args);
  if (parsed.help) {
    process.stdout.write(`${targetCliUsage()}\n`);
    return;
  }
  const configuration = loadTargetConfiguration({
    configPath: parsed.options.configPath,
    projectRoot,
  });
  const target = selectTarget(configuration, parsed.targetId);
  const { command, options } = parsed;
  process.stdout.write(
    `[target=${target.id} platform=${target.platform} stage=${command}] 开始\n`,
  );

  if (command === "provision") {
    if (target.provisioner !== "docker") {
      throw new Error(`目标 ${target.id} 由外部 provisioner 管理，不能自动创建`);
    }
    provisionDockerTarget({
      target,
      projectRoot: configuration.projectRoot,
      rebuild: options.rebuild,
    });
    await ensureRepository(target, configuration);
    process.stdout.write(`[target=${target.id}] Docker/Linux 目标已就绪\n`);
    return;
  }
  if (command === "sync") {
    await synchronizeTarget(target, configuration, options);
    return;
  }
  if (command === "activate") {
    await activateSnapshot(target, configuration);
    return;
  }
  if (command === "bootstrap") {
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "bootstrap",
      args: [target.paths.repository, options.submodules ? "1" : "0"],
    });
    process.stdout.write(`[target=${target.id}] 独立 .venv 与 Bun 依赖已初始化\n`);
    return;
  }
  if (command === "start") {
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "start",
      args: actionArguments(target, options),
    });
    process.stdout.write(`[target=${target.id}] ${options.profile} profile 已启动\n`);
    return;
  }
  if (command === "stop") {
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "stop",
      args: [options.profile],
    });
    process.stdout.write(`[target=${target.id}] ${options.profile} profile 已停止\n`);
    return;
  }
  if (command === "restart") {
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "stop",
      args: [options.profile],
    });
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "start",
      args: actionArguments(target, options),
    });
    process.stdout.write(`[target=${target.id}] ${options.profile} profile 已重启\n`);
    return;
  }
  if (command === "status") {
    const result = await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "status",
      args: [options.profile],
    });
    process.stdout.write(`${result.stdout}\n`);
    return;
  }
  if (command === "shell") {
    await openTargetShell(target, options);
    return;
  }
  if (command === "test") {
    await runTargetAction({
      target,
      projectRoot: configuration.projectRoot,
      action: "test",
      args: [
        target.paths.repository,
        target.paths.home,
        options.profile,
        options.boxteamHome ?? "",
        options.workspace ?? "",
        ...options.passthrough,
      ],
    });
    return;
  }
  if (command === "collect") {
    await collectTargetArtifacts(target, configuration, options);
  }
}

if (import.meta.main) {
  try {
    await runTargetCommand(process.argv.slice(2), { projectRoot: process.cwd() });
  } catch (error) {
    process.stderr.write(
      `[cross-platform-development-target] ${
        error instanceof Error ? error.message : String(error)
      }\n`,
    );
    process.exitCode = 1;
  }
}
