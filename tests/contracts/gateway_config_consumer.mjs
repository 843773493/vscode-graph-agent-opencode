// 同一真实HTTP原件分别交给Node、Web二进制绑定及Web JSON绑定，不另拼一份响应。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { create, fromBinary, fromJson, toBinary, toJson } from "@bufbuild/protobuf";
import * as node from "../../src/workspace-services/protocol/generated/boxteam/gateway/v1/public_pb.js";
import * as web from "../../src/clients/web/src/types/protocol_buf_generated/boxteam/gateway/v1/public_pb.js";
import * as json from "../../src/clients/web/src/types/protocol_generated/boxteam/gateway/v1/public.ts";

const input = JSON.parse(await readFile(process.argv[2], "utf8"));
function compare(original, result) {
  for (const [key, value] of Object.entries(original)) {
    // proto可选标量的null不编码；字典中的Value null则必须保留。
    if (value === null) continue;
    if (Array.isArray(value)) {
      assert.equal(result[key].length, value.length);
      value.forEach((item, index) => {
        if (item && typeof item === "object") compare(item, result[key][index]);
        else assert.deepEqual(result[key][index], item);
      });
    } else if (value && typeof value === "object") compare(value, result[key]);
    else assert.deepEqual(typeof value === "number" ? Number(result[key]) : result[key], value, key);
  }
}
for (const [name, sample] of Object.entries(input)) {
  for (const binding of [node, web]) {
    const schema = binding[`${name}Schema`];
    const message = fromJson(schema, sample);
    const decoded = fromBinary(schema, toBinary(schema, message));
    compare(sample, toJson(schema, decoded, { useProtoFieldName: true, alwaysEmitImplicit: true }));
  }
  compare(sample, json[name].fromJSON(json[name].toJSON(json[name].fromJSON(sample))));
}
for (const binding of [node, web]) {
  const schema = binding.GatewayConfigReloadStatusDTOSchema;
  const sample = fromJson(schema, { layer_digests: { missing: null, present: "digest" }, active_revision: 0 });
  const decoded = fromBinary(schema, toBinary(schema, sample));
  assert.deepEqual(toJson(schema, decoded).layerDigests, { missing: null, present: "digest" });
  assert.equal(decoded.activeRevision, 0n);
  assert.equal(create(schema).activeRevision, undefined);
}
console.log(JSON.stringify({ bindings: ["node-buf", "web-buf", "web-json"], messages: Object.keys(input), status: "passed" }));
