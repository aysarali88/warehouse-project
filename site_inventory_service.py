import hashlib
import json
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import normalize_program
from models import (
    AuditLog,
    Product,
    Site,
    SiteAlias,
    SiteBalance,
    SiteMaterialMovement,
    SiteOpeningBalanceRequest,
)


QUANTITY_QUANTUM = Decimal("0.000001")
SUPPORTED_MOVEMENT_TYPES = {"opening_balance", "incoming", "outgoing", "adjustment", "reversal"}


def _decimal(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Quantity must be a finite decimal") from exc
    if not result.is_finite():
        raise HTTPException(status_code=400, detail="Quantity supports at most six decimal places")
    if abs(result) >= Decimal("1000000000000"):
        raise HTTPException(status_code=400, detail="Quantity exceeds the supported range")
    try:
        if result.quantize(QUANTITY_QUANTUM) != result:
            raise HTTPException(status_code=400, detail="Quantity supports at most six decimal places")
    except InvalidOperation as exc:
        raise HTTPException(status_code=400, detail="Quantity exceeds the supported precision") from exc
    return result


def normalize_uom(value: str) -> str:
    result = str(value or "").strip().upper()
    if not result or len(result) > 24 or any(ord(char) < 32 for char in result):
        raise HTTPException(status_code=400, detail="A valid unit of measure is required")
    return result


def normalize_site_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    words = []
    current = []
    for char in normalized:
        if char.isalnum():
            current.append(char)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    result = " ".join(words)
    if not result or len(result) > 160:
        raise HTTPException(status_code=400, detail="Alias must contain 1 to 160 normalized characters")
    return result


def request_token_hash(program: str, token: str) -> str:
    value = str(token or "").strip()
    if len(value) < 16 or len(value) > 200:
        raise HTTPException(status_code=400, detail="A valid idempotency token is required")
    scoped = f"{normalize_program(program)}\0{value}"
    return hashlib.sha256(scoped.encode("utf-8")).hexdigest()


def legacy_request_token_hash(token: str) -> str:
    value = str(token or "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_event_key(
    program: str,
    event_type: str,
    document_type: str,
    document_id: str,
    document_line_id: str,
) -> str:
    parts = (
        normalize_program(program),
        str(event_type or "").strip().casefold(),
        str(document_type or "").strip().casefold(),
        str(document_id or "").strip(),
        str(document_line_id or "").strip(),
    )
    if any(not part for part in parts):
        raise HTTPException(status_code=400, detail="Event type and complete source document identity are required")
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _require_transaction(db) -> None:
    if not db.in_transaction():
        raise RuntimeError("Site inventory writes require a caller-owned transaction")


def _audit(db, action: str, entity_id: str, actor: str, details: dict) -> None:
    db.add(AuditLog(
        action=action,
        entity_type="site_inventory",
        entity_id=str(entity_id),
        actor=actor or "system",
        details=json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
    ))


def _stage(_name: str) -> None:
    """Internal fault-injection seam used by isolated rollback tests."""


def _check_site_product(db, program: str, site_id: int, product_id: int):
    site = db.query(Site).filter_by(id=site_id, program=program).first()
    product = db.query(Product).filter_by(id=product_id, program=program).first()
    if site is None or product is None:
        raise HTTPException(status_code=404, detail="Site or product not found in this program")
    return site, product


def _validate_uom(product: Product, uom: str) -> str:
    actual = normalize_uom(uom)
    expected = normalize_uom(product.unit or "PCS")
    if actual != expected:
        raise HTTPException(status_code=400, detail=f"Unit of measure must match the product unit ({expected})")
    return actual


def _ensure_balance_row(db, program: str, site_id: int, product_id: int) -> SiteBalance:
    dialect = db.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert_fn is None:
        raise RuntimeError(f"Unsupported database dialect for Site Inventory: {dialect}")
    statement = insert_fn(SiteBalance).values(
        program=program, site_id=site_id, product_id=product_id, on_hand_qty=Decimal("0")
    ).on_conflict_do_nothing(index_elements=["program", "site_id", "product_id"])
    db.execute(statement)
    return (
        db.query(SiteBalance)
        .filter_by(program=program, site_id=site_id, product_id=product_id)
        .with_for_update()
        .one()
    )


def _same_movement(row, *, program, site_id, product_id, movement_type, event_type, quantity_delta,
                   uom, document_type, document_id, document_line_id, event_key, reference, note,
                   reverses_movement_id):
    return (
        row.program == program
        and row.site_id == site_id
        and row.product_id == product_id
        and row.movement_type == movement_type
        and row.event_type == event_type
        and _decimal(row.quantity_delta) == quantity_delta
        and row.uom == uom
        and row.document_type == document_type
        and row.document_id == document_id
        and row.document_line_id == document_line_id
        and row.source_event_key == event_key
        and row.reference == reference
        and row.note == note
        and row.reverses_movement_id == reverses_movement_id
    )


def _find_request_replay(db, program: str, token: str):
    if not token:
        return None
    hashes = {request_token_hash(program, token), legacy_request_token_hash(token)}
    return db.query(SiteMaterialMovement).filter(
        SiteMaterialMovement.program == program,
        SiteMaterialMovement.request_token_hash.in_(hashes),
    ).first()


def post_site_movement(
    db,
    *,
    program: str,
    site_id: int,
    product_id: int,
    movement_type: str,
    event_type: str,
    quantity_delta,
    uom: str,
    document_type: str,
    document_id: str,
    document_line_id: str,
    reference: str,
    note: str,
    created_by: str,
    approved_by: str,
    effective_date: str,
    request_token: str = "",
    reverses_movement_id: int | None = None,
) -> SiteMaterialMovement:
    _require_transaction(db)
    program_key = normalize_program(program)
    kind = str(movement_type or "").strip().lower()
    event = str(event_type or "").strip().lower()
    document_kind = str(document_type or "").strip().lower()
    document = str(document_id or "").strip()
    line = str(document_line_id or "").strip()
    reference_value = str(reference or "").strip()
    note_value = str(note or "").strip()
    actor = str(created_by or "").strip()
    approver = str(approved_by or "").strip()
    quantity = _decimal(quantity_delta)
    if kind not in SUPPORTED_MOVEMENT_TYPES:
        raise HTTPException(status_code=400, detail="Unsupported site movement type")
    if kind == "opening_balance" and (event != "opening_balance_approved" or document_kind != "site_opening_balance_request"):
        raise HTTPException(status_code=400, detail="Opening balances must be posted by an approved opening request")
    if kind == "incoming" and quantity <= 0:
        raise HTTPException(status_code=400, detail="Incoming movement quantity must be positive")
    if kind == "outgoing" and quantity >= 0:
        raise HTTPException(status_code=400, detail="Outgoing movement quantity must be negative")
    if kind == "opening_balance" and quantity < 0:
        raise HTTPException(status_code=400, detail="Opening balance cannot be negative")
    if kind != "opening_balance" and quantity == 0:
        raise HTTPException(status_code=400, detail="Movement quantity cannot be zero")
    if not reference_value or not note_value or not actor or not approver:
        raise HTTPException(status_code=400, detail="Movement reference, note, and actors are required")
    try:
        date.fromisoformat(str(effective_date))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Effective date must use YYYY-MM-DD") from exc
    event_key = source_event_key(program_key, event, document_kind, document, line)
    _, product = _check_site_product(db, program_key, site_id, product_id)
    unit = _validate_uom(product, uom)

    original = None
    if kind == "reversal":
        if reverses_movement_id is None:
            raise HTTPException(status_code=400, detail="A reversal must reference the movement being corrected")
        original = db.query(SiteMaterialMovement).filter_by(
            id=reverses_movement_id, program=program_key, status="approved"
        ).with_for_update().first()
        if original is None:
            raise HTTPException(status_code=404, detail="Movement to reverse was not found")
        if (original.site_id, original.product_id, original.uom) != (site_id, product_id, unit):
            raise HTTPException(status_code=409, detail="A reversal must use the original site, product, and unit")
        if quantity != -_decimal(original.quantity_delta):
            raise HTTPException(status_code=400, detail="A reversal must exactly negate the original movement")
        if db.query(SiteMaterialMovement.id).filter_by(reverses_movement_id=original.id).first():
            raise HTTPException(status_code=409, detail="This movement has already been reversed")
    elif reverses_movement_id is not None:
        raise HTTPException(status_code=400, detail="Only reversal movements may reference another movement")

    replay = db.query(SiteMaterialMovement).filter_by(
        program=program_key, source_event_key=event_key
    ).first()
    request_replay = _find_request_replay(db, program_key, request_token)
    if replay is not None or request_replay is not None:
        existing = replay or request_replay
        if replay is not None and request_replay is not None and replay.id != request_replay.id:
            raise HTTPException(status_code=409, detail="Event identity and request token refer to different movements")
        if not _same_movement(
            existing, program=program_key, site_id=site_id, product_id=product_id,
            movement_type=kind, event_type=event, quantity_delta=quantity, uom=unit,
            document_type=document_kind, document_id=document, document_line_id=line,
            event_key=event_key, reference=reference_value, note=note_value,
            reverses_movement_id=reverses_movement_id,
        ):
            raise HTTPException(status_code=409, detail="Source event or idempotency token was already used for another movement")
        return existing

    balance = _ensure_balance_row(db, program_key, site_id, product_id)
    scoped_hash = request_token_hash(program_key, request_token) if request_token else None
    idempotency_key = scoped_hash or event_key
    values = dict(
        program=program_key,
        site_id=site_id,
        product_id=product_id,
        movement_type=kind,
        event_type=event,
        quantity_delta=quantity,
        uom=unit,
        document_type=document_kind,
        document_id=document,
        document_line_id=line,
        source_event_key=event_key,
        reference=reference_value,
        note=note_value,
        reverses_movement_id=reverses_movement_id,
        created_by=actor,
        approved_by=approver,
        effective_date=str(effective_date),
        status="approved",
        idempotency_key=idempotency_key,
        request_token_hash=scoped_hash,
    )
    dialect = db.get_bind().dialect.name
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
    if insert_fn is None:
        raise RuntimeError(f"Unsupported database dialect for Site Inventory: {dialect}")
    inserted_id = db.execute(
        insert_fn(SiteMaterialMovement)
        .values(**values)
        .on_conflict_do_nothing()
        .returning(SiteMaterialMovement.id)
    ).scalar_one_or_none()
    if inserted_id is None:
        existing = db.query(SiteMaterialMovement).filter_by(
            program=program_key, source_event_key=event_key
        ).first() or _find_request_replay(db, program_key, request_token)
        if existing is None or not _same_movement(
            existing, program=program_key, site_id=site_id, product_id=product_id,
            movement_type=kind, event_type=event, quantity_delta=quantity, uom=unit,
            document_type=document_kind, document_id=document, document_line_id=line,
            event_key=event_key, reference=reference_value, note=note_value,
            reverses_movement_id=reverses_movement_id,
        ):
            raise HTTPException(status_code=409, detail="A conflicting site movement already exists")
        return existing

    movement = db.query(SiteMaterialMovement).filter_by(
        program=program_key, source_event_key=event_key
    ).one()
    _stage("after_movement")
    changed = db.execute(
        update(SiteBalance)
        .where(
            SiteBalance.id == balance.id,
            SiteBalance.on_hand_qty + quantity >= 0,
        )
        .values(on_hand_qty=SiteBalance.on_hand_qty + quantity, updated_at=func.now())
    )
    if changed.rowcount != 1:
        raise HTTPException(status_code=409, detail="Site balance is insufficient; negative balances are blocked")
    _stage("after_balance")
    _audit(db, "site_inventory_movement_posted", str(movement.id), approver, {
        "program": program_key,
        "site_id": site_id,
        "product_id": product_id,
        "movement_type": kind,
        "event_type": event,
        "quantity_delta": quantity,
        "uom": unit,
        "source_event_key": event_key,
        "reverses_movement_id": reverses_movement_id,
    })
    _stage("after_audit")
    return movement


def create_opening_balance_request(
    db,
    *,
    program: str,
    site_id: int,
    product_id: int,
    quantity,
    uom: str,
    effective_date: str,
    reference: str,
    note: str,
    entered_by: str,
    request_token: str,
) -> SiteOpeningBalanceRequest:
    _require_transaction(db)
    program_key = normalize_program(program)
    amount = _decimal(quantity)
    if amount < 0:
        raise HTTPException(status_code=400, detail="Opening balance cannot be negative")
    try:
        date.fromisoformat(str(effective_date))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Opening balance date must use YYYY-MM-DD") from exc
    ref, description, actor = str(reference or "").strip(), str(note or "").strip(), str(entered_by or "").strip()
    if not ref or not description or not actor:
        raise HTTPException(status_code=400, detail="Opening balance reference, note, and requester are required")
    _, product = _check_site_product(db, program_key, site_id, product_id)
    unit = _validate_uom(product, uom)
    scoped_hash = request_token_hash(program_key, request_token)
    legacy_hash = legacy_request_token_hash(request_token)
    previous = db.query(SiteOpeningBalanceRequest).filter(
        SiteOpeningBalanceRequest.program == program_key,
        SiteOpeningBalanceRequest.request_token_hash.in_({scoped_hash, legacy_hash}),
    ).first()
    if previous:
        exact = (
            previous.site_id == site_id
            and previous.product_id == product_id
            and _decimal(previous.quantity) == amount
            and previous.uom == unit
            and previous.effective_date == effective_date
            and previous.reference == ref
            and previous.note == description
        )
        if not exact:
            raise HTTPException(status_code=409, detail="Opening request token was already used for a different request")
        return previous
    if db.query(SiteMaterialMovement.id).filter_by(
        program=program_key, site_id=site_id, product_id=product_id, movement_type="opening_balance"
    ).first():
        raise HTTPException(status_code=409, detail="An approved opening balance already exists for this site and product")
    row = SiteOpeningBalanceRequest(
        program=program_key,
        site_id=site_id,
        product_id=product_id,
        quantity=amount,
        uom=unit,
        effective_date=str(effective_date),
        reference=ref,
        note=description,
        status="pending_approval",
        entered_by=actor,
        request_token_hash=scoped_hash,
    )
    db.add(row)
    db.flush()
    _audit(db, "site_opening_balance_submitted", str(row.id), actor, {
        "program": program_key, "site_id": site_id, "product_id": product_id,
        "quantity": amount, "uom": unit, "reference": ref,
    })
    return row


def approve_opening_balance(db, request_id: int, program: str, approver: str) -> SiteMaterialMovement:
    _require_transaction(db)
    program_key = normalize_program(program)
    request = db.query(SiteOpeningBalanceRequest).filter_by(
        id=request_id, program=program_key
    ).with_for_update().first()
    if request is None:
        raise HTTPException(status_code=404, detail="Opening balance request not found")
    if request.status == "approved":
        key = source_event_key(program_key, "opening_balance_approved", "site_opening_balance_request", str(request.id), "1")
        movement = db.query(SiteMaterialMovement).filter_by(program=program_key, source_event_key=key).first()
        if movement is None:
            raise HTTPException(status_code=409, detail="Approved opening request is missing its ledger movement")
        return movement
    if request.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Only pending opening requests can be approved")
    approver_name = str(approver or "").strip()
    if not approver_name or request.entered_by.casefold() == approver_name.casefold():
        raise HTTPException(status_code=403, detail="Opening balance must be approved by a different user")
    movement = post_site_movement(
        db,
        program=program_key,
        site_id=request.site_id,
        product_id=request.product_id,
        movement_type="opening_balance",
        event_type="opening_balance_approved",
        quantity_delta=request.quantity,
        uom=request.uom,
        document_type="site_opening_balance_request",
        document_id=str(request.id),
        document_line_id="1",
        reference=request.reference,
        note=request.note,
        created_by=request.entered_by,
        approved_by=approver_name,
        effective_date=request.effective_date,
        request_token=f"site-opening-approval:{program_key}:{request.id}",
    )
    request.status = "approved"
    request.approved_by = approver_name
    request.approved_at = datetime.now(timezone.utc)
    _audit(db, "site_opening_balance_approved", str(request.id), approver_name, {
        "program": program_key, "movement_id": movement.id,
        "site_id": request.site_id, "product_id": request.product_id,
        "quantity": request.quantity, "uom": request.uom,
    })
    _stage("after_opening_approval_audit")
    return movement


def reject_opening_balance(db, request_id: int, program: str, actor: str, note: str) -> SiteOpeningBalanceRequest:
    _require_transaction(db)
    row = db.query(SiteOpeningBalanceRequest).filter_by(
        id=request_id, program=normalize_program(program)
    ).with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Opening balance request not found")
    if row.status != "pending_approval":
        raise HTTPException(status_code=409, detail="Only pending opening requests can be rejected")
    actor_name, rejection = str(actor or "").strip(), str(note or "").strip()
    if not actor_name or actor_name.casefold() == row.entered_by.casefold():
        raise HTTPException(status_code=403, detail="Opening balance must be reviewed by a different user")
    if len(rejection) < 3:
        raise HTTPException(status_code=400, detail="A rejection note is required")
    row.status = "rejected"
    row.rejected_by = actor_name
    row.rejected_at = datetime.now(timezone.utc)
    row.rejection_note = rejection
    _audit(db, "site_opening_balance_rejected", str(row.id), actor_name, {
        "program": row.program, "site_id": row.site_id, "product_id": row.product_id,
        "quantity": row.quantity, "uom": row.uom, "note": rejection,
    })
    return row


def reverse_site_movement(db, movement_id: int, program: str, actor: str, reference: str, note: str,
                          request_token: str) -> SiteMaterialMovement:
    _require_transaction(db)
    program_key = normalize_program(program)
    original = db.query(SiteMaterialMovement).filter_by(
        id=movement_id, program=program_key, status="approved"
    ).with_for_update().first()
    if original is None:
        raise HTTPException(status_code=404, detail="Movement to reverse was not found")
    return post_site_movement(
        db,
        program=program_key,
        site_id=original.site_id,
        product_id=original.product_id,
        movement_type="reversal",
        event_type="movement_reversal",
        quantity_delta=-_decimal(original.quantity_delta),
        uom=original.uom,
        document_type="site_material_movement",
        document_id=str(original.id),
        document_line_id="1",
        reference=reference,
        note=note,
        created_by=actor,
        approved_by=actor,
        effective_date=date.today().isoformat(),
        request_token=request_token,
        reverses_movement_id=original.id,
    )


def create_site_alias(db, *, program: str, site_id: int, display_alias: str, actor: str) -> SiteAlias:
    _require_transaction(db)
    program_key = normalize_program(program)
    display = unicodedata.normalize("NFKC", str(display_alias or "")).strip()
    normalized = normalize_site_alias(display)
    site = db.query(Site).filter_by(id=site_id, program=program_key).first()
    if site is None:
        raise HTTPException(status_code=404, detail="Site not found in this program")
    sites = db.query(Site).filter_by(program=program_key).all()
    canonical_matches = [row for row in sites if normalize_site_alias(row.name) == normalized]
    if canonical_matches:
        raise HTTPException(status_code=409, detail="Alias collides with an existing canonical site name")
    existing = db.query(SiteAlias).filter_by(program=program_key, normalized_alias=normalized).first()
    if existing:
        raise HTTPException(status_code=409, detail="Alias is already mapped; ambiguous aliases are not allowed")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise HTTPException(status_code=400, detail="Alias audit actor is required")
    row = SiteAlias(
        program=program_key,
        normalized_alias=normalized,
        display_alias=display,
        site_id=site_id,
        created_by=actor_name,
        updated_by=actor_name,
    )
    db.add(row)
    db.flush()
    _audit(db, "site_alias_created", str(row.id), actor_name, {
        "program": program_key, "site_id": site_id,
        "normalized_alias": normalized, "display_alias": display,
    })
    return row


def resolve_site_alias(db, program: str, value: str) -> Site | None:
    program_key = normalize_program(program)
    normalized = normalize_site_alias(value)
    aliases = db.query(SiteAlias).filter_by(program=program_key, normalized_alias=normalized).all()
    if len(aliases) > 1:
        raise HTTPException(status_code=409, detail="Alias mapping is ambiguous")
    matches = [
        site for site in db.query(Site).filter_by(program=program_key).all()
        if normalize_site_alias(site.name) == normalized
    ]
    if len(matches) > 1:
        raise HTTPException(status_code=409, detail="Canonical site name is ambiguous; add an explicit mapping")
    if aliases and matches and aliases[0].site_id != matches[0].id:
        raise HTTPException(status_code=409, detail="Alias conflicts with a canonical site name")
    if aliases:
        return aliases[0].site
    return matches[0] if matches else None


def prepare_material_requisition_issue(db, program: str, site_label: str, items: list):
    """Resolve and validate every MR line before the caller changes warehouse stock."""
    program_key = normalize_program(program)
    label = str(site_label or "").strip()
    if not label:
        raise HTTPException(
            status_code=409,
            detail="MR site is blank; map the requisition to a Site before warehouse issue",
        )

    try:
        site = resolve_site_alias(db, program_key, label)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail=f"MR site '{label}' is ambiguous: {exc.detail}",
            ) from exc
        raise
    if site is None:
        raise HTTPException(
            status_code=409,
            detail=f"MR site '{label}' is not mapped to a unique Site in {program_key}; add an explicit Site alias",
        )

    prepared = []
    for item in items:
        line_label = str(getattr(item, "line_no", None) or getattr(item, "id", "?"))
        product_id = getattr(item, "product_id", None)
        if not product_id:
            raise HTTPException(status_code=400, detail=f"MR line {line_label} is missing product_id")
        product = db.query(Product).filter_by(id=product_id, program=program_key).first()
        if product is None:
            raise HTTPException(
                status_code=404,
                detail=f"MR line {line_label} product_id {product_id} is not available in {program_key}",
            )
        quantity = _decimal(getattr(item, "quantity", None))
        if quantity <= 0:
            raise HTTPException(status_code=400, detail=f"MR line {line_label} quantity must be greater than zero")
        unit = _validate_uom(product, getattr(item, "uom", ""))
        prepared.append((item, product, quantity, unit))
    return site, prepared


def prepare_material_return_receipt(db, program: str, site_label: str, items: list):
    """Resolve and validate every return line before the warehouse receipt is posted."""
    program_key = normalize_program(program)
    label = str(site_label or "").strip()
    if not label:
        raise HTTPException(
            status_code=409,
            detail="Material return site is blank; map the return to a Site before warehouse receipt",
        )

    try:
        site = resolve_site_alias(db, program_key, label)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail=f"Material return site '{label}' is ambiguous: {exc.detail}",
            ) from exc
        raise
    if site is None:
        raise HTTPException(
            status_code=409,
            detail=f"Material return site '{label}' is not mapped to a unique Site in {program_key}; add an explicit Site alias",
        )

    prepared = []
    for item in items:
        line_label = str(getattr(item, "line_no", None) or getattr(item, "id", "?"))
        product_id = getattr(item, "product_id", None)
        if not product_id:
            raise HTTPException(status_code=400, detail=f"Return line {line_label} is missing product_id")
        product = db.query(Product).filter_by(id=product_id, program=program_key).first()
        if product is None:
            raise HTTPException(
                status_code=404,
                detail=f"Return line {line_label} product_id {product_id} is not available in {program_key}",
            )
        quantity = _decimal(getattr(item, "quantity", None))
        if quantity <= 0:
            raise HTTPException(status_code=400, detail=f"Return line {line_label} quantity must be greater than zero")
        unit = _validate_uom(product, getattr(item, "uom", ""))
        prepared.append((item, product, quantity, unit))
    return site, prepared


def post_material_return_site_movement(
    db,
    *,
    program: str,
    site_id: int,
    return_id: int,
    return_number: str,
    line_id: int,
    product_id: int,
    quantity,
    uom: str,
    actor: str,
    effective_date: str,
) -> SiteMaterialMovement:
    quantity_value = _decimal(quantity)
    if quantity_value <= 0:
        raise HTTPException(status_code=400, detail="Returned quantity must be greater than zero")
    return post_site_movement(
        db,
        program=program,
        site_id=site_id,
        product_id=product_id,
        movement_type="outgoing",
        event_type="material_return_received",
        quantity_delta=-quantity_value,
        uom=uom,
        document_type="material_return",
        document_id=str(return_id),
        document_line_id=str(line_id),
        reference=str(return_number or return_id),
        note=f"Material return {return_number or return_id} received by warehouse",
        created_by=actor,
        approved_by=actor,
        effective_date=effective_date,
    )


def prepare_rollout_consumption(db, program: str, site_label: str, product_id: int, quantity, uom: str):
    """Resolve and validate explicit Field Entry consumption before posting inventory."""
    program_key = normalize_program(program)
    label = str(site_label or "").strip()
    if not label:
        raise HTTPException(
            status_code=409,
            detail="Field Entry Area is blank; map it to a Site before confirming consumption",
        )
    try:
        site = resolve_site_alias(db, program_key, label)
    except HTTPException as exc:
        if exc.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail=f"Field Entry Area '{label}' is ambiguous: {exc.detail}",
            ) from exc
        raise
    if site is None:
        raise HTTPException(
            status_code=409,
            detail=f"Field Entry Area '{label}' is not mapped to one Site in {program_key}; add an explicit Site alias",
        )

    if not product_id:
        raise HTTPException(status_code=400, detail="A numeric product_id is required for consumption confirmation")
    product = db.query(Product).filter_by(id=product_id, program=program_key).first()
    if product is None:
        raise HTTPException(
            status_code=404,
            detail=f"product_id {product_id} is not available in {program_key}",
        )
    amount = _decimal(quantity)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Confirmed consumed quantity must be greater than zero")
    unit = _validate_uom(product, uom)
    return site, product, amount, unit


def post_rollout_consumption_site_movement(
    db,
    *,
    program: str,
    site_id: int,
    rollout_record_pk: int,
    rollout_record_id: str,
    product_id: int,
    quantity,
    uom: str,
    actor: str,
    effective_date: str,
) -> SiteMaterialMovement:
    amount = _decimal(quantity)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Confirmed consumed quantity must be greater than zero")
    reference = str(rollout_record_id or rollout_record_pk)
    return post_site_movement(
        db,
        program=program,
        site_id=site_id,
        product_id=product_id,
        movement_type="outgoing",
        event_type="rollout_consumption_confirmed",
        quantity_delta=-amount,
        uom=uom,
        document_type="rollout_record",
        document_id=str(rollout_record_pk),
        document_line_id="consumption",
        reference=reference,
        note=f"Confirmed consumed material for Field Entry {reference}",
        created_by=actor,
        approved_by=actor,
        effective_date=effective_date,
    )


def post_material_requisition_site_movement(
    db,
    *,
    program: str,
    site_id: int,
    requisition_id: int,
    order_number: str,
    line_id: int,
    product_id: int,
    quantity,
    uom: str,
    actor: str,
    effective_date: str,
) -> SiteMaterialMovement:
    return post_site_movement(
        db,
        program=program,
        site_id=site_id,
        product_id=product_id,
        movement_type="incoming",
        event_type="mr_issue",
        quantity_delta=quantity,
        uom=uom,
        document_type="material_requisition",
        document_id=str(requisition_id),
        document_line_id=str(line_id),
        reference=str(order_number or requisition_id),
        note=f"Actual warehouse issue from MR {order_number or requisition_id}",
        created_by=actor,
        approved_by=actor,
        effective_date=effective_date,
    )


def reconciliation_rows(db, program: str, site_id: int | None = None, product_id: int | None = None) -> list[dict]:
    program_key = normalize_program(program)
    ledger_query = db.query(
        SiteMaterialMovement.site_id,
        SiteMaterialMovement.product_id,
        func.coalesce(func.sum(SiteMaterialMovement.quantity_delta), 0).label("ledger_qty"),
    ).filter(SiteMaterialMovement.program == program_key, SiteMaterialMovement.status == "approved")
    balances_query = db.query(SiteBalance.site_id, SiteBalance.product_id, SiteBalance.on_hand_qty).filter(
        SiteBalance.program == program_key
    )
    if site_id is not None:
        ledger_query = ledger_query.filter(SiteMaterialMovement.site_id == site_id)
        balances_query = balances_query.filter(SiteBalance.site_id == site_id)
    if product_id is not None:
        ledger_query = ledger_query.filter(SiteMaterialMovement.product_id == product_id)
        balances_query = balances_query.filter(SiteBalance.product_id == product_id)
    ledger = {
        (row.site_id, row.product_id): _decimal(row.ledger_qty or 0)
        for row in ledger_query.group_by(SiteMaterialMovement.site_id, SiteMaterialMovement.product_id).all()
    }
    balances = {
        (row.site_id, row.product_id): _decimal(row.on_hand_qty or 0)
        for row in balances_query.all()
    }
    keys = sorted(set(ledger) | set(balances))
    site_ids = {key[0] for key in keys}
    product_ids = {key[1] for key in keys}
    sites = {row.id: row.name for row in db.query(Site).filter(Site.program == program_key, Site.id.in_(site_ids)).all()} if site_ids else {}
    products = {row.id: row for row in db.query(Product).filter(Product.program == program_key, Product.id.in_(product_ids)).all()} if product_ids else {}
    result = []
    for key in keys:
        ledger_qty, balance_qty = ledger.get(key, Decimal("0")), balances.get(key, Decimal("0"))
        difference = balance_qty - ledger_qty
        product = products.get(key[1])
        result.append({
            "program": program_key,
            "site_id": key[0],
            "site": sites.get(key[0], ""),
            "product_id": key[1],
            "sku": product.sku if product else "",
            "product": product.name if product else "",
            "uom": normalize_uom(product.unit if product and product.unit else "PCS"),
            "ledger_qty": ledger_qty,
            "balance_qty": balance_qty,
            "difference": difference,
            "status": "MATCH" if difference == 0 else "MISMATCH",
        })
    return result


def _projection_snapshot(db, program: str):
    ledger_query = db.query(
        SiteMaterialMovement.site_id,
        SiteMaterialMovement.product_id,
        func.coalesce(func.sum(SiteMaterialMovement.quantity_delta), 0).label("on_hand_qty"),
    ).filter(SiteMaterialMovement.program == program, SiteMaterialMovement.status == "approved")
    ledger = {
        (row.site_id, row.product_id): _decimal(row.on_hand_qty or 0)
        for row in ledger_query.group_by(SiteMaterialMovement.site_id, SiteMaterialMovement.product_id).all()
    }
    balances = {
        (row.site_id, row.product_id): _decimal(row.on_hand_qty or 0)
        for row in db.query(SiteBalance).filter_by(program=program).all()
    }
    keys = sorted(set(ledger) | set(balances))
    if any(ledger.get(key, Decimal("0")) < 0 for key in keys):
        raise HTTPException(status_code=409, detail="Ledger totals contain negative inventory; projection rebuild was not applied")
    rows = [{
        "site_id": key[0],
        "product_id": key[1],
        "current_qty": balances.get(key, Decimal("0")),
        "ledger_qty": ledger.get(key, Decimal("0")),
        "difference": ledger.get(key, Decimal("0")) - balances.get(key, Decimal("0")),
    } for key in keys]
    digest_payload = [[row["site_id"], row["product_id"], str(row["current_qty"]), str(row["ledger_qty"])] for row in rows]
    digest = hashlib.sha256(json.dumps([program, digest_payload], separators=(",", ":")).encode("utf-8")).hexdigest()
    return rows, digest


def _lock_projection_tables(db, program: str) -> None:
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return
    mapping = bind._execution_options.get("schema_translate_map", {})
    schema = mapping.get(None) or SiteBalance.__table__.schema
    if not schema:
        schema = db.execute(text("SELECT current_schema()")).scalar_one()
    preparer = bind.dialect.identifier_preparer
    balance_table = f"{preparer.quote_schema(schema)}.{preparer.quote(SiteBalance.__tablename__)}"
    movement_table = f"{preparer.quote_schema(schema)}.{preparer.quote(SiteMaterialMovement.__tablename__)}"
    db.execute(text(f"LOCK TABLE {balance_table} IN EXCLUSIVE MODE"))
    db.execute(text(f"LOCK TABLE {movement_table} IN SHARE MODE"))


def rebuild_site_balances(db, program: str, *, apply: bool = False, expected_digest: str = "", actor: str = "") -> dict:
    program_key = normalize_program(program)
    if apply:
        _require_transaction(db)
        _lock_projection_tables(db, program_key)
    rows, digest = _projection_snapshot(db, program_key)
    if apply and digest != str(expected_digest or ""):
        raise HTTPException(status_code=409, detail="Projection changed since preview; request a new preview before applying")
    changes = [row for row in rows if row["difference"] != 0]
    if apply:
        for row in changes:
            balance = db.query(SiteBalance).filter_by(
                program=program_key, site_id=row["site_id"], product_id=row["product_id"]
            ).with_for_update().first()
            if balance is None:
                balance = SiteBalance(
                    program=program_key, site_id=row["site_id"], product_id=row["product_id"],
                    on_hand_qty=row["ledger_qty"],
                )
                db.add(balance)
            else:
                balance.on_hand_qty = row["ledger_qty"]
                balance.updated_at = datetime.now(timezone.utc)
        db.flush()
        _audit(db, "site_inventory_projection_rebuilt", program_key, actor, {
            "program": program_key,
            "preview_digest": digest,
            "changed_pairs": len(changes),
            "movement_history_changed": False,
        })
        _stage("after_rebuild_projection")
    return {
        "program": program_key,
        "mode": "applied" if apply else "preview",
        "preview_digest": digest,
        "pair_count": len(rows),
        "mismatch_count": len(changes),
        "items": rows,
        "movement_history_changed": False,
    }
