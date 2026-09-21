// 消息流模块对外唯一入口：保持拆分前的公共契约不变。
export * from "./types";
export { createMessageStreamState, writeMessageStreamCache } from "./state";
export { applyMessageStreamEvent } from "./eventReducer";
export { applyMessageStreamSnapshot } from "./snapshotHydration";
export { messageStreamToResponseParts } from "./responseProjection";
