"""Job 控制单测显式注入端口，不构造第二套生产存储。"""
from tests.unit.services.business.job.ports import (
    job_service_factory as job_service_factory,  # noqa: PLC0414
)
