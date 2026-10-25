import { expect, test } from "bun:test";

import { operationDependencyFrontier } from "./intentStore";

test("文件意图依赖前沿保留未被后继覆盖的最近操作", () => {
  const related: Parameters<typeof operationDependencyFrontier>[0] = [
    { intent: { client_operation_id: "op_4", depends_on: ["external"] } },
    { intent: { client_operation_id: "op_3", depends_on: ["op_2", "external"] } },
    { intent: { client_operation_id: "op_2", depends_on: ["op_1"] } },
    { intent: { client_operation_id: "op_1", depends_on: [] } },
  ];

  expect(operationDependencyFrontier(related)).toEqual(["op_4", "op_3"]);
  expect(operationDependencyFrontier([])).toEqual([]);
});
