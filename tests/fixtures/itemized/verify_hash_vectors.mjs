import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// 独立于 Python：数字和字符串仅使用 ECMAScript JSON.stringify；
// 逐个输出已按 UTF-16 排序的 key，避免 JS 再次按整数属性重排。
export function canonicalize(value) {
  if (typeof value === 'string') {
    if (!value.isWellFormed()) throw new TypeError('invalid Unicode');
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('non-finite number');
    return JSON.stringify(value);
  }
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(canonicalize).join(',') + ']';
  if (typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) {
    throw new TypeError('not a JSON value');
  }
  return '{' + Object.keys(value).sort().map(
    key => canonicalize(key) + ':' + canonicalize(value[key]),
  ).join(',') + '}';
}

export function digest(value) {
  return 'sha256:jcs:v1:' + createHash('sha256').update(canonicalize(value), 'utf8').digest('hex');
}

function valueFor(vector) {
  if (vector.input_kind === 'binary64') {
    return Buffer.from(vector.input, 'hex').readDoubleBE(0);
  }
  if (vector.input_kind === 'integer') {
    // Python int 入口要求无损；浮点恢复还允许 JCS 自身输出的十进制整数。
    const exact = BigInt(vector.input);
    const number = Number(exact);
    if (!Number.isFinite(number) || (
      BigInt(number) !== exact && JSON.stringify(number) !== vector.input
    )) throw new TypeError('integer cannot be represented losslessly');
    return number;
  }
  assert.equal(vector.input_kind, 'json');
  return JSON.parse(vector.input);
}

export function verifyVectors(vectors) {
  let accepted = 0;
  let rejected = 0;
  for (const vector of vectors.serialization) {
    if (vector.error) {
      assert.throws(() => canonicalize(valueFor(vector)), undefined, vector.id);
      rejected++;
      continue;
    }
    const value = valueFor(vector);
    assert.equal(canonicalize(value), vector.canonical, vector.id);
    assert.equal(digest(value), vector.hash, vector.id);
    assert.equal(canonicalize(JSON.parse(vector.canonical)), vector.canonical, vector.id);
    accepted++;
  }
  for (const vector of vectors.preimages) {
    assert.equal(canonicalize(vector.preimage), vector.canonical, vector.id);
    assert.equal(digest(vector.preimage), vector.hash, vector.id);
  }
  return { accepted, rejected, preimages: vectors.preimages.length };
}

if (import.meta.main) {
  const path = resolve(process.cwd(), process.argv[2] ?? 'tests/fixtures/itemized/hash_vectors.json');
  const vectors = JSON.parse(readFileSync(path, 'utf8'));
  console.log(JSON.stringify(verifyVectors(vectors)));
}
