from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _active_lot(lookup, assay_id):
    lots = [lot for lot in (lookup("qc_lot", "assay_id", assay_id) or []) if lot["status"] == "active"]
    if not lots:
        return None
    return sorted(lots, key=lambda lot: lot["updated_at"])[-1]


def _confirmed_handover(lookup, assay_id):
    for handover in lookup("handover", "assay_id", assay_id) or []:
        if handover["status"] == "confirmed":
            return handover
    return None


def calibration_is_valid(calibration_due, as_of):
    return str(calibration_due)[:10] >= str(as_of)[:10]


def evaluate_qc(history, value, target, sd, config=None):
    """Evaluate one QC value against numeric and multi-rule criteria."""
    config = dict(config or {})
    try:
        value = float(value)
        target = float(target)
        sd = float(sd)
    except (TypeError, ValueError):
        raise ValidationError("qc value, target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("qc sd must be positive")
    limit = float(config.get("limit_sd", 3.0))
    bias_n = int(config.get("consecutive_n", 4))
    bias_sd = float(config.get("consecutive_sd", 1.0))
    trend_n = int(config.get("trend_n", 4))
    z_score = round((value - target) / sd, 4)
    flags = []
    if abs(z_score) > limit:
        flags.append("1_3s")
    values = [float(item) for item in history] + [value]
    if len(values) >= bias_n:
        window = values[-bias_n:]
        if all(item > target + bias_sd * sd for item in window):
            flags.append("bias_high")
        if all(item < target - bias_sd * sd for item in window):
            flags.append("bias_low")
    if len(values) >= trend_n:
        window = values[-trend_n:]
        if all(window[index] < window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_up")
        if all(window[index] > window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_down")
    passed = not flags
    return {
        "accepted": passed,
        "flags": flags,
        "z_score": z_score,
        "rule_snapshot": {
            "limit_sd": limit,
            "consecutive_n": bias_n,
            "consecutive_sd": bias_sd,
            "trend_n": trend_n,
        },
    }


def unrecovered_rejection(batches):
    return [batch for batch in batches if batch.get("status") == "intercepted"]


def _validate_assay(actor, data, lookup):
    try:
        low = float(data.get("allowed_low"))
        high = float(data.get("allowed_high"))
    except (TypeError, ValueError):
        raise ValidationError("allowed_low and allowed_high must be numeric")
    if low >= high:
        raise ValidationError("allowed_low must be less than allowed_high")
    return {
        "rule_config": dict(data.get("rule_config") or {}),
    }


def _validate_qc_lot(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    try:
        target = float(data.get("target"))
        sd = float(data.get("sd"))
    except (TypeError, ValueError):
        raise ValidationError("target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("sd must be positive")
    duplicate = _find_one(lookup, "qc_lot", "lot_key", "%s:%s" % (data["assay_id"], data["lot_no"]))
    if duplicate:
        raise ConflictError("qc lot already exists for assay")
    return {"lot_key": "%s:%s" % (data["assay_id"], data["lot_no"]), "target": target, "sd": sd}


def _validate_instrument(actor, data, lookup):
    if not str(data.get("serial", "")).strip():
        raise ValidationError("instrument serial is required")
    return {"calibration_due": data.get("calibration_due")}


def _validate_qc_run(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", data.get("qc_lot_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not lot or not instrument:
        raise ValidationError("assay, qc lot and instrument are required")
    if lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc lot does not belong to the assay")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("qc result value must be numeric")
    return {"value": value}


def _validate_result_batch(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    if not _find_one(lookup, "instrument", "id", data.get("instrument_id")):
        raise ValidationError("instrument does not exist")
    run = _find_one(lookup, "qc_run", "id", data.get("qc_run_id"))
    if not run:
        raise ValidationError("qc run does not exist")
    if int(data.get("patient_count", 0)) < 0:
        raise ValidationError("patient_count cannot be negative")
    return {"qc_lot_id": run["data"].get("qc_lot_id")}


def _validate_evaluate(actor, entity, data, lookup):
    assay = _find_one(lookup, "assay", "id", entity["data"].get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", entity["data"].get("qc_lot_id"))
    if not assay or not lot:
        raise ValidationError("assay or qc lot disappeared")
    previous = []
    for run in lookup("qc_run", "instrument_id", entity["data"].get("instrument_id")) or []:
        if run["id"] == entity["id"] or run["status"] not in ("accepted", "rejected"):
            continue
        if run["data"].get("qc_lot_id") != entity["data"].get("qc_lot_id"):
            continue
        if str(run["data"].get("run_at", "")) < str(entity["data"].get("run_at", "")):
            previous.append(run["data"]["value"])
    result = evaluate_qc(
        previous,
        entity["data"].get("value"),
        lot["data"].get("target"),
        lot["data"].get("sd"),
        assay["data"].get("rule_config"),
    )
    if not result["accepted"] and not data.get("reject_reason"):
        result["reject_reason"] = "quality control rule violation"
    result["_next_status"] = "accepted" if result["accepted"] else "rejected"
    return result


def _validate_release(actor, entity, data, lookup):
    run = _find_one(lookup, "qc_run", "id", entity["data"].get("qc_run_id"))
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    if not run or run["status"] != "accepted":
        raise ConflictError("result batch can only be released with an accepted QC run")
    if not instrument or instrument["status"] != "ready":
        raise ConflictError("instrument is not ready")
    if not calibration_is_valid(instrument["data"].get("calibration_due"), entity["data"].get("run_at")):
        raise ConflictError("instrument calibration is not valid at result time")
    active_holds = []
    for batch in lookup("result_batch", "instrument_id", entity["data"].get("instrument_id")) or []:
        if batch["id"] != entity["id"] and batch["status"] == "intercepted":
            active_holds.append(batch)
    if active_holds:
        raise ConflictError("an intercepted result batch must be resolved first")
    handover = _confirmed_handover(lookup, entity["data"].get("assay_id"))
    if handover and entity["data"].get("qc_lot_id") != handover["data"].get("new_lot_id"):
        raise ConflictError("after takeover the batch must be released under the new lot")
    return {"released_by": actor.user_id}


def _validate_qc_retest(actor, entity, data, lookup):
    replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
    if not replacement or replacement["status"] != "accepted":
        raise ValidationError("a replacement run must exist and be accepted")
    if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("replacement run belongs to another assay")
    return {"replacement_run_id": replacement["id"]}


def _validate_switch_lot(actor, entity, data, lookup):
    previous = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not previous or previous["status"] != "active":
        raise ValidationError("previous active lot is required")
    if previous["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("lots must belong to the same assay")
    return {"replaces_lot_id": previous["id"], "switched_at": data.get("switched_at")}


def _validate_correct(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("correction reason is required")
    history = list(entity["data"].get("correction_history") or [])
    history.append({"actor_id": actor.user_id, "reason": data["reason"], "from_status": entity["status"]})
    return {"correction_history": history}


def _validate_handover_create(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    if not assay:
        raise ValidationError("assay does not exist")
    new_lot = _find_one(lookup, "qc_lot", "id", data.get("new_lot_id"))
    if not new_lot or new_lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("new lot does not exist for assay")
    if new_lot["status"] != "registered":
        raise ConflictError("new lot must be registered and not yet active")
    old_lot = _active_lot(lookup, assay["id"])
    if not old_lot:
        raise ConflictError("no active lot to replace; activate the current lot first")
    if old_lot["id"] == new_lot["id"]:
        raise ValidationError("new lot must differ from the active lot")
    instrument_ids = data.get("instrument_ids")
    if not isinstance(instrument_ids, list) or not instrument_ids:
        raise ValidationError("instrument_ids must be a non-empty list")
    if len(set(instrument_ids)) != len(instrument_ids):
        raise ValidationError("instrument_ids must not contain duplicates")
    trials = []
    for instrument_id in instrument_ids:
        instrument = _find_one(lookup, "instrument", "id", instrument_id)
        if not instrument:
            raise ValidationError("instrument does not exist: " + str(instrument_id))
        trials.append(
            {
                "instrument_id": instrument_id,
                "trial_run_id": None,
                "status": "pending",
                "recorded_at": None,
            }
        )
    for handover in lookup("handover", "assay_id", assay["id"]) or []:
        if handover["status"] == "open":
            raise ConflictError("an open handover already exists for this assay")
    data.pop("instrument_ids", None)
    return {
        "old_lot_id": old_lot["id"],
        "trials": trials,
        "confirmed_at": None,
        "confirmed_by": None,
    }


def _validate_record_trial(actor, entity, data, lookup):
    if entity["status"] != "open":
        raise InvalidTransition("handover is not open")
    instrument_id = data.get("instrument_id")
    qc_run_id = data.get("qc_run_id")
    if not instrument_id or not qc_run_id:
        raise ValidationError("instrument_id and qc_run_id are required")
    run = _find_one(lookup, "qc_run", "id", qc_run_id)
    if not run:
        raise ValidationError("qc run does not exist")
    if run["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("qc run belongs to another assay")
    if run["data"].get("qc_lot_id") != entity["data"].get("new_lot_id"):
        raise ValidationError("trial run must use the new lot")
    if run["data"].get("instrument_id") != instrument_id:
        raise ValidationError("qc run was performed on another instrument")
    if run["status"] not in ("accepted", "rejected"):
        raise ConflictError("trial run must be evaluated as accepted or rejected")
    trials = [dict(trial) for trial in entity["data"].get("trials", [])]
    slot = None
    for trial in trials:
        if trial["instrument_id"] == instrument_id:
            slot = trial
            break
    if slot is None:
        raise ValidationError("instrument is not part of this handover")
    trial_status = "passed" if run["status"] == "accepted" else "failed"
    changed = slot.get("trial_run_id") != qc_run_id or slot.get("status") != trial_status
    if changed:
        slot["trial_run_id"] = qc_run_id
        slot["status"] = trial_status
        slot["recorded_at"] = run["updated_at"]
    return {"trials": trials, "_noop": not changed}


def _validate_handover_confirm(actor, entity, data, lookup):
    if entity["status"] != "open":
        raise InvalidTransition("handover is not open")
    unfinished = [
        trial
        for trial in entity["data"].get("trials", [])
        if trial.get("status") != "passed"
    ]
    if unfinished:
        raise ConflictError(
            "cannot confirm: %s instrument(s) have not passed trial" % len(unfinished),
            details={
                "handover_id": entity["id"],
                "current_version": entity["version"],
                "unfinished": unfinished,
            },
        )
    return {}


class RuleEngine:
    ALIASES = {
        "assays": "assay",
        "qc_lots": "qc_lot",
        "instruments": "instrument",
        "qc_runs": "qc_run",
        "result_batches": "result_batch",
        "handovers": "handover",
    }
    INITIAL_STATUS = {
        "assay": "active",
        "qc_lot": "registered",
        "instrument": "ready",
        "qc_run": "pending",
        "result_batch": "waiting",
        "handover": "open",
    }
    TRANSITIONS = {
        "assay": {
            "suspend": (("active",), "suspended"),
            "restore": (("suspended",), "active"),
        },
        "qc_lot": {
            "activate": (("registered", "suspended"), "active"),
            "switch_in": (("registered",), "active"),
            "suspend": (("active",), "suspended"),
            "retire": (("active", "suspended"), "retired"),
        },
        "instrument": {
            "calibrate": (("ready", "maintenance", "failed"), "ready"),
            "fail": (("ready",), "failed"),
            "maintain": (("ready", "failed"), "maintenance"),
            "restore": (("maintenance", "failed"), "ready"),
        },
        "qc_run": {
            "evaluate": (("pending",), "pending"),
            "retest": (("rejected",), "retesting"),
            "investigate": (("rejected",), "investigated"),
            "resolve": (("investigated", "retesting"), "resolved"),
            "correct": (("accepted", "rejected", "investigated", "resolved"), "pending"),
        },
        "result_batch": {
            "release": (("waiting",), "released"),
            "intercept": (("waiting",), "intercepted"),
            "retest": (("intercepted",), "waiting"),
            "investigate": (("intercepted",), "investigating"),
            "resolve": (("investigating",), "resolved"),
            "correct": (("waiting", "intercepted", "investigating", "released", "resolved"), "waiting"),
        },
        "handover": {
            "record_trial": (("open",), "open"),
            "confirm": (("open",), "confirmed"),
            "abort": (("open",), "aborted"),
        },
    }
    CREATE_REQUIRED = {
        "assay": ("name", "unit", "allowed_low", "allowed_high"),
        "qc_lot": ("assay_id", "lot_no", "target", "sd", "expires_at"),
        "instrument": ("name", "serial", "calibration_due"),
        "qc_run": ("assay_id", "qc_lot_id", "instrument_id", "value", "run_at"),
        "result_batch": ("assay_id", "instrument_id", "qc_run_id", "run_at", "patient_count"),
        "handover": ("assay_id", "new_lot_id", "instrument_ids"),
    }
    ACTION_REQUIRED = {
        ("assay", "suspend"): ("reason",),
        ("qc_lot", "switch_in"): ("previous_lot_id", "switched_at"),
        ("qc_lot", "suspend"): ("reason",),
        ("qc_lot", "retire"): ("reason",),
        ("instrument", "calibrate"): ("calibration_due", "certificate_id"),
        ("instrument", "fail"): ("reason",),
        ("instrument", "maintain"): ("reason",),
        ("qc_run", "evaluate"): ("evaluated_by",),
        ("qc_run", "retest"): ("reason",),
        ("qc_run", "investigate"): ("reason",),
        ("qc_run", "resolve"): ("resolution",),
        ("qc_run", "correct"): ("reason", "value"),
        ("result_batch", "release"): ("reviewer_id",),
        ("result_batch", "intercept"): ("reason",),
        ("result_batch", "retest"): ("replacement_run_id", "reason"),
        ("result_batch", "investigate"): ("reason",),
        ("result_batch", "resolve"): ("resolution",),
        ("result_batch", "correct"): ("reason",),
        ("handover", "record_trial"): ("instrument_id", "qc_run_id"),
        ("handover", "confirm"): (),
        ("handover", "abort"): ("reason",),
    }
    CREATE_ROLES = {
        "assay": ("supervisor", "admin"),
        "qc_lot": ("supervisor", "admin"),
        "instrument": ("supervisor", "admin"),
        "qc_run": ("operator", "supervisor", "admin"),
        "result_batch": ("operator", "supervisor", "admin"),
        "handover": ("supervisor", "admin"),
    }
    ROLE_ACTIONS = {
        "suspend": ("supervisor", "admin"),
        "restore": ("supervisor", "admin"),
        "activate": ("supervisor", "admin"),
        "switch_in": ("supervisor", "admin"),
        "retire": ("supervisor", "admin"),
        "calibrate": ("supervisor", "admin"),
        "fail": ("operator", "supervisor", "admin"),
        "maintain": ("operator", "supervisor", "admin"),
        "evaluate": ("operator", "supervisor", "admin"),
        "retest": ("operator", "supervisor", "admin"),
        "investigate": ("supervisor", "admin"),
        "resolve": ("supervisor", "admin"),
        "correct": ("supervisor", "admin"),
        "release": ("supervisor", "admin"),
        "intercept": ("operator", "supervisor", "admin"),
        ("handover", "record_trial"): ("operator", "supervisor", "admin"),
        ("handover", "confirm"): ("supervisor", "admin"),
        ("handover", "abort"): ("supervisor", "admin"),
    }
    CUSTOM_CREATE = {
        "assay": _validate_assay,
        "qc_lot": _validate_qc_lot,
        "instrument": _validate_instrument,
        "qc_run": _validate_qc_run,
        "result_batch": _validate_result_batch,
        "handover": _validate_handover_create,
    }
    CUSTOM_TRANSITIONS = {
        ("qc_run", "evaluate"): _validate_evaluate,
        ("result_batch", "release"): _validate_release,
        ("result_batch", "retest"): _validate_qc_retest,
        ("qc_lot", "switch_in"): _validate_switch_lot,
        ("qc_run", "correct"): _validate_correct,
        ("result_batch", "correct"): _validate_correct,
        ("handover", "record_trial"): _validate_record_trial,
        ("handover", "confirm"): _validate_handover_confirm,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra.get("_next_status"):
            next_status = extra.pop("_next_status")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
