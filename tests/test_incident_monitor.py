import unittest
from unittest.mock import patch

from incident_monitor import IncidentMonitor


class Clock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class IncidentMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.monotonic_patch = patch("incident_monitor.time.monotonic", self.clock)
        self.monotonic_patch.start()
        self.addCleanup(self.monotonic_patch.stop)

    def test_triggers_after_three_reports_from_two_people(self):
        monitor = IncidentMonitor()

        self.assertEqual(monitor.observe(10, 101, "server lag"), [])
        self.assertEqual(monitor.observe(10, 101, "ping cao"), [])
        signals = monitor.observe(10, 202, "tôi bị disconnect")

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].category, "instability")
        self.assertEqual(signals[0].channel_id, 10)
        self.assertEqual(signals[0].unique_reporters, 2)

    def test_three_reports_from_one_person_do_not_trigger(self):
        monitor = IncidentMonitor()

        self.assertEqual(monitor.observe(10, 101, "lag"), [])
        self.assertEqual(monitor.observe(10, 101, "server lag"), [])
        self.assertEqual(monitor.observe(10, 101, "ping cao"), [])

    def test_keywords_do_not_match_inside_other_words(self):
        self.assertEqual(IncidentMonitor.classify("plugin flag và hackathon"), set())
        self.assertEqual(IncidentMonitor.classify("server sắp mở rồi"), set())

    def test_conflict_takes_priority_over_instability(self):
        categories = IncidentMonitor.classify("server lag, hai bạn đừng cãi nhau")
        self.assertEqual(categories, {"conflict"})

    def test_reports_outside_window_do_not_combine(self):
        monitor = IncidentMonitor(window_seconds=180)

        self.assertEqual(monitor.observe(10, 101, "lag"), [])
        self.clock.value = 1
        self.assertEqual(monitor.observe(10, 202, "disconnect"), [])
        self.clock.value = 181

        self.assertEqual(monitor.observe(10, 303, "server offline"), [])

    def test_cooldown_blocks_same_channel_and_category(self):
        monitor = IncidentMonitor(cooldown_seconds=600)

        monitor.observe(10, 101, "lag")
        monitor.observe(10, 101, "ping cao")
        self.assertEqual(len(monitor.observe(10, 202, "disconnect")), 1)

        self.clock.value = 100
        self.assertEqual(monitor.observe(10, 303, "lag"), [])
        self.assertEqual(monitor.observe(10, 303, "ping cao"), [])
        self.assertEqual(monitor.observe(10, 404, "server offline"), [])

    def test_cooldown_is_scoped_to_channel_and_category(self):
        monitor = IncidentMonitor(cooldown_seconds=600, max_alerts_per_hour=10)

        for channel_id in (10, 20):
            with self.subTest(channel_id=channel_id):
                self.assertEqual(monitor.observe(channel_id, 101, "lag"), [])
                self.assertEqual(monitor.observe(channel_id, 101, "ping cao"), [])
                signals = monitor.observe(channel_id, 202, "disconnect")
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0].channel_id, channel_id)

    def test_cooldown_is_independent_between_categories(self):
        monitor = IncidentMonitor(cooldown_seconds=600, max_alerts_per_hour=10)

        monitor.observe(10, 101, "lag")
        monitor.observe(10, 101, "ping cao")
        self.assertEqual(len(monitor.observe(10, 202, "disconnect")), 1)

        monitor.observe(10, 101, "scam")
        monitor.observe(10, 101, "lừa đảo")
        signals = monitor.observe(10, 202, "tố cáo giả danh staff")

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].category, "conflict")

    def test_global_hourly_limit_is_scoped_to_guild(self):
        monitor = IncidentMonitor(cooldown_seconds=0, max_alerts_per_hour=2)

        def trigger(channel_id: int, guild_id: int):
            monitor.observe(channel_id, 101, "lag", guild_id=guild_id)
            monitor.observe(channel_id, 101, "ping cao", guild_id=guild_id)
            return monitor.observe(channel_id, 202, "disconnect", guild_id=guild_id)

        self.assertEqual(len(trigger(10, 7)), 1)
        self.assertEqual(len(trigger(20, 7)), 1)
        self.assertEqual(trigger(30, 7), [])
        self.assertEqual(len(trigger(40, 8)), 1)

    def test_global_hourly_limit_expires_after_one_hour(self):
        monitor = IncidentMonitor(cooldown_seconds=0, max_alerts_per_hour=1)

        def trigger(channel_id: int):
            monitor.observe(channel_id, 101, "lag", guild_id=7)
            monitor.observe(channel_id, 101, "ping cao", guild_id=7)
            return monitor.observe(channel_id, 202, "disconnect", guild_id=7)

        self.assertEqual(len(trigger(10)), 1)
        self.clock.value = 100
        self.assertEqual(trigger(20), [])
        self.clock.value = 3600
        self.assertEqual(len(trigger(30)), 1)


if __name__ == "__main__":
    unittest.main()
