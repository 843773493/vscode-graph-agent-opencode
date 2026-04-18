from __future__ import annotations
from datetime import datetime
from typing import Optional

from app.schemas.job import JobDTO, StepDTO, JobControlRequest, JobControlResponse
from app.schemas.common import JobStatus, RunMode, StepStatus, ControlAction
from app.services.agent_execution_service import AgentExecutionService


class JobService:
    async def list(self, session_id: Optional[str] = None) -> list[JobDTO]:
        now = datetime.now()
        return [
            JobDTO(
                job_id="job_001",
                session_id="ses_123",
                mode=RunMode.hierarchical,
                status=JobStatus.running,
                entry_agent="planner",
                progress=65,
                current_step="step_003",
                error_message=None,
                metadata={"task": "Implement authentication module"},
                created_at=now,
                updated_at=now,
                ended_at=None
            ),
            JobDTO(
                job_id="job_002",
                session_id="ses_123",
                mode=RunMode.single_agent,
                status=JobStatus.completed,
                entry_agent="executor",
                progress=100,
                current_step=None,
                error_message=None,
                metadata={"task": "Refactor database queries"},
                created_at=now,
                updated_at=now,
                ended_at=now
            ),
            JobDTO(
                job_id="job_003",
                session_id="ses_456",
                mode=RunMode.parallel,
                status=JobStatus.failed,
                entry_agent="reviewer",
                progress=30,
                current_step=None,
                error_message="Timeout exceeded while processing file",
                metadata={"task": "Code review for PR #123"},
                created_at=now,
                updated_at=now,
                ended_at=now
            )
        ]

    async def get(self, job_id: str) -> JobDTO:
        now = datetime.now()
        return JobDTO(
            job_id=job_id,
            session_id="ses_123",
            mode=RunMode.hierarchical,
            status=JobStatus.running,
            entry_agent="planner",
            progress=65,
            current_step="step_003",
            error_message=None,
            metadata={"task": "Implement authentication module"},
            created_at=now,
            updated_at=now,
            ended_at=None
        )

    async def list_steps(self, job_id: str) -> list[StepDTO]:
        now = datetime.now()
        return [
            StepDTO(
                step_id="step_001",
                job_id=job_id,
                parent_step_id=None,
                agent_id="planner",
                step_type="planning",
                status=StepStatus.completed,
                input_payload={"task": "Implement authentication"},
                output_payload={"plan": ["setup_models", "create_routes", "add_middleware"]},
                started_at=now,
                ended_at=now
            ),
            StepDTO(
                step_id="step_002",
                job_id=job_id,
                parent_step_id="step_001",
                agent_id="executor",
                step_type="execution",
                status=StepStatus.completed,
                input_payload={"action": "setup_models"},
                output_payload={"files_created": ["models/user.py", "models/auth.py"]},
                started_at=now,
                ended_at=now
            ),
            StepDTO(
                step_id="step_003",
                job_id=job_id,
                parent_step_id="step_001",
                agent_id="executor",
                step_type="execution",
                status=StepStatus.running,
                input_payload={"action": "create_routes"},
                output_payload={},
                started_at=now,
                ended_at=None
            )
        ]

    async def control(self, job_id: str, control_request: JobControlRequest) -> JobControlResponse:
        return JobControlResponse(
            job_id=job_id,
            status=JobStatus.paused if control_request.action == ControlAction.pause else JobStatus.running,
            control_state=f"Action {control_request.action.value} applied successfully"
        )
    
    async def run_agent(self, session_id: str, message: str) -> str:
        """
        启动Agent执行单步调用
        
        Args:
            session_id: 会话ID
            message: 用户输入消息
            
        Returns:
            Agent响应内容
        """
        agent_service = AgentExecutionService.get_instance()
        return await agent_service.run_step(session_id, message)
