import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from bot import DeleteMessagesConfirmView, prepare_delete_messages_preview
from message_cleanup import (
    DeleteMessagesRequest,
    DeleteScanSnapshot,
    delete_snapshot_messages,
    parse_delete_messages_request,
    scan_today_messages,
    vietnam_today_bounds,
)


BOT_USER_ID = 999_999
TARGET_USER_ID = 123_456


class ParseDeleteMessagesRequestTests(unittest.TestCase):
    def test_accepts_accents_and_nickname_mention(self):
        result = parse_delete_messages_request(
            "Bạn hãy xoá tất cả tin nhắn của <@!123456> trong hôm nay.",
            {TARGET_USER_ID},
            BOT_USER_ID,
        )

        self.assertTrue(result.destructive)
        self.assertIsNone(result.error)
        self.assertEqual(result.request.target_user_id, TARGET_USER_ID)
        self.assertEqual(result.request.scope, "channel")

    def test_ignores_leading_bot_mention_and_resolved_bot_user(self):
        result = parse_delete_messages_request(
            "<@999999> vui lòng xóa tin nhắn của <@123456> "
            "trong ngày hôm nay tại đây!",
            {BOT_USER_ID, TARGET_USER_ID},
            BOT_USER_ID,
        )

        self.assertTrue(result.destructive)
        self.assertIsNone(result.error)
        self.assertEqual(result.request.target_user_id, TARGET_USER_ID)
        self.assertEqual(result.request.scope, "channel")

    def test_parses_explicit_guild_scope(self):
        result = parse_delete_messages_request(
            "Xoá messages của <@123456> hôm nay trong toàn bộ server",
            {TARGET_USER_ID},
            BOT_USER_ID,
        )

        self.assertTrue(result.destructive)
        self.assertIsNone(result.error)
        self.assertEqual(result.request.scope, "guild")

    def test_destructive_but_malformed_request_returns_safe_error(self):
        result = parse_delete_messages_request(
            "Xoá tin nhắn hôm nay của <@123456>",
            {TARGET_USER_ID},
            BOT_USER_ID,
        )

        self.assertTrue(result.destructive)
        self.assertIsNone(result.request)
        self.assertIn("cú pháp an toàn", result.error)

    def test_requires_exactly_one_resolved_non_bot_mention(self):
        cases = (
            set(),
            {TARGET_USER_ID, 654_321},
            {BOT_USER_ID},
        )

        for resolved_user_ids in cases:
            with self.subTest(resolved_user_ids=resolved_user_ids):
                result = parse_delete_messages_request(
                    "Xoá tin nhắn của <@123456> trong hôm nay",
                    resolved_user_ids,
                    BOT_USER_ID,
                )
                self.assertTrue(result.destructive)
                self.assertIsNone(result.request)
                self.assertIn("mention đúng một", result.error)

    def test_negation_is_not_treated_as_a_destructive_request(self):
        for text in (
            "Đừng xoá tin nhắn của <@123456> trong hôm nay",
            "Không xóa tin nhắn của <@123456> trong hôm nay",
            "Chưa xoá tin nhắn của <@123456> trong hôm nay",
        ):
            with self.subTest(text=text):
                result = parse_delete_messages_request(
                    text,
                    {TARGET_USER_ID},
                    BOT_USER_ID,
                )
                self.assertFalse(result.destructive)
                self.assertIsNone(result.request)
                self.assertIsNone(result.error)

    def test_unrelated_message_is_not_destructive(self):
        result = parse_delete_messages_request(
            "Hôm nay server có bao nhiêu người online?",
            set(),
            BOT_USER_ID,
        )

        self.assertFalse(result.destructive)
        self.assertIsNone(result.request)


class VietnamTodayBoundsTests(unittest.TestCase):
    def test_midnight_boundary_is_calculated_in_utc_plus_seven(self):
        now = datetime(2026, 8, 28, 0, 30, 15, tzinfo=UTC)

        started_at, ended_at, label = vietnam_today_bounds(now)

        self.assertEqual(started_at, datetime(2026, 8, 27, 17, 0, tzinfo=UTC))
        self.assertEqual(ended_at, now)
        self.assertEqual(label, "28/08/2026")

    def test_utc_afternoon_still_maps_to_previous_vietnamese_day(self):
        now = datetime(2026, 8, 27, 16, 59, 59, tzinfo=UTC)

        started_at, ended_at, label = vietnam_today_bounds(now)

        self.assertEqual(started_at, datetime(2026, 8, 26, 17, 0, tzinfo=UTC))
        self.assertEqual(ended_at, now)
        self.assertEqual(label, "27/08/2026")

    def test_rejects_naive_datetime(self):
        with self.assertRaisesRegex(ValueError, "timezone"):
            vietnam_today_bounds(datetime(2026, 8, 28, 12, 0))


class FakeMessage:
    def __init__(
        self,
        message_id,
        author_id,
        created_at,
        *,
        pinned=False,
        system=False,
    ):
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.created_at = created_at
        self.pinned = pinned
        self._system = system

    def is_system(self):
        return self._system


class FakeScanChannel:
    def __init__(self, channel_id, messages, *, permissions=True):
        self.id = channel_id
        self.messages = messages
        self.permissions = permissions
        self.history_calls = []
        self.permission_members = []

    def permissions_for(self, member):
        self.permission_members.append(member)
        return SimpleNamespace(
            view_channel=self.permissions,
            read_message_history=self.permissions,
            manage_messages=self.permissions,
        )

    def history(self, **kwargs):
        self.history_calls.append(kwargs)

        async def iterator():
            for message in self.messages:
                yield message

        return iterator()

    async def delete_messages(self, messages, *, reason):
        del messages, reason


class ScanTodayMessagesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
        self.started_at = datetime(2026, 8, 27, 17, 0, tzinfo=UTC)
        self.actor = SimpleNamespace(id=11)
        self.bot_member = SimpleNamespace(id=BOT_USER_ID)

    def make_guild(self, *, other_channels=()):
        return SimpleNamespace(
            me=self.bot_member,
            text_channels=list(other_channels),
            threads=[],
        )

    async def test_current_channel_scan_filters_author_and_protected_messages(self):
        messages = [
            FakeMessage(1, TARGET_USER_ID, self.started_at),
            FakeMessage(2, 222_222, self.started_at),
            FakeMessage(3, TARGET_USER_ID, self.started_at, pinned=True),
            FakeMessage(4, TARGET_USER_ID, self.started_at, system=True),
            FakeMessage(5, TARGET_USER_ID, self.started_at),
            FakeMessage(6, TARGET_USER_ID, self.started_at.replace(hour=16)),
            FakeMessage(7, TARGET_USER_ID, self.now),
        ]
        current_channel = FakeScanChannel(10, messages)
        ignored_guild_channel = FakeScanChannel(
            20,
            [FakeMessage(8, TARGET_USER_ID, self.started_at)],
        )

        snapshot = await scan_today_messages(
            guild=self.make_guild(other_channels=(ignored_guild_channel,)),
            current_channel=current_channel,
            actor=self.actor,
            target_user_id=TARGET_USER_ID,
            scope="channel",
            invocation_message_id=5,
            scan_limit_per_channel=100,
            max_matches=100,
            now=self.now,
        )

        self.assertEqual(snapshot.message_ids_by_channel, ((10, (1,)),))
        self.assertEqual(snapshot.scanned_messages, 7)
        self.assertEqual(snapshot.scanned_channels, 1)
        self.assertEqual(snapshot.skipped_channels, 0)
        self.assertEqual(snapshot.skipped_pinned, 1)
        self.assertEqual(snapshot.skipped_system, 1)
        self.assertEqual(snapshot.skipped_invocation, 1)
        self.assertFalse(snapshot.truncated)
        self.assertFalse(snapshot.overflow)
        self.assertEqual(len(current_channel.history_calls), 1)
        self.assertEqual(
            current_channel.history_calls[0]["after"],
            self.started_at - timedelta(milliseconds=1),
        )
        self.assertEqual(current_channel.history_calls[0]["before"], self.now)
        self.assertTrue(current_channel.history_calls[0]["oldest_first"])
        self.assertEqual(current_channel.permission_members, [self.actor, self.bot_member])
        self.assertEqual(ignored_guild_channel.history_calls, [])

    async def test_match_cap_sets_overflow_without_exceeding_cap(self):
        channel = FakeScanChannel(
            10,
            [
                FakeMessage(1, TARGET_USER_ID, self.started_at),
                FakeMessage(2, TARGET_USER_ID, self.started_at),
                FakeMessage(3, TARGET_USER_ID, self.started_at),
            ],
        )

        snapshot = await scan_today_messages(
            guild=self.make_guild(),
            current_channel=channel,
            actor=self.actor,
            target_user_id=TARGET_USER_ID,
            scope="channel",
            invocation_message_id=None,
            scan_limit_per_channel=100,
            max_matches=2,
            now=self.now,
        )

        self.assertEqual(snapshot.message_ids_by_channel, ((10, (1, 2)),))
        self.assertEqual(snapshot.matched_messages, 2)
        self.assertEqual(snapshot.scanned_messages, 3)
        self.assertTrue(snapshot.overflow)
        self.assertFalse(snapshot.truncated)

    async def test_per_channel_scan_limit_sets_truncated(self):
        channel = FakeScanChannel(
            10,
            [
                FakeMessage(1, TARGET_USER_ID, self.started_at),
                FakeMessage(2, TARGET_USER_ID, self.started_at),
                FakeMessage(3, TARGET_USER_ID, self.started_at),
            ],
        )

        snapshot = await scan_today_messages(
            guild=self.make_guild(),
            current_channel=channel,
            actor=self.actor,
            target_user_id=TARGET_USER_ID,
            scope="channel",
            invocation_message_id=None,
            scan_limit_per_channel=2,
            max_matches=100,
            now=self.now,
        )

        self.assertEqual(snapshot.message_ids_by_channel, ((10, (1, 2)),))
        self.assertEqual(snapshot.scanned_messages, 2)
        self.assertTrue(snapshot.truncated)
        self.assertFalse(snapshot.overflow)

    async def test_total_scan_limit_stops_across_channels(self):
        first_channel = FakeScanChannel(
            10,
            [
                FakeMessage(1, 222_222, self.started_at),
                FakeMessage(2, 222_222, self.started_at),
            ],
        )
        second_channel = FakeScanChannel(
            20,
            [
                FakeMessage(3, TARGET_USER_ID, self.started_at),
                FakeMessage(4, TARGET_USER_ID, self.started_at),
            ],
        )

        snapshot = await scan_today_messages(
            guild=self.make_guild(other_channels=(first_channel, second_channel)),
            current_channel=first_channel,
            actor=self.actor,
            target_user_id=TARGET_USER_ID,
            scope="guild",
            invocation_message_id=None,
            scan_limit_per_channel=100,
            scan_limit_total=3,
            max_matches=100,
            now=self.now,
        )

        self.assertEqual(snapshot.scanned_messages, 3)
        self.assertEqual(snapshot.message_ids_by_channel, ((20, (3,)),))
        self.assertTrue(snapshot.truncated)

    async def test_missing_actor_permission_skips_channel_without_history_read(self):
        channel = FakeScanChannel(
            10,
            [FakeMessage(1, TARGET_USER_ID, self.started_at)],
            permissions=False,
        )

        snapshot = await scan_today_messages(
            guild=self.make_guild(),
            current_channel=channel,
            actor=self.actor,
            target_user_id=TARGET_USER_ID,
            scope="channel",
            invocation_message_id=None,
            scan_limit_per_channel=100,
            max_matches=100,
            now=self.now,
        )

        self.assertEqual(snapshot.message_ids_by_channel, ())
        self.assertEqual(snapshot.scanned_channels, 0)
        self.assertEqual(snapshot.skipped_channels, 1)
        self.assertEqual(channel.history_calls, [])


def make_snapshot(channel_id, message_ids):
    instant = datetime(2026, 8, 28, tzinfo=UTC)
    return DeleteScanSnapshot(
        message_ids_by_channel=((channel_id, tuple(message_ids)),),
        scanned_messages=len(message_ids),
        scanned_channels=1,
        skipped_channels=0,
        skipped_pinned=0,
        skipped_system=0,
        skipped_invocation=0,
        truncated=False,
        overflow=False,
        started_at_utc=instant,
        ended_at_utc=instant,
    )


class FakeDeleteChannel:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.batch_sizes = []
        self.reasons = []

    async def delete_messages(self, messages, *, reason):
        self.batch_sizes.append(len(messages))
        self.reasons.append(reason)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome


class FakeClient:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        del channel_id
        return self.channel


class DeleteSnapshotMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_delete_is_batched_as_100_100_1(self):
        channel = FakeDeleteChannel()
        snapshot = make_snapshot(10, range(1, 202))

        result = await delete_snapshot_messages(
            FakeClient(channel),
            snapshot,
            reason="Kiểm duyệt bởi quản trị viên",
        )

        self.assertEqual(channel.batch_sizes, [100, 100, 1])
        self.assertEqual(
            channel.reasons,
            ["Kiểm duyệt bởi quản trị viên"] * 3,
        )
        self.assertEqual(result.deleted, 201)
        self.assertEqual(result.already_missing, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(result.failed_channels, 0)

    async def test_partial_batch_failures_are_counted_and_later_batches_continue(self):
        response = SimpleNamespace(status=404, reason="Not Found")
        not_found = discord.NotFound(
            response,
            {"message": "Unknown Message", "code": 10008},
        )
        channel = FakeDeleteChannel(
            outcomes=(
                None,
                not_found,
                discord.ClientException("bulk delete failed"),
            )
        )
        snapshot = make_snapshot(10, range(1, 202))

        result = await delete_snapshot_messages(
            FakeClient(channel),
            snapshot,
            reason="Kiểm thử lỗi từng phần",
        )

        self.assertEqual(channel.batch_sizes, [100, 100, 1])
        self.assertEqual(result.deleted, 100)
        self.assertEqual(result.already_missing, 100)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.failed_channels, 1)


def make_member(member_id, *, administrator=False, display_name="member"):
    return SimpleNamespace(
        id=member_id,
        display_name=display_name,
        guild_permissions=SimpleNamespace(administrator=administrator),
    )


class PreviewAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_fetches_fresh_target_and_fails_closed_when_fetch_fails(self):
        target = make_member(TARGET_USER_ID)
        actor = make_member(11, administrator=True)
        response = SimpleNamespace(status=404, reason="Not Found")
        guild = SimpleNamespace(
            id=77,
            owner_id=1,
            me=make_member(BOT_USER_ID),
            fetch_member=AsyncMock(
                side_effect=discord.NotFound(
                    response,
                    {"message": "Unknown Member", "code": 10007},
                )
            ),
        )
        channel = FakeScanChannel(10, [])
        bot_instance = SimpleNamespace(
            message_cleanup_last_scan={},
            message_cleanup_locks={},
        )

        with patch("bot.scan_today_messages", new_callable=AsyncMock) as scan:
            content, view = await prepare_delete_messages_preview(
                bot_instance=bot_instance,
                guild=guild,
                channel=channel,
                actor=actor,
                target=target,
                request=DeleteMessagesRequest(TARGET_USER_ID, "channel"),
                invocation_message_id=500,
            )

        guild.fetch_member.assert_awaited_once_with(TARGET_USER_ID)
        scan.assert_not_awaited()
        self.assertIsNone(view)
        self.assertIn("không thể kiểm tra mới quyền", content.casefold())
        self.assertIn("chưa quét hoặc xoá gì", content.casefold())

    async def test_preview_authorizes_with_fresh_roles_not_stale_target_roles(self):
        stale_target = make_member(TARGET_USER_ID, administrator=False)
        fresh_target = make_member(TARGET_USER_ID, administrator=True)
        actor = make_member(11, administrator=True)
        guild = SimpleNamespace(
            id=77,
            owner_id=1,
            me=make_member(BOT_USER_ID),
            fetch_member=AsyncMock(return_value=fresh_target),
        )
        channel = FakeScanChannel(10, [])
        bot_instance = SimpleNamespace(
            message_cleanup_last_scan={},
            message_cleanup_locks={},
        )

        with patch("bot.scan_today_messages", new_callable=AsyncMock) as scan:
            content, view = await prepare_delete_messages_preview(
                bot_instance=bot_instance,
                guild=guild,
                channel=channel,
                actor=actor,
                target=stale_target,
                request=DeleteMessagesRequest(TARGET_USER_ID, "channel"),
                invocation_message_id=None,
            )

        guild.fetch_member.assert_awaited_once_with(TARGET_USER_ID)
        scan.assert_not_awaited()
        self.assertIsNone(view)
        self.assertIn("chỉ chủ server", content.casefold())

    async def test_guild_preview_fails_closed_if_any_channel_was_skipped(self):
        actor = make_member(11, administrator=True)
        target = make_member(TARGET_USER_ID, display_name="Diskiivn")
        guild = SimpleNamespace(
            id=77,
            owner_id=1,
            me=make_member(BOT_USER_ID),
            fetch_member=AsyncMock(return_value=target),
        )
        channel = FakeScanChannel(10, [])
        bot_instance = SimpleNamespace(
            message_cleanup_last_scan={},
            message_cleanup_locks={},
        )
        snapshot = make_snapshot(10, [101])
        snapshot = DeleteScanSnapshot(
            message_ids_by_channel=snapshot.message_ids_by_channel,
            scanned_messages=snapshot.scanned_messages,
            scanned_channels=snapshot.scanned_channels,
            skipped_channels=1,
            skipped_pinned=snapshot.skipped_pinned,
            skipped_system=snapshot.skipped_system,
            skipped_invocation=snapshot.skipped_invocation,
            truncated=snapshot.truncated,
            overflow=snapshot.overflow,
            started_at_utc=snapshot.started_at_utc,
            ended_at_utc=snapshot.ended_at_utc,
        )

        with patch(
            "bot.scan_today_messages",
            new=AsyncMock(return_value=snapshot),
        ) as scan:
            content, view = await prepare_delete_messages_preview(
                bot_instance=bot_instance,
                guild=guild,
                channel=channel,
                actor=actor,
                target=target,
                request=DeleteMessagesRequest(TARGET_USER_ID, "guild"),
                invocation_message_id=None,
            )

        scan.assert_awaited_once()
        self.assertIsNone(view)
        self.assertIn("không quét được **1** kênh", content.casefold())
        self.assertIn("phải được quét đầy đủ", content.casefold())


class ConfirmAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirm_fetches_fresh_target_and_stops_before_delete_on_fetch_failure(self):
        actor = Mock(spec=discord.Member)
        actor.id = 11
        actor.guild_permissions = SimpleNamespace(administrator=True)
        response = SimpleNamespace(status=403, reason="Forbidden")
        guild = SimpleNamespace(
            id=77,
            owner_id=1,
            me=make_member(BOT_USER_ID),
            fetch_member=AsyncMock(
                side_effect=discord.Forbidden(
                    response,
                    {"message": "Missing Access", "code": 50001},
                )
            ),
        )
        interaction = SimpleNamespace(
            guild=guild,
            user=actor,
            channel=FakeScanChannel(10, []),
            response=SimpleNamespace(
                defer=AsyncMock(),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        bot_instance = SimpleNamespace(message_cleanup_locks={})
        view = DeleteMessagesConfirmView(
            bot_instance=bot_instance,
            requester_id=actor.id,
            target_user_id=TARGET_USER_ID,
            guild_id=guild.id,
            scope="channel",
            snapshot=make_snapshot(10, [101]),
        )
        confirm_button = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.style is discord.ButtonStyle.danger
        )

        with patch("bot.delete_snapshot_messages", new_callable=AsyncMock) as delete:
            await confirm_button.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        guild.fetch_member.assert_awaited_once_with(TARGET_USER_ID)
        delete.assert_not_awaited()
        interaction.edit_original_response.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        followup_text = interaction.followup.send.await_args.args[0]
        self.assertIn("thao tác đã dừng an toàn", followup_text.casefold())
        self.assertFalse(view.completed)


if __name__ == "__main__":
    unittest.main()
