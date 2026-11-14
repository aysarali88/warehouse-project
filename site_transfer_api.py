"""Admin-managed Site-to-Site transfers with assigned Requester approval."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import SiteMaterialMovement, SiteTransferApproval
from site_inventory_config import integration_enabled
from site_transfer_service import (
    approve_transfer,
    create_transfer,
    decide_transfer_approval,
    dispatch_transfer,
    get_transfer,
    list_transfers,
    list_transfer_approvers,
    receive_transfer,
    reverse_transfer_dispatch,
    reverse_transfer_receipt,
)


def site_transfer_enabled(program: str, normalize_program_fn) -> bool:
    return integration_enabled("SITE_INVENTORY_TRANSFER_ENABLED", program, normalize_program_fn)


class TransferLineCreate(BaseModel):
    product_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    uom: str = Field(min_length=1, max_length=24)


class SiteTransferCreate(BaseModel):
    program: str = "FTTH"
    from_site_id: int = Field(gt=0)
    to_site_id: int = Field(gt=0)
    approver_user_id: int = Field(gt=0)
    items: list[TransferLineCreate] = Field(min_length=1, max_length=100)
    reference: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=4000)
    request_token: str = Field(min_length=16, max_length=200)


class SiteTransferAction(BaseModel):
    program: str = "FTTH"


class SiteTransferDecisionIn(SiteTransferAction):
    note: str = Field(default="", max_length=1000)


class SiteTransferDispatchIn(SiteTransferAction):
    request_token: str = Field(min_length=16, max_length=200)


class TransferReceiptLine(BaseModel):
    transfer_item_id: int = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    uom: str = Field(min_length=1, max_length=24)


class SiteTransferReceiptIn(SiteTransferDispatchIn):
    items: list[TransferReceiptLine] = Field(min_length=1, max_length=100)
    reference: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=4000)


class SiteTransferCorrectionIn(SiteTransferDispatchIn):
    note: str = Field(min_length=1, max_length=1000)
    confirm_physical_return: bool = False


def build_site_transfer_router(db_dependency, normalize_program_fn, require_roles_fn, actor_fn,
                               enabled_fn=None):
    router = APIRouter()
    gate = enabled_fn or (lambda program: site_transfer_enabled(program, normalize_program_fn))

    def authorize_and_gate(request: Request, program: str) -> str:
        require_roles_fn(request, "Admin")
        program_key = normalize_program_fn(program)
        if not gate(program_key):
            raise HTTPException(
                status_code=503,
                detail="Site-to-Site transfers are disabled until Site Inventory cutover is explicitly enabled for this program",
            )
        return actor_fn(request)

    def authorize_view(request: Request, program: str):
        user = require_roles_fn(request, "Admin", "Requester")
        program_key = normalize_program_fn(program)
        session_program = normalize_program_fn(getattr(request.state, "session_program", getattr(user, "program", "")))
        if program_key != session_program or normalize_program_fn(getattr(user, "program", program_key)) != program_key:
            raise HTTPException(status_code=403, detail="This transfer belongs to a different program")
        if not gate(program_key):
            raise HTTPException(
                status_code=503,
                detail="Site-to-Site transfers are disabled until Site Inventory cutover is explicitly enabled for this program",
            )
        return user

    @router.post("/api/warehouse/site-transfers")
    def site_transfer_create(data: SiteTransferCreate, request: Request, db: Session = Depends(db_dependency)):
        actor = authorize_and_gate(request, data.program)
        try:
            with db.begin():
                row = create_transfer(
                    db,
                    program=data.program,
                    from_site_id=data.from_site_id,
                    to_site_id=data.to_site_id,
                    items=[item.dict() for item in data.items],
                    approver_user_id=data.approver_user_id,
                    actor_user_id=int(request.state.current_user.id),
                    reference=data.reference,
                    notes=data.notes,
                    actor=actor,
                    request_token=data.request_token,
                )
            return {"success": True, "transfer": get_transfer(db, row.id, row.program)}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Transfer identity already exists; retry with the same request token") from exc

    @router.get("/api/warehouse/site-transfers")
    def site_transfer_list(
        request: Request,
        program: str = "FTTH",
        status: str = "",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=100),
        db: Session = Depends(db_dependency),
    ):
        user = authorize_view(request, program)
        approver_user_id = int(user.id) if str(user.role or "").strip().lower() == "requester" else None
        result = list_transfers(db, program, page=page, page_size=page_size, status=status,
                                approver_user_id=approver_user_id)
        return {"success": True, **result}

    @router.get("/api/warehouse/site-transfers/approvers")
    def site_transfer_approvers(
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        authorize_and_gate(request, program)
        return {"success": True, "items": list_transfer_approvers(db, normalize_program_fn(program))}

    @router.get("/api/warehouse/site-transfers/inbox-count")
    def site_transfer_inbox_count(
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        user = authorize_view(request, program)
        if str(user.role or "").strip().lower() != "requester":
            return {"success": True, "count": 0}
        count = db.query(SiteTransferApproval).filter_by(
            program=normalize_program_fn(program), approver_user_id=int(user.id), status="pending"
        ).count()
        return {"success": True, "count": count}

    @router.get("/api/warehouse/site-transfers/{transfer_id}")
    def site_transfer_detail(
        transfer_id: int,
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        user = authorize_view(request, program)
        approver_user_id = int(user.id) if str(user.role or "").strip().lower() == "requester" else None
        return {"success": True, "transfer": get_transfer(db, transfer_id, program,
                                                             approver_user_id=approver_user_id)}

    @router.post("/api/warehouse/site-transfers/{transfer_id}/approve")
    def site_transfer_approve(
        transfer_id: int,
        data: SiteTransferDecisionIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        user = require_roles_fn(request, "Admin", "Requester")
        program_key = normalize_program_fn(data.program)
        session_program = normalize_program_fn(getattr(request.state, "session_program", getattr(user, "program", "")))
        if program_key != session_program or normalize_program_fn(getattr(user, "program", program_key)) != program_key:
            raise HTTPException(status_code=403, detail="This transfer belongs to a different program")
        if not gate(program_key):
            raise HTTPException(status_code=503, detail="Site-to-Site transfers are disabled for this program")
        actor = actor_fn(request)
        try:
            with db.begin():
                if str(user.role or "").strip().lower() == "requester":
                    row = decide_transfer_approval(
                        db, transfer_id, data.program, user_id=int(user.id), actor=actor,
                        decision="approved", note=data.note,
                    )
                else:
                    row = approve_transfer(db, transfer_id, data.program, actor)
            return {"success": True, "transfer": get_transfer(db, row.id, row.program)}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Transfer changed concurrently; reload and retry") from exc

    @router.post("/api/warehouse/site-transfers/{transfer_id}/reject")
    def site_transfer_reject(
        transfer_id: int,
        data: SiteTransferDecisionIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        user = require_roles_fn(request, "Requester")
        program_key = normalize_program_fn(data.program)
        session_program = normalize_program_fn(getattr(request.state, "session_program", getattr(user, "program", "")))
        if program_key != session_program or normalize_program_fn(getattr(user, "program", program_key)) != program_key:
            raise HTTPException(status_code=403, detail="This transfer belongs to a different program")
        if not gate(program_key):
            raise HTTPException(status_code=503, detail="Site-to-Site transfers are disabled for this program")
        actor = actor_fn(request)
        try:
            with db.begin():
                row = decide_transfer_approval(
                    db, transfer_id, data.program, user_id=int(user.id), actor=actor,
                    decision="rejected", note=data.note,
                )
            return {"success": True, "transfer": get_transfer(db, row.id, row.program)}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Transfer decision changed concurrently; reload and retry") from exc

    @router.post("/api/warehouse/site-transfers/{transfer_id}/dispatch")
    def site_transfer_dispatch(
        transfer_id: int,
        data: SiteTransferDispatchIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize_and_gate(request, data.program)
        try:
            with db.begin():
                row = dispatch_transfer(db, transfer_id, data.program, actor, data.request_token)
            return {"success": True, "transfer": get_transfer(db, row.id, row.program)}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Transfer dispatch conflicts with existing inventory history") from exc

    @router.post("/api/warehouse/site-transfers/{transfer_id}/receipts")
    def site_transfer_receive(
        transfer_id: int,
        data: SiteTransferReceiptIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize_and_gate(request, data.program)
        try:
            with db.begin():
                receipt = receive_transfer(
                    db,
                    transfer_id,
                    data.program,
                    actor,
                    items=[item.dict() for item in data.items],
                    reference=data.reference,
                    notes=data.notes,
                    request_token=data.request_token,
                )
                transfer_id = receipt.transfer_id
            return {"success": True, "transfer": get_transfer(db, transfer_id, data.program)}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Transfer receipt conflicts with existing inventory history") from exc

    @router.post("/api/warehouse/site-transfers/{transfer_id}/corrections/receipts/{receipt_item_id}/reverse")
    def site_transfer_receipt_reverse(
        transfer_id: int,
        receipt_item_id: int,
        data: SiteTransferCorrectionIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize_and_gate(request, data.program)
        try:
            with db.begin():
                correction = reverse_transfer_receipt(
                    db, transfer_id, data.program, receipt_item_id, actor, data.note, data.request_token
                )
                movement = db.query(SiteMaterialMovement).filter_by(
                    program=correction.program,
                    document_type="site_transfer_correction",
                    document_id=str(correction.id),
                ).order_by(SiteMaterialMovement.id.desc()).first()
            return {
                "success": True,
                "correction_id": correction.id,
                "reversal_movement_id": movement.id if movement else None,
                "transfer": get_transfer(db, transfer_id, data.program),
            }
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Receipt correction conflicts with existing inventory history") from exc

    @router.post("/api/warehouse/site-transfers/{transfer_id}/corrections/dispatch/reverse")
    def site_transfer_dispatch_reverse(
        transfer_id: int,
        data: SiteTransferCorrectionIn,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize_and_gate(request, data.program)
        try:
            with db.begin():
                correction = reverse_transfer_dispatch(
                    db,
                    transfer_id,
                    data.program,
                    actor,
                    data.note,
                    data.request_token,
                    confirm_physical_return=data.confirm_physical_return,
                )
            return {
                "success": True,
                "correction_id": correction.id,
                "transfer": get_transfer(db, transfer_id, data.program),
            }
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Dispatch correction conflicts with existing inventory history") from exc

    return router
