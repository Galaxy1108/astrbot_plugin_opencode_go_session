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
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_opencode_go_session"
PLUGIN_VERSION = "1.0.0"

DEFAULT_HEADER = "x-opencode-session"
DEFAULT_MATCH = "opencode.ai"

WRAP_MARK = "_ocgo_session_wrapped"

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
    "为 OpenCode Go 注入按会话独立的 x-opencode-session 请求头",
    PLUGIN_VERSION,
)
class OpenCodeGoSessionPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._log_state: tuple[int, int] | None = None

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
