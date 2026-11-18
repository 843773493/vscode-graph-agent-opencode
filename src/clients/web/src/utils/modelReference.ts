import type { ModelReference } from "../types/backend";

export function sameModel(
  left: ModelReference | null | undefined,
  right: ModelReference | null | undefined,
): boolean {
  return left != null && right != null
    && left.provider_id === right.provider_id && left.model_id === right.model_id;
}
