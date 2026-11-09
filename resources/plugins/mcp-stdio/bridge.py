"""MCP stdio bridge 的正式插件入口。"""

import os
import sys
from pathlib import Path

project_root = os.environ.get("BOXTEAM_PROJECT_ROOT")
if not project_root:
    raise RuntimeError("Gateway 未向 MCP bridge 注入 BOXTEAM_PROJECT_ROOT")
sys.path.insert(0, str(Path(project_root).resolve(strict=True)))

from app.core.logging_config import configure_application_logging

configure_application_logging(service="service-plugin")

from app.services.infrastructure.mcp.plugin_bridge.service import main

if __name__ == "__main__":
    main()
