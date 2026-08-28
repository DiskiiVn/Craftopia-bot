import asyncio
import socket
import threading
import time
import unittest
from collections import deque
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch, sentinel

import aiohttp
import discord
from discord.ext import commands

from music import (
    AudioSourceFactory,
    ErrorAwarePCMVolumeTransformer,
    GuildPlayer,
    MusicCog,
    MusicConfig,
    MusicControlView,
    MusicError,
    MusicManager,
    MusicSnapshot,
    MusicTrack,
    PREPARED_STREAM_TTL_SECONDS,
    QueueAddResult,
    ResolvedBatch,
    StreamResource,
    YTDLP_AUDIO_FORMAT,
    YTDLPResolver,
    _safe_stream_url,
    render_music_panel,
    validate_music_query,
)


def make_track(
    identifier: str,
    *,
    requester_id: int = 10,
    title: str | None = None,
    duration: int | None = 180,
) -> MusicTrack:
    return MusicTrack(
        identifier=identifier,
        title=title or f"Track {identifier}",
        webpage_url=f"https://www.youtube.com/watch?v={identifier}",
        source_url=f"https://www.youtube.com/watch?v={identifier}",
        requester_id=requester_id,
        duration=duration,
        thumbnail=None,
        uploader="Craftopia",
    )


class QueryValidationTests(unittest.TestCase):
    def test_search_text_is_normalized_without_becoming_a_url(self):
        self.assertEqual(
            validate_music_query("  lofi   cho Minecraft  ", ("youtube.com",)),
            "lofi cho Minecraft",
        )

    def test_allows_exact_hosts_and_real_subdomains_only(self):
        urls = (
            "https://youtube.com/watch?v=abc",
            "https://www.youtube.com/watch?v=abc",
            "http://m.youtube.com./watch?v=abc",
            "https://soundcloud.com/artist/song",
        )

        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(
                    validate_music_query(url, ("youtube.com", "soundcloud.com")),
                    url,
                )

    def test_rejects_direct_ip_and_private_metadata_targets(self):
        urls = (
            "http://127.0.0.1/audio",
            "http://10.0.0.1/audio",
            "http://169.254.169.254/latest/meta-data",
            "http://[::1]/audio",
            "https://8.8.8.8/audio",
        )

        for url in urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(MusicError, "địa chỉ IP"):
                    validate_music_query(url, ("youtube.com",))

    def test_rejects_allowlist_suffix_tricks_credentials_ports_and_schemes(self):
        cases = (
            "https://youtube.com.evil.example/watch?v=abc",
            "https://notyoutube.com/watch?v=abc",
            "https://user:password@youtube.com/watch?v=abc",
            "https://youtube.com:8443/watch?v=abc",
            "ftp://youtube.com/audio",
            "file:///etc/passwd",
        )

        for query in cases:
            with self.subTest(query=query):
                with self.assertRaises(MusicError):
                    validate_music_query(query, ("youtube.com",))

    def test_rejects_empty_and_oversized_queries(self):
        with self.assertRaises(MusicError):
            validate_music_query("   ", ("youtube.com",))
        with self.assertRaisesRegex(MusicError, "300"):
            validate_music_query("x" * 301, ("youtube.com",))


class StreamUrlSafetyTests(unittest.TestCase):
    def test_rejects_custom_stream_port_before_dns_lookup(self):
        with patch("music.socket.getaddrinfo") as getaddrinfo:
            result = _safe_stream_url("https://cdn.example:8443/audio")

        self.assertIsNone(result)
        getaddrinfo.assert_not_called()

    def test_rejects_dns_name_if_any_resolved_address_is_private(self):
        private_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
        ]
        mixed_result = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)),
        ]

        for addresses in (private_result, mixed_result):
            with self.subTest(addresses=addresses):
                with patch("music.socket.getaddrinfo", return_value=addresses):
                    self.assertIsNone(_safe_stream_url("https://cdn.example/audio"))

    def test_accepts_stream_host_only_when_dns_addresses_are_global(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        url = "https://cdn.example/audio"

        with patch("music.socket.getaddrinfo", return_value=addresses) as getaddrinfo:
            self.assertEqual(_safe_stream_url(url), url)

        getaddrinfo.assert_called_once_with("cdn.example", 443, type=socket.SOCK_STREAM)

    def test_prepared_stream_extractor_rejects_flat_webpage_and_accepts_full_media_url(self):
        webpage_url = "https://www.youtube.com/watch?v=flat"
        flat = {
            "_type": "url",
            "url": webpage_url,
            "webpage_url": webpage_url,
        }
        with patch("music.socket.getaddrinfo") as getaddrinfo:
            self.assertIsNone(YTDLPResolver._stream_url_from_info(flat))
        getaddrinfo.assert_not_called()

        media_url = "https://cdn.example/audio-stream"
        full = {
            "_type": "video",
            "url": media_url,
            "webpage_url": webpage_url,
        }
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ]
        with patch("music.socket.getaddrinfo", return_value=addresses):
            self.assertEqual(YTDLPResolver._stream_url_from_info(full), media_url)


class FullStreamPolicyTests(unittest.TestCase):
    def make_resolver(self, *, allow_live=False, max_duration=300):
        resolver = object.__new__(YTDLPResolver)
        resolver.config = MusicConfig(
            allow_live=allow_live,
            max_duration_seconds=max_duration,
        )
        return resolver

    def extract_full_info(self, info):
        youtube_dl = MagicMock()
        youtube_dl.__enter__.return_value = youtube_dl
        youtube_dl.extract_info.return_value = info
        return patch("music.YoutubeDL", return_value=youtube_dl), youtube_dl

    def test_full_stream_info_rejects_live_when_flat_metadata_did_not_mark_live(self):
        resolver = self.make_resolver(allow_live=False)
        flat_track = make_track("live", duration=None)
        self.assertFalse(flat_track.is_live)
        ytdlp_patch, youtube_dl = self.extract_full_info(
            {
                "url": "https://cdn.example/live-audio",
                "title": "Live discovered only during stream refresh",
                "duration": None,
                "live_status": "is_live",
            }
        )

        with ytdlp_patch, self.assertRaisesRegex(MusicError, "Livestream"):
            resolver._stream_sync(flat_track)

        youtube_dl.extract_info.assert_called_once_with(flat_track.source_url, download=False)

    def test_full_stream_info_rejects_long_track_when_flat_metadata_lacked_duration(self):
        resolver = self.make_resolver(max_duration=300)
        flat_track = make_track("long", duration=None)
        ytdlp_patch, youtube_dl = self.extract_full_info(
            {
                "url": "https://cdn.example/long-audio",
                "title": "Duration discovered only during stream refresh",
                "duration": 301,
                "is_live": False,
            }
        )

        with ytdlp_patch, self.assertRaisesRegex(MusicError, "giới hạn thời lượng"):
            resolver._stream_sync(flat_track)

        youtube_dl.extract_info.assert_called_once_with(flat_track.source_url, download=False)


class ResolverExtractionModeTests(unittest.TestCase):
    GLOBAL_DNS_RESULT = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
    ]

    def make_resolver(self, *, playlist_limit=2):
        resolver = object.__new__(YTDLPResolver)
        resolver.config = MusicConfig(playlist_limit=playlist_limit)
        return resolver

    def resolve_with_mocked_ytdlp(self, query, info, *, requester_id=10):
        resolver = self.make_resolver()
        youtube_dl = MagicMock()
        youtube_dl.__enter__.return_value = youtube_dl
        youtube_dl.extract_info.return_value = info
        with (
            patch("music.YoutubeDL", return_value=youtube_dl) as youtube_dl_class,
            patch("music.socket.getaddrinfo", return_value=self.GLOBAL_DNS_RESULT),
            patch("music.time.monotonic", return_value=123.0),
        ):
            batch = resolver._resolve_sync(query, requester_id)
        return batch, youtube_dl_class, youtube_dl

    def assert_direct_url_options(self, options):
        self.assertEqual(options["extract_flat"], "in_playlist")
        self.assertEqual(options["playlistend"], 2)
        self.assertEqual(options["format"], YTDLP_AUDIO_FORMAT)
        self.assertNotIn("noplaylist", options)

    def test_search_extracts_full_result_and_prepares_stream_in_one_pass(self):
        media = {
            "_type": "video",
            "id": "search-result",
            "title": "Craftopia Lofi",
            "webpage_url": "https://www.youtube.com/watch?v=search-result",
            "url": "https://cdn.example/search-audio",
            "extractor_key": "Youtube",
            "duration": 120,
        }
        info = {"_type": "playlist", "entries": [media]}

        batch, youtube_dl_class, youtube_dl = self.resolve_with_mocked_ytdlp(
            "craftopia lofi",
            info,
        )

        youtube_dl.extract_info.assert_called_once_with(
            "ytsearch1:craftopia lofi",
            download=False,
        )
        options = youtube_dl_class.call_args.args[0]
        self.assertIs(options["extract_flat"], False)
        self.assertEqual(options["playlistend"], 1)
        self.assertIs(options["noplaylist"], True)
        self.assertEqual(options["format"], YTDLP_AUDIO_FORMAT)
        self.assertEqual(len(batch.tracks), 1)
        self.assertIsNotNone(batch.prepared_stream)
        self.assertIs(batch.prepared_stream.track, batch.tracks[0])
        self.assertEqual(batch.prepared_stream.url, media["url"])
        self.assertEqual(batch.prepared_stream.prepared_at, 123.0)

    def test_arbitrary_direct_single_uses_full_info_and_prepares_stream(self):
        query = "https://media.example/items/craftopia-theme"
        info = {
            "_type": "video",
            "id": "direct-single",
            "title": "Craftopia Theme",
            "webpage_url": query,
            "url": "https://cdn.example/direct-audio",
            "extractor_key": "Generic",
            "duration": 90,
        }

        batch, youtube_dl_class, youtube_dl = self.resolve_with_mocked_ytdlp(query, info)

        youtube_dl.extract_info.assert_called_once_with(query, download=False)
        self.assert_direct_url_options(youtube_dl_class.call_args.args[0])
        self.assertEqual(len(batch.tracks), 1)
        self.assertIsNotNone(batch.prepared_stream)
        self.assertIs(batch.prepared_stream.track, batch.tracks[0])
        self.assertEqual(batch.prepared_stream.url, info["url"])
        self.assertEqual(batch.prepared_stream.prepared_at, 123.0)

    def test_arbitrary_direct_playlist_keeps_entries_flat_limits_and_does_not_prepare(self):
        query = "https://media.example/creator/craftopia/uploads"
        entries = [
            {
                "_type": "url",
                "id": identifier,
                "title": f"Upload {identifier}",
                "webpage_url": f"https://media.example/watch/{identifier}",
                "url": f"https://media.example/watch/{identifier}",
                "extractor_key": "Generic",
                "duration": 60,
            }
            for identifier in ("one", "two", "three")
        ]
        info = {
            "_type": "playlist",
            "title": "Craftopia uploads",
            "entries": entries,
        }

        batch, youtube_dl_class, youtube_dl = self.resolve_with_mocked_ytdlp(query, info)

        youtube_dl.extract_info.assert_called_once_with(query, download=False)
        self.assert_direct_url_options(youtube_dl_class.call_args.args[0])
        self.assertEqual(
            tuple(track.title for track in batch.tracks),
            ("Upload one", "Upload two"),
        )
        self.assertEqual(batch.playlist_title, "Craftopia uploads")
        self.assertIsNone(batch.prepared_stream)


class StubManager:
    def __init__(self, config: MusicConfig, *, resource: StreamResource | None = None):
        self.config = config
        self.schedule_panel_update = Mock()
        self.notify = AsyncMock()
        self.leave = AsyncMock()
        self.recover_voice = AsyncMock(return_value=False)
        self.resolver = SimpleNamespace(
            stream_for=AsyncMock(return_value=resource),
        )
        self.audio_factory = SimpleNamespace(
            create=Mock(return_value=FakeAudioSource()),
            executables=("ffmpeg-primary", "ffmpeg-fallback"),
        )


class FakeAudioSource:
    def __init__(self):
        self.cleanup_calls = 0
        self._cleaned = False

    def cleanup(self):
        # Discord AudioSource.__del__ may call cleanup after the player's explicit
        # bounded cleanup. Real FFmpeg cleanup is idempotent, so the fake records
        # one effective cleanup rather than destructor timing.
        if not self._cleaned:
            self._cleaned = True
            self.cleanup_calls += 1


class FakeVoiceClient:
    def __init__(self, channel_id: int = 50):
        self.channel = SimpleNamespace(id=channel_id)
        self.source = None
        self.after = None
        self.play_calls = 0
        self.stop_calls = 0
        self.pause_calls = 0
        self.resume_calls = 0
        self.disconnect_calls = 0
        self.cleanup_calls = 0
        self._connected = True
        self._playing = False
        self._paused = False

    def is_connected(self):
        return self._connected

    def is_playing(self):
        return self._playing

    def is_paused(self):
        return self._paused

    def play(self, source, *, after):
        self.source = source
        self.after = after
        self.play_calls += 1
        self._playing = True
        self._paused = False

    def finish(self, error=None, *, played_seconds: float | None = 180.0):
        # GuildPlayer now measures actual audio frames through ProgressAudioSource.
        # Tests advance that counter explicitly so a normal finish is not mistaken
        # for a premature EOF, while interruption tests can supply a partial value.
        if played_seconds is not None and hasattr(self.source, "played_seconds"):
            self.source.played_seconds = max(0.0, float(played_seconds))
        callback = self.after
        self.after = None
        self._playing = False
        self._paused = False
        if callback:
            callback(error)

    def stop(self):
        self.stop_calls += 1
        self.finish()

    def pause(self):
        self.pause_calls += 1
        self._playing = False
        self._paused = True

    def resume(self):
        self.resume_calls += 1
        self._playing = True
        self._paused = False

    async def disconnect(self, *, force):
        self.assert_force = force
        self.disconnect_calls += 1
        self._connected = False

    def cleanup(self):
        self.cleanup_calls += 1


def make_player(
    *,
    config: MusicConfig | None = None,
    voice: FakeVoiceClient | None = None,
    resource: StreamResource | None = None,
):
    config = config or MusicConfig(
        queue_limit=10,
        per_user_limit=5,
        idle_seconds=180,
        playback_retries=1,
        playback_retry_backoff_seconds=0,
    )
    manager = StubManager(config, resource=resource)
    guild = SimpleNamespace(id=1, voice_client=voice)
    player = GuildPlayer(manager=manager, guild=guild, volume=config.default_volume)
    return player, manager, guild


class QueueLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_capacity_rejects_entire_batch_without_mutation(self):
        config = MusicConfig(queue_limit=3, per_user_limit=3)
        player, manager, _ = make_player(config=config)
        original = (make_track("a", requester_id=1), make_track("b", requester_id=2))
        player.queue.extend(original)
        player.text_channel_id = 700
        player.last_error = "giữ nguyên"
        player.worker_task = SimpleNamespace(done=lambda: False)

        with self.assertRaisesRegex(MusicError, "playlist chưa được thêm"):
            await player.enqueue(
                (make_track("c", requester_id=3), make_track("d", requester_id=3)),
                text_channel_id=701,
            )

        self.assertEqual(tuple(player.queue), original)
        self.assertEqual(player.text_channel_id, 700)
        self.assertEqual(player.last_error, "giữ nguyên")
        self.assertFalse(player.queue_event.is_set())
        manager.schedule_panel_update.assert_not_called()

    async def test_per_user_capacity_rejects_entire_batch_without_partial_append(self):
        config = MusicConfig(queue_limit=10, per_user_limit=2)
        player, manager, _ = make_player(config=config)
        original = (make_track("a", requester_id=10),)
        player.queue.extend(original)
        player.worker_task = SimpleNamespace(done=lambda: False)

        with self.assertRaisesRegex(MusicError, "Mỗi thành viên"):
            await player.enqueue(
                (make_track("b", requester_id=10), make_track("c", requester_id=10)),
                text_channel_id=701,
            )

        self.assertEqual(tuple(player.queue), original)
        self.assertIsNone(player.text_channel_id)
        self.assertFalse(player.queue_event.is_set())
        manager.schedule_panel_update.assert_not_called()

    async def test_successful_batch_preserves_order_and_reports_first_position(self):
        config = MusicConfig(queue_limit=3, per_user_limit=2)
        player, manager, _ = make_player(config=config)
        existing = make_track("a", requester_id=1)
        added = (make_track("b", requester_id=10), make_track("c", requester_id=10))
        player.queue.append(existing)
        player.worker_task = SimpleNamespace(done=lambda: False)

        result = await player.enqueue(added, text_channel_id=701)

        self.assertEqual(tuple(player.queue), (existing, *added))
        self.assertEqual(result.tracks, added)
        self.assertEqual(result.first_position, 2)
        self.assertEqual(player.text_channel_id, 701)
        self.assertTrue(player.queue_event.is_set())
        manager.schedule_panel_update.assert_called_once_with(1)


class PreparedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_enqueue_stores_prepared_stream_by_exact_track_identity_and_consumes_once(self):
        player, _, _ = make_player()
        player.worker_task = SimpleNamespace(done=lambda: False)
        track = make_track("identity")
        equal_clone = replace(track)
        self.assertEqual(equal_clone, track)
        self.assertIsNot(equal_clone, track)
        prepared = StreamResource(track, "https://cdn.example/prepared")
        loop = asyncio.get_running_loop()
        before = loop.time()

        await player.enqueue((track,), 90, prepared)

        self.assertIn(id(track), player.prepared_streams)
        self.assertNotIn(id(equal_clone), player.prepared_streams)
        _, deadline = player.prepared_streams[id(track)]
        self.assertGreaterEqual(deadline, before + PREPARED_STREAM_TTL_SECONDS)
        self.assertLessEqual(deadline, loop.time() + PREPARED_STREAM_TTL_SECONDS)
        self.assertIsNone(await player._take_prepared_stream(equal_clone))
        self.assertIs(await player._take_prepared_stream(track), prepared)
        self.assertIsNone(await player._take_prepared_stream(track))

    async def test_expired_prepared_stream_is_removed_without_sleeping_for_ttl(self):
        player, _, _ = make_player()
        player.worker_task = SimpleNamespace(done=lambda: False)
        track = make_track("expired")
        prepared = StreamResource(track, "https://cdn.example/expired")
        await player.enqueue((track,), 90, prepared)
        player.prepared_streams[id(track)] = (
            prepared,
            asyncio.get_running_loop().time() - 0.001,
        )

        self.assertIsNone(await player._take_prepared_stream(track))
        self.assertNotIn(id(track), player.prepared_streams)

    async def test_rejected_batch_does_not_store_prepared_stream(self):
        player, _, _ = make_player(config=MusicConfig(queue_limit=1, per_user_limit=5))
        player.worker_task = SimpleNamespace(done=lambda: False)
        player.queue.append(make_track("existing", requester_id=1))
        attempted = make_track("attempted", requester_id=2)
        prepared = StreamResource(attempted, "https://cdn.example/attempted")

        with self.assertRaises(MusicError):
            await player.enqueue((attempted,), 90, prepared)

        self.assertEqual(tuple(track.identifier for track in player.queue), ("existing",))
        self.assertEqual(player.prepared_streams, {})

    async def test_prepared_at_age_is_subtracted_from_enqueue_ttl(self):
        player, _, _ = make_player()
        player.worker_task = SimpleNamespace(done=lambda: False)
        track = make_track("aged")
        prepared = StreamResource(
            track,
            "https://cdn.example/aged",
            prepared_at=970.0,
        )
        loop = asyncio.get_running_loop()

        with patch("music.time.monotonic", return_value=1000.0):
            before = loop.time()
            await player.enqueue((track,), 90, prepared)
            after = loop.time()
        stored, deadline = player.prepared_streams[id(track)]
        expected_remaining = PREPARED_STREAM_TTL_SECONDS - 30.0
        self.assertIs(stored, prepared)
        self.assertGreaterEqual(deadline, before + expected_remaining)
        self.assertLessEqual(deadline, after + expected_remaining)

    async def test_already_expired_extracted_stream_is_not_stored(self):
        player, _, _ = make_player()
        player.worker_task = SimpleNamespace(done=lambda: False)
        track = make_track("already-expired")
        prepared = StreamResource(
            track,
            "https://cdn.example/already-expired",
            prepared_at=1000.0 - PREPARED_STREAM_TTL_SECONDS - 1.0,
        )

        with patch("music.time.monotonic", return_value=1000.0):
            result = await player.enqueue((track,), 90, prepared)

        self.assertEqual(result.tracks, (track,))
        self.assertEqual(tuple(player.queue), (track,))
        self.assertNotIn(id(track), player.prepared_streams)


class QueueOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_loop_modes_put_finished_track_in_the_expected_place(self):
        cases = (
            ("off", ("b", "c")),
            ("track", ("a", "b", "c")),
            ("queue", ("b", "c", "a")),
        )

        for loop_mode, expected in cases:
            with self.subTest(loop_mode=loop_mode):
                player, _, _ = make_player()
                player.loop_mode = loop_mode
                player.queue.extend((make_track("a"), make_track("b"), make_track("c")))

                track, generation = await player._take_next()
                self.assertEqual(track.identifier, "a")
                await player._finish_track(generation, None)

                self.assertIsNone(player.current)
                self.assertEqual(tuple(item.identifier for item in player.queue), expected)

    async def test_skip_action_never_requeues_track_even_with_track_loop(self):
        player, _, _ = make_player()
        player.current = make_track("a")
        player.queue.append(make_track("b"))
        player.play_generation = 4
        player.loop_mode = "track"
        player.finish_action = "skip"

        await player._finish_track(4, None)

        self.assertEqual(tuple(item.identifier for item in player.queue), ("b",))
        self.assertIsNone(player.current)

    async def test_shuffle_changes_only_waiting_queue(self):
        player, _, _ = make_player()
        current = make_track("now")
        player.current = current
        player.queue = deque((make_track("a"), make_track("b"), make_track("c")))

        with patch("music.random.SystemRandom") as system_random:
            system_random.return_value.shuffle.side_effect = lambda items: items.reverse()
            text = await player.shuffle()

        self.assertIs(player.current, current)
        self.assertEqual(tuple(item.identifier for item in player.queue), ("c", "b", "a"))
        self.assertIn("3", text)


class PlaybackCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _wait_for_play_calls(self, voice: FakeVoiceClient, expected: int):
        for _ in range(100):
            if voice.play_calls >= expected and voice.after is not None:
                return
            await asyncio.sleep(0)
        self.fail(f"voice.play was not called {expected} time(s)")

    async def _wait_until_play_called(self, voice: FakeVoiceClient):
        await self._wait_for_play_calls(voice, 1)

    async def test_valid_prepared_stream_starts_without_second_resolver_call(self):
        voice = FakeVoiceClient()
        track = make_track("fast")
        prepared = StreamResource(track, "https://cdn.example/prepared-fast")
        fallback = StreamResource(track, "https://cdn.example/should-not-be-used")
        player, manager, _ = make_player(voice=voice, resource=fallback)
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track,), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_until_play_called(voice)
        voice.finish()
        await asyncio.wait_for(play_task, timeout=1)

        manager.resolver.stream_for.assert_not_awaited()
        manager.audio_factory.create.assert_called_once_with(
            prepared.url,
            player.volume,
            attempt=0,
            start_at=0.0,
        )
        self.assertNotIn(id(track), player.prepared_streams)
        idle_task = player.idle_task
        player._cancel_idle()
        if idle_task is not None:
            await asyncio.gather(idle_task, return_exceptions=True)

    async def test_expired_prepared_stream_falls_back_to_stream_resolver(self):
        voice = FakeVoiceClient()
        track = make_track("expired-fast")
        prepared = StreamResource(track, "https://cdn.example/expired-fast")
        fallback = StreamResource(track, "https://cdn.example/refreshed")
        player, manager, _ = make_player(voice=voice, resource=fallback)
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track,), 90, prepared)
        player.prepared_streams[id(track)] = (
            prepared,
            asyncio.get_running_loop().time() - 0.001,
        )
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_until_play_called(voice)
        voice.finish()
        await asyncio.wait_for(play_task, timeout=1)

        manager.resolver.stream_for.assert_awaited_once_with(track)
        manager.audio_factory.create.assert_called_once_with(
            fallback.url,
            player.volume,
            attempt=0,
            start_at=0.0,
        )
        self.assertNotIn(id(track), player.prepared_streams)
        idle_task = player.idle_task
        player._cancel_idle()
        if idle_task is not None:
            await asyncio.gather(idle_task, return_exceptions=True)

    async def test_callback_without_error_captures_inner_sigsegv_then_retries_once(self):
        voice = FakeVoiceClient()
        track = make_track("segfault")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed_track = replace(track, title="Track segfault refreshed")
        refreshed = StreamResource(refreshed_track, "https://cdn.example/refreshed")
        first_source = FakeAudioSource()
        second_source = FakeAudioSource()
        expected_error = discord.FFmpegProcessError("FFmpeg exited with code -11")
        process = Mock()
        inner = SimpleNamespace(_current_error=None, _process=process)

        def detect_exit_code():
            inner._current_error = expected_error

        inner._check_process_returncode = Mock(side_effect=detect_exit_code)
        first_source.original = inner
        player, manager, _ = make_player(voice=voice, resource=refreshed)
        manager.audio_factory.create.side_effect = (first_source, second_source)
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track,), 90, prepared)
        taken_track, generation = await player._take_next()
        player.loop_mode = "track"

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        # Models discord.py receiving EOF before it notices FFmpeg's -11 exit.
        voice.finish(played_seconds=0.0)
        await self._wait_for_play_calls(voice, 2)
        voice.finish()
        await asyncio.wait_for(play_task, timeout=1)

        self.assertEqual(voice.play_calls, 2)
        process.wait.assert_called_once_with(timeout=0.25)
        inner._check_process_returncode.assert_called_once_with()
        manager.resolver.stream_for.assert_awaited_once_with(track)
        self.assertEqual(
            manager.audio_factory.create.call_args_list,
            [
                call(prepared.url, player.volume, attempt=0, start_at=0.0),
                call(refreshed.url, player.volume, attempt=1, start_at=0.0),
            ],
        )
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (refreshed_track,))

    async def test_two_early_ffmpeg_failures_cap_retry_and_notify_once(self):
        voice = FakeVoiceClient()
        track = make_track("double-segfault")
        next_track = make_track("next")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed = StreamResource(track, "https://cdn.example/refreshed")
        player, manager, _ = make_player(voice=voice, resource=refreshed)
        manager.audio_factory.create.side_effect = (FakeAudioSource(), FakeAudioSource())
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track, next_track), 90, prepared)
        taken_track, generation = await player._take_next()
        player.loop_mode = "track"

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice.finish(
            discord.FFmpegProcessError("FFmpeg exited with code -11"),
            played_seconds=0.0,
        )
        await self._wait_for_play_calls(voice, 2)
        voice.finish(
            discord.FFmpegProcessError("fallback exited with code -11"),
            played_seconds=0.0,
        )
        await asyncio.wait_for(play_task, timeout=1)

        self.assertEqual(voice.play_calls, 2)
        manager.resolver.stream_for.assert_awaited_once_with(track)
        self.assertEqual(manager.audio_factory.create.call_count, 2)
        manager.notify.assert_awaited_once()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (next_track,))
        self.assertIsNotNone(player.last_error)

    async def test_midtrack_ffmpeg_failure_refreshes_and_resumes_with_overlap(self):
        voice = FakeVoiceClient()
        track = make_track("midtrack", duration=180)
        next_track = make_track("next")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed = StreamResource(track, "https://cdn.example/refreshed")
        player, manager, _ = make_player(voice=voice, resource=refreshed)
        manager.audio_factory.create.side_effect = (FakeAudioSource(), FakeAudioSource())
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track, next_track), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice.finish(
            discord.FFmpegProcessError("FFmpeg exited midway"),
            played_seconds=60.0,
        )
        await self._wait_for_play_calls(voice, 2)
        voice.finish(played_seconds=122.0)
        await asyncio.wait_for(play_task, timeout=1)

        self.assertEqual(
            manager.audio_factory.create.call_args_list,
            [
                call(prepared.url, player.volume, attempt=0, start_at=0.0),
                call(refreshed.url, player.volume, attempt=1, start_at=58.0),
            ],
        )
        manager.resolver.stream_for.assert_awaited_once_with(track)
        manager.recover_voice.assert_not_awaited()
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (next_track,))
        self.assertEqual(player.resume_offset_seconds, 0.0)

    async def test_premature_clean_eof_refreshes_and_resumes_instead_of_skipping(self):
        voice = FakeVoiceClient()
        track = make_track("premature", duration=180)
        next_track = make_track("next")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed = StreamResource(track, "https://cdn.example/refreshed")
        player, manager, _ = make_player(voice=voice, resource=refreshed)
        manager.audio_factory.create.side_effect = (FakeAudioSource(), FakeAudioSource())
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track, next_track), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice.finish(played_seconds=45.0)
        await self._wait_for_play_calls(voice, 2)
        voice.finish(played_seconds=137.0)
        await asyncio.wait_for(play_task, timeout=1)

        self.assertEqual(
            manager.audio_factory.create.call_args_list,
            [
                call(prepared.url, player.volume, attempt=0, start_at=0.0),
                call(refreshed.url, player.volume, attempt=1, start_at=43.0),
            ],
        )
        manager.resolver.stream_for.assert_awaited_once_with(track)
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (next_track,))

    async def test_refresh_oserror_is_retried_without_crashing_current_session(self):
        config = MusicConfig(
            queue_limit=10,
            per_user_limit=5,
            playback_retries=2,
            playback_retry_backoff_seconds=0,
        )
        voice = FakeVoiceClient()
        track = make_track("refresh-network", duration=180)
        next_track = make_track("next")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed = StreamResource(track, "https://cdn.example/recovered")
        player, manager, _ = make_player(config=config, voice=voice)
        manager.resolver.stream_for = AsyncMock(
            side_effect=(OSError("temporary DNS failure"), refreshed)
        )
        manager.audio_factory.create.side_effect = (FakeAudioSource(), FakeAudioSource())
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track, next_track), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice.finish(
            discord.FFmpegProcessError("stream URL expired"),
            played_seconds=60.0,
        )
        await self._wait_for_play_calls(voice, 2)
        voice.finish(played_seconds=122.0)
        await asyncio.wait_for(play_task, timeout=1)

        self.assertEqual(
            manager.resolver.stream_for.await_args_list,
            [call(track), call(track)],
        )
        self.assertEqual(
            manager.audio_factory.create.call_args_list,
            [
                call(prepared.url, player.volume, attempt=0, start_at=0.0),
                call(refreshed.url, player.volume, attempt=2, start_at=58.0),
            ],
        )
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (next_track,))

    async def test_active_voice_disconnect_recovers_then_resumes_current_track(self):
        voice = FakeVoiceClient()
        track = make_track("voice-reconnect", duration=180)
        next_track = make_track("next")
        prepared = StreamResource(track, "https://cdn.example/first")
        refreshed = StreamResource(track, "https://cdn.example/refreshed")
        player, manager, _ = make_player(voice=voice, resource=refreshed)

        async def recover(active_player, abort_event):
            self.assertIs(active_player, player)
            self.assertIs(abort_event, player.play_abort_event)
            voice._connected = True
            return True

        manager.recover_voice.side_effect = recover
        manager.audio_factory.create.side_effect = (FakeAudioSource(), FakeAudioSource())
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track, next_track), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice._connected = False
        voice.finish(played_seconds=40.0)
        await self._wait_for_play_calls(voice, 2)
        voice.finish(played_seconds=142.0)
        await asyncio.wait_for(play_task, timeout=1)

        manager.recover_voice.assert_awaited_once_with(player, player.play_abort_event)
        manager.resolver.stream_for.assert_awaited_once_with(track)
        self.assertEqual(
            manager.audio_factory.create.call_args_list,
            [
                call(prepared.url, player.volume, attempt=0, start_at=0.0),
                call(refreshed.url, player.volume, attempt=1, start_at=38.0),
            ],
        )
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), (next_track,))

    async def test_stop_while_retry_stream_refresh_is_pending_prevents_second_play(self):
        refresh_started = asyncio.Event()
        refresh_cancelled = asyncio.Event()
        voice = FakeVoiceClient()
        track = make_track("stop-retry")
        prepared = StreamResource(track, "https://cdn.example/first")
        player, manager, _ = make_player(voice=voice)

        async def blocked_refresh(requested_track):
            self.assertIs(requested_track, track)
            refresh_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                refresh_cancelled.set()
                raise

        manager.resolver.stream_for = AsyncMock(side_effect=blocked_refresh)
        player.worker_task = SimpleNamespace(done=lambda: False)
        await player.enqueue((track,), 90, prepared)
        taken_track, generation = await player._take_next()

        play_task = asyncio.create_task(player._play_one(taken_track, generation))
        await self._wait_for_play_calls(voice, 1)
        voice.finish(
            discord.FFmpegProcessError("FFmpeg exited with code -11"),
            played_seconds=0.0,
        )
        await asyncio.wait_for(refresh_started.wait(), timeout=1)
        await player.stop()
        await asyncio.wait_for(play_task, timeout=1)

        self.assertTrue(refresh_cancelled.is_set())
        self.assertEqual(voice.play_calls, 1)
        manager.audio_factory.create.assert_called_once_with(
            prepared.url,
            player.volume,
            attempt=0,
            start_at=0.0,
        )
        manager.notify.assert_not_awaited()
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), ())
        idle_task = player.idle_task
        player._cancel_idle()
        if idle_task is not None:
            await asyncio.gather(idle_task, return_exceptions=True)

    async def test_successful_voice_play_transfers_source_cleanup_ownership_to_discord(self):
        voice = FakeVoiceClient()
        track = make_track("owned")
        resource = StreamResource(track, "https://cdn.example/owned")
        source = FakeAudioSource()
        player, manager, _ = make_player(voice=voice, resource=resource)
        manager.audio_factory.create.return_value = source

        play_task = asyncio.create_task(
            player._play_resource_once(
                resource,
                track,
                0,
                player.play_generation,
                player.play_abort_event,
            )
        )
        await self._wait_for_play_calls(voice, 1)
        voice.finish()
        error, _, handed_to_discord = await asyncio.wait_for(play_task, timeout=1)

        self.assertIsNone(error)
        self.assertTrue(handed_to_discord)
        self.assertEqual(source.cleanup_calls, 0)

    async def test_playback_watchdog_forces_cleanup_of_handed_off_hung_source(self):
        voice = FakeVoiceClient()
        track = make_track("hung-source")
        resource = StreamResource(track, "https://cdn.example/hung")
        source = FakeAudioSource()
        player, manager, _ = make_player(voice=voice, resource=resource)
        manager.audio_factory.create.return_value = source

        real_wait_for = asyncio.wait_for

        async def timeout_playback_only(awaitable, *, timeout):
            if timeout >= 300:
                awaitable.cancel()
                raise TimeoutError
            return await real_wait_for(awaitable, timeout=timeout)

        with patch("music.asyncio.wait_for", side_effect=timeout_playback_only):
            error, _, handed_to_discord = await player._play_resource_once(
                resource,
                track,
                playback_attempt=0,
                generation=player.play_generation,
                abort_event=player.play_abort_event,
            )

        self.assertIsInstance(error, TimeoutError)
        self.assertTrue(handed_to_discord)
        self.assertEqual(voice.stop_calls, 1)
        self.assertEqual(source.cleanup_calls, 1)

    async def test_voice_play_exception_cleans_unowned_source_exactly_once(self):
        voice = FakeVoiceClient()
        voice.play = Mock(side_effect=discord.ClientException("voice rejected source"))
        track = make_track("rejected")
        resource = StreamResource(track, "https://cdn.example/rejected")
        source = FakeAudioSource()
        player, manager, _ = make_player(voice=voice, resource=resource)
        manager.audio_factory.create.return_value = source

        error, _, handed_to_discord = await player._play_resource_once(
            resource,
            track,
            0,
            player.play_generation,
            player.play_abort_event,
        )

        self.assertIsInstance(error, discord.ClientException)
        self.assertFalse(handed_to_discord)
        self.assertEqual(source.cleanup_calls, 1)

    async def test_stop_or_skip_wins_race_before_fallback_voice_play_and_cleans_source(self):
        for control_name in ("stop", "skip"):
            with self.subTest(control=control_name):
                voice = FakeVoiceClient()
                track = make_track(f"preplay-{control_name}")
                resource = StreamResource(track, "https://cdn.example/fallback")
                source = FakeAudioSource()
                player, manager, _ = make_player(voice=voice, resource=resource)
                manager.audio_factory.create.return_value = source
                player.current = track
                player.play_generation = 1
                abort_event = player.play_abort_event

                # Queue the control operation ahead of the final pre-play lock.
                # asyncio.Lock is fair, so stop/skip deterministically wins when
                # the gate is released after the fallback source was created.
                await player.lock.acquire()
                control_task = asyncio.create_task(getattr(player, control_name)())
                await asyncio.sleep(0)
                play_task = asyncio.create_task(
                    player._play_resource_once(
                        resource,
                        track,
                        1,
                        player.play_generation,
                        abort_event,
                    )
                )
                await asyncio.sleep(0)
                manager.audio_factory.create.assert_called_once_with(
                    resource.url,
                    player.volume,
                    attempt=1,
                    start_at=0.0,
                )
                player.lock.release()

                await asyncio.wait_for(control_task, timeout=1)
                error, _, handed_to_discord = await asyncio.wait_for(play_task, timeout=1)

                self.assertIsNone(error)
                self.assertFalse(handed_to_discord)
                self.assertEqual(voice.play_calls, 0)
                self.assertEqual(source.cleanup_calls, 1)
                self.assertTrue(abort_event.is_set())

    async def test_audio_thread_after_callback_completes_on_event_loop(self):
        voice = FakeVoiceClient()
        track = make_track("a")
        resource = StreamResource(track=track, url="https://cdn.example/audio")
        player, manager, _ = make_player(voice=voice, resource=resource)
        player.current = track
        player.queue.append(make_track("b"))
        player.play_generation = 1
        task = asyncio.create_task(player._play_one(track, 1))
        await self._wait_until_play_called(voice)
        callback_thread_id = None

        def finish_from_audio_thread():
            nonlocal callback_thread_id
            callback_thread_id = threading.get_ident()
            voice.finish()

        thread = threading.Thread(target=finish_from_audio_thread)
        thread.start()
        thread.join(timeout=1)
        await asyncio.wait_for(task, timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertNotEqual(callback_thread_id, threading.get_ident())
        self.assertEqual(voice.play_calls, 1)
        self.assertIsNone(player.current)
        self.assertEqual(tuple(item.identifier for item in player.queue), ("b",))
        manager.notify.assert_not_awaited()

    async def test_skip_stops_once_and_does_not_double_requeue_from_after_callback(self):
        voice = FakeVoiceClient()
        track = make_track("a")
        resource = StreamResource(track=track, url="https://cdn.example/audio")
        player, _, _ = make_player(voice=voice, resource=resource)
        player.current = track
        player.queue.append(make_track("b"))
        player.play_generation = 1
        player.loop_mode = "track"
        task = asyncio.create_task(player._play_one(track, 1))
        await self._wait_until_play_called(voice)

        text = await player.skip()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(voice.stop_calls, 1)
        self.assertIn("Track a", text)
        self.assertIsNone(player.current)
        self.assertEqual(tuple(item.identifier for item in player.queue), ("b",))
        self.assertEqual(player.finish_action, "normal")

    async def test_stop_clears_queue_and_after_callback_cannot_restore_looped_track(self):
        voice = FakeVoiceClient()
        track = make_track("a")
        resource = StreamResource(track=track, url="https://cdn.example/audio")
        player, _, _ = make_player(voice=voice, resource=resource)
        player.current = track
        player.queue.extend((make_track("b"), make_track("c")))
        player.play_generation = 1
        player.loop_mode = "queue"
        task = asyncio.create_task(player._play_one(track, 1))
        await self._wait_until_play_called(voice)

        text = await player.stop()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(voice.stop_calls, 1)
        self.assertIn("3", text)
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), ())
        self.assertEqual(player.loop_mode, "off")
        idle_task = player.idle_task
        player._cancel_idle()
        if idle_task is not None:
            await asyncio.gather(idle_task, return_exceptions=True)

    async def _make_resolver_blocked_play(self, *, loop_mode):
        started = asyncio.Event()
        cancelled = asyncio.Event()
        track = make_track("resolving")

        async def blocked_stream_for(requested_track):
            self.assertIs(requested_track, track)
            started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        player, manager, _ = make_player(voice=None)
        manager.resolver.stream_for = AsyncMock(side_effect=blocked_stream_for)
        player.current = track
        player.play_generation = 1
        player.loop_mode = loop_mode
        play_task = asyncio.create_task(player._play_one(track, 1))
        await asyncio.wait_for(started.wait(), timeout=1)
        return player, manager, play_task, cancelled

    async def test_skip_aborts_pending_stream_resolve_immediately_without_requeue(self):
        player, manager, play_task, cancelled = await self._make_resolver_blocked_play(
            loop_mode="track"
        )
        player.queue.append(make_track("next"))

        await player.skip()
        await asyncio.wait_for(play_task, timeout=0.5)

        self.assertTrue(cancelled.is_set())
        self.assertIsNone(player.current)
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        self.assertEqual(player.finish_action, "normal")
        manager.audio_factory.create.assert_not_called()
        manager.notify.assert_not_awaited()

    async def test_stop_aborts_pending_stream_resolve_immediately_without_loop_restore(self):
        player, manager, play_task, cancelled = await self._make_resolver_blocked_play(
            loop_mode="queue"
        )
        player.queue.extend((make_track("next-1"), make_track("next-2")))

        text = await player.stop()
        await asyncio.wait_for(play_task, timeout=0.5)

        self.assertTrue(cancelled.is_set())
        self.assertIn("3", text)
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), ())
        self.assertEqual(player.loop_mode, "off")
        manager.audio_factory.create.assert_not_called()
        manager.notify.assert_not_awaited()
        idle_task = player.idle_task
        player._cancel_idle()
        if idle_task is not None:
            await asyncio.gather(idle_task, return_exceptions=True)


class VolumeConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_simultaneous_relative_volume_updates_are_not_lost(self):
        player, manager, _ = make_player()
        player.volume = 50

        results = await asyncio.gather(
            player.adjust_volume(10),
            player.adjust_volume(10),
        )

        self.assertEqual(player.volume, 70)
        self.assertEqual({text.split("**")[1] for text in results}, {"60%", "70%"})
        self.assertEqual(manager.schedule_panel_update.call_count, 2)


def make_voice_channel(channel_id: int):
    channel = Mock(spec=discord.VoiceChannel)
    channel.id = channel_id
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        connect=True,
        speak=True,
    )
    return channel


def make_member(guild, channel, *, member_id=10, manage_guild=False, administrator=False, roles=()):
    member = Mock(spec=discord.Member)
    member.id = member_id
    member.guild = guild
    member.voice = SimpleNamespace(channel=channel)
    member.guild_permissions = SimpleNamespace(
        manage_guild=manage_guild,
        administrator=administrator,
    )
    member.roles = list(roles)
    return member


class VoiceConnectCleanupTests(unittest.IsolatedAsyncioTestCase):
    def make_manager_and_channel(self):
        resolver = SimpleNamespace(close=AsyncMock())
        manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        channel = make_voice_channel(50)
        guild = SimpleNamespace(
            id=77,
            voice_client=None,
            me=SimpleNamespace(id=999),
        )
        return manager, guild, channel

    async def test_cancelled_connect_force_disconnects_registered_dangling_voice(self):
        manager, guild, channel = self.make_manager_and_channel()
        connect_started = asyncio.Event()
        cleanup = Mock()

        async def disconnect(*, force):
            self.assertIs(force, True)
            guild.voice_client = None

        dangling = SimpleNamespace(
            disconnect=AsyncMock(side_effect=disconnect),
            cleanup=cleanup,
        )

        async def connect(**kwargs):
            self.assertEqual(
                kwargs,
                {"timeout": 20, "reconnect": True, "self_deaf": True},
            )
            guild.voice_client = dangling
            connect_started.set()
            await asyncio.Future()

        channel.connect = AsyncMock(side_effect=connect)
        connect_task = asyncio.create_task(manager.ensure_voice(guild, channel))
        await asyncio.wait_for(connect_started.wait(), timeout=1)

        connect_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connect_task

        dangling.disconnect.assert_awaited_once_with(force=True)
        cleanup.assert_not_called()
        self.assertIsNone(guild.voice_client)
        await manager.close()

    async def test_cancelled_connect_uses_cleanup_when_force_disconnect_raises(self):
        manager, guild, channel = self.make_manager_and_channel()
        connect_started = asyncio.Event()

        def cleanup():
            guild.voice_client = None

        dangling = SimpleNamespace(
            disconnect=AsyncMock(side_effect=RuntimeError("disconnect failed")),
            cleanup=Mock(side_effect=cleanup),
        )

        async def connect(**kwargs):
            del kwargs
            guild.voice_client = dangling
            connect_started.set()
            await asyncio.Future()

        channel.connect = AsyncMock(side_effect=connect)
        connect_task = asyncio.create_task(manager.ensure_voice(guild, channel))
        await asyncio.wait_for(connect_started.wait(), timeout=1)

        connect_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await connect_task

        dangling.disconnect.assert_awaited_once_with(force=True)
        dangling.cleanup.assert_called_once_with()
        self.assertIsNone(guild.voice_client)
        await manager.close()


class VoiceStabilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_manager(*, auto_leave=False, grace=0.02, bot=None):
        resolver = SimpleNamespace(close=AsyncMock())
        manager = MusicManager(
            bot or SimpleNamespace(),
            MusicConfig(
                auto_leave=auto_leave,
                idle_seconds=0.01,
                voice_disconnect_grace_seconds=grace,
            ),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        return manager

    def test_stay_mode_is_default_and_env_can_enable_auto_leave(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(MusicConfig.from_env().auto_leave)

        with patch.dict(
            "os.environ",
            {
                "MUSIC_AUTO_LEAVE": "true",
                "MUSIC_VOICE_DISCONNECT_GRACE_SECONDS": "25",
            },
            clear=True,
        ):
            config = MusicConfig.from_env()

        self.assertTrue(config.auto_leave)
        self.assertEqual(config.voice_disconnect_grace_seconds, 25)

    async def test_stay_mode_does_not_schedule_idle_or_empty_channel_leave(self):
        player, manager, _ = make_player(
            config=MusicConfig(auto_leave=False, idle_seconds=0.01)
        )

        player._schedule_idle_if_needed()
        player.schedule_empty_channel_timer()
        await asyncio.sleep(0.03)

        self.assertIsNone(player.idle_task)
        self.assertIsNone(player.empty_channel_task)
        manager.leave.assert_not_awaited()

    async def test_auto_leave_can_still_be_enabled_explicitly(self):
        voice = FakeVoiceClient()
        voice.channel.members = []
        player, manager, _ = make_player(
            config=MusicConfig(auto_leave=True, idle_seconds=0.01),
            voice=voice,
        )

        player.schedule_empty_channel_timer()
        await asyncio.wait_for(player.empty_channel_task, timeout=0.2)

        manager.leave.assert_awaited_once_with(
            player.guild.id,
            reason="Kênh voice không còn người nghe",
        )

    async def test_transient_voice_gap_keeps_player_current_and_queue(self):
        manager = self.make_manager()
        voice = FakeVoiceClient()
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")
        player.queue.append(make_track("next"))
        voice._connected = False

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        await asyncio.sleep(0.005)
        voice._connected = True
        await asyncio.wait_for(check, timeout=0.2)

        self.assertIs(manager.state_for(guild.id), player)
        self.assertEqual(player.current.identifier, "current")
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        await manager.close()

    async def test_reconnect_while_teardown_waits_for_guild_lock_keeps_session(self):
        manager = self.make_manager(grace=0.01)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")
        player.queue.append(make_track("next"))
        guild_lock = manager._guild_lock(guild.id)
        await guild_lock.acquire()

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        await asyncio.sleep(0.03)
        voice._connected = True
        guild_lock.release()
        await asyncio.wait_for(check, timeout=0.2)

        self.assertIs(manager.state_for(guild.id), player)
        self.assertFalse(player.closed)
        self.assertEqual(player.current.identifier, "current")
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        await manager.close()

    async def test_cancelling_pending_disconnect_check_keeps_session_and_queue(self):
        manager = self.make_manager(grace=10)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")
        player.queue.append(make_track("next"))

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        manager.cancel_bot_disconnect_check(guild.id)
        await asyncio.gather(check, return_exceptions=True)

        self.assertTrue(check.cancelled() or check.done())
        self.assertEqual(manager._voice_disconnect_tasks, {})
        self.assertIs(manager.state_for(guild.id), player)
        self.assertFalse(player.closed)
        self.assertEqual(player.current.identifier, "current")
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        await manager.close()

    async def test_bot_reconnect_voice_event_cancels_pending_teardown(self):
        bot = SimpleNamespace(user=SimpleNamespace(id=999))
        manager = self.make_manager(grace=10, bot=bot)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")
        player.queue.append(make_track("next"))
        member = SimpleNamespace(id=bot.user.id, guild=guild)
        channel = SimpleNamespace(id=50, members=[])
        cog = MusicCog(bot, manager)

        await cog.on_voice_state_update(
            member,
            SimpleNamespace(channel=channel),
            SimpleNamespace(channel=None),
        )
        check = manager._voice_disconnect_tasks[guild.id]
        voice._connected = True
        await cog.on_voice_state_update(
            member,
            SimpleNamespace(channel=None),
            SimpleNamespace(channel=channel),
        )
        await asyncio.gather(check, return_exceptions=True)

        self.assertEqual(manager._voice_disconnect_tasks, {})
        self.assertIs(manager.state_for(guild.id), player)
        self.assertFalse(player.closed)
        self.assertEqual(player.current.identifier, "current")
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        await manager.close()

    async def test_manager_shutdown_cancels_pending_disconnect_check_without_resurrection(self):
        manager = self.make_manager(grace=10)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.queue.append(make_track("queued"))

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        await manager.close()

        self.assertTrue(check.cancelled() or check.done())
        self.assertEqual(manager._voice_disconnect_tasks, {})
        self.assertIsNone(manager.state_for(guild.id))
        self.assertTrue(player.closed)
        manager.resolver.close.assert_awaited_once_with()

    async def test_stale_disconnect_check_cannot_close_replacement_player(self):
        manager = self.make_manager()
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        stale_player = manager.get_or_create(guild)
        stale_player.queue.append(make_track("stale"))

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        replacement = GuildPlayer(manager, guild, volume=manager.config.default_volume)
        replacement.queue.append(make_track("replacement"))
        manager.players[guild.id] = replacement
        await asyncio.wait_for(check, timeout=0.2)

        self.assertIs(manager.state_for(guild.id), replacement)
        self.assertFalse(replacement.closed)
        self.assertEqual(
            tuple(track.identifier for track in replacement.queue),
            ("replacement",),
        )
        await stale_player.close(disconnect=False)
        await manager.close()

    async def test_persistent_voice_disconnect_in_stay_mode_keeps_session_for_recovery(self):
        manager = self.make_manager(grace=0.01)
        manager.recover_voice = AsyncMock(return_value=False)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")
        player.queue.append(make_track("next"))

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        for _ in range(100):
            if player.last_error is not None:
                break
            await asyncio.sleep(0.002)

        self.assertIs(manager.state_for(guild.id), player)
        self.assertFalse(player.closed)
        self.assertEqual(player.current.identifier, "current")
        self.assertEqual(tuple(track.identifier for track in player.queue), ("next",))
        self.assertIn("giữ nguyên phiên", player.last_error)
        manager.recover_voice.assert_awaited_once_with(player)
        self.assertFalse(check.done())
        await manager.close()

    async def test_persistent_voice_disconnect_tears_down_when_auto_leave_enabled(self):
        manager = self.make_manager(auto_leave=True)
        voice = FakeVoiceClient()
        voice._connected = False
        guild = SimpleNamespace(id=77, voice_client=voice)
        player = manager.get_or_create(guild)
        player.current = make_track("current")

        manager.schedule_bot_disconnect_check(guild)
        check = manager._voice_disconnect_tasks[guild.id]
        await asyncio.wait_for(check, timeout=0.2)

        self.assertIsNone(manager.state_for(guild.id))
        self.assertTrue(player.closed)
        self.assertEqual(voice.cleanup_calls, 1)
        self.assertEqual(manager._voice_disconnect_tasks, {})
        await manager.close()

    async def test_network_error_while_notifying_does_not_escape_or_clear_session(self):
        channel = SimpleNamespace(
            send=AsyncMock(
                side_effect=[
                    OSError("DNS unavailable"),
                    aiohttp.ServerDisconnectedError(),
                ]
            )
        )
        bot = SimpleNamespace(get_channel=lambda channel_id: channel)
        manager = self.make_manager(bot=bot)
        guild = SimpleNamespace(id=77, voice_client=None)
        player = manager.get_or_create(guild)
        player.text_channel_id = 90
        player.queue.append(make_track("queued"))

        await manager.notify(guild.id, "test")
        await manager.notify(guild.id, "test again")

        self.assertEqual(tuple(track.identifier for track in player.queue), ("queued",))
        self.assertEqual(channel.send.await_count, 2)
        await manager.close()

    async def test_hung_voice_disconnect_is_bounded_and_forces_cache_cleanup(self):
        voice = FakeVoiceClient()
        started = asyncio.Event()

        async def blocked_disconnect(*, force):
            self.assertTrue(force)
            started.set()
            await asyncio.Future()

        voice.disconnect = AsyncMock(side_effect=blocked_disconnect)
        player, _, _ = make_player(voice=voice)

        with patch("music.VOICE_DISCONNECT_TIMEOUT_SECONDS", 0.01):
            await asyncio.wait_for(player.close(disconnect=True), timeout=0.2)

        self.assertTrue(started.is_set())
        voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(voice.cleanup_calls, 1)

    async def test_close_cleans_stale_voice_cache_when_voice_is_already_disconnected(self):
        voice = FakeVoiceClient()
        voice._connected = False
        player, _, _ = make_player(voice=voice)

        await player.close(disconnect=True)

        self.assertEqual(voice.disconnect_calls, 0)
        self.assertEqual(voice.cleanup_calls, 1)

    async def test_disconnect_transport_error_still_forces_voice_cache_cleanup(self):
        voice = FakeVoiceClient()
        voice.disconnect = AsyncMock(side_effect=OSError("voice network unavailable"))
        player, _, _ = make_player(voice=voice)

        await player.close(disconnect=True)

        voice.disconnect.assert_awaited_once_with(force=True)
        self.assertEqual(voice.cleanup_calls, 1)


class MusicAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.voice_channel = make_voice_channel(50)
        self.voice = FakeVoiceClient(channel_id=50)
        self.guild = SimpleNamespace(id=1, voice_client=self.voice, me=SimpleNamespace(id=999))
        self.resolver = SimpleNamespace(close=AsyncMock())
        self.manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(dj_role_id=777),
            resolver=self.resolver,
            audio_factory=SimpleNamespace(),
        )
        self.player = self.manager.get_or_create(self.guild)

    def test_regular_member_can_control_only_from_same_voice(self):
        member = make_member(self.guild, self.voice_channel)

        self.assertIs(self.manager.authorize_control(member), self.player)

        member.voice = SimpleNamespace(channel=make_voice_channel(51))
        with self.assertRaisesRegex(MusicError, "cùng kênh voice"):
            self.manager.authorize_control(member)

    def test_destructive_controls_require_dj_or_manage_server(self):
        regular = make_member(self.guild, self.voice_channel)
        with self.assertRaisesRegex(MusicError, "Manage Server"):
            self.manager.authorize_control(regular, destructive=True)

        dj = make_member(
            self.guild,
            self.voice_channel,
            roles=(SimpleNamespace(id=777),),
        )
        manager = make_member(self.guild, self.voice_channel, manage_guild=True)
        administrator = make_member(self.guild, self.voice_channel, administrator=True)

        self.assertIs(self.manager.authorize_control(dj, destructive=True), self.player)
        self.assertIs(self.manager.authorize_control(manager, destructive=True), self.player)
        self.assertIs(
            self.manager.authorize_control(administrator, destructive=True),
            self.player,
        )

    def test_add_from_current_panel_can_start_before_bot_connects(self):
        disconnected_guild = SimpleNamespace(
            id=2,
            voice_client=None,
            me=SimpleNamespace(id=999),
        )
        member = make_member(disconnected_guild, self.voice_channel)
        player = self.manager.get_or_create(disconnected_guild)
        player.panel_message = SimpleNamespace(id=500)

        self.assertIs(self.manager.authorize_add_from_panel(member), player)
        with self.assertRaisesRegex(MusicError, "cùng kênh voice"):
            self.manager.authorize_control(member)


class AddQueryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def make_blocked_manager(self):
        started = asyncio.Event()
        release = asyncio.Event()
        track = make_track("resolved")

        async def resolve(query, requester_id):
            del query, requester_id
            started.set()
            await release.wait()
            return ResolvedBatch((track,))

        resolver = SimpleNamespace(
            resolve=AsyncMock(side_effect=resolve),
            close=AsyncMock(),
        )
        voice_channel = make_voice_channel(50)
        voice_channel.connect = AsyncMock()
        guild = SimpleNamespace(
            id=77,
            voice_client=None,
            me=SimpleNamespace(id=999),
        )
        member = make_member(guild, voice_channel)
        manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        return manager, resolver, guild, member, started, release

    async def test_resolve_started_before_leave_cannot_connect_or_recreate_player(self):
        manager, _, guild, member, started, release = self.make_blocked_manager()
        text_channel = SimpleNamespace(id=90)
        ensure_started = asyncio.Event()

        async def connect_early(*args):
            del args
            ensure_started.set()
            return sentinel.voice

        ensure_voice = AsyncMock(side_effect=connect_early)

        with (
            patch.object(manager, "ensure_voice", new=ensure_voice),
            patch.object(GuildPlayer, "enqueue", new_callable=AsyncMock) as enqueue,
        ):
            add_task = asyncio.create_task(
                manager.add_query(guild, member, text_channel, "delayed search")
            )
            await asyncio.wait_for(
                asyncio.gather(started.wait(), ensure_started.wait()),
                timeout=1,
            )
            await manager.leave(guild.id, reason="regression test")
            release.set()
            with self.assertRaisesRegex(MusicError, "Phiên nhạc đã kết thúc"):
                await asyncio.wait_for(add_task, timeout=1)

        ensure_voice.assert_awaited_once()
        enqueue.assert_not_awaited()
        self.assertIsNone(manager.state_for(guild.id))
        await manager.close()

    async def test_resolve_started_before_manager_close_cannot_connect_or_recreate_player(self):
        manager, resolver, guild, member, started, release = self.make_blocked_manager()
        text_channel = SimpleNamespace(id=90)
        ensure_started = asyncio.Event()

        async def connect_early(*args):
            del args
            ensure_started.set()
            return sentinel.voice

        ensure_voice = AsyncMock(side_effect=connect_early)

        with (
            patch.object(manager, "ensure_voice", new=ensure_voice),
            patch.object(GuildPlayer, "enqueue", new_callable=AsyncMock) as enqueue,
        ):
            add_task = asyncio.create_task(
                manager.add_query(guild, member, text_channel, "delayed search")
            )
            await asyncio.wait_for(
                asyncio.gather(started.wait(), ensure_started.wait()),
                timeout=1,
            )
            await manager.close()
            release.set()
            with self.assertRaisesRegex(MusicError, "Tính năng nhạc đang tắt"):
                await asyncio.wait_for(add_task, timeout=1)

        ensure_voice.assert_awaited_once()
        enqueue.assert_not_awaited()
        self.assertIsNone(manager.state_for(guild.id))
        resolver.close.assert_awaited_once_with()

    async def test_same_member_cannot_start_second_request_while_first_is_inflight(self):
        manager, resolver, guild, member, started, release = self.make_blocked_manager()
        text_channel = SimpleNamespace(id=90)
        first_task = asyncio.create_task(
            manager.add_query(guild, member, text_channel, "first delayed search")
        )
        await asyncio.wait_for(started.wait(), timeout=1)

        with self.assertRaisesRegex(MusicError, "đã có một yêu cầu nhạc đang được xử lý"):
            await manager.add_query(guild, member, text_channel, "second search")

        self.assertEqual(resolver.resolve.await_count, 1)
        await manager.leave(guild.id, reason="finish inflight test")
        release.set()
        with self.assertRaisesRegex(MusicError, "Phiên nhạc đã kết thúc"):
            await asyncio.wait_for(first_task, timeout=1)
        self.assertNotIn((guild.id, member.id), manager._inflight_users)
        self.assertNotIn(guild.id, manager._inflight_by_guild)
        await manager.close()

    async def test_resolve_and_voice_connect_start_concurrently_and_enqueue_prepared_stream(self):
        resolve_started = asyncio.Event()
        connect_started = asyncio.Event()
        release_resolve = asyncio.Event()
        release_connect = asyncio.Event()
        track = make_track("parallel-fast")
        prepared = StreamResource(track, "https://cdn.example/parallel-fast")
        batch = ResolvedBatch((track,), prepared_stream=prepared)

        async def resolve(query, requester_id):
            del query, requester_id
            resolve_started.set()
            await release_resolve.wait()
            return batch

        connect_calls = 0

        async def ensure_voice(guild, channel):
            nonlocal connect_calls
            del guild, channel
            connect_calls += 1
            if connect_calls == 1:
                connect_started.set()
                await release_connect.wait()
            return sentinel.voice

        resolver = SimpleNamespace(
            resolve=AsyncMock(side_effect=resolve),
            close=AsyncMock(),
        )
        voice_channel = make_voice_channel(50)
        guild = SimpleNamespace(id=88, voice_client=None, me=SimpleNamespace(id=999))
        member = make_member(guild, voice_channel)
        manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        text_channel = SimpleNamespace(id=90)
        enqueue_result = QueueAddResult((track,), first_position=1, skipped=0)

        with (
            patch.object(manager, "ensure_voice", new=AsyncMock(side_effect=ensure_voice)) as connect,
            patch.object(
                GuildPlayer,
                "enqueue",
                new=AsyncMock(return_value=enqueue_result),
            ) as enqueue,
        ):
            add_task = asyncio.create_task(
                manager.add_query(guild, member, text_channel, "parallel search")
            )

            await asyncio.wait_for(
                asyncio.gather(resolve_started.wait(), connect_started.wait()),
                timeout=0.5,
            )
            self.assertFalse(add_task.done())
            release_resolve.set()
            release_connect.set()
            result = await asyncio.wait_for(add_task, timeout=1)

        self.assertEqual(result.tracks, (track,))
        self.assertEqual(connect.await_count, 2)
        enqueue.assert_awaited_once_with((track,), text_channel.id, prepared)
        self.assertNotIn((guild.id, member.id), manager._inflight_users)
        await manager.close()

    async def test_stop_during_pending_add_invalidates_request_before_enqueue_or_autoplay(self):
        manager, _, guild, member, resolve_started, release_resolve = self.make_blocked_manager()
        player = manager.get_or_create(guild)
        connect_started = asyncio.Event()

        async def connect_early(*args):
            del args
            connect_started.set()
            return sentinel.voice

        ensure_voice = AsyncMock(side_effect=connect_early)
        text_channel = SimpleNamespace(id=90)
        epoch_before = manager._guild_epoch(guild.id)

        with (
            patch.object(manager, "ensure_voice", new=ensure_voice),
            patch.object(GuildPlayer, "enqueue", new_callable=AsyncMock) as enqueue,
        ):
            add_task = asyncio.create_task(
                manager.add_query(guild, member, text_channel, "pending before stop")
            )
            await asyncio.wait_for(
                asyncio.gather(resolve_started.wait(), connect_started.wait()),
                timeout=1,
            )

            stop_text = await manager.stop_player(player)
            self.assertEqual(manager._guild_epoch(guild.id), epoch_before + 1)
            release_resolve.set()
            with self.assertRaisesRegex(MusicError, "Phiên nhạc đã kết thúc"):
                await asyncio.wait_for(add_task, timeout=1)

        self.assertIn("Đã dừng", stop_text)
        ensure_voice.assert_awaited_once()
        enqueue.assert_not_awaited()
        self.assertIs(manager.state_for(guild.id), player)
        self.assertIsNone(player.current)
        self.assertEqual(tuple(player.queue), ())
        self.assertEqual(player.prepared_streams, {})
        self.assertFalse(player.queue_event.is_set())
        self.assertNotIn((guild.id, member.id), manager._inflight_users)
        await manager.close()


class MusicPanelCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_context(self, guild, member):
        return SimpleNamespace(
            guild=guild,
            author=member,
            channel=SimpleNamespace(id=90),
            interaction=None,
            reply=AsyncMock(),
        )

    async def test_music_command_reuses_panel_and_requires_voice_access(self):
        guild = SimpleNamespace(id=77)
        member = Mock(spec=discord.Member)
        member.id = 10
        panel = SimpleNamespace(jump_url="https://discord.com/channels/1/2/3")
        manager = SimpleNamespace(
            validate_voice_access=Mock(),
            publish_panel=AsyncMock(return_value=panel),
        )
        cog = MusicCog(SimpleNamespace(), manager)
        ctx = self.make_context(guild, member)

        await MusicCog.music_panel.callback(cog, ctx)

        manager.validate_voice_access.assert_called_once_with(guild, member)
        manager.publish_panel.assert_awaited_once_with(guild, ctx.channel)
        reply_text = ctx.reply.await_args.args[0]
        self.assertIn(panel.jump_url, reply_text)

        manager.validate_voice_access.reset_mock()
        manager.validate_voice_access.side_effect = MusicError("Bạn phải vào một kênh voice trước.")
        manager.publish_panel.reset_mock()
        no_voice_ctx = self.make_context(guild, member)

        await MusicCog.music_panel.callback(cog, no_voice_ctx)

        manager.publish_panel.assert_not_awaited()
        self.assertIn("Bạn phải vào một kênh voice", no_voice_ctx.reply.await_args.args[0])

    def test_music_command_has_member_cooldown(self):
        buckets = MusicCog.music_panel._buckets

        self.assertEqual(buckets._cooldown.rate, 1)
        self.assertEqual(buckets._cooldown.per, 10.0)
        self.assertIs(buckets._type, commands.BucketType.member)

    async def test_publish_panel_edits_same_channel_message_instead_of_sending_another(self):
        resolver = SimpleNamespace(close=AsyncMock())
        manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        guild = SimpleNamespace(id=77, voice_client=None)
        player = manager.get_or_create(guild)
        existing = SimpleNamespace(
            id=500,
            channel=SimpleNamespace(id=90),
            edit=AsyncMock(),
        )
        player.panel_message = existing
        channel = SimpleNamespace(id=90, send=AsyncMock())

        result = await manager.publish_panel(guild, channel)

        self.assertIs(result, existing)
        existing.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        await manager.close()

    async def test_panel_debounce_runs_trailing_update_when_state_changes_during_edit(self):
        resolver = SimpleNamespace(close=AsyncMock())
        manager = MusicManager(
            SimpleNamespace(),
            MusicConfig(),
            resolver=resolver,
            audio_factory=SimpleNamespace(),
        )
        guild = SimpleNamespace(id=77, voice_client=None)
        player = manager.get_or_create(guild)
        player.panel_message = SimpleNamespace(id=500)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        trailing_finished = asyncio.Event()
        calls = 0

        async def update_panel(guild_id):
            nonlocal calls
            self.assertEqual(guild_id, guild.id)
            calls += 1
            if calls == 1:
                first_started.set()
                await release_first.wait()
            elif calls == 2:
                trailing_finished.set()

        with patch.object(
            manager,
            "update_panel",
            new=AsyncMock(side_effect=update_panel),
        ) as update:
            manager.schedule_panel_update(guild.id)
            debounce_task = player.panel_update_task
            await asyncio.wait_for(first_started.wait(), timeout=1)

            manager.schedule_panel_update(guild.id)
            release_first.set()
            await asyncio.wait_for(trailing_finished.wait(), timeout=1)
            await asyncio.wait_for(debounce_task, timeout=1)

        self.assertEqual(update.await_count, 2)
        self.assertFalse(player.panel_dirty)
        self.assertIsNone(player.panel_update_task)
        await manager.close()


class StopRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_panel_stop_routes_through_manager_lifecycle_method(self):
        player = SimpleNamespace(stop=AsyncMock())
        manager = SimpleNamespace(
            stop_player=AsyncMock(return_value="⏹️ stopped safely"),
        )
        view = MusicControlView(manager)
        interaction = SimpleNamespace(
            guild_id=77,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with patch.object(
            MusicControlView,
            "_guard",
            new=AsyncMock(return_value=player),
        ):
            await view._run_player_action(
                interaction,
                "stop",
                destructive=True,
            )

        manager.stop_player.assert_awaited_once_with(player)
        player.stop.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)

    async def test_prefix_stop_routes_through_manager_lifecycle_method(self):
        guild = SimpleNamespace(id=77)
        member = Mock(spec=discord.Member)
        member.id = 10
        player = SimpleNamespace(stop=AsyncMock())
        manager = SimpleNamespace(
            authorize_control=Mock(return_value=player),
            stop_player=AsyncMock(return_value="⏹️ stopped safely"),
            publish_panel=AsyncMock(),
        )
        cog = MusicCog(SimpleNamespace(), manager)
        ctx = SimpleNamespace(
            guild=guild,
            author=member,
            channel=SimpleNamespace(id=90),
            interaction=None,
            reply=AsyncMock(),
        )

        await cog._control(ctx, "stop", destructive=True)

        manager.authorize_control.assert_called_once_with(member, destructive=True)
        manager.stop_player.assert_awaited_once_with(player)
        player.stop.assert_not_awaited()
        manager.publish_panel.assert_awaited_once_with(guild, ctx.channel)


class PanelRenderingTests(unittest.TestCase):
    def test_panel_escapes_titles_urls_and_truncates_queue_and_errors(self):
        current = make_track(
            "current",
            title="@everyone **bold** [song]",
            duration=65,
        )
        current = MusicTrack(
            identifier=current.identifier,
            title=current.title,
            webpage_url="https://youtube.com/watch?v=bad)value",
            source_url=current.source_url,
            requester_id=current.requester_id,
            duration=current.duration,
            uploader="**Uploader**",
        )
        queued = tuple(
            make_track(
                str(index),
                title=("@here _queue_ " + ("x" * 100)) if index == 1 else f"Queue {index}",
            )
            for index in range(1, 9)
        )
        snapshot = MusicSnapshot(
            guild_id=1,
            current=current,
            queued=queued,
            paused=False,
            playing=True,
            loading=False,
            loop_mode="queue",
            volume=80,
            voice_channel_id=50,
            last_error="*" * 1000,
        )

        embed = render_music_panel(snapshot)

        self.assertEqual(embed.author.name, "Author • DiskiiVN")
        self.assertIn("by DiskiiVN", embed.footer.text)
        self.assertNotIn("@everyone", embed.description)
        self.assertIn("@\u200beveryone", embed.description)
        self.assertIn(r"\*\*bold\*\*", embed.description)
        self.assertIn("bad%29value", embed.description)
        queue_field = embed.fields[0]
        self.assertIn("8 bài", queue_field.name)
        self.assertIn("2** bài khác", queue_field.value)
        self.assertNotIn("Queue 7", queue_field.value)
        self.assertNotIn("@here", queue_field.value)
        self.assertIn("@\u200bhere", queue_field.value)
        error_field = next(field for field in embed.fields if field.name == "⚠️ Lỗi gần nhất")
        self.assertEqual(len(error_field.value), 900)


class AudioSourceFactoryTests(unittest.TestCase):
    @staticmethod
    def unprobed_factory(config: MusicConfig, *executables: str) -> AudioSourceFactory:
        factory = object.__new__(AudioSourceFactory)
        factory.config = config
        factory.available = True
        factory.executables = tuple(executables)
        factory.executable = factory.executables[0]
        return factory

    def test_before_options_use_portable_baseline_and_conservative_retry(self):
        options = AudioSourceFactory.BEFORE_OPTIONS

        for unsupported in (
            "reconnect_delay_total_max",
            "reconnect_on_network_error",
            "reconnect_on_http_error",
        ):
            with self.subTest(unsupported=unsupported):
                self.assertNotIn(unsupported, options)
        for required in (
            "-nostdin",
            "-rw_timeout 45000000",
            "-reconnect 1",
            "-reconnect_streamed 1",
            "-reconnect_delay_max 5",
        ):
            with self.subTest(required=required):
                self.assertIn(required, options)
        self.assertEqual(
            AudioSourceFactory.SAFE_BEFORE_OPTIONS,
            "-nostdin -rw_timeout 45000000",
        )

    def test_executable_priority_is_configured_then_system_then_bundled(self):
        completed = SimpleNamespace(returncode=0)
        with (
            patch("music.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("music.imageio_ffmpeg.get_ffmpeg_exe", return_value="/wheel/ffmpeg"),
            patch("music.subprocess.run", return_value=completed) as run,
            patch("music.discord.opus.is_loaded", return_value=True),
        ):
            factory = AudioSourceFactory(MusicConfig(ffmpeg_path="/custom/ffmpeg"))

        self.assertTrue(factory.available)
        self.assertEqual(
            factory.executables,
            ("/custom/ffmpeg", "/usr/bin/ffmpeg", "/wheel/ffmpeg"),
        )
        self.assertEqual(factory.executable, "/custom/ffmpeg")
        self.assertEqual(factory.attempt_count, 2)
        self.assertEqual(
            run.call_args_list,
            [
                call(
                    ["/custom/ffmpeg", "-version"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                ),
                call(
                    ["/usr/bin/ffmpeg", "-version"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                ),
                call(
                    ["/wheel/ffmpeg", "-version"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                ),
            ],
        )

    def test_executable_candidates_are_deduplicated_by_resolved_path(self):
        def resolved(candidate):
            if candidate in {"/custom/ffmpeg", "/usr/bin/ffmpeg"}:
                return "/same/ffmpeg"
            return candidate

        with (
            patch("music.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("music.imageio_ffmpeg.get_ffmpeg_exe", return_value="/wheel/ffmpeg"),
            patch("music.os.path.realpath", side_effect=resolved),
            patch("music.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
            patch("music.discord.opus.is_loaded", return_value=True),
        ):
            factory = AudioSourceFactory(MusicConfig(ffmpeg_path="/custom/ffmpeg"))

        self.assertEqual(factory.executables, ("/custom/ffmpeg", "/wheel/ffmpeg"))
        self.assertEqual(run.call_count, 2)

    def test_candidate_probe_discards_crashing_and_unlaunchable_executables(self):
        with (
            patch("music.shutil.which", return_value="/usr/bin/ffmpeg"),
            patch("music.imageio_ffmpeg.get_ffmpeg_exe", return_value="/wheel/ffmpeg"),
            patch(
                "music.subprocess.run",
                side_effect=(
                    SimpleNamespace(returncode=-11),
                    OSError("cannot execute"),
                    SimpleNamespace(returncode=0),
                ),
            ) as run,
            patch("music.discord.opus.is_loaded", return_value=True),
        ):
            factory = AudioSourceFactory(MusicConfig(ffmpeg_path="/custom/crashing"))

        self.assertTrue(factory.available)
        self.assertEqual(factory.executables, ("/wheel/ffmpeg",))
        self.assertEqual(factory.executable, "/wheel/ffmpeg")
        self.assertEqual(factory.attempt_count, 1)
        self.assertEqual(run.call_count, 3)

    def test_pcm_transformer_exposes_inner_ffmpeg_process_error(self):
        class InnerPCM(discord.AudioSource):
            def __init__(self, error):
                self._current_error = error

            def read(self):
                return b""

            def is_opus(self):
                return False

        expected = discord.FFmpegProcessError("FFmpeg exited with code -11")
        source = ErrorAwarePCMVolumeTransformer(InnerPCM(expected), volume=0.8)

        self.assertIs(source._current_error, expected)

    def test_falls_back_to_ffmpeg_opus_without_system_opus(self):
        config = MusicConfig(
            ffmpeg_path="ffmpeg-test",
            bitrate_kbps=160,
        )
        factory = self.unprobed_factory(config, "ffmpeg-test", "ffmpeg-safe")

        with (
            patch("music.discord.opus.is_loaded", return_value=False),
            patch("music.BoundedFFmpegOpusAudio", return_value=sentinel.opus) as opus,
            patch("music.BoundedFFmpegPCMAudio") as pcm,
            patch("music.ErrorAwarePCMVolumeTransformer") as transformer,
        ):
            source = factory.create(
                "https://cdn.example/audio",
                80,
                attempt=0,
                start_at=0.0,
            )

        self.assertIs(source, sentinel.opus)
        pcm.assert_not_called()
        transformer.assert_not_called()
        opus.assert_called_once()
        args, kwargs = opus.call_args
        self.assertEqual(args, ("https://cdn.example/audio",))
        self.assertEqual(kwargs["executable"], "ffmpeg-test")
        self.assertEqual(kwargs["bitrate"], 160)
        self.assertEqual(kwargs["before_options"], AudioSourceFactory.BEFORE_OPTIONS)
        self.assertIn("volume=0.80", kwargs["options"])
        self.assertNotIn("stderr", kwargs)

    def test_pcm_source_does_not_pass_integer_devnull_as_stderr(self):
        config = MusicConfig(ffmpeg_path="ffmpeg-test")
        factory = self.unprobed_factory(config, "ffmpeg-test", "ffmpeg-safe")

        with (
            patch("music.discord.opus.is_loaded", return_value=True),
            patch("music.BoundedFFmpegPCMAudio", return_value=sentinel.pcm) as pcm,
            patch(
                "music.ErrorAwarePCMVolumeTransformer",
                return_value=sentinel.transformer,
            ) as transformer,
            patch("music.BoundedFFmpegOpusAudio") as opus,
        ):
            source = factory.create(
                "https://cdn.example/audio",
                80,
                attempt=0,
                start_at=0.0,
            )

        self.assertIs(source, sentinel.transformer)
        opus.assert_not_called()
        pcm.assert_called_once()
        args, kwargs = pcm.call_args
        self.assertEqual(args, ("https://cdn.example/audio",))
        self.assertEqual(kwargs["executable"], "ffmpeg-test")
        self.assertEqual(kwargs["before_options"], AudioSourceFactory.BEFORE_OPTIONS)
        self.assertNotIn("stderr", kwargs)
        transformer.assert_called_once_with(sentinel.pcm, volume=0.8)

    def test_retry_attempt_uses_second_executable_and_safe_flags(self):
        config = MusicConfig(ffmpeg_path="ffmpeg-primary", bitrate_kbps=160)
        factory = self.unprobed_factory(config, "ffmpeg-primary", "ffmpeg-fallback")

        with (
            patch("music.discord.opus.is_loaded", return_value=False),
            patch("music.BoundedFFmpegOpusAudio", return_value=sentinel.opus) as opus,
        ):
            source = factory.create(
                "https://cdn.example/audio",
                80,
                attempt=1,
                start_at=58.25,
            )

        self.assertIs(source, sentinel.opus)
        _, kwargs = opus.call_args
        self.assertEqual(kwargs["executable"], "ffmpeg-fallback")
        self.assertEqual(
            kwargs["before_options"],
            f"{AudioSourceFactory.SAFE_BEFORE_OPTIONS} -ss 58.250",
        )


if __name__ == "__main__":
    unittest.main()
