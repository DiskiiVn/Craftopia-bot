import unittest
from unittest.mock import AsyncMock, Mock, patch

from ai_support import AIProviderError, ConversationMemory, MinecraftSupportAI


class ConversationMemoryTests(unittest.TestCase):
    def test_history_expires_after_ttl(self):
        memory = ConversationMemory(max_messages=2, ttl_seconds=60)
        with patch("ai_support.time.monotonic", return_value=0):
            memory.add((1, 2, 3), "user", "xin chào")
        with patch("ai_support.time.monotonic", return_value=59):
            self.assertEqual(len(memory.get((1, 2, 3))), 1)
        with patch("ai_support.time.monotonic", return_value=60):
            self.assertEqual(memory.get((1, 2, 3)), [])
            self.assertNotIn((1, 2, 3), memory.data)

    def test_history_respects_message_limit(self):
        memory = ConversationMemory(max_messages=2, ttl_seconds=60)
        memory.add((1, 2, 3), "user", "one")
        memory.add((1, 2, 3), "assistant", "two")
        memory.add((1, 2, 3), "user", "three")
        self.assertEqual(
            memory.get((1, 2, 3)),
            [("assistant", "two"), ("user", "three")],
        )


class HealthCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_check_returns_provider_text(self):
        ai = MinecraftSupportAI(Mock())
        ai._generate = AsyncMock(return_value={
            "candidates": [{"content": {"parts": [{"text": "OK"}]}}]
        })
        self.assertEqual(await ai.health_check(), "OK")

    async def test_health_check_rejects_empty_response(self):
        ai = MinecraftSupportAI(Mock())
        ai._generate = AsyncMock(return_value={})
        with self.assertRaises(AIProviderError):
            await ai.health_check()


if __name__ == "__main__":
    unittest.main()
