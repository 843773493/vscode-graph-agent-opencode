import { lstatSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import { runChecked } from "./process.mjs";

const DEFAULT_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024;

function git(projectRoot, args, { environment = process.env, label } = {}) {
  return runChecked(["git", ...args], {
    cwd: projectRoot,
    environment,
    label: label ?? `git ${args[0]}`,
  }).stdout;
}

export function assertCleanInitializedSubmodules(projectRoot) {
  const status = git(projectRoot, ["submodule", "status", "--recursive"], {
    label: "检查 Git 子模块",
  });
  if (!status) return;
  const problems = [];
  for (const line of status.split(/\r?\n/)) {
    if (!line) continue;
    const state = line[0];
    const fields = line.slice(1).trim().split(/\s+/);
    const submodulePath = fields[1];
    if (!submodulePath || state === "-") continue;
    if (state === "+" || state === "U") {
      problems.push(`${submodulePath} (gitlink 与 HEAD 不一致)`);
      continue;
    }
    const worktreeStatus = git(projectRoot, [
      "-C",
      submodulePath,
      "status",
      "--porcelain",
      "--untracked-files=all",
    ]);
    if (worktreeStatus) problems.push(`${submodulePath} (包含未提交内容)`);
  }
  if (problems.length > 0) {
    throw new Error(`子模块不能安全纳入主仓库快照:\n${problems.join("\n")}`);
  }
}

function stagedPaths(projectRoot, environment) {
  const output = git(projectRoot, ["diff", "--cached", "--name-only", "-z"], {
    environment,
    label: "读取快照文件列表",
  });
  return output.split("\0").filter(Boolean);
}

function snapshotSize(projectRoot, files) {
  let bytes = 0;
  for (const relativePath of files) {
    const absolutePath = path.join(projectRoot, relativePath);
    try {
      const metadata = lstatSync(absolutePath);
      if (metadata.isFile() || metadata.isSymbolicLink()) bytes += metadata.size;
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw new Error(`无法读取快照文件: ${relativePath}: ${error.message}`);
      }
    }
  }
  return bytes;
}

export function createWorkspaceSnapshot({
  projectRoot = process.cwd(),
  maxSnapshotBytes = DEFAULT_MAX_SNAPSHOT_BYTES,
  now = new Date(),
} = {}) {
  const resolvedProjectRoot = path.resolve(projectRoot);
  assertCleanInitializedSubmodules(resolvedProjectRoot);
  const temporaryRoot = mkdtempSync(path.join(os.tmpdir(), "boxteam-git-snapshot-"));
  const indexPath = path.join(temporaryRoot, "index");
  const environment = { ...process.env, GIT_INDEX_FILE: indexPath };
  try {
    git(resolvedProjectRoot, ["read-tree", "HEAD"], {
      environment,
      label: "初始化临时 Git index",
    });
    git(resolvedProjectRoot, ["add", "-A", "--", "."], {
      environment,
      label: "收集脏工作区快照",
    });
    const files = stagedPaths(resolvedProjectRoot, environment);
    const bytes = snapshotSize(resolvedProjectRoot, files);
    if (bytes > maxSnapshotBytes) {
      throw new Error(
        `快照内容过大: ${bytes} bytes，目标上限 ${maxSnapshotBytes} bytes；` +
          "请检查未忽略的大文件",
      );
    }
    const tree = git(resolvedProjectRoot, ["write-tree"], {
      environment,
      label: "写入快照 Git tree",
    });
    const head = git(resolvedProjectRoot, ["rev-parse", "HEAD"], {
      label: "读取当前 Git HEAD",
    });
    const timestamp = now.toISOString().replaceAll(/[-:.TZ]/g, "");
    const commitEnvironment = {
      ...environment,
      GIT_AUTHOR_NAME: "BoxTeam Development Target",
      GIT_AUTHOR_EMAIL: "boxteam-development-target@localhost",
      GIT_COMMITTER_NAME: "BoxTeam Development Target",
      GIT_COMMITTER_EMAIL: "boxteam-development-target@localhost",
    };
    const commit = git(
      resolvedProjectRoot,
      ["commit-tree", tree, "-p", head, "-m", `BoxTeam workspace snapshot ${timestamp}`],
      { environment: commitEnvironment, label: "创建临时快照提交" },
    );
    return {
      commit,
      parent: head,
      tree,
      files,
      bytes,
      ref: `refs/boxteam/snapshots/${timestamp}-${commit.slice(0, 12)}`,
    };
  } finally {
    rmSync(temporaryRoot, { recursive: true, force: true });
  }
}
