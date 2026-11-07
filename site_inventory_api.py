from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Product, Site, SiteAlias, SiteBalance, SiteMaterialMovement, SiteOpeningBalanceRequest
from site_inventory_service import (
    approve_opening_balance,
    create_opening_balance_request,
    create_site_alias,
    post_site_movement,
    rebuild_site_balances,
    reject_opening_balance,
    reconciliation_rows,
    resolve_site_alias,
    reverse_site_movement,
)
from site_inventory_config import site_inventory_readiness


class SiteAliasCreate(BaseModel):
    program: str = "FTTH"
    site_id: int = Field(gt=0)
    display_alias: str = Field(min_length=1, max_length=200)


class OpeningBalanceCreate(BaseModel):
    program: str = "FTTH"
    site_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    quantity: Decimal = Field(ge=0)
    uom: str = Field(min_length=1, max_length=24)
    effective_date: str = Field(min_length=10, max_length=10)
    reference: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=4000)
    request_token: str = Field(min_length=16, max_length=200)


class OpeningBalanceReject(BaseModel):
    program: str = "FTTH"
    note: str = Field(min_length=3, max_length=1000)


class SiteMovementCreate(BaseModel):
    program: str = "FTTH"
    site_id: int = Field(gt=0)
    product_id: int = Field(gt=0)
    movement_type: Literal["incoming", "outgoing", "adjustment"]
    quantity: Decimal = Field(gt=0)
    direction: Literal["increase", "decrease"] = "increase"
    uom: str = Field(min_length=1, max_length=24)
    event_type: str = Field(min_length=3, max_length=64)
    document_type: str = Field(min_length=1, max_length=80)
    document_id: str = Field(min_length=1, max_length=120)
    document_line_id: str = Field(min_length=1, max_length=120)
    reference: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=4000)
    effective_date: str = Field(default_factory=lambda: date.today().isoformat(), min_length=10, max_length=10)
    request_token: str = Field(min_length=16, max_length=200)


class SiteMovementReverse(BaseModel):
    program: str = "FTTH"
    reference: str = Field(min_length=1, max_length=200)
    note: str = Field(min_length=1, max_length=4000)
    request_token: str = Field(min_length=16, max_length=200)


class ProjectionRebuildRequest(BaseModel):
    program: str = "FTTH"
    apply: bool = False
    expected_digest: str = ""


def build_site_inventory_router(db_dependency, normalize_program_fn, require_roles_fn, actor_fn):
    router = APIRouter()

    def authorize(request: Request, *roles: str) -> str:
        require_roles_fn(request, *roles)
        return actor_fn(request)

    @router.get("/api/warehouse/site-inventory/readiness")
    def site_inventory_readiness_status(
        request: Request,
        program: str = "FTTH",
    ):
        authorize(request, "Admin")
        return {"success": True, **site_inventory_readiness(program, normalize_program_fn)}

    @router.get("/api/warehouse/site-inventory/options")
    def site_inventory_options(
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        program_key = normalize_program_fn(program)
        sites = db.query(Site).filter_by(program=program_key).order_by(Site.name).all()
        products = db.query(Product).filter_by(program=program_key, status="active").order_by(Product.name).all()
        pending = db.query(SiteOpeningBalanceRequest).filter_by(
            program=program_key, status="pending_approval"
        ).order_by(SiteOpeningBalanceRequest.id).all()
        return {
            "success": True,
            "sites": [{"id": row.id, "name": row.name} for row in sites],
            "products": [{"id": row.id, "sku": row.sku, "name": row.name, "uom": row.unit or "PCS"} for row in products],
            "pending_openings": [{
                "id": row.id, "site_id": row.site_id, "site": row.site.name,
                "product_id": row.product_id, "product": row.product.name,
                "quantity": str(row.quantity), "uom": row.uom,
                "effective_date": row.effective_date, "reference": row.reference,
                "note": row.note, "entered_by": row.entered_by,
            } for row in pending],
        }

    @router.get("/api/warehouse/site-inventory")
    def site_inventory_list(
        request: Request,
        program: str = "FTTH",
        site_id: int | None = Query(default=None, gt=0),
        product_id: int | None = Query(default=None, gt=0),
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        query = db.query(SiteBalance).filter(SiteBalance.program == normalize_program_fn(program))
        if site_id is not None:
            query = query.filter(SiteBalance.site_id == site_id)
        if product_id is not None:
            query = query.filter(SiteBalance.product_id == product_id)
        rows = query.order_by(SiteBalance.site_id, SiteBalance.product_id).all()
        return {"success": True, "items": [{
            "program": row.program, "site_id": row.site_id, "site": row.site.name,
            "product_id": row.product_id, "sku": row.product.sku,
            "product": row.product.name, "uom": row.product.unit or "PCS",
            "on_hand_qty": str(row.on_hand_qty), "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        } for row in rows]}

    @router.get("/api/warehouse/site-inventory/aliases")
    def site_inventory_aliases(
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        rows = db.query(SiteAlias).filter_by(program=normalize_program_fn(program)).order_by(SiteAlias.normalized_alias).all()
        return {"success": True, "items": [{
            "id": row.id, "site_id": row.site_id, "site": row.site.name,
            "display_alias": row.display_alias, "normalized_alias": row.normalized_alias,
            "created_by": row.created_by, "created_at": row.created_at.isoformat() if row.created_at else "",
            "updated_by": row.updated_by, "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        } for row in rows]}

    @router.post("/api/warehouse/site-inventory/aliases")
    def site_inventory_alias_create(
        data: SiteAliasCreate,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin")
        try:
            with db.begin():
                row = create_site_alias(
                    db, program=data.program, site_id=data.site_id,
                    display_alias=data.display_alias, actor=actor,
                )
            return {"success": True, "id": row.id, "site_id": row.site_id, "display_alias": row.display_alias}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Alias collides with an existing mapping") from exc

    @router.get("/api/warehouse/site-inventory/aliases/resolve")
    def site_inventory_alias_resolve(
        request: Request,
        alias: str,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        site = resolve_site_alias(db, normalize_program_fn(program), alias)
        if site is None:
            raise HTTPException(status_code=404, detail="No exact site name or alias mapping exists")
        return {"success": True, "site_id": site.id, "site": site.name, "program": site.program}

    @router.get("/api/warehouse/site-inventory/ledger")
    def site_inventory_ledger(
        request: Request,
        site_id: int = Query(gt=0),
        product_id: int = Query(gt=0),
        program: str = "FTTH",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=500),
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        base = db.query(SiteMaterialMovement).filter_by(
            program=normalize_program_fn(program), site_id=site_id, product_id=product_id
        )
        total = base.count()
        rows = base.order_by(SiteMaterialMovement.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
        return {"success": True, "total": total, "page": page, "page_size": page_size, "items": [{
            "id": row.id, "movement_type": row.movement_type, "event_type": row.event_type,
            "quantity_delta": str(row.quantity_delta), "uom": row.uom,
            "reference": row.reference, "note": row.note, "document_type": row.document_type,
            "document_id": row.document_id, "document_line_id": row.document_line_id,
            "source_event_key": row.source_event_key, "reverses_movement_id": row.reverses_movement_id,
            "created_by": row.created_by, "approved_by": row.approved_by,
            "created_at": row.created_at.isoformat() if row.created_at else "",
        } for row in rows]}

    @router.get("/api/warehouse/site-inventory/reconciliation")
    def site_inventory_reconciliation(
        request: Request,
        program: str = "FTTH",
        site_id: int | None = Query(default=None, gt=0),
        product_id: int | None = Query(default=None, gt=0),
        db: Session = Depends(db_dependency),
    ):
        authorize(request, "Admin", "Management", "Approval")
        items = reconciliation_rows(db, normalize_program_fn(program), site_id, product_id)
        return {"success": True, "items": [{
            **row,
            "ledger_qty": str(row["ledger_qty"]),
            "balance_qty": str(row["balance_qty"]),
            "difference": str(row["difference"]),
        } for row in items]}

    @router.post("/api/warehouse/site-inventory/opening-balances")
    def site_inventory_opening_create(
        data: OpeningBalanceCreate,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin")
        try:
            with db.begin():
                row = create_opening_balance_request(
                    db, program=data.program, site_id=data.site_id, product_id=data.product_id,
                    quantity=data.quantity, uom=data.uom, effective_date=data.effective_date,
                    reference=data.reference, note=data.note, entered_by=actor,
                    request_token=data.request_token,
                )
            return {"success": True, "opening_request": {
                "id": row.id, "status": row.status, "site_id": row.site_id,
                "product_id": row.product_id, "quantity": str(row.quantity), "uom": row.uom,
                "entered_by": row.entered_by,
            }}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="An opening request already exists for this site and product") from exc

    @router.post("/api/warehouse/site-inventory/opening-balances/{request_id}/approve")
    def site_inventory_opening_approve(
        request_id: int,
        request: Request,
        program: str = "FTTH",
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin", "Approval")
        try:
            with db.begin():
                movement = approve_opening_balance(db, request_id, program, actor)
            return {"success": True, "movement_id": movement.id, "status": "approved"}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Opening balance approval conflicts with an existing movement") from exc

    @router.post("/api/warehouse/site-inventory/opening-balances/{request_id}/reject")
    def site_inventory_opening_reject(
        request_id: int,
        data: OpeningBalanceReject,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin", "Approval")
        try:
            with db.begin():
                row = reject_opening_balance(db, request_id, data.program, actor, data.note)
            return {"success": True, "status": row.status}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Opening request changed concurrently") from exc

    @router.post("/api/warehouse/site-inventory/movements")
    def site_inventory_movement_create(
        data: SiteMovementCreate,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin")
        quantity = data.quantity if data.movement_type == "incoming" or data.direction == "increase" else -data.quantity
        try:
            with db.begin():
                movement = post_site_movement(
                    db,
                    program=data.program,
                    site_id=data.site_id,
                    product_id=data.product_id,
                    movement_type=data.movement_type,
                    event_type=data.event_type,
                    quantity_delta=quantity,
                    uom=data.uom,
                    document_type=data.document_type,
                    document_id=data.document_id,
                    document_line_id=data.document_line_id,
                    reference=data.reference,
                    note=data.note,
                    created_by=actor,
                    approved_by=actor,
                    effective_date=data.effective_date,
                    request_token=data.request_token,
                )
            return {"success": True, "movement_id": movement.id, "source_event_key": movement.source_event_key}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Movement source identity already exists") from exc

    @router.post("/api/warehouse/site-inventory/movements/{movement_id}/reverse")
    def site_inventory_movement_reverse(
        movement_id: int,
        data: SiteMovementReverse,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin")
        try:
            with db.begin():
                movement = reverse_site_movement(
                    db, movement_id, data.program, actor, data.reference, data.note, data.request_token
                )
            return {"success": True, "movement_id": movement.id, "reverses_movement_id": movement.reverses_movement_id}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Movement has already been reversed") from exc

    @router.post("/api/warehouse/site-inventory/rebuild")
    def site_inventory_rebuild(
        data: ProjectionRebuildRequest,
        request: Request,
        db: Session = Depends(db_dependency),
    ):
        actor = authorize(request, "Admin") if data.apply else authorize(request, "Admin", "Management", "Approval")
        try:
            if data.apply:
                with db.begin():
                    result = rebuild_site_balances(
                        db, data.program, apply=True, expected_digest=data.expected_digest, actor=actor
                    )
            else:
                result = rebuild_site_balances(db, data.program)
            return {"success": True, **result, "items": [{
                **row,
                "current_qty": str(row["current_qty"]),
                "ledger_qty": str(row["ledger_qty"]),
                "difference": str(row["difference"]),
            } for row in result["items"]]}
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Projection rebuild conflicted with another update") from exc

    return router
