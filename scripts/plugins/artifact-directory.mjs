import { createHash } from "node:crypto";
import { createWriteStream, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { finished } from "node:stream/promises";
import yazl from "yazl";

export function pluginFiles(root, directory = root) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name);
    const relative = path.relative(root, file).split(path.sep).join("/");
    if (entry.isSymbolicLink()) throw new Error(`插件制品不能包含符号链接: ${relative}`);
    return entry.isDirectory() ? pluginFiles(root, file) : [[file, relative]];
  }).sort((left, right) => Buffer.compare(Buffer.from(left[1]), Buffer.from(right[1])));
}

// 与安装 owner 的原目录摘要合同一致：有序 UTF-8 路径和正文均带 uint64 长度。
export function pluginDirectoryDigest(root) {
  const hash = createHash("sha256");
  const length = value => { const bytes = Buffer.alloc(8); bytes.writeBigUInt64BE(BigInt(value)); return bytes; };
  for (const [file, relative] of pluginFiles(root)) {
    const name = Buffer.from(relative);
    const content = readFileSync(file);
    hash.update(length(name.length)).update(name).update(length(content.length)).update(content);
  }
  return hash.digest("hex");
}

export async function archivePluginDirectory(directory, output) {
  const zip = new yazl.ZipFile();
  for (const [file, relative] of pluginFiles(directory)) zip.addFile(file, relative, { compress: true });
  const stream = createWriteStream(output, { flags: "wx" });
  const closed = finished(stream);
  zip.outputStream.pipe(stream);
  zip.end();
  await closed;
}
