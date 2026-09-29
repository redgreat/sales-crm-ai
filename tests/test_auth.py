"""服务间认证单元测试：签名、时效、篡改、重放（nonce 持久层单独测）。"""
from __future__ import annotations

import time

import pytest

from app.auth import OperatorContext, sign_request, verify_signed_request
from app.errors import Unauthenticated


SECRET = "test-secret"
KEY_ID = "crm-ai"


def _verify(**kwargs):
    defaults = dict(
        method="POST",
        path="/api/v1/runs",
        body=b"{}",
        headers={},
        secret_provider=lambda key_id: SECRET if key_id == KEY_ID else None,
        timestamp_window_seconds=300,
    )
    defaults.update(kwargs)
    return verify_signed_request(**defaults)


def test_valid_signature_with_operator():
    headers = sign_request(
        secret=SECRET,
        key_id=KEY_ID,
        method="POST",
        path="/api/v1/runs",
        body=b"{}",
        operator=OperatorContext(user_id="u1", user_name="张三"),
    )
    verified = _verify(headers=headers)
    assert verified.operator is not None
    assert verified.operator.user_id == "u1"


def test_wrong_signature_rejected():
    headers = sign_request(secret="other-secret", key_id=KEY_ID, method="POST",
                           path="/api/v1/runs", body=b"{}")
    with pytest.raises(Unauthenticated):
        _verify(headers=headers)


def test_tampered_body_rejected():
    headers = sign_request(secret=SECRET, key_id=KEY_ID, method="POST",
                           path="/api/v1/runs", body=b'{"a":1}')
    with pytest.raises(Unauthenticated):
        _verify(headers=headers, body=b'{"a":2}')


def test_expired_timestamp_rejected():
    headers = sign_request(secret=SECRET, key_id=KEY_ID, method="POST",
                           path="/api/v1/runs", body=b"{}", timestamp=int(time.time()) - 4000)
    with pytest.raises(Unauthenticated):
        _verify(headers=headers)


def test_unknown_key_id_rejected():
    headers = sign_request(secret=SECRET, key_id="unknown", method="POST",
                           path="/api/v1/runs", body=b"{}")
    with pytest.raises(Unauthenticated):
        _verify(headers=headers)


def test_identity_must_be_signed():
    """身份字段参与签名串：篡改 user 头导致签名不匹配。"""
    headers = sign_request(secret=SECRET, key_id=KEY_ID, method="POST",
                           path="/api/v1/runs", body=b"{}",
                           operator=OperatorContext(user_id="u1"))
    forged = dict(headers)
    forged["X-SAI-User-Id"] = "victim"
    with pytest.raises(Unauthenticated):
        _verify(headers=forged)
