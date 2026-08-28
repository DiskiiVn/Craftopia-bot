from __future__ import annotations

import asyncio
import ctypes.util
import importlib.metadata
import ipaddress
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Literal, Sequence
from urllib.parse import urlparse

import aiohttp
import discord
import imageio_ffmpeg
from discord import app_commands
from discord.ext import commands
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


logger = logging.getLogger("craftopia-music")
ALLOWED_MENTIONS_NONE = discord.AllowedMentions.none()
LoopMode = Literal["off", "track", "queue"]
CRAFTOPIA_AUTHOR = "DiskiiVN"
MUSIC_RECOVERY_BUILD = "2026.08.28-music-recovery-r1"
PREPARED_STREAM_TTL_SECONDS = 90.0
VOICE_DISCONNECT_TIMEOUT_SECONDS = 3.0
FFMPEG_FRAME_SECONDS = 0.02
FFMPEG_CLEANUP_TIMEOUT_SECONDS = 2.0
PLAYBACK_SEEK_OVERLAP_SECONDS = 2.0
PREMATURE_END_MIN_TOLERANCE_SECONDS = 12.0
PREMATURE_END_MAX_TOLERANCE_SECONDS = 30.0
YTDLP_AUDIO_FORMAT = (
    "bestaudio[protocol=https]/bestaudio[protocol=http]/"
    "bestaudio[protocol!*=m3u8]/bestaudio/best"
)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class MusicConfig:
    enabled: bool = True
    auto_leave: bool = False
    dj_role_id: int = 0
    queue_limit: int = 100
    per_user_limit: int = 20
    playlist_limit: int = 20
    max_duration_seconds: int = 10_800
    allow_live: bool = False
    idle_seconds: int = 180
    voice_disconnect_grace_seconds: int = 30
    voice_reconnect_attempts: int = 4
    voice_reconnect_backoff_seconds: int = 2
    resolve_timeout_seconds: int = 35
    resolve_workers: int = 2
    playback_retries: int = 4
    playback_retry_backoff_seconds: int = 2
    stream_rw_timeout_seconds: int = 45
    bitrate_kbps: int = 128
    default_volume: int = 80
    ffmpeg_path: str = ""
    allowed_hosts: tuple[str, ...] = ("youtube.com", "youtu.be", "soundcloud.com")

    @classmethod
    def from_env(cls) -> MusicConfig:
        allowed_hosts = tuple(
            dict.fromkeys(
                host.strip().lower().lstrip(".")
                for host in os.getenv(
                    "MUSIC_ALLOWED_HOSTS", "youtube.com,youtu.be,soundcloud.com"
                ).split(",")
                if host.strip()
            )
        )
        return cls(
            enabled=_env_bool("MUSIC_ENABLED", True),
            auto_leave=_env_bool("MUSIC_AUTO_LEAVE", False),
            dj_role_id=_env_int("MUSIC_DJ_ROLE_ID", 0, 0, 2**63 - 1),
            queue_limit=_env_int("MUSIC_MAX_QUEUE", 100, 5, 500),
            per_user_limit=_env_int("MUSIC_MAX_PER_USER", 20, 1, 100),
            playlist_limit=_env_int("MUSIC_MAX_PLAYLIST", 20, 1, 100),
            max_duration_seconds=_env_int("MUSIC_MAX_DURATION_SECONDS", 10_800, 60, 86_400),
            allow_live=_env_bool("MUSIC_ALLOW_LIVE", False),
            idle_seconds=_env_int("MUSIC_IDLE_SECONDS", 180, 30, 1800),
            voice_disconnect_grace_seconds=_env_int(
                "MUSIC_VOICE_DISCONNECT_GRACE_SECONDS", 30, 25, 180
            ),
            voice_reconnect_attempts=_env_int("MUSIC_VOICE_RECONNECT_ATTEMPTS", 4, 1, 10),
            voice_reconnect_backoff_seconds=_env_int(
                "MUSIC_VOICE_RECONNECT_BACKOFF_SECONDS", 2, 1, 15
            ),
            resolve_timeout_seconds=_env_int("MUSIC_RESOLVE_TIMEOUT_SECONDS", 35, 10, 120),
            resolve_workers=_env_int("MUSIC_RESOLVE_WORKERS", 2, 1, 4),
            playback_retries=_env_int("MUSIC_PLAYBACK_RETRIES", 4, 1, 10),
            playback_retry_backoff_seconds=_env_int(
                "MUSIC_PLAYBACK_RETRY_BACKOFF_SECONDS", 2, 1, 15
            ),
            stream_rw_timeout_seconds=_env_int(
                "MUSIC_STREAM_RW_TIMEOUT_SECONDS", 45, 15, 120
            ),
            bitrate_kbps=_env_int("MUSIC_BITRATE_KBPS", 128, 64, 320),
            default_volume=_env_int("MUSIC_DEFAULT_VOLUME", 80, 10, 100),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "").strip(),
            allowed_hosts=allowed_hosts,
        )


class MusicError(RuntimeError):
    """An expected music error that is safe to show to Discord users."""


class PlaybackInterrupted(MusicError):
    """The source stopped before the expected end and can be resumed."""


class VoiceUnavailable(MusicError):
    """Discord voice is temporarily unavailable and can be reconnected."""


@dataclass(frozen=True, slots=True)
class MusicTrack:
    identifier: str
    title: str
    webpage_url: str
    source_url: str
    requester_id: int
    duration: int | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    is_live: bool = False


@dataclass(frozen=True, slots=True)
class ResolvedBatch:
    tracks: tuple[MusicTrack, ...]
    skipped: int = 0
    playlist_title: str | None = None
    prepared_stream: StreamResource | None = None


@dataclass(frozen=True, slots=True)
class StreamResource:
    track: MusicTrack
    url: str
    prepared_at: float | None = None


@dataclass(frozen=True, slots=True)
class QueueAddResult:
    tracks: tuple[MusicTrack, ...]
    first_position: int
    skipped: int


@dataclass(frozen=True, slots=True)
class MusicSnapshot:
    guild_id: int
    current: MusicTrack | None
    queued: tuple[MusicTrack, ...]
    paused: bool
    playing: bool
    loading: bool
    loop_mode: LoopMode
    volume: int
    voice_channel_id: int | None
    last_error: str | None


def clean_metadata_text(value: object, limit: int = 180) -> str:
    text = str(value or "Không rõ")
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text or "Không rõ")[:limit]


def format_duration(seconds: int | None, *, live: bool = False) -> str:
    if live:
        return "LIVE"
    if seconds is None or seconds < 0:
        return "?:??"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _safe_http_url(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        return None
    return text


def _safe_stream_url(value: object) -> str | None:
    text = _safe_http_url(value)
    if not text:
        return None
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        return None
    if port not in {None, 80, 443}:
        return None
    host = parsed.hostname
    if not host:
        return None
    try:
        addresses = socket.getaddrinfo(
            host,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return None
    if not addresses:
        return None
    try:
        if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            return None
    except (ValueError, IndexError):
        return None
    return text


def validate_music_query(query: str, allowed_hosts: Sequence[str]) -> str:
    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        raise MusicError("Hãy nhập tên bài hát hoặc link YouTube/SoundCloud.")
    if len(query) > 300:
        raise MusicError("Tên/link bài hát tối đa 300 ký tự.")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", query):
        return query
    try:
        parsed = urlparse(query)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise MusicError("Link nhạc không hợp lệ.") from exc
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise MusicError("Chỉ chấp nhận link HTTP/HTTPS công khai.")
    if port not in {None, 80, 443}:
        raise MusicError("Link nhạc dùng cổng không được phép.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        raise MusicError("Không chấp nhận địa chỉ IP trực tiếp làm nguồn nhạc.")
    if not any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts):
        supported = ", ".join(allowed_hosts) or "không có domain nào"
        raise MusicError(f"Nguồn link chưa được cho phép. Hiện hỗ trợ: {supported}.")
    return query


class _YTDLPQuietLogger:
    """Avoid logging user queries, cookies, or signed media URLs from yt-dlp."""

    def debug(self, message: str) -> None:
        return

    def warning(self, message: str) -> None:
        return

    def error(self, message: str) -> None:
        return


class YTDLPResolver:
    def __init__(self, config: MusicConfig) -> None:
        self.config = config
        self._executor = ThreadPoolExecutor(
            max_workers=config.resolve_workers,
            thread_name_prefix="craftopia-ytdlp",
        )
        self._slots = asyncio.Semaphore(config.resolve_workers)
        self._closed = False

    async def _run(self, function, *args):
        if self._closed:
            raise MusicError("Bộ tìm nhạc đang tắt.")
        try:
            await asyncio.wait_for(
                self._slots.acquire(),
                timeout=self.config.resolve_timeout_seconds,
            )
        except TimeoutError as exc:
            raise MusicError("Hàng chờ tìm nhạc đang bận; hãy thử lại sau.") from exc
        if self._closed:
            self._slots.release()
            raise MusicError("Bộ tìm nhạc đang tắt.")
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self._executor, function, *args)
        except RuntimeError as exc:
            self._slots.release()
            raise MusicError("Bộ tìm nhạc đang tắt.") from exc
        future.add_done_callback(lambda _: self._slots.release())
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                timeout=self.config.resolve_timeout_seconds,
            )
        except TimeoutError as exc:
            raise MusicError("Nguồn nhạc phản hồi quá chậm; hãy thử lại sau.") from exc

    async def resolve(self, query: str, requester_id: int) -> ResolvedBatch:
        normalized = validate_music_query(query, self.config.allowed_hosts)
        try:
            return await self._run(self._resolve_sync, normalized, requester_id)
        except DownloadError as exc:
            logger.info("yt-dlp metadata lookup failed: %s", type(exc).__name__)
            raise MusicError(
                "Không tìm thấy nguồn phát công khai. Bài có thể riêng tư, giới hạn tuổi hoặc bị chặn."
            ) from exc

    def _resolve_sync(self, query: str, requester_id: int) -> ResolvedBatch:
        is_url = bool(re.match(r"^https?://", query, re.IGNORECASE))
        source = query if is_url else f"ytsearch1:{query}"
        options = {
            "logger": _YTDLPQuietLogger(),
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "cachedir": False,
            # Direct URLs may point to any playlist-like extractor (channel pages,
            # sets, albums, future providers). yt-dlp decides whether it is a
            # playlist; entries stay flat while a single track remains full.
            "extract_flat": "in_playlist" if is_url else False,
            "playlistend": self.config.playlist_limit if is_url else 1,
            "format": YTDLP_AUDIO_FORMAT,
            "socket_timeout": min(20, self.config.resolve_timeout_seconds),
            "retries": 1,
            "extractor_retries": 1,
            "fragment_retries": 1,
        }
        if not is_url:
            options["noplaylist"] = True
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(source, download=False)
        if not info:
            raise MusicError("Không tìm thấy bài hát phù hợp.")
        entries = info.get("entries") if hasattr(info, "get") else None
        playlist_mode = is_url and entries is not None
        raw_entries = list(entries or ()) if entries is not None else [info]
        tracks: list[MusicTrack] = []
        skipped = 0
        prepared_stream: StreamResource | None = None
        for entry in raw_entries[: self.config.playlist_limit]:
            track = self._track_from_info(entry, requester_id)
            if track is None:
                skipped += 1
            else:
                tracks.append(track)
                if not playlist_mode and prepared_stream is None:
                    stream_url = self._stream_url_from_info(entry)
                    if stream_url:
                        prepared_stream = StreamResource(track, stream_url, time.monotonic())
        if not tracks:
            raise MusicError(
                "Không có bài nào phù hợp giới hạn thời lượng/live hoặc nguồn đã bị hạn chế."
            )
        playlist_title = None
        if entries is not None and playlist_mode:
            playlist_title = clean_metadata_text(info.get("title"), 120)
        return ResolvedBatch(tuple(tracks), skipped, playlist_title, prepared_stream)

    @staticmethod
    def _stream_url_from_info(info: object) -> str | None:
        if not info or not hasattr(info, "get"):
            return None
        # Flat playlist/search entries point at a webpage, not a playable media URL.
        # Only cache a URL from a fully extracted media record.
        if info.get("_type") in {"url", "url_transparent", "playlist", "multi_video"}:
            return None
        candidates: list[object] = [info.get("url")]
        for key in ("requested_downloads", "requested_formats"):
            values = info.get(key)
            if isinstance(values, (list, tuple)):
                candidates.extend(
                    value.get("url")
                    for value in values
                    if value and hasattr(value, "get")
                )
        for candidate in candidates:
            stream_url = _safe_stream_url(candidate)
            if stream_url:
                return stream_url
        return None

    def _track_from_info(self, info: object, requester_id: int) -> MusicTrack | None:
        if not info or not hasattr(info, "get"):
            return None
        is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        raw_duration = info.get("duration")
        try:
            duration = int(float(raw_duration)) if raw_duration is not None else None
        except (TypeError, ValueError, OverflowError):
            duration = None
        if is_live and not self.config.allow_live:
            return None
        if duration is not None and duration > self.config.max_duration_seconds:
            return None
        webpage_url = _safe_http_url(info.get("webpage_url") or info.get("original_url"))
        raw_url = _safe_http_url(info.get("url"))
        source_url = webpage_url or raw_url
        extractor = clean_metadata_text(info.get("extractor_key") or info.get("extractor"), 40)
        identifier = clean_metadata_text(info.get("id"), 120)
        if not source_url and "youtube" in extractor.lower() and identifier != "Không rõ":
            source_url = f"https://www.youtube.com/watch?v={identifier}"
        if not source_url:
            return None
        return MusicTrack(
            identifier=f"{extractor}:{identifier}",
            title=clean_metadata_text(info.get("title"), 180),
            webpage_url=webpage_url or source_url,
            source_url=source_url,
            requester_id=requester_id,
            duration=duration,
            thumbnail=_safe_http_url(info.get("thumbnail")),
            uploader=clean_metadata_text(info.get("uploader") or info.get("channel"), 100),
            is_live=is_live,
        )

    async def stream_for(self, track: MusicTrack) -> StreamResource:
        try:
            return await self._run(self._stream_sync, track)
        except DownloadError as exc:
            logger.info("yt-dlp stream refresh failed for track %s", track.identifier)
            raise MusicError("Nguồn phát của bài hiện không còn khả dụng.") from exc

    def _stream_sync(self, track: MusicTrack) -> StreamResource:
        options = {
            "logger": _YTDLPQuietLogger(),
            "format": YTDLP_AUDIO_FORMAT,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "cachedir": False,
            "noplaylist": True,
            "socket_timeout": min(20, self.config.resolve_timeout_seconds),
            "retries": 1,
            "extractor_retries": 1,
            "fragment_retries": 1,
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(track.source_url, download=False)
        entries = info.get("entries") if info and hasattr(info, "get") else None
        if entries is not None:
            info = next((entry for entry in entries if entry), None)
        if not info or not hasattr(info, "get"):
            raise MusicError("Nguồn phát không trả về dữ liệu âm thanh.")
        title = clean_metadata_text(info.get("title") or track.title, 180)
        raw_duration = info.get("duration")
        try:
            duration = int(float(raw_duration)) if raw_duration is not None else track.duration
        except (TypeError, ValueError, OverflowError):
            duration = track.duration
        is_live = bool(info.get("is_live") or info.get("live_status") == "is_live")
        if is_live and not self.config.allow_live:
            raise MusicError("Livestream không được bật trên bot này.")
        if duration is not None and duration > self.config.max_duration_seconds:
            raise MusicError("Bài vượt quá giới hạn thời lượng của server.")
        stream_url = self._stream_url_from_info(info)
        if not stream_url:
            raise MusicError("Nguồn phát trả về địa chỉ mạng không an toàn.")
        refreshed = replace(
            track,
            title=title,
            duration=duration,
            thumbnail=_safe_http_url(info.get("thumbnail")) or track.thumbnail,
            uploader=clean_metadata_text(
                info.get("uploader") or info.get("channel") or track.uploader,
                100,
            ),
            is_live=is_live,
        )
        return StreamResource(refreshed, stream_url)

    async def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


class ErrorAwarePCMVolumeTransformer(discord.PCMVolumeTransformer):
    """Expose FFmpeg failures hidden by discord.py's volume transformer."""

    @property
    def _current_error(self) -> Exception | None:
        return getattr(self.original, "_current_error", None)


class ProgressAudioSource(discord.AudioSource):
    """Count actual 20 ms Discord audio frames without counting pauses or stalls."""

    def __init__(self, original: discord.AudioSource) -> None:
        self.original = original
        self.played_seconds = 0.0
        self._cleanup_lock = threading.Lock()
        self._cleaned = False

    def read(self) -> bytes:
        data = self.original.read()
        if data:
            self.played_seconds += FFMPEG_FRAME_SECONDS
        return data

    def is_opus(self) -> bool:
        return self.original.is_opus()

    def cleanup(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
        self.original.cleanup()

    @property
    def _current_error(self) -> Exception | None:
        return getattr(self.original, "_current_error", None)


class _BoundedFFmpegCleanupMixin:
    """Reap FFmpeg with hard deadlines instead of an unbounded communicate()."""

    def _kill_process(self) -> None:
        try:
            self._check_process_returncode()
        except Exception:
            pass
        process = getattr(self, "_process", None)
        if process is None or not callable(getattr(process, "poll", None)):
            return
        pid = getattr(process, "pid", "unknown")
        try:
            running = process.poll() is None
        except Exception:
            running = False
        if running:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    logger.error("FFmpeg process %s did not exit before cleanup deadline", pid)
                except Exception:
                    logger.warning("Could not force-kill FFmpeg process %s", pid)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except Exception:
                    logger.warning("Could not terminate FFmpeg process %s", pid)
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        logger.info(
            "FFmpeg process %s cleanup completed with return code %s",
            pid,
            getattr(process, "returncode", "unknown"),
        )


class BoundedFFmpegPCMAudio(_BoundedFFmpegCleanupMixin, discord.FFmpegPCMAudio):
    pass


class BoundedFFmpegOpusAudio(_BoundedFFmpegCleanupMixin, discord.FFmpegOpusAudio):
    pass


class AudioSourceFactory:
    BEFORE_OPTIONS = (
        "-nostdin -rw_timeout 45000000 -reconnect 1 -reconnect_streamed 1 "
        "-reconnect_delay_max 5"
    )
    SAFE_BEFORE_OPTIONS = "-nostdin -rw_timeout 45000000"

    def __init__(self, config: MusicConfig) -> None:
        self.config = config
        candidates: list[str] = []
        if config.ffmpeg_path:
            candidates.append(config.ffmpeg_path)
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            candidates.append(system_ffmpeg)
        try:
            candidates.append(imageio_ffmpeg.get_ffmpeg_exe())
        except (RuntimeError, OSError, ValueError):
            pass

        usable: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            identity = os.path.normcase(os.path.realpath(candidate))
            if identity in seen:
                continue
            seen.add(identity)
            try:
                result = subprocess.run(
                    [candidate, "-version"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                usable.append(candidate)

        self.available = bool(usable)
        self.executables = tuple(usable or (config.ffmpeg_path or system_ffmpeg or "ffmpeg",))
        self.executable = self.executables[0]
        logger.info(
            "FFmpeg primary=%s fallback_candidates=%s",
            self.executable,
            max(0, len(self.executables) - 1),
        )
        if not discord.opus.is_loaded():
            opus_library = ctypes.util.find_library("opus")
            if opus_library:
                try:
                    discord.opus.load_opus(opus_library)
                except OSError:
                    logger.warning("System libopus was found but could not be loaded")

    @property
    def attempt_count(self) -> int:
        return max(1, min(2, len(self.executables)))

    def _before_options(self, attempt: int, start_at: float) -> str:
        timeout = self.config.stream_rw_timeout_seconds * 1_000_000
        options = f"-nostdin -rw_timeout {timeout}"
        if attempt <= 0:
            options += " -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        if start_at > 0:
            options += f" -ss {start_at:.3f}"
        return options

    def create(
        self,
        stream_url: str,
        volume: int,
        *,
        attempt: int = 0,
        start_at: float = 0.0,
    ) -> discord.AudioSource:
        normalized_volume = max(10, min(100, volume)) / 100
        executable = self.executables[max(0, attempt) % len(self.executables)]
        before_options = self._before_options(attempt, max(0.0, float(start_at)))
        if discord.opus.is_loaded():
            pcm = BoundedFFmpegPCMAudio(
                stream_url,
                executable=executable,
                before_options=before_options,
                options="-vn -sn -dn -loglevel warning",
            )
            return ErrorAwarePCMVolumeTransformer(pcm, volume=normalized_volume)
        # Opus output works without a system libopus and is ideal for generic hosting.
        return BoundedFFmpegOpusAudio(
            stream_url,
            executable=executable,
            bitrate=self.config.bitrate_kbps,
            before_options=before_options,
            options=f"-vn -sn -dn -filter:a volume={normalized_volume:.2f}",
        )


def render_music_panel(snapshot: MusicSnapshot) -> discord.Embed:
    if snapshot.current:
        colour = discord.Color.from_rgb(179, 75, 255)
        title = "🎧 CRAFTOPIA PREMIUM MUSIC"
    else:
        colour = discord.Color.from_rgb(88, 101, 242)
        title = "🎶 CRAFTOPIA MUSIC LOUNGE"
    embed = discord.Embed(title=title, color=colour)
    embed.set_author(name=f"Author • {CRAFTOPIA_AUTHOR}")
    track = snapshot.current
    if track:
        safe_title = discord.utils.escape_markdown(discord.utils.escape_mentions(track.title))
        url = track.webpage_url.replace(")", "%29")
        status = "⏸️ Đang tạm dừng" if snapshot.paused else (
            "⏳ Đang tải nguồn phát" if snapshot.loading else "▶️ Đang phát"
        )
        embed.description = (
            f"{status}\n### [{safe_title}]({url})\n"
            f"`{format_duration(track.duration, live=track.is_live)}` · "
            f"{discord.utils.escape_markdown(track.uploader or 'Không rõ')}\n"
            f"Yêu cầu bởi <@{track.requester_id}>"
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
    else:
        embed.description = (
            "Hiện chưa phát bài nào. Bấm **➕ Thêm bài** hoặc dùng "
            "`/play` · `~play` để bắt đầu."
        )

    if snapshot.queued:
        lines = []
        for index, queued in enumerate(snapshot.queued[:6], 1):
            safe = discord.utils.escape_markdown(discord.utils.escape_mentions(queued.title))
            lines.append(f"`{index:02d}` **{safe[:70]}** · `{format_duration(queued.duration, live=queued.is_live)}`")
        remaining = len(snapshot.queued) - len(lines)
        if remaining:
            lines.append(f"… và **{remaining}** bài khác")
        embed.add_field(name=f"📜 Hàng đợi · {len(snapshot.queued)} bài", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="📜 Hàng đợi", value="Trống", inline=False)

    loop_labels = {"off": "Tắt", "track": "Lặp bài", "queue": "Lặp hàng đợi"}
    voice_label = f"<#{snapshot.voice_channel_id}>" if snapshot.voice_channel_id else "Chưa kết nối"
    embed.add_field(name="🔁 Chế độ lặp", value=loop_labels[snapshot.loop_mode], inline=True)
    embed.add_field(name="🔊 Âm lượng", value=f"{snapshot.volume}%", inline=True)
    embed.add_field(name="🎙️ Voice", value=voice_label, inline=True)
    if snapshot.last_error:
        embed.add_field(
            name="⚠️ Lỗi gần nhất",
            value=discord.utils.escape_markdown(snapshot.last_error)[:900],
            inline=False,
        )
    embed.set_footer(
        text=f"Craftopia VIP Controller • by {CRAFTOPIA_AUTHOR} • luôn kiểm tra voice và quyền"
    )
    return embed


def render_queue_embed(snapshot: MusicSnapshot) -> discord.Embed:
    embed = discord.Embed(title="📜 Hàng đợi Craftopia", color=discord.Color.blurple())
    embed.set_author(name=f"Author • {CRAFTOPIA_AUTHOR}")
    if snapshot.current:
        embed.add_field(
            name="Đang phát",
            value=(
                f"**{discord.utils.escape_markdown(snapshot.current.title)[:180]}** "
                f"· `{format_duration(snapshot.current.duration, live=snapshot.current.is_live)}`"
            ),
            inline=False,
        )
    if snapshot.queued:
        lines = []
        for index, track in enumerate(snapshot.queued[:15], 1):
            safe = discord.utils.escape_markdown(discord.utils.escape_mentions(track.title))
            lines.append(f"`{index:02d}` {safe[:75]} · `{format_duration(track.duration, live=track.is_live)}`")
        if len(snapshot.queued) > 15:
            lines.append(f"… và **{len(snapshot.queued) - 15}** bài khác")
        embed.description = "\n".join(lines)
    elif not snapshot.current:
        embed.description = "Hàng đợi đang trống."
    return embed


@dataclass(slots=True)
class GuildPlayer:
    manager: MusicManager
    guild: discord.Guild
    queue: deque[MusicTrack] = field(default_factory=deque)
    current: MusicTrack | None = None
    loop_mode: LoopMode = "off"
    volume: int = 80
    last_error: str | None = None
    text_channel_id: int | None = None
    panel_message: object | None = None
    finish_action: Literal["normal", "skip", "stop"] = "normal"
    play_generation: int = 0
    closed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    panel_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    queue_event: asyncio.Event = field(default_factory=asyncio.Event)
    worker_task: asyncio.Task | None = None
    idle_task: asyncio.Task | None = None
    empty_channel_task: asyncio.Task | None = None
    panel_update_task: asyncio.Task | None = None
    play_abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    panel_dirty: bool = False
    prepared_streams: dict[int, tuple[StreamResource, float]] = field(default_factory=dict)
    desired_voice_channel_id: int | None = None
    resume_offset_seconds: float = 0.0

    @property
    def voice(self) -> discord.VoiceClient | None:
        voice = self.guild.voice_client
        return voice if isinstance(voice, discord.VoiceClient) else voice

    def _ensure_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(
                self._worker(), name=f"craftopia-music-{self.guild.id}"
            )

    def _cancel_idle(self) -> None:
        if (
            self.idle_task
            and self.idle_task is not asyncio.current_task()
            and not self.idle_task.done()
        ):
            self.idle_task.cancel()
        self.idle_task = None

    def cancel_empty_channel_timer(self) -> None:
        if (
            self.empty_channel_task
            and self.empty_channel_task is not asyncio.current_task()
            and not self.empty_channel_task.done()
        ):
            self.empty_channel_task.cancel()
        self.empty_channel_task = None

    def schedule_empty_channel_timer(self) -> None:
        if (
            not self.manager.config.auto_leave
            or self.closed
            or (self.empty_channel_task and not self.empty_channel_task.done())
        ):
            return
        self.empty_channel_task = asyncio.create_task(
            self._disconnect_if_still_empty(),
            name=f"craftopia-music-empty-{self.guild.id}",
        )

    async def _disconnect_if_still_empty(self) -> None:
        try:
            await asyncio.sleep(self.manager.config.idle_seconds)
            voice = self.voice
            channel = getattr(voice, "channel", None)
            humans = [member for member in getattr(channel, "members", ()) if not member.bot]
            if voice and voice.is_connected() and not humans:
                logger.info(
                    "Auto-leaving empty voice channel guild=%s after=%ss",
                    self.guild.id,
                    self.manager.config.idle_seconds,
                )
                await self.manager.leave(self.guild.id, reason="Kênh voice không còn người nghe")
        except asyncio.CancelledError:
            return

    def _schedule_idle_if_needed(self) -> None:
        if (
            not self.manager.config.auto_leave
            or self.closed
            or self.current is not None
            or self.queue
        ):
            return
        if self.idle_task and not self.idle_task.done():
            return
        self.idle_task = asyncio.create_task(
            self._disconnect_after_idle(),
            name=f"craftopia-music-idle-{self.guild.id}",
        )

    async def _disconnect_after_idle(self) -> None:
        try:
            await asyncio.sleep(self.manager.config.idle_seconds)
            async with self.lock:
                should_leave = not self.closed and self.current is None and not self.queue
            if should_leave:
                logger.info(
                    "Auto-leaving idle voice channel guild=%s after=%ss",
                    self.guild.id,
                    self.manager.config.idle_seconds,
                )
                await self.manager.leave(self.guild.id, reason="Hàng đợi đã trống")
        except asyncio.CancelledError:
            return

    async def enqueue(
        self,
        tracks: Sequence[MusicTrack],
        text_channel_id: int,
        prepared_stream: StreamResource | None = None,
    ) -> QueueAddResult:
        if not tracks:
            raise MusicError("Không có bài nào để thêm vào hàng đợi.")
        requester_id = tracks[0].requester_id
        async with self.lock:
            if self.closed:
                raise MusicError("Phiên nhạc vừa kết thúc; hãy chạy lại `/play`.")
            if len(self.queue) + len(tracks) > self.manager.config.queue_limit:
                raise MusicError(
                    f"Hàng đợi tối đa {self.manager.config.queue_limit} bài; playlist chưa được thêm."
                )
            owned = sum(track.requester_id == requester_id for track in self.queue)
            if self.current and self.current.requester_id == requester_id:
                owned += 1
            if owned + len(tracks) > self.manager.config.per_user_limit:
                raise MusicError(
                    f"Mỗi thành viên giữ tối đa {self.manager.config.per_user_limit} bài trong phiên."
                )
            first_position = len(self.queue) + 1
            self.queue.extend(tracks)
            if prepared_stream is not None and any(
                track is prepared_stream.track for track in tracks
            ):
                remaining_ttl = PREPARED_STREAM_TTL_SECONDS
                if prepared_stream.prepared_at is not None:
                    remaining_ttl -= max(0.0, time.monotonic() - prepared_stream.prepared_at)
                if remaining_ttl > 0:
                    deadline = asyncio.get_running_loop().time() + remaining_ttl
                    self.prepared_streams[id(prepared_stream.track)] = (
                        prepared_stream,
                        deadline,
                    )
            self.text_channel_id = text_channel_id
            self.last_error = None
            self._cancel_idle()
            self.queue_event.set()
            self._ensure_worker()
        self.manager.schedule_panel_update(self.guild.id)
        return QueueAddResult(tuple(tracks), first_position, 0)

    async def _take_prepared_stream(self, track: MusicTrack) -> StreamResource | None:
        async with self.lock:
            prepared = self.prepared_streams.pop(id(track), None)
        if prepared is None:
            return None
        resource, deadline = prepared
        if resource.track is not track:
            return None
        if asyncio.get_running_loop().time() > deadline:
            return None
        return resource

    async def _take_next(self) -> tuple[MusicTrack, int] | None:
        async with self.lock:
            if self.closed:
                return None
            if not self.queue:
                self.queue_event.clear()
                self._schedule_idle_if_needed()
                return None
            track = self.queue.popleft()
            self.current = track
            self.finish_action = "normal"
            self.last_error = None
            self.resume_offset_seconds = 0.0
            self.play_generation += 1
            self.play_abort_event = asyncio.Event()
            generation = self.play_generation
            if not self.queue:
                self.queue_event.clear()
            return track, generation

    async def _worker(self) -> None:
        try:
            while not self.closed:
                await self.queue_event.wait()
                item = await self._take_next()
                if item is None:
                    continue
                track, generation = item
                try:
                    await self._play_one(track, generation)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # A resolver, Discord, or third-party library must never take
                    # the whole guild queue down with one bad track.
                    logger.exception(
                        "Unexpected music track failure guild=%s track=%s",
                        self.guild.id,
                        track.identifier,
                    )
                    async with self.lock:
                        if generation == self.play_generation and not self.closed:
                            self.current = None
                            self.resume_offset_seconds = 0.0
                            self.last_error = (
                                "Bài vừa phát gặp lỗi bất ngờ; bot đã giữ hàng đợi và chuyển bài."
                            )
                            if self.queue:
                                self.queue_event.set()
                            else:
                                self.queue_event.clear()
                                self._schedule_idle_if_needed()
                    self.manager.schedule_panel_update(self.guild.id)
                    await self.manager.notify(
                        self.guild.id,
                        "⚠️ Một bài nhạc gặp lỗi bất ngờ; Craftopia đã giữ hàng đợi và chuyển bài.",
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Music worker crashed for guild %s", self.guild.id)
            async with self.lock:
                self.last_error = "Trình phát gặp lỗi bất ngờ và đã dừng an toàn."
                self.current = None
                self.queue.clear()
                self.prepared_streams.clear()
                self.queue_event.clear()
                self._schedule_idle_if_needed()
            self.manager.schedule_panel_update(self.guild.id)

    @staticmethod
    def _capture_ffmpeg_failure(source: discord.AudioSource) -> Exception | None:
        """Capture an FFmpeg exit that raced with discord.py's EOF check."""
        current: object | None = source
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            error = getattr(current, "_current_error", None)
            if isinstance(error, Exception):
                return error
            process = getattr(current, "_process", None)
            wait = getattr(process, "wait", None)
            if callable(wait):
                try:
                    wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    pass
            check = getattr(current, "_check_process_returncode", None)
            if callable(check):
                try:
                    check()
                except Exception:
                    pass
            error = getattr(current, "_current_error", None)
            if isinstance(error, Exception):
                return error
            current = getattr(current, "original", None)
        return None

    async def _cleanup_source_bounded(
        self,
        source: discord.AudioSource,
        reason: str,
    ) -> None:
        # Do not use asyncio.to_thread here: a pathological cleanup would leave a
        # non-daemon executor worker alive and could keep container shutdown hung.
        loop = asyncio.get_running_loop()
        completed = asyncio.Event()
        cleanup_error: list[BaseException] = []

        def cleanup() -> None:
            try:
                source.cleanup()
            except BaseException as exc:
                cleanup_error.append(exc)
            finally:
                try:
                    loop.call_soon_threadsafe(completed.set)
                except RuntimeError:
                    pass

        threading.Thread(
            target=cleanup,
            name=f"craftopia-audio-cleanup-{self.guild.id}",
            daemon=True,
        ).start()
        try:
            await asyncio.wait_for(completed.wait(), timeout=FFMPEG_CLEANUP_TIMEOUT_SECONDS)
        except TimeoutError:
            logger.error(
                "Audio source cleanup exceeded %.1fs guild=%s reason=%s",
                FFMPEG_CLEANUP_TIMEOUT_SECONDS,
                self.guild.id,
                reason,
            )
        if cleanup_error:
            logger.warning(
                "Audio source cleanup failed guild=%s reason=%s error=%s",
                self.guild.id,
                reason,
                type(cleanup_error[0]).__name__,
            )

    async def _play_resource_once(
        self,
        resource: StreamResource,
        track: MusicTrack,
        playback_attempt: int,
        generation: int,
        abort_event: asyncio.Event,
        *,
        start_at: float = 0.0,
    ) -> tuple[Exception | None, float, bool]:
        """Play one source and return (error, audio seconds, handed to Discord)."""
        voice = self.voice
        if voice is None or not voice.is_connected():
            return VoiceUnavailable("Bot đã mất kết nối voice."), 0.0, False

        source: ProgressAudioSource | None = None
        handed_to_discord = False
        try:
            source = ProgressAudioSource(
                self.manager.audio_factory.create(
                    resource.url,
                    self.volume,
                    attempt=playback_attempt,
                    start_at=start_at,
                )
            )
            loop = asyncio.get_running_loop()
            finished: asyncio.Future[Exception | None] = loop.create_future()

            def complete(error: Exception | None) -> None:
                if not finished.done():
                    finished.set_result(error)

            def after(error: Exception | None) -> None:
                # PCMVolumeTransformer in discord.py does not expose the inner
                # FFmpeg failure. Our transformer does, and this fallback keeps
                # the callback robust with compatible/custom audio sources too.
                source_error = None
                if (
                    error is None
                    and self.finish_action == "normal"
                    and not self.play_abort_event.is_set()
                ):
                    source_error = self._capture_ffmpeg_failure(source)
                try:
                    loop.call_soon_threadsafe(complete, error or source_error)
                except RuntimeError:
                    return

            # Make the final lifecycle check and synchronous ownership transfer
            # atomic with stop/skip/close. Whichever gets this lock first wins.
            async with self.lock:
                if (
                    generation != self.play_generation
                    or self.closed
                    or self.finish_action != "normal"
                    or abort_event.is_set()
                ):
                    return None, source.played_seconds, False
                voice.play(source, after=after)
                handed_to_discord = True
            self.manager.schedule_panel_update(self.guild.id)
            expected = max(1.0, (track.duration or self.manager.config.max_duration_seconds) - start_at)
            playback_timeout = min(
                self.manager.config.max_duration_seconds + 300,
                max(300, expected + 300),
            )
            try:
                error = await asyncio.wait_for(finished, timeout=playback_timeout)
            except TimeoutError as exc:
                voice.stop()
                # AudioPlayer.stop() only sets a thread flag. If FFmpeg/read() is
                # wedged, the thread cannot reach its normal finally/cleanup, so
                # force cleanup here to kill FFmpeg and unblock the pipe. The
                # discord.py cleanup that follows is idempotent after _process is
                # cleared by FFmpegAudio.cleanup().
                await self._cleanup_source_bounded(source, "playback timeout")
                error = exc
            if (
                error is None
                and self.finish_action == "normal"
                and not abort_event.is_set()
            ):
                if not voice.is_connected():
                    error = VoiceUnavailable("Discord Voice bị ngắt giữa bài.")
                elif track.duration is not None and not track.is_live:
                    remaining = max(0.0, float(track.duration) - start_at)
                    tolerance = min(
                        PREMATURE_END_MAX_TOLERANCE_SECONDS,
                        max(PREMATURE_END_MIN_TOLERANCE_SECONDS, track.duration * 0.05),
                    )
                    if remaining > tolerance and source.played_seconds + tolerance < remaining:
                        error = PlaybackInterrupted(
                            "Nguồn âm thanh kết thúc sớm trước thời lượng dự kiến."
                        )
            return error, source.played_seconds, handed_to_discord
        except asyncio.CancelledError:
            if voice.is_playing() or voice.is_paused():
                voice.stop()
            raise
        except Exception as exc:
            return exc, source.played_seconds if source is not None else 0.0, handed_to_discord
        finally:
            # Once voice.play succeeds, discord.py's AudioPlayer owns and cleans
            # the source. Cleaning it here too races and logs the same PID twice.
            if source is not None and not handed_to_discord:
                await self._cleanup_source_bounded(source, "unowned source")

    async def _refresh_stream_for_retry(
        self,
        track: MusicTrack,
        abort_event: asyncio.Event,
    ) -> StreamResource | None:
        resolve_task = asyncio.create_task(self.manager.resolver.stream_for(track))
        abort_task = asyncio.create_task(abort_event.wait())
        try:
            done, _ = await asyncio.wait(
                (resolve_task, abort_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort_task in done and abort_event.is_set():
                resolve_task.cancel()
                await asyncio.gather(resolve_task, return_exceptions=True)
                return None
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)
            return await resolve_task
        finally:
            for task in (resolve_task, abort_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(resolve_task, abort_task, return_exceptions=True)

    async def _retry_still_allowed(self, generation: int, abort_event: asyncio.Event) -> bool:
        async with self.lock:
            return bool(
                generation == self.play_generation
                and not self.closed
                and self.finish_action == "normal"
                and not abort_event.is_set()
            )

    async def _wait_retry_backoff(
        self,
        retry_number: int,
        generation: int,
        abort_event: asyncio.Event,
    ) -> bool:
        delay = min(
            8,
            self.manager.config.playback_retry_backoff_seconds
            * (2 ** max(0, retry_number - 1)),
        )
        try:
            await asyncio.wait_for(abort_event.wait(), timeout=delay)
            return False
        except TimeoutError:
            return await self._retry_still_allowed(generation, abort_event)

    async def _play_one(self, track: MusicTrack, generation: int) -> None:
        self.manager.schedule_panel_update(self.guild.id)
        abort_event = self.play_abort_event
        resource = await self._take_prepared_stream(track)
        error_text: str | None = None
        for attempt in range(2) if resource is None else ():
            resolve_task = asyncio.create_task(self.manager.resolver.stream_for(track))
            abort_task = asyncio.create_task(abort_event.wait())
            try:
                done, _ = await asyncio.wait(
                    (resolve_task, abort_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if abort_task in done and abort_event.is_set():
                    resolve_task.cancel()
                    await asyncio.gather(resolve_task, return_exceptions=True)
                    resource = None
                    break
                abort_task.cancel()
                await asyncio.gather(abort_task, return_exceptions=True)
                resource = await resolve_task
                break
            except MusicError as exc:
                error_text = str(exc)
                if attempt == 0:
                    try:
                        await asyncio.wait_for(abort_event.wait(), timeout=0.5)
                        resource = None
                        error_text = None
                        break
                    except TimeoutError:
                        pass
            except Exception as exc:
                error_text = "Không thể tải nguồn nhạc do lỗi mạng hoặc yt-dlp."
                logger.warning(
                    "Initial music stream resolution raised guild=%s track=%s error=%s "
                    "attempt=%s/2",
                    self.guild.id,
                    track.identifier,
                    type(exc).__name__,
                    attempt + 1,
                )
                if attempt == 0:
                    try:
                        await asyncio.wait_for(abort_event.wait(), timeout=0.5)
                        resource = None
                        error_text = None
                        break
                    except TimeoutError:
                        pass
            finally:
                if not abort_task.done():
                    abort_task.cancel()
                if not resolve_task.done():
                    resolve_task.cancel()
                await asyncio.gather(abort_task, resolve_task, return_exceptions=True)
        if resource is not None:
            async with self.lock:
                if generation != self.play_generation or self.closed:
                    return
                if self.finish_action != "normal":
                    resource = None
                else:
                    self.current = resource.track
                    track = resource.track
                    error_text = None

        playback_error: Exception | None = None
        if resource is not None:
            resume_from = max(0.0, self.resume_offset_seconds)
            attempts = 1 + self.manager.config.playback_retries
            for playback_attempt in range(attempts):
                if playback_attempt > 0:
                    if not await self._wait_retry_backoff(
                        playback_attempt,
                        generation,
                        abort_event,
                    ):
                        playback_error = None
                        break
                    voice = self.voice
                    if voice is None or not voice.is_connected():
                        recovered = await self.manager.recover_voice(self, abort_event)
                        if not recovered:
                            playback_error = VoiceUnavailable(
                                "Discord Voice chưa kết nối lại được."
                            )
                            continue
                    try:
                        refreshed = await self._refresh_stream_for_retry(track, abort_event)
                    except MusicError as exc:
                        playback_error = exc
                        logger.warning(
                            "Music stream refresh failed guild=%s track=%s error=%s attempt=%s/%s",
                            self.guild.id,
                            track.identifier,
                            type(exc).__name__,
                            playback_attempt,
                            self.manager.config.playback_retries,
                        )
                        continue
                    except Exception as exc:
                        playback_error = MusicError("Không thể làm mới nguồn nhạc do lỗi mạng.")
                        logger.warning(
                            "Music stream refresh raised guild=%s track=%s error=%s attempt=%s/%s",
                            self.guild.id,
                            track.identifier,
                            type(exc).__name__,
                            playback_attempt,
                            self.manager.config.playback_retries,
                        )
                        continue
                    if refreshed is None or not await self._retry_still_allowed(
                        generation, abort_event
                    ):
                        playback_error = None
                        break
                    resource = refreshed
                    async with self.lock:
                        if generation != self.play_generation or self.closed:
                            return
                        self.current = refreshed.track
                        track = refreshed.track

                start_at = 0.0 if track.is_live else max(
                    0.0,
                    resume_from - (PLAYBACK_SEEK_OVERLAP_SECONDS if playback_attempt else 0.0),
                )
                playback_error, played_seconds, handed_to_discord = (
                    await self._play_resource_once(
                        resource,
                        track,
                        playback_attempt,
                        generation,
                        abort_event,
                        start_at=start_at,
                    )
                )
                resume_from = start_at + max(0.0, played_seconds)
                if track.duration is not None:
                    resume_from = min(resume_from, float(track.duration))
                async with self.lock:
                    if generation == self.play_generation and not self.closed:
                        self.resume_offset_seconds = resume_from
                if playback_error is None:
                    break
                if not await self._retry_still_allowed(generation, abort_event):
                    playback_error = None
                    break
                retryable = bool(
                    not handed_to_discord
                    or isinstance(
                        playback_error,
                        (
                            PlaybackInterrupted,
                            VoiceUnavailable,
                            discord.FFmpegProcessError,
                            OSError,
                            TimeoutError,
                        ),
                    )
                )
                if not retryable or playback_attempt + 1 >= attempts:
                    break
                logger.warning(
                    "Playback interrupted; refreshing and resuming guild=%s track=%s "
                    "error=%s retry=%s/%s resume=%.1fs executable=%s",
                    self.guild.id,
                    track.identifier,
                    type(playback_error).__name__,
                    playback_attempt + 1,
                    self.manager.config.playback_retries,
                    resume_from,
                    self.manager.audio_factory.executables[
                        playback_attempt % len(self.manager.audio_factory.executables)
                    ],
                )
            if playback_error is not None:
                logger.warning(
                    "Playback failed after recovery attempts guild=%s track=%s error=%s",
                    self.guild.id,
                    track.identifier,
                    type(playback_error).__name__,
                )
                error_text = "FFmpeg/Discord không thể phục hồi bài này; bot đã chuyển bài."

        await self._finish_track(generation, error_text)
        if error_text:
            await self.manager.notify(
                self.guild.id,
                f"⚠️ Đã bỏ qua **{discord.utils.escape_markdown(track.title)[:120]}**: {error_text}",
            )

    async def _finish_track(self, generation: int, error_text: str | None) -> None:
        async with self.lock:
            if generation != self.play_generation or self.closed:
                return
            finished = self.current
            action = self.finish_action
            self.current = None
            self.finish_action = "normal"
            self.resume_offset_seconds = 0.0
            if error_text:
                self.last_error = clean_metadata_text(error_text, 300)
            elif finished is not None and action == "normal":
                if self.loop_mode == "track":
                    self.queue.appendleft(finished)
                elif self.loop_mode == "queue":
                    self.queue.append(finished)
            if self.queue:
                self.queue_event.set()
            else:
                self.queue_event.clear()
                self._schedule_idle_if_needed()
        self.manager.schedule_panel_update(self.guild.id)

    async def snapshot(self) -> MusicSnapshot:
        async with self.lock:
            voice = self.voice
            playing = bool(voice and voice.is_playing())
            paused = bool(voice and voice.is_paused())
            voice_channel = getattr(voice, "channel", None)
            return MusicSnapshot(
                guild_id=self.guild.id,
                current=self.current,
                queued=tuple(self.queue),
                paused=paused,
                playing=playing,
                loading=self.current is not None and not playing and not paused,
                loop_mode=self.loop_mode,
                volume=self.volume,
                voice_channel_id=getattr(voice_channel, "id", None),
                last_error=self.last_error,
            )

    async def toggle_pause(self) -> str:
        voice = self.voice
        if not voice or self.current is None:
            raise MusicError("Hiện không có bài nào đang phát.")
        if voice.is_paused():
            voice.resume()
            text = "▶️ Đã tiếp tục phát nhạc."
        elif voice.is_playing():
            voice.pause()
            text = "⏸️ Đã tạm dừng."
        else:
            raise MusicError("Bài đang tải nguồn; hãy thử lại sau vài giây.")
        self.manager.schedule_panel_update(self.guild.id)
        return text

    async def pause(self) -> str:
        voice = self.voice
        if not voice or self.current is None:
            raise MusicError("Hiện không có bài nào đang phát.")
        if voice.is_paused():
            return "⏸️ Nhạc đã tạm dừng sẵn."
        if not voice.is_playing():
            raise MusicError("Bài đang tải nguồn; hãy thử lại sau vài giây.")
        voice.pause()
        self.manager.schedule_panel_update(self.guild.id)
        return "⏸️ Đã tạm dừng."

    async def resume(self) -> str:
        voice = self.voice
        if not voice or self.current is None:
            raise MusicError("Hiện không có bài nào đang phát.")
        if voice.is_playing():
            return "▶️ Nhạc đang phát sẵn."
        if not voice.is_paused():
            raise MusicError("Bài đang tải nguồn; hãy thử lại sau vài giây.")
        voice.resume()
        self.manager.schedule_panel_update(self.guild.id)
        return "▶️ Đã tiếp tục phát nhạc."

    async def skip(self) -> str:
        async with self.lock:
            if self.current is None:
                raise MusicError("Không có bài nào để bỏ qua.")
            skipped_title = self.current.title
            self.finish_action = "skip"
            self.resume_offset_seconds = 0.0
            self.play_abort_event.set()
            voice = self.voice
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
        self.manager.schedule_panel_update(self.guild.id)
        return f"⏭️ Đã bỏ qua **{discord.utils.escape_markdown(skipped_title)[:120]}**."

    async def stop(self) -> str:
        async with self.lock:
            removed = len(self.queue) + (1 if self.current else 0)
            self.queue.clear()
            self.prepared_streams.clear()
            self.queue_event.clear()
            self.loop_mode = "off"
            self.finish_action = "stop"
            self.resume_offset_seconds = 0.0
            self.play_abort_event.set()
            voice = self.voice
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
            elif self.current is None:
                self._schedule_idle_if_needed()
        self.manager.schedule_panel_update(self.guild.id)
        return f"⏹️ Đã dừng và xoá **{removed}** bài khỏi phiên nhạc."

    async def shuffle(self) -> str:
        async with self.lock:
            if len(self.queue) < 2:
                raise MusicError("Cần ít nhất 2 bài chờ để trộn hàng đợi.")
            items = list(self.queue)
            random.SystemRandom().shuffle(items)
            self.queue = deque(items)
        self.manager.schedule_panel_update(self.guild.id)
        return f"🔀 Đã trộn **{len(items)}** bài đang chờ."

    async def set_loop(self, mode: str | None = None) -> str:
        modes: tuple[LoopMode, ...] = ("off", "track", "queue")
        async with self.lock:
            if mode is None:
                self.loop_mode = modes[(modes.index(self.loop_mode) + 1) % len(modes)]
            else:
                normalized = mode.strip().lower()
                aliases = {
                    "off": "off", "tat": "off", "tắt": "off",
                    "track": "track", "bai": "track", "bài": "track",
                    "queue": "queue", "hangdoi": "queue", "hàngđợi": "queue",
                }
                selected = aliases.get(normalized)
                if selected is None:
                    raise MusicError("Chế độ loop hợp lệ: `off`, `track`, `queue`.")
                self.loop_mode = selected
            selected_mode = self.loop_mode
        self.manager.schedule_panel_update(self.guild.id)
        labels = {"off": "tắt", "track": "lặp bài hiện tại", "queue": "lặp toàn hàng đợi"}
        return f"🔁 Loop: **{labels[selected_mode]}**."

    @staticmethod
    def _pcm_transformer(source: object) -> discord.PCMVolumeTransformer | None:
        current = source
        visited: set[int] = set()
        while current is not None and id(current) not in visited:
            visited.add(id(current))
            if isinstance(current, discord.PCMVolumeTransformer):
                return current
            current = getattr(current, "original", None)
        return None

    async def set_volume(self, volume: int) -> str:
        volume = max(10, min(100, int(volume)))
        async with self.lock:
            self.volume = volume
            voice = self.voice
            source = getattr(voice, "source", None) if voice else None
            transformer = self._pcm_transformer(source)
            immediate = transformer is not None
            if transformer is not None:
                transformer.volume = volume / 100
        self.manager.schedule_panel_update(self.guild.id)
        suffix = "" if immediate else " (áp dụng từ bài kế tiếp trên hosting không có libopus)"
        return f"🔊 Âm lượng: **{volume}%**{suffix}."

    async def adjust_volume(self, delta: int) -> str:
        async with self.lock:
            volume = max(10, min(100, self.volume + int(delta)))
            self.volume = volume
            voice = self.voice
            source = getattr(voice, "source", None) if voice else None
            transformer = self._pcm_transformer(source)
            immediate = transformer is not None
            if transformer is not None:
                transformer.volume = volume / 100
        self.manager.schedule_panel_update(self.guild.id)
        suffix = "" if immediate else " (áp dụng từ bài kế tiếp trên hosting không có libopus)"
        return f"🔊 Âm lượng: **{volume}%**{suffix}."

    async def close(self, *, disconnect: bool = True) -> None:
        async with self.lock:
            if self.closed:
                return
            self.closed = True
            self.play_generation += 1
            self.play_abort_event.set()
            self.queue.clear()
            self.prepared_streams.clear()
            self.current = None
            self.resume_offset_seconds = 0.0
            self.queue_event.set()
            self._cancel_idle()
            self.cancel_empty_channel_timer()
            voice = self.voice
            if voice and (voice.is_playing() or voice.is_paused()):
                voice.stop()
        tasks = [self.worker_task, self.panel_update_task]
        current_task = asyncio.current_task()
        for task in tasks:
            if task and task is not current_task and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in tasks if task and task is not current_task),
            return_exceptions=True,
        )
        voice = self.voice
        if not disconnect or voice is None:
            return

        def force_cache_cleanup() -> None:
            cleanup = getattr(voice, "cleanup", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    logger.warning("Voice cache cleanup failed for guild %s", self.guild.id)

        if not voice.is_connected():
            force_cache_cleanup()
            return
        try:
            await asyncio.wait_for(
                voice.disconnect(force=True),
                timeout=VOICE_DISCONNECT_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Voice disconnect timed out after %.1fs for guild %s; forcing cache cleanup",
                VOICE_DISCONNECT_TIMEOUT_SECONDS,
                self.guild.id,
            )
            force_cache_cleanup()
        except (discord.ClientException, discord.HTTPException, OSError):
            logger.warning("Voice disconnect failed for guild %s; forcing cache cleanup", self.guild.id)
            force_cache_cleanup()


class MusicManager:
    def __init__(
        self,
        bot: commands.Bot,
        config: MusicConfig | None = None,
        *,
        resolver: YTDLPResolver | None = None,
        audio_factory: AudioSourceFactory | None = None,
    ) -> None:
        self.bot = bot
        self.config = config or MusicConfig.from_env()
        self.resolver = resolver or (YTDLPResolver(self.config) if self.config.enabled else None)
        self.audio_factory = audio_factory or (
            AudioSourceFactory(self.config) if self.config.enabled else None
        )
        self.players: dict[int, GuildPlayer] = {}
        self.control_view = MusicControlView(self)
        self._closed = False
        # Searches may resolve concurrently, while connect/enqueue and teardown are
        # serialized per guild. Epochs invalidate searches started before a leave.
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._guild_epochs: dict[int, int] = {}
        self._request_lock = asyncio.Lock()
        self._inflight_users: set[tuple[int, int]] = set()
        self._inflight_by_guild: dict[int, int] = {}
        self._last_request_at: dict[tuple[int, int], float] = {}
        self._voice_disconnect_tasks: dict[int, asyncio.Task] = {}
        self._voice_recovery_events: dict[int, asyncio.Event] = {}

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._guild_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._guild_locks[guild_id] = lock
        return lock

    def _guild_epoch(self, guild_id: int) -> int:
        return self._guild_epochs.get(guild_id, 0)

    def _assert_active_epoch(self, guild_id: int, epoch: int) -> None:
        if self._closed or not self.config.enabled:
            raise MusicError("Tính năng nhạc đang tắt.")
        if self._guild_epoch(guild_id) != epoch:
            raise MusicError("Phiên nhạc đã kết thúc trong lúc tìm bài; hãy chạy lệnh lại.")

    def _advance_guild_epoch(self, guild_id: int) -> None:
        self._guild_epochs[guild_id] = self._guild_epoch(guild_id) + 1

    async def _begin_request(self, guild_id: int, member_id: int) -> None:
        key = (guild_id, member_id)
        async with self._request_lock:
            now = asyncio.get_running_loop().time()
            if key in self._inflight_users:
                raise MusicError("Bạn đã có một yêu cầu nhạc đang được xử lý.")
            if now - self._last_request_at.get(key, -10_000.0) < 3.0:
                raise MusicError("Bạn thao tác quá nhanh; hãy chờ 3 giây rồi thử lại.")
            guild_limit = max(4, self.config.resolve_workers * 2)
            if self._inflight_by_guild.get(guild_id, 0) >= guild_limit:
                raise MusicError("Server đang có quá nhiều yêu cầu tìm nhạc; hãy thử lại sau.")
            self._inflight_users.add(key)
            self._inflight_by_guild[guild_id] = self._inflight_by_guild.get(guild_id, 0) + 1
            self._last_request_at[key] = now

    async def _end_request(self, guild_id: int, member_id: int) -> None:
        key = (guild_id, member_id)
        async with self._request_lock:
            self._inflight_users.discard(key)
            remaining = self._inflight_by_guild.get(guild_id, 0) - 1
            if remaining > 0:
                self._inflight_by_guild[guild_id] = remaining
            else:
                self._inflight_by_guild.pop(guild_id, None)

    def state_for(self, guild_id: int) -> GuildPlayer | None:
        return self.players.get(guild_id)

    def cancel_bot_disconnect_check(self, guild_id: int) -> None:
        event = self._voice_recovery_events.pop(guild_id, None)
        if event is not None:
            event.set()
        task = self._voice_disconnect_tasks.pop(guild_id, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def signal_bot_voice_recovered(self, guild_id: int) -> None:
        event = self._voice_recovery_events.get(guild_id)
        if event is not None:
            event.set()

    async def recover_voice(
        self,
        player: GuildPlayer,
        abort_event: asyncio.Event | None = None,
    ) -> bool:
        guild = player.guild
        for attempt in range(1, self.config.voice_reconnect_attempts + 1):
            if (
                self._closed
                or player.closed
                or self.players.get(guild.id) is not player
                or (abort_event is not None and abort_event.is_set())
            ):
                return False
            voice = guild.voice_client
            if voice and voice.is_connected():
                self.signal_bot_voice_recovered(guild.id)
                return True
            channel_id = player.desired_voice_channel_id or getattr(
                getattr(voice, "channel", None), "id", None
            )
            get_channel = getattr(guild, "get_channel", None)
            channel = get_channel(channel_id) if callable(get_channel) and channel_id else None
            if channel is None and getattr(getattr(voice, "channel", None), "id", None) == channel_id:
                channel = voice.channel
            if channel is None or not callable(getattr(channel, "connect", None)):
                logger.warning(
                    "Cannot recover Discord voice because channel %s is unavailable guild=%s",
                    channel_id,
                    guild.id,
                )
                return False
            try:
                await self.ensure_voice(guild, channel)
            except MusicError as exc:
                logger.warning(
                    "Discord voice reconnect failed guild=%s attempt=%s/%s error=%s",
                    guild.id,
                    attempt,
                    self.config.voice_reconnect_attempts,
                    type(exc).__name__,
                )
            except Exception as exc:
                logger.warning(
                    "Discord voice reconnect raised guild=%s attempt=%s/%s error=%s",
                    guild.id,
                    attempt,
                    self.config.voice_reconnect_attempts,
                    type(exc).__name__,
                )
            else:
                voice = guild.voice_client
                if voice and voice.is_connected():
                    player.desired_voice_channel_id = channel_id
                    self.signal_bot_voice_recovered(guild.id)
                    logger.info(
                        "Discord voice reconnected guild=%s channel=%s attempt=%s",
                        guild.id,
                        channel_id,
                        attempt,
                    )
                    return True
            if attempt >= self.config.voice_reconnect_attempts:
                break
            delay = min(
                15,
                self.config.voice_reconnect_backoff_seconds * (2 ** (attempt - 1)),
            )
            if abort_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(abort_event.wait(), timeout=delay)
                    return False
                except TimeoutError:
                    pass
        return False

    def schedule_bot_disconnect_check(
        self,
        guild: discord.Guild,
        channel_id: int | None = None,
    ) -> None:
        """Preserve the session while Discord reconnects, then actively rejoin."""
        player = self.players.get(guild.id)
        if self._closed or player is None or player.closed:
            return
        if channel_id is None:
            channel_id = getattr(getattr(guild.voice_client, "channel", None), "id", None)
        if channel_id is not None:
            player.desired_voice_channel_id = channel_id
        self.cancel_bot_disconnect_check(guild.id)
        recovered_event = asyncio.Event()
        self._voice_recovery_events[guild.id] = recovered_event

        async def delayed_check() -> None:
            warned = False
            try:
                try:
                    await asyncio.wait_for(
                        recovered_event.wait(),
                        timeout=self.config.voice_disconnect_grace_seconds,
                    )
                    return
                except TimeoutError:
                    pass
                while not self._closed and self.players.get(guild.id) is player and not player.closed:
                    voice = guild.voice_client
                    if voice and voice.is_connected():
                        logger.info("Discord voice connection recovered for guild %s", guild.id)
                        return
                    logger.warning(
                        "Discord voice did not self-recover after %ss; reconnecting guild=%s",
                        self.config.voice_disconnect_grace_seconds,
                        guild.id,
                    )
                    if await self.recover_voice(player):
                        return
                    if self.config.auto_leave:
                        await self.handle_bot_disconnect(
                            guild.id,
                            expected_player=player,
                            confirm_disconnected=True,
                        )
                        return
                    if not warned:
                        warned = True
                        player.last_error = (
                            "Voice đang mất kết nối; bot giữ nguyên phiên và sẽ tiếp tục thử lại."
                        )
                        self.schedule_panel_update(guild.id)
                        await self.notify(
                            guild.id,
                            "⚠️ Discord Voice đang gián đoạn; Craftopia giữ nguyên phiên nhạc và tự nối lại.",
                        )
                    try:
                        await asyncio.wait_for(
                            recovered_event.wait(),
                            timeout=max(30, self.config.voice_disconnect_grace_seconds),
                        )
                        return
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                return
            finally:
                if self._voice_disconnect_tasks.get(guild.id) is asyncio.current_task():
                    self._voice_disconnect_tasks.pop(guild.id, None)
                if self._voice_recovery_events.get(guild.id) is recovered_event:
                    self._voice_recovery_events.pop(guild.id, None)

        task = asyncio.create_task(
            delayed_check(),
            name=f"craftopia-music-voice-grace-{guild.id}",
        )
        self._voice_disconnect_tasks[guild.id] = task

    def get_or_create(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None or player.closed:
            player = GuildPlayer(self, guild, volume=self.config.default_volume)
            self.players[guild.id] = player
        return player

    def is_dj(self, member: discord.Member) -> bool:
        permissions = member.guild_permissions
        if permissions.administrator or permissions.manage_guild:
            return True
        return bool(
            self.config.dj_role_id
            and any(role.id == self.config.dj_role_id for role in member.roles)
        )

    def member_voice_channel(self, member: discord.Member) -> discord.VoiceChannel:
        voice_state = member.voice
        channel = voice_state.channel if voice_state else None
        if channel is None:
            raise MusicError("Bạn phải vào một kênh voice trước.")
        if isinstance(channel, discord.StageChannel):
            raise MusicError("Bản nhạc hiện chỉ hỗ trợ Voice Channel, chưa hỗ trợ Stage Channel.")
        if not isinstance(channel, discord.VoiceChannel):
            raise MusicError("Không xác định được Voice Channel của bạn.")
        return channel

    def validate_voice_access(
        self, guild: discord.Guild, member: discord.Member
    ) -> discord.VoiceChannel:
        channel = self.member_voice_channel(member)
        bot_member = guild.me
        if bot_member is None:
            raise MusicError("Bot chưa đọc được quyền voice của chính mình.")
        permissions = channel.permissions_for(bot_member)
        if not (permissions.view_channel and permissions.connect and permissions.speak):
            raise MusicError("Bot cần quyền **View Channel**, **Connect** và **Speak** tại voice này.")
        voice = guild.voice_client
        existing_channel = getattr(voice, "channel", None)
        if voice and voice.is_connected() and getattr(existing_channel, "id", None) != channel.id:
            raise MusicError("Bot đang phục vụ một kênh voice khác trong server.")
        return channel

    def authorize_control(self, member: discord.Member, *, destructive: bool = False) -> GuildPlayer:
        player = self.players.get(member.guild.id)
        if player is None or player.closed:
            raise MusicError("Server chưa có phiên nhạc. Dùng `/play` hoặc `~play` trước.")
        channel = self.member_voice_channel(member)
        voice = member.guild.voice_client
        if not voice or not voice.is_connected() or getattr(voice.channel, "id", None) != channel.id:
            raise MusicError("Bạn phải ở cùng kênh voice với bot để điều khiển.")
        if destructive and not self.is_dj(member):
            role_hint = " role DJ đã cấu hình hoặc" if self.config.dj_role_id else ""
            raise MusicError(f"Lệnh này cần{role_hint} quyền **Manage Server**.")
        return player

    def authorize_add_from_panel(self, member: discord.Member) -> GuildPlayer:
        """Permit Add on a fresh panel, or require the bot's current voice channel."""
        player = self.players.get(member.guild.id)
        if player is None or player.closed:
            raise MusicError("Bảng nhạc này không còn hoạt động. Dùng `/music` hoặc `~music` lại.")
        self.validate_voice_access(member.guild, member)
        return player

    @staticmethod
    async def _cleanup_failed_voice_connect(guild: discord.Guild, previous_voice: object) -> None:
        """Remove a voice client registered by a cancelled/failed Discord handshake."""
        dangling = guild.voice_client
        if dangling is None or dangling is previous_voice:
            return
        try:
            await asyncio.wait_for(
                dangling.disconnect(force=True),
                timeout=VOICE_DISCONNECT_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning("Could not clean up failed voice handshake for guild %s", guild.id)
        finally:
            # VoiceClient.disconnect can itself fail before removing Discord.py's
            # internal voice-client cache. Its synchronous cleanup is the required
            # last resort or later connects can incorrectly report "already connected".
            if guild.voice_client is dangling:
                cleanup = getattr(dangling, "cleanup", None)
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        logger.warning(
                            "Could not clear failed voice cache for guild %s", guild.id
                        )

    async def ensure_voice(
        self,
        guild: discord.Guild,
        channel: discord.VoiceChannel,
    ) -> discord.VoiceClient:
        player = self.get_or_create(guild)
        async with player.connect_lock:
            voice = guild.voice_client
            if voice and voice.is_connected():
                if getattr(voice.channel, "id", None) != channel.id:
                    raise MusicError("Bot đang phục vụ một kênh voice khác trong server.")
                player.desired_voice_channel_id = channel.id
                return voice
            if voice:
                try:
                    await asyncio.wait_for(
                        voice.disconnect(force=True),
                        timeout=VOICE_DISCONNECT_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.warning("Could not disconnect stale voice client for guild %s", guild.id)
                finally:
                    if guild.voice_client is voice:
                        cleanup = getattr(voice, "cleanup", None)
                        if callable(cleanup):
                            try:
                                cleanup()
                            except Exception:
                                logger.warning(
                                    "Could not clear stale voice cache for guild %s", guild.id
                                )
            previous_voice = voice
            try:
                connected = await channel.connect(
                    timeout=20,
                    reconnect=True,
                    self_deaf=True,
                )
            except asyncio.CancelledError:
                await self._cleanup_failed_voice_connect(guild, previous_voice)
                raise
            except asyncio.TimeoutError as exc:
                await self._cleanup_failed_voice_connect(guild, previous_voice)
                raise MusicError("Kết nối Discord Voice bị timeout.") from exc
            except (discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
                await self._cleanup_failed_voice_connect(guild, previous_voice)
                raise MusicError("Không thể kết nối voice; hãy kiểm tra quyền và UDP của hosting.") from exc
            except BaseException:
                await self._cleanup_failed_voice_connect(guild, previous_voice)
                raise
            player._cancel_idle()
            player.desired_voice_channel_id = channel.id
            return connected

    async def _connect_for_request(
        self,
        guild: discord.Guild,
        member: discord.Member,
        expected_channel_id: int,
        epoch: int,
    ) -> None:
        async with self._guild_lock(guild.id):
            self._assert_active_epoch(guild.id, epoch)
            current_channel = self.validate_voice_access(guild, member)
            if current_channel.id != expected_channel_id:
                raise MusicError("Bạn đã đổi voice trong lúc tìm bài; hãy chạy lệnh lại.")
            await self.ensure_voice(guild, current_channel)
            self._assert_active_epoch(guild.id, epoch)
            post_connect_channel = self.validate_voice_access(guild, member)
            if post_connect_channel.id != expected_channel_id:
                raise MusicError("Bạn đã đổi voice trong lúc kết nối; hãy chạy lệnh lại.")

    async def add_query(
        self,
        guild: discord.Guild,
        member: discord.Member,
        text_channel: object,
        query: str,
    ) -> QueueAddResult:
        if self._closed or not self.config.enabled:
            raise MusicError("Tính năng nhạc đang tắt.")
        epoch = self._guild_epoch(guild.id)
        voice_channel = self.validate_voice_access(guild, member)
        await self._begin_request(guild.id, member.id)
        try:
            resolve_task = asyncio.create_task(
                self.resolver.resolve(query, member.id),
                name=f"craftopia-music-resolve-{guild.id}-{member.id}",
            )
            connect_task = asyncio.create_task(
                self._connect_for_request(guild, member, voice_channel.id, epoch),
                name=f"craftopia-music-connect-{guild.id}-{member.id}",
            )
            try:
                batch, _ = await asyncio.gather(resolve_task, connect_task)
            except BaseException:
                for task in (resolve_task, connect_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(resolve_task, connect_task, return_exceptions=True)
                player = self.players.get(guild.id)
                if player is not None:
                    player._schedule_idle_if_needed()
                raise
            self._assert_active_epoch(guild.id, epoch)
            async with self._guild_lock(guild.id):
                self._assert_active_epoch(guild.id, epoch)
                current_channel = self.validate_voice_access(guild, member)
                if current_channel.id != voice_channel.id:
                    raise MusicError("Bạn đã đổi voice trong lúc tìm bài; hãy chạy lệnh lại.")
                player: GuildPlayer | None = None
                enqueued = False
                try:
                    await self.ensure_voice(guild, current_channel)
                    self._assert_active_epoch(guild.id, epoch)
                    post_connect_channel = self.validate_voice_access(guild, member)
                    if post_connect_channel.id != voice_channel.id:
                        raise MusicError("Bạn đã đổi voice trong lúc kết nối; hãy chạy lệnh lại.")
                    player = self.get_or_create(guild)
                    text_channel_id = int(getattr(text_channel, "id", 0) or 0)
                    result = await player.enqueue(
                        batch.tracks,
                        text_channel_id,
                        batch.prepared_stream,
                    )
                    enqueued = True
                finally:
                    if not enqueued:
                        player = player or self.players.get(guild.id)
                        if player is not None:
                            player._schedule_idle_if_needed()
            return replace(result, skipped=batch.skipped)
        finally:
            await self._end_request(guild.id, member.id)

    async def snapshot(self, guild_id: int) -> MusicSnapshot:
        player = self.players.get(guild_id)
        if player is None:
            return MusicSnapshot(
                guild_id=guild_id,
                current=None,
                queued=(),
                paused=False,
                playing=False,
                loading=False,
                loop_mode="off",
                volume=self.config.default_volume,
                voice_channel_id=None,
                last_error=None,
            )
        return await player.snapshot()

    async def stop_player(self, player: GuildPlayer) -> str:
        """Stop playback and invalidate play requests that have not reached the queue."""
        guild_id = player.guild.id
        async with self._guild_lock(guild_id):
            current = self.players.get(guild_id)
            if current is not player or player.closed:
                raise MusicError("Phiên nhạc vừa kết thúc; hãy chạy lại `/play`.")
            self._advance_guild_epoch(guild_id)
            return await player.stop()

    async def publish_panel(
        self,
        guild: discord.Guild,
        channel: object,
        *,
        force_new: bool = False,
    ) -> object:
        if self._closed or not self.config.enabled:
            raise MusicError("Tính năng nhạc đang tắt.")
        player = self.get_or_create(guild)
        send = getattr(channel, "send", None)
        if not callable(send):
            raise MusicError("Kênh này không thể nhận bảng điều khiển nhạc.")
        async with player.panel_lock:
            snapshot = await player.snapshot()
            embed = render_music_panel(snapshot)
            existing = player.panel_message
            same_channel = getattr(getattr(existing, "channel", None), "id", None) == getattr(channel, "id", None)
            if existing is not None and same_channel and not force_new:
                try:
                    await existing.edit(embed=embed, view=self.control_view)
                    return existing
                except discord.NotFound:
                    player.panel_message = None
                except (discord.Forbidden, discord.HTTPException) as exc:
                    raise MusicError("Không thể cập nhật bảng nhạc; kiểm tra quyền Embed Links.") from exc
            if existing is not None:
                try:
                    await existing.edit(view=None)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    pass
            try:
                message = await send(
                    embed=embed,
                    view=self.control_view,
                    allowed_mentions=ALLOWED_MENTIONS_NONE,
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise MusicError("Không thể gửi bảng nhạc; bot cần Send Messages và Embed Links.") from exc
            player.panel_message = message
            player.text_channel_id = int(getattr(channel, "id", 0) or 0)
            return message

    async def update_panel(self, guild_id: int) -> None:
        player = self.players.get(guild_id)
        if player is None or player.panel_message is None:
            return
        async with player.panel_lock:
            message = player.panel_message
            try:
                await message.edit(
                    embed=render_music_panel(await player.snapshot()),
                    view=self.control_view,
                )
            except discord.NotFound:
                player.panel_message = None
            except (discord.Forbidden, discord.HTTPException, aiohttp.ClientError, OSError):
                logger.warning("Cannot update music panel for guild %s", guild_id)

    def schedule_panel_update(self, guild_id: int) -> None:
        player = self.players.get(guild_id)
        if player is None or player.panel_message is None:
            return
        player.panel_dirty = True
        if player.panel_update_task and not player.panel_update_task.done():
            return

        async def delayed() -> None:
            try:
                while True:
                    await asyncio.sleep(0.35)
                    current = self.players.get(guild_id)
                    if current is not player or player.closed:
                        return
                    player.panel_dirty = False
                    await self.update_panel(guild_id)
                    if not player.panel_dirty:
                        return
            finally:
                current = self.players.get(guild_id)
                if current and current.panel_update_task is asyncio.current_task():
                    current.panel_update_task = None
                    if current.panel_dirty and not current.closed:
                        self.schedule_panel_update(guild_id)

        player.panel_update_task = asyncio.create_task(
            delayed(), name=f"craftopia-music-panel-{guild_id}"
        )

    def panel_is_current(self, guild_id: int, message_id: int | None) -> bool:
        player = self.players.get(guild_id)
        if player is None or player.panel_message is None:
            return False
        return getattr(player.panel_message, "id", None) == message_id

    async def notify(self, guild_id: int, content: str) -> None:
        player = self.players.get(guild_id)
        if player is None or not player.text_channel_id:
            return
        channel = self.bot.get_channel(player.text_channel_id)
        send = getattr(channel, "send", None)
        if callable(send):
            try:
                await send(content, allowed_mentions=ALLOWED_MENTIONS_NONE)
            except (
                discord.Forbidden,
                discord.HTTPException,
                aiohttp.ClientError,
                OSError,
            ) as exc:
                logger.warning(
                    "Cannot send music notice for guild %s: %s",
                    guild_id,
                    type(exc).__name__,
                )

    async def leave(self, guild_id: int, *, reason: str = "Theo yêu cầu") -> None:
        self.cancel_bot_disconnect_check(guild_id)
        logger.info("Leaving voice guild=%s reason=%s", guild_id, reason)
        async with self._guild_lock(guild_id):
            self._advance_guild_epoch(guild_id)
            player = self.players.get(guild_id)
            if player is None:
                return
            await player.close(disconnect=True)
            if self.players.get(guild_id) is player:
                self.players.pop(guild_id, None)
        message = player.panel_message
        if message is not None:
            try:
                snapshot = MusicSnapshot(
                    guild_id=guild_id,
                    current=None,
                    queued=(),
                    paused=False,
                    playing=False,
                    loading=False,
                    loop_mode="off",
                    volume=player.volume,
                    voice_channel_id=None,
                    last_error=f"Đã rời voice: {reason}",
                )
                await message.edit(embed=render_music_panel(snapshot), view=None)
            except (
                discord.Forbidden,
                discord.NotFound,
                discord.HTTPException,
                aiohttp.ClientError,
                OSError,
            ):
                pass

    async def handle_bot_disconnect(
        self,
        guild_id: int,
        *,
        expected_player: GuildPlayer | None = None,
        confirm_disconnected: bool = False,
    ) -> None:
        self.cancel_bot_disconnect_check(guild_id)
        async with self._guild_lock(guild_id):
            player = self.players.get(guild_id)
            if player is None:
                return
            if expected_player is not None and player is not expected_player:
                return
            voice = player.guild.voice_client
            if confirm_disconnected and voice and voice.is_connected():
                logger.info(
                    "Discord voice recovered before teardown lock for guild %s",
                    guild_id,
                )
                return
            self._advance_guild_epoch(guild_id)
            # The voice-state event may leave a stale discord.py cache entry even
            # though transport.is_connected() is already false. close(True) now
            # cleans that entry without sending another disconnect request.
            await player.close(disconnect=True)
            if self.players.get(guild_id) is player:
                self.players.pop(guild_id, None)
        message = player.panel_message
        if message is not None:
            try:
                snapshot = MusicSnapshot(
                    guild_id=guild_id,
                    current=None,
                    queued=(),
                    paused=False,
                    playing=False,
                    loading=False,
                    loop_mode="off",
                    volume=player.volume,
                    voice_channel_id=None,
                    last_error="Bot đã bị ngắt kết nối khỏi voice.",
                )
                await message.edit(embed=render_music_panel(snapshot), view=None)
            except (
                discord.Forbidden,
                discord.NotFound,
                discord.HTTPException,
                aiohttp.ClientError,
                OSError,
            ):
                pass

    def reconcile_listeners(self, guild: discord.Guild) -> None:
        player = self.players.get(guild.id)
        if player is None:
            return
        voice = guild.voice_client
        channel = getattr(voice, "channel", None)
        if channel is None:
            return
        humans = [member for member in getattr(channel, "members", ()) if not member.bot]
        if humans:
            player.cancel_empty_channel_timer()
        else:
            player.schedule_empty_channel_timer()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info("Music manager shutdown started guilds=%s", len(self.players))
        recovery_tasks = tuple(self._voice_disconnect_tasks.values())
        self._voice_disconnect_tasks.clear()
        recovery_events = tuple(self._voice_recovery_events.values())
        self._voice_recovery_events.clear()
        for event in recovery_events:
            event.set()
        for task in recovery_tasks:
            if not task.done():
                task.cancel()
        if recovery_tasks:
            await asyncio.gather(*recovery_tasks, return_exceptions=True)

        async def close_guild(guild_id: int) -> None:
            async with self._guild_lock(guild_id):
                self._advance_guild_epoch(guild_id)
                player = self.players.pop(guild_id, None)
                if player is not None:
                    await player.close(disconnect=True)

        await asyncio.gather(
            *(close_guild(guild_id) for guild_id in tuple(self.players)),
            return_exceptions=True,
        )
        if self.resolver is not None:
            await self.resolver.close()
        logger.info("Music manager shutdown completed")


class MusicAddModal(discord.ui.Modal, title="Thêm bài vào Craftopia Music"):
    query = discord.ui.TextInput(
        label="Tên bài hoặc link YouTube/SoundCloud",
        placeholder="Ví dụ: nhạc chill minecraft",
        min_length=1,
        max_length=300,
    )

    def __init__(self, manager: MusicManager) -> None:
        super().__init__(timeout=180)
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Chỉ dùng được trong server.", ephemeral=True)
            return
        try:
            self.manager.authorize_add_from_panel(interaction.user)
        except MusicError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self.manager.add_query(
                interaction.guild,
                interaction.user,
                interaction.channel,
                str(self.query.value),
            )
            self.manager.schedule_panel_update(interaction.guild.id)
            text = _queue_result_text(result)
        except MusicError as exc:
            text = f"❌ {exc}"
        except Exception:
            logger.exception("Music add modal failed in guild %s", interaction.guild.id)
            text = "❌ Không thể thêm bài do lỗi tạm thời."
        await interaction.followup.send(
            text,
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )


class MusicControlView(discord.ui.View):
    def __init__(self, manager: MusicManager) -> None:
        super().__init__(timeout=None)
        self.manager = manager

    async def _guard(
        self,
        interaction: discord.Interaction,
        *,
        destructive: bool = False,
    ) -> GuildPlayer | None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Chỉ dùng được trong server.", ephemeral=True)
            return None
        message_id = getattr(interaction.message, "id", None)
        if not self.manager.panel_is_current(interaction.guild.id, message_id):
            await interaction.response.send_message(
                "Đây là bảng điều khiển cũ. Dùng `/music` hoặc `~music` để tạo bảng mới.",
                ephemeral=True,
            )
            return None
        try:
            return self.manager.authorize_control(
                interaction.user,
                destructive=destructive,
            )
        except MusicError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return None

    async def _run_player_action(
        self,
        interaction: discord.Interaction,
        method_name: str,
        *,
        destructive: bool = False,
        args: tuple = (),
    ) -> None:
        player = await self._guard(interaction, destructive=destructive)
        if player is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            if method_name == "stop":
                text = await self.manager.stop_player(player)
            else:
                text = await getattr(player, method_name)(*args)
        except MusicError as exc:
            text = f"❌ {exc}"
        except Exception:
            logger.exception("Panel action %s failed in guild %s", method_name, interaction.guild_id)
            text = "❌ Điều khiển nhạc gặp lỗi tạm thời."
        await interaction.followup.send(
            text,
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )

    @discord.ui.button(
        label="Dừng / Phát",
        emoji="⏯️",
        style=discord.ButtonStyle.primary,
        custom_id="craftopia:music:toggle",
        row=0,
    )
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(interaction, "toggle_pause")

    @discord.ui.button(
        label="Bỏ qua",
        emoji="⏭️",
        style=discord.ButtonStyle.primary,
        custom_id="craftopia:music:skip",
        row=0,
    )
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(interaction, "skip")

    @discord.ui.button(
        label="Dừng hết",
        emoji="⏹️",
        style=discord.ButtonStyle.danger,
        custom_id="craftopia:music:stop",
        row=0,
    )
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(interaction, "stop", destructive=True)

    @discord.ui.button(
        label="Loop",
        emoji="🔁",
        style=discord.ButtonStyle.secondary,
        custom_id="craftopia:music:loop",
        row=0,
    )
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(interaction, "set_loop")

    @discord.ui.button(
        label="Trộn",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        custom_id="craftopia:music:shuffle",
        row=0,
    )
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(interaction, "shuffle")

    @discord.ui.button(
        label="Thêm bài",
        emoji="➕",
        style=discord.ButtonStyle.success,
        custom_id="craftopia:music:add",
        row=1,
    )
    async def add(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Chỉ dùng được trong server.", ephemeral=True)
            return
        message_id = getattr(interaction.message, "id", None)
        if not self.manager.panel_is_current(interaction.guild.id, message_id):
            await interaction.response.send_message(
                "Đây là bảng điều khiển cũ. Dùng `/music` hoặc `~music` để tạo bảng mới.",
                ephemeral=True,
            )
            return
        try:
            self.manager.authorize_add_from_panel(interaction.user)
        except MusicError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_modal(MusicAddModal(self.manager))

    @discord.ui.button(
        label="Hàng đợi",
        emoji="📜",
        style=discord.ButtonStyle.secondary,
        custom_id="craftopia:music:queue",
        row=1,
    )
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._guard(interaction)
        if player is None:
            return
        await interaction.response.send_message(
            embed=render_queue_embed(await player.snapshot()),
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS_NONE,
        )

    @discord.ui.button(
        label="-10%",
        emoji="🔉",
        style=discord.ButtonStyle.secondary,
        custom_id="craftopia:music:volume_down",
        row=1,
    )
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(
            interaction,
            "adjust_volume",
            args=(-10,),
        )

    @discord.ui.button(
        label="+10%",
        emoji="🔊",
        style=discord.ButtonStyle.secondary,
        custom_id="craftopia:music:volume_up",
        row=1,
    )
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._run_player_action(
            interaction,
            "adjust_volume",
            args=(10,),
        )

    @discord.ui.button(
        label="Rời voice",
        emoji="🚪",
        style=discord.ButtonStyle.danger,
        custom_id="craftopia:music:leave",
        row=1,
    )
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = await self._guard(interaction, destructive=True)
        if player is None:
            return
        await interaction.response.defer(ephemeral=True)
        await self.manager.leave(player.guild.id, reason=f"Yêu cầu bởi {interaction.user.id}")
        await interaction.followup.send("🚪 Đã rời voice và kết thúc phiên nhạc.", ephemeral=True)


def _queue_result_text(result: QueueAddResult) -> str:
    if len(result.tracks) == 1:
        track = result.tracks[0]
        text = (
            f"✅ Đã thêm **{discord.utils.escape_markdown(track.title)[:150]}** "
            f"vào vị trí **{result.first_position}**."
        )
    else:
        text = (
            f"✅ Đã thêm **{len(result.tracks)}** bài, bắt đầu từ vị trí "
            f"**{result.first_position}**."
        )
    if result.skipped:
        text += f" Bỏ qua **{result.skipped}** mục không khả dụng/quá giới hạn."
    return text


class MusicCog(commands.Cog, name="Craftopia Music"):
    def __init__(self, bot: commands.Bot, manager: MusicManager) -> None:
        self.bot = bot
        self.manager = manager

    async def _defer(self, ctx: commands.Context) -> None:
        if ctx.interaction and not ctx.interaction.response.is_done():
            await ctx.defer(ephemeral=True)

    async def _send(
        self,
        ctx: commands.Context,
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        ephemeral: bool = True,
    ) -> None:
        if ctx.interaction:
            await ctx.send(
                content,
                embed=embed,
                ephemeral=ephemeral,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )
        else:
            await ctx.reply(
                content,
                embed=embed,
                mention_author=False,
                allowed_mentions=ALLOWED_MENTIONS_NONE,
            )

    def _member(self, ctx: commands.Context) -> discord.Member:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            raise MusicError("Lệnh nhạc chỉ dùng trong server.")
        return ctx.author

    async def _control(
        self,
        ctx: commands.Context,
        method_name: str,
        *,
        destructive: bool = False,
        args: tuple = (),
    ) -> None:
        try:
            member = self._member(ctx)
            player = self.manager.authorize_control(member, destructive=destructive)
            await self._defer(ctx)
            if method_name == "stop":
                text = await self.manager.stop_player(player)
            else:
                text = await getattr(player, method_name)(*args)
            try:
                await self.manager.publish_panel(ctx.guild, ctx.channel)
            except MusicError as panel_error:
                text += f"\n⚠️ Điều khiển đã chạy nhưng bảng nhạc chưa cập nhật: {panel_error}"
        except MusicError as exc:
            text = f"❌ {exc}"
        except Exception:
            logger.exception("Music command %s failed in guild %s", method_name, getattr(ctx.guild, "id", None))
            text = "❌ Lệnh nhạc gặp lỗi tạm thời."
        await self._send(ctx, text)

    @commands.hybrid_command(
        name="play",
        aliases=["p"],
        description="Phát hoặc thêm bài vào hàng đợi Craftopia",
    )
    @app_commands.describe(query="Tên bài hoặc link YouTube/SoundCloud")
    @commands.cooldown(2, 10.0, commands.BucketType.member)
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        try:
            member = self._member(ctx)
            await self._defer(ctx)
            result = await self.manager.add_query(ctx.guild, member, ctx.channel, query)
            text = _queue_result_text(result)
            try:
                await self.manager.publish_panel(ctx.guild, ctx.channel)
            except MusicError as panel_error:
                text += f"\n⚠️ Bài đã được thêm nhưng chưa gửi được bảng nhạc: {panel_error}"
        except MusicError as exc:
            text = f"❌ {exc}"
        except Exception:
            logger.exception("Play command failed in guild %s", getattr(ctx.guild, "id", None))
            text = "❌ Không thể tìm hoặc phát bài do lỗi tạm thời."
        await self._send(ctx, text)

    @commands.hybrid_command(
        name="music",
        aliases=["panel"],
        description="Gửi bảng điều khiển Craftopia Music VIP",
    )
    @commands.cooldown(1, 10.0, commands.BucketType.member)
    async def music_panel(self, ctx: commands.Context) -> None:
        try:
            member = self._member(ctx)
            self.manager.validate_voice_access(ctx.guild, member)
            await self._defer(ctx)
            panel = await self.manager.publish_panel(ctx.guild, ctx.channel)
            jump_url = getattr(panel, "jump_url", None)
            suffix = f" {jump_url}" if jump_url else ""
            text = f"🎛️ Bảng điều khiển Craftopia Music VIP đã sẵn sàng.{suffix}"
        except MusicError as exc:
            text = f"❌ {exc}"
        await self._send(ctx, text)

    @commands.hybrid_command(
        name="queue",
        aliases=["q"],
        description="Xem hàng đợi nhạc Craftopia",
    )
    async def music_queue(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            await self._send(ctx, "❌ Lệnh nhạc chỉ dùng trong server.")
            return
        snapshot = await self.manager.snapshot(ctx.guild.id)
        await self._send(ctx, embed=render_queue_embed(snapshot))

    @commands.hybrid_command(
        name="nowplaying",
        aliases=["np"],
        description="Xem bài đang phát và bảng điều khiển",
    )
    async def now_playing(self, ctx: commands.Context) -> None:
        if not ctx.guild:
            await self._send(ctx, "❌ Lệnh nhạc chỉ dùng trong server.")
            return
        await self._send(ctx, embed=render_music_panel(await self.manager.snapshot(ctx.guild.id)))

    @commands.hybrid_command(name="pause", description="Tạm dừng nhạc")
    async def pause_music(self, ctx: commands.Context) -> None:
        await self._control(ctx, "pause")

    @commands.hybrid_command(name="resume", description="Tiếp tục phát nhạc")
    async def resume_music(self, ctx: commands.Context) -> None:
        await self._control(ctx, "resume")

    @commands.hybrid_command(name="skip", aliases=["s"], description="Bỏ qua bài hiện tại")
    async def skip_music(self, ctx: commands.Context) -> None:
        await self._control(ctx, "skip")

    @commands.hybrid_command(name="shuffle", description="Trộn hàng đợi")
    async def shuffle_music(self, ctx: commands.Context) -> None:
        await self._control(ctx, "shuffle")

    @commands.hybrid_command(name="loop", description="Đổi chế độ lặp: off, track hoặc queue")
    @app_commands.describe(mode="Để trống để chuyển sang chế độ kế tiếp")
    async def loop_music(self, ctx: commands.Context, mode: str | None = None) -> None:
        await self._control(ctx, "set_loop", args=(mode,))

    @commands.hybrid_command(name="volume", description="Đặt âm lượng từ 10 đến 100 phần trăm")
    @app_commands.describe(percent="Âm lượng 10–100")
    async def volume_music(self, ctx: commands.Context, percent: int) -> None:
        if percent < 10 or percent > 100:
            await self._send(ctx, "❌ Âm lượng phải từ 10 đến 100.")
            return
        await self._control(ctx, "set_volume", args=(percent,))

    @commands.hybrid_command(name="stop", description="Dừng và xoá hàng đợi (DJ/Manage Server)")
    async def stop_music(self, ctx: commands.Context) -> None:
        await self._control(ctx, "stop", destructive=True)

    @commands.hybrid_command(name="leave", description="Cho bot rời voice (DJ/Manage Server)")
    async def leave_music(self, ctx: commands.Context) -> None:
        try:
            member = self._member(ctx)
            player = self.manager.authorize_control(member, destructive=True)
            await self._defer(ctx)
            await self.manager.leave(player.guild.id, reason=f"Yêu cầu bởi {member.id}")
            text = "🚪 Đã rời voice và kết thúc phiên nhạc."
        except MusicError as exc:
            text = f"❌ {exc}"
        await self._send(ctx, text)

    @commands.hybrid_command(
        name="music_diagnose",
        description="Kiểm tra voice, DAVE và FFmpeg cho quản trị viên",
    )
    async def music_diagnose(self, ctx: commands.Context) -> None:
        try:
            member = self._member(ctx)
            if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
                raise MusicError("Lệnh này cần quyền **Manage Server**.")
            await self._defer(ctx)
            versions = {}
            for package in (
                "discord.py",
                "aiohttp",
                "PyNaCl",
                "davey",
                "yt-dlp",
                "imageio-ffmpeg",
            ):
                try:
                    versions[package] = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    versions[package] = "THIẾU"

            def ffmpeg_version() -> str:
                result = subprocess.run(
                    [self.manager.audio_factory.executable, "-version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                return (result.stdout or result.stderr).splitlines()[0][:180]

            try:
                ffmpeg = await asyncio.to_thread(ffmpeg_version)
            except (OSError, subprocess.SubprocessError):
                ffmpeg = "THIẾU/không chạy được"
            voice_line = "Bạn chưa ở voice"
            try:
                channel = self.manager.member_voice_channel(member)
                permissions = channel.permissions_for(ctx.guild.me)
                voice_line = (
                    f"{channel.name}: View={permissions.view_channel}, "
                    f"Connect={permissions.connect}, Speak={permissions.speak}"
                )
            except MusicError:
                pass
            mode = "PCM + volume tức thời" if discord.opus.is_loaded() else "Opus FFmpeg + volume từ bài kế"
            text = (
                "🎛️ **Craftopia Music Diagnose**\n"
                f"Build: `{MUSIC_RECOVERY_BUILD}`\n"
                f"discord.py: `{versions['discord.py']}` · aiohttp: `{versions['aiohttp']}` · "
                f"DAVE: `{versions['davey']}` · "
                f"PyNaCl: `{versions['PyNaCl']}`\n"
                f"yt-dlp: `{versions['yt-dlp']}` · imageio-ffmpeg: `{versions['imageio-ffmpeg']}`\n"
                f"FFmpeg: `{discord.utils.escape_markdown(ffmpeg)}`\n"
                f"Executable: `{discord.utils.escape_markdown(self.manager.audio_factory.executable)[:300]}` · "
                f"fallback: **{max(0, len(self.manager.audio_factory.executables) - 1)}**\n"
                f"Audio mode: **{mode}**\n"
                f"Stay mode: **{'24/7 (không tự rời)' if not self.manager.config.auto_leave else f'tự rời sau {self.manager.config.idle_seconds}s'}** · "
                f"voice grace: **{self.manager.config.voice_disconnect_grace_seconds}s** · "
                f"reconnect: **{self.manager.config.voice_reconnect_attempts}** · "
                f"playback retries: **{self.manager.config.playback_retries}**\n"
                f"Voice permissions: **{discord.utils.escape_markdown(voice_line)}**\n"
                f"Voice States Intent: **{self.bot.intents.voice_states}**"
            )
        except MusicError as exc:
            text = f"❌ {exc}"
        await self._send(ctx, text)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel and not after.channel:
                self.manager.schedule_bot_disconnect_check(
                    member.guild,
                    getattr(before.channel, "id", None),
                )
            elif after.channel:
                player = self.manager.state_for(member.guild.id)
                if player is not None:
                    player.desired_voice_channel_id = getattr(after.channel, "id", None)
                self.manager.signal_bot_voice_recovered(member.guild.id)
                self.manager.reconcile_listeners(member.guild)
            return
        player = self.manager.state_for(member.guild.id)
        if player is None:
            return
        voice = member.guild.voice_client
        bot_channel_id = getattr(getattr(voice, "channel", None), "id", None)
        if bot_channel_id in {
            getattr(before.channel, "id", None),
            getattr(after.channel, "id", None),
        }:
            self.manager.reconcile_listeners(member.guild)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            text = f"Bạn thao tác nhạc quá nhanh. Thử lại sau {error.retry_after:.0f} giây."
        elif isinstance(error, commands.MissingRequiredArgument):
            text = "Thiếu tham số. Ví dụ: `~play tên bài hát`."
        else:
            logger.error("Music prefix command failed: %r", error)
            text = "Lệnh nhạc gặp lỗi tạm thời."
        await self._send(ctx, text)
