"""worker 配置在启动前拒绝无法工作的参数。"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import WorkerSettings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 0),
        ("poll_interval_seconds", 0),
        ("lease_seconds", 0),
        ("max_attempts", 0),
        ("reap_interval_seconds", 0),
        ("backoff_base_seconds", -1),
    ],
)
def test_invalid_worker_settings_rejected(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        WorkerSettings.model_validate({field: value})
