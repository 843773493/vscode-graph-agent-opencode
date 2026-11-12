import path from "node:path";
import { realpathSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { assertCleanCheckout } from "../launch/worktree/configuration.mjs";
import { commitFor, git, gitText, readMetadata, repositoryContext } from "../launch/worktree/repository.mjs";

const prefix = "refs/boxteam/releases/";

export function releaseBaselines(context) {
  const refs = gitText(context.root, ["for-each-ref", "--format=%(refname)", prefix]).split("\n").filter(Boolean);
  return refs.map((ref) => {
    const tag = gitText(context.root, ["cat-file", "tag", ref]);
    const record = JSON.parse(tag.slice(tag.indexOf("\n\n") + 2));
    if (record.schema_version !== 1 || typeof record.evidence !== "string" || !record.evidence.trim() ||
        !record.accepted_by_user || !record.recorded_at || commitFor(context.root, record.commit) !== record.commit || commitFor(context.root, ref) !== record.commit) throw new Error(`发布基线记录非法: ${ref}`);
    return { ...record, ref };
  }).sort((a, b) => b.recorded_at.localeCompare(a.recorded_at) || b.ref.localeCompare(a.ref));
}

export function recordAcceptedRelease(context, ref, evidence, acceptedByUser) {
  if (!acceptedByUser || !evidence?.trim()) throw new Error("仅在用户完整验收并已用于发布后，显式提供 --accepted-by-user --evidence=<验收及发布记录>");
  const commit = commitFor(context.checkout, ref);
  if (releaseBaselines(context).some((record) => record.commit === commit)) throw new Error("该提交已登记发布基线");
  const record = { schema_version: 1, commit, accepted_by_user: true, evidence, recorded_at: new Date().toISOString() };
  const name = `${prefix}${record.recorded_at.replaceAll(/[:.]/g, "-")}-${randomUUID().slice(0, 8)}`;
  const identity = gitText(context.root, ["var", "GIT_COMMITTER_IDENT"]);
  const tag = git(context.root, ["mktag"], { input: `object ${commit}\ntype commit\ntag ${name.slice(prefix.length)}\ntagger ${identity}\n\n${JSON.stringify(record)}\n` }).stdout.toString().trim();
  git(context.root, ["update-ref", name, tag, ""]);
  return { ...record, ref: name };
}

export function migrationComparison(context, candidate) {
  const baselines = releaseBaselines(context).slice(0, 2);
  const current = baselines[0]?.commit;
  const previous = baselines[1]?.commit;
  const to = candidate ? commitFor(context.checkout, candidate) : current;
  const from = candidate ? current : previous;
  if (!from || !to) return { baselines, from: from ?? null, to: to ?? null, status: "missing-release-baseline", files: [], note: "不能将开发 HEAD/HEAD~1 当作已验收版本；未自动迁移任何数据" };
  return { baselines, from, to, status: "review-required", files: gitText(context.root, ["diff", "--name-status", from, to, "--", "configs", "app", "protocol", "packages/launcher"]).split("\n").filter(Boolean), note: "差异是审查清单；需核对实际读写方、DDL、协议与语义，不代表无需迁移" };
}

export function assertReleaseSource(projectRoot, { development = false } = {}) {
  const context = repositoryContext(projectRoot);
  const requestedRoot = development
    ? realpathSync.native(projectRoot)
    : path.resolve(projectRoot);
  if (context.checkout !== requestedRoot) throw new Error("打包目录必须为 checkout 根目录");
  const commit = gitText(projectRoot, ["rev-parse", "HEAD"]);
  if (development) return { projectRoot, commit, purpose: "development-verification" };
  const metadata = readMetadata(projectRoot);
  if (projectRoot !== path.join(context.pool, "release") || metadata?.role !== "release" || metadata.primary_root !== context.root || metadata.archived) throw new Error("正式发布必须从受管 out/worktrees/release 构建；定向构建验证请显式传 --development");
  if (git(projectRoot, ["symbolic-ref", "-q", "HEAD"], { allowFailure: true }).status === 0) throw new Error("release 必须 detached，先选定明确提交");
  if (metadata.base_commit !== commit) throw new Error("release HEAD 与选定提交不一致，请重新创建候选 worktree");
  assertCleanCheckout(projectRoot);
  return { projectRoot, commit, purpose: "release-candidate" };
}

export function packagingSource(projectRoot, args = process.argv.slice(2)) {
  const development = args.includes("--development");
  const result = assertReleaseSource(projectRoot, { development });
  console.log(`[release-source] ${JSON.stringify(result)}`);
  return args.filter((arg) => arg !== "--development");
}
