import { DatabaseSync } from "node:sqlite";
import { parentPort, workerData } from "node:worker_threads";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { importLegacy } from "./migrations/v3_json_to_v1_sqlite.js";

const { stateDir, stateFile, legacyFile, workspaceId } = workerData;
const schema = readFileSync(new URL("./schema.sql", import.meta.url), "utf8");
let database = null;
const progress = { stage: undefined };

function openDatabase(file) {
  progress.stage = "数据库打开";
  const connection = new DatabaseSync(file);
  try {
    connection.exec("PRAGMA busy_timeout=5000");
    progress.stage = "记录核验";
    const version = connection.prepare("PRAGMA user_version").get().user_version;
    if (version === 0 && connection.prepare("SELECT count(*) AS count FROM sqlite_schema").get().count === 0) {
      connection.exec("BEGIN IMMEDIATE");
      connection.exec(schema);
      connection.prepare("INSERT INTO terminal_state VALUES (1, ?, NULL)").run(workspaceId);
      connection.exec("COMMIT");
    } else if (version !== 1) {
      throw new Error(`终端状态数据库版本不受支持: expected=1, actual=${version}`);
    }
    const owner = connection.prepare("SELECT workspace_id FROM terminal_state WHERE singleton=1").get();
    if (owner?.workspace_id !== workspaceId) throw new Error(`终端状态数据库工作区身份不匹配: expected=${workspaceId}, actual=${owner?.workspace_id}`);
    return connection;
  } catch (error) {
    connection.close();
    throw error;
  }
}

function openCurrent(create) {
  if (database) return database;
  progress.stage = "目录创建";
  mkdirSync(stateDir, { recursive: true });
  if (!existsSync(stateFile)) {
    progress.stage = "记录核验";
    if (existsSync(legacyFile)) throw new Error("终端旧 JSON 状态须在停止服务后通过 storage/migrations/v3_json_to_v1_sqlite.mjs 显式导入");
    if (!create) return null;
  }
  const connection = openDatabase(stateFile);
  progress.stage = "数据库打开";
  try {
    connection.exec("PRAGMA journal_mode=WAL");
  } catch (error) {
    connection.close();
    throw error;
  }
  database = connection;
  return database;
}

function writeBatch(connection, { changed, positions, removed, updatedAt }) {
  progress.stage = "事务写入";
  connection.exec("BEGIN IMMEDIATE");
  try {
    const remove = connection.prepare("DELETE FROM terminal_records WHERE terminal_id=?");
    const put = connection.prepare("INSERT INTO terminal_records VALUES (?,?,?) ON CONFLICT(terminal_id) DO UPDATE SET position=excluded.position, payload=excluded.payload");
    const move = connection.prepare("UPDATE terminal_records SET position=? WHERE terminal_id=?");
    for (const id of removed) remove.run(id);
    for (const { record, position } of changed) {
      progress.stage = "正文编码";
      const encoded = JSON.stringify(record);
      progress.stage = "事务写入";
      put.run(record.terminal_id, position, encoded);
    }
    for (const { id, position } of positions) move.run(position, id);
    connection.prepare("UPDATE terminal_state SET updated_at=? WHERE singleton=1").run(updatedAt ?? null);
    progress.stage = "事务提交";
    connection.exec("COMMIT");
  } catch (error) {
    try {
      connection.exec("ROLLBACK");
    } catch (rollbackError) {
      // 保留原失败阶段；两份原生异常在主线程按发生顺序发布。
      throw Object.assign(error, { rollbackError });
    }
    throw error;
  }
}

parentPort.on("message", ({ id, operation, ...payload }) => {
  try {
    let result;
    if (operation === "import") {
      result = importLegacy({ stateDir, stateFile, legacyFile, workspaceId, openDatabase, writeBatch }, progress);
    } else if (operation === "close") {
      progress.stage = "数据库关闭";
      database?.close();
      database = null;
    } else if (operation === "read") {
      const connection = openCurrent(false);
      progress.stage = "正文解析";
      result = connection
        ? connection.prepare("SELECT payload FROM terminal_records ORDER BY position").all().map(row => JSON.parse(row.payload)) : null;
    } else if (operation === "write") {
      writeBatch(openCurrent(true), payload);
    } else {
      throw new Error(`未知终端存储操作: ${operation}`);
    }
    parentPort.postMessage({ id, result });
    if (operation === "close") parentPort.close();
  } catch (error) {
    const { rollbackError, cleanupError, ...details } = error;
    parentPort.postMessage({ id, error, details, rollbackError, cleanupError, stage: progress.stage });
  }
});
