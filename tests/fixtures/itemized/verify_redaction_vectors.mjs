import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { canonicalize, digest } from './verify_hash_vectors.mjs';

// 仅验证冻结的文本 golden；不导入 Python，不自动更新预期。
const path = resolve(process.cwd(), 'tests/fixtures/itemized/hash_redaction_vectors.json');
const fixture = JSON.parse(readFileSync(path, 'utf8'));
for (const vector of fixture.vectors) {
  assert.equal(canonicalize(vector.value), vector.canonical, vector.id);
  const marker = {
    redaction_class: vector.redaction_class,
    content_length: Buffer.byteLength(
      typeof vector.value === 'string' ? vector.value : canonicalize(vector.value), 'utf8',
    ),
    redacted_stable_digest: 'hmac-sha256:session:v1:' + createHmac(
      'sha256', Buffer.from(vector.key_hex, 'hex'),
    ).update(canonicalize(vector.value), 'utf8').digest('hex'),
  };
  assert.deepEqual(marker, vector.marker, vector.id);
  assert.equal(canonicalize(marker), vector.marker_canonical, vector.id);
  assert.equal(digest(marker), vector.marker_hash, vector.id);
}
console.log(JSON.stringify({ redaction_vectors: fixture.vectors.length }));
