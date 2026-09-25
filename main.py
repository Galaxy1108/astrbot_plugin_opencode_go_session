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

import asyncio
import contextvars
import functools
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_NAME = "astrbot_plugin_opencode_go_session"
PLUGIN_VERSION = "1.2.0"

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
# (text label, card label, key in the usage payload)
USAGE_WINDOWS: tuple[tuple[str, str, str], ...] = (
    ("[5h]", "5 小时", "rolling"),
    ("[1w]", "每周", "weekly"),
    ("[1m]", "每月", "monthly"),
)
LIMITED_STATUSES = {"rate-limited", "rate_limited", "limited", "exceeded"}

# ---- image card (drawn with Pillow, no browser / network needed) ----
CARD_SCALE = 2
CARD_WIDTH = 620
CARD_PAD = 26
CARD_RADIUS = 16
CARD_BG = (31, 31, 31)
CARD_BORDER = (54, 54, 54)
COLOR_TITLE = (245, 245, 245)
COLOR_LABEL = (232, 232, 232)
COLOR_MUTED = (148, 148, 148)
COLOR_TRACK = (48, 48, 48)
COLOR_GREEN = (63, 185, 80)
COLOR_AMBER = (210, 153, 34)
COLOR_RED = (248, 81, 73)

FONTS_REGULAR = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
FONTS_BOLD = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/Dengb.ttf",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

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

    @staticmethod
    def _window_state(window: Any) -> tuple[float, bool]:
        """Return (percent clamped to 0..100, is_rate_limited)."""
        if not isinstance(window, dict):
            return 0.0, False
        try:
            percent = float(window.get("percent") or 0)
        except (TypeError, ValueError):
            percent = 0.0
        percent = max(0.0, min(100.0, percent))
        status = str(window.get("status") or "").strip().lower()
        return percent, status in LIMITED_STATUSES or percent >= 100

    @staticmethod
    def _fill_color(percent: float, limited: bool) -> tuple[int, int, int]:
        if limited or percent >= 80:
            return COLOR_RED
        if percent >= 50:
            return COLOR_AMBER
        return COLOR_GREEN

    @staticmethod
    def _stamp_text(target: datetime, tz: timezone) -> str:
        local = target.astimezone(tz)
        return f"{local.year}/{local.month}/{local.day} {local:%H:%M:%S}"

    def _format_window(
        self,
        label: str,
        window: Any,
        now: datetime,
        tz: timezone,
    ) -> tuple[str, bool]:
        """Return the display line and whether this window is rate limited."""
        if not isinstance(window, dict):
            return f"{label} 数据缺失", False

        percent, limited = self._window_state(window)
        line = f"{label} {self._bar(percent)} {percent:>3.0f}%"
        target = self._parse_iso(window.get("resetsAt"))
        if target is not None:
            line += f"  {self._relative(target, now)}（{self._absolute(target, now, tz)}）"
        if limited:
            line += f"  {LIMITED_MARK} 已限流"
        return line, limited

    def _format_usage(self, name: str, usage: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        tz = self._display_timezone()
        lines = [f"OpenCode Go 用量 · {name}"]
        limited_any = False
        for text_label, _card_label, key in USAGE_WINDOWS:
            line, limited = self._format_window(text_label, usage.get(key), now, tz)
            lines.append(line)
            limited_any = limited_any or limited
        if not any(isinstance(usage.get(key), dict) for _t, _c, key in USAGE_WINDOWS):
            lines.append("（返回里没有可识别的用量窗口）")
        if limited_any:
            lines.append(f"{LIMITED_MARK} 已限流：该窗口额度已用尽，等重置或改用免费模型")
        return "\n".join(lines)

    # -------------------------------------------------------------- image card
    def _font_path(self, bold: bool) -> str | None:
        override = str(self._cfg("usage_font", "") or "").strip()
        if override and os.path.isfile(override):
            return override
        return self._first_existing(FONTS_BOLD if bold else FONTS_REGULAR)

    @staticmethod
    def _first_existing(paths: tuple[str, ...]) -> str | None:
        for path in paths:
            if path and os.path.isfile(path):
                return path
        return None

    def _card_output_path(self, name: str) -> Path:
        out_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"usage_{short_digest(name)[:8]}.png"

    def _render_usage_card(self, name: str, usage: dict[str, Any]) -> str | None:
        """Draw the usage card with Pillow. Returns a file path, or None.

        Runs in a worker thread (``asyncio.to_thread``), so it must stay sync.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] Pillow 不可用: {exc}")
            return None

        regular = self._font_path(False)
        bold = self._font_path(True) or regular
        if not regular or not bold:
            logger.debug(f"[{PLUGIN_NAME}] 找不到可用的中文字体，跳过图片渲染")
            return None

        scale = CARD_SCALE
        tz = self._display_timezone()
        now = datetime.now(timezone.utc)

        def load(path: str, size: int):
            return ImageFont.truetype(path, size * scale)

        f_title = load(bold, 22)
        f_sub = load(regular, 13)
        f_meta = load(regular, 12)
        f_label = load(regular, 16)
        f_pct = load(bold, 16)
        f_small = load(regular, 12)

        width = CARD_WIDTH * scale
        pad = CARD_PAD * scale
        bar_h = 8 * scale
        gap_label_bar = 9 * scale
        gap_bar_reset = 8 * scale
        gap_section = 20 * scale

        rows = []
        for _text_label, card_label, key in USAGE_WINDOWS:
            window = usage.get(key)
            percent, limited = self._window_state(window)
            target = (
                self._parse_iso(window.get("resetsAt"))
                if isinstance(window, dict)
                else None
            )
            rows.append((card_label, percent, limited, target))

        # Measure with a throwaway canvas, then build the real one at that height.
        probe = ImageDraw.Draw(Image.new("RGB", (width, 8)))

        def line_h(text: str, font) -> int:
            box = probe.textbbox((0, 0), text, font=font)
            return box[3] - box[1]

        title_h = line_h("OpenCode Go 用量", f_title)
        sub_h = line_h("账号额度 · 已用百分比", f_sub)
        meta_h = line_h("更新于 2000/00/00 00:00:00", f_meta)
        label_h = line_h("5 小时", f_label)
        reset_h = line_h("重置于 2000/00/00 00:00:00", f_small)

        height = pad + title_h + 10 * scale + sub_h + 6 * scale + meta_h + 20 * scale
        for _label, _percent, _limited, _target in rows:
            height += label_h + gap_label_bar + bar_h + gap_bar_reset + reset_h
            height += gap_section
        height -= gap_section
        if any(row[2] for row in rows):
            height += 12 * scale + meta_h
        height += pad

        img = Image.new("RGB", (width, height), CARD_BG)
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=CARD_RADIUS * scale,
            fill=CARD_BG,
            outline=CARD_BORDER,
            width=max(1, scale),
        )

        cursor = pad

        # Title row: title on the left, provider name right-aligned.
        draw.text((pad, cursor), "OpenCode Go 用量", font=f_title, fill=COLOR_TITLE)
        name_w = probe.textlength(name, font=f_sub)
        draw.text(
            (width - pad - name_w, cursor + (title_h - sub_h)),
            name,
            font=f_sub,
            fill=COLOR_MUTED,
        )
        cursor += title_h + 10 * scale

        draw.text((pad, cursor), "账号额度 · 已用百分比", font=f_sub, fill=COLOR_MUTED)
        cursor += sub_h + 6 * scale

        draw.text(
            (pad, cursor),
            f"更新于 {self._stamp_text(now, tz)}",
            font=f_meta,
            fill=COLOR_MUTED,
        )
        cursor += meta_h + 20 * scale

        track_w = width - pad * 2
        for label, percent, limited, target in rows:
            color = self._fill_color(percent, limited)
            draw.text((pad, cursor), label, font=f_label, fill=COLOR_LABEL)
            pct_text = f"{percent:.0f}%"
            pct_w = probe.textlength(pct_text, font=f_pct)
            draw.text(
                (width - pad - pct_w, cursor),
                pct_text,
                font=f_pct,
                fill=COLOR_TITLE,
            )
            cursor += label_h + gap_label_bar

            draw.rounded_rectangle(
                (pad, cursor, pad + track_w, cursor + bar_h),
                radius=bar_h // 2,
                fill=COLOR_TRACK,
            )
            if percent > 0:
                fill_w = max(int(track_w * percent / 100.0), bar_h)
                draw.rounded_rectangle(
                    (pad, cursor, pad + fill_w, cursor + bar_h),
                    radius=bar_h // 2,
                    fill=color,
                )
            cursor += bar_h + gap_bar_reset

            if target is not None:
                draw.text(
                    (pad, cursor),
                    f"重置于 {self._stamp_text(target, tz)}",
                    font=f_small,
                    fill=COLOR_MUTED,
                )
                rel = self._relative(target, now)
                rel_w = probe.textlength(rel, font=f_small)
                draw.text(
                    (width - pad - rel_w, cursor),
                    rel,
                    font=f_small,
                    fill=COLOR_MUTED,
                )
            else:
                draw.text((pad, cursor), "重置于 —", font=f_small, fill=COLOR_MUTED)
            cursor += reset_h + gap_section

        if any(row[2] for row in rows):
            cursor -= gap_section
            draw.text(
                (pad, cursor + 12 * scale),
                f"{LIMITED_MARK} 已限流：该窗口额度已用尽，等重置或改用免费模型",
                font=f_meta,
                fill=COLOR_RED,
            )

        out = self._card_output_path(name)
        img.save(out, "PNG")
        return str(out)

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

        cards: list[str] = []
        texts: list[str] = []
        render_mode = str(self._cfg("usage_render", "auto")).strip().lower()

        for index, provider in enumerate(providers):
            name = self._provider_label(provider, index)
            key = self._resolve_key(provider)
            url = self._usage_url(provider)
            if not key or not url:
                texts.append(f"OpenCode Go 用量 · {name}\n读取 API Key 或 api_base 失败")
                continue
            try:
                data = await self._fetch_usage(provider, url, key)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 查询用量失败: {exc}")
                texts.append(f"OpenCode Go 用量 · {name}\n查询失败：{exc}")
                continue

            usage = data.get("usage")
            if not isinstance(usage, dict):
                texts.append(f"OpenCode Go 用量 · {name}\n返回里没有 usage 字段")
                continue

            if render_mode != "text":
                card = await asyncio.to_thread(
                    self._render_usage_card,
                    name,
                    usage,
                )
                if card:
                    cards.append(card)
                    continue
                if render_mode == "image":
                    logger.warning(f"[{PLUGIN_NAME}] 图片渲染失败，回退为文本")
            texts.append(self._format_usage(name, usage))

        for card in cards:
            yield event.image_result(card)
        if texts:
            yield event.plain_result("\n\n".join(texts))
