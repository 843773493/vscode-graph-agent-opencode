import { existsSync, mkdirSync, readFileSync, openSync, closeSync, linkSync, unlinkSync } from "node:fs";
import { validateTerminalRecord } from "../../terminalSession.js";

export function importLegacy({ stateDir, stateFile, legacyFile, workspaceId, openDatabase, writeBatch }, progress) {
  progress.stage = "旧记录读取";
  const payload = JSON.parse(readFileSync(legacyFile, "utf8"));
  progress.stage = "记录核验";
  if (payload.schema_version !== 3 || payload.workspace_id !== workspaceId || !Array.isArray(payload.terminals)) {
    throw new Error("终端导入只接受同一工作区的 schema 3 JSON 状态");
  }
  const identities = new Set();
  for (const record of payload.terminals) {
    validateTerminalRecord(record, workspaceId);
    if (typeof record.terminal_id !== "string" || !record.terminal_id || identities.has(record.terminal_id)) throw new Error("终端导入记录身份为空或重复");
    identities.add(record.terminal_id);
  }
  if (existsSync(stateFile)) throw new Error("终端目标数据库已存在，拒绝重复导入或覆盖");
  progress.stage = "目录创建";
  mkdirSync(stateDir, { recursive: true });
  const temporaryFile = `${stateFile}.importing`;
  // 独占临时文件；未发布的旧尝试由操作者核实，不自动覆盖或恢复。
  closeSync(openSync(temporaryFile, "wx"));
  let imported, failure;
  try {
    imported = openDatabase(temporaryFile);
    writeBatch(imported, {
      changed: payload.terminals.map((record, position) => ({ record, position })), positions: [], removed: [], updatedAt: payload.updated_at,
    });
    imported.close();
    imported = null;
    progress.stage = "文件发布";
    // 同文件系统硬链接原子发布且拒绝覆盖，原 JSON 只作为离线原件保留。
    linkSync(temporaryFile, stateFile);
  } catch (error) {
    failure = error;
  } finally {
    try {
      imported?.close();
      unlinkSync(temporaryFile);
    } catch (cleanupError) {
      if (!failure) throw cleanupError;
      failure.cleanupError = cleanupError;
    }
  }
  if (failure) throw failure;
  return { terminals: payload.terminals.length };
}
