import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

PAST_START = "2020-01-01T00:00:00+00:00"
PAST_END = "2020-01-01T04:00:00+00:00"
FUTURE_START = "2099-01-01T00:00:00+00:00"
FUTURE_END = "2099-01-01T04:00:00+00:00"


def _change_data(unit_id, start=FUTURE_START, end=FUTURE_END):
    return {
        "unit_id": unit_id,
        "description": "bypass level interlock",
        "work_start": start,
        "work_end": end,
        "isolation": "close XV-101 and lock out",
        "restore_owner": "O-1",
    }


class TempChangeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.safety = Actor("safety-1", "safety")
        self.engineer = Actor("eng-1", "engineer")
        self.operator = Actor("op-1", "operator")
        self.unit = self.service.create(
            self.admin, "unit", {"name": "Reactor-1", "location": "Plant-A"}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _create_change(self, **kwargs):
        return self.service.create(
            self.engineer, "temp_change", _change_data(self.unit["id"], **kwargs)
        )

    def test_confirm_then_restore_flow(self):
        change = self._create_change()
        self.assertEqual(change["status"], "pending")
        confirmed = self.service.transition(self.safety, change["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(confirmed["data"]["confirmed_by"], "safety-1")
        restored = self.service.transition(
            self.operator,
            change["id"],
            "restore",
            {"restored_by": "O-1", "restore_note": "isolation removed"},
        )
        self.assertEqual(restored["status"], "restored")

    def test_confirm_requires_safety_role(self):
        change = self._create_change()
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.engineer, change["id"], "confirm", {})

    def test_overlapping_window_is_rejected_with_conflict_id(self):
        first = self._create_change()
        self.service.transition(self.safety, first["id"], "confirm", {})
        overlap = self._create_change(
            start="2099-01-01T02:00:00+00:00", end="2099-01-01T06:00:00+00:00"
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.safety, overlap["id"], "confirm", {})
        self.assertIn(first["id"], str(ctx.exception))
        touching = self._create_change(
            start="2099-01-01T04:00:00+00:00", end="2099-01-01T08:00:00+00:00"
        )
        confirmed = self.service.transition(self.safety, touching["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")

    def test_overdue_unrestored_blocks_startup_until_restored(self):
        change = self._create_change(start=PAST_START, end=PAST_END)
        self.service.transition(self.safety, change["id"], "confirm", {})
        self.service.transition(
            self.operator, self.unit["id"], "shutdown", {"reason": "repair"}
        )
        with self.assertRaises(ValidationError) as ctx:
            self.service.transition(self.operator, self.unit["id"], "startup", {})
        self.assertIn(change["id"], str(ctx.exception))
        self.service.transition(
            self.operator,
            change["id"],
            "restore",
            {"restored_by": "O-1", "restore_note": "done"},
        )
        started = self.service.transition(self.operator, self.unit["id"], "startup", {})
        self.assertEqual(started["status"], "operating")

    def test_renew_requires_risk_reassessment_and_new_confirmation(self):
        change = self._create_change()
        self.service.transition(self.safety, change["id"], "confirm", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer,
                change["id"],
                "renew",
                {"work_end": "2099-01-02T00:00:00+00:00", "risk_level": "high"},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.engineer,
                change["id"],
                "renew",
                {
                    "work_end": "2099-01-01T02:00:00+00:00",
                    "risk_level": "high",
                    "risk_note": "backwards",
                },
            )
        renewed = self.service.transition(
            self.engineer,
            change["id"],
            "renew",
            {
                "work_end": "2099-01-02T00:00:00+00:00",
                "risk_level": "high",
                "risk_note": "re-assessed after overtime",
            },
        )
        self.assertEqual(renewed["status"], "pending")
        self.assertEqual(renewed["data"]["revision"], 2)
        self.assertEqual(renewed["data"]["work_end"], "2099-01-02T00:00:00+00:00")
        confirmed = self.service.transition(self.safety, change["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "active")

    def test_create_validation(self):
        with self.assertRaises(ValidationError):
            self._create_change(start=FUTURE_END, end=FUTURE_START)
        with self.assertRaises(ValidationError):
            self.service.create(
                self.engineer, "temp_change", _change_data("no-such-unit")
            )

    def test_board_reports_blockers_and_conflicts(self):
        overdue = self._create_change(start=PAST_START, end=PAST_END)
        self.service.transition(self.safety, overdue["id"], "confirm", {})
        pending = self._create_change(start=PAST_START, end=PAST_END)
        board = self.service.board()
        unit_view = next(u for u in board["units"] if u["id"] == self.unit["id"])
        self.assertIn(overdue["id"], unit_view["startup_blockers"])
        overdue_view = next(c for c in board["temp_changes"] if c["id"] == overdue["id"])
        self.assertTrue(overdue_view["overdue"])
        pending_view = next(c for c in board["temp_changes"] if c["id"] == pending["id"])
        self.assertEqual(pending_view["conflicts"], [overdue["id"]])


if __name__ == "__main__":
    unittest.main()
