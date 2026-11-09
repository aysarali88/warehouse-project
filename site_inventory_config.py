"""Fail-closed configuration gates for Site Inventory integrations."""

import os
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo


_TRUE_VALUES = {"1", "true", "yes", "on"}
_TRIPOLI_TZ = ZoneInfo("Africa/Tripoli")


def feature_flag_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in _TRUE_VALUES


def site_inventory_admin_write_enabled() -> bool:
    return feature_flag_enabled("SITE_INVENTORY_ADMIN_WRITE_ENABLED")


def cutover_programs(normalize_program_fn) -> set[str]:
    raw = os.getenv("SITE_INVENTORY_CUTOVER_PROGRAMS", "")
    known_programs = {"FTTH", "SINGLE_RAN", "SR", "SINGLERAN"}
    programs = set()
    for value in raw.split(","):
        token = value.strip().upper().replace("-", "_").replace(" ", "_")
        if token in known_programs:
            programs.add(normalize_program_fn(token))
    return programs


def cutover_program_enabled(program: str, normalize_program_fn) -> bool:
    return normalize_program_fn(program) in cutover_programs(normalize_program_fn)


def integration_enabled(flag_name: str, program: str, normalize_program_fn) -> bool:
    return feature_flag_enabled(flag_name) and cutover_program_enabled(program, normalize_program_fn)


def parse_cutover_timestamp(value: str | None = None) -> datetime | None:
    raw = (os.getenv("SITE_INVENTORY_CUTOVER_AT", "") if value is None else value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("SITE_INVENTORY_CUTOVER_AT must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("SITE_INVENTORY_CUTOVER_AT must include a timezone offset")
    return parsed.astimezone(_TRIPOLI_TZ)


def _parse_record_timestamp(value: str, *, allow_date_only: bool) -> tuple[datetime, bool]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Rollout activity date is missing; cutover eligibility cannot be established")
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        date_only = len(candidate) == 10 and "T" not in candidate and " " not in candidate
    except ValueError:
        parsed = None
        date_only = False
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed_date = datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
            if fmt in {"%d/%m/%Y", "%d-%m-%Y"}:
                first, second = (int(part) for part in raw.replace("-", "/").split("/"))
                if first <= 12 and second <= 12:
                    raise ValueError("Rollout activity date is ambiguous; cutover eligibility cannot be established")
            parsed = datetime.combine(parsed_date, time.min)
            date_only = True
            break
        if parsed is None:
            raise ValueError("Rollout activity date is invalid; cutover eligibility cannot be established")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_TRIPOLI_TZ)
    if date_only and not allow_date_only:
        raise ValueError("Rollout entry timestamp is not precise enough to establish cutover eligibility")
    return parsed.astimezone(_TRIPOLI_TZ), date_only


def validate_rollout_cutover_eligibility(record, cutover_at: datetime) -> None:
    recorded_at = getattr(record, "site_inventory_recorded_at", None)
    if not isinstance(recorded_at, datetime):
        raise ValueError("This Field Entry has no trusted server recording time and cannot be confirmed after cutover")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        recorded_at = recorded_at.replace(tzinfo=timezone.utc)
    if recorded_at.astimezone(_TRIPOLI_TZ) < cutover_at:
        raise ValueError("Field Entries recorded before Site Inventory cutover cannot be confirmed")

    activity_at, date_only = _parse_record_timestamp(record.date, allow_date_only=True)
    if date_only and activity_at.date() == cutover_at.date() and cutover_at.timetz().replace(tzinfo=None) != time.min:
        raise ValueError("The activity date does not include a time and cannot be proven to be after cutover")
    if activity_at < cutover_at:
        raise ValueError("Field Entries with activity before Site Inventory cutover cannot be confirmed")

    entry_time = str(getattr(record, "entry_time", "") or "").strip()
    if entry_time:
        entered_at, _ = _parse_record_timestamp(entry_time, allow_date_only=False)
        if entered_at < cutover_at:
            raise ValueError("Field Entries entered before Site Inventory cutover cannot be confirmed")


def site_inventory_readiness(program: str, normalize_program_fn) -> dict:
    try:
        cutoff_at = parse_cutover_timestamp()
        cutoff_status = "configured" if cutoff_at is not None else "missing"
    except ValueError:
        cutoff_at = None
        cutoff_status = "invalid"

    program_enabled = cutover_program_enabled(program, normalize_program_fn)
    integration_flags = {
        "mr": "SITE_INVENTORY_MR_ENABLED",
        "return": "SITE_INVENTORY_RETURN_ENABLED",
        "rollout": "SITE_INVENTORY_ROLLOUT_ENABLED",
        "transfer": "SITE_INVENTORY_TRANSFER_ENABLED",
    }
    return {
        "program": normalize_program_fn(program),
        "admin_write_enabled": site_inventory_admin_write_enabled(),
        "cutover_program_enabled": program_enabled,
        "cutover_timestamp_status": cutoff_status,
        "configuration_ready": program_enabled and cutoff_at is not None,
        "integrations": {
            name: {
                "flag_enabled": feature_flag_enabled(flag),
                "enabled": integration_enabled(flag, program, normalize_program_fn)
                and (name != "rollout" or cutoff_at is not None),
            }
            for name, flag in integration_flags.items()
        },
    }
