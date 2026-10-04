import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.migrate import run_migrate
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repository = SQLiteRepository(Path(self.tmp.name) / "handover.db")
        self.service = DomainService(self.repository, RuleEngine())
        self.supervisor = Actor("qc-supervisor", "supervisor")
        self.operator = Actor("qc-operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup(self, instrument_count=2):
        assay = self.service.create(
            self.supervisor,
            "assay",
            {
                "name": "Glucose",
                "unit": "mmol/L",
                "allowed_low": 3.9,
                "allowed_high": 6.1,
                "rule_config": {"limit_sd": 3, "trend_n": 4, "consecutive_n": 4},
            },
        )
        old_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-1", "target": 5.0, "sd": 0.1, "expires_at": "2099-01-01"},
        )
        old_lot = self.service.transition(self.supervisor, old_lot["id"], "activate", {"activated_by": "qc-1"})
        new_lot = self.service.create(
            self.supervisor,
            "qc_lot",
            {"assay_id": assay["id"], "lot_no": "LOT-2", "target": 5.1, "sd": 0.1, "expires_at": "2099-06-01"},
        )
        instruments = []
        for index in range(instrument_count):
            instruments.append(
                self.service.create(
                    self.supervisor,
                    "instrument",
                    {
                        "name": "Analyzer %s" % (index + 1),
                        "serial": "A-%s" % (index + 1),
                        "calibration_due": "2099-01-01",
                    },
                )
            )
        return assay, old_lot, new_lot, instruments

    def _trial_run(self, assay, lot, instrument, value):
        run = self.service.create(
            self.supervisor,
            "qc_run",
            {
                "assay_id": assay["id"],
                "qc_lot_id": lot["id"],
                "instrument_id": instrument["id"],
                "value": value,
                "run_at": "2026-09-27T08:00:00Z",
            },
        )
        return self.service.transition(self.supervisor, run["id"], "evaluate", {"evaluated_by": "qc-1"})

    def _patient_batch(self, assay, lot, instrument, status="waiting"):
        run = self._trial_run(assay, lot, instrument, 5.02)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instrument["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T08:05:00Z",
                "patient_count": 12,
            },
        )
        if status == "released":
            batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        return batch

    def _open_handover(self, assay, new_lot, instruments):
        return self.service.create(
            self.supervisor,
            "handover",
            {
                "assay_id": assay["id"],
                "new_lot_id": new_lot["id"],
                "instrument_ids": [instrument["id"] for instrument in instruments],
            },
        )

    def _record_all_trials(self, handover, assay, new_lot, instruments, value=5.1):
        for instrument in instruments:
            run = self._trial_run(assay, new_lot, instrument, value)
            self.service.transition(
                self.supervisor,
                handover["id"],
                "record_trial",
                {"instrument_id": instrument["id"], "qc_run_id": run["id"]},
            )
        return self.service.get(handover["id"])

    def test_handover_create_and_confirm_flow(self):
        assay, old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        self.assertEqual(handover["status"], "open")
        self.assertEqual(handover["data"]["old_lot_id"], old_lot["id"])
        self.assertEqual(handover["data"]["new_lot_id"], new_lot["id"])
        self.assertEqual(len(handover["data"]["trials"]), 2)
        self.assertTrue(all(trial["status"] == "pending" for trial in handover["data"]["trials"]))

        handover = self._record_all_trials(handover, assay, new_lot, instruments)
        self.assertTrue(all(trial["status"] == "passed" for trial in handover["data"]["trials"]))

        confirmed = self.service.transition(self.supervisor, handover["id"], "confirm", {})
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIsNotNone(confirmed["data"]["confirmed_at"])
        self.assertEqual(confirmed["data"]["confirmed_by"], "qc-supervisor")
        self.assertEqual(self.service.get(new_lot["id"])["status"], "active")
        self.assertEqual(self.service.get(old_lot["id"])["status"], "retired")

    def test_confirm_blocked_when_any_trial_fails(self):
        assay, old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        good = self._trial_run(assay, new_lot, instruments[0], 5.1)
        bad = self._trial_run(assay, new_lot, instruments[1], 9.9)
        self.service.transition(
            self.supervisor, handover["id"], "record_trial",
            {"instrument_id": instruments[0]["id"], "qc_run_id": good["id"]},
        )
        self.service.transition(
            self.supervisor, handover["id"], "record_trial",
            {"instrument_id": instruments[1]["id"], "qc_run_id": bad["id"]},
        )
        with self.assertRaises(ConflictError) as context:
            self.service.transition(self.supervisor, handover["id"], "confirm", {})
        details = context.exception.details
        current = self.service.get(handover["id"])
        self.assertEqual(details["current_version"], current["version"])
        self.assertEqual(len(details["unfinished"]), 1)
        self.assertEqual(details["unfinished"][0]["instrument_id"], instruments[1]["id"])
        self.assertEqual(details["unfinished"][0]["status"], "failed")
        # Old lot stays active, new lot stays registered, handover stays open.
        self.assertEqual(self.service.get(old_lot["id"])["status"], "active")
        self.assertEqual(self.service.get(new_lot["id"])["status"], "registered")
        self.assertEqual(self.service.get(handover["id"])["status"], "open")

    def test_confirm_blocked_when_trial_pending(self):
        assay, _old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        good = self._trial_run(assay, new_lot, instruments[0], 5.1)
        self.service.transition(
            self.supervisor, handover["id"], "record_trial",
            {"instrument_id": instruments[0]["id"], "qc_run_id": good["id"]},
        )
        with self.assertRaises(ConflictError) as context:
            self.service.transition(self.supervisor, handover["id"], "confirm", {})
        self.assertEqual(len(context.exception.details["unfinished"]), 1)
        self.assertEqual(context.exception.details["unfinished"][0]["status"], "pending")

    def test_concurrent_confirm_only_one_wins(self):
        assay, _old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        self._record_all_trials(handover, assay, new_lot, instruments)
        start_version = self.service.get(handover["id"])["version"]

        barrier = threading.Barrier(2)
        outcomes = []

        def do_confirm():
            barrier.wait()
            try:
                outcomes.append(("ok", self.service.transition(self.supervisor, handover["id"], "confirm", {})))
            except ConflictError as exc:
                outcomes.append(("conflict", exc))

        first = threading.Thread(target=do_confirm)
        second = threading.Thread(target=do_confirm)
        first.start()
        second.start()
        first.join()
        second.join()

        # Exactly one confirm actually transitions; the loser either conflicts
        # (and sees the latest version) or idempotently sees the confirmed state.
        self.assertEqual(len(outcomes), 2)
        confirmed = self.service.get(handover["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["version"], start_version + 1)
        for kind, payload in outcomes:
            if kind == "conflict":
                self.assertGreaterEqual(payload.details["current_version"], start_version + 1)
            else:
                self.assertEqual(payload["status"], "confirmed")
        confirm_audits = [
            entry for entry in self.service.audit_log(handover["id"]) if entry["action"] == "confirm"
        ]
        self.assertEqual(len(confirm_audits), 1, "confirm must be audited exactly once")

    def test_unreleased_batches_repointed_released_kept(self):
        assay, old_lot, new_lot, instruments = self._setup()
        waiting = self._patient_batch(assay, old_lot, instruments[0], status="waiting")
        released = self._patient_batch(assay, old_lot, instruments[1], status="released")
        self.assertEqual(waiting["data"]["qc_lot_id"], old_lot["id"])
        self.assertEqual(released["data"]["qc_lot_id"], old_lot["id"])

        handover = self._open_handover(assay, new_lot, instruments)
        self._record_all_trials(handover, assay, new_lot, instruments)
        self.service.transition(self.supervisor, handover["id"], "confirm", {})

        self.assertEqual(self.service.get(waiting["id"])["data"]["qc_lot_id"], new_lot["id"])
        self.assertEqual(self.service.get(released["id"])["data"]["qc_lot_id"], old_lot["id"])
        self.assertEqual(self.service.get(released["id"])["status"], "released")

    def test_release_after_takeover_uses_new_lot(self):
        assay, old_lot, new_lot, instruments = self._setup()
        batch = self._patient_batch(assay, old_lot, instruments[0], status="waiting")
        handover = self._open_handover(assay, new_lot, instruments)
        self._record_all_trials(handover, assay, new_lot, instruments)
        self.service.transition(self.supervisor, handover["id"], "confirm", {})
        batch = self.service.get(batch["id"])
        self.assertEqual(batch["data"]["qc_lot_id"], new_lot["id"])
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(batch["status"], "released")

    def test_batch_created_after_takeover_uses_new_lot(self):
        assay, _old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        self._record_all_trials(handover, assay, new_lot, instruments)
        self.service.transition(self.supervisor, handover["id"], "confirm", {})
        run = self._trial_run(assay, new_lot, instruments[0], 5.1)
        batch = self.service.create(
            self.supervisor,
            "result_batch",
            {
                "assay_id": assay["id"],
                "instrument_id": instruments[0]["id"],
                "qc_run_id": run["id"],
                "run_at": "2026-09-27T09:00:00Z",
                "patient_count": 5,
            },
        )
        self.assertEqual(batch["data"]["qc_lot_id"], new_lot["id"])

    def test_record_trial_idempotent_no_duplicate_audit(self):
        assay, _old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        run = self._trial_run(assay, new_lot, instruments[0], 5.1)
        self.service.transition(
            self.supervisor, handover["id"], "record_trial",
            {"instrument_id": instruments[0]["id"], "qc_run_id": run["id"]},
        )
        # Retry the same write (e.g. after a lost response): no new slot/audit.
        self.service.transition(
            self.supervisor, handover["id"], "record_trial",
            {"instrument_id": instruments[0]["id"], "qc_run_id": run["id"]},
        )
        handover = self.service.get(handover["id"])
        self.assertEqual(len(handover["data"]["trials"]), 2)
        trial = next(t for t in handover["data"]["trials"] if t["instrument_id"] == instruments[0]["id"])
        self.assertEqual(trial["trial_run_id"], run["id"])
        self.assertEqual(trial["status"], "passed")
        audits = [entry for entry in self.service.audit_log(handover["id"]) if entry["action"] == "record_trial"]
        self.assertEqual(len(audits), 1)

    def test_confirm_idempotent_retry_no_duplicate_audit(self):
        assay, _old_lot, new_lot, instruments = self._setup()
        handover = self._open_handover(assay, new_lot, instruments)
        self._record_all_trials(handover, assay, new_lot, instruments)
        first = self.service.transition(self.supervisor, handover["id"], "confirm", {})
        second = self.service.transition(self.supervisor, handover["id"], "confirm", {})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["status"], "confirmed")
        audits = [entry for entry in self.service.audit_log(handover["id"]) if entry["action"] == "confirm"]
        self.assertEqual(len(audits), 1)

    def test_old_data_without_handover_releases_and_is_queryable(self):
        assay, old_lot, _new_lot, instruments = self._setup()
        batch = self._patient_batch(assay, old_lot, instruments[0], status="waiting")
        # No handover exists: the batch is treated as not taken over and releases
        # under its original lot.
        batch = self.service.transition(self.supervisor, batch["id"], "release", {"reviewer_id": "qc-2"})
        self.assertEqual(batch["status"], "released")
        self.assertEqual(batch["data"]["qc_lot_id"], old_lot["id"])
        # Historical QC results and original lot numbers remain queryable.
        history = self.service.list("qc_run")
        self.assertTrue(any(run["data"]["qc_lot_id"] == old_lot["id"] for run in history))
        self.assertEqual(self.service.get(old_lot["id"])["data"]["lot_no"], "LOT-1")

    def test_migration_backfills_missing_lot_without_handover(self):
        assay, old_lot, _new_lot, instruments = self._setup()
        batch = self._patient_batch(assay, old_lot, instruments[0], status="waiting")
        # Simulate a legacy batch created before the handover feature: it has no
        # qc_lot_id and no handover.
        legacy = dict(batch["data"])
        legacy.pop("qc_lot_id", None)
        self.repository.update_entity(batch["id"], batch["version"], batch["status"], legacy)
        self.assertIsNone(self.service.get(batch["id"])["data"].get("qc_lot_id"))

        changed = run_migrate(self.repository)
        self.assertGreaterEqual(changed, 1)
        migrated = self.service.get(batch["id"])
        self.assertEqual(migrated["data"]["qc_lot_id"], old_lot["id"])
        # Migration is idempotent.
        self.assertEqual(run_migrate(self.repository), 0)


if __name__ == "__main__":
    unittest.main()
