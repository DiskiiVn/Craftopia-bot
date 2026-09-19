from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from ai_support import AIAnswer, AIProviderError, ImageInput, MinecraftSupportAI
from converter import ConversionError, convert_oraxen_pack
from env_loader import load_dotenv
from incident_monitor import IncidentMonitor, IncidentSignal
from knowledge import KnowledgeBase
from message_cleanup import (
    VIETNAM_TZ,
    DeleteMessagesRequest,
    DeleteScanSnapshot,
    delete_snapshot_messages,
    parse_delete_messages_request,
    scan_today_messages,
)
from mc_status import EndpointStatus, MinecraftStatus, concise_status, query_minecraft_status
from music import CRAFTOPIA_AUTHOR, MUSIC_RECOVERY_BUILD, MusicCog, MusicConfig, MusicManager


load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("craftopia-bot")

BOT_BUILD = MUSIC_RECOVERY_BUILD
ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_MB", "8")) * 1024 * 1024
SUPPORT_CHANNEL_IDS = tuple(dict.fromkeys(
    int(value) for value in os.getenv("SUPPORT_CHANNEL_IDS", "").split(",")
    if value.strip().isdigit()
))
_auto_reply_channels_raw = os.getenv("AI_AUTO_REPLY_CHANNEL_IDS")
AI_AUTO_REPLY_CHANNEL_IDS = (
    SUPPORT_CHANNEL_IDS
    if _auto_reply_channels_raw is None
    else tuple(dict.fromkeys(
        int(value) for value in _auto_reply_channels_raw.split(",")
        if value.strip().isdigit()
    ))
)
DISCORD_GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0") or 0)
STAFF_ROLE_ID = int(os.getenv("STAFF_ROLE_ID", "0") or 0)
STATUS_CHANNEL_ID = int(os.getenv("STATUS_CHANNEL_ID", "0") or 0)
MONITOR_ALL_CHANNELS = os.getenv("MONITOR_ALL_CHANNELS", "true").lower() == "true"
MONITOR_EXCLUDED_CHANNEL_IDS = {
    int(value) for value in os.getenv("MONITOR_EXCLUDED_CHANNEL_IDS", "").split(",")
    if value.strip().isdigit()
}
CRAFTOPIA_WALLET_API_BASE = os.getenv(
    "CRAFTOPIA_WALLET_API_BASE",
    "https://store.craftopics.online/api/discord",
).rstrip("/")
CRAFTOPIA_BOT_API_KEY = os.getenv("CRAFTOPIA_BOT_API_KEY", "").strip()

MC_HOST = os.getenv("MC_HOST", "play.craftopics.online")
JAVA_PORT = int(os.getenv("JAVA_PORT", "25565"))
BEDROCK_PORT = int(os.getenv("BEDROCK_PORT", "19132"))
STATUS_CHECK_SECONDS = max(30, int(os.getenv("STATUS_CHECK_SECONDS", "60")))
STATUS_CACHE_SECONDS = max(5, min(60, int(os.getenv("STATUS_CACHE_SECONDS", "15"))))
STATUS_FAILURE_THRESHOLD = max(2, int(os.getenv("STATUS_FAILURE_THRESHOLD", "3")))
STATUS_RECOVERY_THRESHOLD = max(2, int(os.getenv("STATUS_RECOVERY_THRESHOLD", "2")))
STATS_CHANNEL_ID = int(os.getenv("STATS_CHANNEL_ID", "0") or 0)
STATS_UPDATE_SECONDS = max(60, int(os.getenv("STATS_UPDATE_SECONDS", "120")))
TRACK_DISCORD_PRESENCE = os.getenv("TRACK_DISCORD_PRESENCE", "false").lower() == "true"
MAX_TRAINING_CHARS = max(3000, min(200_000, int(os.getenv("MAX_TRAINING_CHARS", "50000"))))
MAX_TRAINING_FILE_BYTES = 1024 * 1024
MAX_TRAINING_FILES = 3
DELETE_SCAN_LIMIT_PER_CHANNEL = max(
    100, min(20_000, int(os.getenv("DELETE_SCAN_LIMIT_PER_CHANNEL", "5000")))
)
DELETE_SCAN_LIMIT_TOTAL = max(
    DELETE_SCAN_LIMIT_PER_CHANNEL,
    min(100_000, int(os.getenv("DELETE_SCAN_LIMIT_TOTAL", "20000"))),
)
DELETE_MAX_MESSAGES = max(1, min(1000, int(os.getenv("DELETE_MAX_MESSAGES", "500"))))
DELETE_CONFIRM_SECONDS = max(30, min(300, int(os.getenv("DELETE_CONFIRM_SECONDS", "120"))))
DELETE_SCAN_COOLDOWN_SECONDS = max(10, int(os.getenv("DELETE_SCAN_COOLDOWN_SECONDS", "30")))
MONITOR_WINDOW_SECONDS = max(60, int(os.getenv("MONITOR_WINDOW_SECONDS", "180")))
MONITOR_COOLDOWN_SECONDS = max(300, int(os.getenv("MONITOR_COOLDOWN_SECONDS", "900")))
MONITOR_MAX_ALERTS_PER_HOUR = max(1, int(os.getenv("MONITOR_MAX_ALERTS_PER_HOUR", "3")))
ALLOWED_MENTIONS_NONE = discord.AllowedMentions.none()
STATS_STATE_PATH = ROOT / "data" / "stats-dashboard.json"
DEFAULT_TRAINING_TOPIC = "Kiến thức Craftopia"


SENSITIVE_TRAINING_PATTERNS = (
    re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/", re.IGNORECASE),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bsk-(?:proj-)?[0-9A-Za-z_-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bmfa\.[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\b[0-9A-Za-z_-]{20,}\.[0-9A-Za-z_-]{6}\.[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bbearer\s+[0-9A-Za-z._~+/-]{12,}", re.IGNORECASE),
    re.compile(
        r"\b(?:password|passwd|mật\s*khẩu|mat\s*khau|api[ _-]?key|secret|token)\b"
        r"\s*[:=]\s*[\"']?\S{8,}",
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class TrainingDocument:
    topic: str
    fact: str
    source_count: int


@dataclass(frozen=True)
class DiscordGuildStats:
    members: int | None
    online: int | None
    approximate: bool
    detail: str


def split_discord_text(text: str, limit: int = 1900) -> list[str]:
    text = text.strip()
    if not text:
        return ["Mình chưa tạo được câu trả lời."]
    parts: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def format_answer(answer: AIAnswer) -> str:
    text = answer.text
    if answer.sources:
        labels: list[str] = []
        for source in answer.sources:
            source_path = Path(source)
            if source_path.parts[:1] == ("managed",):
                label = "kiến thức quản trị đã xác nhận qua ~hl"
            elif source_path.parts[:1] == ("imports",):
                label = "tài liệu quản trị đã nhập"
            elif source_path.name == "admin-facts.md":
                label = "kiến thức quản trị đã xác nhận"
            else:
                label = source_path.stem.replace("-", " ").replace("_", " ")[:80]
            if label not in labels:
                labels.append(label)
        text += "\n\n-# Nguồn nội bộ: " + ", ".join(f"`{label}`" for label in labels)
    if answer.needs_staff:
        text += "\n-# Trường hợp này có thể cần staff kiểm tra trực tiếp."
    return text


def endpoint_status_text(endpoint: EndpointStatus) -> str:
    if not endpoint.online:
        if endpoint.error_kind == "protocol":
            return f"🟡 Có phản hồi nhưng không đọc được status (`{endpoint.host}:{endpoint.port}`)"
        return f"🔴 Không phản hồi (`{endpoint.host}:{endpoint.port}`)"
    players = (
        f"{endpoint.players_online}/{endpoint.players_max} người"
        if endpoint.players_online is not None and endpoint.players_max is not None
        else "chưa rõ số người"
    )
    latency = f"{endpoint.latency_ms:.0f} ms" if endpoint.latency_ms is not None else "chưa rõ ping"
    version = discord.utils.escape_markdown(discord.utils.escape_mentions(endpoint.version or "chưa rõ"))
    motd = re.sub(r"§.", "", endpoint.motd or "").strip()
    motd = discord.utils.escape_markdown(discord.utils.escape_mentions(motd))[:250]
    lines = [
        f"🟢 Online — **{players}**",
        f"Ping: **{latency}** · Phiên bản: `{version[:100]}`",
    ]
    if motd:
        lines.append(f"MOTD: {motd}")
    return "\n".join(lines)


def minecraft_status_embed(status: MinecraftStatus) -> discord.Embed:
    online_count = int(status.java.online) + int(status.bedrock.online)
    color = (
        discord.Color.green() if online_count == 2
        else discord.Color.orange() if online_count == 1
        else discord.Color.red()
    )
    embed = discord.Embed(
        title="Trạng thái máy chủ Craftopia",
        description=f"Địa chỉ: `{MC_HOST}`",
        color=color,
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name=f"{status.java.edition} · {JAVA_PORT}", value=endpoint_status_text(status.java), inline=False)
    embed.add_field(
        name=f"{status.bedrock.edition} · {BEDROCK_PORT}",
        value=endpoint_status_text(status.bedrock),
        inline=False,
    )
    embed.set_footer(text="Dữ liệu ping công khai; không thể hiện TPS, RAM hoặc plugin")
    return embed


def channel_is_nsfw(channel: object) -> bool:
    is_nsfw_method = getattr(channel, "is_nsfw", None)
    return bool(is_nsfw_method()) if callable(is_nsfw_method) else False


def channel_is_private_thread(channel: object) -> bool:
    return (
        isinstance(channel, discord.Thread)
        and channel.type is discord.ChannelType.private_thread
    )


def channel_has_ai_auto_reply(channel: object) -> bool:
    if channel_is_nsfw(channel) or channel_is_private_thread(channel):
        return False
    channel_id = getattr(channel, "id", 0)
    if channel_id in AI_AUTO_REPLY_CHANNEL_IDS:
        return True
    return (
        isinstance(channel, discord.Thread)
        and getattr(channel, "parent_id", None) in AI_AUTO_REPLY_CHANNEL_IDS
    )


def _write_utf8(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _atomic_write_utf8(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_stats_target(path: Path = STATS_STATE_PATH) -> dict[str, int]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    target: dict[str, int] = {}
    for key in ("guild_id", "channel_id", "message_id"):
        raw = value.get(key)
        if isinstance(raw, int) and raw > 0:
            target[key] = raw
    return target


def _write_stats_target(path: Path, target: dict[str, int]) -> None:
    content = json.dumps(target, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_utf8(path, content)


def has_sensitive_training_data(text: str) -> bool:
    return any(pattern.search(text) for pattern in SENSITIVE_TRAINING_PATTERNS)


def _clean_training_text(text: str, label: str) -> str:
    if "\x00" in text:
        raise ValueError(f"{label} chứa dữ liệu nhị phân không hợp lệ")
    cleaned = text.strip()
    if not cleaned:
        raise ValueError(f"{label} đang trống")
    return cleaned


def build_training_document(
    lesson: str = "",
    replied_text: str = "",
    documents: tuple[tuple[str, str], ...] = (),
) -> TrainingDocument:
    """Build one explicit admin lesson without silently dropping any content."""
    if "\x00" in lesson:
        raise ValueError("Nội dung lệnh chứa dữ liệu nhị phân không hợp lệ")
    lesson = lesson.strip()
    replied_text = replied_text.strip()
    has_external_content = bool(replied_text or documents)

    topic = DEFAULT_TRAINING_TOPIC
    inline_fact = ""
    if "|" in lesson:
        raw_topic, inline_fact = lesson.split("|", 1)
        topic = raw_topic.strip()
        if not topic:
            raise ValueError("Chủ đề trước dấu | không được để trống")
        inline_fact = inline_fact.strip()
    elif has_external_content:
        topic = lesson or DEFAULT_TRAINING_TOPIC
    else:
        inline_fact = lesson

    topic = re.sub(r"[\r\n#]+", " ", topic).strip()
    if not topic:
        raise ValueError("Chủ đề không được để trống")
    if len(topic) > 100:
        raise ValueError("Chủ đề tối đa 100 ký tự")

    sections: list[tuple[str, str]] = []
    if inline_fact:
        sections.append(("Nội dung lệnh", _clean_training_text(inline_fact, "Nội dung lệnh")))
    if replied_text:
        sections.append(("Tin nhắn được trả lời", _clean_training_text(replied_text, "Tin nhắn trả lời")))
    for filename, content in documents:
        safe_name = re.sub(r"[^0-9A-Za-zÀ-ỹ._ -]+", "-", filename).strip(" .-")[:80]
        safe_name = safe_name or "document.txt"
        sections.append((f"Tệp {safe_name}", _clean_training_text(content, f"Tệp {safe_name}")))

    if not sections:
        raise ValueError(
            "Không có nội dung để học. Hãy nhập nội dung, reply một tin nhắn hoặc đính kèm .md/.txt"
        )
    if len(sections) == 1 and sections[0][0] == "Nội dung lệnh":
        fact = sections[0][1]
    else:
        fact = "\n\n".join(f"### {label}\n\n{content}" for label, content in sections)
    if len(fact) > MAX_TRAINING_CHARS:
        raise ValueError(
            f"Nội dung có {len(fact):,} ký tự, vượt giới hạn {MAX_TRAINING_CHARS:,}; bot không cắt âm thầm"
        )
    if has_sensitive_training_data(topic + "\n" + fact):
        raise ValueError(
            "Phát hiện dữ liệu giống token, API key, mật khẩu, webhook hoặc private key; bot từ chối lưu"
        )
    return TrainingDocument(topic=topic, fact=fact, source_count=len(sections))


def server_stats_embed(
    guild: discord.Guild,
    guild_stats: DiscordGuildStats,
    minecraft_status: MinecraftStatus,
) -> discord.Embed:
    online_endpoints = int(minecraft_status.java.online) + int(minecraft_status.bedrock.online)
    color = (
        discord.Color.green() if online_endpoints == 2
        else discord.Color.orange() if online_endpoints == 1
        else discord.Color.red()
    )
    embed = discord.Embed(
        title="Craftopia · Bảng theo dõi server",
        description=f"IP Java: `{MC_HOST}:{JAVA_PORT}`\nIP Bedrock/PE: `{MC_HOST}:{BEDROCK_PORT}`",
        color=color,
        timestamp=datetime.now(UTC),
    )
    marker = "≈" if guild_stats.approximate else ""
    member_text = f"{marker}{guild_stats.members:,}" if guild_stats.members is not None else "chưa rõ"
    online_text = f"{marker}{guild_stats.online:,}" if guild_stats.online is not None else "chưa rõ"
    embed.add_field(
        name=f"Discord · {discord.utils.escape_markdown(guild.name)[:80]}",
        value=(
            f"Thành viên: **{member_text}**\n"
            f"Đang online: **{online_text}**\n"
            f"-# {guild_stats.detail}"
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{minecraft_status.java.edition} · {JAVA_PORT}",
        value=endpoint_status_text(minecraft_status.java),
        inline=False,
    )
    embed.add_field(
        name=f"{minecraft_status.bedrock.edition} · {BEDROCK_PORT}",
        value=endpoint_status_text(minecraft_status.bedrock),
        inline=False,
    )
    embed.set_footer(text="Tự động cập nhật; Discord online là số tổng hợp, không lưu danh tính")
    return embed


async def craftopia_wallet_api(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
    params: dict | None = None,
) -> tuple[int, dict]:
    """Call Craftopia Wallet API without exposing the bot API key."""
    if not CRAFTOPIA_BOT_API_KEY:
        return 503, {"error": "Bot chưa được cấu hình CRAFTOPIA_BOT_API_KEY."}

    url = f"{CRAFTOPIA_WALLET_API_BASE}/{path.lstrip(chr(47))}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-craftopia-bot-key": CRAFTOPIA_BOT_API_KEY,
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(
                method.upper(),
                url,
                headers=headers,
                json=payload if payload is not None else None,
                params=params,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    text_body = (await response.text())[:500]
                    data = {"error": text_body or "API trả dữ liệu không hợp lệ."}
                return response.status, data if isinstance(data, dict) else {"data": data}
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning("Craftopia Wallet API request failed: %s", exc)
        return 503, {"error": "Không kết nối được Craftopia Wallet API."}

class StaffHelpView(discord.ui.View):
    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=600)
        self.requester_id = requester_id

    @discord.ui.button(label="Gọi staff", style=discord.ButtonStyle.secondary, emoji="🛟")
    async def call_staff(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Nút này dành cho người đã đặt câu hỏi.", ephemeral=True)
            return
        role = interaction.guild.get_role(STAFF_ROLE_ID) if interaction.guild and STAFF_ROLE_ID else None
        if role is None:
            await interaction.response.send_message(
                "Admin chưa cấu hình `STAFF_ROLE_ID`. Bạn hãy tag staff thủ công.", ephemeral=True
            )
            return
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"{role.mention}, {interaction.user.mention} cần hỗ trợ thêm tại đây.",
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=True, users=True, replied_user=False
            ),
        )


class CraftopiaBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = TRACK_DISCORD_PRESENCE
        intents.presences = TRACK_DISCORD_PRESENCE
        super().__init__(
            command_prefix=("~", "!"),
            intents=intents,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name=f"by {CRAFTOPIA_AUTHOR}",
            ),
        )
        self.knowledge = KnowledgeBase(ROOT / "knowledge")
        self.ai = MinecraftSupportAI(self.knowledge)
        self.music = MusicManager(self, MusicConfig.from_env())
        self.ai_slots = asyncio.Semaphore(int(os.getenv("MAX_AI_CONCURRENCY", "3")))
        self.convert_slots = asyncio.Semaphore(1)
        self.knowledge_write_lock = asyncio.Lock()
        self.status_lock = asyncio.Lock()
        self.stats_dashboard_lock = asyncio.Lock()
        self.stats_update_lock = asyncio.Lock()
        self.message_cleanup_locks: dict[int, asyncio.Lock] = {}
        self.message_cleanup_last_scan: dict[tuple[int, int], float] = {}
        self.incident_monitor = IncidentMonitor(
            window_seconds=MONITOR_WINDOW_SECONDS,
            cooldown_seconds=MONITOR_COOLDOWN_SECONDS,
            max_alerts_per_hour=MONITOR_MAX_ALERTS_PER_HOUR,
        )
        self.last_mc_status: MinecraftStatus | None = None
        self.last_mc_status_at = 0.0
        self.health = {
            "java": {"failures": 0, "successes": 0, "alerted": False},
            "bedrock": {"failures": 0, "successes": 0, "alerted": False},
        }
        self.background_tasks: set[asyncio.Task] = set()
        self.stats_dashboard_target = _load_stats_target()
        if STATS_CHANNEL_ID and self.stats_dashboard_target.get("channel_id") != STATS_CHANNEL_ID:
            self.stats_dashboard_target = {"channel_id": STATS_CHANNEL_ID}

    async def setup_hook(self) -> None:
        if self.music.config.enabled:
            await self.add_cog(MusicCog(self, self.music))
            self.add_view(self.music.control_view)
        if os.getenv("SYNC_COMMANDS", "true").lower() == "true":
            try:
                if DISCORD_GUILD_ID:
                    guild = discord.Object(id=DISCORD_GUILD_ID)
                    self.tree.copy_global_to(guild=guild)
                    synced = await self.tree.sync(guild=guild)
                    logger.info("Synced %s slash commands to guild %s", len(synced), DISCORD_GUILD_ID)
                else:
                    synced = await self.tree.sync()
                    logger.info("Synced %s global slash commands", len(synced))
            except discord.HTTPException:
                logger.exception("Slash command sync failed; prefix command ~ai is still available")
        self.create_background_task(self.status_monitor_loop())
        self.create_background_task(self.stats_dashboard_loop())

    async def close(self) -> None:
        logger.info(
            "CraftopiaBot.close started build=%s background_tasks=%s",
            BOT_BUILD,
            len(self.background_tasks),
        )
        completed = False
        try:
            for task in self.background_tasks:
                task.cancel()
            if self.background_tasks:
                await asyncio.gather(*self.background_tasks, return_exceptions=True)
            await self.music.close()
            await self.ai.close()
            await super().close()
            completed = True
        finally:
            logger.info("CraftopiaBot.close finished build=%s completed=%s", BOT_BUILD, completed)

    def create_background_task(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def refresh_mc_status(self, notify: bool = False) -> MinecraftStatus:
        loop = asyncio.get_running_loop()
        if (
            not notify
            and self.last_mc_status is not None
            and loop.time() - self.last_mc_status_at < STATUS_CACHE_SECONDS
        ):
            return self.last_mc_status
        async with self.status_lock:
            if (
                not notify
                and self.last_mc_status is not None
                and loop.time() - self.last_mc_status_at < STATUS_CACHE_SECONDS
            ):
                return self.last_mc_status
            status = await query_minecraft_status(MC_HOST, JAVA_PORT, BEDROCK_PORT)
            self.last_mc_status = status
            self.last_mc_status_at = loop.time()
        if notify:
            await self.process_status_change(status)
        return status

    async def process_status_change(self, status: MinecraftStatus) -> None:
        outages: list[tuple[EndpointStatus, dict[str, int | bool]]] = []
        recoveries: list[tuple[EndpointStatus, dict[str, int | bool]]] = []
        for key, endpoint in (("java", status.java), ("bedrock", status.bedrock)):
            state = self.health[key]
            if endpoint.online:
                state["failures"] = 0
                state["successes"] = min(state["successes"] + 1, STATUS_RECOVERY_THRESHOLD)
                if state["alerted"] and state["successes"] >= STATUS_RECOVERY_THRESHOLD:
                    recoveries.append((endpoint, state))
                continue
            if getattr(endpoint, "error_kind", None) == "protocol":
                state["failures"] = 0
                state["successes"] = 0
                logger.warning("Cannot parse %s status response: %s", endpoint.edition, endpoint.error)
                continue
            state["successes"] = 0
            state["failures"] += 1
            if state["failures"] >= STATUS_FAILURE_THRESHOLD and not state["alerted"]:
                outages.append((endpoint, state))

        if outages:
            editions = " và ".join(endpoint.edition for endpoint, _ in outages)
            sent = await self.send_status_alert(
                f"⚠️ **{editions} của Craftopia không phản hồi** sau ít nhất "
                f"{STATUS_FAILURE_THRESHOLD} lần kiểm tra liên tiếp. Staff vui lòng kiểm tra server."
            )
            if sent:
                for _, state in outages:
                    state["alerted"] = True

        if recoveries:
            editions = " và ".join(endpoint.edition for endpoint, _ in recoveries)
            sent = await self.send_status_alert(
                f"✅ **{editions} của Craftopia đã hoạt động lại** — đã phản hồi "
                f"{STATUS_RECOVERY_THRESHOLD} lần kiểm tra liên tiếp."
            )
            if sent:
                for _, state in recoveries:
                    state["alerted"] = False

    async def send_status_alert(self, content: str) -> bool:
        channel_id = STATUS_CHANNEL_ID or next(iter(SUPPORT_CHANNEL_IDS), 0)
        if not channel_id:
            return False
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.HTTPException, aiohttp.ClientError, OSError):
                return False
        if not callable(getattr(channel, "send", None)):
            logger.warning("STATUS_CHANNEL_ID %s is not a message channel", channel_id)
            return False
        role = channel.guild.get_role(STAFF_ROLE_ID) if getattr(channel, "guild", None) and STAFF_ROLE_ID else None
        if role:
            content += f"\n{role.mention}"
        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=bool(role), users=False, replied_user=False
                ),
            )
            return True
        except (
            discord.Forbidden,
            discord.NotFound,
            discord.HTTPException,
            aiohttp.ClientError,
            OSError,
        ):
            logger.warning("Cannot send Minecraft status alert to channel %s", channel_id)
            return False

    async def status_monitor_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self.refresh_mc_status(notify=True)
            except Exception:
                logger.exception("Minecraft status monitor failed")
            await asyncio.sleep(STATUS_CHECK_SECONDS)

    async def get_discord_guild_stats(self, guild: discord.Guild) -> DiscordGuildStats:
        if TRACK_DISCORD_PRESENCE:
            try:
                if not guild.chunked:
                    await asyncio.wait_for(guild.chunk(cache=True), timeout=45)
                total = guild.member_count or len(guild.members)
                online = sum(member.status is not discord.Status.offline for member in guild.members)
                cache_complete = guild.chunked and len(guild.members) >= total
                return DiscordGuildStats(
                    members=total,
                    online=online,
                    approximate=not cache_complete,
                    detail=(
                        "Presence + Members Intent; số đang online gồm online/idle/dnd."
                        if cache_complete
                        else "Member cache chưa đầy nên số đang online chỉ xấp xỉ."
                    ),
                )
            except (discord.Forbidden, discord.HTTPException, discord.ClientException, TimeoutError):
                logger.warning("Cannot build exact Discord presence snapshot for guild %s", guild.id)

        try:
            snapshot = await self.fetch_guild(guild.id, with_counts=True)
            members = snapshot.approximate_member_count
            online = snapshot.approximate_presence_count
            if members is None:
                members = guild.member_count
            return DiscordGuildStats(
                members=members,
                online=online,
                approximate=True,
                detail="Số xấp xỉ do Discord cung cấp; online gồm online/idle/dnd.",
            )
        except discord.HTTPException:
            logger.warning("Cannot fetch aggregate Discord counts for guild %s", guild.id)
            return DiscordGuildStats(
                members=guild.member_count,
                online=None,
                approximate=True,
                detail="Discord chưa trả về số online trong lần cập nhật này.",
            )

    async def configure_stats_dashboard(
        self,
        guild: discord.Guild,
        channel: discord.abc.Messageable,
    ) -> bool:
        channel_id = getattr(channel, "id", 0)
        if not channel_id:
            return False
        async with self.stats_update_lock:
            async with self.stats_dashboard_lock:
                current_target = dict(self.stats_dashboard_target)
            target = {
                "guild_id": guild.id,
                "channel_id": channel_id,
            }
            if current_target.get("channel_id") == channel_id:
                message_id = current_target.get("message_id")
                if message_id:
                    target["message_id"] = message_id
            updated_target, created_message = await self._render_stats_dashboard(target)
            if updated_target is None:
                return False
            return await self._commit_stats_dashboard_target(updated_target, created_message)

    async def update_stats_dashboard(self) -> bool:
        async with self.stats_update_lock:
            async with self.stats_dashboard_lock:
                target = dict(self.stats_dashboard_target)
            updated_target, created_message = await self._render_stats_dashboard(target)
            if updated_target is None:
                return False
            return await self._commit_stats_dashboard_target(updated_target, created_message)

    async def _render_stats_dashboard(
        self,
        original_target: dict[str, int],
    ) -> tuple[dict[str, int] | None, discord.Message | None]:
        target = dict(original_target)
        channel_id = target.get("channel_id", 0)
        if not channel_id:
            return None, None
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.warning("Stats channel %s is unavailable", channel_id)
                return None, None
        guild = getattr(channel, "guild", None)
        if not isinstance(guild, discord.Guild):
            logger.warning("Stats channel %s does not belong to a guild", channel_id)
            return None, None
        if target.get("guild_id") not in (None, guild.id):
            logger.warning("Stats target guild/channel mismatch for channel %s", channel_id)
            return None, None

        guild_stats, minecraft_status = await asyncio.gather(
            self.get_discord_guild_stats(guild),
            self.refresh_mc_status(notify=False),
        )
        embed = server_stats_embed(guild, guild_stats, minecraft_status)
        message = None
        created_message = None
        message_id = target.get("message_id", 0)
        if message_id:
            fetch_message = getattr(channel, "fetch_message", None)
            if not callable(fetch_message):
                logger.warning("Stats channel %s cannot fetch messages", channel_id)
                return None, None
            try:
                message = await fetch_message(message_id)
            except discord.NotFound:
                message = None
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("Cannot fetch dashboard message %s", message_id)
                return None, None
        try:
            if message is None:
                send = getattr(channel, "send", None)
                if not callable(send):
                    return None, None
                message = await send(embed=embed, allowed_mentions=ALLOWED_MENTIONS_NONE)
                created_message = message
                target["message_id"] = message.id
            else:
                await message.edit(embed=embed, allowed_mentions=ALLOWED_MENTIONS_NONE)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.warning("Cannot create or update stats dashboard in channel %s", channel_id)
            return None, None

        target["guild_id"] = guild.id
        target["channel_id"] = channel_id
        return target, created_message

    async def _commit_stats_dashboard_target(
        self,
        target: dict[str, int],
        created_message: discord.Message | None,
    ) -> bool:
        try:
            await asyncio.to_thread(_write_stats_target, STATS_STATE_PATH, target)
        except OSError:
            logger.exception("Cannot persist stats dashboard state")
            if created_message is not None:
                try:
                    await created_message.delete()
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.warning("Cannot roll back dashboard message after persistence failure")
            return False
        async with self.stats_dashboard_lock:
            self.stats_dashboard_target = target
        return True

    async def stats_dashboard_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if self.stats_dashboard_target.get("channel_id"):
                    await self.update_stats_dashboard()
            except Exception:
                logger.exception("Stats dashboard update failed")
            await asyncio.sleep(STATS_UPDATE_SECONDS)

    async def create_ai_answer(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        question: str,
        attachments: list[discord.Attachment],
    ) -> AIAnswer:
        question = question.strip()[:4000]
        if not question and not attachments:
            return AIAnswer("Bạn hãy nhập câu hỏi hoặc gửi kèm ảnh lỗi.", ())
        try:
            await asyncio.wait_for(self.ai_slots.acquire(), timeout=8)
        except TimeoutError:
            return AIAnswer("Bot đang xử lý nhiều câu hỏi. Bạn vui lòng thử lại sau một lát.", ())
        try:
            images: list[ImageInput] = []
            remaining_image_bytes = MAX_IMAGE_BYTES
            for attachment in attachments[:2]:
                mime = (attachment.content_type or "").lower()
                if (
                    not mime.startswith("image/")
                    or attachment.size <= 0
                    or attachment.size > remaining_image_bytes
                ):
                    continue
                data = await attachment.read()
                if len(data) > remaining_image_bytes:
                    continue
                images.append(ImageInput(mime, data))
                remaining_image_bytes -= len(data)
            if not question and not images:
                return AIAnswer("Bot chỉ đọc ảnh PNG/JPEG/WebP tối đa dung lượng cho phép.", ())
            if not question:
                question = "Hãy đọc ảnh lỗi này và hướng dẫn người chơi xử lý theo tài liệu Craftopia."
            return await asyncio.wait_for(
                self.ai.answer((guild_id, channel_id, user_id), user_id, question, images),
                timeout=55,
            )
        except AIProviderError as exc:
            logger.warning("Gemini request failed: %s", exc)
            return AIAnswer(f"AI tạm thời chưa trả lời được: {exc}", (), True)
        except TimeoutError:
            return AIAnswer("AI phản hồi quá chậm. Bạn vui lòng thử lại hoặc gọi staff.", (), True)
        except Exception:
            logger.exception("Unexpected AI error")
            return AIAnswer("AI đang gặp lỗi tạm thời. Bạn hãy thử lại sau hoặc gọi staff.", (), True)
        finally:
            self.ai_slots.release()


def _cleanup_authorization_error(
    guild: discord.Guild,
    channel: object,
    actor: discord.Member,
    target: discord.Member,
    scope: str,
) -> str | None:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return "Lệnh này chỉ dùng trong kênh chat hoặc thread Discord."
    actor_permissions = permissions_for(actor)
    if not (
        actor_permissions.view_channel
        and actor_permissions.read_message_history
        and actor_permissions.manage_messages
    ):
        return "Bạn cần quyền **Manage Messages** và **Read Message History** trong kênh này."
    if scope == "guild" and not actor.guild_permissions.administrator:
        return "Phạm vi toàn server chỉ dành cho thành viên có quyền **Administrator**."
    target_is_protected = target.id == guild.owner_id or target.guild_permissions.administrator
    if target_is_protected and actor.id != guild.owner_id:
        return "Chỉ chủ server mới được chuẩn bị xoá tin nhắn của Owner/Administrator."
    if scope == "channel":
        bot_member = guild.me
        if bot_member is None:
            return "Bot chưa xác định được quyền của chính mình."
        bot_permissions = permissions_for(bot_member)
        if not (
            bot_permissions.view_channel
            and bot_permissions.read_message_history
            and bot_permissions.manage_messages
        ):
            return "Bot cần View Channel, Read Message History và Manage Messages trong kênh này."
    return None


def _safe_member_label(member: discord.Member) -> str:
    label = discord.utils.escape_markdown(discord.utils.escape_mentions(member.display_name))
    return f"{label[:80]} (`{member.id}`)"


class DeleteMessagesConfirmView(discord.ui.View):
    def __init__(
        self,
        bot_instance: CraftopiaBot,
        requester_id: int,
        target_user_id: int,
        guild_id: int,
        scope: str,
        snapshot: DeleteScanSnapshot,
    ) -> None:
        super().__init__(timeout=DELETE_CONFIRM_SECONDS)
        self.bot_instance = bot_instance
        self.requester_id = requester_id
        self.target_user_id = target_user_id
        self.guild_id = guild_id
        self.scope = scope
        self.snapshot = snapshot
        self.completed = False
        self.action_lock = asyncio.Lock()
        self.message: object | None = None

    def _disable_buttons(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Chỉ người tạo bản xem trước mới được xác nhận hoặc huỷ.", ephemeral=True
            )
            return False
        if self.completed:
            await interaction.response.send_message("Yêu cầu này đã kết thúc.", ephemeral=True)
            return False
        if self.action_lock.locked():
            await interaction.response.send_message("Yêu cầu này đang được xử lý.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.completed:
            return
        self.completed = True
        self._disable_buttons()
        edit = getattr(self.message, "edit", None)
        if callable(edit):
            try:
                await edit(view=self)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(label="Xác nhận xoá", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with self.action_lock:
            if self.completed:
                await interaction.response.send_message("Yêu cầu này đã kết thúc.", ephemeral=True)
                return
            if not interaction.guild or interaction.guild.id != self.guild_id:
                await interaction.response.send_message("Không xác định được server Discord.", ephemeral=True)
                return
            if not isinstance(interaction.user, discord.Member) or interaction.channel is None:
                await interaction.response.send_message("Không xác định được quyền hiện tại.", ephemeral=True)
                return
            await interaction.response.defer()
            try:
                # Never authorize from the member cache: roles may have changed since preview.
                target = await interaction.guild.fetch_member(self.target_user_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                await interaction.followup.send(
                    "Không thể kiểm tra lại thành viên mục tiêu; thao tác đã dừng an toàn.",
                    ephemeral=True,
                )
                return
            authorization_error = _cleanup_authorization_error(
                interaction.guild,
                interaction.channel,
                interaction.user,
                target,
                self.scope,
            )
            if authorization_error:
                await interaction.followup.send(authorization_error, ephemeral=True)
                return

            self.completed = True
            self._disable_buttons()
            await interaction.edit_original_response(
                content="⏳ Đang xoá đúng các tin nhắn trong bản xem trước…",
                view=self,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )
            guild_lock = self.bot_instance.message_cleanup_locks.setdefault(
                self.guild_id, asyncio.Lock()
            )
            reason = (
                f"Craftopia cleanup actor={self.requester_id} target={self.target_user_id} "
                f"scope={self.scope} date="
                f"{self.snapshot.ended_at_utc.astimezone(VIETNAM_TZ).date().isoformat()}"
            )
            try:
                async with guild_lock:
                    result = await delete_snapshot_messages(
                        self.bot_instance,
                        self.snapshot,
                        reason,
                    )
            except Exception:
                logger.exception(
                    "Unexpected message cleanup failure guild=%s actor=%s target=%s scope=%s",
                    self.guild_id,
                    self.requester_id,
                    self.target_user_id,
                    self.scope,
                )
                await interaction.edit_original_response(
                    content=(
                        "❌ Discord gặp lỗi bất ngờ trong lúc xử lý. Bot đã dừng; "
                        "một phần tin nhắn có thể đã được xoá trước khi lỗi xảy ra. "
                        "Hãy kiểm tra kênh trước khi tạo yêu cầu mới."
                    ),
                    view=self,
                    allowed_mentions=ALLOWED_MENTIONS_NONE,
                )
                return
            logger.info(
                "message_cleanup guild=%s actor=%s target=%s scope=%s matched=%s "
                "deleted=%s missing=%s failed=%s",
                self.guild_id,
                self.requester_id,
                self.target_user_id,
                self.scope,
                self.snapshot.matched_messages,
                result.deleted,
                result.already_missing,
                result.failed,
            )
            status_emoji = "✅" if not result.failed else "⚠️"
            lines = [
                f"{status_emoji} Đã xoá **{result.deleted}** tin nhắn trong bản xem trước.",
            ]
            if result.already_missing:
                lines.append(f"Tin đã được xoá trước đó: **{result.already_missing}**.")
            if result.failed:
                lines.append(
                    f"Không xoá được: **{result.failed}** tin tại "
                    f"**{result.failed_channels}** kênh. Kiểm tra quyền bot."
                )
            lines.append("Thao tác xoá không thể hoàn tác.")
            await interaction.edit_original_response(
                content="\n".join(lines),
                view=self,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )

    @discord.ui.button(label="Huỷ", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def cancel_delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        async with self.action_lock:
            if self.completed:
                await interaction.response.send_message("Yêu cầu này đã kết thúc.", ephemeral=True)
                return
            self.completed = True
            self._disable_buttons()
            await interaction.response.edit_message(
                content="Đã huỷ. Không có tin nhắn nào bị xoá.",
                view=self,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )


async def prepare_delete_messages_preview(
    bot_instance: CraftopiaBot,
    guild: discord.Guild,
    channel: object,
    actor: discord.Member,
    target: discord.Member,
    request: DeleteMessagesRequest,
    invocation_message_id: int | None,
) -> tuple[str, DeleteMessagesConfirmView | None]:
    loop = asyncio.get_running_loop()
    cooldown_key = (guild.id, actor.id)
    last_scan = bot_instance.message_cleanup_last_scan.get(cooldown_key, 0.0)
    remaining = DELETE_SCAN_COOLDOWN_SECONDS - (loop.time() - last_scan)
    if remaining > 0:
        return f"Bạn vừa yêu cầu quét tin nhắn. Hãy thử lại sau **{remaining:.0f} giây**.", None
    bot_instance.message_cleanup_last_scan[cooldown_key] = loop.time()

    try:
        # Fetch from Discord before every preview so a stale role cache cannot bypass protection.
        target = await guild.fetch_member(target.id)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        return "❌ Không thể kiểm tra mới quyền của thành viên mục tiêu; bot chưa quét hoặc xoá gì.", None

    authorization_error = _cleanup_authorization_error(
        guild, channel, actor, target, request.scope
    )
    if authorization_error:
        return f"❌ {authorization_error}", None

    guild_lock = bot_instance.message_cleanup_locks.setdefault(guild.id, asyncio.Lock())
    if guild_lock.locked():
        return "Một thao tác xoá khác đang chạy trong server. Hãy thử lại sau.", None
    async with guild_lock:
        snapshot = await scan_today_messages(
            guild=guild,
            current_channel=channel,
            actor=actor,
            target_user_id=target.id,
            scope=request.scope,
            invocation_message_id=invocation_message_id,
            scan_limit_per_channel=DELETE_SCAN_LIMIT_PER_CHANNEL,
            max_matches=DELETE_MAX_MESSAGES,
            scan_limit_total=DELETE_SCAN_LIMIT_TOTAL,
        )

    if snapshot.overflow:
        return (
            f"❌ Tìm thấy hơn **{DELETE_MAX_MESSAGES}** tin nhắn. Bot chưa xoá gì; "
            "hãy thu hẹp về kênh hiện tại hoặc tăng `DELETE_MAX_MESSAGES` có kiểm soát.",
            None,
        )
    if snapshot.truncated:
        return (
            f"❌ Phạm vi vượt giới hạn quét (**{DELETE_SCAN_LIMIT_PER_CHANNEL}** tin/kênh, "
            f"**{DELETE_SCAN_LIMIT_TOTAL}** tin tổng). "
            "Bot chưa xoá gì để tránh kết quả không đầy đủ.",
            None,
        )
    if request.scope == "guild" and snapshot.skipped_channels:
        return (
            f"❌ Không quét được **{snapshot.skipped_channels}** kênh do thiếu quyền hoặc lỗi Discord. "
            "Bot chưa xoá gì vì phạm vi toàn server phải được quét đầy đủ.",
            None,
        )
    if snapshot.matched_messages == 0:
        return (
            f"Không tìm thấy tin nhắn có thể xoá của **{_safe_member_label(target)}** "
            "trong hôm nay. Tin ghim, tin hệ thống và chính tin ra lệnh không được tính.",
            None,
        )

    date_label = snapshot.ended_at_utc.astimezone(VIETNAM_TZ).strftime("%d/%m/%Y")
    if request.scope == "guild":
        scope_label = (
            f"toàn server trong **{snapshot.scanned_channels}** kênh/text thread công khai đang hoạt động"
        )
    else:
        scope_label = getattr(channel, "mention", "kênh hiện tại")
    skipped_total = snapshot.skipped_pinned + snapshot.skipped_system + snapshot.skipped_invocation
    lines = [
        "⚠️ **Bản xem trước xoá tin nhắn**",
        f"Mục tiêu: **{_safe_member_label(target)}**",
        f"Thời gian: hôm nay **{date_label}**, từ 00:00 đến lúc quét (UTC+7)",
        f"Phạm vi: **{scope_label}**",
        f"Sẽ xoá vĩnh viễn: **{snapshot.matched_messages}** tin nhắn",
    ]
    if skipped_total:
        lines.append(
            f"Đã giữ lại: **{snapshot.skipped_pinned}** tin ghim, "
            f"**{snapshot.skipped_system}** tin hệ thống và "
            f"**{snapshot.skipped_invocation}** tin ra lệnh."
        )
    if snapshot.skipped_channels:
        lines.append(
            f"Không quét được **{snapshot.skipped_channels}** kênh do quyền/loại kênh; "
            "thread lưu trữ và private thread không thuộc phạm vi toàn server."
        )
    lines.append(
        f"Chỉ đúng các ID trong bản xem trước sẽ bị xoá. Bấm xác nhận trong "
        f"**{DELETE_CONFIRM_SECONDS} giây**; thao tác không thể hoàn tác."
    )
    view = DeleteMessagesConfirmView(
        bot_instance=bot_instance,
        requester_id=actor.id,
        target_user_id=target.id,
        guild_id=guild.id,
        scope=request.scope,
        snapshot=snapshot,
    )
    return "\n".join(lines), view


bot = CraftopiaBot()


async def save_ai_fact(topic: str, fact: str) -> tuple[str, int, bool]:
    if "\x00" in topic:
        raise ValueError("Chủ đề chứa dữ liệu nhị phân không hợp lệ")
    topic = re.sub(r"[\r\n#]+", " ", topic).strip()
    fact = _clean_training_text(fact, "Nội dung")
    if not topic:
        raise ValueError("Chủ đề không được để trống")
    if len(topic) > 100:
        raise ValueError("Chủ đề tối đa 100 ký tự")
    if len(fact) > MAX_TRAINING_CHARS:
        raise ValueError(
            f"Nội dung có {len(fact):,} ký tự, vượt giới hạn {MAX_TRAINING_CHARS:,}; bot không cắt âm thầm"
        )
    if has_sensitive_training_data(topic + "\n" + fact):
        raise ValueError(
            "Phát hiện dữ liệu giống token, API key, mật khẩu, webhook hoặc private key; bot từ chối lưu"
        )
    fingerprint = hashlib.sha256((topic + "\n" + fact).encode("utf-8")).hexdigest()
    destination = ROOT / "knowledge" / "managed" / f"{fingerprint}.md"
    entry = (
        f"# {topic}\n\n{fact}\n\n"
        f"_Cập nhật: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}_\n"
    )
    async with bot.knowledge_write_lock:
        created = not destination.exists()
        if created:
            await asyncio.to_thread(_atomic_write_utf8, destination, entry)
        count = bot.knowledge.reload()
    return topic, count, created


async def resolve_training_reply(message: discord.Message) -> discord.Message | None:
    reference = message.reference
    if reference is None or reference.message_id is None:
        return None
    resolved = reference.resolved
    if isinstance(resolved, discord.Message):
        replied = resolved
    else:
        fetch_message = getattr(message.channel, "fetch_message", None)
        if not callable(fetch_message):
            raise ValueError("Không thể đọc tin nhắn được reply trong kênh này")
        try:
            replied = await fetch_message(reference.message_id)
        except discord.NotFound as exc:
            raise ValueError("Tin nhắn được reply đã bị xóa hoặc không còn tồn tại") from exc
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise ValueError("Bot thiếu quyền Read Message History để đọc tin nhắn được reply") from exc
    if replied.guild is None or message.guild is None or replied.guild.id != message.guild.id:
        raise ValueError("Chỉ có thể học tin nhắn trong cùng server Discord")
    if replied.author.bot or replied.webhook_id:
        raise ValueError("Bot không học lại tin nhắn của bot/webhook để tránh vòng lặp sai kiến thức")
    if replied.author.id != message.author.id:
        raise ValueError(
            "Để bảo vệ quyền riêng tư, ~hl chỉ học tin nhắn do chính người chạy lệnh đã gửi"
        )
    return replied


async def read_training_documents(
    attachments: list[discord.Attachment],
) -> tuple[tuple[str, str], ...]:
    if len(attachments) > MAX_TRAINING_FILES:
        raise ValueError(f"Mỗi lần ~hl nhận tối đa {MAX_TRAINING_FILES} tệp")
    total_size = sum(max(0, attachment.size) for attachment in attachments)
    if total_size > MAX_TRAINING_FILE_BYTES:
        raise ValueError("Tổng tệp .md/.txt vượt giới hạn 1 MB")
    documents: list[tuple[str, str]] = []
    for attachment in attachments:
        if Path(attachment.filename).suffix.lower() not in {".md", ".txt"}:
            raise ValueError("~hl chỉ đọc tệp `.md` hoặc `.txt` dùng mã hóa UTF-8")
        try:
            raw = await attachment.read()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            raise ValueError(f"Không thể tải tệp {attachment.filename}") from exc
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Tệp {attachment.filename} phải dùng mã hóa UTF-8") from exc
        if "\x00" in content:
            raise ValueError(f"Tệp {attachment.filename} có dữ liệu nhị phân không hợp lệ")
        documents.append((attachment.filename, content))
    return tuple(documents)


async def send_interaction_answer(interaction: discord.Interaction, answer: AIAnswer) -> None:
    parts = split_discord_text(format_answer(answer))
    view = StaffHelpView(interaction.user.id)
    await interaction.followup.send(parts[0], view=view, allowed_mentions=ALLOWED_MENTIONS_NONE)
    for part in parts[1:]:
        await interaction.followup.send(part, allowed_mentions=ALLOWED_MENTIONS_NONE)


async def send_message_answer(message: discord.Message, answer: AIAnswer) -> None:
    parts = split_discord_text(format_answer(answer))
    view = StaffHelpView(message.author.id)
    await message.reply(
        parts[0],
        view=view,
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )
    for part in parts[1:]:
        await message.channel.send(part, allowed_mentions=ALLOWED_MENTIONS_NONE)


async def resolve_cleanup_target(
    guild: discord.Guild,
    target_user_id: int,
    mentioned_users: list[discord.Member | discord.User],
) -> discord.Member | None:
    target = guild.get_member(target_user_id)
    if target is None:
        target = next(
            (
                user
                for user in mentioned_users
                if user.id == target_user_id and isinstance(user, discord.Member)
            ),
            None,
        )
    if target is None:
        try:
            target = await guild.fetch_member(target_user_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return None
    return target


async def handle_natural_delete_request(
    message: discord.Message,
    request: DeleteMessagesRequest,
) -> None:
    if not message.guild or not isinstance(message.author, discord.Member):
        return
    target = await resolve_cleanup_target(message.guild, request.target_user_id, message.mentions)
    if target is None:
        await message.reply(
            "Không tìm thấy thành viên mục tiêu trong server Craftopia.",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )
        return
    processing = await message.reply(
        "🔎 Đang quét lịch sử tin nhắn hôm nay; nội dung không được lưu hoặc gửi tới Gemini…",
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )
    content, view = await prepare_delete_messages_preview(
        bot_instance=bot,
        guild=message.guild,
        channel=message.channel,
        actor=message.author,
        target=target,
        request=request,
        invocation_message_id=message.id,
    )
    await processing.edit(
        content=content,
        view=view,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )
    if view is not None:
        view.message = processing


async def handle_incident_signal(
    channel: discord.abc.Messageable,
    guild: discord.Guild,
    signal: IncidentSignal,
) -> None:
    notify_staff = signal.category == "conflict"
    if signal.category == "conflict":
        content = (
            "🛡️ Mình nhận thấy cuộc trò chuyện đang có dấu hiệu căng thẳng. "
            "Mọi người vui lòng dừng công kích, không đáp trả thêm và giữ bằng chứng để staff xử lý. "
            "Bot không kết luận ai đúng hoặc sai."
        )
    else:
        try:
            status = await bot.refresh_mc_status(notify=False)
            status_line = concise_status(status)
            notify_staff = not (status.java.online and status.bedrock.online)
            if notify_staff:
                content = (
                    "⚠️ Mình ghi nhận nhiều báo cáo về kết nối Craftopia. "
                    f"Kiểm tra trực tiếp hiện tại: **{status_line}**. "
                    "Hệ thống chưa xác định nguyên nhân; staff vui lòng kiểm tra thêm."
                )
            else:
                content = (
                    "📡 Mình vừa kiểm tra Craftopia sau khi nhận nhiều báo cáo: "
                    f"**{status_line}**. Hai cổng vẫn đang phản hồi. "
                    "Nếu bạn vẫn gặp lỗi, hãy gửi nền tảng Java/Bedrock, phiên bản và ảnh lỗi để staff đối chiếu."
                )
        except Exception:
            logger.exception("Incident-triggered Minecraft status check failed")
            notify_staff = True
            content = (
                "⚠️ Mình ghi nhận nhiều báo cáo về kết nối Craftopia nhưng chưa kiểm tra được trạng thái. "
                "Staff vui lòng kiểm tra server; người chơi hãy gửi nền tảng, phiên bản và ảnh lỗi."
            )

    role = guild.get_role(STAFF_ROLE_ID) if notify_staff and STAFF_ROLE_ID else None
    if role:
        content += f"\n{role.mention}"
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                roles=bool(role),
                users=False,
                replied_user=False,
            ),
        )
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        logger.warning("Cannot send incident notice to channel %s", signal.channel_id)


@bot.tree.command(name="link", description="Liên kết Discord với tài khoản Craftopia Wallet")
@app_commands.describe(code="Mã liên kết 8 ký tự tạo trên store.craftopics.online")
@app_commands.checks.cooldown(4, 60.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def link_wallet(interaction: discord.Interaction, code: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)

    normalized = re.sub(r"[^A-Z0-9]", "", code.upper())
    if len(normalized) != 8:
        await interaction.followup.send(
            "❌ Mã liên kết phải có **8 ký tự**. Hãy tạo mã mới trên `store.craftopics.online`.",
            ephemeral=True,
        )
        return

    status, data = await craftopia_wallet_api(
        "POST",
        "link",
        payload={
            "code": normalized,
            "discordId": str(interaction.user.id),
            "discordUsername": interaction.user.name,
        },
    )

    if status == 200 and data.get("linked"):
        await interaction.followup.send(
            "✅ **Liên kết thành công!**\n"
            f"Tài khoản Craftopia: **{data.get('username', 'Player')}**\n"
            f"Số dư hiện tại: **{int(data.get('balance', 0)):,} Coin**\n"
            "Bạn có thể dùng `/coin` để xem số dư bất cứ lúc nào.",
            ephemeral=True,
        )
        return

    error = str(data.get("error") or "Không thể liên kết tài khoản.")
    if status == 401:
        error = "Bot chưa được cấp API key hợp lệ. Staff cần kiểm tra `CRAFTOPIA_BOT_API_KEY`."
    elif status == 404:
        error = "Mã liên kết không tồn tại. Hãy tạo **mã mới** trên website rồi thử lại."
    elif status == 409:
        error = "Mã này đã được sử dụng. Hãy tạo mã mới."
    elif status == 410:
        error = "Mã đã hết hạn. Hãy tạo mã mới (mã chỉ dùng trong khoảng 10 phút)."

    await interaction.followup.send(f"❌ {error}", ephemeral=True)


@bot.tree.command(name="coin", description="Xem số dư Coin Craftopia của bạn")
@app_commands.checks.cooldown(6, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def wallet_balance(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    status, data = await craftopia_wallet_api(
        "GET",
        "balance",
        params={"discordId": str(interaction.user.id)},
    )
    if status == 200 and data.get("linked"):
        await interaction.followup.send(
            f"💎 **{data.get('username', 'Player')}**\n"
            f"Số dư: **{int(data.get('balance', 0)):,} Coin**",
            ephemeral=True,
        )
        return
    if status == 404:
        await interaction.followup.send(
            "❌ Discord của bạn chưa liên kết Craftopia Wallet. "
            "Vào `store.craftopics.online`, tạo mã rồi dùng `/link <mã>`.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"❌ {data.get('error', 'Không thể đọc số dư Coin.')}",
        ephemeral=True,
    )

@bot.tree.command(name="ask", description="Hỏi AI hỗ trợ của Craftopia")
@app_commands.describe(question="Câu hỏi về server Craftopia", image="Ảnh lỗi (không bắt buộc)")
@app_commands.checks.cooldown(4, 60.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def ask(interaction: discord.Interaction, question: str, image: discord.Attachment | None = None) -> None:
    if not interaction.guild_id or not interaction.channel_id:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server Craftopia.", ephemeral=True)
        return
    delete_parse = parse_delete_messages_request(
        question,
        resolved_user_ids=set(),
        bot_user_id=bot.user.id if bot.user else 0,
    )
    if delete_parse.destructive:
        await interaction.response.send_message(
            "AI không tự thực hiện thao tác xoá. Hãy dùng `/delete_today` hoặc gửi câu lệnh "
            "trực tiếp với đúng một mention để nhận bản xem trước xác nhận.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(thinking=True)
    answer = await bot.create_ai_answer(
        interaction.guild_id, interaction.channel_id, interaction.user.id,
        question, [image] if image else [],
    )
    await send_interaction_answer(interaction, answer)


@bot.command(name="ai")
@commands.guild_only()
@commands.cooldown(4, 60.0, commands.BucketType.member)
async def ask_prefix(ctx: commands.Context, *, question: str) -> None:
    """Ask Craftopia AI explicitly: ~ai <question>."""
    delete_parse = parse_delete_messages_request(
        question,
        resolved_user_ids={user.id for user in ctx.message.mentions},
        bot_user_id=bot.user.id if bot.user else 0,
    )
    if delete_parse.destructive:
        if delete_parse.request is None:
            await ctx.reply(
                delete_parse.error or "Yêu cầu xoá không hợp lệ.",
                mention_author=False,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )
        else:
            await handle_natural_delete_request(ctx.message, delete_parse.request)
        return
    async with ctx.typing():
        answer = await bot.create_ai_answer(
            ctx.guild.id,
            ctx.channel.id,
            ctx.author.id,
            question,
            ctx.message.attachments,
        )
    await send_message_answer(ctx.message, answer)


@ask_prefix.error
async def ask_prefix_error(ctx: commands.Context, error_value: commands.CommandError) -> None:
    if isinstance(error_value, commands.CommandOnCooldown):
        await ctx.reply(
            f"Bạn hỏi hơi nhanh. Hãy thử lại sau {error_value.retry_after:.0f} giây.",
            mention_author=False,
        )
    elif isinstance(error_value, commands.MissingRequiredArgument):
        await ctx.reply("Cú pháp: `~ai câu hỏi của bạn`", mention_author=False)
    elif isinstance(error_value, commands.NoPrivateMessage):
        return
    else:
        logger.error("~ai failed: %r", error_value)
        await ctx.reply("AI đang gặp lỗi tạm thời.", mention_author=False)


@bot.tree.command(name="ai_reset", description="Xóa ngữ cảnh trò chuyện AI của bạn")
async def ai_reset(interaction: discord.Interaction) -> None:
    if interaction.guild_id and interaction.channel_id:
        bot.ai.memory.clear((interaction.guild_id, interaction.channel_id, interaction.user.id))
    await interaction.response.send_message("Đã xóa ngữ cảnh trò chuyện của bạn.", ephemeral=True)


@bot.tree.command(name="ai_reload", description="Nạp lại tài liệu hỗ trợ Craftopia")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_reload(interaction: discord.Interaction) -> None:
    async with bot.knowledge_write_lock:
        count = bot.knowledge.reload()
    await interaction.response.send_message(f"Đã nạp lại **{count}** phần tài liệu.", ephemeral=True)


@bot.tree.command(name="ai_teach", description="Dạy AI một thông tin chính thức về Craftopia")
@app_commands.describe(topic="Chủ đề, ví dụ: Lệnh survival", fact="Thông tin chính xác AI cần ghi nhớ")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_teach(interaction: discord.Interaction, topic: str, fact: str) -> None:
    try:
        topic, count, created = await save_ai_fact(topic, fact)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    await interaction.response.send_message(
        f"{'Đã dạy' if created else 'Kiến thức này đã tồn tại trong'} AI chủ đề "
        f"**{discord.utils.escape_markdown(discord.utils.escape_mentions(topic))}**. "
        f"Kho kiến thức hiện có {count} phần.",
        ephemeral=True,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )


@bot.command(name="hl")
@commands.guild_only()
@commands.has_permissions(manage_guild=True)
async def train_ai_prefix(ctx: commands.Context, *, lesson: str = "") -> None:
    """Teach Craftopia AI from command text, a replied message and UTF-8 documents."""
    try:
        replied = await resolve_training_reply(ctx.message)
        attachments = list(ctx.message.attachments)
        if replied is not None:
            attachments.extend(replied.attachments)
        supported_attachments = [
            attachment
            for attachment in attachments
            if Path(attachment.filename).suffix.lower() in {".md", ".txt"}
        ]
        ignored_attachments = len(attachments) - len(supported_attachments)
        documents = await read_training_documents(supported_attachments)
        training = build_training_document(
            lesson=lesson,
            replied_text=replied.content if replied is not None else "",
            documents=documents,
        )
        topic, count, created = await save_ai_fact(training.topic, training.fact)
    except ValueError as exc:
        await ctx.reply(
            f"❌ {exc}\n"
            "Cách dùng: `~hl nội dung`, `~hl Chủ đề | nội dung`, hoặc reply tin nhắn/tệp rồi gõ `~hl`.",
            mention_author=False,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )
        return
    display_topic = discord.utils.escape_markdown(discord.utils.escape_mentions(topic))
    verb = "Đã huấn luyện" if created else "Kiến thức này đã có trong"
    await ctx.reply(
        f"✅ {verb} AI chủ đề **{display_topic}** từ **{training.source_count}** nguồn "
        f"({len(training.fact):,} ký tự). Kho kiến thức có {count} phần."
        + (
            f" Đã bỏ qua {ignored_attachments} tệp không phải `.md`/`.txt`."
            if ignored_attachments
            else ""
        ),
        mention_author=False,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )


@train_ai_prefix.error
async def train_ai_prefix_error(ctx: commands.Context, error_value: commands.CommandError) -> None:
    if isinstance(error_value, commands.MissingPermissions):
        await ctx.reply("Bạn cần quyền **Manage Server** để huấn luyện AI.", mention_author=False)
    elif isinstance(error_value, commands.NoPrivateMessage):
        return
    else:
        logger.error("~hl failed: %r", error_value)
        await ctx.reply("Không thể lưu kiến thức lúc này.", mention_author=False)


@bot.tree.command(name="ai_import", description="Nhập tài liệu .md/.txt vào kiến thức Craftopia")
@app_commands.describe(document="Tài liệu luật, lệnh, FAQ hoặc hướng dẫn; tối đa 1 MB")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_import(interaction: discord.Interaction, document: discord.Attachment) -> None:
    suffix = Path(document.filename).suffix.lower()
    if suffix not in {".md", ".txt"} or document.size > 1024 * 1024:
        await interaction.response.send_message("Chỉ nhận `.md`/`.txt` tối đa 1 MB.", ephemeral=True)
        return
    try:
        content = (await document.read()).decode("utf-8-sig")
    except UnicodeDecodeError:
        await interaction.response.send_message("Tài liệu phải dùng mã hóa UTF-8.", ephemeral=True)
        return
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(document.filename).stem).strip("-.")[:80]
    safe_stem = safe_stem or "document"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    destination = ROOT / "knowledge" / "imports" / f"{stamp}-{safe_stem}{suffix}"
    async with bot.knowledge_write_lock:
        await asyncio.to_thread(_write_utf8, destination, content)
        count = bot.knowledge.reload()
    await interaction.response.send_message(
        f"Đã nhập `{document.filename}` và nạp lại {count} phần kiến thức.", ephemeral=True
    )


@bot.tree.command(name="ai_status", description="Xem trạng thái AI hỗ trợ")
async def ai_status(interaction: discord.Interaction) -> None:
    configured = bool(bot.ai.api_key)
    await interaction.response.send_message(
        f"Cấu hình AI: **{'đã có API key' if configured else 'thiếu GEMINI_API_KEY'}**\n"
        f"Model: `{bot.ai.model}`\n"
        f"Kho kiến thức: **{len(bot.knowledge.chunks)}** phần\n"
        f"Kênh tự trả lời: **{len(AI_AUTO_REPLY_CHANNEL_IDS)}**\n"
        "Cách hỏi: `/ask`, `~ai câu hỏi`, mention bot hoặc nhắn trong kênh tự trả lời.\n"
        "Dùng `/ai_test` để kiểm tra kết nối Gemini thật.",
        ephemeral=True,
    )


@bot.tree.command(name="ai_test", description="Kiểm tra kết nối Gemini của Craftopia")
@app_commands.checks.has_permissions(manage_guild=True)
async def ai_test(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        reply = await asyncio.wait_for(bot.ai.health_check(), timeout=30)
        await interaction.followup.send(
            f"✅ Gemini hoạt động. Model: `{bot.ai.model}` · Phản hồi: `{reply}`",
            ephemeral=True,
        )
    except (AIProviderError, TimeoutError) as exc:
        await interaction.followup.send(f"❌ Kiểm tra AI thất bại: {exc}", ephemeral=True)


@bot.tree.command(name="bot_diagnose", description="Kiểm tra cấu hình và quyền của bot")
@app_commands.checks.has_permissions(manage_guild=True)
async def bot_diagnose(interaction: discord.Interaction) -> None:
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server.", ephemeral=True)
        return
    member = interaction.guild.me
    permissions = interaction.channel.permissions_for(member) if member else None
    required = {
        "View Channel": "view_channel",
        "Send Messages": "send_messages",
        "Read Message History": "read_message_history",
        "Manage Messages": "manage_messages",
        "Embed Links": "embed_links",
        "Attach Files": "attach_files",
    }
    if isinstance(interaction.channel, discord.Thread):
        required["Send Messages in Threads"] = "send_messages_in_threads"
    missing = [
        label for label, attribute in required.items()
        if permissions is None or not getattr(permissions, attribute, False)
    ]
    auto_reply_here = channel_has_ai_auto_reply(interaction.channel)
    stats_channel = bot.stats_dashboard_target.get("channel_id", 0)
    presence_mode = (
        "Presence + Members Intent (chi tiết)"
        if TRACK_DISCORD_PRESENCE
        else "số tổng hợp xấp xỉ của Discord (không cần privileged intent)"
    )
    await interaction.response.send_message(
        f"Discord: **đã kết nối**\n"
        f"Message Content Intent trong code: **bật**\n"
        f"Gemini API key: **{'đã có' if bot.ai.api_key else 'thiếu'}**\n"
        f"Model: `{bot.ai.model}`\n"
        f"Tự trả lời tại kênh này: **{'có' if auto_reply_here else 'không'}**\n"
        f"Thống kê Discord: **{presence_mode}**\n"
        f"Kênh dashboard: **{stats_channel or 'chưa cấu hình'}**\n"
        f"Slash sync: **{'guild ' + str(DISCORD_GUILD_ID) if DISCORD_GUILD_ID else 'global'}**\n"
        f"Quyền còn thiếu: **{', '.join(missing) if missing else 'không có'}**\n\n"
        "Nếu AI vẫn lỗi, chạy `/ai_test`. Nếu mention và `~ai` không hoạt động, kiểm tra "
        "Message Content Intent trong Discord Developer Portal.",
        ephemeral=True,
    )


@bot.tree.command(
    name="delete_today",
    description="Xem trước rồi xoá tin nhắn hôm nay của một thành viên",
)
@app_commands.describe(
    target="Thành viên có tin nhắn cần xoá",
    all_server="Bật để quét kênh text và thread công khai đang hoạt động trên toàn server",
)
@app_commands.checks.cooldown(1, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def delete_today(
    interaction: discord.Interaction,
    target: discord.Member,
    all_server: bool = False,
) -> None:
    if not interaction.guild or not interaction.channel or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Lệnh này chỉ dùng trong server Craftopia.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    request = DeleteMessagesRequest(
        target_user_id=target.id,
        scope="guild" if all_server else "channel",
    )
    content, view = await prepare_delete_messages_preview(
        bot_instance=bot,
        guild=interaction.guild,
        channel=interaction.channel,
        actor=interaction.user,
        target=target,
        request=request,
        invocation_message_id=None,
    )
    response_message = await interaction.followup.send(
        content,
        view=view,
        ephemeral=True,
        wait=True,
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )
    if view is not None:
        view.message = response_message


@bot.tree.command(name="ip", description="Xem địa chỉ kết nối Craftopia")
async def server_ip(interaction: discord.Interaction) -> None:
    embed = discord.Embed(title="Kết nối Craftopia", color=discord.Color.green())
    embed.add_field(name="Java", value=f"`{MC_HOST}:{JAVA_PORT}`", inline=False)
    embed.add_field(
        name="Bedrock / PE", value=f"IP: `{MC_HOST}`\nPort: `{BEDROCK_PORT}`", inline=False
    )
    await interaction.response.send_message(embed=embed, allowed_mentions=ALLOWED_MENTIONS_NONE)


@bot.tree.command(name="mcstatus", description="Kiểm tra trạng thái Java và Bedrock của Craftopia")
@app_commands.checks.cooldown(2, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def mcstatus(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    status = await bot.refresh_mc_status(notify=False)
    await interaction.followup.send(
        embed=minecraft_status_embed(status),
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )


@bot.tree.command(name="serverstats", description="Xem số thành viên Discord và trạng thái Minecraft")
@app_commands.checks.cooldown(2, 30.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def serverstats(interaction: discord.Interaction) -> None:
    if not interaction.guild:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server Craftopia.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    guild_stats, status = await asyncio.gather(
        bot.get_discord_guild_stats(interaction.guild),
        bot.refresh_mc_status(notify=False),
    )
    await interaction.followup.send(
        embed=server_stats_embed(interaction.guild, guild_stats, status),
        allowed_mentions=ALLOWED_MENTIONS_NONE,
    )


@bot.tree.command(name="stats_setup", description="Tạo bảng theo dõi Craftopia tự cập nhật tại kênh này")
@app_commands.checks.has_permissions(manage_guild=True)
async def stats_setup(interaction: discord.Interaction) -> None:
    if not interaction.guild or not interaction.channel:
        await interaction.response.send_message("Lệnh này chỉ dùng trong server Craftopia.", ephemeral=True)
        return
    if not callable(getattr(interaction.channel, "send", None)) or not callable(
        getattr(interaction.channel, "fetch_message", None)
    ):
        await interaction.response.send_message("Hãy chạy lệnh trong một kênh chat Discord.", ephemeral=True)
        return
    member = interaction.guild.me
    permissions = interaction.channel.permissions_for(member) if member else None
    missing = [
        label
        for label, attribute in {
            "View Channel": "view_channel",
            "Send Messages": "send_messages",
            "Read Message History": "read_message_history",
            "Embed Links": "embed_links",
        }.items()
        if permissions is None or not getattr(permissions, attribute, False)
    ]
    if missing:
        await interaction.response.send_message(
            "Bot còn thiếu quyền tại kênh này: " + ", ".join(missing),
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        configured = await bot.configure_stats_dashboard(interaction.guild, interaction.channel)
    except OSError:
        logger.exception("Cannot save stats dashboard configuration")
        configured = False
    if configured:
        await interaction.followup.send(
            f"✅ Đã tạo bảng theo dõi; bot cập nhật mỗi {STATS_UPDATE_SECONDS} giây. "
            "Bạn có thể pin tin nhắn đó thủ công.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "❌ Không thể tạo bảng. Kiểm tra quyền View/Send/Embed/Read History và thử lại.",
            ephemeral=True,
        )


@bot.tree.command(name="convert", description="Chuyển pack Oraxen sang Geyser Bedrock")
@app_commands.describe(pack="ZIP chứa resource-pack/ và Oraxen/", fallback_material="Material dự phòng")
@app_commands.checks.cooldown(1, 60.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
async def convert(interaction: discord.Interaction, pack: discord.Attachment, fallback_material: str = "paper") -> None:
    await interaction.response.defer(thinking=True)
    if not pack.filename.lower().endswith(".zip"):
        await interaction.followup.send("File phải có định dạng `.zip`.", ephemeral=True)
        return
    if pack.size > MAX_UPLOAD_BYTES:
        await interaction.followup.send(f"File vượt giới hạn {MAX_UPLOAD_BYTES // 1024 // 1024} MB.", ephemeral=True)
        return
    async with bot.convert_slots:
        try:
            with tempfile.TemporaryDirectory(prefix="oraxen-convert-") as temp:
                source, output = Path(temp) / "source.zip", Path(temp) / "result.zip"
                await pack.save(source)
                summary = await asyncio.to_thread(convert_oraxen_pack, source, output, fallback_material)
                await interaction.followup.send(
                    f"Đã xử lý **{summary.item_count} item**; {summary.warning_count} cảnh báo.",
                    file=discord.File(output, filename="oraxen-geyser-output.zip"),
                )
        except ConversionError as exc:
            await interaction.followup.send(f"Không thể convert: {exc}", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("Kết quả vượt giới hạn upload của Discord.", ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.webhook_id or not message.guild or not bot.user:
        return
    context = await bot.get_context(message)
    if context.valid:
        await bot.invoke(context)
        return

    mentioned = bot.user in message.mentions
    referenced = message.reference.resolved if message.reference else None
    replied_to_bot = (
        isinstance(referenced, discord.Message)
        and referenced.author.id == bot.user.id
    )
    parent_id = getattr(message.channel, "parent_id", None)
    monitor_excluded = (
        message.channel.id in MONITOR_EXCLUDED_CHANNEL_IDS
        or parent_id in MONITOR_EXCLUDED_CHANNEL_IDS
    )
    private_thread = channel_is_private_thread(message.channel)
    is_nsfw = channel_is_nsfw(message.channel)
    auto_reply = channel_has_ai_auto_reply(message.channel)
    if message.content.lstrip().startswith(("~", "!")):
        auto_reply = False
    explicit_request = mentioned or replied_to_bot
    delete_parse = parse_delete_messages_request(
        message.content,
        resolved_user_ids={user.id for user in message.mentions},
        bot_user_id=bot.user.id,
    )
    if delete_parse.destructive and (explicit_request or auto_reply):
        try:
            if delete_parse.request is None:
                await message.reply(
                    delete_parse.error or "Yêu cầu xoá không hợp lệ.",
                    mention_author=False,
                    allowed_mentions=ALLOWED_MENTIONS_NONE,
                )
            else:
                await handle_natural_delete_request(message, delete_parse.request)
        except discord.Forbidden:
            logger.warning("Missing permission to prepare message cleanup in channel %s", message.channel.id)
        except discord.HTTPException:
            logger.exception("Discord API failed while preparing message cleanup")
        return
    signals: list[IncidentSignal] = []
    if MONITOR_ALL_CHANNELS and not monitor_excluded and not private_thread and not is_nsfw:
        signals = bot.incident_monitor.observe(
            message.channel.id,
            message.author.id,
            message.content,
            guild_id=message.guild.id,
        )
        for signal in signals:
            bot.create_background_task(handle_incident_signal(message.channel, message.guild, signal))

    if signals and not explicit_request:
        return
    if not explicit_request and not auto_reply:
        await bot.process_commands(message)
        return
    question = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
    if auto_reply and not explicit_request and not question:
        return
    attachments = message.attachments if explicit_request else []
    try:
        async with message.channel.typing():
            answer = await bot.create_ai_answer(
                message.guild.id,
                message.channel.id,
                message.author.id,
                question,
                attachments,
            )
        await send_message_answer(message, answer)
    except discord.Forbidden:
        logger.warning("Missing permission to answer in channel %s", message.channel.id)
    except discord.HTTPException:
        logger.exception("Discord API failed while answering in channel %s", message.channel.id)
    await bot.process_commands(message)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.CommandOnCooldown):
        text = f"Bạn thao tác quá nhanh. Hãy thử lại sau {error.retry_after:.0f} giây."
    elif isinstance(error, app_commands.MissingPermissions):
        text = "Bạn không có quyền dùng lệnh này."
    else:
        logger.error("Application command failed: %r", error)
        text = "Lệnh gặp lỗi tạm thời."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


@bot.event
async def on_ready() -> None:
    logger.info(
        "Craftopia bot logged in as %s (%s) build=%s",
        bot.user,
        bot.user.id if bot.user else "unknown",
        BOT_BUILD,
    )
    logger.info(
        "AI model=%s knowledge_chunks=%s auto_reply_channels=%s stats_channel=%s "
        "presence_mode=%s music=%s music_auto_leave=%s voice_grace=%ss",
        bot.ai.model,
        len(bot.knowledge.chunks),
        len(AI_AUTO_REPLY_CHANNEL_IDS),
        bot.stats_dashboard_target.get("channel_id", 0),
        "intents" if TRACK_DISCORD_PRESENCE else "aggregate",
        "enabled" if bot.music.config.enabled else "disabled",
        bot.music.config.auto_leave,
        bot.music.config.voice_disconnect_grace_seconds,
    )
    if not bot.ai.api_key:
        logger.warning("GEMINI_API_KEY is missing; /mcstatus and local monitor still work")
    if not AI_AUTO_REPLY_CHANNEL_IDS:
        logger.warning("No AI auto-reply channel configured; use /ask, ~ai, or mention the bot")
    for channel_id in AI_AUTO_REPLY_CHANNEL_IDS:
        channel = bot.get_channel(channel_id)
        if channel is None:
            logger.warning("AI auto-reply channel %s is not visible to the bot", channel_id)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Thiếu DISCORD_TOKEN trong file .env")
    bot.run(token, log_handler=None)
