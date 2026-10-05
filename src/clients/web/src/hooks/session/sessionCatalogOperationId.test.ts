import { describe, expect, test } from "bun:test";
import { createSessionCatalogOperationId } from "./sessionCatalogOperationId";

describe("会话目录 operation identity", () => {
  test("生成 op_ 前缀的 UUIDv7 profile", () => {
    const operationId = createSessionCatalogOperationId();

    expect(operationId).toMatch(/^op_[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$/);
    expect(Number.parseInt(operationId.slice(3, 15), 16)).toBeGreaterThan(0);
  });
});
