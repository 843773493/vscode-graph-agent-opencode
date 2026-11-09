"""本地运行时采集的设置与实际状态；不包含业务正文或动态标签。"""

from typing import cast

from pydantic import BaseModel, ConfigDict, Field


class TelemetryRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    sample_ratio: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)
    sample_buffer_limit: int = Field(default=256, ge=1, le=4096)


class StatisticsRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    collection_interval_ms: int = Field(default=5000, ge=1000, le=60000)


class RuntimeObservationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings

    @classmethod
    def from_gateway_config(cls, payload: dict[str, object]) -> "RuntimeObservationSettings":
        return cls.model_validate({name: cast(dict[str, object], payload[name])["runtime"]
                                   for name in ("telemetry", "statistics")})


class RuntimeObservationApplyRequest(RuntimeObservationSettings):
    instance_id: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class RuntimeRequestSample(BaseModel):
    kind: str
    status_code: int
    duration_ms: float
    failed: bool


class RuntimeRequestStatistics(BaseModel):
    count: int = 0
    errors: int = 0
    duration_ms: float = 0
    proxy_count: int = 0
    proxy_errors: int = 0
    proxy_duration_ms: float = 0


class RuntimeProcessResources(BaseModel):
    service: str
    pid: int
    rss_bytes: int | None = None
    cpu_seconds: float | None = None
    error: str | None = None


class RuntimeObservationStatus(BaseModel):
    instance_id: str
    applied_revision: str
    telemetry: TelemetryRuntimeSettings
    statistics: StatisticsRuntimeSettings
    requests: RuntimeRequestStatistics | None
    samples: list[RuntimeRequestSample]
    processes: list[RuntimeProcessResources]
    collection_error: str | None = None
    unsupported_metrics: list[str] = Field(default_factory=lambda: ["plugin_internal_stages"])
