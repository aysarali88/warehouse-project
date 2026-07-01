import json
import hmac
import re
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session, joinedload, selectinload

from database import Base, SessionLocal, engine
from models import (
    AuditLog,
    IssueOrder,
    IssueOrderItem,
    MaterialRequisition,
    MaterialRequisitionItem,
    Product,
    ProductSerial,
    ReceiveOrder,
    ReceiveOrderItem,
    RolloutRecord,
    StockBalance,
    StockMovement,
    Technician,
    TechnicianBalance,
    Warehouse,
)

Base.metadata.create_all(bind=engine)


def ensure_optional_columns():
    if engine.dialect.name == "postgresql":
        statements = [
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS qr_code VARCHAR DEFAULT ''",
            "ALTER TABLE receive_orders ADD COLUMN IF NOT EXISTS receipt_date VARCHAR DEFAULT ''",
        ]
    else:
        statements = [
            "ALTER TABLE products ADD COLUMN qr_code VARCHAR DEFAULT ''",
            "ALTER TABLE receive_orders ADD COLUMN receipt_date VARCHAR DEFAULT ''",
        ]
    with engine.begin() as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
            except Exception:
                pass


ensure_optional_columns()

app = FastAPI(title="FTTH Rollout")
app.mount("/static", StaticFiles(directory="static"), name="static")


APP_USERS = {
    "admin": {"name": "Admin", "role": "Admin", "password": "Admin@123"},
    "requester": {"name": "Requester", "role": "Requester", "password": "Requester@123"},
    "approval": {"name": "Approver", "role": "Approver", "password": "Approval@123"},
    "warehouse": {"name": "Warehouse Manager", "role": "Warehouse Manager", "password": "Warehouse@123"},
}


TRIPOLI_TZ = ZoneInfo("Africa/Tripoli")


def local_today() -> str:
    return datetime.now(TRIPOLI_TZ).date().isoformat()


class LoginIn(BaseModel):
    username: str
    password: str


class WarehouseIn(BaseModel):
    name: str
    location: str = ""


class TechnicianIn(BaseModel):
    name: str
    phone: str = ""


class ProductIn(BaseModel):
    sku: str
    category: str = ""
    name: str
    item_detail: str = ""
    qr_code: str = ""
    unit: str = "PCS"
    tracking_type: Literal["bulk", "serialized"] = "bulk"
    min_stock: float = 0


class ReceiveItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    serial_numbers: list[str] = []


class ReceiveIn(BaseModel):
    warehouse_id: int
    supplier: str = ""
    receipt_number: str = ""
    receipt_date: str = ""
    created_by: str = "system"
    items: list[ReceiveItemIn]


class InventoryReceiveIn(BaseModel):
    receipt_date: str = ""
    receipt_number: str = ""
    supplier: str = ""
    warehouse_id: int
    sku: str
    name: str
    quantity: float = Field(gt=0)
    unit: str = "PCS"
    qr_code: str = ""
    category: str = ""
    created_by: str = "manager"


class InventoryAdjustmentIn(BaseModel):
    warehouse_id: int
    sku: str
    quantity: float = Field(ge=0)
    note: str = ""
    created_by: str = "manager"


class ProductPurgeIn(BaseModel):
    actor: str = "admin"
    role: str = "Admin"


class IssueItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    serial_numbers: list[str] = []


class IssueIn(BaseModel):
    warehouse_id: int
    technician_id: int
    created_by: str = "system"
    items: list[IssueItemIn]


class MaterialRequisitionItemIn(BaseModel):
    product_id: int | None = None
    part_nbr: str = ""
    model: str = ""
    description: str
    uom: str = "PCS"
    quantity: float = Field(gt=0)
    remark: str = ""


class MaterialRequisitionIn(BaseModel):
    creation_date: str = ""
    warehouse_id: int
    entity: str = "Rollout"
    project_name: str = "FTTH"
    site_id: str = ""
    site_address: str = ""
    wo_no: str = ""
    product_domain: str = "Passive"
    team_leader: str = ""
    receiver_tel: str = ""
    request_shipment_time: str = ""
    request_arrived_site_time: str = ""
    requester_name: str = ""
    requester_title: str = ""
    requester_signature: str = ""
    requester_date: str = ""
    requester_comment: str = ""
    receiver_name: str = ""
    receiver_title: str = ""
    receiver_signature: str = ""
    receiver_date: str = ""
    receiver_comment: str = ""
    created_by: str = "manager"
    issue_immediately: bool = False
    items: list[MaterialRequisitionItemIn]


class MaterialRequisitionSignatureIn(BaseModel):
    role: Literal["requester", "receiver"]
    name: str = ""
    title: str = ""
    date: str = ""
    signature: str
    comment: str = ""


class MaterialRequisitionActionIn(BaseModel):
    actor: str = "manager"
    title: str = ""
    comment: str = ""
    signature: str = ""


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def next_number(db: Session, model, prefix: str) -> str:
    return f"{prefix}-{db.query(model).count() + 1:05d}"


def log_audit(db: Session, action: str, entity_type: str, entity_id: str, actor: str, details: dict):
    db.add(
        AuditLog(
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            actor=actor or "system",
            details=json.dumps(details, ensure_ascii=False),
        )
    )


def stock_balance(db: Session, warehouse_id: int, product_id: int) -> StockBalance:
    row = (
        db.query(StockBalance)
        .filter(StockBalance.warehouse_id == warehouse_id, StockBalance.product_id == product_id)
        .first()
    )
    if row is None:
        row = StockBalance(warehouse_id=warehouse_id, product_id=product_id, quantity=0)
        db.add(row)
        db.flush()
    return row


def technician_balance(db: Session, technician_id: int, product_id: int) -> TechnicianBalance:
    row = (
        db.query(TechnicianBalance)
        .filter(TechnicianBalance.technician_id == technician_id, TechnicianBalance.product_id == product_id)
        .first()
    )
    if row is None:
        row = TechnicianBalance(technician_id=technician_id, product_id=product_id, quantity=0)
        db.add(row)
        db.flush()
    return row


def require_product(db: Session, product_id: int) -> Product:
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} not found")
    return product


def require_warehouse(db: Session, warehouse_id: int) -> Warehouse:
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise HTTPException(status_code=404, detail=f"Warehouse {warehouse_id} not found")
    return warehouse


def require_technician(db: Session, technician_id: int) -> Technician:
    technician = db.get(Technician, technician_id)
    if technician is None:
        raise HTTPException(status_code=404, detail=f"Technician {technician_id} not found")
    return technician


def validate_serial_count(product: Product, quantity: float, serial_numbers: list[str]):
    if product.tracking_type != "serialized":
        return
    if quantity != int(quantity):
        raise HTTPException(status_code=400, detail="Serialized item quantity must be a whole number")
    if len(serial_numbers) != int(quantity):
        raise HTTPException(status_code=400, detail=f"{product.sku} requires one serial number per unit")
    if len(set(serial_numbers)) != len(serial_numbers):
        raise HTTPException(status_code=400, detail=f"{product.sku} has duplicate serial numbers in the request")


def row_to_record(row: RolloutRecord) -> dict:
    return {
        "ID": row.record_id,
        "Date": row.date,
        "Supervisor Name": row.supervisor_name,
        "team leader": row.team_leader,
        "Area": row.area,
        "city": row.city,
        "Activity": row.activity,
        "item": row.item,
        "material type": row.material_type,
        "mount type": row.mount_type,
        "item serial": row.item_serial,
        "planed quantity": row.planned_quantity,
        "actual": row.actual,
        "stock remaining": row.stock_remaining,
        "staus": row.status,
        "laser": row.laser,
        "acceptance": row.acceptance,
        "scan": row.scan,
        "labeling": row.labeling,
    }


FOUR_CORE_CABLE_NAMES = {
    "EOSDC309I": "4-coreCable_70m",
    "EOSDC309J": "4-coreCable_100m",
    "EOSDC309K": "4-coreCable_150m",
    "EOSDC309L": "4-coreCable_200m",
    "EOSDC309M": "4-coreCable_300m",
    "EOSDC309N": "4-coreCable_500m",
}


def material_display_name(name: str = "", sku: str = "") -> str:
    sku_key = (sku or "").strip().upper()
    if sku_key in FOUR_CORE_CABLE_NAMES:
        return FOUR_CORE_CABLE_NAMES[sku_key]
    text = (name or "").strip()
    match = re.fullmatch(r"coreCable_(\d+m)-4", text, flags=re.IGNORECASE)
    if match:
        return f"4-coreCable_{match.group(1)}"
    return text


def product_display_name(product: Product | None) -> str:
    return material_display_name(product.name, product.sku) if product else ""


def product_to_dict(row: Product) -> dict:
    return {
        "id": row.id,
        "sku": row.sku,
        "category": row.category,
        "name": material_display_name(row.name, row.sku),
        "item_detail": row.item_detail,
        "qr_code": row.qr_code,
        "unit": row.unit,
        "tracking_type": row.tracking_type,
        "min_stock": row.min_stock,
        "status": row.status,
    }


def balance_to_dict(row: StockBalance) -> dict:
    return {
        "warehouse_id": row.warehouse_id,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "product_id": row.product_id,
        "sku": row.product.sku if row.product else "",
        "product": product_display_name(row.product),
        "unit": row.product.unit if row.product else "",
        "quantity": row.quantity,
    }


def technician_balance_to_dict(row: TechnicianBalance) -> dict:
    return {
        "technician_id": row.technician_id,
        "technician": row.technician.name if row.technician else "",
        "product_id": row.product_id,
        "sku": row.product.sku if row.product else "",
        "product": product_display_name(row.product),
        "unit": row.product.unit if row.product else "",
        "quantity": row.quantity,
    }


def movement_to_dict(row: StockMovement) -> dict:
    return {
        "id": row.id,
        "type": row.movement_type,
        "reference": row.reference,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "technician": row.technician.name if row.technician else "",
        "sku": row.product.sku if row.product else "",
        "product": product_display_name(row.product),
        "quantity": row.quantity,
        "serial_number": row.serial_number,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def receive_order_to_dict(row: ReceiveOrder) -> dict:
    items = []
    for item in row.items:
        product = item.product
        items.append(
            {
                "product_id": item.product_id,
                "sku": product.sku if product else "",
                "name": product_display_name(product),
                "unit": product.unit if product else "",
                "qr_code": product.qr_code if product else "",
                "quantity": item.quantity,
                "serial_number": item.serial_number,
            }
        )
    return {
        "id": row.id,
        "order_number": row.order_number,
        "receipt_date": row.receipt_date,
        "supplier": row.supplier,
        "warehouse_id": row.warehouse_id,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "items": items,
    }


def requisition_to_dict(row: MaterialRequisition) -> dict:
    return {
        "id": row.id,
        "order_number": row.order_number,
        "creation_date": row.creation_date,
        "warehouse_id": row.warehouse_id,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "entity": row.entity,
        "project_name": row.project_name,
        "site_id": row.site_id,
        "site_address": row.site_address,
        "wo_no": row.wo_no,
        "product_domain": row.product_domain,
        "team_leader": row.team_leader,
        "receiver_tel": row.receiver_tel,
        "request_shipment_time": row.request_shipment_time,
        "request_arrived_site_time": row.request_arrived_site_time,
        "requester_name": row.requester_name,
        "requester_title": row.requester_title,
        "requester_signature": row.requester_signature,
        "requester_date": row.requester_date,
        "requester_comment": row.requester_comment,
        "receiver_name": row.receiver_name,
        "receiver_title": row.receiver_title,
        "receiver_signature": row.receiver_signature,
        "receiver_date": row.receiver_date,
        "receiver_comment": row.receiver_comment,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "items": [
            {
                "id": item.id,
                "line_no": item.line_no,
                "product_id": item.product_id,
                "part_nbr": item.part_nbr,
                "model": item.model,
                "description": item.description,
                "uom": item.uom,
                "quantity": item.quantity,
                "remark": item.remark,
            }
            for item in row.items
        ],
    }


def issue_material_requisition_row(db: Session, row: MaterialRequisition) -> str:
    if row.status == "issued":
        raise HTTPException(status_code=400, detail="Material requisition is already issued")
    if row.status not in {"approved", "signed"}:
        raise HTTPException(status_code=400, detail="MR must be approved before warehouse issue")
    if not row.receiver_name.strip():
        raise HTTPException(status_code=400, detail="Approver Name is required before warehouse issue")

    technician_name = (row.team_leader or row.requester_name or row.receiver_name).strip()
    technician = db.query(Technician).filter(Technician.name == technician_name).first()
    if technician is None:
        technician = Technician(name=technician_name, phone=row.receiver_tel)
        db.add(technician)
        db.flush()

    issue = IssueOrder(
        order_number=next_number(db, IssueOrder, "MR-ISS"),
        warehouse_id=row.warehouse_id,
        technician_id=technician.id,
        status="confirmed",
        created_by=row.created_by,
    )
    db.add(issue)
    db.flush()

    for item in row.items:
        if not item.product_id:
            raise HTTPException(status_code=400, detail=f"MR line {item.line_no} is not linked to a product")
        product = require_product(db, item.product_id)
        balance = stock_balance(db, row.warehouse_id, item.product_id)
        if balance.quantity < item.quantity:
            warehouse_name = row.warehouse.name if row.warehouse else str(row.warehouse_id)
            material_name = product_display_name(product) or product.sku or f"product {product.id}"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock for {material_name} in {warehouse_name}. "
                    f"Requested {item.quantity}, available {balance.quantity}."
                ),
            )
        balance.quantity -= item.quantity
        technician_balance(db, technician.id, item.product_id).quantity += item.quantity
        db.add(IssueOrderItem(issue_order_id=issue.id, product_id=item.product_id, quantity=item.quantity, serial_number=""))
        db.add(
            StockMovement(
                movement_type="issue_to_technician",
                product_id=item.product_id,
                warehouse_id=row.warehouse_id,
                technician_id=technician.id,
                quantity=-item.quantity,
                reference=row.order_number,
                note="Issued from material requisition",
                created_by=row.created_by,
            )
        )

    row.status = "issued"
    log_audit(db, "issue_material_requisition", "material_requisition", row.order_number, row.created_by, {"issue_order": issue.order_number})
    return issue.order_number


@app.get("/")
def home():
    return FileResponse("static/materials_inventory.html")


@app.get("/rollout")
def rollout_home():
    return FileResponse("static/ftth_rollout.html")


@app.get("/warehouse")
def warehouse_home():
    return FileResponse("static/materials_inventory.html")


@app.post("/api/auth/login")
def login(data: LoginIn):
    key = data.username.strip().lower()
    user = APP_USERS.get(key)
    if user is None or not hmac.compare_digest(data.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {
        "success": True,
        "user": {
            "username": key,
            "name": user["name"],
            "role": user["role"],
        },
    }


@app.get("/api/auth/users")
def list_app_users():
    return {
        "success": True,
        "users": [
            {"username": username, "name": data["name"], "role": data["role"]}
            for username, data in APP_USERS.items()
        ],
    }


@app.get("/api/records")
def list_records(db: Session = Depends(db_session)):
    rows = db.query(RolloutRecord).order_by(RolloutRecord.id.desc()).all()
    return {"success": True, "records": [row_to_record(row) for row in rows]}


@app.post("/api/records")
def save_record(data: dict, db: Session = Depends(db_session)):
    record_id = data.get("ID") or f"RDP-{db.query(RolloutRecord).count() + 1:03d}"
    row = db.query(RolloutRecord).filter(RolloutRecord.record_id == record_id).first()

    if row is None:
        row = RolloutRecord(record_id=record_id)
        db.add(row)

    row.date = data.get("Date", "")
    row.supervisor_name = data.get("Supervisor Name", "")
    row.team_leader = data.get("team leader", "")
    row.area = data.get("Area", "")
    row.city = data.get("city", "")
    row.activity = data.get("Activity", "")
    row.item = data.get("item", "")
    row.material_type = data.get("material type", "")
    row.mount_type = data.get("mount type", "")
    row.item_serial = data.get("item serial", "")
    row.planned_quantity = float(data.get("planed quantity") or 0)
    row.actual = float(data.get("actual") or 0)
    row.stock_remaining = float(data.get("stock remaining") or 0)
    row.status = data.get("staus", "")
    row.laser = data.get("laser", "")
    row.acceptance = data.get("acceptance", "")
    row.scan = data.get("scan", "")
    row.labeling = data.get("labeling", "")

    db.commit()
    db.refresh(row)

    return {
        "success": True,
        "message": "Progress saved",
        "record": row_to_record(row),
    }


@app.get("/api/warehouse/summary")
def warehouse_summary(db: Session = Depends(db_session)):
    return {
        "success": True,
        "warehouses": db.query(Warehouse).count(),
        "technicians": db.query(Technician).count(),
        "products": db.query(Product).count(),
        "stock_movements": db.query(StockMovement).count(),
        "open_serials": db.query(ProductSerial).filter(ProductSerial.status.in_(["in_warehouse", "with_technician"])).count(),
    }


@app.get("/api/warehouse/bootstrap")
def warehouse_bootstrap(db: Session = Depends(db_session)):
    stock = list_stock_balances(db)
    usage = list_stock_usage(db)
    tech = list_technician_balances(db)
    rollout = list_rollout_material_usage(db)
    movements = list_stock_movements(40, db)
    audit = list_audit_logs(40, db)
    mrs = list_material_requisitions(200, db)
    receipts = list_receive_orders(60, db)
    return {
        "success": True,
        "summary": warehouse_summary(db),
        "warehouses": list_warehouses(db)["warehouses"],
        "technicians": list_technicians(db)["technicians"],
        "products": list_products(db)["products"],
        "stockBalances": stock["balances"],
        "stockUsage": usage["usage"],
        "technicianBalances": tech["balances"],
        "rolloutUsage": rollout["usage"],
        "rolloutRecords": rollout["rollout_records"],
        "movements": movements["movements"],
        "audit": audit["logs"],
        "mrs": mrs["requisitions"],
        "receipts": receipts["receipts"],
    }


@app.get("/api/warehouse/warehouses")
def list_warehouses(db: Session = Depends(db_session)):
    rows = db.query(Warehouse).order_by(Warehouse.name).all()
    return {"success": True, "warehouses": [{"id": r.id, "name": r.name, "location": r.location, "status": r.status} for r in rows]}


@app.post("/api/warehouse/warehouses")
def create_warehouse(data: WarehouseIn, db: Session = Depends(db_session)):
    row = Warehouse(name=data.name.strip(), location=data.location.strip())
    db.add(row)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Warehouse name already exists") from exc
    db.refresh(row)
    return {"success": True, "warehouse": {"id": row.id, "name": row.name, "location": row.location, "status": row.status}}


@app.get("/api/warehouse/technicians")
def list_technicians(db: Session = Depends(db_session)):
    rows = db.query(Technician).order_by(Technician.name).all()
    return {"success": True, "technicians": [{"id": r.id, "name": r.name, "phone": r.phone, "status": r.status} for r in rows]}


@app.post("/api/warehouse/technicians")
def create_technician(data: TechnicianIn, db: Session = Depends(db_session)):
    row = Technician(name=data.name.strip(), phone=data.phone.strip())
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"success": True, "technician": {"id": row.id, "name": row.name, "phone": row.phone, "status": row.status}}


@app.get("/api/warehouse/products")
def list_products(db: Session = Depends(db_session)):
    rows = db.query(Product).order_by(Product.sku).all()
    return {"success": True, "products": [product_to_dict(r) for r in rows]}


@app.post("/api/warehouse/products")
def create_product(data: ProductIn, db: Session = Depends(db_session)):
    sku = data.sku.strip()
    name = material_display_name(data.name.strip(), sku)
    row = Product(
        sku=sku,
        category=data.category.strip(),
        name=name,
        item_detail=data.item_detail.strip(),
        qr_code=data.qr_code.strip(),
        unit=data.unit.strip() or "PCS",
        tracking_type=data.tracking_type,
        min_stock=data.min_stock,
    )
    db.add(row)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Product SKU already exists") from exc
    db.refresh(row)
    return {"success": True, "product": product_to_dict(row)}


@app.post("/api/warehouse/products/{product_id}/purge")
def purge_product(product_id: int, data: ProductPurgeIn, db: Session = Depends(db_session)):
    if data.role.strip().lower() != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    product = db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    receive_order_ids = [row[0] for row in db.query(ReceiveOrderItem.receive_order_id).filter(ReceiveOrderItem.product_id == product.id).distinct().all()]
    issue_order_ids = [row[0] for row in db.query(IssueOrderItem.issue_order_id).filter(IssueOrderItem.product_id == product.id).distinct().all()]
    snapshot = product_to_dict(product)

    db.query(ProductSerial).filter(ProductSerial.product_id == product.id).delete(synchronize_session=False)
    db.query(StockBalance).filter(StockBalance.product_id == product.id).delete(synchronize_session=False)
    db.query(TechnicianBalance).filter(TechnicianBalance.product_id == product.id).delete(synchronize_session=False)
    db.query(StockMovement).filter(StockMovement.product_id == product.id).delete(synchronize_session=False)
    db.query(ReceiveOrderItem).filter(ReceiveOrderItem.product_id == product.id).delete(synchronize_session=False)
    db.query(IssueOrderItem).filter(IssueOrderItem.product_id == product.id).delete(synchronize_session=False)
    db.query(MaterialRequisitionItem).filter(MaterialRequisitionItem.product_id == product.id).delete(synchronize_session=False)

    for order_id in receive_order_ids:
        has_items = db.query(ReceiveOrderItem).filter(ReceiveOrderItem.receive_order_id == order_id).first()
        if has_items is None:
            db.query(ReceiveOrder).filter(ReceiveOrder.id == order_id).delete(synchronize_session=False)
    for order_id in issue_order_ids:
        has_items = db.query(IssueOrderItem).filter(IssueOrderItem.issue_order_id == order_id).first()
        if has_items is None:
            db.query(IssueOrder).filter(IssueOrder.id == order_id).delete(synchronize_session=False)

    db.delete(product)
    log_audit(db, "purge_product", "product", snapshot["sku"], data.actor, snapshot)
    db.commit()
    return {"success": True, "deleted": snapshot}


@app.get("/api/warehouse/stock-balances")
def list_stock_balances(db: Session = Depends(db_session)):
    rows = (
        db.query(StockBalance)
        .options(joinedload(StockBalance.warehouse), joinedload(StockBalance.product))
        .order_by(StockBalance.warehouse_id, StockBalance.product_id)
        .all()
    )
    return {"success": True, "balances": [balance_to_dict(r) for r in rows]}


@app.get("/api/warehouse/stock-usage")
def list_stock_usage(db: Session = Depends(db_session)):
    balances = (
        db.query(StockBalance)
        .options(joinedload(StockBalance.warehouse), joinedload(StockBalance.product))
        .order_by(StockBalance.warehouse_id, StockBalance.product_id)
        .all()
    )
    received_totals = {
        (warehouse_id, product_id): total or 0
        for warehouse_id, product_id, total in (
            db.query(StockMovement.warehouse_id, StockMovement.product_id, func.sum(StockMovement.quantity))
            .filter(StockMovement.warehouse_id.isnot(None), StockMovement.movement_type == "receive")
            .group_by(StockMovement.warehouse_id, StockMovement.product_id)
            .all()
        )
    }
    consumed_totals = {
        (warehouse_id, product_id): total or 0
        for warehouse_id, product_id, total in (
            db.query(StockMovement.warehouse_id, StockMovement.product_id, func.sum(-StockMovement.quantity))
            .filter(StockMovement.warehouse_id.isnot(None), StockMovement.movement_type == "issue_to_technician")
            .group_by(StockMovement.warehouse_id, StockMovement.product_id)
            .all()
        )
    }
    adjustment_totals = {
        (warehouse_id, product_id): total or 0
        for warehouse_id, product_id, total in (
            db.query(StockMovement.warehouse_id, StockMovement.product_id, func.sum(StockMovement.quantity))
            .filter(StockMovement.warehouse_id.isnot(None), StockMovement.movement_type == "adjustment")
            .group_by(StockMovement.warehouse_id, StockMovement.product_id)
            .all()
        )
    }
    usage_rows = []
    for balance in balances:
        key = (balance.warehouse_id, balance.product_id)
        total_received = received_totals.get(key, 0)
        total_consumed = consumed_totals.get(key, 0)
        total_adjustment = adjustment_totals.get(key, 0)
        remaining = balance.quantity or 0
        display_total = remaining + total_consumed
        denominator = display_total if display_total > 0 else total_received
        usage_percent = round((total_consumed / denominator) * 100, 2) if denominator else 0
        usage_rows.append(
            {
                "warehouse_id": balance.warehouse_id,
                "warehouse": balance.warehouse.name if balance.warehouse else "",
                "product_id": balance.product_id,
                "sku": balance.product.sku if balance.product else "",
                "product": product_display_name(balance.product),
                "unit": balance.product.unit if balance.product else "",
                "total_received": display_total,
                "received_movements": total_received,
                "total_consumed": total_consumed,
                "total_adjustment": total_adjustment,
                "remaining": remaining,
                "usage_percent": usage_percent,
            }
        )
    return {"success": True, "usage": usage_rows}


@app.get("/api/warehouse/technician-balances")
def list_technician_balances(db: Session = Depends(db_session)):
    rows = (
        db.query(TechnicianBalance)
        .options(joinedload(TechnicianBalance.technician), joinedload(TechnicianBalance.product))
        .order_by(TechnicianBalance.technician_id, TechnicianBalance.product_id)
        .all()
    )
    return {"success": True, "balances": [technician_balance_to_dict(r) for r in rows]}


@app.get("/api/warehouse/technician-material-usage")
def list_technician_material_usage(db: Session = Depends(db_session)):
    rows: dict[tuple[str, int], dict] = {}
    requisitions = (
        db.query(MaterialRequisition)
        .options(selectinload(MaterialRequisition.items).joinedload(MaterialRequisitionItem.product))
        .order_by(MaterialRequisition.id.asc())
        .all()
    )
    for requisition in requisitions:
        if requisition.status not in {"issued", "signed"}:
            continue
        technician_name = (requisition.receiver_name or "").strip()
        if not technician_name:
            continue
        area = (requisition.site_id or requisition.site_address or "").strip()
        for item in requisition.items:
            if not item.product_id:
                continue
            product = item.product
            key = (area or technician_name, item.product_id)
            current = rows.setdefault(
                key,
                {
                    "technician": technician_name,
                    "area": area,
                    "site_id": requisition.site_id,
                    "site_address": requisition.site_address,
                    "material": material_display_name(item.description or product_display_name(product), product.sku if product else ""),
                    "sku": item.part_nbr or (product.sku if product else ""),
                    "mr_issued_qty": 0,
                    "current_app_balance": 0,
                    "last_mr": "",
                    "last_sync": "",
                },
            )
            current["mr_issued_qty"] += item.quantity or 0
            current["last_mr"] = requisition.order_number

    balances = db.query(TechnicianBalance).all()
    for balance in balances:
        technician_name = balance.technician.name if balance.technician else ""
        if not technician_name:
            continue
        product = balance.product
        key = (technician_name, balance.product_id)
        current = rows.setdefault(
            key,
            {
                "technician": technician_name,
                "area": "",
                "site_id": "",
                "site_address": "",
                "material": product_display_name(product),
                "sku": product.sku if product else "",
                "mr_issued_qty": 0,
                "current_app_balance": 0,
                "last_mr": "",
                "last_sync": "",
            },
        )
        current["current_app_balance"] = balance.quantity or 0
        if not current["material"]:
            current["material"] = product_display_name(product)
        if not current["sku"]:
            current["sku"] = product.sku if product else ""

    today = local_today()
    usage = sorted(rows.values(), key=lambda row: (row["technician"], row["material"]))
    for row in usage:
        row["last_sync"] = today
    return {"success": True, "usage": usage}


def normalize_usage_key(value: str) -> str:
    return "".join(ch.lower() for ch in (value or "") if ch.isalnum())


def canonical_material_key(value: str) -> str:
    key = normalize_usage_key(value)
    if not key:
        return ""

    length_match = re.search(r"(\d+)m", key)
    length = length_match.group(1) if length_match else ""
    if "dropcable" in key and length:
        return f"dropcable{length}m"
    if "distributioncable" in key and length:
        return f"distributioncable{length}m"
    if "corecable" in key and length and ("4core" in key or key.endswith("4")):
        return f"4corecable{length}m"

    aliases = {
        "subbox": ["subbox", "fat2810ss8a"],
        "endbox": ["endbox", "fat2810se8a"],
        "xbox": ["xbox", "ssc2802tx8b"],
        "hubbox": ["hubbox", "fat2811sh4b"],
        "atb": ["atb", "e00atb101"],
        "bigtail": ["bigtail", "pigtail", "l0524vdd"],
        "plumringhook": ["plumringhook", "itc3301p1"],
        "stypeclamp": ["stypeclamp", "itc3103a1"],
        "polemountingassembly": ["polemountingassembly", "e00dkba04"],
        "plasticcablestoringassembly": ["plasticcablestoringassembly", "itc2102p2"],
        "metalwedgeclamping": ["metalwedgeclamping", "itc3301p103"],
    }
    for canonical, values in aliases.items():
        if any(alias in key for alias in values):
            return canonical
    return key


def is_workflow_role(value: str) -> bool:
    return normalize_usage_key(value) in {"approver", "admin"}


TECHNICIAN_USAGE_ALIASES = {
    "ali": ["علي قراب", "علي"],
    "hamza": ["حمزه بشايره", "حمزة بشايره", "حمزه", "حمزة"],
    "fathoi": ["فتحي", "fathi", "fathoi"],
}


def technician_usage_keys(name: str) -> set[str]:
    base = normalize_usage_key(name)
    keys = {base} if base else set()
    aliases = TECHNICIAN_USAGE_ALIASES.get(base, [])
    for alias in aliases:
        alias_key = normalize_usage_key(alias)
        if alias_key:
            keys.add(alias_key)
    return keys


def area_usage_keys(site_id: str, site_address: str) -> set[str]:
    source = site_id or site_address
    key = normalize_usage_key(source)
    return {key} if key else set()


@app.get("/api/warehouse/rollout-material-usage")
def list_rollout_material_usage(db: Session = Depends(db_session)):
    rollout_rows = db.query(RolloutRecord).all()
    rollout_area_usage: dict[tuple[str, str], float] = {}
    rollout_material_usage: dict[str, float] = {}
    for record in rollout_rows:
        area = (record.area or "").strip()
        material = (record.material_type or record.item or "").strip()
        if not area or not material:
            continue
        actual = record.actual or 0
        material_key = canonical_material_key(material)
        key = (normalize_usage_key(area), material_key)
        rollout_area_usage[key] = rollout_area_usage.get(key, 0) + actual
        rollout_material_usage[material_key] = rollout_material_usage.get(material_key, 0) + actual

    mr_usage = list_technician_material_usage(db)["usage"]
    rows = []
    for row in mr_usage:
        material = row["material"]
        issued = row["mr_issued_qty"] or 0
        if issued <= 0:
            continue
        material_key = canonical_material_key(material)
        area_used = sum(
            rollout_area_usage.get((area_key, material_key), 0)
            for area_key in area_usage_keys(row.get("site_id", ""), row.get("site_address", ""))
        )
        material_used = rollout_material_usage.get(material_key, 0)
        used = area_used
        remaining = issued - used
        rows.append(
            {
                **row,
                "rollout_used_qty": used,
                "rollout_actual_qty": area_used,
                "remaining_after_rollout": remaining,
                "usage_percent": (used / issued * 100) if issued else 0,
                "usage_match": "area" if area_used else ("area_not_found" if material_used else "none"),
            }
        )
    return {"success": True, "usage": rows, "rollout_records": len(rollout_rows)}


@app.get("/api/warehouse/movements")
def list_stock_movements(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(StockMovement)
        .options(
            joinedload(StockMovement.warehouse),
            joinedload(StockMovement.technician),
            joinedload(StockMovement.product),
        )
        .order_by(StockMovement.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "movements": [movement_to_dict(r) for r in rows]}


@app.get("/api/warehouse/audit-logs")
def list_audit_logs(limit: int = 50, db: Session = Depends(db_session)):
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 200)).all()
    return {
        "success": True,
        "logs": [
            {
                "id": row.id,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "actor": row.actor,
                "details": row.details,
                "created_at": row.created_at.isoformat() if row.created_at else "",
            }
            for row in rows
        ],
    }


@app.get("/api/warehouse/material-requisitions")
def list_material_requisitions(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialRequisition)
        .options(joinedload(MaterialRequisition.warehouse), selectinload(MaterialRequisition.items))
        .order_by(MaterialRequisition.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "requisitions": [requisition_to_dict(r) for r in rows]}


@app.get("/api/warehouse/material-requisitions/{requisition_id}")
def get_material_requisition(requisition_id: int, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.get("/api/warehouse/notifications")
def warehouse_notifications(user: str = "", db: Session = Depends(db_session)):
    pending = db.query(MaterialRequisition).filter(MaterialRequisition.status == "pending_approval").all()
    user_key = normalize_usage_key(user)
    approval_count = len(pending) if not user_key else sum(1 for row in pending if normalize_usage_key(row.receiver_name) == user_key)
    approved_count = db.query(MaterialRequisition).filter(MaterialRequisition.status == "approved").count()
    return {"success": True, "approval_count": approval_count, "warehouse_queue_count": approved_count}


@app.get("/api/warehouse/receive-orders")
def list_receive_orders(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(ReceiveOrder)
        .options(joinedload(ReceiveOrder.warehouse), selectinload(ReceiveOrder.items).joinedload(ReceiveOrderItem.product))
        .order_by(ReceiveOrder.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "receipts": [receive_order_to_dict(r) for r in rows]}


@app.post("/api/warehouse/material-requisitions")
def create_material_requisition(data: MaterialRequisitionIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    order_number = str(db.query(MaterialRequisition).count() + 1)
    row = MaterialRequisition(
        order_number=order_number,
        creation_date=data.creation_date,
        warehouse_id=data.warehouse_id,
        entity=data.entity,
        project_name=data.project_name,
        site_id=data.site_id,
        site_address=data.site_address,
        wo_no=data.wo_no,
        product_domain=data.product_domain,
        team_leader=data.team_leader,
        receiver_tel=data.receiver_tel,
        request_shipment_time=data.request_shipment_time,
        request_arrived_site_time=data.request_arrived_site_time,
        requester_name=data.requester_name,
        requester_title=data.requester_title,
        requester_signature=data.requester_signature,
        requester_date=data.requester_date,
        requester_comment=data.requester_comment,
        receiver_name=data.receiver_name,
        receiver_title=data.receiver_title,
        receiver_signature=data.receiver_signature,
        receiver_date=data.receiver_date,
        receiver_comment=data.receiver_comment,
        status="draft",
        created_by=data.created_by,
    )
    db.add(row)
    db.flush()

    for index, item in enumerate(data.items, start=1):
        product = db.get(Product, item.product_id) if item.product_id else None
        db.add(
            MaterialRequisitionItem(
                requisition_id=row.id,
                line_no=index,
                product_id=item.product_id,
                part_nbr=item.part_nbr or (product.sku if product else ""),
                model=item.model,
                description=material_display_name(item.description or product_display_name(product), product.sku if product else ""),
                uom=item.uom or (product.unit if product else "PCS"),
                quantity=item.quantity,
                remark=item.remark,
            )
        )

    db.flush()
    issue_order = None
    if data.issue_immediately:
        row.status = "approved"
        issue_order = issue_material_requisition_row(db, row)
    else:
        row.status = "pending_approval"

    log_audit(db, "create_material_requisition", "material_requisition", row.order_number, data.created_by, data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "issue_order": issue_order, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/signature")
def sign_material_requisition(requisition_id: int, data: MaterialRequisitionSignatureIn, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    if data.role == "requester":
        row.requester_name = data.name or row.requester_name
        row.requester_title = data.title or row.requester_title
        row.requester_date = data.date or row.requester_date
        row.requester_comment = data.comment
        row.requester_signature = data.signature
    else:
        row.receiver_name = data.name or row.receiver_name
        row.receiver_title = data.title or row.receiver_title
        row.receiver_date = data.date or row.receiver_date
        row.receiver_comment = data.comment
        row.receiver_signature = data.signature
    if row.status != "issued" and row.requester_signature and row.receiver_signature:
        row.status = "signed"
    log_audit(db, "sign_material_requisition", "material_requisition", row.order_number, data.name or "manager", data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/approve")
def approve_material_requisition(requisition_id: int, data: MaterialRequisitionActionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    if row.status not in {"pending_approval", "draft", "rejected"}:
        raise HTTPException(status_code=400, detail=f"MR cannot be approved from status {row.status}")
    actor = data.actor.strip()
    if row.receiver_name.strip() and actor and normalize_usage_key(row.receiver_name) != normalize_usage_key(actor) and not is_workflow_role(data.title):
        raise HTTPException(status_code=403, detail="Only the assigned approver can approve this MR")
    row.receiver_name = row.receiver_name or actor
    row.receiver_title = data.title or row.receiver_title
    row.receiver_date = local_today()
    row.receiver_comment = data.comment
    if data.signature:
        row.receiver_signature = data.signature
    row.status = "approved"
    log_audit(db, "approve_material_requisition", "material_requisition", row.order_number, actor or "approver", data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/reject")
def reject_material_requisition(requisition_id: int, data: MaterialRequisitionActionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    if row.status not in {"pending_approval", "draft"}:
        raise HTTPException(status_code=400, detail=f"MR cannot be rejected from status {row.status}")
    actor = data.actor.strip()
    if row.receiver_name.strip() and actor and normalize_usage_key(row.receiver_name) != normalize_usage_key(actor) and not is_workflow_role(data.title):
        raise HTTPException(status_code=403, detail="Only the assigned approver can reject this MR")
    row.receiver_name = row.receiver_name or actor
    row.receiver_title = data.title or row.receiver_title
    row.receiver_date = local_today()
    row.receiver_comment = data.comment
    row.status = "rejected"
    log_audit(db, "reject_material_requisition", "material_requisition", row.order_number, actor or "approver", data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/issue")
def issue_material_requisition(requisition_id: int, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    issue_order = issue_material_requisition_row(db, row)
    db.commit()
    db.refresh(row)
    return {"success": True, "issue_order": issue_order, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/receive")
def receive_stock(data: ReceiveIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    order = ReceiveOrder(
        order_number=data.receipt_number.strip() or next_number(db, ReceiveOrder, "GRN"),
        supplier=data.supplier.strip(),
        receipt_date=data.receipt_date,
        warehouse_id=data.warehouse_id,
        created_by=data.created_by,
    )
    db.add(order)
    db.flush()

    for item in data.items:
        product = require_product(db, item.product_id)
        validate_serial_count(product, item.quantity, item.serial_numbers)
        stock_balance(db, data.warehouse_id, item.product_id).quantity += item.quantity

        serials = item.serial_numbers if product.tracking_type == "serialized" else [""]
        for serial in serials:
            if serial:
                exists = db.query(ProductSerial).filter(ProductSerial.serial_number == serial).first()
                if exists:
                    raise HTTPException(status_code=400, detail=f"Serial already exists: {serial}")
                db.add(
                    ProductSerial(
                        product_id=item.product_id,
                        serial_number=serial,
                        status="in_warehouse",
                        warehouse_id=data.warehouse_id,
                    )
                )
            db.add(ReceiveOrderItem(receive_order_id=order.id, product_id=item.product_id, quantity=1 if serial else item.quantity, serial_number=serial))
            db.add(
                StockMovement(
                    movement_type="receive",
                    product_id=item.product_id,
                    warehouse_id=data.warehouse_id,
                    quantity=1 if serial else item.quantity,
                    serial_number=serial,
                    reference=order.order_number,
                    created_by=data.created_by,
                )
            )

    log_audit(db, "receive_stock", "receive_order", order.order_number, data.created_by, data.model_dump())
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"success": True, "order_number": order.order_number}


@app.post("/api/warehouse/receive-inventory")
def receive_inventory(data: InventoryReceiveIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    sku = data.sku.strip()
    name = material_display_name(data.name.strip(), sku)
    if not sku:
        raise HTTPException(status_code=400, detail="SKU is required")
    if not name:
        raise HTTPException(status_code=400, detail="Material name is required")

    product = db.query(Product).filter(Product.sku == sku).first()
    if product is None:
        product = Product(
            sku=sku,
            category=data.category.strip(),
            name=name,
            item_detail=name,
            qr_code=data.qr_code.strip(),
            unit=data.unit.strip() or "PCS",
            tracking_type="bulk",
            min_stock=0,
        )
        db.add(product)
        db.flush()
    else:
        product.name = material_display_name(name, sku) or product.name
        product.item_detail = product.item_detail or name
        product.unit = data.unit.strip() or product.unit or "PCS"
        product.qr_code = data.qr_code.strip() or product.qr_code
        product.category = data.category.strip() or product.category

    order = ReceiveOrder(
        order_number=data.receipt_number.strip() or next_number(db, ReceiveOrder, "GRN"),
        supplier=data.supplier.strip(),
        receipt_date=data.receipt_date,
        warehouse_id=data.warehouse_id,
        created_by=data.created_by,
    )
    db.add(order)
    db.flush()

    stock_balance(db, data.warehouse_id, product.id).quantity += data.quantity
    db.add(ReceiveOrderItem(receive_order_id=order.id, product_id=product.id, quantity=data.quantity, serial_number=""))
    db.add(
        StockMovement(
            movement_type="receive",
            product_id=product.id,
            warehouse_id=data.warehouse_id,
            quantity=data.quantity,
            serial_number="",
            reference=order.order_number,
            created_by=data.created_by,
            note=f"Inventory receive: {data.receipt_number.strip()}",
        )
    )
    log_audit(db, "receive_inventory", "receive_order", order.order_number, data.created_by, data.model_dump())
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Receipt number already exists or receive failed") from exc
    db.refresh(order)
    db.refresh(product)
    balance = stock_balance(db, data.warehouse_id, product.id)
    return {
        "success": True,
        "order_number": order.order_number,
        "product": product_to_dict(product),
        "receipt": receive_order_to_dict(order),
        "balance": balance.quantity,
    }


@app.post("/api/warehouse/adjust-inventory")
def adjust_inventory(data: InventoryAdjustmentIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    product = db.query(Product).filter(Product.sku == data.sku.strip()).first()
    if product is None:
        raise HTTPException(status_code=404, detail="SKU not found")

    balance = stock_balance(db, data.warehouse_id, product.id)
    old_quantity = balance.quantity or 0
    delta = data.quantity - old_quantity
    balance.quantity = data.quantity

    if delta:
        db.add(
            StockMovement(
                movement_type="adjustment",
                product_id=product.id,
                warehouse_id=data.warehouse_id,
                quantity=delta,
                serial_number="",
                reference=f"ADJ-{local_today()}",
                created_by=data.created_by,
                note=data.note.strip() or f"Stock adjusted from {old_quantity} to {data.quantity}",
            )
        )
    log_audit(
        db,
        "adjust_inventory",
        "stock_balance",
        f"{data.warehouse_id}:{product.id}",
        data.created_by,
        {"sku": product.sku, "old_quantity": old_quantity, "new_quantity": data.quantity, "delta": delta, "note": data.note},
    )
    db.commit()
    return {
        "success": True,
        "sku": product.sku,
        "product": product_display_name(product),
        "old_quantity": old_quantity,
        "new_quantity": data.quantity,
        "delta": delta,
    }


@app.post("/api/warehouse/issue")
def issue_to_technician(data: IssueIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    require_technician(db, data.technician_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    order = IssueOrder(
        order_number=next_number(db, IssueOrder, "ISS"),
        warehouse_id=data.warehouse_id,
        technician_id=data.technician_id,
        created_by=data.created_by,
    )
    db.add(order)
    db.flush()

    for item in data.items:
        product = require_product(db, item.product_id)
        validate_serial_count(product, item.quantity, item.serial_numbers)
        balance = stock_balance(db, data.warehouse_id, item.product_id)
        if balance.quantity < item.quantity:
            warehouse = db.get(Warehouse, data.warehouse_id)
            warehouse_name = warehouse.name if warehouse else str(data.warehouse_id)
            material_name = product_display_name(product) or product.sku or f"product {product.id}"
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Insufficient stock for {material_name} in {warehouse_name}. "
                    f"Requested {item.quantity}, available {balance.quantity}."
                ),
            )
        balance.quantity -= item.quantity
        technician_balance(db, data.technician_id, item.product_id).quantity += item.quantity

        serials = item.serial_numbers if product.tracking_type == "serialized" else [""]
        for serial in serials:
            if serial:
                serial_row = (
                    db.query(ProductSerial)
                    .filter(
                        ProductSerial.product_id == item.product_id,
                        ProductSerial.serial_number == serial,
                        ProductSerial.status == "in_warehouse",
                        ProductSerial.warehouse_id == data.warehouse_id,
                    )
                    .first()
                )
                if serial_row is None:
                    raise HTTPException(status_code=400, detail=f"Serial not available in warehouse: {serial}")
                serial_row.status = "with_technician"
                serial_row.warehouse_id = None
                serial_row.technician_id = data.technician_id

            db.add(IssueOrderItem(issue_order_id=order.id, product_id=item.product_id, quantity=1 if serial else item.quantity, serial_number=serial))
            db.add(
                StockMovement(
                    movement_type="issue_to_technician",
                    product_id=item.product_id,
                    warehouse_id=data.warehouse_id,
                    technician_id=data.technician_id,
                    quantity=-(1 if serial else item.quantity),
                    serial_number=serial,
                    reference=order.order_number,
                    created_by=data.created_by,
                )
            )

    log_audit(db, "issue_to_technician", "issue_order", order.order_number, data.created_by, data.model_dump())
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"success": True, "order_number": order.order_number}
