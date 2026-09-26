from datetime import datetime, timedelta, timezone

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("change description is required")


def required_approval_level(risk_level):
    levels = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    return levels.get(str(risk_level).lower(), 4)


def _validate_assess(actor, entity, data, lookup):
    return {"required_approvals": required_approval_level(data.get("risk_level"))}


def _validate_approve(actor, entity, data, lookup):
    required = int(entity["data"].get("required_approvals", 1))
    approvals = data.get("approvals") or []
    if len(set(approvals)) < required:
        raise ValidationError("not enough distinct approvals")
    return {"approved_by": actor.user_id}


def _validate_commission(actor, entity, data, lookup):
    items = lookup("action_item", "change_id", entity["id"]) or [] if lookup else []
    unresolved = [item["id"] for item in items if item["status"] != "verified"]
    if unresolved:
        raise ValidationError("unresolved action items: " + ", ".join(unresolved))
    return {"commissioned_by": actor.user_id}


def _parse_instant(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        raise ValidationError("invalid datetime for %s: %s" % (field, value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _utcnow():
    return datetime.now(timezone.utc)


def _validate_window(data):
    start = _parse_instant(data.get("window_start"), "window_start")
    end = _parse_instant(data.get("window_end"), "window_end")
    if end <= start:
        raise ValidationError("window_end must be after window_start")
    return start, end


def _validate_temp_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    _validate_window(data)
    if not str(data.get("isolation", "")).strip():
        raise ValidationError("isolation measures are required")
    if not str(data.get("restore_owner", "")).strip():
        raise ValidationError("restore owner is required")


def find_window_conflicts(lookup, unit_id, start, end, exclude_id=None):
    """Ids of active temp changes on the unit whose windows overlap [start, end)."""
    if lookup is None:
        return []
    conflicts = []
    for item in lookup("temp_change", "unit_id", unit_id) or []:
        if item["id"] == exclude_id or item["status"] != "active":
            continue
        other_start, other_end = _validate_window(item["data"])
        if start < other_end and other_start < end:
            conflicts.append(item["id"])
    return sorted(conflicts)


def find_unit_blockers(lookup, unit_id, now=None):
    """Ids of active temp changes on the unit whose window expired without restore."""
    if lookup is None:
        return []
    moment = now or _utcnow()
    blockers = []
    for item in lookup("temp_change", "unit_id", unit_id) or []:
        if item["status"] != "active":
            continue
        _, end = _validate_window(item["data"])
        if moment > end:
            blockers.append(item["id"])
    return sorted(blockers)


def _validate_temp_confirm(actor, entity, data, lookup):
    start, end = _validate_window(entity["data"])
    conflicts = find_window_conflicts(
        lookup, entity["data"].get("unit_id"), start, end, exclude_id=entity["id"]
    )
    if conflicts:
        raise ConflictError(
            "作业时段与已生效临时变更冲突，冲突单: " + ", ".join(conflicts)
        )
    return {
        "confirmed_by": actor.user_id,
        "confirmed_at": _utcnow().isoformat(timespec="seconds"),
    }


def _validate_temp_restore(actor, entity, data, lookup):
    return {"restored_at": _utcnow().isoformat(timespec="seconds")}


def _validate_temp_renew(actor, entity, data, lookup):
    _, current_end = _validate_window(entity["data"])
    if _utcnow() > current_end:
        raise ValidationError("临时变更已到期未恢复，不能直接续期，请重新登记申请")
    _validate_window(data)
    return {
        "revision": int(entity["data"].get("revision", 1)) + 1,
        "reassessed_by": data.get("analyst"),
        "previous_window_end": current_end.isoformat(timespec="seconds"),
    }


def _validate_unit_startup(actor, entity, data, lookup):
    blockers = find_unit_blockers(lookup, entity["id"])
    if blockers:
        raise ValidationError(
            "存在到期未恢复的临时变更，装置不能启动，阻塞单: " + ", ".join(blockers)
        )


CUSTOM_CREATE = {'change': _validate_change, 'temp_change': _validate_temp_change}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'commission'): _validate_commission, ('temp_change', 'confirm'): _validate_temp_confirm, ('temp_change', 'restore'): _validate_temp_restore, ('temp_change', 'renew'): _validate_temp_renew, ('unit', 'startup'): _validate_unit_startup}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'temp_changes': 'temp_change'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'temp_change': 'draft'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}, 'temp_change': {'submit': (('draft',), 'submitted'), 'confirm': (('submitted',), 'active'), 'restore': (('active',), 'restored'), 'renew': (('active',), 'submitted')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'temp_change': ('unit_id', 'description', 'window_start', 'window_end', 'isolation', 'restore_owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',), ('temp_change', 'restore'): ('restored_by',), ('temp_change', 'renew'): ('window_start', 'window_end', 'risk_level', 'analyst')}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'temp_change': ('admin', 'engineer', 'operator')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier'), 'submit': ('admin', 'engineer', 'operator'), 'confirm': ('admin', 'safety'), 'restore': ('admin', 'operator'), 'renew': ('admin', 'engineer')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
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
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
