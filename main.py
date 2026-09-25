"""Inject a per-conversation x-opencode-session header into OpenCode Go requests.

OpenCode Go (https://opencode.ai/docs/go/) rejects requests that do not carry a
stable session id:

    400 {"type":"error","error":{"type":"MissingSessionID", ...}}

The session id is meant to be per conversation. AstrBot's provider config can
only set a static ``custom_headers`` value (one id shared by every chat), so this
plugin does it properly:

* ``on_llm_request`` records the current conversation's session id in a
  ContextVar (same async task as the provider call, so it is concurrency safe).
* each OpenCode Go provider's SDK resource (``chat.completions``,
  ``messages``, ``responses``) is wrapped so the outgoing call receives
  ``extra_headers`` with that session id.

Non-message-driven LLM calls (no hook fired -> ContextVar unset) are left alone,
so a static ``custom_headers`` fallback in the provider config still applies.

It also exposes ``/ocgo``, which reports the OpenCode Go usage windows
(5h / weekly / monthly) and when each one resets, via
``GET <api_base>/usage`` -> ``{usage: {rolling, weekly, monthly}}`` where each
window is ``{status, percent, resetsAt}``.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_opencode_go_session"
PLUGIN_VERSION = "1.1.0"

DEFAULT_HEADER = "x-opencode-session"
DEFAULT_MATCH = "opencode.ai"

WRAP_MARK = "_ocgo_session_wrapped"

# Usage query
USAGE_PATH = "usage"
HTTP_TIMEOUT_SECONDS = 20
BAR_WIDTH = 10
USAGE_CACHE_SECONDS = 30
# Progress bar glyphs. Deliberately kept inside GBK so a Windows console or a
# GBK-encoded log sink cannot blow up on the reply text.
BAR_FILLED = "█"
BAR_EMPTY = "─"
LIMITED_MARK = "※"
# label, key in the usage payload
USAGE_WINDOWS: tuple[tuple[str, str], ...] = (
    ("[5h]", "rolling"),
    ("[1w]", "weekly"),
    ("[1m]", "monthly"),
)
LIMITED_STATUSES = {"rate-limited", "rate_limited", "limited", "exceeded"}

SESSION_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    f"{PLUGIN_NAME}_session",
    default=None,
)


def short_digest(value: str) -> str:
    """Stable, non-reversible and short id (no chat identifiers leave the box)."""
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()[:32]


@register(
    PLUGIN_NAME,
    "Galaxy1108",
    "为 OpenCode Go 注入按会话独立的 x-opencode-session，并支持 /ocgo 查询用量",
    PLUGIN_VERSION,
)
class OpenCodeGoSessionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._log_state: tuple[int, int] | None = None
        # api_base -> (monotonic timestamp, usage dict)
        self._usage_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ------------------------------------------------------------------ config
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("enable", True))

    @property
    def header_name(self) -> str:
        name = str(self._cfg("header_name", DEFAULT_HEADER)).strip()
        return name or DEFAULT_HEADER

    @property
    def api_base_match(self) -> str:
        return str(self._cfg("match_api_base", DEFAULT_MATCH)).strip().lower()

    @property
    def fallback_session(self) -> str:
        return str(self._cfg("fallback_session", "") or "").strip()

    # ------------------------------------------------------------- provider side
    def _providers(self) -> list[Any]:
        try:
            return list(self.context.get_all_providers())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 读取 provider 列表失败: {exc}")
            return []

    def _is_target(self, provider: Any) -> bool:
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return False
        api_base = str(config.get("api_base") or "").lower()
        match = self.api_base_match
        return bool(match) and match in api_base

    def _wrap_resource(self, resource: Any) -> bool:
        """Wrap ``resource.create`` so every call carries the session header."""
        if resource is None or getattr(resource, WRAP_MARK, False):
            return False
        original = getattr(resource, "create", None)
        if original is None or not callable(original):
            return False

        header = self.header_name
        fallback = self.fallback_session

        @functools.wraps(original)
        async def create(*args: Any, **kwargs: Any) -> Any:
            session = SESSION_VAR.get() or fallback
            if session:
                headers = dict(kwargs.get("extra_headers") or {})
                headers.setdefault(header, session)
                kwargs["extra_headers"] = headers
            return await original(*args, **kwargs)

        try:
            setattr(resource, "create", create)
            setattr(resource, WRAP_MARK, True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 包装 SDK 资源失败: {exc}")
            return False
        return True

    def _install(self, provider: Any) -> bool:
        client = getattr(provider, "client", None)
        if client is None:
            return False

        wrapped = False
        chat = getattr(client, "chat", None)
        if chat is not None:
            wrapped |= self._wrap_resource(getattr(chat, "completions", None))
        # OpenCode Go also serves /messages (Anthropic family) and /responses.
        wrapped |= self._wrap_resource(getattr(client, "messages", None))
        wrapped |= self._wrap_resource(getattr(client, "responses", None))
        return wrapped

    def install_all(self) -> int:
        """Wrap every OpenCode provider client; idempotent, safe to call often."""
        installed = 0
        targets = 0
        for provider in self._providers():
            if not self._is_target(provider):
                continue
            targets += 1
            try:
                if self._install(provider):
                    installed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 安装 provider 失败: {exc}")

        state = (targets, installed)
        if state != self._log_state:
            self._log_state = state
            if installed:
                logger.info(
                    f"[{PLUGIN_NAME}] 已为 {installed} 个 provider 客户端安装 "
                    f"{self.header_name} 注入（匹配 '{self.api_base_match}'）",
                )
            elif targets:
                logger.info(f"[{PLUGIN_NAME}] {targets} 个 provider 已安装过，跳过")
        return installed

    # -------------------------------------------------------------------- hooks
    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        if not self.enabled:
            logger.info(f"[{PLUGIN_NAME}] 插件已禁用，跳过安装")
            return
        self.install_all()

    @filter.on_llm_request()
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.enabled:
            return None

        # Providers may be added or edited while AstrBot runs.
        self.install_all()

        source = str(self._cfg("session_source", "session_id")).strip()
        if source == "unified_msg_origin":
            raw = str(getattr(event, "unified_msg_origin", "") or "")
        else:
            raw = str(getattr(req, "session_id", "") or "")
        if not raw:
            raw = str(
                getattr(req, "session_id", "")
                or getattr(event, "unified_msg_origin", "")
                or "",
            )
        if not raw:
            SESSION_VAR.set(None)
            return None

        prefix = str(self._cfg("session_prefix", "astrbot") or "").strip()
        if bool(self._cfg("hash_session", True)):
            body = short_digest(raw)
        else:
            body = raw
        SESSION_VAR.set(f"{prefix}-{body}" if prefix else body)
        return None

    @filter.on_llm_response()
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        # Drop the value so unrelated background calls cannot inherit it.
        SESSION_VAR.set(None)
        return None

    # ------------------------------------------------------------------- usage
    def _opencode_providers(self) -> list[Any]:
        return [provider for provider in self._providers() if self._is_target(provider)]

    @staticmethod
    def _provider_label(provider: Any, index: int) -> str:
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            for field in ("id", "provider", "model"):
                value = config.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return f"provider {index + 1}"

    @staticmethod
    def _resolve_key(provider: Any) -> str | None:
        """Prefer the key AstrBot already resolved; fall back to the raw config."""
        resolved = getattr(provider, "chosen_api_key", None)
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()

        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return None
        raw = config.get("key")
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if not isinstance(raw, str) or not raw.strip():
            return None
        raw = raw.strip()
        if raw.startswith("$"):
            return os.environ.get(raw[1:], "").strip() or None
        return raw

    @staticmethod
    def _usage_url(provider: Any) -> str | None:
        config = getattr(provider, "provider_config", None)
        if not isinstance(config, dict):
            return None
        base = str(config.get("api_base") or "").strip().rstrip("/")
        if not base:
            return None
        return f"{base}/{USAGE_PATH}"

    @staticmethod
    def _request_headers(provider: Any, key: str) -> dict[str, str]:
        """Reuse the provider's headers so AstrBot's User-Agent is preserved."""
        headers = {
            str(name): str(value)
            for name, value in (getattr(provider, "request_headers", None) or {}).items()
        }
        headers.setdefault("User-Agent", "astrbot")
        headers["Accept"] = "application/json"
        headers["Authorization"] = f"Bearer {key}"
        return headers

    async def _fetch_usage(self, provider: Any, url: str, key: str) -> dict[str, Any]:
        cached = self._usage_cache.get(url)
        now = time.monotonic()
        if cached is not None and now - cached[0] < USAGE_CACHE_SECONDS:
            return cached[1]

        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers=self._request_headers(provider, key),
            ) as response:
                body = await response.text()
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}: {body[:160]}")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"返回不是合法 JSON: {body[:120]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"返回格式异常: {str(data)[:120]}")

        self._usage_cache[url] = (now, data)
        return data

    def _display_timezone(self) -> timezone:
        raw = self._cfg("usage_timezone_offset", 8)
        try:
            hours = float(raw)
        except (TypeError, ValueError):
            hours = 8.0
        return timezone(timedelta(hours=hours))

    @staticmethod
    def _parse_iso(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _bar(percent: float) -> str:
        ratio = max(0.0, min(100.0, percent)) / 100.0
        filled = int(round(ratio * BAR_WIDTH))
        if ratio > 0 and filled == 0:
            filled = 1
        return BAR_FILLED * filled + BAR_EMPTY * (BAR_WIDTH - filled)

    @staticmethod
    def _relative(target: datetime, now: datetime) -> str:
        seconds = (target - now).total_seconds()
        if seconds <= 0:
            return "即将重置"
        minutes = max(int(seconds // 60), 1)
        if minutes < 60:
            return f"{minutes} 分钟后重置"
        hours, mins = divmod(minutes, 60)
        if hours < 24:
            return f"{hours} 小时 {mins} 分后重置" if mins else f"{hours} 小时后重置"
        days, rem = divmod(hours, 24)
        return f"{days} 天 {rem} 小时后重置" if rem else f"{days} 天后重置"

    @staticmethod
    def _absolute(target: datetime, now: datetime, tz: timezone) -> str:
        local = target.astimezone(tz)
        local_now = now.astimezone(tz)
        days = (local.date() - local_now.date()).days
        if days == 0:
            prefix = "今天"
        elif days == 1:
            prefix = "明天"
        else:
            prefix = local.strftime("%m-%d")
        return f"{prefix} {local.strftime('%H:%M')}"

    def _format_window(
        self,
        label: str,
        window: Any,
        now: datetime,
        tz: timezone,
    ) -> str:
        if not isinstance(window, dict):
            return f"{label} 数据缺失"

        try:
            percent = float(window.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0.0
        percent = max(0.0, min(100.0, percent))

        status = str(window.get("status") or "").strip().lower()
        limited = status in LIMITED_STATUSES or percent >= 100

        line = f"{label} {self._bar(percent)} {percent:>3.0f}%"
        target = self._parse_iso(window.get("resetsAt"))
        if target is not None:
            line += f"  {self._relative(target, now)}（{self._absolute(target, now, tz)}）"
        if limited:
            line += f"  {LIMITED_MARK} 已限流"
        return line

    def _format_usage(self, name: str, usage: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        tz = self._display_timezone()
        lines = [f"OpenCode Go 用量 · {name}"]
        for label, key in USAGE_WINDOWS:
            lines.append(self._format_window(label, usage.get(key), now, tz))
        if not any(isinstance(usage.get(key), dict) for _label, key in USAGE_WINDOWS):
            lines.append("（返回里没有可识别的用量窗口）")
        return "\n".join(lines)

    @filter.command("ocgo", alias={"opencode用量", "go用量"})
    async def ocgo_usage(self, event: AstrMessageEvent):
        """查看 OpenCode Go 的 5 小时 / 周 / 月 用量与重置时间。用法：/ocgo"""
        if not self.enabled or not bool(self._cfg("usage_enable", True)):
            return
        if bool(self._cfg("usage_admin_only", False)) and not event.is_admin():
            yield event.plain_result("OpenCode Go 用量查询仅管理员可用。")
            return

        providers = self._opencode_providers()
        if not providers:
            yield event.plain_result(
                f"没有找到 api_base 含 “{self.api_base_match}” 的模型提供商。",
            )
            return

        blocks: list[str] = []
        for index, provider in enumerate(providers):
            name = self._provider_label(provider, index)
            key = self._resolve_key(provider)
            url = self._usage_url(provider)
            if not key or not url:
                blocks.append(f"OpenCode Go 用量 · {name}\n读取 API Key 或 api_base 失败")
                continue
            try:
                data = await self._fetch_usage(provider, url, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 查询用量失败: {exc}")
                blocks.append(f"OpenCode Go 用量 · {name}\n查询失败：{exc}")
                continue

            usage = data.get("usage")
            if not isinstance(usage, dict):
                blocks.append(f"OpenCode Go 用量 · {name}\n返回里没有 usage 字段")
                continue
            blocks.append(self._format_usage(name, usage))

        yield event.plain_result("\n\n".join(blocks))
