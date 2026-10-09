"""出站网络守卫：防止 SSRF 与凭据外泄。

背景：AI 仓会用管理员配置的 MCP 地址并携带 `Authorization: Bearer <api_key>`
主动发起出站请求（`app/integrations/research.py`）。配置后台可编辑该地址，
因此"地址可信"不能作为假设：一次误配置或被篡改的配置就会把供应商凭据
发到内网/本机服务（云元数据、内网管理端等）。

出站前必须满足：
- 仅 https，且 URL 自带的用户名/密码一律拒绝；
- 主机名（解析后的 IP）不得属于回环/私网/链路本地/保留/组播段；
- 若配置了 `allowed_hosts` 白名单，主机必须命中；
- 禁止跟随重定向——重定向会把凭据带到已校验目标之外。

残留风险（已在评审文档登记，不声称已消除）：校验与实际建连之间存在
DNS rebinding 窗口；彻底消除需要在 socket 层锁定解析结果并重写 SNI/Host，
当前做法是"解析后逐个拒绝危险 IP"，可挡住绝大多数误配置与内网直连。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

# 明确拒绝的地址段：回环、私网、CGNAT、链路本地（含云元数据 169.254.169.254）、
# 保留/文档段、组播与未指定地址。
_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.88.99.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)

MAX_RESOLVED_ADDRESSES = 8


class OutboundUrlRejected(ValueError):
    """出站地址不被允许（配置错误或疑似 SSRF）。"""


def _as_ip(value: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return None


def _is_blocked(ip: ipaddress._BaseAddress) -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    candidate = mapped if mapped is not None else ip
    if any(candidate in network for network in _BLOCKED_NETWORKS):
        return True
    # 兜底：任何未被 IANA 视为全局可路由的地址同样拒绝
    return not candidate.is_global


def _reject_if_blocked(ip: ipaddress._BaseAddress, host: str) -> None:
    if _is_blocked(ip):
        raise OutboundUrlRejected(f"出站地址指向非公网地址段: {host} -> {ip}")


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise OutboundUrlRejected(f"出站主机无法解析: {host}") from exc
    addresses: list[ipaddress._BaseAddress] = []
    for info in infos:
        ip = _as_ip(info[4][0])
        if ip is not None and ip not in addresses:
            addresses.append(ip)
    if not addresses:
        raise OutboundUrlRejected(f"出站主机无法解析: {host}")
    return addresses[:MAX_RESOLVED_ADDRESSES]


def host_matches_allowlist(host: str, allowed_hosts: tuple[str, ...] | list[str] | None) -> bool:
    """白名单匹配：精确主机名或子域（`.example.com` 命中 `a.example.com`）。"""
    if not allowed_hosts:
        return True
    target = host.lower().rstrip(".")
    for item in allowed_hosts:
        entry = str(item).strip().lower().rstrip(".")
        if not entry:
            continue
        if target == entry or target.endswith("." + entry):
            return True
    return False


def assert_public_https_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] | None = None,
    resolve: bool = True,
) -> str:
    """校验出站 URL；不合法抛 `OutboundUrlRejected`，合法时返回原 URL。

    `resolve=False` 用于启动期配置校验：只做语法与字面 IP 检查，不做 DNS，
    避免启动依赖外部解析、也避免离线环境被误判。真正的地址校验发生在出站前
    （`resolve=True`），由调用方在每次请求时执行。
    """
    raw = (url or "").strip()
    if not raw:
        raise OutboundUrlRejected("URL 为空")
    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise OutboundUrlRejected(f"只允许 https 出站，当前为 {parts.scheme or '空'}")
    if parts.username or parts.password:
        raise OutboundUrlRejected("URL 不允许内嵌用户名/密码")
    host = (parts.hostname or "").strip().rstrip(".")
    if not host:
        raise OutboundUrlRejected("URL 缺少主机名")
    if not host_matches_allowlist(host, allowed_hosts):
        raise OutboundUrlRejected(f"出站主机不在允许列表中: {host}")
    literal = _as_ip(host)
    if literal is not None:
        _reject_if_blocked(literal, host)
        return raw
    if resolve:
        for ip in _resolve(host):
            _reject_if_blocked(ip, host)
    return raw
