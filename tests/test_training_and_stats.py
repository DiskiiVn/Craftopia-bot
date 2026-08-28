import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import discord

from bot import (
    DEFAULT_TRAINING_TOPIC,
    _load_stats_target,
    _write_stats_target,
    bot as craftopia_bot,
    build_training_document,
    has_sensitive_training_data,
    resolve_training_reply,
    save_ai_fact,
)
from knowledge import KnowledgeBase


class TrainingDocumentTests(unittest.TestCase):
    def test_plain_text_preserves_newlines_and_more_than_3000_characters(self):
        lesson = "Dòng đầu\n" + ("x" * 3_500) + "\nDòng cuối"

        document = build_training_document(lesson)

        self.assertEqual(document.topic, DEFAULT_TRAINING_TOPIC)
        self.assertEqual(document.fact, lesson)
        self.assertGreater(len(document.fact), 3_000)
        self.assertEqual(document.source_count, 1)

    def test_topic_separator_splits_only_the_first_pipe(self):
        document = build_training_document("Luật PvP | Không combat-log | ngoại lệ khi bảo trì")

        self.assertEqual(document.topic, "Luật PvP")
        self.assertEqual(document.fact, "Không combat-log | ngoại lệ khi bảo trì")

    def test_reply_only_uses_default_topic(self):
        document = build_training_document(replied_text="Server bảo trì lúc 22:00.")

        self.assertEqual(document.topic, DEFAULT_TRAINING_TOPIC)
        self.assertEqual(
            document.fact,
            "### Tin nhắn được trả lời\n\nServer bảo trì lúc 22:00.",
        )
        self.assertEqual(document.source_count, 1)

    def test_lesson_becomes_topic_when_training_a_reply(self):
        document = build_training_document(
            "Lịch bảo trì",
            replied_text="Server bảo trì lúc 22:00.",
        )

        self.assertEqual(document.topic, "Lịch bảo trì")
        self.assertIn("### Tin nhắn được trả lời", document.fact)
        self.assertTrue(document.fact.endswith("Server bảo trì lúc 22:00."))

    def test_inline_reply_and_files_are_combined_in_source_order(self):
        document = build_training_document(
            "Hướng dẫn | Nội dung nhập trực tiếp",
            replied_text="Nội dung từ tin nhắn reply",
            documents=(
                ("rules.md", "Nội dung rules"),
                ("faq.txt", "Nội dung FAQ"),
            ),
        )

        labels = (
            "### Nội dung lệnh",
            "### Tin nhắn được trả lời",
            "### Tệp rules.md",
            "### Tệp faq.txt",
        )
        positions = [document.fact.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(document.source_count, 4)
        self.assertIn("Nội dung nhập trực tiếp", document.fact)
        self.assertIn("Nội dung từ tin nhắn reply", document.fact)
        self.assertIn("Nội dung rules", document.fact)
        self.assertIn("Nội dung FAQ", document.fact)

    def test_empty_lesson_reply_and_documents_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Không có nội dung"):
            build_training_document()

    def test_content_over_configured_limit_is_rejected_without_truncation(self):
        with patch("bot.MAX_TRAINING_CHARS", 20):
            with self.assertRaisesRegex(ValueError, "vượt giới hạn 20"):
                build_training_document("x" * 21)

    def test_secret_patterns_are_rejected(self):
        secrets = (
            "password=abcdefgh",
            "api_key: ABCDEFGHIJKLMNOP",
            "Bearer abcdefghijklmnop",
            "AIza" + ("A" * 32),
            "-----BEGIN PRIVATE KEY-----",
            "https://discord.com/api/webhooks/123456789/secret-value",
        )

        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertTrue(has_sensitive_training_data(secret))
                with self.assertRaisesRegex(ValueError, "dữ liệu giống"):
                    build_training_document(secret)

    def test_public_craftopia_address_is_not_treated_as_a_secret(self):
        address = "IP Bedrock là play.craftopics.online:19132"

        self.assertFalse(has_sensitive_training_data(address))
        self.assertEqual(build_training_document(address).fact, address)

    def test_nul_is_rejected_in_every_input_source(self):
        cases = (
            lambda: build_training_document("nội dung\x00 lỗi"),
            lambda: build_training_document("Chủ đề\x00 | nội dung hợp lệ"),
            lambda: build_training_document(replied_text="reply\x00 lỗi"),
            lambda: build_training_document(documents=(("facts.txt", "file\x00 lỗi"),)),
        )

        for build in cases:
            with self.subTest(build=build):
                with self.assertRaisesRegex(ValueError, "nhị phân"):
                    build()


class StatsTargetPersistenceTests(unittest.TestCase):
    def test_stats_target_round_trip_uses_valid_json(self):
        target = {
            "guild_id": 111,
            "channel_id": 222,
            "message_id": 333,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "data" / "stats-dashboard.json"

            _write_stats_target(path, target)

            self.assertEqual(_load_stats_target(path), target)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), target)
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_missing_malformed_and_invalid_fields_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stats-dashboard.json"
            self.assertEqual(_load_stats_target(path), {})

            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(_load_stats_target(path), {})

            path.write_text(
                json.dumps(
                    {
                        "guild_id": 123,
                        "channel_id": 0,
                        "message_id": "456",
                        "untrusted_extra": 789,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(_load_stats_target(path), {"guild_id": 123})


class SaveAiFactTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_full_lesson_atomically_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            knowledge = KnowledgeBase(root / "knowledge")
            long_fact = "Dòng đầu\n" + ("nội dung " * 600) + "\nDòng cuối"
            with patch("bot.ROOT", root), patch.object(craftopia_bot, "knowledge", knowledge):
                topic, count, created = await save_ai_fact("Luật Craftopia", long_fact)
                _, duplicate_count, duplicate_created = await save_ai_fact("Luật Craftopia", long_fact)

            files = list((root / "knowledge" / "managed").glob("*.md"))
            self.assertEqual(topic, "Luật Craftopia")
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(count, duplicate_count)
            self.assertEqual(len(files), 1)
            self.assertIn(long_fact, files[0].read_text(encoding="utf-8"))
            self.assertGreater(count, 1)


class TrainingReplyPrivacyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_message(author_id: int, replied_author_id: int):
        guild = SimpleNamespace(id=123)
        replied = Mock(spec=discord.Message)
        replied.guild = guild
        replied.author = SimpleNamespace(id=replied_author_id, bot=False)
        replied.webhook_id = None
        return SimpleNamespace(
            guild=guild,
            author=SimpleNamespace(id=author_id),
            channel=SimpleNamespace(id=456),
            reference=SimpleNamespace(message_id=789, resolved=replied),
        ), replied

    async def test_own_replied_message_is_allowed(self):
        message, replied = self.make_message(author_id=10, replied_author_id=10)
        self.assertIs(await resolve_training_reply(message), replied)

    async def test_other_members_replied_message_is_rejected(self):
        message, _ = self.make_message(author_id=10, replied_author_id=11)
        with self.assertRaisesRegex(ValueError, "quyền riêng tư"):
            await resolve_training_reply(message)


if __name__ == "__main__":
    unittest.main()
