import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import path from "node:path";
import Ajv from "ajv";
import {
  SessionContextReadRequest,
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
      const resource = "boxteam://session/ses_contract" + (view === "assembly" ? "#assembly=assembly-contract" : "");
      const wire = { resource, view, revision: "sealed-contract", include: [], items: [], partial_errors: [] };
      const projected = model === "SessionContextReadRequest"
        ? SessionContextReadRequest.toJSON(SessionContextReadRequest.fromJSON(wire))
        : SessionContextReadResultDTO.toJSON(SessionContextReadResultDTO.fromJSON(wire));
      expect(projected).toMatchObject({ resource, view });
      expect(validate((projected as { view: string }).view)).toBe(true);
    });

    test.each(["assemblie", "Assembly", "", null])("离线枚举拒绝非法 view %j", (view) => {
      expect(validate(view)).toBe(false);
      expect(validate.errors?.some((error) => ["enum", "type"].includes(error.keyword))).toBe(true);
    });
  });
}
