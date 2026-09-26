from datetime import datetime, timezone

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


def parse_instant(value):
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ValidationError("invalid datetime: " + str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def windows_overlap(a_start, a_end, b_start, b_end):
    return parse_instant(a_start) < parse_instant(b_end) and parse_instant(b_start) < parse_instant(a_end)


def is_overdue(entity, now=None):
    if entity.get("status") != "active":
        return False
    end = entity.get("data", {}).get("work_end")
    if not end:
        return False
    now = now or datetime.now(timezone.utc)
    return parse_instant(end) < now


def temp_change_conflicts(entity, others):
    data = entity.get("data", {})
    hits = []
    for other in others or []:
        if other.get("id") == entity.get("id") or other.get("status") != "active":
            continue
        other_data = other.get("data", {})
        if other_data.get("unit_id") != data.get("unit_id"):
            continue
        if windows_overlap(
            data.get("work_start"), data.get("work_end"),
            other_data.get("work_start"), other_data.get("work_end"),
        ):
            hits.append(other["id"])
    return sorted(hits)


def startup_blockers(unit_id, temp_changes, now=None):
    now = now or datetime.now(timezone.utc)
    return sorted(
        item["id"]
        for item in temp_changes or []
        if item.get("data", {}).get("unit_id") == unit_id and is_overdue(item, now)
    )


def _validate_temp_change(actor, data, lookup):
    unit = _find_one(lookup, "unit", "id", data.get("unit_id"))
    if not unit:
        raise ValidationError("unit does not exist")
    if not data.get("description", "").strip():
        raise ValidationError("work description is required")
    start = parse_instant(data.get("work_start"))
    end = parse_instant(data.get("work_end"))
    if end <= start:
        raise ValidationError("work_end must be later than work_start")


def _validate_tc_confirm(actor, entity, data, lookup):
    others = lookup("temp_change", "unit_id", entity["data"].get("unit_id")) if lookup else []
    hits = temp_change_conflicts(entity, others)
    if hits:
        raise ValidationError("window conflicts with active temp change(s): " + ", ".join(hits))
    return {"confirmed_by": actor.user_id}


def _validate_tc_renew(actor, entity, data, lookup):
    old_end = parse_instant(entity["data"].get("work_end"))
    new_end = parse_instant(data.get("work_end"))
    if new_end <= old_end:
        raise ValidationError("new work_end must be later than current work_end")
    return {
        "revision": int(entity["data"].get("revision", 1)) + 1,
        "risk_level": data.get("risk_level"),
        "risk_note": data.get("risk_note"),
    }


def _validate_unit_startup(actor, entity, data, lookup):
    changes = lookup("temp_change", "unit_id", entity["id"]) if lookup else []
    blocked = startup_blockers(entity["id"], changes)
    if blocked:
        raise ValidationError("overdue unrestored temp change(s) block startup: " + ", ".join(blocked))


CUSTOM_CREATE = {'change': _validate_change, 'temp_change': _validate_temp_change}
CUSTOM_TRANSITIONS = {('change', 'assess'): _validate_assess, ('change', 'approve'): _validate_approve, ('change', 'commission'): _validate_commission, ('temp_change', 'confirm'): _validate_tc_confirm, ('temp_change', 'renew'): _validate_tc_renew, ('unit', 'startup'): _validate_unit_startup}


class RuleEngine:
    ALIASES = {'units': 'unit', 'changes': 'change', 'action_items': 'action_item', 'temp_changes': 'temp_change'}
    INITIAL_STATUS = {'unit': 'operating', 'change': 'draft', 'action_item': 'open', 'temp_change': 'pending'}
    TRANSITIONS = {'unit': {'shutdown': (('operating',), 'shutdown'), 'startup': (('shutdown',), 'operating'), 'freeze': (('operating',), 'frozen'), 'unfreeze': (('frozen',), 'operating')}, 'change': {'assess': (('draft',), 'assessed'), 'approve': (('assessed',), 'approved'), 'implement': (('approved',), 'implemented'), 'commission': (('implemented',), 'commissioned'), 'rollback': (('implemented', 'commissioned'), 'rolled_back'), 'close': (('rolled_back',), 'closed')}, 'action_item': {'complete': (('open',), 'completed'), 'verify': (('completed',), 'verified'), 'reopen': (('verified',), 'open')}, 'temp_change': {'confirm': (('pending',), 'active'), 'restore': (('active',), 'restored'), 'renew': (('active',), 'pending'), 'reject': (('pending',), 'rejected')}}
    CREATE_REQUIRED = {'unit': ('name', 'location'), 'change': ('unit_id', 'description'), 'action_item': ('change_id', 'description', 'owner'), 'temp_change': ('unit_id', 'description', 'work_start', 'work_end', 'isolation', 'restore_owner')}
    ACTION_REQUIRED = {('unit', 'shutdown'): ('reason',), ('unit', 'freeze'): ('reason',), ('change', 'assess'): ('risk_level', 'analyst'), ('change', 'approve'): ('approvals', 'permit_id'), ('change', 'implement'): ('procedure_version',), ('change', 'commission'): ('tests_passed',), ('change', 'rollback'): ('reason',), ('change', 'close'): ('outcome',), ('action_item', 'complete'): ('completed_by', 'evidence'), ('action_item', 'verify'): ('verifier',), ('action_item', 'reopen'): ('reason',), ('temp_change', 'restore'): ('restored_by', 'restore_note'), ('temp_change', 'renew'): ('work_end', 'risk_level', 'risk_note'), ('temp_change', 'reject'): ('reason',)}
    CREATE_ROLES = {'unit': ('admin', 'engineer'), 'change': ('admin', 'engineer'), 'action_item': ('admin', 'safety'), 'temp_change': ('admin', 'engineer', 'operator')}
    ROLE_ACTIONS = {'shutdown': ('admin', 'operator'), 'startup': ('admin', 'operator'), 'freeze': ('admin', 'operator'), 'unfreeze': ('admin', 'operator'), 'assess': ('admin', 'engineer'), 'approve': ('admin', 'safety'), 'implement': ('admin', 'engineer'), 'commission': ('admin', 'engineer'), 'rollback': ('admin', 'engineer'), 'close': ('admin', 'safety'), 'complete': ('admin', 'engineer'), 'verify': ('admin', 'verifier'), 'reopen': ('admin', 'verifier'), 'confirm': ('admin', 'safety'), 'restore': ('admin', 'operator', 'engineer'), 'renew': ('admin', 'engineer', 'operator'), 'reject': ('admin', 'safety')}

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
