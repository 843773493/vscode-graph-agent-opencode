from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from deepagents.backends import CompositeBackend, FilesystemBackend
from deepagents.backends.protocol import (
    EditResult,
    FileUploadResponse,
    LsResult,
    WriteResult,
)

from app.core.env import get_project_root
from app.core.path_utils import get_session_path_resolver, safe_join
from app.core.session_paths import SessionPathResolver

BOXTEAM_ARTIFACTS_ROOT = "/.boxteam"
SESSION_ARTIFACT_ROUTE = "/session-artifacts/"
WORKSPACE_SKILLS_ROUTE = "/.boxteam/skills/"
BUNDLED_SKILLS_SOURCE = "/.boxteam/bundled-skills/"


class SessionArtifactBackend(FilesystemBackend):
    """把稳定 session ID 虚拟路径映射到可移动的物理会话目录。"""

    def __init__(self, workspace_root: Path) -> None:
        sessions_root = workspace_root / ".boxteam" / "sessions"
        super().__init__(root_dir=sessions_root, virtual_mode=True)
        self._path_resolver: SessionPathResolver = get_session_path_resolver(
            sessions_root
        )

    def _resolve_path(self, key: str) -> Path:
        virtual_path = Path(key.lstrip("/"))
        if not virtual_path.parts:
            return self.cwd
        if ".." in virtual_path.parts:
            raise ValueError(f"会话产物虚拟路径无效: {key}")
        session_id = virtual_path.parts[0]
        try:
            session_root = self._path_resolver.resolve_session_node(session_id)
        except KeyError as error:
            raise FileNotFoundError(
                f"会话产物路径引用了不存在的稳定 ID: {session_id}"
            ) from error
        return safe_join(session_root, *virtual_path.parts[1:])

    def _to_virtual_path(self, path: Path) -> str:
        resolved = path.resolve()
        session_nodes = sorted(
            (
                node
                for node in self._path_resolver.list_nodes()
                if node.kind == "session"
            ),
            key=lambda node: len(node.path.parts),
            reverse=True,
        )
        for node in session_nodes:
            try:
                tail = resolved.relative_to(node.path)
            except ValueError:
                continue
            return f"/{node.node_id}/{tail.as_posix()}"
        return super()._to_virtual_path(path)


class BundledSkillBackend(FilesystemBackend):
    """把发行包内的共享 Skill 以只读虚拟目录暴露给 Agent。"""

    def __init__(self, *, root_dir: Path, skill_groups: Sequence[str]) -> None:
        resolved_groups = tuple(skill_groups)
        if not resolved_groups:
            raise ValueError("内置 Skill 后端至少需要一个 Skill 组")
        super().__init__(root_dir=str(root_dir), virtual_mode=True)
        self._skill_groups = frozenset(resolved_groups)
        for skill_group in resolved_groups:
            if (
                not skill_group
                or skill_group in {".", ".."}
                or skill_group != Path(skill_group).name
                or "/" in skill_group
                or "\\" in skill_group
            ):
                raise ValueError(f"内置 Skill 组 ID 不能包含路径: {skill_group!r}")
            skill_root = root_dir / skill_group
            if not skill_root.is_dir():
                raise FileNotFoundError(f"内置 Skill 组目录不存在: {skill_root}")
            skill_file = skill_root / "SKILL.md"
            if not skill_file.is_file():
                raise FileNotFoundError(f"内置 Skill 组缺少 SKILL.md: {skill_file}")

    def ls(self, path: str) -> LsResult:
        result = super().ls(path)
        if path.rstrip("/") != "":
            return result
        entries = [
            entry
            for entry in result.entries or []
            if entry.get("path", "").strip("/").split("/", 1)[0]
            in self._skill_groups
        ]
        return LsResult(error=result.error, entries=entries)

    def write(self, file_path: str, content: str) -> WriteResult:
        del content
        return WriteResult(error=f"内置 Skill 资源只读，不能写入: {file_path}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        del old_string, new_string, replace_all
        return EditResult(error=f"内置 Skill 资源只读，不能编辑: {file_path}")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=file_path, error="permission_denied")
            for file_path, _content in files
        ]


def build_workspace_backend(
    workspace_root: Path,
    *,
    bundled_skill_groups: Sequence[str] = (),
    project_root: Path | None = None,
) -> CompositeBackend:
    """构建统一的 DeepAgents 工作区后端，并隔离框架运行产物。"""
    workspace_files = FilesystemBackend(
        root_dir=str(workspace_root),
        virtual_mode=True,
    )
    workspace_skills = FilesystemBackend(
        root_dir=workspace_root / ".boxteam" / "skills",
        virtual_mode=True,
    )
    session_artifacts = SessionArtifactBackend(workspace_root)
    routes = {
        SESSION_ARTIFACT_ROUTE: session_artifacts,
        WORKSPACE_SKILLS_ROUTE: workspace_skills,
    }
    if bundled_skill_groups:
        resolved_project_root = project_root or get_project_root()
        bundled_skills_root = resolved_project_root / "resources" / "skills"
        routes[BUNDLED_SKILLS_SOURCE] = BundledSkillBackend(
            root_dir=bundled_skills_root,
            skill_groups=bundled_skill_groups,
        )
    return CompositeBackend(
        default=workspace_files,
        routes=routes,
        artifacts_root=BOXTEAM_ARTIFACTS_ROOT,
    )
