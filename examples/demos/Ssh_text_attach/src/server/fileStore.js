import { mkdir, open, stat, writeFile } from "node:fs/promises";
import path from "node:path";

export class TextFileStore {
  constructor({ filePath, label }) {
    if (!filePath) {
      throw new Error("TextFileStore 缺少 filePath");
    }
    if (!label) {
      throw new Error("TextFileStore 缺少 label");
    }
    this.filePath = path.resolve(filePath);
    this.label = label;
    this.filePreparation = null;
    this.fileOperation = Promise.resolve();
  }

  initialFileContent() {
    return `${this.label}\n\n这是 Ssh_text_attach demo 管理的 txt 文件。\n首次创建: ${new Date().toISOString()}\n`;
  }

  async ensureFileExists() {
    if (!this.filePreparation) {
      this.filePreparation = this.#prepareFile();
    }
    const filePreparation = this.filePreparation;
    try {
      await filePreparation;
    } finally {
      if (this.filePreparation === filePreparation) {
        this.filePreparation = null;
      }
    }
  }

  async #prepareFile() {
    await mkdir(path.dirname(this.filePath), { recursive: true });
    try {
      await stat(this.filePath);
      return;
    } catch (error) {
      if (error?.code !== "ENOENT") {
        throw error;
      }
    }

    try {
      await writeFile(this.filePath, this.initialFileContent(), {
        encoding: "utf8",
        flag: "wx",
      });
    } catch (error) {
      if (error?.code !== "EEXIST") {
        throw error;
      }
    }
  }

  async snapshot() {
    return this.#enqueueFileOperation(async () => {
      await this.ensureFileExists();
      return this.#readSnapshot();
    });
  }

  async #readSnapshot() {
    const file = await open(this.filePath, "r");
    try {
      const [content, metadata] = await Promise.all([
        file.readFile("utf8"),
        file.stat(),
      ]);
      return this.#createSnapshot(content, metadata);
    } finally {
      await file.close();
    }
  }

  #createSnapshot(content, metadata) {
    return {
      path: this.filePath,
      content,
      updatedAt: metadata.mtime.toISOString(),
      bytes: metadata.size,
    };
  }

  async save(content) {
    if (typeof content !== "string") {
      throw new Error("保存内容必须是字符串");
    }
    return this.#enqueueFileOperation(async () => {
      await this.ensureFileExists();
      const encodedContent = Buffer.from(content, "utf8");
      await writeFile(this.filePath, encodedContent);
      return this.#createSnapshot(encodedContent.toString("utf8"), await stat(this.filePath));
    });
  }

  #enqueueFileOperation(operation) {
    const fileOperation = this.fileOperation.then(operation);
    this.fileOperation = fileOperation.catch(() => undefined);
    return fileOperation;
  }
}
