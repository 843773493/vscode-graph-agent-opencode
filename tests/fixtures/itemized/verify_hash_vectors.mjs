import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// 独立于 Python：数字和字符串仅使用 ECMAScript JSON.stringify；
// 逐个输出已按 UTF-16 排序的 key，避免 JS 再次按整数属性重排。
export function canonicalizeJcsJson(value) {
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
    key => canonicalizeJcsJson(key) + ':' + canonicalizeJcsJson(value[key]),
  ).join(',') + '}';
}

export function digestJcsJson(value) {
  return digestCanonicalJcsJson(canonicalizeJcsJson(value));
}

function digestCanonicalJcsJson(canonicalJson) {
  const sha256Hex = createHash('sha256')
    .update(canonicalJson, 'utf8')
    .digest('hex');
  return `sha256:jcs:v1:${sha256Hex}`;
}

function decodeVectorInput(vector) {
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

export function verifyHashVectorFixture(fixture) {
  let accepted = 0;
  let rejected = 0;
  for (const vector of fixture.serialization) {
    if (vector.error) {
      assert.throws(
        () => canonicalizeJcsJson(decodeVectorInput(vector)),
        undefined,
        vector.id,
      );
      rejected++;
      continue;
    }
    const value = decodeVectorInput(vector);
    const canonicalJson = canonicalizeJcsJson(value);
    assert.equal(canonicalJson, vector.canonical, vector.id);
    assert.equal(digestCanonicalJcsJson(canonicalJson), vector.hash, vector.id);
    assert.equal(
      canonicalizeJcsJson(JSON.parse(canonicalJson)),
      canonicalJson,
      vector.id,
    );
    accepted++;
  }
  for (const vector of fixture.preimages) {
    const canonicalJson = canonicalizeJcsJson(vector.preimage);
    assert.equal(canonicalJson, vector.canonical, vector.id);
    assert.equal(digestCanonicalJcsJson(canonicalJson), vector.hash, vector.id);
  }
  return { accepted, rejected, preimages: fixture.preimages.length };
}

if (import.meta.main) {
  const fixturePaths = process.argv.slice(2);
  if (fixturePaths.length === 0) fixturePaths.push(
    'tests/fixtures/itemized/hash_vectors.json',
    'tests/fixtures/itemized/resource_activation_vectors.json',
  );
  const totals = { accepted: 0, rejected: 0, preimages: 0 };
  for (const fixturePath of fixturePaths) {
    const resolvedFixturePath = resolve(process.cwd(), fixturePath);
    const fixture = JSON.parse(readFileSync(resolvedFixturePath, 'utf8'));
    const result = verifyHashVectorFixture(fixture);
    for (const key of Object.keys(totals)) totals[key] += result[key];
  }
  console.log(JSON.stringify(totals));
}
