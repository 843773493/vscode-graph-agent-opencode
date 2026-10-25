import { expect, test } from "bun:test";
import type { Message } from "../../types/backend";
import { normalizeTimelineMessage } from "./timelineMessages";

test("用户消息只按顺序把 data image URL 补入附件预览", () => {
  const message = {
    message_id: "msg_image_preview",
    session_id: "ses_image_preview",
    thread_id: "thr_image_preview",
    role: "user",
    content: "查看这两张图片",
    attachments: [{ file_id: "file_first" }, { file_id: "file_second" }],
    created_at: "2026-07-13T00:00:00.000Z",
    updated_at: "2026-07-13T00:00:00.000Z",
    metadata: {
      content_blocks: [
        { type: "text", text: "说明" },
        { type: "image_url", image_url: { url: "https://example.com/image.png" } },
        { type: "image_url", image_url: { url: "data:image/png;base64,first" } },
        { type: "image_url", image_url: { url: "data:image/jpeg;base64,second" } },
      ],
    },
  } satisfies Message;

  const timelineItem = normalizeTimelineMessage(message);

  expect(timelineItem).toMatchObject({
    kind: "message",
    attachments: [
      { file_id: "file_first", data_url: "data:image/png;base64,first" },
      { file_id: "file_second", data_url: "data:image/jpeg;base64,second" },
    ],
  });
});
