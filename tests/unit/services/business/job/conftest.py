"""Job 控制单测显式注入端口，不构造第二套生产存储。"""
import asyncio
from unittest.mock import Mock

import pytest

from tests.unit.services.business.job.ports import (
    job_service_factory as job_service_factory,  # noqa: PLC0414
)


@pytest.fixture
def boundary_executor():
    class Executor:
        def __init__(self):
            self.started = asyncio.Event()
            self.finish_first = asyncio.Event()
            self.first_finished = asyncio.Event()
            self.continued = asyncio.Event()
            self.finish_job = asyncio.Event()
            self.runtimes = []

        async def run(self, job):
            self.runtimes.append(job)
            if len(self.runtimes) == 1:
                async with job.execution_pause.boundary():
                    self.started.set()
                    await self.finish_first.wait()
                self.first_finished.set()
                async with job.execution_pause.boundary():
                    self.continued.set()
                    await self.finish_job.wait()
            return "done"

    return Executor()


@pytest.fixture
def terminal_writer():
    return Mock(mark_job_terminal_status=Mock(return_value=True))
