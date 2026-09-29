"""P6 验收扫描：无网关依赖、无计费表、无平台默认账号、无生产 Stub。

扫描范围：
- app/ 源码无 gateway/billing/usage/cost/quota 持久化
- 无硬编码密钥（api_key/secret/token 从环境或配置读取）
- 生产环境拒绝 Stub Provider
- 无平台默认账号
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"

# 禁止出现在 app/ 源码中的模式（网关/计费/平台）
FORBIDDEN_PATTERNS = [
    r"import.*gateway",
    r"from.*gateway",
    r"billing",
    r"quota",
    r"cost_callback",
    r"usage_callback",
    r"platform_key",
    r"open_gateway",
    r"smart_gateway",
]

# 允许出现的 usage 相关（仅剥离逻辑）
ALLOWED_USAGE_PATTERNS = [
    r"strip_usage_metadata",
    r"usage_metadata",
    r"USAGE_METADATA_KEYS",
    r"不落.*usage",
    r"剥离.*usage",
]


def _scan_files() -> list[tuple[str, str, int]]:
    """扫描 app/ 下所有 .py 文件，返回 (文件, 匹配模式, 行号)。"""
    violations = []
    for py_file in APP_DIR.rglob("*.py"):
        rel_path = str(py_file.relative_to(PROJECT_ROOT))
        content = py_file.read_text(encoding="utf-8")
        for line_no, line in enumerate(content.splitlines(), 1):
            for pattern in FORBIDDEN_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    # 检查是否是允许的 usage 剥离逻辑
                    if any(re.search(a, line, re.IGNORECASE) for a in ALLOWED_USAGE_PATTERNS):
                        continue
                    violations.append((rel_path, pattern, line_no))
    return violations


class TestNoGatewayDependencies:
    """P6: 无网关运行依赖。"""

    def test_no_gateway_imports(self):
        """app/ 源码不导入任何网关模块。"""
        violations = _scan_files()
        gateway_violations = [v for v in violations if "gateway" in v[1]]
        assert not gateway_violations, f"发现网关引用: {gateway_violations}"

    def test_no_billing_persistence(self):
        """app/ 源码不持久化计费/配额数据。"""
        violations = _scan_files()
        billing_violations = [v for v in violations if v[1] in ("billing", "quota", "cost_callback")]
        assert not billing_violations, f"发现计费引用: {billing_violations}"


class TestNoHardcodedSecrets:
    """P6: 无硬编码密钥。"""

    def test_no_hardcoded_api_key(self):
        """app/ 源码不硬编码 API Key。"""
        for py_file in APP_DIR.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            # 检查是否有硬编码的 key（排除配置类和注释）
            lines = content.splitlines()
            for line_no, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                # 匹配 api_key = "xxx" 或 secret = "xxx" 形式
                if re.search(r'(api_key|secret|token)\s*=\s*["\'][^"\']{10,}', line):
                    # 排除配置类中的默认值
                    if "str = " in line or ": str" in line:
                        continue
                    pytest.fail(f"硬编码密钥: {py_file.relative_to(PROJECT_ROOT)}:{line_no}: {stripped}")


class TestNoProductionStub:
    """P6: 生产环境拒绝 Stub。"""

    def test_stub_rejected_in_production(self):
        """Stub Provider 在生产环境拒绝启动。"""
        from app.config import Settings, SettingsError

        with pytest.raises(SettingsError, match="生产环境禁止 Stub"):
            Settings(
                environment="prod",
                database_url="postgresql://localhost/test",
                model={"provider": "stub"},
            )


class TestNoPlatformDefaultAccount:
    """P6: 无平台默认账号。"""

    def test_no_default_admin_in_code(self):
        """app/ 源码不包含平台默认账号。"""
        for py_file in APP_DIR.rglob("*.py"):
            content = py_file.read_text(encoding="utf-8")
            assert "default_admin" not in content.lower()
            assert "admin123" not in content.lower()
            assert "password123" not in content.lower()
