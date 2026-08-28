from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import discord


VIETNAM_TZ = timezone(timedelta(hours=7), "Asia/Saigon")


@dataclass(frozen=True)
class DeleteMessagesRequest:
    target_user_id: int
    scope: Literal["channel", "guild"]


@dataclass(frozen=True)
class DeleteParseResult:
    destructive: bool
    request: DeleteMessagesRequest | None = None
    error: str | None = None


@dataclass(frozen=True)
class DeleteScanSnapshot:
    message_ids_by_channel: tuple[tuple[int, tuple[int, ...]], ...]
    scanned_messages: int
    scanned_channels: int
    skipped_channels: int
    skipped_pinned: int
    skipped_system: int
    skipped_invocation: int
    truncated: bool
    overflow: bool
    started_at_utc: datetime
    ended_at_utc: datetime

    @property
    def matched_messages(self) -> int:
        return sum(len(message_ids) for _, message_ids in self.message_ids_by_channel)


@dataclass(frozen=True)
class DeleteExecutionResult:
    deleted: int
    already_missing: int
    failed: int
    failed_channels: int


def _normalize_vietnamese(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold()).replace("đ", "d")
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", normalized).strip()


_DELETE_CANDIDATE_RE = re.compile(r"\b(?:xoa|delete|don)\b.*\b(?:tin nhan|messages?)\b")
_DELETE_NEGATION_RE = re.compile(r"\b(?:khong|dung|chua)\s+(?:hay\s+)?(?:xoa|delete|don)\b")
_DELETE_FULL_RE = re.compile(
    r"^(?:(?:ban|bot)\s+)?(?:(?:hay|vui long)\s+)?"
    r"(?:xoa|delete|don)\s+(?:(?:toan bo|tat ca)\s+)?(?:tin nhan|messages?)\s+"
    r"(?:cua|do)\s+(?P<target><@!?[0-9]+>)\s+"
    r"(?:trong\s+)?(?:ngay\s+)?hom nay"
    r"(?:\s+(?:(?:o|trong|tai)\s+)?(?P<scope>"
    r"kenh nay|tai day|toan server|toan bo server|tat ca (?:cac )?kenh"
    r"))?[.!?]*$"
)


def parse_delete_messages_request(
    text: str,
    resolved_user_ids: set[int],
    bot_user_id: int,
) -> DeleteParseResult:
    normalized = _normalize_vietnamese(text)
    normalized = re.sub(rf"<@!?{bot_user_id}>", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" ,:;-")
    if _DELETE_NEGATION_RE.search(normalized) or not _DELETE_CANDIDATE_RE.search(normalized):
        return DeleteParseResult(destructive=False)
    match = _DELETE_FULL_RE.fullmatch(normalized)
    if match is None:
        return DeleteParseResult(
            destructive=True,
            error=(
                "Yêu cầu xoá chưa đúng cú pháp an toàn. Dùng: "
                "`xoá tin nhắn của @user trong hôm nay` hoặc thêm `trong toàn server`."
            ),
        )
    target_id = int(re.search(r"[0-9]+", match.group("target")).group())
    non_bot_mentions = {user_id for user_id in resolved_user_ids if user_id != bot_user_id}
    if non_bot_mentions != {target_id}:
        return DeleteParseResult(
            destructive=True,
            error="Bạn phải mention đúng một thành viên cần xoá tin nhắn.",
        )
    raw_scope = match.group("scope") or ""
    scope: Literal["channel", "guild"] = (
        "guild"
        if raw_scope in {"toan server", "toan bo server", "tat ca kenh", "tat ca cac kenh"}
        else "channel"
    )
    return DeleteParseResult(
        destructive=True,
        request=DeleteMessagesRequest(target_user_id=target_id, scope=scope),
    )


def vietnam_today_bounds(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now phải có timezone")
    local = current.astimezone(VIETNAM_TZ)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC), current.astimezone(UTC), local.strftime("%d/%m/%Y")


def _channel_permissions_ok(channel: object, member: discord.Member | discord.ClientUser) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return False
    permissions = permissions_for(member)
    return bool(
        permissions.view_channel
        and permissions.read_message_history
        and permissions.manage_messages
    )


def cleanup_channels(
    guild: discord.Guild,
    current_channel: object,
    scope: Literal["channel", "guild"],
) -> list[object]:
    if scope == "channel":
        return [current_channel]
    channels: list[object] = list(guild.text_channels)
    channels.extend(
        thread
        for thread in guild.threads
        if thread.type is not discord.ChannelType.private_thread
    )
    unique: dict[int, object] = {}
    for channel in channels:
        channel_id = getattr(channel, "id", 0)
        if channel_id:
            unique[channel_id] = channel
    return list(unique.values())


async def scan_today_messages(
    guild: discord.Guild,
    current_channel: object,
    actor: discord.Member,
    target_user_id: int,
    scope: Literal["channel", "guild"],
    invocation_message_id: int | None,
    scan_limit_per_channel: int,
    max_matches: int,
    scan_limit_total: int = 20_000,
    now: datetime | None = None,
) -> DeleteScanSnapshot:
    started_at, ended_at, _ = vietnam_today_bounds(now)
    bot_member = guild.me
    candidates: list[tuple[int, tuple[int, ...]]] = []
    scanned_messages = 0
    scanned_channels = 0
    skipped_channels = 0
    skipped_pinned = 0
    skipped_system = 0
    skipped_invocation = 0
    truncated = False
    overflow = False
    total_limit_reached = False

    for channel in cleanup_channels(guild, current_channel, scope):
        if bot_member is None or not _channel_permissions_ok(channel, actor) or not _channel_permissions_ok(
            channel, bot_member
        ):
            skipped_channels += 1
            continue
        history = getattr(channel, "history", None)
        delete_messages = getattr(channel, "delete_messages", None)
        if not callable(history) or not callable(delete_messages):
            skipped_channels += 1
            continue
        channel_ids: list[int] = []
        channel_scanned = 0
        try:
            async for message in history(
                limit=scan_limit_per_channel + 1,
                # discord.py converts datetime `after` to the final snowflake in that
                # millisecond. Step back so messages exactly at 00:00:00.000 are included;
                # the explicit timestamp filter below still enforces the true boundary.
                after=started_at - timedelta(milliseconds=1),
                before=ended_at,
                oldest_first=True,
            ):
                channel_scanned += 1
                if channel_scanned > scan_limit_per_channel:
                    truncated = True
                    break
                if scanned_messages >= scan_limit_total:
                    truncated = True
                    total_limit_reached = True
                    break
                scanned_messages += 1
                if not (started_at <= message.created_at < ended_at):
                    continue
                if message.author.id != target_user_id:
                    continue
                if invocation_message_id is not None and message.id == invocation_message_id:
                    skipped_invocation += 1
                    continue
                if message.pinned:
                    skipped_pinned += 1
                    continue
                if message.is_system():
                    skipped_system += 1
                    continue
                if sum(len(ids) for _, ids in candidates) + len(channel_ids) >= max_matches:
                    overflow = True
                    break
                channel_ids.append(message.id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            skipped_channels += 1
            continue
        scanned_channels += 1
        if channel_ids:
            candidates.append((channel.id, tuple(channel_ids)))
        if overflow or total_limit_reached:
            break

    return DeleteScanSnapshot(
        message_ids_by_channel=tuple(candidates),
        scanned_messages=scanned_messages,
        scanned_channels=scanned_channels,
        skipped_channels=skipped_channels,
        skipped_pinned=skipped_pinned,
        skipped_system=skipped_system,
        skipped_invocation=skipped_invocation,
        truncated=truncated,
        overflow=overflow,
        started_at_utc=started_at,
        ended_at_utc=ended_at,
    )


async def delete_snapshot_messages(
    client: discord.Client,
    snapshot: DeleteScanSnapshot,
    reason: str,
) -> DeleteExecutionResult:
    deleted = 0
    already_missing = 0
    failed = 0
    failed_channels = 0
    for channel_id, message_ids in snapshot.message_ids_by_channel:
        channel = client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await client.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                failed += len(message_ids)
                failed_channels += 1
                continue
        delete_messages = getattr(channel, "delete_messages", None)
        if not callable(delete_messages):
            failed += len(message_ids)
            failed_channels += 1
            continue
        channel_failed = False
        for position in range(0, len(message_ids), 100):
            batch_ids = message_ids[position:position + 100]
            snowflakes = [discord.Object(id=message_id) for message_id in batch_ids]
            try:
                await delete_messages(snowflakes, reason=reason)
                deleted += len(batch_ids)
            except discord.NotFound:
                already_missing += len(batch_ids)
            except (discord.Forbidden, discord.HTTPException, discord.ClientException):
                failed += len(batch_ids)
                channel_failed = True
        if channel_failed:
            failed_channels += 1
    return DeleteExecutionResult(
        deleted=deleted,
        already_missing=already_missing,
        failed=failed,
        failed_channels=failed_channels,
    )
