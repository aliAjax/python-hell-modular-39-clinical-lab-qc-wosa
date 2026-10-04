import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TakeoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "takeover.db"),
            RuleEngine(),
        )
        self.supervisor = Actor("sup-1", "supervisor")
        self.supervisor2 = Actor("sup-2", "supervisor")
        self.operator = Actor("op-1", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _assay_and_lots(self):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {"name": "Glucose", "unit": "mmol/L", "allowed_low": 3.9, "allowed_high": 6.1},
        )
        old_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-OLD", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        old_lot = self.service.transition(self.supervisor, old_lot["id"], "activate", {"activated_by": "sup-1"})
        new_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-NEW", "target": 5.05, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        return assay, old_lot, new_lot

    def _instrument(self, name, serial):
        return self.service.create(
            self.supervisor,
            "instrument",
            {"name": name, "serial": serial, "calibration_due": "2099-01-01"},
        )

    def _run(self, assay, lot, instrument, value, at="2026-10-04T08:00:00Z", actor=None):
        run = self.service.create(
            actor or self.operator,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": at,
            },
        )
        return self.service.transition(
            actor or self.operator, run["id"], "evaluate", {"evaluated_by": "op-1"}
        )

    def _trial(self, takeover, instrument, run, actor=None, idem=None):
        return self.service.transition(
            actor or self.operator,
            takeover["id"],
            "record_trial",
            {"instrument_id": instrument["id"], "qc_run_id": run["id"]},
            idempotency_key=idem,
        )

    def _batch(self, assay, lot, instrument, at, patient_count=3, actor=None):
        run = self._run(assay, lot, instrument, 5.0, at=at, actor=actor)
        return self.service.create(
            actor or self.operator,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": at,
                "patient_count": patient_count,
            },
        )

    def test_full_takeover_flips_lots_and_reassigns_unreleased_batches_only(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst_a = self._instrument("A", "S-A")
        inst_b = self._instrument("B", "S-B")

        released_batch = self._batch(assay, old_lot, inst_a, "2026-10-03T08:00:00Z")
        released_batch = self.service.transition(
            self.supervisor, released_batch["id"], "release", {"reviewer_id": "sup-1"}
        )
        waiting_batch = self._batch(assay, old_lot, inst_b, "2026-10-04T07:00:00Z")

        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst_a["id"], inst_b["id"]],
            },
        )
        self.assertEqual(takeover["status"], "trialing")

        run_a = self._run(assay, new_lot, inst_a, 5.02, at="2026-10-04T09:00:00Z")
        takeover = self._trial(takeover, inst_a, run_a)
        self.assertEqual(takeover["status"], "trialing")

        run_b = self._run(assay, new_lot, inst_b, 5.04, at="2026-10-04T09:05:00Z")
        takeover = self._trial(takeover, inst_b, run_b)
        self.assertEqual(takeover["status"], "ready")

        # Cannot take over while using a stale version.
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(
                self.supervisor2, takeover["id"], "confirm", {}, expected_version=1
            )
        self.assertEqual(ctx.exception.details["current_version"], takeover["version"])

        takeover = self.service.transition(self.supervisor, takeover["id"], "confirm", {})
        self.assertEqual(takeover["status"], "confirmed")
        self.assertTrue(takeover["data"].get("confirmed_at"))
        self.assertEqual(takeover["data"].get("confirmed_by"), "sup-1")

        active = self.service.list("qc_lot", status="active")
        self.assertEqual([lot["id"] for lot in active], [new_lot["id"]])
        old = self.service.get(old_lot["id"])
        self.assertEqual(old["status"], "retired")

        # Released batch stays on the old lot; unreleased batch moved to new lot.
        released = self.service.get(released_batch["id"])
        waiting = self.service.get(waiting_batch["id"])
        self.assertEqual(self.service.effective_lot_id(released), old_lot["id"])
        self.assertEqual(self.service.effective_lot_id(waiting), new_lot["id"])
        self.assertIn(waiting_batch["id"], takeover["data"]["reassigned_batch_ids"])
        self.assertNotIn(released_batch["id"], takeover["data"]["reassigned_batch_ids"])

        # New batches after takeover are stamped with the new lot.
        later = self._batch(assay, new_lot, inst_a, "2026-10-04T10:00:00Z")
        self.assertEqual(self.service.effective_lot_id(self.service.get(later["id"])), new_lot["id"])

    def test_any_instrument_failing_keeps_old_lot_and_blocks_confirm(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst_a = self._instrument("A", "S-A")
        inst_b = self._instrument("B", "S-B")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst_a["id"], inst_b["id"]],
            },
        )
        run_a = self._run(assay, new_lot, inst_a, 5.01, at="2026-10-04T09:00:00Z")
        takeover = self._trial(takeover, inst_a, run_a)
        bad_b = self._run(assay, new_lot, inst_b, 5.9, at="2026-10-04T09:05:00Z")
        self.assertEqual(bad_b["status"], "rejected")
        takeover = self._trial(takeover, inst_b, bad_b)
        self.assertEqual(takeover["status"], "failed")

        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(self.supervisor, takeover["id"], "confirm", {})
        # The late armer sees the unfinished instruments and the latest version.
        self.assertEqual(ctx.exception.details["pending_instrument_ids"], [inst_b["id"]])
        self.assertEqual(ctx.exception.details["current_version"], takeover["version"])

        self.assertEqual(self.service.get(old_lot["id"])["status"], "active")
        self.assertEqual(self.service.get(new_lot["id"])["status"], "registered")
        # A failed takeover is terminal; a new attempt needs a new order.
        with self.assertRaises(InvalidTransition):
            self._trial(takeover, inst_b, bad_b)

    def test_concurrent_confirmations_only_one_succeeds(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst["id"]],
            },
        )
        run = self._run(assay, new_lot, inst, 5.03, at="2026-10-04T09:00:00Z")
        takeover = self._trial(takeover, inst, run)
        version = takeover["version"]

        barrier = threading.Barrier(2)
        outcomes = []

        def confirm(actor):
            barrier.wait()
            try:
                result = self.service.transition(
                    actor, takeover["id"], "confirm", {}, expected_version=version
                )
                outcomes.append(("ok", result["status"], actor.user_id))
            except ConflictError as exc:
                outcomes.append(("conflict", exc.details, actor.user_id))

        t1 = threading.Thread(target=confirm, args=(self.supervisor,))
        t2 = threading.Thread(target=confirm, args=(self.supervisor2,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [item for item in outcomes if item[0] == "ok"]
        failures = [item for item in outcomes if item[0] == "conflict"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(successes[0][1], "confirmed")
        self.assertIn("current_version", failures[0][1])
        # Only one set of takeover side effects.
        takeover_audits = [
            row for row in self.service.audit_log(takeover["id"]) if row["action"] == "confirm"
        ]
        self.assertEqual(len(takeover_audits), 1)
        retire_audits = [
            row for row in self.service.audit_log(old_lot["id"]) if row["action"] == "retire"
        ]
        self.assertEqual(len(retire_audits), 1)

    def test_trial_write_retry_does_not_double_occupy_slot_or_audit(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst["id"]],
            },
        )
        run = self._run(assay, new_lot, inst, 5.02, at="2026-10-04T09:00:00Z")

        first = self._trial(takeover, inst, run, idem="trial-retry-1")
        # Same client write retried against the takeover order: replay returns the
        # same slot without a new audit row.
        second = self._trial(takeover, inst, run, idem="trial-retry-1")
        self.assertEqual(first["version"], second["version"])
        audits = self.service.audit_log(takeover["id"])
        self.assertEqual(len([row for row in audits if row["action"] == "record_trial"]), 1)

        # A different run cannot steal the occupied instrument slot.
        other_run = self._run(assay, new_lot, inst, 5.01, at="2026-10-04T09:10:00Z")
        with self.assertRaises(ConflictError) as ctx:
            self._trial(first, inst, other_run)
        self.assertEqual(ctx.exception.details["recorded_qc_run_id"], run["id"])

    def test_confirm_retry_with_same_key_is_a_noop(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst["id"]],
            },
        )
        run = self._run(assay, new_lot, inst, 5.02, at="2026-10-04T09:00:00Z")
        takeover = self._trial(takeover, inst, run)

        confirmed = self.service.transition(
            self.supervisor, takeover["id"], "confirm", {}, idempotency_key="confirm-1"
        )
        self.assertEqual(confirmed["status"], "confirmed")
        # Retried write after partial failure: no exception, no duplicate effects.
        replayed = self.service.transition(
            self.supervisor, takeover["id"], "confirm", {}, idempotency_key="confirm-1"
        )
        self.assertEqual(replayed["id"], confirmed["id"])
        audits = self.service.audit_log(takeover["id"])
        self.assertEqual(len([row for row in audits if row["action"] == "confirm"]), 1)

    def test_loser_retry_after_rival_confirm_converges_without_side_effects(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst["id"]],
            },
        )
        run = self._run(assay, new_lot, inst, 5.02, at="2026-10-04T09:00:00Z")
        takeover = self._trial(takeover, inst, run)
        stale = takeover["version"]

        winner = self.service.transition(
            self.supervisor, takeover["id"], "confirm", {}, expected_version=stale
        )
        self.assertEqual(winner["status"], "confirmed")
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.supervisor2, takeover["id"], "confirm", {}, expected_version=stale
            )
        # Loser retries the write against the takeover order (fresh state): it
        # converges to confirmed without a second audit trail.
        retried = self.service.transition(self.supervisor2, takeover["id"], "confirm", {})
        self.assertEqual(retried["status"], "confirmed")
        self.assertEqual(
            len([row for row in self.service.audit_log(takeover["id"]) if row["action"] == "confirm"]),
            1,
        )

    def test_legacy_data_without_takeover_is_unmanaged_and_still_queryable(self):
        assay, old_lot, _ = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        # Legacy batch stamped at creation through its QC run's lot.
        batch = self._batch(assay, old_lot, inst, "2026-09-01T08:00:00Z")
        self.assertEqual(self.service.effective_lot_id(batch), old_lot["id"])

        # Simulate truly legacy rows (created before stamping existed).
        batch["data"].pop("qc_lot_id", None)
        self.service.repository.update_entity(batch["id"], batch["version"], batch["status"], batch["data"])
        unstamped = self.service.get(batch["id"])
        self.assertEqual(self.service.effective_lot_id(unstamped), old_lot["id"])

        # Historical QC results and the original lot remain queryable.
        run = self.service.get(unstamped["data"]["qc_run_id"])
        self.assertEqual(run["data"]["qc_lot_id"], old_lot["id"])
        self.assertEqual(self.service.get(old_lot["id"])["data"]["lot_no"], "LOT-OLD")
        self.assertEqual(self.service.list("takeover"), [])

    def test_takeover_order_references_results_and_instruments(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst_a = self._instrument("A", "S-A")
        inst_b = self._instrument("B", "S-B")
        batch = self._batch(assay, old_lot, inst_a, "2026-10-04T06:00:00Z")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst_a["id"], inst_b["id"]],
            },
        )
        view = self.service.takeover_view(takeover["id"])
        self.assertEqual(view["data"]["assay"]["id"], assay["id"])
        self.assertEqual(view["data"]["previous_lot"]["id"], old_lot["id"])
        self.assertEqual(view["data"]["new_lot"]["id"], new_lot["id"])
        referenced = {slot["instrument"]["id"] for slot in view["data"]["slot_details"]}
        self.assertEqual(referenced, {inst_a["id"], inst_b["id"]})
        self.assertEqual(view["data"]["pending_instrument_ids"], [inst_a["id"], inst_b["id"]])
        batch_ids = {item["id"] for item in view["data"]["result_batches"]}
        self.assertIn(batch["id"], batch_ids)

    def test_takeover_requires_supervisor_and_unique_instruments(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst = self._instrument("A", "S-A")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.operator,
                "takeover",
                {
                    "assay_id": assay["id"],
                    "previous_lot_id": old_lot["id"],
                    "new_lot_id": new_lot["id"],
                    "instrument_ids": [inst["id"]],
                },
            )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.supervisor,
                "takeover",
                {
                    "assay_id": assay["id"],
                    "previous_lot_id": old_lot["id"],
                    "new_lot_id": new_lot["id"],
                    "instrument_ids": [inst["id"], inst["id"]],
                },
            )

    def test_trial_must_use_new_lot_and_named_instrument(self):
        assay, old_lot, new_lot = self._assay_and_lots()
        inst_a = self._instrument("A", "S-A")
        inst_b = self._instrument("B", "S-B")
        takeover = self.service.create(
            self.supervisor,
            "takeover",
            {
                "assay_id": assay["id"],
                "previous_lot_id": old_lot["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [inst_a["id"]],
            },
        )
        wrong_run = self._run(assay, old_lot, inst_a, 5.0, at="2026-10-04T09:00:00Z")
        with self.assertRaises(ValidationError):
            self._trial(takeover, inst_a, wrong_run)
        good_run = self._run(assay, new_lot, inst_a, 5.02, at="2026-10-04T09:05:00Z")
        with self.assertRaises(ValidationError):
            self._trial(takeover, inst_b, good_run)


if __name__ == "__main__":
    unittest.main()
