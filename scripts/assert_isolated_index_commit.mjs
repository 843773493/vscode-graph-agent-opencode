#!/usr/bin/env node
/**
 * 多 agent 共享工作树时的隔离索引提交防线。
 *
 * 背景：本仓实测发生过三次「陈旧索引快照吞掉并发提交」的真实内容覆盖事故
 * （bb3f3812 吞 90130062、3525edd0 吞 73e06164、另有 session_service 一笔），
 * 起因都是 `GIT_INDEX_FILE=... git read-tree HEAD` 取到的 HEAD 快照早于
 * 提交瞬间的 HEAD，而 commit 只提交索引内容，于是「他人刚提交的新增」被
 * 当作「相对本索引的删除」前向还原。
 *
 * 本脚本在提交前后各做一次机械核对，不碰共享 .git/index，也不改写历史：
 *   1. 记录 read-tree 时的 HEAD；
 *   2. 提交前用 `git diff --cached --name-only` 列出待提交面；
 *   3. 提交后校验该提交是当前 HEAD 的祖先，且没有再吞掉并发提交。
 *
 * 用法（A 阶段，read-tree 之后、add 之前）：
 *   node scripts/assert_isolated_index_commit.mjs record --index /tmp/task.idx --task task_name
 * 用法（B 阶段，commit 之后）：
 *   node scripts/assert_isolated_index_commit.mjs verify --index /tmp/task.idx --task task_name
 *
 * 退出码 0 表示核对通过；非 0 会打印可读的失败原因（绝不静默通过）。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const args = process.argv.slice(2);
const mode = args[0];
const option = (name) => {
  const at = args.indexOf(`--${name}`);
  return at === -1 ? null : (args[at + 1] ?? null);
};

const indexFile = option("index");
const task = option("task");
if (!["record", "verify"].includes(mode) || !indexFile || !task) {
  console.error(
    "用法: node scripts/assert_isolated_index_commit.mjs <record|verify> --index <idx> --task <name>",
  );
  process.exit(2);
}

// 强制走本任务自己的隔离索引：本仓共享 .git/index 已被并发 agent 污染
// （实测 `git diff --cached` 会列出 679 个与真实提交面无关的文件）。
const git = (gitArgs) =>
  execFileSync("git", gitArgs, {
    encoding: "utf-8",
    env: { ...process.env, GIT_INDEX_FILE: indexFile },
  }).trim();
const stateFile = `${dirname(indexFile)}/${task}.isolated-index.json`;

if (mode === "record") {
  const snapshot = {
    task,
    readTreeHead: git(["rev-parse", "HEAD"]),
    staged: git(["diff", "--cached", "--name-only"]),
    recordedAt: new Date().toISOString(),
  };
  mkdirSync(dirname(stateFile), { recursive: true });
  writeFileSync(stateFile, `${JSON.stringify(snapshot, null, 2)}\n`, "utf-8");
  console.log(`已记录 read-tree 快照: HEAD=${snapshot.readTreeHead}`);
  console.log(`待提交面 ${snapshot.staged ? snapshot.staged.split("\n").length : 0} 个文件`);
  process.exit(0);
}

let recorded;
try {
  recorded = JSON.parse(readFileSync(stateFile, "utf-8"));
} catch (error) {
  console.error(`缺少 record 阶段快照 ${stateFile}: ${error.message}`);
  process.exit(3);
}

const currentHead = git(["rev-parse", "HEAD"]);
const failures = [];

if (recorded.readTreeHead !== currentHead) {
  failures.push(
    `read-tree 快照 HEAD (${recorded.readTreeHead}) 与当前 HEAD (${currentHead}) 不同：` +
      "期间有并发提交落地。若你已 commit，请核对是否吞掉了他人新增；若尚未 commit，必须重做 read-tree。",
  );
}

const changedByHead = git(["diff", "--name-only", `${recorded.readTreeHead}`, currentHead]);
const concurrent = new Set(
  changedByHead ? changedByHead.split("\n").filter(Boolean) : [],
);
const myFiles = new Set(
  recorded.staged ? recorded.staged.split("\n").filter(Boolean) : [],
);
const swallowed = [...concurrent].filter((file) => myFiles.has(file));
if (swallowed.length > 0) {
  failures.push(
    `以下文件既在并发提交中变更、又在你的提交面内，可能发生了内容覆盖：\n  - ${swallowed.join(
      "\n  - ",
    )}\n请用 git show 逐笔核对；确认被吞时按 AGENTS.md 以新提交前向恢复（不得 --amend）。`,
  );
}

if (failures.length > 0) {
  console.error("隔离索引核对失败：");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log(`隔离索引核对通过：提交面 ${myFiles.size} 个文件，无并发覆盖迹象。`);
process.exit(0);
