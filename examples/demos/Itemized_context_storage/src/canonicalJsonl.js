import { open, stat } from "node:fs/promises";

export async function assertCanonicalJsonlTail(jsonlPath, indexedEnd) {
  const fileSize = (await stat(jsonlPath)).size;
  if (fileSize !== indexedEnd) {
    throw new Error(
      "canonical JSONL 与 catalog 尾部不一致: indexed_end="
        + indexedEnd
        + ", file_size="
        + fileSize,
    );
  }
}

export async function readCanonicalItemsByCatalogOffsets(jsonlPath, catalogEntries) {
  if (catalogEntries.length === 0) {
    return [];
  }

  const orderedEntries = catalogEntries
    .map((entry, resultIndex) => ({ ...entry, resultIndex }))
    .sort((left, right) => left.jsonl_offset - right.jsonl_offset);
  const ranges = [];
  for (const entry of orderedEntries) {
    const lastRange = ranges.at(-1);
    if (lastRange && lastRange.offset + lastRange.length === entry.jsonl_offset) {
      lastRange.length += entry.jsonl_length;
      lastRange.entries.push(entry);
    } else {
      ranges.push({
        offset: entry.jsonl_offset,
        length: entry.jsonl_length,
        entries: [entry],
      });
    }
  }

  const records = new Array(catalogEntries.length);
  const file = await open(jsonlPath, "r");
  try {
    for (const range of ranges) {
      const bytes = Buffer.allocUnsafe(range.length);
      let bytesRead = 0;
      while (bytesRead < bytes.length) {
        const result = await file.read(
          bytes,
          bytesRead,
          bytes.length - bytesRead,
          range.offset + bytesRead,
        );
        if (result.bytesRead === 0) {
          throw new Error(
            `canonical JSONL 记录不完整: offset=${range.offset}, expected=${bytes.length}`,
          );
        }
        bytesRead += result.bytesRead;
      }
      for (const entry of range.entries) {
        const start = entry.jsonl_offset - range.offset;
        const line = bytes.subarray(start, start + entry.jsonl_length);
        const record = JSON.parse(line.toString("utf8"));
        if (
          record?.item_id !== entry.item_id
          || record?.item_sequence !== entry.item_sequence
        ) {
          throw new Error(
            "canonical JSONL/catalog item 身份不匹配: expected="
              + entry.item_id
              + "/"
              + entry.item_sequence
              + ", actual="
              + (record?.item_id ?? "unknown")
              + "/"
              + (record?.item_sequence ?? "unknown"),
          );
        }
        records[entry.resultIndex] = record;
      }
    }
  } finally {
    await file.close();
  }
  return records;
}
