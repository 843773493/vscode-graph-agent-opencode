import { parseArgs } from "node:util";
import { repositoryContext, withPoolLock } from "../launch/worktree/repository.mjs";
import { assertReleaseSource, migrationComparison, recordAcceptedRelease, releaseBaselines } from "./source-policy.mjs";

export async function main(args = process.argv.slice(2), cwd = process.cwd()) {
  const { values, positionals } = parseArgs({ args, allowPositionals: true, options: {
    evidence: { type: "string" }, "accepted-by-user": { type: "boolean" },
  } });
  const [command, ref] = positionals;
  const context = repositoryContext(cwd);
  if (command === "list" && positionals.length === 1) return releaseBaselines(context).slice(0, 2);
  if (command === "compare" && positionals.length <= 2) return migrationComparison(context, ref);
  if (command === "check" && positionals.length === 1) return assertReleaseSource(context.checkout);
  if (command === "record" && positionals.length === 2) return withPoolLock(context, () => recordAcceptedRelease(context, ref, values.evidence, values["accepted-by-user"]));
  throw new Error("用法: policy.mjs list | compare [candidate-ref] | check | record <published-ref> --accepted-by-user --evidence=<完整验收及发布记录>");
}
if (import.meta.main) {
  try { console.log(JSON.stringify(await main(), null, 2)); }
  catch (error) { console.error(error.stack); process.exitCode = 1; }
}
