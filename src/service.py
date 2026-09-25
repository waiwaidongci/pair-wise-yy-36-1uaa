from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CONFIRM_ROLES, CREATE_ROLES, ENTITY,
                    RECORD_ROLES, REVIEW_STATE, TERMINAL_STATES, TITLE,
                    VIEW_ROLES, completion_blockers, confirmation_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        closing = target in TERMINAL_STATES
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if closing:
            blockers += self._confirmation_blockers(item_id)
        if blockers:
            raise ConflictError("；".join(blockers))
        guard = self._confirmation_guard(item_id) if closing else None
        updated = self.repository.transition_item(item_id, target, expected_version,
                                                  actor, guard=guard)
        detail = {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        }
        if closing:
            confirmation = self.repository.latest_confirmation(item_id)
            detail["confirmation_batch"] = confirmation["id"] if confirmation else None
        self.repository.append_audit("transition", ENTITY, item_id, actor, detail)
        return self.enrich(updated)

    def confirm_review(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != REVIEW_STATE:
            raise ConflictError("仅复查中的事件可以进行复查确认")
        confirmation, created = self.repository.create_confirmation(item_id, actor)
        if created:
            self.repository.append_audit("confirm_review", ENTITY, item_id, actor, {
                "batch": confirmation["id"],
                "item_version": confirmation["item_version"],
                "record_count": confirmation["record_count"],
                "record_fingerprint": confirmation["record_fingerprint"],
            })
        return {"confirmation": confirmation, "created": created}

    def _confirmation_blockers(self, item_id: int) -> list:
        item = self.repository.get_item(item_id)
        record_ids = [record["id"] for record in self.repository.list_records(item_id)]
        confirmation = self.repository.latest_confirmation(item_id)
        return confirmation_blockers(item, record_ids, confirmation)

    def _confirmation_guard(self, item_id: int):
        def guard() -> None:
            blockers = self._confirmation_blockers(item_id)
            if blockers:
                raise ConflictError("；".join(blockers))
        return guard

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        open_records = self.repository.open_record_count(item["id"])
        record_ids = [r["id"] for r in self.repository.list_records(item["id"])]
        confirmation = self.repository.latest_confirmation(item["id"])
        result["open_records"] = open_records
        result["latest_confirmation_batch"] = confirmation["id"] if confirmation else None
        blockers: list = []
        if item["status"] == REVIEW_STATE:
            blockers = completion_blockers("closed", open_records)
            blockers += confirmation_blockers(item, record_ids, confirmation)
        elif item["status"] not in TERMINAL_STATES:
            blockers = ["事件尚未进入复查，不能关闭"]
        result["close_blockers"] = blockers
        result["can_close"] = item["status"] == REVIEW_STATE and not blockers
        return result
