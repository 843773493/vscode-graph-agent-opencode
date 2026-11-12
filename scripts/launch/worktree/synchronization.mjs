import { existsSync, lstatSync, rmSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { assertNoLinkedParents, atomicJson, git, gitText, metadataPath, relativeFile } from "./repository.mjs";

function privateIndex(checkout, action) {
  const index = path.join(gitText(checkout, ["rev-parse", "--absolute-git-dir"]), `boxteam-index-${randomUUID()}`);
  const run = (args, options = {}) => git(checkout, args, { ...options, environment: { GIT_INDEX_FILE: index } });
  try { return action(run); }
  finally { rmSync(index, { force: true }); rmSync(`${index}.lock`, { force: true }); }
}

function snapshot(checkout, files) {
  for (const file of files) {
    const absolute = path.join(checkout, file);
    assertNoLinkedParents(checkout, absolute);
    if (existsSync(absolute) && !lstatSync(absolute).isFile()) throw new Error(`只能同步普通文件或删除: ${file}`);
    if (gitText(checkout, ["ls-files", "--others", "--ignored", "--exclude-standard", "--", file]) &&
        git(checkout, ["ls-files", "--error-unmatch", "--", file], { allowFailure: true }).status !== 0) {
      throw new Error(`拒绝同步忽略文件（本地配置使用创建时复制）: ${file}`);
    }
  }
  return privateIndex(checkout, (run) => {
    run(["read-tree", "HEAD"]);
    // 只将确实存在于基线或磁盘的文件交给 add，允许重复同步删除。
    const selected = files.filter((file) => existsSync(path.join(checkout, file)) || entry(checkout, "HEAD", file));
    if (selected.length) run(["add", "-A", "--", ...selected]);
    return run(["write-tree"]).stdout.toString().trim();
  });
}

function entry(checkout, tree, file) {
  const raw = git(checkout, ["ls-tree", "-z", tree, "--", file]).stdout.toString();
  if (!raw) return null;
  const match = /^(\d+) blob ([a-f0-9]+)\t/.exec(raw);
  if (!match) throw new Error(`同步路径不是普通 blob: ${file}`);
  return { mode: match[1], hash: match[2] };
}

function same(first, second) { return JSON.stringify(first) === JSON.stringify(second); }

export function synchronize(context, target, metadata, inputFiles) {
  if (metadata.role === "release") throw new Error("release 不接收未提交源码");
  const files = [...new Set(inputFiles.map(relativeFile))];
  if (!files.length) throw new Error("sync 必须显式指定完整切片的文件");
  if (gitText(target, ["diff", "--cached", "--name-only", "--", ...files])) throw new Error("目标选中文件已有暂存改动，请先处理");
  if (metadata.sync_source && metadata.sync_source !== context.root) throw new Error("同步来源与已有记录不一致");
  const sourceHead = gitText(context.root, ["rev-parse", "HEAD"]);
  const sourceTree = snapshot(context.root, files);
  const targetTree = snapshot(target, files);
  const expected = {};
  const desired = {};
  for (const file of files) {
    expected[file] = Object.hasOwn(metadata.synced_files ?? {}, file) ? metadata.synced_files[file] : entry(context.root, sourceHead, file);
    desired[file] = entry(context.root, sourceTree, file);
    const actual = entry(target, targetTree, file);
    if (!same(actual, expected[file]) && !same(actual, desired[file])) throw new Error(`目标存在重叠修改或基础不兼容: ${file}`);
  }
  // 从目标当前内容构造差异，只覆盖已经核对过的明确文件；源共享 index 从不参与写入。
  const desiredTree = privateIndex(target, (run) => {
    run(["read-tree", targetTree]);
    for (const file of files) {
      const selected = desired[file];
      run(selected ? ["update-index", "--add", "--cacheinfo", `${selected.mode},${selected.hash},${file}`]
        : ["update-index", "--force-remove", "--", file]);
    }
    return run(["write-tree"]).stdout.toString().trim();
  });
  const patch = git(context.root, ["diff", "--binary", "--full-index", targetTree, desiredTree, "--", ...files]).stdout;
  if (gitText(context.root, ["rev-parse", "HEAD"]) !== sourceHead || snapshot(context.root, files) !== sourceTree || snapshot(target, files) !== targetTree) {
    throw new Error("同步准备期间源码变化，请重试本次切片");
  }
  if (patch.length) {
    git(target, ["apply", "--check", "--binary", "--whitespace=nowarn", "-"], { input: patch });
    git(target, ["apply", "--binary", "--whitespace=nowarn", "-"], { input: patch });
  }
  atomicJson(metadataPath(target), { ...metadata, sync_source: context.root, synced_files: { ...metadata.synced_files, ...desired } });
  return { target, source_commit: sourceHead, files, changed: patch.length > 0 };
}
