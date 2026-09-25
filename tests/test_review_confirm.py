import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request

from src.domain import ConflictError, PermissionDenied
from src.http_api import make_handler
from src.repository import Repository
from src.rules import STATES
from src.service import Service


def advance_to_review(service, item_id):
    """把事件推进到复查阶段并返回复查态事件（所有整改项均已关闭）。"""
    current = service.get_item(item_id, "viewer")
    for target in STATES[1:-1]:
        role = "compliance_officer" if target in ("assessing", "inspection") else "operator"
        current = service.transition(current["id"], target, current["version"], "reviewer", role)
    return current


class ReviewConfirmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item({
            "title": "review item", "description": "review confirmation flow",
            "severity": "exceedance", "quantity": 12, "threshold": 6,
            "external_ref": "REV-1",
        }, "creator", "operator")
        self.service.add_record(self.item["id"], {
            "kind": "evidence", "detail": "base material", "status": "closed",
            "external_ref": "EV-1",
        }, "recorder", "operator")
        self.review = advance_to_review(self.service, self.item["id"])

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_close_blocked_before_confirmation_and_open_after(self):
        detail = self.service.get_item(self.item["id"], "viewer")
        self.assertFalse(detail["review"]["can_close"])
        self.assertIn("复查确认", detail["review"]["close_blockers"][0])
        self.assertIsNone(detail["review"]["latest_batch_no"])
        with self.assertRaises(ConflictError):
            self.service.transition(self.item["id"], "closed", self.review["version"],
                                    "boss", "director")
        confirmation = self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        self.assertTrue(confirmation["created"])
        self.assertEqual(confirmation["batch_no"], 1)
        self.assertEqual(len(confirmation["record_ids"]), 1)
        self.assertEqual(confirmation["item_version"], self.review["version"])
        detail = self.service.get_item(self.item["id"], "viewer")
        self.assertTrue(detail["review"]["can_close"])
        self.assertEqual(detail["review"]["latest_batch_no"], 1)
        closed = self.service.transition(self.item["id"], "closed", self.review["version"],
                                         "boss", "director")
        self.assertEqual(closed["status"], "closed")

    def test_new_material_invalidates_confirmation_and_close_returns_409(self):
        self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        new_record = self.service.add_record(self.item["id"], {
            "kind": "supplement", "detail": "later material", "status": "closed",
            "external_ref": "EV-2",
        }, "recorder", "operator")
        detail = self.service.get_item(self.item["id"], "viewer")
        self.assertFalse(detail["review"]["can_close"])
        self.assertTrue(any("重新确认" in b for b in detail["review"]["close_blockers"]))
        self.assertEqual(detail["review"]["latest_batch_no"], 1)
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.item["id"], "closed", self.review["version"],
                                    "boss", "director")
        self.assertIn("重新确认", str(ctx.exception))
        # 新材料编号确实出现在重新确认的快照里
        again = self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        self.assertTrue(again["created"])
        self.assertEqual(again["batch_no"], 2)
        self.assertIn(new_record["id"], again["record_ids"])
        closed = self.service.transition(self.item["id"], "closed", self.review["version"],
                                         "boss", "director")
        self.assertEqual(closed["status"], "closed")

    def test_repeated_confirmation_of_same_batch_does_not_add_batch(self):
        first = self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        second = self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["batch_no"], second["batch_no"])
        self.assertEqual(self.service.get_item(self.item["id"], "viewer")["review"]["latest_batch_no"], 1)
        review_audits = [e for e in self.service.audit("viewer", self.item["id"])
                         if e["action"] == "review_confirm"]
        self.assertEqual(len(review_audits), 1)

    def test_only_compliance_officer_can_confirm(self):
        with self.assertRaises(PermissionDenied):
            self.service.confirm_review(self.item["id"], "boss", "director")
        with self.assertRaises(PermissionDenied):
            self.service.confirm_review(self.item["id"], "op", "operator")

    def test_confirmation_only_in_review_state(self):
        early = advance_to_review  # 仅用于可读性
        del early
        fresh = self.service.create_item({
            "title": "early", "description": "not in review", "severity": "watch",
            "quantity": 1, "threshold": 10, "external_ref": "REV-2",
        }, "creator", "operator")
        with self.assertRaises(ConflictError):
            self.service.confirm_review(fresh["id"], "co", "compliance_officer")

    def test_closed_item_rejects_more_material(self):
        self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        self.service.transition(self.item["id"], "closed", self.review["version"],
                                "boss", "director")
        with self.assertRaises(ConflictError):
            self.service.add_record(self.item["id"], {
                "kind": "supplement", "detail": "too late", "status": "closed",
                "external_ref": "EV-LATE",
            }, "recorder", "operator")

    def test_concurrent_supplement_and_close_never_loses_new_material(self):
        self.service.confirm_review(self.item["id"], "co", "compliance_officer")
        version = self.review["version"]
        outcomes = {"closed": 0, "conflict": 0}
        barrier = threading.Barrier(2)

        def close():
            barrier.wait()
            try:
                self.service.transition(self.item["id"], "closed", version,
                                        "boss", "director")
                outcomes["closed"] += 1
            except ConflictError:
                outcomes["conflict"] += 1

        def supplement():
            barrier.wait()
            self.service.add_record(self.item["id"], {
                "kind": "supplement", "detail": "racing material", "status": "closed",
                "external_ref": "EV-RACE",
            }, "recorder", "operator")

        t1 = threading.Thread(target=close)
        t2 = threading.Thread(target=supplement)
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(outcomes["closed"] + outcomes["conflict"], 1)
        if outcomes["closed"]:
            self.assertEqual(self.service.get_item(self.item["id"], "viewer")["status"], "closed")
        else:
            # 补材料先落库：关闭必须被拒绝并要求重新确认
            self.assertIn("重新确认", self.service.get_item(self.item["id"], "viewer")
                          ["review"]["close_blockers"][0])


class ReviewConfirmHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "http.db"))
        service = Service(self.repo)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service, static_dir))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        item = service.create_item({
            "title": "http review", "description": "http", "severity": "watch",
            "quantity": 1, "threshold": 10, "external_ref": "HTTP-1",
        }, "creator", "operator")
        service.add_record(item["id"], {
            "kind": "evidence", "detail": "base", "status": "closed",
            "external_ref": "HEV-1",
        }, "recorder", "operator")
        self.review = advance_to_review(service, item["id"])
        self.item_id = item["id"]
        self.service = service

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.repo.close()
        self.tmp.cleanup()

    def _request(self, method, path, role, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                              headers={"X-Actor": "tester", "X-Role": role,
                                       "Content-Type": "application/json"}, method=method)
        try:
            with request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_http_confirmation_and_409_close(self):
        status, _ = self._request("POST", f"/api/items/{self.item_id}/review-confirmations",
                                  "compliance_officer")
        self.assertEqual(status, 201)
        status, payload = self._request("POST", f"/api/items/{self.item_id}/records",
                                        "operator",
                                        {"kind": "supplement", "detail": "new",
                                         "status": "closed", "external_ref": "HEV-2"})
        self.assertEqual(status, 201)
        status, payload = self._request("POST", f"/api/items/{self.item_id}/transition",
                                        "director",
                                        {"target": "closed", "expected_version": self.review["version"]})
        self.assertEqual(status, 409)
        self.assertIn("重新确认", payload["message"])
        status, detail = self._request("GET", f"/api/items/{self.item_id}", "viewer")
        self.assertEqual(status, 200)
        self.assertFalse(detail["review"]["can_close"])
        self.assertEqual(detail["review"]["latest_batch_no"], 1)
        # 同批重复确认返回200且不新增批次
        status, again = self._request("POST", f"/api/items/{self.item_id}/review-confirmations",
                                      "compliance_officer")
        self.assertEqual(status, 201)  # 上一步已新增材料，本次属于新批次
        status, duplicate = self._request("POST", f"/api/items/{self.item_id}/review-confirmations",
                                          "compliance_officer")
        self.assertEqual(status, 200)
        self.assertFalse(duplicate["created"])
        self.assertEqual(duplicate["batch_no"], again["batch_no"])


if __name__ == "__main__":
    unittest.main()
