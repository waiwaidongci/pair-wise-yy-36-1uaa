from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    REVIEW_CONFIRM_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, is_review_state, priority_score,
                    response_deadline_hours, review_blockers, role_for_transition,
                    same_confirmation_snapshot, validate_transition)


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
        if target == "closed":
            # 预校验给出可读的409原因；repository.close_item在事务内做权威判定，
            # 防止补材料与关闭并发时漏掉新材料。
            blockers = self._close_blockers(item)
            if blockers:
                raise ConflictError("；".join(blockers))
            updated = self.repository.close_item(item_id, expected_version, actor)
        else:
            blockers = completion_blockers(target, self.repository.open_record_count(item_id))
            if blockers:
                raise ConflictError("；".join(blockers))
            updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def confirm_review(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        """合规员在复查阶段按当前全部材料编号与事件版本做确认。
        同一批材料重复确认不新增批次；材料或版本变化后确认生成新批次。"""
        ensure_role(role, REVIEW_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if not is_review_state(item["status"]):
            raise ConflictError("仅复查阶段可以做复查确认")
        record_ids = self.repository.all_record_ids(item_id)
        version = item["version"]
        latest = self.repository.latest_review_confirmation(item_id)
        if latest is not None and same_confirmation_snapshot(
                record_ids, version, latest["record_ids"], latest["item_version"]):
            return {"created": False, "batch_no": latest["batch_no"],
                    "item_version": version, "record_ids": record_ids,
                    "created_by": latest["created_by"], "created_at": latest["created_at"]}
        confirmation = self.repository.save_review_confirmation(
            item_id, version, record_ids, actor)
        self.repository.append_audit("review_confirm", ENTITY, item_id, actor, {
            "batch_no": confirmation["batch_no"], "item_version": version,
            "record_count": len(record_ids),
        })
        return {"created": True, "batch_no": confirmation["batch_no"],
                "item_version": version, "record_ids": record_ids,
                "created_by": actor, "created_at": confirmation["created_at"]}

    def _close_blockers(self, item: Dict[str, Any]) -> list:
        record_rows = self.repository.list_records(item["id"])
        open_records = sum(1 for r in record_rows if r["status"] == "open")
        latest = self.repository.latest_review_confirmation(item["id"])
        return review_blockers(
            item["status"], open_records, [r["id"] for r in record_rows],
            item["version"],
            latest["record_ids"] if latest else None,
            latest["item_version"] if latest else None)

    def _review_info(self, item: Dict[str, Any]) -> Dict[str, Any]:
        record_rows = self.repository.list_records(item["id"])
        latest = self.repository.latest_review_confirmation(item["id"])
        blockers = review_blockers(
            item["status"],
            sum(1 for r in record_rows if r["status"] == "open"),
            [r["id"] for r in record_rows], item["version"],
            latest["record_ids"] if latest else None,
            latest["item_version"] if latest else None)
        return {
            "can_close": not blockers,
            "close_blockers": blockers,
            "latest_batch_no": latest["batch_no"] if latest else None,
            "confirmed_item_version": latest["item_version"] if latest else None,
            "confirmed_record_count": len(latest["record_ids"]) if latest else 0,
            "confirmed_by": latest["created_by"] if latest else None,
            "confirmed_at": latest["created_at"] if latest else None,
        }

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        item = self.repository.get_item(item_id)
        result = self.enrich(item)
        result["review"] = self._review_info(item)
        return result

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
