#!/usr/bin/env python3
"""Twitch stream start/stop notifier for Telegram and Discord.

Uses only the Python standard library. Python 3.10+ recommended.
Can run continuously on a PC/VPS or once per invocation in GitHub Actions.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("BOT_CONFIG", BASE_DIR / "config.json"))
LOG = logging.getLogger("twitch-alert-bot")

DEFAULT_MESSAGES = {
    "start": (
        "🔴 {channel_name} НАЧАЛ(А) СТРИМ!\n\n"
        "🎮 {game}\n📝 {title}\n👀 Зрителей: {viewers}\n\n"
        "Смотреть: {url}"
    ),
    "end": (
        "⚫️ {channel_name} ЗАВЕРШИЛ(А) СТРИМ.\n\n"
        "Последняя категория: {game}\nПоследнее название: {title}\n\n"
        "Канал: {url}"
    ),
}


class BotError(Exception):
    pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise BotError(f"Файл {path} содержит некорректный JSON: {exc}") from exc


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(path)


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 20,
) -> tuple[int, Any]:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            status = response.status
    except HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        raise BotError(
    f"Не удалось подключиться к внешнему API "
    f"(сетевая ошибка: {type(exc).__name__})."
) from None

    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, raw.decode("utf-8", errors="replace")


def config_from_environment() -> dict[str, Any]:
    """Build config in memory; secrets are read only from Actions environment."""
    env = os.environ
    tg_token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    discord_webhook = env.get("DISCORD_WEBHOOK_URL", "").strip()
    return {
        "twitch": {
            "client_id": env.get("TWITCH_CLIENT_ID", "").strip(),
            "client_secret": env.get("TWITCH_CLIENT_SECRET", "").strip(),
            "channel_login": env.get("TWITCH_CHANNEL_LOGIN", "").strip(),
            "poll_interval_seconds": 300,
            "notify_if_live_on_startup": False,
        },
        "telegram": {
            "enabled": bool(tg_token or tg_chat_id),
            "bot_token": tg_token,
            "chat_id": tg_chat_id,
        },
        "discord": {
            "enabled": bool(discord_webhook),
            "webhook_url": discord_webhook,
        },
        "messages": DEFAULT_MESSAGES,
        "state_file": "state.json",
    }


def load_config() -> dict[str, Any]:
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return config_from_environment()
    if CONFIG_PATH.exists():
        config = read_json(CONFIG_PATH)
        if isinstance(config, dict):
            return config
        raise BotError("Корневой config.json должен содержать JSON-объект.")
    # Also allow environment configuration for containers/other schedulers.
    if os.environ.get("TWITCH_CLIENT_ID"):
        return config_from_environment()
    raise BotError(
        f"Не найден {CONFIG_PATH}. Скопируйте config.example.json в config.json "
        "и заполните его либо задайте переменные окружения."
    )


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str, channel_login: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.channel_login = channel_login.strip().lstrip("@").lower()
        self.token: str | None = None
        self.token_expires_at = 0.0

    def _get_token(self) -> str:
        if self.token and time.time() < self.token_expires_at - 300:
            return self.token

        payload = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }).encode("utf-8")
        status, data = http_json(
            "https://id.twitch.tv/oauth2/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
        if status != 200 or not isinstance(data, dict) or "access_token" not in data:
            raise BotError(f"Twitch не выдал токен (HTTP {status}): {data}")
        self.token = str(data["access_token"])
        self.token_expires_at = time.time() + int(data.get("expires_in", 3600))
        return self.token

    def get_stream(self) -> dict[str, Any] | None:
        query = urlencode({"user_login": self.channel_login})
        url = f"https://api.twitch.tv/helix/streams?{query}"

        for attempt in range(2):
            token = self._get_token()
            status, data = http_json(
                url,
                headers={
                    "Client-Id": self.client_id,
                    "Authorization": f"Bearer {token}",
                },
            )
            if status == 401 and attempt == 0:
                self.token = None
                continue
            if status != 200 or not isinstance(data, dict):
                raise BotError(f"Ошибка Twitch API (HTTP {status}): {data}")
            streams = data.get("data", [])
            return streams[0] if streams else None

        raise BotError("Не удалось авторизоваться в Twitch API.")


class Notifier:
    def __init__(self, config: dict[str, Any]):
        self.telegram = config.get("telegram", {})
        self.discord = config.get("discord", {})

    def send_telegram(self, text: str) -> bool:
        cfg = self.telegram
        if not cfg.get("enabled", False):
            return True
        token = str(cfg.get("bot_token", "")).strip()
        chat_id = str(cfg.get("chat_id", "")).strip()
        if not token or not chat_id:
            LOG.error("Telegram включён, но не заданы bot_token или chat_id.")
            return False

        body = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": False,
        }).encode("utf-8")
        status, data = http_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=body,
        )
        if status == 200 and isinstance(data, dict) and data.get("ok") is True:
            LOG.info("Уведомление отправлено в Telegram.")
            return True
        LOG.error("Не удалось отправить сообщение в Telegram (HTTP %s): %s", status, data)
        return False

    def send_discord(self, text: str) -> bool:
        cfg = self.discord
        if not cfg.get("enabled", False):
            return True
        webhook = str(cfg.get("webhook_url", "")).strip()
        if not webhook.startswith("https://"):
            LOG.error("Discord включён, но webhook_url не задан или не начинается с https://.")
            return False

        body = json.dumps({
            "content": text,
            "allowed_mentions": {"parse": []},
        }).encode("utf-8")
        status, data = http_json(
            webhook,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
            body=body,
        )
        if status in (200, 204):
            LOG.info("Уведомление отправлено в Discord.")
            return True
        LOG.error("Не удалось отправить сообщение в Discord (HTTP %s): %s", status, data)
        return False

    def deliver(self, text: str, pending: dict[str, Any]) -> None:
        # Keep per-destination status so a temporary outage does not lose the alert.
        if not pending.get("telegram_done", False):
            try:
                pending["telegram_done"] = self.send_telegram(text)
            except BotError as exc:
                LOG.error("Ошибка Telegram: %s", exc)
        if not pending.get("discord_done", False):
            try:
                pending["discord_done"] = self.send_discord(text)
            except BotError as exc:
                LOG.error("Ошибка Discord: %s", exc)


def format_message(template: str, stream: dict[str, Any], channel_login: str) -> str:
    channel_name = str(stream.get("user_name") or channel_login)
    values = {
        "channel_name": channel_name,
        "channel_login": channel_login,
        "title": str(stream.get("title") or "Без названия"),
        "game": str(stream.get("game_name") or "Не указана"),
        "url": f"https://www.twitch.tv/{channel_login}",
        "started_at": str(stream.get("started_at") or ""),
        "viewers": str(stream.get("viewer_count") or "0"),
    }
    try:
        return template.format(**values)
    except (KeyError, ValueError) as exc:
        LOG.warning("Ошибка в шаблоне сообщения (%s). Использую безопасный текст.", exc)
        return f"{values['channel_name']} — {values['url']}"


def stream_snapshot(stream: dict[str, Any]) -> dict[str, Any]:
    """Persist only useful stable fields; viewer count changes too often."""
    keys = ("user_name", "user_login", "title", "game_name", "started_at")
    return {key: stream[key] for key in keys if key in stream}


def default_state() -> dict[str, Any]:
    return {"known": False, "online": False, "stream": {}, "pending": []}


def queue_event(state: dict[str, Any], kind: str, stream: dict[str, Any]) -> None:
    state.setdefault("pending", []).append({
        "kind": kind,
        "stream": dict(stream),
        "telegram_done": False,
        "discord_done": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def process_pending(
    state: dict[str, Any], notifier: Notifier, messages: dict[str, str], channel_login: str
) -> None:
    remaining = []
    for item in state.get("pending", []):
        kind = item.get("kind")
        template = messages.get(kind, "{channel_name}: {url}")
        text = format_message(template, item.get("stream", {}), channel_login)
        notifier.deliver(text, item)
        if not (item.get("telegram_done") and item.get("discord_done")):
            remaining.append(item)
    state["pending"] = remaining


def validate_config(config: dict[str, Any]) -> None:
    twitch = config.get("twitch", {})
    for key in ("client_id", "client_secret", "channel_login"):
        if not str(twitch.get(key, "")).strip():
            raise BotError(f"Не заполнено twitch.{key} (или соответствующий GitHub Secret).")
    tg_enabled = config.get("telegram", {}).get("enabled", False)
    dc_enabled = config.get("discord", {}).get("enabled", False)
    if not tg_enabled and not dc_enabled:
        raise BotError("Не настроены каналы. Добавьте Telegram и/или Discord Secrets.")
    if tg_enabled:
        tg = config["telegram"]
        if not tg.get("bot_token") or not tg.get("chat_id"):
            raise BotError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
    if dc_enabled and not config["discord"].get("webhook_url"):
        raise BotError("Заполните DISCORD_WEBHOOK_URL.")


def run_cycle(config: dict[str, Any], state_path: Path) -> None:
    twitch_cfg = config["twitch"]
    channel_login = str(twitch_cfg["channel_login"]).strip().lstrip("@").lower()
    notify_on_startup = bool(twitch_cfg.get("notify_if_live_on_startup", False))

    state = read_json(state_path, default_state())
    if not isinstance(state, dict):
        raise BotError(f"Файл состояния {state_path} должен содержать JSON-объект.")
    state.setdefault("known", False)
    state.setdefault("online", False)
    state.setdefault("stream", {})
    state.setdefault("pending", [])

    twitch = TwitchClient(twitch_cfg["client_id"], twitch_cfg["client_secret"], channel_login)
    notifier = Notifier(config)
    messages = config.get("messages", DEFAULT_MESSAGES)

    live_stream = twitch.get_stream()
    if live_stream:
        if not state.get("known", False):
            if notify_on_startup:
                queue_event(state, "start", live_stream)
            state["known"] = True
            state["online"] = True
        elif not state.get("online", False):
            queue_event(state, "start", live_stream)
            state["online"] = True
        # Avoid committing every five minutes because viewer_count changes constantly.
        state["stream"] = stream_snapshot(live_stream)
    else:
        if state.get("known", False) and state.get("online", False):
            queue_event(state, "end", state.get("stream", {}))
        state["known"] = True
        state["online"] = False
        state["stream"] = {}

    # Persist state transition before sending; failed destinations remain queued.
    write_json_atomic(state_path, state)
    process_pending(state, notifier, messages, channel_login)
    write_json_atomic(state_path, state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Twitch stream alerts for Telegram and Discord")
    parser.add_argument(
        "--once",
        action="store_true",
        help="проверить Twitch один раз и завершить работу (для GitHub Actions)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        config = load_config()
        validate_config(config)
    except BotError as exc:
        LOG.error("%s", exc)
        return 2

    state_path = Path(config.get("state_file", "state.json"))
    if not state_path.is_absolute():
        state_path = BASE_DIR / state_path

    twitch_cfg = config["twitch"]
    channel_login = str(twitch_cfg["channel_login"]).strip().lstrip("@").lower()

    if args.once:
        LOG.info("Однократная проверка Twitch-канала: %s", channel_login)
        try:
            run_cycle(config, state_path)
        except BotError as exc:
            LOG.error("Проверка не удалась; состояние не меняю: %s", exc)
            return 1
        except Exception:
            LOG.exception("Неожиданная ошибка проверки.")
            return 1
        LOG.info("Проверка завершена.")
        return 0

    interval = max(30, int(twitch_cfg.get("poll_interval_seconds", 60)))
    LOG.info("Бот запущен. Канал Twitch: %s. Интервал проверки: %s сек.", channel_login, interval)
    while True:
        try:
            run_cycle(config, state_path)
        except BotError as exc:
            # API/network errors are not interpreted as an offline status.
            LOG.error("Проверка не удалась; состояние не меняю: %s", exc)
        except KeyboardInterrupt:
            LOG.info("Остановка по Ctrl+C.")
            return 0
        except Exception:
            LOG.exception("Неожиданная ошибка цикла. Продолжаю работу.")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            LOG.info("Остановка по Ctrl+C.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
