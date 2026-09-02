import { describe, expect, test } from "bun:test";
import { detectAttachmentMediaKind } from "./mediaAttachments";

describe("mediaAttachments", () => {
  test("把 PDF 等通用文件归入 file，不伪造图片或视频类型", () => {
    const pdf = new File(["%PDF-1.7"], "报告.pdf", {
      type: "application/pdf",
    });
    const unknown = new File(["plain text"], "说明.custom", {
      type: "application/octet-stream",
    });

    expect(detectAttachmentMediaKind(pdf)).toBe("file");
    expect(detectAttachmentMediaKind(unknown)).toBe("file");
  });

  test("仍然保留明确支持的视频扩展名识别", () => {
    const video = new File(["video"], "录屏.mkv", {
      type: "video/x-matroska",
    });

    expect(detectAttachmentMediaKind(video)).toBe("video");
  });
});
