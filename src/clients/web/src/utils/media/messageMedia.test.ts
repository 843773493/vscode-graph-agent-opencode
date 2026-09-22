import { describe, expect, test } from "bun:test";
import { buildMessageMediaItems, messageMediaKind } from "./messageMedia";

describe("消息媒体附件分类", () => {
  test("为图片、音频和视频保留统一媒体类型", () => {
    expect(messageMediaKind({ file_id: "a", content_type: "image/png" })).toBe("image");
    expect(messageMediaKind({ file_id: "b", content_type: "audio/mpeg" })).toBe("audio");
    expect(messageMediaKind({ file_id: "c", content_type: "video/mp4" })).toBe("video");
  });

  test("同一文件出现多次时生成不同的渲染标识", () => {
    const items = buildMessageMediaItems([
      { file_id: "same", content_type: "image/png", name: "first.png" },
      { file_id: "same", content_type: "image/png", name: "second.png" },
    ]);

    expect(items[0].id).not.toBe(items[1].id);
  });
});
