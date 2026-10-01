#!/usr/bin/env node
/**
 * 共享工作树下的隔离索引提交防线。
 *
 * 实测事故（2026-10-01，三次同类真实内容覆盖）：
 *   bb3f3812 吞掉 90130062、3525edd0 吞掉 73e06164、3ea48090 反向还原 1287390f。
 * 根因：GIT_INDEX_FILE=... git read-tree HEAD 取到的 HEAD 快照早于 commit 瞬间，
 * 而 commit 只提交索引内容，于是别人刚落地的新增被当作「删除」前向还原。
 *
 * 用法：
 *   # read-tree + 精确 add 之后
 *   node scripts/assert_isolated_index_commit.mjs record --index /tmp/task.idx --task <名>
 *   # commit 之后
 *   node scripts/assert_isolated_index_commit.mjs verify --index /tmp/task.idx --task <名>
 *
 * record 记录 read-tree 时的 HEAD 与待提交面。verify 以本笔提交的父提交为比较
 * 起点，检查「read-tree 之后落地的并发提交改动了哪些文件」与「本笔提交实际改动了
 * 哪些文件」是否重叠——重叠即说明本索引可能把别人的新增当作删除还原了。
 * 关键：必须用本笔提交的实际 diff，而不是 record 时的 staged 列表；staged 里本来
 * 就不会出现「被别人新增、被我顺手还原」的文件（这正是事故的隐蔽之处）。
 *
 * 退出码：0 通过；1 发现风险；2 参数错误；3 缺少 record 快照。
 * 全程强制走 --index 指定的隔离索引，不读取已被并发污染的共享 .git/index。
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

const args = process.argv.slice(2);
const mode = args[0];
const option = (name) => {
  const at = args.indexOf("--" + name);
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

// 强制绑定本任务的隔离索引：共享 .git/index 已被并发 agent 污染（实测
// git diff --cached 会列出数百个与真实提交面无关的文件）。
const git = (gitArgs) =>
  execFileSync("git", gitArgs, {
    encoding: "utf-8",
    env: { ...process.env, GIT_INDEX_FILE: indexFile },
  }).trim();

const isAncestor = (candidate) => {
  try {
    execFileSync("git", ["merge-base", "--is-ancestor", candidate, "HEAD"], {
      stdio: "ignore",
      env: { ...process.env, GIT_INDEX_FILE: indexFile },
    });
    return true;
  } catch {
    return false;
  }
};

const split = (text) => (text ? text.split("\n").filter(Boolean) : []);
// 对可能指向不存在对象的 rev 做容错：返回 null 而不是抛异常，交由上层判定。
const tryGit = (gitArgs) => {
  try {
    return git(gitArgs);
  } catch {
    return null;
  }
};
const stateFile = dirname(indexFile) + "/" + task + ".isolated-index.json";

if (mode === "record") {
  const snapshot = {
    task,
    readTreeHead: git(["rev-parse", "HEAD"]),
    staged: split(git(["diff", "--cached", "--name-only"])),
    recordedAt: new Date().toISOString(),
  };
  mkdirSync(dirname(stateFile), { recursive: true });
  writeFileSync(stateFile, JSON.stringify(snapshot, null, 2) + "\n", "utf-8");
  console.log("已记录 read-tree 快照: HEAD=" + snapshot.readTreeHead);
  console.log("待提交面 " + snapshot.staged.length + " 个文件");
  process.exit(0);
}

let recorded;
try {
  recorded = JSON.parse(readFileSync(stateFile, "utf-8"));
} catch (error) {
  console.error("缺少 record 阶段快照 " + stateFile + ": " + error.message);
  process.exit(3);
}

const committed = option("commit") ?? git(["rev-parse", "HEAD"]);
const baseline = tryGit(["rev-parse", committed + "~1"]);
const failures = [];

if (baseline === null) {
  console.error("提交 " + committed + " 不存在或其父提交无法解析，无法核对。");
  process.exit(1);
}

if (!isAncestor(committed)) {
  failures.push("提交 " + committed + " 不是当前 HEAD 的祖先，历史可能已被改写。");
}

// 方向校验：read-tree 快照必须是提交起点的祖先或等同。否则说明 record 与
// commit 之间发生了历史改写，或传入的快照与提交不匹配，此时 diff 会反向，
// 判定结果没有意义，必须显式拒绝而不是给出一个「看起来通过」的结论。
const snapshotIsAncestor =
  recorded.readTreeHead === baseline ||
  (() => {
    try {
      execFileSync(
        "git",
        ["merge-base", "--is-ancestor", recorded.readTreeHead, baseline],
        { stdio: "ignore", env: { ...process.env, GIT_INDEX_FILE: indexFile } },
      );
      return true;
    } catch {
      return false;
    }
  })();
if (!snapshotIsAncestor) {
  failures.push(
    "read-tree 快照 (" +
      recorded.readTreeHead.slice(0, 8) +
      ") 不是提交起点 (" +
      baseline.slice(0, 8) +
      ") 的祖先：快照与本次提交不匹配，或期间发生了历史改写。请重做 read-tree 流程。",
  );
}

// 本笔提交实际改动的文件：用 diff 而不是 record 时的 staged 列表。
const myFiles = new Set(split(git(["diff", "--name-only", baseline, committed])));
// read-tree 之后、本笔提交起点之前落地的并发提交改动的文件。
const concurrent = snapshotIsAncestor && baseline !== recorded.readTreeHead
  ? split(tryGit(["diff", "--name-only", recorded.readTreeHead, baseline]) ?? "")
  : [];
const swallowed = concurrent.filter((file) => myFiles.has(file));

if (swallowed.length > 0) {
  failures.push(
    "read-tree 快照 (" +
      recorded.readTreeHead.slice(0, 8) +
      ") 与提交起点 (" +
      baseline.slice(0, 8) +
      ") 之间的并发提交改动了以下同文件，而本笔提交也改动了它们，可能发生了" +
      "内容覆盖:\n  - " +
      swallowed.join("\n  - ") +
      "\n请用 git show 逐笔核对；确认被吞时按 AGENTS.md 以新提交前向恢复（不得 --amend）。",
  );
}

if (failures.length > 0) {
  console.error("隔离索引核对失败：");
  for (const failure of failures) {
    console.error("- " + failure);
  }
  process.exit(1);
}

console.log(
  "隔离索引核对通过：提交 " +
    committed.slice(0, 8) +
    "，本笔改动 " +
    myFiles.size +
    " 个文件，并发窗口内改动 " +
    concurrent.length +
    " 个文件，无重叠。",
);
process.exit(0);
