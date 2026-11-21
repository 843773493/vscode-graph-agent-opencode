import { createContext, useContext, useEffect, useRef } from "react";
import type { WorkspaceFileSelection } from "../../../utils/workspaceFileReferences";

export interface WorkspaceCodePreviewProps {
  path: string;
  content: string;
  language: string;
  readOnly: boolean;
  selection: WorkspaceFileSelection | null;
  onBeginEdit: () => void;
  onChange: (content: string) => void;
  onSave: () => void;
}

export interface WorkspaceCodePreviewHandle {
  update: (props: WorkspaceCodePreviewProps) => void;
  dispose: () => void;
}

export type WorkspaceCodePreviewRenderer = (
  container: HTMLElement, props: WorkspaceCodePreviewProps,
) => WorkspaceCodePreviewHandle;

export const WorkspaceCodePreviewContext = createContext<WorkspaceCodePreviewRenderer | null>(null);

/** 正文和保存仍归原预览 hook；宿主只提供原版编辑器及其视图态。 */
export default function WorkspaceCodePreview(props: WorkspaceCodePreviewProps) {
  const render = useContext(WorkspaceCodePreviewContext);
  const container = useRef<HTMLDivElement>(null);
  const editor = useRef<WorkspaceCodePreviewHandle>();
  useEffect(() => {
    if (!container.current) return;
    if (!render) throw new Error("文件预览未接入 Workbench 编辑器宿主");
    const handle = render(container.current, props);
    editor.current = handle;
    return () => { editor.current = undefined; handle.dispose(); };
  }, [render, props.path]);
  useEffect(() => { editor.current?.update(props); }, [props]);
  return <div ref={container} className="workspace-preview-text-editor-shell" data-path={props.path} />;
}
