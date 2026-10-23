"""基础设施服务单元测试的公共 fixture。"""

from __future__ import annotations

import pytest

from app.services.infrastructure.events.event_channel_service import EventChannelService


@pytest.fixture
def event_channel_service() -> EventChannelService:
    return EventChannelService()
