import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from ai_support import AIAnswer
from bot import (
    CraftopiaBot,
    STATUS_FAILURE_THRESHOLD,
    STATUS_RECOVERY_THRESHOLD,
    _cleanup_authorization_error,
    channel_has_ai_auto_reply,
)
from mc_status import EndpointStatus, MinecraftStatus


def make_status(online: bool) -> MinecraftStatus:
    java = EndpointStatus("Java", "example.test", 25565, online, latency_ms=10.0)
    bedrock = EndpointStatus("Bedrock/PE", "example.test", 19132, True, latency_ms=10.0)
    return MinecraftStatus(java=java, bedrock=bedrock)


class StatusTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CraftopiaBot()
        self.alerts: list[str] = []

        async def capture_alert(content: str) -> bool:
            self.alerts.append(content)
            return True

        self.bot.send_status_alert = capture_alert

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_requires_consecutive_failures_and_successes(self):
        for _ in range(STATUS_FAILURE_THRESHOLD - 1):
            await self.bot.process_status_change(make_status(False))
        self.assertEqual(self.alerts, [])

        await self.bot.process_status_change(make_status(False))
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("không phản hồi", self.alerts[0])

        for _ in range(STATUS_RECOVERY_THRESHOLD - 1):
            await self.bot.process_status_change(make_status(True))
        self.assertEqual(len(self.alerts), 1)

        await self.bot.process_status_change(make_status(True))
        self.assertEqual(len(self.alerts), 2)
        self.assertIn("hoạt động lại", self.alerts[1])

    async def test_full_outage_is_combined_into_one_alert(self):
        offline = MinecraftStatus(
            EndpointStatus("Java", "example.test", 25565, False, error_kind="network"),
            EndpointStatus("Bedrock/PE", "example.test", 19132, False, error_kind="network"),
        )
        for _ in range(STATUS_FAILURE_THRESHOLD):
            await self.bot.process_status_change(offline)

        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Java và Bedrock/PE", self.alerts[0])

    async def test_protocol_error_is_not_reported_as_offline(self):
        malformed = MinecraftStatus(
            EndpointStatus(
                "Java", "example.test", 25565, False,
                error="invalid JSON", error_kind="protocol",
            ),
            EndpointStatus("Bedrock/PE", "example.test", 19132, True, latency_ms=10.0),
        )
        for _ in range(STATUS_FAILURE_THRESHOLD + 1):
            await self.bot.process_status_change(malformed)

        self.assertEqual(self.alerts, [])

    async def test_failed_delivery_is_retried(self):
        attempts = 0

        async def flaky_alert(content: str) -> bool:
            nonlocal attempts
            attempts += 1
            return attempts > 1

        self.bot.send_status_alert = flaky_alert
        for _ in range(STATUS_FAILURE_THRESHOLD + 1):
            await self.bot.process_status_change(make_status(False))

        self.assertEqual(attempts, 2)
        self.assertTrue(self.bot.health["java"]["alerted"])

    async def test_manual_status_requests_share_short_cache(self):
        status = make_status(True)
        query = AsyncMock(return_value=status)
        with patch("bot.query_minecraft_status", query):
            first = await self.bot.refresh_mc_status(notify=False)
            second = await self.bot.refresh_mc_status(notify=False)

        self.assertIs(first, status)
        self.assertIs(second, status)
        query.assert_awaited_once()


class AIImageMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CraftopiaBot()
        self.bot.ai.answer = AsyncMock(return_value=AIAnswer("ok", ()))

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_total_image_budget_applies_across_all_attachments(self):
        first = SimpleNamespace(
            content_type="image/png",
            size=6,
            read=AsyncMock(return_value=b"123456"),
        )
        second = SimpleNamespace(
            content_type="image/jpeg",
            size=6,
            read=AsyncMock(return_value=b"abcdef"),
        )

        with patch("bot.MAX_IMAGE_BYTES", 10):
            result = await self.bot.create_ai_answer(1, 2, 3, "xem lỗi", [first, second])

        self.assertEqual(result.text, "ok")
        first.read.assert_awaited_once_with()
        second.read.assert_not_awaited()
        images = self.bot.ai.answer.await_args.args[3]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].data, b"123456")

    async def test_attachment_is_not_loaded_until_ai_slot_is_available(self):
        attachment = SimpleNamespace(
            content_type="image/png",
            size=4,
            read=AsyncMock(return_value=b"data"),
        )
        self.bot.ai_slots = asyncio.Semaphore(0)

        task = asyncio.create_task(
            self.bot.create_ai_answer(1, 2, 3, "xem lỗi", [attachment])
        )
        await asyncio.sleep(0.01)
        attachment.read.assert_not_awaited()
        self.bot.ai_slots.release()
        result = await asyncio.wait_for(task, timeout=0.2)

        self.assertEqual(result.text, "ok")
        attachment.read.assert_awaited_once_with()


class AutoReplyChannelTests(unittest.TestCase):
    def test_direct_opt_in_channel_is_enabled(self):
        channel = SimpleNamespace(id=10, parent_id=999, is_nsfw=lambda: False)
        with patch("bot.AI_AUTO_REPLY_CHANNEL_IDS", (10,)):
            self.assertTrue(channel_has_ai_auto_reply(channel))

    def test_category_parent_does_not_enable_text_channel(self):
        channel = SimpleNamespace(id=10, parent_id=999, is_nsfw=lambda: False)
        with patch("bot.AI_AUTO_REPLY_CHANNEL_IDS", (999,)):
            self.assertFalse(channel_has_ai_auto_reply(channel))

    def test_public_thread_inherits_opt_in_parent(self):
        channel = Mock(spec=discord.Thread)
        channel.id = 11
        channel.parent_id = 10
        channel.type = discord.ChannelType.public_thread
        channel.is_nsfw.return_value = False
        with patch("bot.AI_AUTO_REPLY_CHANNEL_IDS", (10,)):
            self.assertTrue(channel_has_ai_auto_reply(channel))

    def test_private_and_nsfw_channels_are_disabled(self):
        private_thread = Mock(spec=discord.Thread)
        private_thread.id = 11
        private_thread.parent_id = 10
        private_thread.type = discord.ChannelType.private_thread
        private_thread.is_nsfw.return_value = False
        nsfw_channel = SimpleNamespace(id=10, is_nsfw=lambda: True)
        with patch("bot.AI_AUTO_REPLY_CHANNEL_IDS", (10, 11)):
            self.assertFalse(channel_has_ai_auto_reply(private_thread))
            self.assertFalse(channel_has_ai_auto_reply(nsfw_channel))


class DiscordGuildStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CraftopiaBot()

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_default_mode_uses_discord_aggregate_counts(self):
        guild = SimpleNamespace(id=123, member_count=118)
        self.bot.fetch_guild = AsyncMock(return_value=SimpleNamespace(
            approximate_member_count=120,
            approximate_presence_count=35,
        ))
        with patch("bot.TRACK_DISCORD_PRESENCE", False):
            stats = await self.bot.get_discord_guild_stats(guild)

        self.assertEqual(stats.members, 120)
        self.assertEqual(stats.online, 35)
        self.assertTrue(stats.approximate)
        self.bot.fetch_guild.assert_awaited_once_with(123, with_counts=True)

    async def test_presence_mode_counts_non_offline_cached_members(self):
        guild = SimpleNamespace(
            id=123,
            member_count=4,
            chunked=True,
            members=[
                SimpleNamespace(status=discord.Status.online),
                SimpleNamespace(status=discord.Status.idle),
                SimpleNamespace(status=discord.Status.dnd),
                SimpleNamespace(status=discord.Status.offline),
            ],
        )
        with patch("bot.TRACK_DISCORD_PRESENCE", True):
            stats = await self.bot.get_discord_guild_stats(guild)

        self.assertEqual(stats.members, 4)
        self.assertEqual(stats.online, 3)
        self.assertFalse(stats.approximate)


class DashboardCommitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = CraftopiaBot()

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_commit_updates_memory_only_after_persistence_succeeds(self):
        target = {"guild_id": 1, "channel_id": 2, "message_id": 3}
        with patch("bot._write_stats_target") as write_target:
            result = await self.bot._commit_stats_dashboard_target(target, None)

        self.assertTrue(result)
        self.assertEqual(self.bot.stats_dashboard_target, target)
        write_target.assert_called_once()

    async def test_persistence_failure_rolls_back_new_message(self):
        original = dict(self.bot.stats_dashboard_target)
        created_message = SimpleNamespace(delete=AsyncMock())
        target = {"guild_id": 1, "channel_id": 2, "message_id": 3}
        with patch("bot._write_stats_target", side_effect=OSError("disk full")):
            result = await self.bot._commit_stats_dashboard_target(target, created_message)

        self.assertFalse(result)
        self.assertEqual(self.bot.stats_dashboard_target, original)
        created_message.delete.assert_awaited_once()


class CleanupAuthorizationTests(unittest.TestCase):
    @staticmethod
    def make_context(*, actor_admin=False, target_admin=False, actor_is_owner=False):
        actor_id = 1 if actor_is_owner else 2
        actor = SimpleNamespace(
            id=actor_id,
            guild_permissions=SimpleNamespace(administrator=actor_admin),
        )
        target = SimpleNamespace(
            id=3,
            guild_permissions=SimpleNamespace(administrator=target_admin),
        )
        bot_member = SimpleNamespace(id=99)
        guild = SimpleNamespace(owner_id=1, me=bot_member)
        allowed = SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            manage_messages=True,
        )
        channel = SimpleNamespace(permissions_for=lambda member: allowed)
        return guild, channel, actor, target

    def test_current_channel_moderator_is_allowed(self):
        guild, channel, actor, target = self.make_context()
        self.assertIsNone(
            _cleanup_authorization_error(guild, channel, actor, target, "channel")
        )

    def test_whole_server_requires_administrator(self):
        guild, channel, actor, target = self.make_context()
        error = _cleanup_authorization_error(guild, channel, actor, target, "guild")
        self.assertIn("Administrator", error)

    def test_only_owner_can_target_an_administrator(self):
        guild, channel, actor, target = self.make_context(actor_admin=True, target_admin=True)
        self.assertIn(
            "chủ server",
            _cleanup_authorization_error(guild, channel, actor, target, "guild"),
        )

        guild, channel, owner, target = self.make_context(
            actor_admin=True,
            target_admin=True,
            actor_is_owner=True,
        )
        self.assertIsNone(
            _cleanup_authorization_error(guild, channel, owner, target, "guild")
        )


if __name__ == "__main__":
    unittest.main()
