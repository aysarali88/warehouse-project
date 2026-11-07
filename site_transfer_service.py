"""Transactional Site-to-Site transfer orchestration over the Site Inventory ledger."""

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import aliased

from database import normalize_program
from models import (
    AuditLog,
    Product,
    Site,
    SiteMaterialMovement,
    SiteTransfer,
    SiteTransferCorrection,
    SiteTransferDispatch,
    SiteTransferItem,
    SiteTransferReceipt,
    SiteTransferReceiptItem,
)
from site_inventory_service import (
    _decimal,
    _require_transaction,
    normalize_uom,
    post_site_movement,
    request_token_hash,
    source_event_key,
)


def _actor(value: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise HTTPException(status_code=400, detail="An authenticated actor is required")
    return result


def _positive_quantity(value) -> Decimal:
    quantity = _decimal(value)
    if quantity <= 0:
        raise HTTPException(status_code=400, detail="Transfer quantities must be greater than zero")
    return quantity


def _clean_text(value, limit: int, label: str, *, required: bool = False) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise HTTPException(status_code=400, detail=f"{label} is required")
    if len(result) > limit:
        raise HTTPException(status_code=400, detail=f"{label} exceeds {limit} characters")
    return result


def _audit(db, action: str, transfer_id: int, actor: str, details: dict) -> None:
    import json

    db.add(AuditLog(
        action=action,
        entity_type="site_transfer",
        entity_id=str(transfer_id),
        actor=actor,
        details=json.dumps(details, ensure_ascii=False, sort_keys=True, default=str),
    ))


def _transfer(db, transfer_id: int, program: str, *, lock: bool = False) -> SiteTransfer:
    query = db.query(SiteTransfer).filter_by(id=transfer_id, program=normalize_program(program))
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Site transfer not found")
    return row


def _validate_route_and_lines(db, program: str, from_site_id: int, to_site_id: int, items: list[dict]):
    if from_site_id == to_site_id:
        raise HTTPException(status_code=400, detail="From Site and To Site must be different")
    from_site = db.query(Site).filter_by(id=from_site_id, program=program).first()
    to_site = db.query(Site).filter_by(id=to_site_id, program=program).first()
    if from_site is None or to_site is None:
        raise HTTPException(status_code=404, detail="Both sites must exist in the selected program")
    if not items:
        raise HTTPException(status_code=400, detail="At least one transfer line is required")

    normalized = []
    seen_products = set()
    for raw in items:
        product_id = int(raw.get("product_id") or 0)
        if product_id <= 0:
            raise HTTPException(status_code=400, detail="A numeric product_id is required for every transfer line")
        if product_id in seen_products:
            raise HTTPException(status_code=400, detail="A product may appear only once in a transfer")
        seen_products.add(product_id)
        product = db.query(Product).filter_by(id=product_id, program=program).first()
        if product is None or str(product.status or "active").strip().lower() != "active":
            raise HTTPException(status_code=404, detail=f"Active product {product_id} was not found in this program")
        quantity = _positive_quantity(raw.get("quantity"))
        uom = normalize_uom(raw.get("uom"))
        product_uom = normalize_uom(product.unit or "PCS")
        if uom != product_uom:
            raise HTTPException(status_code=400, detail=f"UOM for product {product_id} must match product unit ({product_uom})")
        normalized.append({"product_id": product_id, "quantity": quantity, "uom": uom})
    return normalized


def _lines_for_transfer(db, transfer_id: int) -> list[SiteTransferItem]:
    return db.query(SiteTransferItem).filter_by(transfer_id=transfer_id).order_by(
        SiteTransferItem.product_id, SiteTransferItem.id
    ).all()


def _active_receipt_totals(db, transfer_id: int) -> dict[int, Decimal]:
    reversed_movement = aliased(SiteMaterialMovement)
    rows = (
        db.query(
            SiteTransferReceiptItem.transfer_item_id,
            func.coalesce(func.sum(SiteTransferReceiptItem.received_qty), 0),
        )
        .join(SiteTransferReceipt, SiteTransferReceipt.id == SiteTransferReceiptItem.receipt_id)
        .outerjoin(reversed_movement, reversed_movement.reverses_movement_id == SiteTransferReceiptItem.movement_id)
        .filter(SiteTransferReceipt.transfer_id == transfer_id, reversed_movement.id.is_(None))
        .group_by(SiteTransferReceiptItem.transfer_item_id)
        .all()
    )
    return {int(item_id): _decimal(quantity or 0) for item_id, quantity in rows}


def _dispatch_movement(db, transfer: SiteTransfer, item: SiteTransferItem) -> SiteMaterialMovement:
    key = source_event_key(
        transfer.program, "site_transfer_dispatched", "site_transfer_dispatch", str(transfer.id), str(item.id)
    )
    movement = db.query(SiteMaterialMovement).filter_by(
        program=transfer.program, source_event_key=key
    ).first()
    if movement is None:
        raise HTTPException(status_code=409, detail="Transfer dispatch is missing a ledger movement")
    return movement


def _is_reversed(db, movement_id: int) -> bool:
    return db.query(SiteMaterialMovement.id).filter_by(reverses_movement_id=movement_id).first() is not None


def _refresh_status(db, transfer: SiteTransfer) -> None:
    dispatch = db.query(SiteTransferDispatch).filter_by(
        program=transfer.program, transfer_id=transfer.id
    ).first()
    if dispatch is None:
        return
    items = _lines_for_transfer(db, transfer.id)
    dispatch_movements = [_dispatch_movement(db, transfer, item) for item in items]
    if dispatch_movements and all(_is_reversed(db, row.id) for row in dispatch_movements):
        new_status = "cancelled"
    else:
        received = sum(_active_receipt_totals(db, transfer.id).values(), Decimal("0"))
        sent = sum((_decimal(item.requested_qty) for item in items), Decimal("0"))
        if received >= sent:
            new_status = "received"
        elif received > 0:
            new_status = "partially_received"
        else:
            new_status = "in_transit"
    if transfer.status != new_status:
        transfer.status = new_status
        transfer.updated_at = datetime.now(timezone.utc)


def _transfer_view(db, transfer: SiteTransfer) -> dict:
    items = _lines_for_transfer(db, transfer.id)
    dispatch = db.query(SiteTransferDispatch).filter_by(
        program=transfer.program, transfer_id=transfer.id
    ).first()
    totals = _active_receipt_totals(db, transfer.id) if dispatch else {}
    cancelled = transfer.status == "cancelled"
    result_items = []
    for item in items:
        requested = _decimal(item.requested_qty)
        received = totals.get(item.id, Decimal("0"))
        in_transit = Decimal("0") if not dispatch or cancelled else max(requested - received, Decimal("0"))
        result_items.append({
            "id": item.id,
            "product_id": item.product_id,
            "sku": item.product.sku,
            "product": item.product.name,
            "uom": item.uom,
            "requested_qty": str(requested),
            "sent_qty": str(requested if dispatch else Decimal("0")),
            "received_qty": str(received),
            "in_transit_qty": str(in_transit),
        })
    return {
        "id": transfer.id,
        "program": transfer.program,
        "from_site_id": transfer.from_site_id,
        "from_site": transfer.from_site.name,
        "to_site_id": transfer.to_site_id,
        "to_site": transfer.to_site.name,
        "status": transfer.status,
        "reference": transfer.reference,
        "notes": transfer.notes,
        "created_by": transfer.created_by,
        "created_at": transfer.created_at.isoformat() if transfer.created_at else "",
        "approved_by": transfer.approved_by,
        "approved_at": transfer.approved_at.isoformat() if transfer.approved_at else "",
        "dispatched_by": dispatch.dispatched_by if dispatch else "",
        "dispatched_at": dispatch.dispatched_at.isoformat() if dispatch else "",
        "items": result_items,
    }


def create_transfer(db, *, program: str, from_site_id: int, to_site_id: int, items: list[dict],
                    reference: str, notes: str, actor: str, request_token: str) -> SiteTransfer:
    _require_transaction(db)
    program_key = normalize_program(program)
    actor_name = _actor(actor)
    reference_value = _clean_text(reference, 200, "Reference")
    notes_value = _clean_text(notes, 4000, "Notes")
    token_hash = request_token_hash(program_key, request_token)
    normalized_lines = _validate_route_and_lines(db, program_key, from_site_id, to_site_id, items)

    existing = db.query(SiteTransfer).filter_by(program=program_key, request_token_hash=token_hash).first()
    if existing is not None:
        old_lines = [
            {"product_id": row.product_id, "quantity": _decimal(row.requested_qty), "uom": row.uom}
            for row in _lines_for_transfer(db, existing.id)
        ]
        exact = (
            existing.from_site_id == from_site_id
            and existing.to_site_id == to_site_id
            and existing.reference == reference_value
            and existing.notes == notes_value
            and old_lines == sorted(normalized_lines, key=lambda row: row["product_id"])
        )
        if not exact:
            raise HTTPException(status_code=409, detail="Transfer request token was already used for different details")
        return existing

    transfer = SiteTransfer(
        program=program_key,
        from_site_id=from_site_id,
        to_site_id=to_site_id,
        status="draft",
        reference=reference_value,
        notes=notes_value,
        created_by=actor_name,
        request_token_hash=token_hash,
    )
    db.add(transfer)
    db.flush()
    for line in normalized_lines:
        db.add(SiteTransferItem(
            transfer_id=transfer.id,
            product_id=line["product_id"],
            requested_qty=line["quantity"],
            uom=line["uom"],
        ))
    db.flush()
    _audit(db, "site_transfer_created", transfer.id, actor_name, {
        "program": program_key,
        "from_site_id": from_site_id,
        "to_site_id": to_site_id,
        "item_count": len(normalized_lines),
        "reference": reference_value,
    })
    return transfer


def approve_transfer(db, transfer_id: int, program: str, actor: str) -> SiteTransfer:
    _require_transaction(db)
    row = _transfer(db, transfer_id, program, lock=True)
    actor_name = _actor(actor)
    if row.status == "draft":
        row.status = "approved"
        row.approved_by = actor_name
        row.approved_at = datetime.now(timezone.utc)
        row.updated_at = row.approved_at
        _audit(db, "site_transfer_approved", row.id, actor_name, {"program": row.program})
    elif row.status not in {"approved", "in_transit", "partially_received", "received"}:
        raise HTTPException(status_code=409, detail="Only a draft transfer can be approved")
    return row


def dispatch_transfer(db, transfer_id: int, program: str, actor: str, request_token: str) -> SiteTransfer:
    _require_transaction(db)
    row = _transfer(db, transfer_id, program, lock=True)
    actor_name = _actor(actor)
    token_hash = request_token_hash(row.program, request_token)
    existing_token = db.query(SiteTransferDispatch).filter_by(
        program=row.program, request_token_hash=token_hash
    ).first()
    if existing_token is not None:
        if existing_token.transfer_id != row.id:
            raise HTTPException(status_code=409, detail="Dispatch request token was already used for another transfer")
        return row
    existing = db.query(SiteTransferDispatch).filter_by(program=row.program, transfer_id=row.id).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="This transfer has already been dispatched")
    if row.status != "approved":
        raise HTTPException(status_code=409, detail="Only an approved transfer can be dispatched")

    items = _lines_for_transfer(db, row.id)
    if not items:
        raise HTTPException(status_code=409, detail="Transfer has no lines")
    for item in items:
        product = db.query(Product).filter_by(id=item.product_id, program=row.program).with_for_update().first()
        if product is None or str(product.status or "active").strip().lower() != "active":
            raise HTTPException(status_code=409, detail=f"Product {item.product_id} is no longer active")
        unit = normalize_uom(product.unit or "PCS")
        if unit != item.uom:
            raise HTTPException(status_code=409, detail=f"Product {item.product_id} UOM changed after transfer creation")
        post_site_movement(
            db,
            program=row.program,
            site_id=row.from_site_id,
            product_id=item.product_id,
            movement_type="outgoing",
            event_type="site_transfer_dispatched",
            quantity_delta=-_decimal(item.requested_qty),
            uom=item.uom,
            document_type="site_transfer_dispatch",
            document_id=str(row.id),
            document_line_id=str(item.id),
            reference=row.reference or f"ST-{row.id}",
            note=row.notes or f"Dispatch for site transfer {row.id}",
            created_by=actor_name,
            approved_by=actor_name,
            effective_date=datetime.now(timezone.utc).date().isoformat(),
            request_token=f"site-transfer-dispatch:{row.id}:{item.id}",
        )
    dispatch = SiteTransferDispatch(
        program=row.program,
        transfer_id=row.id,
        request_token_hash=token_hash,
        reference=row.reference,
        dispatched_by=actor_name,
    )
    db.add(dispatch)
    db.flush()
    row.status = "in_transit"
    row.updated_at = datetime.now(timezone.utc)
    _audit(db, "site_transfer_dispatched", row.id, actor_name, {
        "program": row.program, "item_count": len(items),
    })
    return row


def receive_transfer(db, transfer_id: int, program: str, actor: str, *, items: list[dict],
                     reference: str, notes: str, request_token: str) -> SiteTransferReceipt:
    _require_transaction(db)
    row = _transfer(db, transfer_id, program, lock=True)
    actor_name = _actor(actor)
    reference_value = _clean_text(reference, 200, "Receipt reference")
    notes_value = _clean_text(notes, 4000, "Receipt notes")
    token_hash = request_token_hash(row.program, request_token)
    normalized = []
    seen = set()
    for raw in items:
        transfer_item_id = int(raw.get("transfer_item_id") or 0)
        quantity = _positive_quantity(raw.get("quantity"))
        uom = normalize_uom(raw.get("uom"))
        if transfer_item_id <= 0 or transfer_item_id in seen:
            raise HTTPException(status_code=400, detail="Each receipt must contain unique valid transfer_item_id values")
        seen.add(transfer_item_id)
        normalized.append({"transfer_item_id": transfer_item_id, "quantity": quantity, "uom": uom})
    if not normalized:
        raise HTTPException(status_code=400, detail="At least one received line is required")

    replay = db.query(SiteTransferReceipt).filter_by(program=row.program, request_token_hash=token_hash).first()
    if replay is not None:
        old_lines = [
            {"transfer_item_id": item.transfer_item_id, "quantity": _decimal(item.received_qty), "uom": item.uom}
            for item in db.query(SiteTransferReceiptItem).filter_by(receipt_id=replay.id).order_by(
                SiteTransferReceiptItem.transfer_item_id
            ).all()
        ]
        exact = (
            replay.transfer_id == row.id
            and replay.reference == reference_value
            and replay.notes == notes_value
            and old_lines == sorted(normalized, key=lambda value: value["transfer_item_id"])
        )
        if not exact:
            raise HTTPException(status_code=409, detail="Receipt request token was already used for different details")
        return replay

    dispatch = db.query(SiteTransferDispatch).filter_by(program=row.program, transfer_id=row.id).first()
    if dispatch is None or row.status == "cancelled":
        raise HTTPException(status_code=409, detail="Only a dispatched transfer can receive material")
    totals = _active_receipt_totals(db, row.id)
    item_map = {item.id: item for item in _lines_for_transfer(db, row.id)}
    dispatch_movements = [_dispatch_movement(db, row, item) for item in item_map.values()]
    if any(_is_reversed(db, movement.id) for movement in dispatch_movements):
        raise HTTPException(status_code=409, detail="A reversed dispatch cannot receive further material")

    validated = []
    for line in normalized:
        transfer_item = item_map.get(line["transfer_item_id"])
        if transfer_item is None:
            raise HTTPException(status_code=404, detail="Receipt line does not belong to this transfer")
        if line["uom"] != transfer_item.uom:
            raise HTTPException(status_code=400, detail="Receipt UOM must match the dispatched transfer line")
        product = db.query(Product).filter_by(id=transfer_item.product_id, program=row.program).with_for_update().first()
        if product is None or str(product.status or "active").strip().lower() != "active":
            raise HTTPException(status_code=409, detail=f"Product {transfer_item.product_id} is no longer active")
        if normalize_uom(product.unit or "PCS") != transfer_item.uom:
            raise HTTPException(status_code=409, detail=f"Product {transfer_item.product_id} UOM changed after dispatch")
        remaining = _decimal(transfer_item.requested_qty) - totals.get(transfer_item.id, Decimal("0"))
        if line["quantity"] > remaining:
            raise HTTPException(status_code=409, detail=f"Receipt exceeds remaining in-transit quantity for line {transfer_item.id}")
        validated.append((transfer_item, line["quantity"], line["uom"]))

    receipt = SiteTransferReceipt(
        program=row.program,
        transfer_id=row.id,
        request_token_hash=token_hash,
        reference=reference_value,
        notes=notes_value,
        received_by=actor_name,
    )
    db.add(receipt)
    db.flush()
    for transfer_item, quantity, uom in sorted(validated, key=lambda value: value[0].id):
        movement = post_site_movement(
            db,
            program=row.program,
            site_id=row.to_site_id,
            product_id=transfer_item.product_id,
            movement_type="incoming",
            event_type="site_transfer_received",
            quantity_delta=quantity,
            uom=uom,
            document_type="site_transfer_receipt",
            document_id=str(receipt.id),
            document_line_id=str(transfer_item.id),
            reference=reference_value or row.reference or f"ST-{row.id}",
            note=notes_value or f"Receipt for site transfer {row.id}",
            created_by=actor_name,
            approved_by=actor_name,
            effective_date=datetime.now(timezone.utc).date().isoformat(),
            request_token=f"site-transfer-receipt:{receipt.id}:{transfer_item.id}",
        )
        db.add(SiteTransferReceiptItem(
            receipt_id=receipt.id,
            transfer_item_id=transfer_item.id,
            movement_id=movement.id,
            received_qty=quantity,
            uom=uom,
        ))
    db.flush()
    _refresh_status(db, row)
    _audit(db, "site_transfer_received", row.id, actor_name, {
        "program": row.program,
        "receipt_id": receipt.id,
        "line_count": len(validated),
        "status": row.status,
    })
    return receipt


def _existing_correction(db, program: str, token_hash: str, transfer_id: int,
                         correction_type: str, receipt_item_id: int | None, note: str):
    row = db.query(SiteTransferCorrection).filter_by(program=program, request_token_hash=token_hash).first()
    if row is None:
        return None
    if (row.transfer_id, row.correction_type, row.receipt_item_id, row.note) != (
        transfer_id, correction_type, receipt_item_id, note
    ):
        raise HTTPException(status_code=409, detail="Correction request token was already used for different details")
    return row


def reverse_transfer_receipt(db, transfer_id: int, program: str, receipt_item_id: int, actor: str,
                             note: str, request_token: str) -> SiteTransferCorrection:
    _require_transaction(db)
    row = _transfer(db, transfer_id, program, lock=True)
    actor_name = _actor(actor)
    note_value = _clean_text(note, 1000, "Correction note", required=True)
    token_hash = request_token_hash(row.program, request_token)
    previous = _existing_correction(db, row.program, token_hash, row.id, "reverse_receipt", receipt_item_id, note_value)
    if previous is not None:
        return previous
    receipt_item = (
        db.query(SiteTransferReceiptItem)
        .join(SiteTransferReceipt, SiteTransferReceipt.id == SiteTransferReceiptItem.receipt_id)
        .filter(SiteTransferReceipt.transfer_id == row.id, SiteTransferReceiptItem.id == receipt_item_id)
        .with_for_update()
        .first()
    )
    if receipt_item is None:
        raise HTTPException(status_code=404, detail="Receipt item was not found for this transfer")
    original = db.query(SiteMaterialMovement).filter_by(id=receipt_item.movement_id, program=row.program).with_for_update().first()
    if original is None or _is_reversed(db, original.id):
        raise HTTPException(status_code=409, detail="Receipt movement has already been reversed")

    correction = SiteTransferCorrection(
        program=row.program,
        transfer_id=row.id,
        receipt_item_id=receipt_item.id,
        correction_type="reverse_receipt",
        request_token_hash=token_hash,
        note=note_value,
        created_by=actor_name,
    )
    db.add(correction)
    db.flush()
    movement = post_site_movement(
        db,
        program=row.program,
        site_id=original.site_id,
        product_id=original.product_id,
        movement_type="reversal",
        event_type="site_transfer_receipt_reversed",
        quantity_delta=-_decimal(original.quantity_delta),
        uom=original.uom,
        document_type="site_transfer_correction",
        document_id=str(correction.id),
        document_line_id=str(receipt_item.id),
        reference=original.reference,
        note=note_value,
        created_by=actor_name,
        approved_by=actor_name,
        effective_date=datetime.now(timezone.utc).date().isoformat(),
        request_token=f"site-transfer-correction:{correction.id}:receipt:{receipt_item.id}",
        reverses_movement_id=original.id,
    )
    db.flush()
    _refresh_status(db, row)
    _audit(db, "site_transfer_receipt_reversed", row.id, actor_name, {
        "program": row.program, "correction_id": correction.id, "receipt_item_id": receipt_item.id,
        "movement_id": movement.id, "reverses_movement_id": original.id,
    })
    return correction


def reverse_transfer_dispatch(db, transfer_id: int, program: str, actor: str, note: str,
                              request_token: str, *, confirm_physical_return: bool) -> SiteTransferCorrection:
    _require_transaction(db)
    row = _transfer(db, transfer_id, program, lock=True)
    actor_name = _actor(actor)
    note_value = _clean_text(note, 1000, "Correction note", required=True)
    if not confirm_physical_return:
        raise HTTPException(status_code=400, detail="Confirm that dispatched material is physically back at the From Site")
    token_hash = request_token_hash(row.program, request_token)
    previous = _existing_correction(db, row.program, token_hash, row.id, "reverse_dispatch", None, note_value)
    if previous is not None:
        return previous
    dispatch = db.query(SiteTransferDispatch).filter_by(program=row.program, transfer_id=row.id).with_for_update().first()
    if dispatch is None:
        raise HTTPException(status_code=409, detail="Only a dispatched transfer can be corrected")
    active_received = sum(_active_receipt_totals(db, row.id).values(), Decimal("0"))
    if active_received != 0:
        raise HTTPException(status_code=409, detail="Reverse all active receipts before reversing the dispatch")
    items = _lines_for_transfer(db, row.id)
    originals = [_dispatch_movement(db, row, item) for item in items]
    if any(_is_reversed(db, movement.id) for movement in originals):
        raise HTTPException(status_code=409, detail="Dispatch has already been reversed")

    correction = SiteTransferCorrection(
        program=row.program,
        transfer_id=row.id,
        receipt_item_id=None,
        correction_type="reverse_dispatch",
        request_token_hash=token_hash,
        note=note_value,
        created_by=actor_name,
    )
    db.add(correction)
    db.flush()
    for item, original in zip(items, originals):
        post_site_movement(
            db,
            program=row.program,
            site_id=original.site_id,
            product_id=original.product_id,
            movement_type="reversal",
            event_type="site_transfer_dispatch_reversed",
            quantity_delta=-_decimal(original.quantity_delta),
            uom=original.uom,
            document_type="site_transfer_correction",
            document_id=str(correction.id),
            document_line_id=str(item.id),
            reference=row.reference or f"ST-{row.id}",
            note=note_value,
            created_by=actor_name,
            approved_by=actor_name,
            effective_date=datetime.now(timezone.utc).date().isoformat(),
            request_token=f"site-transfer-correction:{correction.id}:dispatch:{item.id}",
            reverses_movement_id=original.id,
        )
    db.flush()
    _refresh_status(db, row)
    _audit(db, "site_transfer_dispatch_reversed", row.id, actor_name, {
        "program": row.program, "correction_id": correction.id, "item_count": len(items),
        "physical_return_confirmed": True,
    })
    return correction


def get_transfer(db, transfer_id: int, program: str) -> dict:
    return _transfer_view(db, _transfer(db, transfer_id, program))


def list_transfers(db, program: str, *, page: int = 1, page_size: int = 50, status: str = "") -> dict:
    program_key = normalize_program(program)
    query = db.query(SiteTransfer).filter_by(program=program_key)
    status_value = str(status or "").strip().lower()
    if status_value:
        allowed = {"draft", "approved", "in_transit", "partially_received", "received", "cancelled"}
        if status_value not in allowed:
            raise HTTPException(status_code=400, detail="Unsupported transfer status filter")
        query = query.filter(SiteTransfer.status == status_value)
    total = query.count()
    rows = query.order_by(SiteTransfer.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "items": [_transfer_view(db, row) for row in rows],
    }
