import { browserViewKey, openBrowserViewDatabase, updateBrowserView } from "../browserViewStore";
import { openWorkspaceFileIntentDatabase } from "../workspaceFiles/intentStore";
import { openCatalogOutboxDatabase } from "../session/sessionCatalogOutboxStore";
import { planBrowserViewRecovery, type RecoveryIssue } from "./browserViews";
import { transformIdentityStores, type IdentityUpgrade, type StoredEntries } from "./indexedDb";
import { parseWorkspaceIdentityRecoveries, record, type WorkspaceIdentityRecovery } from "./material";
import { recoverCatalogPartition, recoverFileIntent, recoverFileScopeKey } from "./queues";

export interface RecoveryBatch {
  storage: string;
  changed: number;
  issues: RecoveryIssue[];
}
export interface WorkspaceIdentityRecoveryReport {
  migrations: { migration_id: string; registry_gateway_id: string }[];
  batches: RecoveryBatch[];
}

async function recoverViews(materials: readonly WorkspaceIdentityRecovery[]): Promise<RecoveryBatch> {
  const database = await openBrowserViewDatabase();
  return new Promise((resolve, reject) => {
    const transaction = database.transaction("views", "readwrite");
    let batch: RecoveryBatch = { storage: "boxteam-browser-views", changed: 0, issues: [] };
    let failure: Error | undefined;
    transformIdentityStores(transaction, ["views"], entries => {
      const stored = entries.get("views")!;
      const values = new Map(stored.keys.flatMap((key, index) => typeof key === "string" ? [[key, stored.values[index]] as const] : []));
      const store = transaction.objectStore("views");
      for (const material of materials) {
        const plan = planBrowserViewRecovery(values, material);
        for (const change of plan.changes) if (change.before !== change.after) store.delete(change.before);
        for (const change of plan.changes) store.put(change.value, change.after);
        batch.changed += plan.changes.length;
        batch.issues.push(...plan.issues);
      }
    }, error => { failure = error; });
    transaction.oncomplete = () => resolve(batch);
    transaction.onabort = () => reject(failure ?? transaction.error ?? new Error("浏览器视图身份恢复事务已中止"));
  });
}

interface Rewrite { before: IDBValidKey; after: IDBValidKey; value: Record<string, unknown> }

function queuePlan(entries: StoredEntries, convert: (value: unknown) => Record<string, unknown>, key: (value: Record<string, unknown>) => IDBValidKey,
  issues: RecoveryIssue[]): Rewrite[] {
  const targets = new Set<string>();
  return entries.values.flatMap((value, index) => {
    const before = entries.keys[index]!;
    try {
      const original = record(value, "队列记录");
      if (JSON.stringify(key(original)) !== JSON.stringify(before)) throw new Error("队列原主键与正文不一致");
      const converted = convert(value), after = key(converted), identity = JSON.stringify(after);
      if (targets.has(identity)) throw new Error(`多个旧分区合并后发生主键冲突：${identity}`);
      targets.add(identity);
      return [{ before, after, value: converted }];
    } catch (error) {
      issues.push({ key: JSON.stringify(before), message: error instanceof Error ? error.message : String(error) });
      return [];
    }
  });
}

function writeQueue(transaction: IDBTransaction, name: string, rewrites: Rewrite[]): void {
  const store = transaction.objectStore(name);
  for (const rewrite of rewrites) if (JSON.stringify(rewrite.before) !== JSON.stringify(rewrite.after)) store.delete(rewrite.before);
  for (const rewrite of rewrites) store.put(rewrite.value);
}

function validateSequences(records: Rewrite[], sequences: Rewrite[], partition: string, nextField: string, operationSequence: (value: Record<string, unknown>) => number): void {
  const seen = new Set<string>(), high = new Map<string, number>();
  for (const entry of sequences) {
    const next = entry.value[nextField];
    if (!Number.isSafeInteger(next) || (next as number) < 1) throw new Error("队列序号高水位无效");
    high.set(entry.value[partition] as string, next as number);
  }
  for (const entry of records) {
    const sequence = operationSequence(entry.value), owner = entry.value[partition] as string;
    if (!Number.isSafeInteger(sequence) || sequence < 1 || !high.has(owner) || high.get(owner)! <= sequence) throw new Error("队列序号与原高水位不一致");
    const key = JSON.stringify([owner, sequence]);
    if (seen.has(key)) throw new Error("旧分区合并后存在重复 client_sequence，拒绝重排原命令");
    seen.add(key);
  }
}

async function recoverQueue(materials: readonly WorkspaceIdentityRecovery[], kind: "files" | "catalog"): Promise<RecoveryBatch> {
  const batch: RecoveryBatch = { storage: kind === "files" ? "boxteam-workspace-file-intents" : "boxteam-session-catalog-outbox", changed: 0, issues: [] };
  let failure: Error | undefined;
  const recordsName = kind === "files" ? "intents" : "operations", sequencesName = kind === "files" ? "sequences" : "partition-sequences";
  const upgrade: IdentityUpgrade = transaction => transformIdentityStores(transaction, [recordsName, sequencesName], entries => {
    const records = queuePlan(entries.get(recordsName)!, value => {
      if (kind === "files") return recoverFileIntent(value, materials);
      const row = record(value, "目录命令");
      const operation = record(row.operation, "目录操作");
      if (operation.client_sequence !== row.client_sequence) throw new Error("目录命令原 client_sequence 不一致");
      return { ...row, partition_key: recoverCatalogPartition(row.partition_key, materials) };
    }, row => kind === "files" ? [row.scopeKey as string, record(row.intent, "文件 intent").client_operation_id as string]
      : [row.partition_key as string, row.client_sequence as number], batch.issues);
    const sequences = queuePlan(entries.get(sequencesName)!, value => {
      const row = record(value, "队列高水位");
      return kind === "files" ? { ...row, scopeKey: recoverFileScopeKey(row.scopeKey, materials) }
        : { ...row, partition_key: recoverCatalogPartition(row.partition_key, materials) };
    }, row => row[kind === "files" ? "scopeKey" : "partition_key"] as string, batch.issues);
    if (batch.issues.length) throw new Error("队列归属恢复未完成，原数据库批次已保留");
    validateSequences(records, sequences, kind === "files" ? "scopeKey" : "partition_key", kind === "files" ? "next" : "next_client_sequence",
      row => kind === "files" ? record(row.intent, "文件 intent").client_sequence as number : row.client_sequence as number);
    writeQueue(transaction, recordsName, records);
    writeQueue(transaction, sequencesName, sequences);
    batch.changed = records.length + sequences.length;
    if (kind === "catalog") for (const material of materials) {
      transaction.objectStore("identity-recovery").put({ migration_id: material.migration_id,
        registry_gateway_id: material.registry_gateway_id, records: records.filter(entry => Array.isArray(entry.before)
          && String(entry.before[0]).startsWith(`${material.registry_gateway_id}\u0000`)).length });
    }
  }, error => { failure = error; });
  try {
    const database = await (kind === "files" ? openWorkspaceFileIntentDatabase(upgrade) : openCatalogOutboxDatabase(upgrade));
    database.close();
  } catch (error) {
    batch.changed = 0;
    if (!batch.issues.length) batch.issues.push({ key: batch.storage, message: (failure ?? error) instanceof Error ? (failure ?? error as Error).message : String(error) });
  }
  return batch;
}

/** 只由显式导入命令调用；各存储原子提交，重入不重复转换新格式。 */
export async function recoverWorkspaceIdentity(value: unknown): Promise<WorkspaceIdentityRecoveryReport> {
  const materials = parseWorkspaceIdentityRecoveries(value);
  const report: WorkspaceIdentityRecoveryReport = { migrations: materials.map(({ migration_id, registry_gateway_id }) => ({ migration_id, registry_gateway_id })), batches: [] };
  try { report.batches.push(await recoverViews(materials)); }
  catch (error) { report.batches.push({ storage: "boxteam-browser-views", changed: 0, issues: [{ key: "views", message: error instanceof Error ? error.message : String(error) }] }); }
  report.batches.push(await recoverQueue(materials, "files"));
  report.batches.push(await recoverQueue(materials, "catalog"));
  for (const material of materials) await updateBrowserView(browserViewKey(material.registry_gateway_id, "workspace-identity-recovery", material.migration_id), () => report);
  return report;
}
