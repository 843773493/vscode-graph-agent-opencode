# 目录用途

存放跨页面共享的基础视觉规则和工作台骨架样式。

```text
foundation/
├── theme.css              # 全局主题变量与基础外观
├── themeSurfaces.css      # 按主题变化的表面颜色
├── panelShared.css        # 面板共用布局基线
├── workbenchLayout.css    # 主窗口区域与尺寸布局
├── workbenchPanel.css     # 停靠面板外观
└── themeSurfaces.test.ts  # 主题表面变量约束
```

# 可修改内容

- 共享主题、工作台骨架与面板基础样式。
- 上述样式的结构性检查。

# 不可修改内容

- 不放置仅供会话、工作区或检查视图使用的专属样式。
- 不放置 React 组件、运行时业务逻辑或生成产物。

# 规范

- 保持 `src/main.tsx` 中样式导入的层叠顺序。
- 新增样式须先判断是否为共享基线；单个视图样式放入对应领域目录。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
