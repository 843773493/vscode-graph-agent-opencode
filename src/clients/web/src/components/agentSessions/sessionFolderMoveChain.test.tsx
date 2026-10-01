import { describe, expect, test } from "bun:test";
import {
  FOLDER_ID,
  SESSION_ID,
  TARGET_FOLDER_ID,
  clickMenuButton,
  installClipboard,
  renderFolderMenu,
} from "./sessionFolderClipboardHarness";

describe("会话文件夹「复制文件夹 ID → 移动到剪贴板文件夹」真实链路", () => {
  test("复制 canonical 文件夹 ID 后，移动到剪贴板文件夹把该 ID 交给 moveSessionFolder", async () => {
    installClipboard();
    // 第一步：右键源文件夹 →「复制文件夹 ID」，剪贴板写入后端 canonical 的
    // ses_ 形态 folder id（folder 与 session 同表同形态）。
    const source = await renderFolderMenu({
      workspaceId: "gw_1",
      folderId: FOLDER_ID,
      parentNodeId: null,
      name: "源文件夹",
    });
    await clickMenuButton(source.tree, "复制文件夹 ID");
    expect(source.statuses).toEqual(["已复制会话文件夹 ID: " + FOLDER_ID]);

    // 第二步：右键目标文件夹 →「移动到剪贴板文件夹」，必须把剪贴板里的
    // ses_ folder id 作为目标父目录提交，而不是报「没有有效的会话文件夹 ID」。
    const target = await renderFolderMenu({
      workspaceId: "gw_1",
      folderId: TARGET_FOLDER_ID,
      parentNodeId: null,
      name: "目标文件夹",
    });
    await clickMenuButton(target.tree, "移动到剪贴板文件夹");

    expect(target.errors).toEqual([]);
    expect(target.moveSessionFolderCalls).toEqual([
      ["gw_1", TARGET_FOLDER_ID, FOLDER_ID, null],
    ]);
    expect(target.statuses).toEqual(["已移动文件夹 目标文件夹"]);
  });
});
