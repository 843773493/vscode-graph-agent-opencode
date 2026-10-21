import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import Ajv from "ajv";
import {
  SessionContextReadRequest,
  SessionContextItemDTO,
  SessionContextReadResultDTO,
} from "../../../src/clients/web/src/types/protocol_generated/boxteam/workspace/v2/public";

const webSnapshot = readFileSync(path.resolve("src/clients/web/openapi.json"), "utf8");
const tsSnapshot = readFileSync(path.resolve("src/clients/web/src/types/openapi/index.json"), "utf8");
const document = JSON.parse(tsSnapshot);
const views = ["overview", "messages", "records", "information", "inventory", "assembly", "assemblies"];

test("Web 快照与 TS 消费目录中的离线 OpenAPI 完全一致", () => {
  expect(tsSnapshot).toBe(webSnapshot);
});

for (const model of ["SessionContextReadRequest", "SessionContextReadResultDTO"] as const) {
  describe(model, () => {
    const schema = document.components.schemas[model].properties.view;
    const validate = new Ajv().compile(schema);

    test("七种 view 保留既有 active 读取并包含两个冻结检查入口", () => {
      expect(schema.enum).toEqual(views);
      for (const view of views) expect(validate(view)).toBe(true);
    });

    test.each(["assembly", "assemblies"])("Proto 生成的 TS JSON 绑定无损保留 %s", (view) => {
      // OpenAPI 负责枚举约束；公开 TS DTO 继续唯一来自 Proto string 字段。
      const reference = {
        resource_identity: "11111111-1111-5111-8111-111111111111",
        uri: "boxteam://workspace/11111111-1111-4111-8111-111111111111/resources/session/ses_contract",
        scope: "workspace",
        ...(view === "assembly" ? { assembly_ref: "22222222-2222-5222-8222-222222222222" } : {}),
      };
      const wire = { ...reference, view, revision: "sealed-contract", include: [], items: [], partial_errors: [] };
      const projected = model === "SessionContextReadRequest"
        ? SessionContextReadRequest.toJSON(SessionContextReadRequest.fromJSON(wire))
        : SessionContextReadResultDTO.toJSON(SessionContextReadResultDTO.fromJSON(wire));
      expect(projected).toMatchObject({ ...reference, view });
      expect(projected).not.toHaveProperty("resource");
      expect(validate((projected as { view: string }).view)).toBe(true);
    });

    test.each(["assemblie", "Assembly", "", null])("离线枚举拒绝非法 view %j", (view) => {
      expect(validate(view)).toBe(false);
      expect(validate.errors?.some((error) => ["enum", "type"].includes(error.keyword))).toBe(true);
    });
  });
}


test("Proto JSON绑定保留typed封存来源与nullable URI", () => {
  const refs = [null, "boxteam://workspace/test/resources/skills/demo/SKILL.md"].map((uri, index) => ({
    ...(uri === null ? {} : { display_uri: uri }),
    resource_kind: "skills", owner_scope: "session", facet: "activation",
    revision: `sha256:jcs:v1:${"a".repeat(64)}`, availability: "available",
    provenance_ref: { kind: "sealed_resource", token: `sha256:jcs:v1:${String(index).repeat(64)}` },
  }));
  const wire = { kind: "assembly", resource_refs: refs };
  const rebuilt = SessionContextItemDTO.toJSON(SessionContextItemDTO.fromJSON(wire));
  expect(rebuilt).toMatchObject(wire);
});
