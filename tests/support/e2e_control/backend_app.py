"""显式测试 ASGI composition；生产入口没有测试环境或测试路由。"""

from __future__ import annotations

import faulthandler
import json
import os

# 只观察本显式测试 composition 的 import；进入本模块前的 Python/uvicorn 不在覆盖范围。
faulthandler.dump_traceback_later(55, repeat=False)

from app.core.identifier import bind_identifier_factory
from app.core.phase_observer import bind_phase_observer
from app.runtime.runtime_ports import RuntimeClockPorts
from tests.support.e2e_control.runtime_ports import (
    ControlledClock,
    ControlledDeadlineTimer,
    ControlledIdentifiers,
    ControlledPhases,
)

_address = json.loads(os.environ["BOXTEAM_E2E_TEST_CONTROL"])
bind_identifier_factory(ControlledIdentifiers(_address))
bind_phase_observer(ControlledPhases(_address))
_clock = ControlledClock(_address)

# 测试装配先绑定 port，再导入既有应用与真实 lifespan，不创建第二应用。
from app.main import app

faulthandler.cancel_dump_traceback_later()
app.state.clock_ports = RuntimeClockPorts(
    residency=_clock.residency, wall=_clock.utc_now, wait=_clock,
    deadline_timer=ControlledDeadlineTimer(_address, _clock),
)
