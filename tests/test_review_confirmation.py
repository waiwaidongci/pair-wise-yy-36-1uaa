import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ReviewConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "review item", "description": "review confirmation",
             "severity": "exceedance", "quantity": 12, "threshold": 6,
             "external_ref": "RC-1"}, "creator", "operator")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _to_inspection(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def _close(self, item):
        return self.service.transition(item["id"], "closed", item["version"],
                                       "director", "director")

    def test_close_requires_confirmation_then_passes(self):
        current = self._to_inspection()
        with self.assertRaises(ConflictError) as ctx:
            self._close(current)
        self.assertIn("确认", str(ctx.exception))
        result = self.service.confirm_review(current["id"], "officer",
                                             "compliance_officer")
        self.assertTrue(result["created"])
        closed = self._close(self.service.get_item(current["id"], "viewer"))
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["latest_confirmation_batch"],
                         result["confirmation"]["id"])

    def test_new_material_invalidates_confirmation(self):
        current = self._to_inspection()
        first = self.service.confirm_review(current["id"], "officer",
                                            "compliance_officer")["confirmation"]
        self.service.add_record(current["id"], {
            "kind": "material", "detail": "onsite supplement",
            "status": "closed"}, "field", "operator")
        fresh = self.service.get_item(current["id"], "viewer")
        self.assertFalse(fresh["can_close"])
        self.assertTrue(any("重新确认" in b for b in fresh["close_blockers"]))
        with self.assertRaises(ConflictError) as ctx:
            self._close(fresh)
        self.assertIn("重新确认", str(ctx.exception))
        second = self.service.confirm_review(current["id"], "officer",
                                             "compliance_officer")
        self.assertTrue(second["created"])
        self.assertGreater(second["confirmation"]["id"], first["id"])
        closed = self._close(self.service.get_item(current["id"], "viewer"))
        self.assertEqual(closed["status"], "closed")

    def test_same_batch_confirm_is_idempotent(self):
        current = self._to_inspection()
        self.service.add_record(current["id"], {
            "kind": "material", "detail": "m1", "status": "closed"},
            "field", "operator")
        first = self.service.confirm_review(current["id"], "officer",
                                            "compliance_officer")
        again = self.service.confirm_review(current["id"], "officer2",
                                            "compliance_officer")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["confirmation"]["id"], again["confirmation"]["id"])
        self.assertEqual(len(self.repo.list_confirmations(current["id"])), 1)
        audits = [e for e in self.service.audit("viewer", current["id"])
                  if e["action"] == "confirm_review"]
        self.assertEqual(len(audits), 1)

    def test_confirm_role_and_state_guards(self):
        with self.assertRaises(ConflictError):
            self.service.confirm_review(self.item["id"], "officer",
                                        "compliance_officer")
        current = self._to_inspection()
        with self.assertRaises(PermissionDenied):
            self.service.confirm_review(current["id"], "op", "operator")

    def test_detail_shows_close_readiness_and_batch(self):
        current = self._to_inspection()
        detail = self.service.get_item(current["id"], "viewer")
        self.assertFalse(detail["can_close"])
        self.assertIsNone(detail["latest_confirmation_batch"])
        self.assertTrue(detail["close_blockers"])
        confirmation = self.service.confirm_review(
            current["id"], "officer", "compliance_officer")["confirmation"]
        detail = self.service.get_item(current["id"], "viewer")
        self.assertTrue(detail["can_close"])
        self.assertEqual(detail["latest_confirmation_batch"], confirmation["id"])
        self.assertEqual(detail["close_blockers"], [])

    def test_close_guard_rechecks_inside_lock(self):
        current = self._to_inspection()
        self.service.confirm_review(current["id"], "officer", "compliance_officer")

        def guard():
            # 模拟关闭提交前新材料落库：守卫在同一临界区内必须看到并阻止
            self.repo.add_record(current["id"], "material", "late material",
                                 "closed", None, "field")
            blockers = self.service._confirmation_blockers(current["id"])
            if blockers:
                raise ConflictError("；".join(blockers))

        with self.assertRaises(ConflictError):
            self.repo.transition_item(current["id"], "closed",
                                      current["version"], "director", guard=guard)
        after = self.service.get_item(current["id"], "viewer")
        self.assertEqual(after["status"], "inspection")
        self.assertEqual(after["version"], current["version"])
        self.assertFalse(after["can_close"])
        self.assertEqual(len(self.service.list_records(current["id"], "viewer")), 1)

    def test_concurrent_supplement_and_close(self):
        current = self._to_inspection()
        self.service.confirm_review(current["id"], "officer", "compliance_officer")
        version = current["version"]
        outcomes = {}

        def supplement():
            try:
                self.service.add_record(current["id"], {
                    "kind": "material", "detail": "racing supplement",
                    "status": "closed"}, "field", "operator")
                outcomes["add"] = "ok"
            except Exception as exc:  # noqa: BLE001
                outcomes["add"] = exc

        def close():
            try:
                self.service.transition(current["id"], "closed", version,
                                        "director", "director")
                outcomes["close"] = "ok"
            except ConflictError as exc:
                outcomes["close"] = exc

        threads = [threading.Thread(target=supplement),
                   threading.Thread(target=close)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes["add"], "ok")
        final = self.service.get_item(current["id"], "viewer")
        if outcomes["close"] == "ok":
            # 关闭先于补材料落库：材料属于关闭之后，状态须一致
            self.assertEqual(final["status"], "closed")
        else:
            # 补材料先落库：关闭必须被409拦下，不能漏掉新材料
            self.assertIsInstance(outcomes["close"], ConflictError)
            self.assertEqual(final["status"], "inspection")
            self.assertFalse(final["can_close"])


if __name__ == "__main__":
    unittest.main()
