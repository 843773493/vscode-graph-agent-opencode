import { createWriteStream } from 'node:fs';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';

/** 按 esbuild 的两级字典流式写入，避免整份 metadata 再占一块连续字符串内存。 */
export async function writeBuildMetadata(path, metadata) {
  async function* chunks() {
    yield '{';
    let sectionSeparator = '';
    for (const [section, items] of Object.entries(metadata)) {
      yield `${sectionSeparator}${JSON.stringify(section)}:{`;
      let itemSeparator = '';
      for (const [key, value] of Object.entries(items)) {
        yield `${itemSeparator}${JSON.stringify(key)}:${JSON.stringify(value)}`;
        itemSeparator = ',';
      }
      yield '}'; sectionSeparator = ',';
    }
    yield '}';
  }
  await pipeline(Readable.from(chunks()), createWriteStream(path));
}
