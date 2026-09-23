import { describe, expect, test } from "bun:test";
import type { WorkspacePreviewTab } from "../../components/workspace/WorkspaceFilePreviewArea";
import type { WorkspaceRuntimePreviewTab } from "../../components/workspace/WorkspaceRuntimePreviewArea";
import { projectWorkspacePreviewTabs } from "./previewTabProjection";

/**
 * 预览页签投影的契约：页签按 previewType 归属到文件/差异/运行时三个区域，
 * 当前活动页签只认本区域的页签，错误通道也按区域隔离。
 */

function tab(previewType: string, path: string): WorkspacePreviewTab {
  return { previewType, path, name: path } as unknown as WorkspacePreviewTab;
}

const FILE_A = tab("file", "src/a.ts");
const DIFF_X = tab("session-diff", "session-diff://cs-1/src%2Fx.ts");
const TERMINAL_T = tab("terminal", "terminal://t1") as WorkspaceRuntimePreviewTab;

describe("预览页签投影", () => {
  test("页签按 previewType 分到文件、差异与运行时三个区域", () => {
    const projection = projectWorkspacePreviewTabs({
      tabs: [FILE_A, DIFF_X, TERMINAL_T],
      auxiliaryTab: "files",
      activePath: null,
      loadingPath: null,
      error: null,
    });

    expect(projection.filePreviewTabs).toEqual([FILE_A]);
    expect(projection.changePreviewTabs).toEqual([DIFF_X]);
    expect(projection.runtimePreviewTabs).toEqual([TERMINAL_T]);
  });

  test("更改标签消费差异页签，其它标签消费文件页签", () => {
    const changes = projectWorkspacePreviewTabs({
      tabs: [FILE_A, DIFF_X],
      auxiliaryTab: "changes",
      activePath: null,
      loadingPath: null,
      error: null,
    });
    expect(changes.codePreviewTabs).toEqual([DIFF_X]);

    const files = projectWorkspacePreviewTabs({
      tabs: [FILE_A, DIFF_X],
      auxiliaryTab: "files",
      activePath: null,
      loadingPath: null,
      error: null,
    });
    expect(files.codePreviewTabs).toEqual([FILE_A]);
  });

  test("活动路径命中本区域页签时保留，否则退化到首个页签", () => {
    const hit = projectWorkspacePreviewTabs({
      tabs: [FILE_A, tab("file", "src/b.ts")],
      auxiliaryTab: "files",
      activePath: "src/b.ts",
      loadingPath: null,
      error: null,
    });
    expect(hit.activeCodePreviewPath).toBe("src/b.ts");
    expect(hit.activeFilePath).toBe("src/b.ts");

    const miss = projectWorkspacePreviewTabs({
      tabs: [FILE_A],
      auxiliaryTab: "files",
      activePath: "terminal://t1",
      loadingPath: null,
      error: null,
    });
    expect(miss.activeCodePreviewPath).toBe("src/a.ts");
    expect(miss.activeFilePath).toBe("src/a.ts");
  });

  test("文件路径不认差异页签", () => {
    const projection = projectWorkspacePreviewTabs({
      tabs: [DIFF_X, FILE_A],
      auxiliaryTab: "changes",
      activePath: "session-diff://cs-1/src%2Fx.ts",
      loadingPath: null,
      error: null,
    });

    expect(projection.activeCodePreviewPath).toBe("session-diff://cs-1/src%2Fx.ts");
    // 差异页签不属于文件区，活动文件路径必须回落到文件页签。
    expect(projection.activeFilePath).toBe("src/a.ts");
  });

  test("运行时活动预览只按运行时段路径匹配", () => {
    const projection = projectWorkspacePreviewTabs({
      tabs: [FILE_A, TERMINAL_T],
      auxiliaryTab: "files",
      activePath: "terminal://t1",
      loadingPath: null,
      error: null,
    });
    expect(projection.activeRuntimePreview).toEqual(TERMINAL_T);
  });

  test("加载路径只在本区域页签内成立", () => {
    const hit = projectWorkspacePreviewTabs({
      tabs: [FILE_A],
      auxiliaryTab: "files",
      activePath: null,
      loadingPath: "src/a.ts",
      error: null,
    });
    expect(hit.codePreviewLoadingPath).toBe("src/a.ts");

    const miss = projectWorkspacePreviewTabs({
      tabs: [FILE_A],
      auxiliaryTab: "files",
      activePath: null,
      loadingPath: "terminal://t1",
      error: null,
    });
    expect(miss.codePreviewLoadingPath).toBeNull();
  });

  test("错误通道按区域隔离：运行时错误不显示到文件预览", () => {
    const runtimeError = projectWorkspacePreviewTabs({
      tabs: [TERMINAL_T],
      auxiliaryTab: "files",
      activePath: "terminal://t1",
      loadingPath: null,
      error: "终端连接失败",
    });
    expect(runtimeError.codePreviewError).toBeNull();

    const fileError = projectWorkspacePreviewTabs({
      tabs: [FILE_A],
      auxiliaryTab: "files",
      activePath: "src/a.ts",
      loadingPath: null,
      error: "文件读取失败",
    });
    expect(fileError.codePreviewError).toBe("文件读取失败");

    const diffError = projectWorkspacePreviewTabs({
      tabs: [DIFF_X],
      auxiliaryTab: "changes",
      activePath: "session-diff://cs-1/src%2Fx.ts",
      loadingPath: null,
      error: "差异读取失败",
    });
    expect(diffError.codePreviewError).toBe("差异读取失败");
  });

  test("没有错误时不产生预览错误", () => {
    const projection = projectWorkspacePreviewTabs({
      tabs: [FILE_A],
      auxiliaryTab: "files",
      activePath: "src/a.ts",
      loadingPath: null,
      error: null,
    });
    expect(projection.codePreviewError).toBeNull();
  });
});
