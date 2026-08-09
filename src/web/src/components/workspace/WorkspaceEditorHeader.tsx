import { useEffect, useRef, useState, type DragEvent } from "react";

import type { WorkspaceAuxiliaryTab } from "./WorkspaceAuxiliaryPanel";

interface WorkspaceEditorHeaderProps {
  auxiliaryTab: WorkspaceAuxiliaryTab;
  tabOrder: ReadonlyArray<WorkspaceAuxiliaryTab>;
  onSelectAuxiliaryTab: (tab: WorkspaceAuxiliaryTab) => void;
  onReorderAuxiliaryTabs: (tabOrder: WorkspaceAuxiliaryTab[]) => void;
}

const DEFAULT_AUXILIARY_TAB_ORDER: ReadonlyArray<WorkspaceAuxiliaryTab> = [
  "files",
  "changes",
  "automation",
  "resources",
];

const WORKSPACE_COMPONENT_OPTIONS: ReadonlyArray<{
  tab: WorkspaceAuxiliaryTab;
  label: string;
  description: string;
  icon: string;
}> = [
  {
    tab: "files",
    label: "文件",
    description: "浏览工作区文件",
    icon: "codicon-file-directory",
  },
  {
    tab: "changes",
    label: "更改",
    description: "查看会话和工作区文件变更",
    icon: "codicon-diff",
  },
  {
    tab: "automation",
    label: "自动化",
    description: "管理自动化任务",
    icon: "codicon-gear",
  },
  {
    tab: "resources",
    label: "运行与连接",
    description: "查看终端、浏览器和后台连接",
    icon: "codicon-server-process",
  },
];

export default function WorkspaceEditorHeader({
  auxiliaryTab,
  tabOrder,
  onSelectAuxiliaryTab,
  onReorderAuxiliaryTabs,
}: WorkspaceEditorHeaderProps) {
  const activeTabRef = useRef<HTMLButtonElement>(null);
  const draggedTabRef = useRef<WorkspaceAuxiliaryTab | null>(null);
  const [dropTargetTab, setDropTargetTab] = useState<WorkspaceAuxiliaryTab | null>(null);

  useEffect(() => {
    activeTabRef.current?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [auxiliaryTab]);

  const handleDragStart = (
    event: DragEvent<HTMLButtonElement>,
    tab: WorkspaceAuxiliaryTab,
  ) => {
    draggedTabRef.current = tab;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", tab);
  };

  const handleDragOver = (
    event: DragEvent<HTMLButtonElement>,
    tab: WorkspaceAuxiliaryTab,
  ) => {
    if (!draggedTabRef.current || draggedTabRef.current === tab) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    setDropTargetTab(tab);
  };

  const handleDrop = (
    event: DragEvent<HTMLButtonElement>,
    targetTab: WorkspaceAuxiliaryTab,
  ) => {
    event.preventDefault();
    const sourceTab = draggedTabRef.current;
    if (!sourceTab || sourceTab === targetTab) return;

    const nextOrder = [...tabOrder];
    const sourceIndex = nextOrder.indexOf(sourceTab);
    const targetIndex = nextOrder.indexOf(targetTab);
    if (sourceIndex < 0 || targetIndex < 0) return;
    nextOrder.splice(sourceIndex, 1);
    nextOrder.splice(nextOrder.indexOf(targetTab), 0, sourceTab);
    onReorderAuxiliaryTabs(nextOrder);
  };

  const handleDragEnd = () => {
    draggedTabRef.current = null;
    setDropTargetTab(null);
  };

  const orderedOptions = (tabOrder ?? DEFAULT_AUXILIARY_TAB_ORDER).flatMap((tab) => {
    const option = WORKSPACE_COMPONENT_OPTIONS.find((candidate) => candidate.tab === tab);
    return option ? [option] : [];
  });

  return (
    <header className="workspace-editor-header" aria-label="右侧侧边栏组件">
      <nav className="workspace-component-tabs" role="tablist" aria-label="右侧侧边栏组件标签">
        {orderedOptions.map((option) => (
          <button
            type="button"
            role="tab"
            draggable
            className={`workspace-component-tab${auxiliaryTab === option.tab ? " active" : ""}${dropTargetTab === option.tab ? " drop-target" : ""}`}
            aria-selected={auxiliaryTab === option.tab}
            title={option.description}
            ref={auxiliaryTab === option.tab ? activeTabRef : undefined}
            key={option.tab}
            onClick={() => onSelectAuxiliaryTab(option.tab)}
            onDragStart={(event) => handleDragStart(event, option.tab)}
            onDragOver={(event) => handleDragOver(event, option.tab)}
            onDrop={(event) => handleDrop(event, option.tab)}
            onDragEnd={handleDragEnd}
          >
            <span className={`codicon ${option.icon}`} aria-hidden="true" />
            <span>{option.label}</span>
          </button>
        ))}
      </nav>
    </header>
  );
}
