import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError, ValidationError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'create',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)
            # Upgrade databases created before idempotency records were scoped to an action.
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(idempotency)").fetchall()
            }
            if "action" not in columns:
                connection.execute(
                    "ALTER TABLE idempotency ADD COLUMN action TEXT NOT NULL DEFAULT 'create'"
                )

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key, action="create"):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id, action FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        if not row:
            return None
        return row["entity_id"] if row["action"] == action else False

    def save_idempotency(self, actor_id, idem_key, entity_id, action="create"):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, action, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, action, utcnow()),
            )

    def confirm_takeover(self, takeover_id, actor, expected_version=None, idempotency_key=None):
        """Take over at the confirmation instant in one atomic transaction.

        Flips the new QC lot to active, retires the old lot, and re-points every
        unreleased patient result batch to the new lot. Already released batches
        stay on the old lot. Optimistic versioning plus a single write transaction
        guarantees that only one of two concurrent supervisors succeeds and that a
        retried write neither occupies an instrument slot twice nor double-writes
        audit entries.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'takeover'",
                (takeover_id,),
            ).fetchone()
            if not row:
                raise NotFoundError("takeover not found: " + takeover_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version),
                    details={"current_version": current_version},
                )
            status = row["status"]
            data = json.loads(row["data"])
            if status == "confirmed":
                raise ConflictError(
                    "takeover already confirmed",
                    details={"current_version": current_version, "takeover_id": takeover_id},
                )
            pending = [
                slot["instrument_id"]
                for slot in data.get("slots", [])
                if slot.get("status") != "passed"
            ]
            if status != "ready" or pending:
                raise ConflictError(
                    "takeover is not ready; %d instrument(s) still pending" % len(pending),
                    details={"pending_instrument_ids": pending, "current_version": current_version},
                )

            old_id = data["previous_lot_id"]
            new_id = data["new_lot_id"]
            now = utcnow()

            def write_entity(entity_id, next_status, next_data):
                payload = json.dumps(next_data, ensure_ascii=False, sort_keys=True)
                connection.execute(
                    "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                    "WHERE id = ?",
                    (next_status, payload, now, entity_id),
                )

            def audit(entity_id, action, from_status, to_status, detail):
                connection.execute(
                    "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, "
                    "from_status, to_status, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        actor.user_id,
                        actor.role,
                        action,
                        from_status,
                        to_status,
                        json.dumps(detail, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )

            old_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'qc_lot'", (old_id,)
            ).fetchone()
            if not old_row:
                raise ValidationError("previous qc lot disappeared")
            if old_row["status"] != "active":
                raise ConflictError(
                    "previous lot is no longer active (status %s)" % old_row["status"],
                    details={"previous_lot_status": old_row["status"], "current_version": current_version},
                )
            old_data = json.loads(old_row["data"])
            old_from = old_row["status"]
            write_entity(old_id, "retired", old_data)
            audit(old_id, "retire", old_from, "retired", {"takeover_id": takeover_id, "reason": "lot takeover"})

            new_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'qc_lot'", (new_id,)
            ).fetchone()
            if not new_row:
                raise ValidationError("new qc lot disappeared")
            if new_row["status"] != "registered":
                raise ConflictError(
                    "new lot is no longer registered (status %s)" % new_row["status"],
                    details={"new_lot_status": new_row["status"], "current_version": current_version},
                )
            new_data = json.loads(new_row["data"])
            new_from = new_row["status"]
            new_data["replaces_lot_id"] = old_id
            write_entity(new_id, "active", new_data)
            audit(new_id, "activate", new_from, "active", {"takeover_id": takeover_id})

            reassigned = []
            batch_rows = connection.execute(
                "SELECT * FROM entities WHERE kind = 'result_batch' AND status != 'released'"
            ).fetchall()
            for batch_row in batch_rows:
                batch_data = json.loads(batch_row["data"])
                if batch_data.get("assay_id") != data.get("assay_id"):
                    continue
                # Already released rows are excluded above; unreleased batches run
                # before the takeover move onto the new lot.
                batch_data["qc_lot_id"] = new_id
                write_entity(batch_row["id"], batch_row["status"], batch_data)
                audit(
                    batch_row["id"],
                    "reassign_lot",
                    batch_row["status"],
                    batch_row["status"],
                    {"from_lot_id": old_id, "to_lot_id": new_id, "takeover_id": takeover_id},
                )
                reassigned.append(batch_row["id"])

            data["confirmed_by"] = actor.user_id
            data["confirmed_at"] = now
            data["reassigned_batch_ids"] = reassigned
            write_entity(takeover_id, "confirmed", data)
            audit(takeover_id, "confirm", status, "confirmed", {
                "previous_lot_id": old_id,
                "new_lot_id": new_id,
                "confirmed_at": now,
                "reassigned_batch_ids": reassigned,
            })
            if idempotency_key:
                connection.execute(
                    "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, action, created_at) "
                    "VALUES (?, ?, ?, 'confirm', ?)",
                    (actor.user_id, idempotency_key, takeover_id, now),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(takeover_id)

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
