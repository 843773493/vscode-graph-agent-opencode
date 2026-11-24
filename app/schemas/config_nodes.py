"""Gateway 与 Workspace 共用的已解析配置节点投影。"""

from pydantic import BaseModel

from app.core.config_sources import ConfigSourceURI


class ConfigNodeInspectionDTO(BaseModel):
    node_id: str
    parent_id: str | None
    source_key: str
    source_uri: ConfigSourceURI
    definition_path: str
    reference: str
