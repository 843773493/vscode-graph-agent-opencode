import os
from pathlib import Path

import psutil
import pytest

from scripts import generate_real_model_rollout_fixture as generator


def test_inherit_running_provider_environment_selects_same_project_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_key = "BOXTEAM_TEST_PROVIDER_KEY"
    organization_id = "BOXTEAM_TEST_PROVIDER_ORGANIZATION"
    existing_key = "BOXTEAM_TEST_PROVIDER_EXISTING_KEY"
    monkeypatch.delenv(provider_key, raising=False)
    monkeypatch.delenv(organization_id, raising=False)
    monkeypatch.setenv(existing_key, "shell-value")
    monkeypatch.setattr(generator, "PROJECT_ROOT", tmp_path.resolve())

    class ProcessStub:
        def __init__(
            self,
            *,
            cwd: Path,
            command: list[str],
            environment: dict[str, str],
        ) -> None:
            self.info = {"cwd": str(cwd), "cmdline": command}
            self._environment = environment
            self.pid = 4321

        def environ(self) -> dict[str, str]:
            return self._environment

        def cwd(self) -> str:
            return str(self.info["cwd"])

    processes = [
        ProcessStub(
            cwd=tmp_path / "another-project",
            command=["python", "-m", "app.main"],
            environment={provider_key: "wrong-project"},
        ),
        ProcessStub(
            cwd=tmp_path,
            command=["python", "worker.py"],
            environment={provider_key: "wrong-command"},
        ),
        ProcessStub(
            cwd=tmp_path,
            command=["python", "-m", "app.main"],
            environment={
                provider_key: "backend-value",
                organization_id: "backend-organization",
                existing_key: "must-not-replace-shell-value",
            },
        ),
    ]

    def process_iter(*, attrs: tuple[str, ...]) -> list[ProcessStub]:
        assert attrs == ("cmdline",)
        return processes

    monkeypatch.setattr(generator.psutil, "process_iter", process_iter)

    generator._inherit_running_provider_environment(
        {provider_key, organization_id, existing_key}
    )

    assert os.environ[provider_key] == "backend-value"
    assert os.environ[organization_id] == "backend-organization"
    assert os.environ[existing_key] == "shell-value"


def test_inherit_running_provider_environment_reports_inaccessible_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_key = "BOXTEAM_TEST_PROVIDER_KEY_UNREADABLE"
    monkeypatch.delenv(provider_key, raising=False)
    monkeypatch.setattr(generator, "PROJECT_ROOT", tmp_path.resolve())

    class ProcessStub:
        def __init__(self) -> None:
            self.pid = 4321
            self.info = {
                "cmdline": ["python", "-m", "app.main"],
            }

        def cwd(self) -> str:
            return str(tmp_path)

        def environ(self) -> dict[str, str]:
            raise psutil.AccessDenied(pid=self.pid, name="python")

    monkeypatch.setattr(
        generator.psutil,
        "process_iter",
        lambda **_kwargs: [ProcessStub()],
    )

    with pytest.raises(RuntimeError, match=r"pid=4321, missing=BOXTEAM_TEST_PROVIDER_KEY_UNREADABLE") as raised:
        generator._inherit_running_provider_environment({provider_key})

    assert isinstance(raised.value.__cause__, psutil.AccessDenied)
