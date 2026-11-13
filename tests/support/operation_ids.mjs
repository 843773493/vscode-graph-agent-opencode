import { randomUUID } from "node:crypto";

export function newOperationId() {
  return `op_${randomUUID().replaceAll("-", "")}`;
}
