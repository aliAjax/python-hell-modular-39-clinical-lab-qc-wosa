from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "handover":
            return self._handover_transition(actor, entity, action, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _handover_transition(self, actor, entity, action, data, expected_version):
        if action == "confirm" and entity["status"] == "confirmed":
            # Idempotent retry of an already-confirmed handover.
            return entity
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        if action == "record_trial":
            noop = bool(patch.pop("_noop", False))
            if noop:
                # Retry of the same trial recording: nothing to write, no audit.
                return entity
            merged = dict(entity["data"])
            merged.update(patch)
            updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
            self.audit.record(
                entity["id"], actor, action, entity["status"], updated["status"],
                {"instrument_id": data.get("instrument_id"), "qc_run_id": data.get("qc_run_id")},
            )
            return updated
        if action == "confirm":
            now = utcnow()
            handover_data = dict(entity["data"])
            handover_data.update(patch)
            handover_data["confirmed_at"] = now
            handover_data["confirmed_by"] = actor.user_id
            new_lot_id = entity["data"].get("new_lot_id")
            old_lot_id = entity["data"].get("old_lot_id")
            new_lot = self.repository.get_entity(new_lot_id)
            old_lot = self.repository.get_entity(old_lot_id)
            new_lot_data = dict(new_lot["data"])
            new_lot_data["replaces_lot_id"] = old_lot_id
            new_lot_data["switched_at"] = now
            lot_updates = [
                (new_lot_id, "active", new_lot_data),
                (old_lot_id, "retired", dict(old_lot["data"])),
            ]
            batch_updates = []
            for batch in self._lookup("result_batch", "assay_id", entity["data"].get("assay_id")) or []:
                if batch["status"] == "released":
                    # Already released batches keep the old lot as history.
                    continue
                if batch["data"].get("qc_lot_id") == old_lot_id:
                    batch_data = dict(batch["data"])
                    batch_data["qc_lot_id"] = new_lot_id
                    batch_updates.append((batch["id"], batch_data))
            try:
                updated = self.repository.confirm_handover(
                    entity["id"], expected, next_status, handover_data, lot_updates, batch_updates
                )
            except ConflictError:
                current = self.repository.get_entity(entity["id"])
                unfinished = [
                    trial for trial in current["data"].get("trials", [])
                    if trial.get("status") != "passed"
                ]
                raise ConflictError(
                    "version conflict: handover was modified by another supervisor",
                    details={
                        "handover_id": current["id"],
                        "current_version": current["version"],
                        "unfinished": unfinished,
                    },
                )
            self.audit.record(
                entity["id"], actor, action, entity["status"], updated["status"],
                {
                    "new_lot_id": new_lot_id,
                    "old_lot_id": old_lot_id,
                    "confirmed_at": now,
                    "repointed_batches": len(batch_updates),
                },
            )
            return updated
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity["id"], expected, next_status, merged)
        self.audit.record(
            entity["id"], actor, action, entity["status"], updated["status"],
            {"patch": patch},
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
