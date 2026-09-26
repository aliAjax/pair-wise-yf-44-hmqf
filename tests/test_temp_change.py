import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TempChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("safe-1", "safety")
        self.operator = Actor("op-1", "operator")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _register(self, start, end, unit_id=None):
        entity = self.service.create(
            self.operator,
            "temp_change",
            {
                "unit_id": unit_id or self.unit["id"],
                "description": "临时旁路联锁",
                "window_start": start,
                "window_end": end,
                "isolation": "切断进料阀并挂牌上锁",
                "restore_owner": "张工",
            },
        )
        return self.service.transition(self.operator, entity["id"], "submit", {})

    def test_submit_and_safety_confirm_occupies_unit(self):
        ticket = self._register("2030-01-01T08:00:00", "2030-01-01T20:00:00")
        self.assertEqual(ticket["status"], "submitted")
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.operator, ticket["id"], "confirm", {})
        confirmed = self.service.transition(self.safety, ticket["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(confirmed["data"]["confirmed_by"], "safe-1")

    def test_confirm_rejects_overlapping_active_window(self):
        first = self._register("2030-01-01T08:00:00", "2030-01-01T20:00:00")
        self.service.transition(self.safety, first["id"], "confirm", {})
        second = self._register("2030-01-01T12:00:00", "2030-01-02T08:00:00")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.safety, second["id"], "confirm", {})
        self.assertIn(first["id"], str(ctx.exception))
        # 非重叠时段可以确认
        third = self._register("2030-01-02T08:00:00", "2030-01-02T20:00:00")
        confirmed = self.service.transition(self.safety, third["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")

    def test_expired_unrestored_blocks_startup_until_restored(self):
        ticket = self._register("2020-01-01T08:00:00", "2020-01-01T20:00:00")
        self.service.transition(self.safety, ticket["id"], "confirm", {})
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "临停"}
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.operator, self.unit["id"], "startup", {})
        self.assertIn(ticket["id"], str(ctx.exception))

        blockers = self.service.unit_blockers(self.unit["id"])
        self.assertFalse(blockers["can_startup"])
        self.assertEqual([b["id"] for b in blockers["blockers"]], [ticket["id"]])

        restored = self.service.transition(
            self.operator, ticket["id"], "restore", {"restored_by": "张工"}
        )
        self.assertEqual(restored["status"], "restored")
        self.assertTrue(self.service.unit_blockers(self.unit["id"])["can_startup"])
        started = self.service.transition(self.operator, self.unit["id"], "startup", {})
        self.assertEqual(started["status"], "operating")

    def test_expired_ticket_cannot_be_renewed(self):
        ticket = self._register("2020-01-01T08:00:00", "2020-01-01T20:00:00")
        self.service.transition(self.safety, ticket["id"], "confirm", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                ticket["id"],
                "renew",
                {
                    "window_start": "2020-01-01T20:00:00",
                    "window_end": "2020-01-02T08:00:00",
                    "risk_level": "high",
                    "analyst": "E-7",
                },
            )

    def test_renew_reassesses_and_requires_new_confirmation(self):
        ticket = self._register("2030-01-01T08:00:00", "2030-01-01T20:00:00")
        self.service.transition(self.safety, ticket["id"], "confirm", {})
        renewed = self.service.transition(
            self.admin,
            ticket["id"],
            "renew",
            {
                "window_start": "2030-01-01T20:00:00",
                "window_end": "2030-01-02T08:00:00",
                "risk_level": "high",
                "analyst": "E-7",
            },
        )
        self.assertEqual(renewed["status"], "submitted")
        self.assertEqual(renewed["data"]["revision"], 2)
        self.assertEqual(renewed["data"]["reassessed_by"], "E-7")
        confirmed = self.service.transition(self.safety, ticket["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")

    def test_create_requires_isolation_and_restore_owner(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.operator,
                "temp_change",
                {
                    "unit_id": self.unit["id"],
                    "description": "临时旁路联锁",
                    "window_start": "2030-01-01T08:00:00",
                    "window_end": "2030-01-01T20:00:00",
                    "isolation": "",
                    "restore_owner": "张工",
                },
            )


if __name__ == "__main__":
    unittest.main()
