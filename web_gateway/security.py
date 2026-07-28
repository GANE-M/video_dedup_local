from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse


PUBLIC_RECAP_RENDERING_FIELDS = {
    "hardware_acceleration",
    "crf",
    "caption_y_percent",
    "caption_font_size",
}
HARDWARE_CHOICES = {"auto", "nvidia", "amd", "intel", "apple", "cpu"}


def normalize_public_recap_rendering(
    value: dict[str, Any] | None,
    *,
    defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    submitted = dict(value or {})
    unknown = set(submitted) - PUBLIC_RECAP_RENDERING_FIELDS
    if unknown:
        raise ValueError(f"远程解说渲染配置不允许这些字段: {sorted(unknown)}")
    safe_defaults = {
        key: item for key, item in dict(defaults or {}).items()
        if key in PUBLIC_RECAP_RENDERING_FIELDS
    }
    rendering = {
        "hardware_acceleration": "nvidia",
        "crf": 23,
        "caption_y_percent": 12.0,
        "caption_font_size": 38,
        **safe_defaults,
        **submitted,
    }
    hardware = str(rendering["hardware_acceleration"] or "auto").strip().casefold()
    if hardware not in HARDWARE_CHOICES:
        raise ValueError(f"无效硬件加速方式: {hardware}")
    rendering["hardware_acceleration"] = hardware
    rendering["crf"] = int(rendering["crf"])
    rendering["caption_y_percent"] = float(rendering["caption_y_percent"])
    rendering["caption_font_size"] = int(rendering["caption_font_size"])
    if not 0 <= rendering["crf"] <= 51:
        raise ValueError("解说输出质量必须在 0 到 51 之间")
    if not 0 <= rendering["caption_y_percent"] <= 100:
        raise ValueError("解说字幕位置必须在 0 到 100 之间")
    if not 8 <= rendering["caption_font_size"] <= 160:
        raise ValueError("解说字幕字号必须在 8 到 160 之间")
    return rendering


def _is_forbidden_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def validate_public_http_url(url: str, *, resolve_dns: bool = True) -> str:
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("接口地址必须是有效的 HTTP 或 HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("接口地址不得包含用户名或密码")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost"):
        raise ValueError("远程接口不得指向服务器本机")
    try:
        ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if _is_forbidden_address(hostname):
            raise ValueError("远程接口不得指向本机、内网或保留地址")
        return text
    if resolve_dns:
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            }
        except socket.gaierror as exc:
            raise ValueError(f"接口域名无法解析: {hostname}") from exc
        if not addresses or any(_is_forbidden_address(address) for address in addresses):
            raise ValueError("接口域名解析到了本机、内网或保留地址")
    return text


class SlidingWindowRateLimiter:
    def __init__(self, maximum: int, window_seconds: float) -> None:
        self.maximum = max(1, int(maximum))
        self.window_seconds = max(1.0, float(window_seconds))
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[str(key)]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.maximum:
                return False
            events.append(now)
            return True
