import { describe, expect, test } from "bun:test";
import React from "react";
import { act, create } from "react-test-renderer";
import MessageAttachments from "./MessageAttachments";
import type { AttachmentRef } from "../../types/backend";

const image: AttachmentRef = {
  file_id: "boxteam-session://ses_test/attachments/image.png",
  name: "image.png",
  content_type: "image/png",
  data_url: "data:image/png;base64,preview",
};

describe("MessageAttachments", () => {
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
