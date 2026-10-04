from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
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
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key, "create")
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
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, "create")
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None, idempotency_key=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "takeover":
            if action == "record_trial":
                return self._record_trial(
                    actor, entity, dict(data or {}), expected_version, idempotency_key
                )
            if action == "confirm":
                return self._confirm_takeover(actor, entity, expected_version, idempotency_key)
            raise InvalidTransition("unknown action %s for takeover" % action)

        payload = dict(data or {})
        if idempotency_key:
            replayed = self.repository.get_idempotency(actor.user_id, idempotency_key, action)
            if replayed == entity_id:
                # Same request retried after a failed write: return the current
                # state without occupying another slot or writing another audit.
                return self.repository.get_entity(entity_id)
            if replayed:
                raise ConflictError("idempotency key is already used for another request")

        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
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
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id, action)
        return updated

    # ------------------------------------------------------------------
    # Takeover order (接管单): parallel QC trial on every instrument, then an
    # atomic takeover at the confirmation instant.
    # ------------------------------------------------------------------

    def _record_trial(self, actor, takeover, data, expected_version, idempotency_key):
        self.rules.ensure_action_role("takeover", "record_trial", actor)
        if idempotency_key:
            replayed = self.repository.get_idempotency(actor.user_id, idempotency_key, "record_trial")
            if replayed == takeover["id"]:
                return self.takeover_view(takeover["id"])
            if replayed:
                raise ConflictError("idempotency key is already used for another request")
        self.rules.require_fields(data, ("instrument_id", "qc_run_id"))
        if takeover["status"] not in ("trialing", "ready"):
            raise InvalidTransition(
                "cannot record a trial on a takeover with status %s" % takeover["status"]
            )

        instrument_id = data["instrument_id"]
        qc_run_id = data["qc_run_id"]
        slots = [dict(slot) for slot in takeover["data"].get("slots", [])]
        slot = next((item for item in slots if item["instrument_id"] == instrument_id), None)
        if slot is None:
            raise ValidationError("instrument is not part of this takeover: " + str(instrument_id))

        run = self.repository.get_entity(qc_run_id)
        if not run or run["kind"] != "qc_run":
            raise ValidationError("qc run does not exist: " + str(qc_run_id))
        if run["data"].get("instrument_id") != instrument_id:
            raise ValidationError("qc run was produced by another instrument")
        if run["data"].get("assay_id") != takeover["data"].get("assay_id"):
            raise ValidationError("qc run belongs to another assay")
        if run["data"].get("qc_lot_id") != takeover["data"].get("new_lot_id"):
            raise ValidationError("trial qc run must use the new lot")
        if run["status"] not in ("accepted", "rejected"):
            raise ConflictError(
                "qc run must be evaluated before recording the trial",
                details={"qc_run_id": qc_run_id, "qc_run_status": run["status"]},
            )
        outcome = "passed" if run["status"] == "accepted" else "failed"

        if slot.get("status") == outcome and slot.get("qc_run_id") == qc_run_id:
            # Idempotent replay of the same trial: do not re-occupy the slot and
            # do not write another audit entry.
            return self._hydrate_takeover(takeover)
        if slot.get("status") in ("passed", "failed"):
            raise ConflictError(
                "instrument slot already has a recorded trial",
                details={
                    "instrument_id": instrument_id,
                    "recorded_qc_run_id": slot.get("qc_run_id"),
                    "slot_status": slot.get("status"),
                },
            )

        slot["status"] = outcome
        slot["qc_run_id"] = qc_run_id
        if any(item["status"] == "failed" for item in slots):
            next_status = "failed"
        elif all(item["status"] == "passed" for item in slots):
            next_status = "ready"
        else:
            next_status = "trialing"

        merged = dict(takeover["data"])
        merged["slots"] = slots
        expected = int(expected_version) if expected_version is not None else takeover["version"]
        updated = self.repository.update_entity(takeover["id"], expected, next_status, merged)
        self.audit.record(
            takeover["id"],
            actor,
            "record_trial",
            takeover["status"],
            updated["status"],
            {"instrument_id": instrument_id, "qc_run_id": qc_run_id, "outcome": outcome},
        )
        if idempotency_key:
            self.repository.save_idempotency(
                actor.user_id, idempotency_key, takeover["id"], "record_trial"
            )
        return self._hydrate_takeover(updated)

    def _confirm_takeover(self, actor, takeover, expected_version, idempotency_key):
        self.rules.ensure_action_role("takeover", "confirm", actor)
        if idempotency_key:
            replayed = self.repository.get_idempotency(actor.user_id, idempotency_key, "confirm")
            if replayed == takeover["id"]:
                return self.takeover_view(takeover["id"])
            if replayed:
                raise ConflictError("idempotency key is already used for another request")
        expected = int(expected_version) if expected_version is not None else takeover["version"]
        if expected != takeover["version"]:
            # A rival confirmation already moved the order: tell the late writer
            # the latest version so they reload instead of repeating the write.
            raise ConflictError(
                "version conflict: expected %s, found %s" % (expected, takeover["version"]),
                details={"current_version": takeover["version"], "status": takeover["status"]},
            )
        # A write that failed mid-way may be retried after another supervisor has
        # already completed the takeover. A retry reloaded against the current
        # order converges on the confirmed state without repeating any side effect.
        if takeover["status"] == "confirmed":
            if idempotency_key:
                self.repository.save_idempotency(
                    actor.user_id, idempotency_key, takeover["id"], "confirm"
                )
            return self.takeover_view(takeover["id"])
        updated = self.repository.confirm_takeover(
            takeover["id"], actor, expected_version=expected, idempotency_key=idempotency_key
        )
        return self._hydrate_takeover(updated)

    def effective_lot_id(self, batch):
        """Resolve the QC lot a patient result batch is bound to.

        Batches created after the takeover carry the stamp directly; batches
        released before it keep the old lot. Legacy rows without a stamp (no
        takeover order ever existed) resolve through their referenced QC run,
        so historical QC results and original lots stay queryable.
        """
        lot_id = batch["data"].get("qc_lot_id")
        if lot_id:
            return lot_id
        run = self.repository.get_entity(batch["data"].get("qc_run_id"))
        return run["data"].get("qc_lot_id") if run else None

    def takeover_view(self, takeover_id):
        entity = self.repository.get_entity(takeover_id)
        if not entity or entity["kind"] != "takeover":
            raise NotFoundError("takeover not found: " + str(takeover_id))
        return self._hydrate_takeover(entity)

    def _hydrate_takeover(self, takeover):
        view = dict(takeover)
        data = dict(takeover["data"])
        view["data"] = data

        def reference(entity_id):
            entity = self.repository.get_entity(entity_id) if entity_id else None
            if not entity:
                return {"id": entity_id, "missing": True}
            return {"id": entity["id"], "kind": entity["kind"], "status": entity["status"], "data": entity["data"]}

        data["assay"] = reference(data.get("assay_id"))
        data["previous_lot"] = reference(data.get("previous_lot_id"))
        data["new_lot"] = reference(data.get("new_lot_id"))
        slot_details = []
        for slot in data.get("slots", []):
            detail = dict(slot)
            detail["instrument"] = reference(slot.get("instrument_id"))
            detail["qc_run"] = reference(slot.get("qc_run_id"))
            slot_details.append(detail)
        data["slot_details"] = slot_details
        data["pending_instrument_ids"] = [
            slot["instrument_id"]
            for slot in data.get("slots", [])
            if slot.get("status") not in ("passed", "failed")
        ]

        batches = []
        for batch in self.repository.list_entities(kind="result_batch"):
            if batch["data"].get("assay_id") != data.get("assay_id"):
                continue
            batches.append(
                {
                    "id": batch["id"],
                    "status": batch["status"],
                    "instrument_id": batch["data"].get("instrument_id"),
                    "effective_lot_id": self.effective_lot_id(batch),
                }
            )
        data["result_batches"] = batches
        return view

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
