import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import MessageAttachments from "./MessageAttachments";
import type { AttachmentRef, TurnAttachment } from "../../types/backend";

const image: AttachmentRef = {
  file_id: "boxteam-session://ses_test/attachments/image.png",
  name: "image.png",
  content_type: "image/png",
  data_url: "data:image/png;base64,preview",
};

describe("MessageAttachments", () => {
  test("历史 owner 标记不可用的附件直接显示状态并禁用打开", () => {
    const attachment: TurnAttachment = { file_id: image.file_id, name: image.name, content_type: image.content_type, availability: "unavailable" };
    let renderer!: ReturnType<typeof create>;
    act(() => { renderer = create(<MessageAttachments attachments={[attachment]} apiPort={8014} sessionId="ses_test" onOpenAttachment={() => { throw new Error("不可用附件不得打开"); }} />); });
    expect(renderer.root.findByProps({ role: "status" }).children).toEqual(["附件不可用"]);
    expect(renderer.root.findByProps({ "data-availability": "unavailable" }).type).toBe("article");
    expect(renderer.root.findAllByType("button").every((button) => button.props.disabled)).toBe(true);
    act(() => renderer.unmount());
  });

  test("点击图片时把稳定附件引用交给右侧资源入口，不打开正文 modal", () => {
    const opened: AttachmentRef[] = [];
    let renderer!: ReturnType<typeof create>;
    act(() => {
      renderer = create(
        <MessageAttachments
          attachments={[image]}
          apiPort={8014}
          sessionId="ses_test"
          onOpenAttachment={(attachment) => opened.push(attachment)}
        />,
      );
    });

    const button = renderer.root.findAllByProps({
      "aria-label": "在右侧打开附件：image.png",
    })[0];
    act(() => button.props.onClick());

    expect(opened).toEqual([image]);
    expect(renderer.root.findAllByProps({ role: "dialog" })).toHaveLength(0);
    renderer.unmount();
  });

  test("通用文件即使没有图片预览也可以通过右侧入口打开", () => {
    const file: AttachmentRef = {
      file_id: "boxteam-session://ses_test/attachments/report.pdf",
      name: "report.pdf",
      content_type: "application/pdf",
    };
    const opened: AttachmentRef[] = [];
    let renderer!: ReturnType<typeof create>;
    act(() => {
      renderer = create(
        <MessageAttachments
          attachments={[file]}
          apiPort={8014}
          sessionId="ses_test"
          onOpenAttachment={(attachment) => opened.push(attachment)}
        />,
      );
    });

    const button = renderer.root.findAllByProps({
      "aria-label": "在右侧打开附件：report.pdf",
    })[0];
    act(() => button.props.onClick());

    expect(opened).toEqual([file]);
    renderer.unmount();
  });
});
