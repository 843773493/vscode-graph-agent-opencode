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

  test("按 MIME 前缀识别图片与视频，未知视频类型回退为 file", () => {
    const image = new File(["img"], "照片.png", { type: "image/png" });
    const mp4 = new File(["v"], "片段.mp4", { type: "video/mp4" });
    const unsupportedVideo = new File(["v"], "片段.avi", { type: "video/x-msvideo" });

    expect(detectAttachmentMediaKind(image)).toBe("image");
    expect(detectAttachmentMediaKind(mp4)).toBe("video");
    expect(detectAttachmentMediaKind(unsupportedVideo)).toBe("file");
  });

  test("缺少可用 MIME 时按视频扩展名回退识别", () => {
    const byExtension = new File(["v"], "录屏.mkv", { type: "" });

    expect(detectAttachmentMediaKind(byExtension)).toBe("video");
  });
});
