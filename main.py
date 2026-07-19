import csv
import io
import json
import hmac
import logging
import os
import re
import smtplib
import time
import urllib.parse
import urllib.request
import ssl
import bcrypt
from datetime import datetime, timezone, timedelta
from email.utils import formataddr
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook, load_workbook
from email.message import EmailMessage
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, text
from sqlalchemy.orm import Session, joinedload, selectinload

from database import Base, SessionLocal, engine
from models import (
    AuditLog,
    AppUser,
    IssueOrder,
    IssueOrderItem,
    MaterialRequisition,
    MaterialRequisitionItem,
    MaterialReturn,
    MaterialReturnItem,
    MaterialScanLog,
    MaterialTransfer,
    MaterialTransferItem,
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

WAREHOUSE_CACHE: dict[str, tuple[float, dict]] = {}
WAREHOUSE_CACHE_TTL = 25
ROLLOUT_CSV_CACHE: tuple[float, list[dict], str] | None = None
ROLLOUT_CSV_CACHE_TTL = 60
ROLLOUT_DAILY_PROGRESS_SHEET_ID = "1ZT9e9acJ9Y60J4f_DIFZiYyHa8GvNZdlTvpucHju7Ec"
ROLLOUT_DAILY_PROGRESS_GID = "440090582"
DEFAULT_ROLLOUT_DAILY_PROGRESS_LIVE_CSV_URL = f"https://docs.google.com/spreadsheets/d/{ROLLOUT_DAILY_PROGRESS_SHEET_ID}/gviz/tq?tqx=out:csv&gid={ROLLOUT_DAILY_PROGRESS_GID}"
DEFAULT_ROLLOUT_DAILY_PROGRESS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRI1yMD_QsfGAQY3IpwY9X9B3VBO59X_TEGKxUSMQ2S3ciCDbf3lPPGUyXuLrR5os9NI4SBwcyOTWt7/pub?gid=440090582&single=true&output=csv"
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)


def ensure_optional_columns():
    if engine.dialect.name == "postgresql":
        statements = [
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS qr_code VARCHAR DEFAULT ''",
            "ALTER TABLE products ADD COLUMN IF NOT EXISTS part_number VARCHAR DEFAULT ''",
            "ALTER TABLE receive_orders ADD COLUMN IF NOT EXISTS receipt_date VARCHAR DEFAULT ''",
            "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS email VARCHAR DEFAULT ''",
            "ALTER TABLE app_users ADD COLUMN IF NOT EXISTS warehouse_name VARCHAR DEFAULT ''",
            "ALTER TABLE material_requisitions ADD COLUMN IF NOT EXISTS return_reason TEXT DEFAULT ''",
        ]
    else:
        statements = [
            "ALTER TABLE products ADD COLUMN qr_code VARCHAR DEFAULT ''",
            "ALTER TABLE products ADD COLUMN part_number VARCHAR DEFAULT ''",
            "ALTER TABLE receive_orders ADD COLUMN receipt_date VARCHAR DEFAULT ''",
            "ALTER TABLE app_users ADD COLUMN email VARCHAR DEFAULT ''",
            "ALTER TABLE app_users ADD COLUMN warehouse_name VARCHAR DEFAULT ''",
            "ALTER TABLE material_requisitions ADD COLUMN return_reason TEXT DEFAULT ''",
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


APP_USERS = [
    {"username": "Aysar", "name": "Aysar", "role": "Admin", "password_hash": "$2b$12$3c72OtpvCsB4.CNyImCvcuQN//O7KqonCK3QuZWJs3jO/oUH6DMMO"},
    {"username": "Hamza", "name": "Hamza", "role": "Admin", "password_hash": "$2b$12$xAUkLrhxFwUXzQtTrrs86OQnEUl1a16kQhQtMlBtrfIN3HvHSZTVK"},
    {"username": "Aysar", "name": "Aysar", "role": "Requester", "password_hash": "$2b$12$xA.6M2y7h5OiplZjVzMi8.qsIoHbsjzt8.1zVBKQidm6u3UYiHYum"},
    {"username": "Hamza", "name": "Hamza", "role": "Requester", "password_hash": "$2b$12$LKYijkxCRZVxKPeY0AIA/O2I4actBdhNWVefJ7CN9c/1ituhQzt7q"},
    {"username": "Ryadh", "name": "Ryadh", "role": "Requester", "password_hash": "$2b$12$WiquFYrheGfO2LtKtswLo.J85oZ3IzlnI0md6tsQQTY4VqXR6hX12"},
    {"username": "Adel", "name": "Adel", "role": "Requester", "password_hash": "$2b$12$IaMjT3MNkht3xqCaj7jP4e2LmNwN7BpPzjs8YyS69pIHzX/6xZSPm"},
    {"username": "Nadeer", "name": "Nadeer", "role": "Requester", "password_hash": "$2b$12$DHRZmTb0yv1Qn7KtgV0L3Ongy6HznxkkLO1SN15v/3rsKSptFyOcq"},
    {"username": "Ghassan", "name": "Ghassan", "role": "Requester", "password_hash": "$2b$12$zEzqVlt6sJZgj7kQCsWY3.BElMy5nICAHRKq4dUcRvKrQba5B0Ef6"},
    {"username": "Mustafa", "name": "Mustafa", "role": "Approval", "password_hash": "$2b$12$7P79odcv0uV9bKpbOY6NWe0omDZ3ZYgCsFWp3me8i7cveBjcTU6YO"},
    {"username": "Tripoli", "name": "Tripoli", "role": "Warehouse Manager", "password_hash": "$2b$12$BqOEHrljHOmLipn6j4g1j.nKJpWWrp2SXu9fWw2.cUoZ434jKQCfy", "warehouse_name": "Tripoli"},
    {"username": "Misurata", "name": "Misurata", "role": "Warehouse Manager", "password_hash": "$2b$12$BqOEHrljHOmLipn6j4g1j.nKJpWWrp2SXu9fWw2.cUoZ434jKQCfy", "warehouse_name": "Misurata"},
    {"username": "FreeZone", "name": "FreeZone", "role": "Warehouse Manager", "password_hash": "$2b$12$dLg/7wO3.EBdDBzislnwlujcdQoMlPwrGi6H61X3OwMIiq3PgoIQS", "warehouse_name": "FreeZone"},
]

TEMP_MR_WAREHOUSE_MANAGER_OVERRIDE = "Misurata"


try:
    TRIPOLI_TZ = ZoneInfo("Africa/Tripoli")
except Exception:
    TRIPOLI_TZ = timezone(timedelta(hours=2))


def local_today() -> str:
    return datetime.now(TRIPOLI_TZ).date().isoformat()


def app_user_key(username: str, role: str) -> tuple[str, str]:
    return (username.strip().lower(), role.strip().lower())


def is_bcrypt_hash(value: str = "") -> bool:
    text = str(value or "")
    return text.startswith(("$2a$", "$2b$", "$2y$"))


def is_legacy_password_hash(value: str = "") -> bool:
    return str(value or "").startswith("pbkdf2_sha256$")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, stored: str = "") -> bool:
    if not password or not stored:
        return False
    if is_bcrypt_hash(stored):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False
    if is_legacy_password_hash(stored):
        return False
    return hmac.compare_digest(password, stored)


def serialize_app_user(row, fallback: bool = False) -> dict:
    return {
        "id": getattr(row, "id", "") or "",
        "username": row["username"] if fallback else row.username,
        "name": (row.get("name") if fallback else row.name) or (row["username"] if fallback else row.username),
        "role": row["role"] if fallback else row.role,
        "email": (row.get("email") if fallback else row.email) or "",
        "warehouse_name": (row.get("warehouse_name") if fallback else row.warehouse_name) or "",
        "is_fallback": fallback,
    }


def fallback_user_template(username: str, role: str) -> dict | None:
    key = app_user_key(username, role)
    return next((row for row in APP_USERS if app_user_key(row["username"], row["role"]) == key), None)


def migrate_app_user_password_hashes() -> None:
    with SessionLocal() as db:
        rows = db.query(AppUser).all()
        changed = False
        for row in rows:
            if row.password_hash and not is_bcrypt_hash(row.password_hash) and not is_legacy_password_hash(row.password_hash) and len(str(row.password_hash)) <= 72:
                row.password_hash = hash_password(row.password_hash)
                changed = True
        if changed:
            db.commit()


migrate_app_user_password_hashes()


def sync_product_part_numbers() -> None:
    with SessionLocal() as db:
        rows = db.query(Product).all()
        changed = False
        for row in rows:
            desired = product_part_number(row.sku, row.part_number)
            if desired != (row.part_number or ""):
                row.part_number = desired
                changed = True
        if changed:
            db.commit()



def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [part.strip() for part in raw.split(",") if part.strip()]


def send_email(to_emails: list[str], subject: str, body: str) -> bool:
    host = os.getenv("SMTP_HOST", "").strip()
    try:
        port = int(os.getenv("SMTP_PORT", "587"))
    except ValueError:
        logger.warning("MR email skipped: SMTP_PORT is invalid")
        return False
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_email = os.getenv("EMAIL_FROM", username).strip()
    from_name = os.getenv("EMAIL_FROM_NAME", "Global Technology Company").strip()
    if not host or not port or not username or not password or not from_email or not to_emails:
        logger.warning("MR email skipped: SMTP settings or recipients are missing")
        return False

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["Reply-To"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = subject
    msg.set_content(body)

    use_ssl = env_bool("SMTP_USE_SSL", port == 465)
    use_tls = env_bool("SMTP_USE_TLS", not use_ssl and port != 465)
    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=15, context=context) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            if use_tls:
                smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(msg)
    return True


def approval_notification_emails(db: Session) -> list[str]:
    rows = (
        db.query(AppUser)
        .filter(AppUser.status == "active", func.lower(AppUser.role) == "approval")
        .all()
    )
    emails = [row.email.strip() for row in rows if getattr(row, "email", "") and "@" in row.email]
    return emails or env_list("APPROVAL_EMAIL_RECIPIENTS")


def normalize_email_list(values: list[str]) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for value in values:
        email = str(value or "").strip()
        if "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        emails.append(email)
    return emails


def active_user_emails(db: Session, role: str, identifiers: list[str] | None = None) -> list[str]:
    query = db.query(AppUser).filter(AppUser.status == "active", func.lower(AppUser.role) == role.strip().lower())
    rows = query.all()
    if identifiers:
        keys = {normalize_usage_key(value) for value in identifiers if str(value or "").strip()}
        rows = [
            row
            for row in rows
            if normalize_usage_key(row.username) in keys
            or normalize_usage_key(row.name) in keys
            or normalize_usage_key(getattr(row, "warehouse_name", "")) in keys
        ]
    return normalize_email_list([row.email for row in rows])


def requester_notification_emails(row: MaterialRequisition, db: Session) -> list[str]:
    identifiers = [row.requester_name, row.created_by]
    return active_user_emails(db, "requester", identifiers)


def warehouse_manager_handles_mr(viewer: str, row: MaterialRequisition) -> bool:
    viewer_key = normalize_usage_key(viewer)
    override_key = normalize_usage_key(TEMP_MR_WAREHOUSE_MANAGER_OVERRIDE)
    if override_key:
        return viewer_key == override_key
    return normalize_usage_key(row.warehouse.name if row.warehouse else "") == viewer_key


def warehouse_manager_notification_emails(row: MaterialRequisition, db: Session) -> list[str]:
    identifiers = [row.site_address or TEMP_MR_WAREHOUSE_MANAGER_OVERRIDE or (row.warehouse.name if row.warehouse else "")]
    return active_user_emails(db, "warehouse manager", identifiers)


def transfer_source_warehouse_manager_emails(row: MaterialTransfer, db: Session) -> list[str]:
    identifiers = [row.from_warehouse.name if row.from_warehouse else ""]
    return active_user_emails(db, "warehouse manager", identifiers)


def is_source_warehouse_manager(actor: str, row: MaterialTransfer, db: Session) -> bool:
    actor_key = normalize_usage_key(actor)
    warehouse_key = normalize_usage_key(row.from_warehouse.name if row.from_warehouse else "")
    if not actor_key or not warehouse_key:
        return False
    rows = (
        db.query(AppUser)
        .filter(AppUser.status == "active", func.lower(AppUser.role) == "warehouse manager")
        .all()
    )
    return any(
        (normalize_usage_key(user.username) == actor_key or normalize_usage_key(user.name) == actor_key)
        and normalize_usage_key(getattr(user, "warehouse_name", "")) == warehouse_key
        for user in rows
    )


def notify_mr_email(row: MaterialRequisition, db: Session, recipients: list[str], subject: str, lines: list[str], audit_prefix: str) -> None:
    recipients = normalize_email_list(recipients)
    if not recipients:
        logger.warning("MR email skipped: no recipients configured for %s", audit_prefix)
        try:
            log_audit(
                db,
                f"{audit_prefix}_skipped",
                "material_requisition",
                row.order_number,
                "system",
                {"reason": "no_recipients", "recipients": []},
            )
            db.commit()
        except Exception:
            db.rollback()
        return
    try:
        send_email(recipients, subject, "\n".join(lines))
        log_audit(
            db,
            f"{audit_prefix}_sent",
            "material_requisition",
            row.order_number,
            "system",
            {"recipients": recipients},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to send MR email: %s", audit_prefix)
        try:
            log_audit(
                db,
                f"{audit_prefix}_failed",
                "material_requisition",
                row.order_number,
                "system",
                {"recipients": recipients},
            )
            db.commit()
        except Exception:
            db.rollback()


def notify_transfer_email(row: MaterialTransfer, db: Session, recipients: list[str], subject: str, lines: list[str], audit_prefix: str) -> None:
    recipients = normalize_email_list(recipients)
    if not recipients:
        logger.warning("Transfer email skipped: no recipients configured for %s", audit_prefix)
        return
    try:
        send_email(recipients, subject, "\n".join(lines))
        log_audit(
            db,
            f"{audit_prefix}_sent",
            "material_transfer",
            row.transfer_number,
            "system",
            {"recipients": recipients},
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Failed to send transfer email: %s", audit_prefix)


def notify_transfer_created(row: MaterialTransfer, db: Session) -> None:
    from_name = row.from_warehouse.name if row.from_warehouse else ""
    to_name = row.to_warehouse.name if row.to_warehouse else ""
    notify_transfer_email(
        row,
        db,
        transfer_source_warehouse_manager_emails(row, db),
        f"Warehouse approval needed: Material Transfer {row.transfer_number}",
        [
            "Hello,",
            "",
            "A material transfer has been created and is waiting for source warehouse approval.",
            "",
            f"Transfer No: {row.transfer_number}",
            f"Requester: {row.requester_name or '-'}",
            f"From Warehouse: {from_name or '-'}",
            f"To Warehouse: {to_name or '-'}",
            f"Status: Pending approval",
            f"Date: {row.transfer_date or local_today()}",
            "",
            "Please sign in to the warehouse system and review this transfer.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "transfer_created_email",
    )


def notify_mr_created(row: MaterialRequisition, db: Session) -> None:
    warehouse_name = row.warehouse.name if row.warehouse else ""
    lines = [
        "Hello,",
        "",
        "A new material request has been submitted and is ready for your review in the warehouse system.",
        "",
        f"Material Request No: {row.order_number}",
        f"Requester: {row.requester_name or '-'}",
        f"Warehouse: {warehouse_name or '-'}",
        f"Site: {row.site_id or row.site_address or '-'}",
        f"Status: Pending approval",
        f"Date: {row.creation_date or local_today()}",
        "",
        "Please sign in to the warehouse system and review this request when convenient.",
        "",
        "This is an automated notification from Global Technology Company.",
    ]
    notify_mr_email(
        row,
        db,
        approval_notification_emails(db),
        f"Approval needed: Material Request {row.order_number}",
        lines,
        "mr_email",
    )


def notify_mr_approved(row: MaterialRequisition, db: Session) -> None:
    warehouse_name = row.warehouse.name if row.warehouse else ""
    approver_copy = approval_notification_emails(db)
    notify_mr_email(
        row,
        db,
        warehouse_manager_notification_emails(row, db) + approver_copy,
        f"Warehouse action needed: Material Request {row.order_number}",
        [
            "Hello,",
            "",
            "A material request has been approved and is now ready for warehouse action.",
            "",
            f"Material Request No: {row.order_number}",
            f"Requester: {row.requester_name or '-'}",
            f"Warehouse: {warehouse_name or '-'}",
            f"Site: {row.site_id or row.site_address or '-'}",
            f"Approver: {row.receiver_name or '-'}",
            f"Status: Approved",
            "",
            "Please sign in to the warehouse system and continue processing this request.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "mr_approved_warehouse_email",
    )
    notify_mr_email(
        row,
        db,
        requester_notification_emails(row, db) + approver_copy,
        f"Your material request {row.order_number} was approved",
        [
            "Hello,",
            "",
            "Your material request has been approved.",
            "",
            f"Material Request No: {row.order_number}",
            f"Warehouse: {warehouse_name or '-'}",
            f"Site: {row.site_id or row.site_address or '-'}",
            f"Approved by: {row.receiver_name or '-'}",
            f"Status: Approved",
            "",
            "The request is now with the warehouse team for the next step.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "mr_approved_requester_email",
    )


def notify_mr_rejected(row: MaterialRequisition, db: Session) -> None:
    warehouse_name = row.warehouse.name if row.warehouse else ""
    notify_mr_email(
        row,
        db,
        requester_notification_emails(row, db) + approval_notification_emails(db),
        f"Your material request {row.order_number} was rejected",
        [
            "Hello,",
            "",
            "Your material request was rejected.",
            "",
            f"Material Request No: {row.order_number}",
            f"Warehouse: {warehouse_name or '-'}",
            f"Site: {row.site_id or row.site_address or '-'}",
            f"Reviewed by: {row.receiver_name or '-'}",
            f"Comment: {row.receiver_comment or '-'}",
            f"Status: Rejected",
            "",
            "Please sign in to the warehouse system for details.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "mr_rejected_email",
    )


def notify_mr_returned_for_edit(row: MaterialRequisition, db: Session) -> None:
    warehouse_name = row.warehouse.name if row.warehouse else ""
    notify_mr_email(
        row,
        db,
        requester_notification_emails(row, db) + approval_notification_emails(db),
        f"Material request {row.order_number} returned for edit",
        [
            "Hello,",
            "",
            "Your material request was returned for update.",
            "",
            f"Material Request No: {row.order_number}",
            f"Warehouse: {warehouse_name or '-'}",
            f"Site: {row.site_id or row.site_address or '-'}",
            f"Return reason: {row.return_reason or row.receiver_comment or '-'}",
            f"Status: Returned for edit",
            "",
            "Please sign in to the warehouse system, update the request, and submit it again.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "mr_returned_email",
    )


def notify_mr_issued(row: MaterialRequisition, db: Session) -> None:
    warehouse_name = row.warehouse.name if row.warehouse else ""
    notify_mr_email(
        row,
        db,
        requester_notification_emails(row, db) + approval_notification_emails(db),
        f"Material request {row.order_number} was issued",
        [
            "Hello,",
            "",
            "A material request has been issued by the warehouse team.",
            "",
            f"Material Request No: {row.order_number}",
            f"Warehouse: {warehouse_name or '-'}",
            f"Site: {row.site_id or row.site_address or '-'}",
            f"Requester: {row.requester_name or '-'}",
            f"Approver: {row.receiver_name or '-'}",
            f"Status: Issued",
            "",
            "Please sign in to the warehouse system for details.",
            "",
            "This is an automated notification from Global Technology Company.",
        ],
        "mr_issued_email",
    )


class LoginIn(BaseModel):
    username: str
    password: str


class AppUserIn(BaseModel):
    username: str
    password: str = ""
    role: Literal["Admin", "Management", "Requester", "Approval", "Warehouse Manager"]
    name: str = ""
    email: str = ""
    warehouse_name: str = ""


class AppUserDeleteIn(BaseModel):
    username: str
    role: str


class WarehouseIn(BaseModel):
    name: str
    location: str = ""


class TechnicianIn(BaseModel):
    name: str
    phone: str = ""


class ProductIn(BaseModel):
    sku: str
    part_number: str = ""
    category: str = ""
    name: str
    item_detail: str = ""
    qr_code: str = ""
    unit: str = "PCS"
    tracking_type: Literal["bulk", "serialized"] = "bulk"
    min_stock: float = 0


class MaterialScanIn(BaseModel):
    code: str
    actor: str = "system"


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
    part_number: str = ""
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
    return_reason: str = ""
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


class MaterialTransferItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    remark: str = ""


class MaterialTransferIn(BaseModel):
    transfer_date: str = ""
    from_warehouse_id: int
    to_warehouse_id: int
    reference_no: str = ""
    reason: str = ""
    requester_name: str = ""
    requester_title: str = ""
    approver_name: str = ""
    approver_title: str = ""
    receiver_name: str = ""
    created_by: str = "manager"
    items: list[MaterialTransferItemIn]


class MaterialReturnItemIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)
    condition: str = "Good"
    remark: str = ""


class MaterialReturnIn(BaseModel):
    return_date: str = ""
    site_id: str = ""
    site_address: str = ""
    warehouse_id: int
    returned_by: str = ""
    received_by: str = ""
    reason: str = ""
    created_by: str = "manager"
    items: list[MaterialReturnItemIn]


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def next_number(db: Session, model, prefix: str) -> str:
    return f"{prefix}-{db.query(model).count() + 1:05d}"


def clear_warehouse_cache():
    WAREHOUSE_CACHE.clear()


def log_audit(db: Session, action: str, entity_type: str, entity_id: str, actor: str, details: dict):
    clear_warehouse_cache()
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


def normalize_rollout_row(data: dict) -> dict:
    return {
        "ID": str(first_value(data, "ID", "id", default="") or ""),
        "Date": str(first_value(data, "Date", "date", default="") or ""),
        "Supervisor Name": str(first_value(data, "Supervisor Name", "supervisor_name", default="") or ""),
        "team leader": str(first_value(data, "team leader", "Team Leader", "team_leader", default="") or ""),
        "Area": str(first_value(data, "Area", "area", default="") or ""),
        "city": str(first_value(data, "city", "City", default="") or ""),
        "Activity": str(first_value(data, "Activity", "activity", default="") or ""),
        "item": str(first_value(data, "item", "Item", default="") or ""),
        "material type": str(first_value(data, "material type", "Material Type", "material_type", default="") or ""),
        "mount type": str(first_value(data, "mount type", "Mount Type", "mount_type", default="") or ""),
        "item serial": str(first_value(data, "item serial", "Item Serial", "item_serial", default="") or ""),
        "planed quantity": safe_float(first_value(data, "planed quantity", "planned quantity", "planned_quantity", default=0)),
        "actual": safe_float(first_value(data, "actual", "Actual", default=0)),
        "stock remaining": safe_float(first_value(data, "stock remaining", "Stock Remaining", "stock_remaining", default=0)),
        "staus": str(first_value(data, "staus", "status", "Status", default="") or ""),
        "laser": str(first_value(data, "laser", "Laser", default="") or ""),
        "acceptance": str(first_value(data, "acceptance", "Acceptance", default="") or ""),
        "scan": str(first_value(data, "scan", "Scan", default="") or ""),
        "labeling": str(first_value(data, "labeling", "Labeling", default="") or ""),
    }


def first_value(data: dict, *keys: str, default=""):
    for key in keys:
        if key in data and data.get(key) is not None:
            return data.get(key)
    lowered = {str(k).strip().lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.strip().lower())
        if value is not None:
            return value
    return default


def safe_float(value) -> float:
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        return 0


def rollout_csv_urls() -> list[tuple[str, str]]:
    live_url = (os.getenv("ROLLOUT_DAILY_PROGRESS_LIVE_CSV_URL") or DEFAULT_ROLLOUT_DAILY_PROGRESS_LIVE_CSV_URL).strip()
    published_url = (os.getenv("ROLLOUT_DAILY_PROGRESS_CSV_URL") or DEFAULT_ROLLOUT_DAILY_PROGRESS_CSV_URL).strip()
    urls = []
    if live_url:
        urls.append(("google_live_csv", live_url))
    if published_url and published_url != live_url:
        urls.append(("google_published_csv", published_url))
    return urls


def add_cache_buster(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "_"]
    query.append(("_", str(int(time.time() * 1000))))
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment))


def read_rollout_daily_progress_url(url: str, force: bool = False) -> list[dict]:
    fetch_url = add_cache_buster(url) if force else url
    request = urllib.request.Request(
        fetch_url,
        headers={
            "User-Agent": "warehouse-rollout-reader/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read()
    text = raw.decode("utf-8-sig")
    rows = [normalize_rollout_row(row) for row in csv.DictReader(io.StringIO(text))]
    return [row for row in rows if any(str(value or "").strip() for value in row.values())]


def fetch_rollout_daily_progress_csv(force: bool = False) -> tuple[list[dict], str]:
    global ROLLOUT_CSV_CACHE
    urls = rollout_csv_urls()
    if not urls:
        return [], "none"
    if not force and ROLLOUT_CSV_CACHE and time.monotonic() - ROLLOUT_CSV_CACHE[0] < ROLLOUT_CSV_CACHE_TTL:
        return ROLLOUT_CSV_CACHE[1], ROLLOUT_CSV_CACHE[2]
    for source, url in urls:
        try:
            rows = read_rollout_daily_progress_url(url, force=force)
            if rows:
                ROLLOUT_CSV_CACHE = (time.monotonic(), rows, source)
                return rows, source
        except Exception:
            continue
    if ROLLOUT_CSV_CACHE:
        return ROLLOUT_CSV_CACHE[1], ROLLOUT_CSV_CACHE[2]
    return [], "none"


def db_rollout_records(db: Session) -> list[dict]:
    return [row_to_record(row) for row in db.query(RolloutRecord).order_by(RolloutRecord.id.asc()).all()]


def rollout_daily_progress_records(db: Session, force: bool = False) -> tuple[list[dict], str]:
    rows, source = fetch_rollout_daily_progress_csv(force)
    if rows:
        return rows, source
    return db_rollout_records(db), "database"


def upsert_rollout_record(data: dict, db: Session) -> tuple[RolloutRecord, bool]:
    record_id = str(first_value(data, "ID", "id", default="")).strip()
    if not record_id:
        seed = db.query(RolloutRecord).count() + 1
        record_id = f"RDP-{seed:03d}"
    row = db.query(RolloutRecord).filter(RolloutRecord.record_id == record_id).first()
    created = row is None
    if row is None:
        row = RolloutRecord(record_id=record_id)
        db.add(row)

    row.date = str(first_value(data, "Date", "date", default="") or "")
    row.supervisor_name = str(first_value(data, "Supervisor Name", "supervisor_name", default="") or "")
    row.team_leader = str(first_value(data, "team leader", "Team Leader", "team_leader", default="") or "")
    row.area = str(first_value(data, "Area", "area", default="") or "")
    row.city = str(first_value(data, "city", "City", default="") or "")
    row.activity = str(first_value(data, "Activity", "activity", default="") or "")
    row.item = str(first_value(data, "item", "Item", default="") or "")
    row.material_type = str(first_value(data, "material type", "Material Type", "material_type", default="") or "")
    row.mount_type = str(first_value(data, "mount type", "Mount Type", "mount_type", default="") or "")
    row.item_serial = str(first_value(data, "item serial", "Item Serial", "item_serial", default="") or "")
    row.planned_quantity = safe_float(first_value(data, "planed quantity", "planned quantity", "planned_quantity", default=0))
    row.actual = safe_float(first_value(data, "actual", "Actual", default=0))
    row.stock_remaining = safe_float(first_value(data, "stock remaining", "Stock Remaining", "stock_remaining", default=0))
    row.status = str(first_value(data, "staus", "status", "Status", default="") or "")
    row.laser = str(first_value(data, "laser", "Laser", default="") or "")
    row.acceptance = str(first_value(data, "acceptance", "Acceptance", default="") or "")
    row.scan = str(first_value(data, "scan", "Scan", default="") or "")
    row.labeling = str(first_value(data, "labeling", "Labeling", default="") or "")
    return row, created


FOUR_CORE_CABLE_NAMES = {
    "EOSDC309I": "4-coreCable_70m",
    "EOSDC309J": "4-coreCable_100m",
    "EOSDC309K": "4-coreCable_150m",
    "EOSDC309L": "4-coreCable_200m",
    "EOSDC309M": "4-coreCable_300m",
    "EOSDC309N": "4-coreCable_500m",
}

PART_NUMBER_BY_SKU = {
    "ITC3103-A1": "52590161",
    "E0SDC309J": "14130BQC-010",
    "E0SDC309K": "#N/A",
    "E0SDC309L": "14130BQC-012",
    "E0SDC309M": "14130BQC",
    "E0SDC309N": "14130BQC-001",
    "E0SDC309I": "14130BQC-009",
    "E00ATB101": "14260372",
    "FAT2810-SE-8-A": "14261299",
    "SSC2814-TM-2": "14261384",
    "SSC2812": "#N/A",
    "FAT2811-SH-4-B": "14261785",
    "ITC3301-P1_03": "52590919",
    "E0SDC309F": "#N/A",
    "ITC2102-P2": "14261388",
    "ITC3301-P1": "52590160",
    "E00DKBA04": "21150804",
    "L05-24VDD": "#N/A",
    "E0SDC030": "14137938-002",
    "E0SDC032": "14137938-004",
    "E0SDC034": "14137938-006",
    "E0SDC035": "14137938-007",
    "E0SDC024": "14137938",
    "E0SDC038": "14137938-011",
    "E0SDC029": "14137938-001",
    "E0SDC2155": "14130ALQ-003",
    "E0SDC2157": "14130ALQ-005",
    "E0SDC2171": "14130ALQ-007",
    "E0SDC2172": "14130ALQ-008",
    "E0SDC2173": "14130ALQ-009",
    "E0SDC2147": "14130ALQ",
    "E0SDC2153": "14130ALQ-001",
    "E0SDC2154": "14130ALQ-002",
    "FAT2810-SS-8-A": "14261298",
    "SSC2814-TM-2U": "14261383",
    "SSC2802-TX-8-B": "14261816",
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


def product_part_number(sku: str = "", explicit: str = "") -> str:
    explicit_value = str(explicit or "").strip()
    if explicit_value:
        return explicit_value
    return PART_NUMBER_BY_SKU.get(str(sku or "").strip().upper(), "")


sync_product_part_numbers()


def product_display_name(product: Product | None) -> str:
    return material_display_name(product.name, product.sku) if product else ""


def product_to_dict(row: Product) -> dict:
    return {
        "id": row.id,
        "sku": row.sku,
        "part_number": product_part_number(row.sku, row.part_number),
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
        "part_number": product_part_number(row.product.sku if row.product else "", row.product.part_number if row.product else ""),
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
        "part_number": product_part_number(row.product.sku if row.product else "", row.product.part_number if row.product else ""),
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


def scan_log_to_dict(row: MaterialScanLog) -> dict:
    requisition = row.requisition
    product = row.product
    warehouse = row.warehouse
    return {
        "id": row.id,
        "material_requisition_id": row.material_requisition_id,
        "mr_order": requisition.order_number if requisition else "Stock Scan",
        "mr_status": requisition.status if requisition else row.status,
        "site_id": requisition.site_id if requisition else "",
        "site_address": requisition.site_address if requisition else "",
        "warehouse_id": row.warehouse_id,
        "warehouse": warehouse.name if warehouse else "",
        "product_id": row.product_id,
        "sku": product.sku if product else "",
        "material": product_display_name(product),
        "scan_code": row.scan_code,
        "serial_number": row.serial_number,
        "match_type": row.match_type,
        "status": row.status,
        "scanned_by": row.scanned_by,
        "note": row.note,
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


def receive_order_header_to_dict(row: ReceiveOrder) -> dict:
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
        "items": [],
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
        "return_reason": row.return_reason,
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


def issue_material_requisition_row(db: Session, row: MaterialRequisition, actor: str = "") -> str:
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

    issued_by = actor.strip() or row.created_by
    issue = IssueOrder(
        order_number=next_number(db, IssueOrder, "MR-ISS"),
        warehouse_id=row.warehouse_id,
        technician_id=technician.id,
        status="confirmed",
        created_by=issued_by,
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
                created_by=issued_by,
            )
        )

    row.status = "issued"
    log_audit(db, "issue_material_requisition", "material_requisition", row.order_number, issued_by, {"issue_order": issue.order_number})
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
def login(data: LoginIn, db: Session = Depends(db_session)):
    key = data.username.strip().lower()
    try:
        db_user = (
            db.query(AppUser)
            .filter(func.lower(AppUser.username) == key, AppUser.status == "active")
            .all()
        )
        user = next((row for row in db_user if verify_password(data.password, row.password_hash)), None)
        if user:
            if user.password_hash and not is_bcrypt_hash(user.password_hash):
                user.password_hash = hash_password(data.password)
                db.commit()
            return {
                "success": True,
                "user": {
                    "username": user.username,
                    "name": user.name or user.username,
                    "role": user.role,
                    "warehouse_name": user.warehouse_name or "",
                },
            }

        deleted_fallback = (
            db.query(AppUser)
            .filter(func.lower(AppUser.username) == key, AppUser.status == "inactive")
            .all()
        )
        if any(verify_password(data.password, row.password_hash) for row in deleted_fallback):
            raise HTTPException(status_code=401, detail="Invalid username or password")
    except HTTPException:
        raise
    except Exception:
        db.rollback()

    fallback_user = next(
        (
            row
            for row in APP_USERS
            if row["username"].lower() == key and verify_password(data.password, row["password_hash"])
        ),
        None,
    )
    if fallback_user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {
        "success": True,
        "user": {
            "username": fallback_user["username"],
            "name": fallback_user["name"],
            "role": fallback_user["role"],
            "warehouse_name": fallback_user.get("warehouse_name", ""),
        },
    }


def requisition_header_to_dict(row: MaterialRequisition) -> dict:
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
        "requester_date": row.requester_date,
        "requester_comment": row.requester_comment,
        "receiver_name": row.receiver_name,
        "receiver_title": row.receiver_title,
        "receiver_date": row.receiver_date,
        "receiver_comment": row.receiver_comment,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "items": [],
    }


def user_can_view_requisition(row: MaterialRequisition, viewer: str = "", role: str = "") -> bool:
    role_key = normalize_usage_key(role)
    viewer_key = normalize_usage_key(viewer)
    if role_key in {"admin", "management"}:
        return True
    if role_key == "warehousemanager":
        return warehouse_manager_handles_mr(viewer, row) or normalize_usage_key(row.created_by) == viewer_key
    if role_key in {"approval", "approver"}:
        return row.status == "pending_approval" or normalize_usage_key(row.receiver_name) == viewer_key
    if role_key == "requester":
        return normalize_usage_key(row.requester_name) == viewer_key or normalize_usage_key(row.created_by) == viewer_key
    return normalize_usage_key(row.created_by) == viewer_key


def requisition_history_row_to_dict(row: MaterialRequisition) -> dict:
    item_count = len(row.items or [])
    total_quantity = sum(float(item.quantity or 0) for item in row.items)
    materials = [item.description for item in row.items if str(item.description or "").strip()]
    return {
        "id": row.id,
        "order_number": row.order_number,
        "creation_date": row.creation_date,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "warehouse_id": row.warehouse_id,
        "site_id": row.site_id,
        "site_address": row.site_address,
        "requester_name": row.requester_name,
        "team_leader": row.team_leader,
        "receiver_name": row.receiver_name,
        "status": row.status,
        "entity": row.entity,
        "project_name": row.project_name,
        "product_domain": row.product_domain,
        "created_by": row.created_by,
        "item_count": item_count,
        "total_quantity": total_quantity,
        "materials": materials,
        "materials_text": ", ".join(materials),
    }


def requisition_history_payload(
    db: Session,
    warehouse: str = "",
    area: str = "",
    technician: str = "",
    requester: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    viewer: str = "",
    role: str = "",
) -> dict:
    rows = (
        db.query(MaterialRequisition)
        .options(joinedload(MaterialRequisition.warehouse), selectinload(MaterialRequisition.items))
        .order_by(MaterialRequisition.id.desc())
        .all()
    )
    visible_rows = [row for row in rows if user_can_view_requisition(row, viewer, role)]
    options = {
        "warehouses": sorted({str(row.warehouse.name if row.warehouse else "").strip() for row in visible_rows if str(row.warehouse.name if row.warehouse else "").strip()}),
        "areas": sorted({str(row.site_id or "").strip() for row in visible_rows if str(row.site_id or "").strip()}),
        "technicians": sorted({str(row.team_leader or "").strip() for row in visible_rows if str(row.team_leader or "").strip()}),
        "requesters": sorted({str(row.requester_name or "").strip() for row in visible_rows if str(row.requester_name or "").strip()}),
        "statuses": sorted({str(row.status or "").strip() for row in visible_rows if str(row.status or "").strip()}),
    }

    def match_filter(value: str, target: str) -> bool:
        if not value:
            return True
        return normalize_usage_key(target) == normalize_usage_key(value)

    filtered_rows = []
    for row in visible_rows:
        if not match_filter(warehouse, row.warehouse.name if row.warehouse else ""):
            continue
        if not match_filter(area, row.site_id):
            continue
        if not match_filter(technician, row.team_leader):
            continue
        if not match_filter(requester, row.requester_name):
            continue
        if not match_filter(status, row.status):
            continue
        creation_date = str(row.creation_date or "").strip()
        if date_from and creation_date and creation_date < date_from:
            continue
        if date_to and creation_date and creation_date > date_to:
            continue
        if (date_from or date_to) and not creation_date:
            continue
        filtered_rows.append(row)

    items_total = sum(len(row.items or []) for row in filtered_rows)
    quantity_total = sum(sum(float(item.quantity or 0) for item in row.items) for row in filtered_rows)
    return {
        "rows": [requisition_history_row_to_dict(row) for row in filtered_rows],
        "summary": {
            "mr_count": len(filtered_rows),
            "item_count": items_total,
            "total_quantity": quantity_total,
        },
        "options": options,
    }


def transfer_to_dict(row: MaterialTransfer, include_items: bool = True) -> dict:
    items = []
    if include_items:
        items = [
            {
                "id": item.id,
                "line_no": item.line_no,
                "product_id": item.product_id,
                "part_nbr": item.part_nbr,
                "description": item.description,
                "uom": item.uom,
                "quantity": item.quantity,
                "remark": item.remark,
            }
            for item in row.items
        ]
    return {
        "id": row.id,
        "transfer_number": row.transfer_number,
        "transfer_date": row.transfer_date,
        "from_warehouse_id": row.from_warehouse_id,
        "from_warehouse": row.from_warehouse.name if row.from_warehouse else "",
        "to_warehouse_id": row.to_warehouse_id,
        "to_warehouse": row.to_warehouse.name if row.to_warehouse else "",
        "reference_no": row.reference_no,
        "reason": row.reason,
        "requester_name": row.requester_name,
        "requester_title": row.requester_title,
        "approver_name": row.approver_name,
        "approver_title": row.approver_title,
        "approver_date": row.approver_date,
        "approver_comment": row.approver_comment,
        "receiver_name": row.receiver_name,
        "receiver_date": row.receiver_date,
        "receiver_comment": row.receiver_comment,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "items": items,
    }


def material_return_to_dict(row: MaterialReturn, include_items: bool = True) -> dict:
    items = []
    if include_items:
        items = [
            {
                "id": item.id,
                "line_no": item.line_no,
                "product_id": item.product_id,
                "part_nbr": item.part_nbr,
                "description": item.description,
                "uom": item.uom,
                "quantity": item.quantity,
                "condition": item.condition,
                "remark": item.remark,
            }
            for item in row.items
        ]
    return {
        "id": row.id,
        "return_number": row.return_number,
        "return_date": row.return_date,
        "site_id": row.site_id,
        "site_address": row.site_address,
        "warehouse_id": row.warehouse_id,
        "warehouse": row.warehouse.name if row.warehouse else "",
        "returned_by": row.returned_by,
        "received_by": row.received_by,
        "reason": row.reason,
        "status": row.status,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "items": items,
    }


@app.get("/api/auth/users")
def list_app_users(db: Session = Depends(db_session)):
    try:
        db_rows = db.query(AppUser).filter(AppUser.status == "active").order_by(AppUser.id.asc()).all()
        db_users = [serialize_app_user(row) for row in db_rows]
        seen = {app_user_key(row.username, row.role) for row in db_rows}
        deleted = {
            app_user_key(row.username, row.role)
            for row in db.query(AppUser).filter(AppUser.status == "inactive").all()
        }
    except Exception:
        db.rollback()
        db_users = []
        seen = set()
        deleted = set()
    fallback_users = [
        serialize_app_user(row, fallback=True)
        for row in APP_USERS
        if app_user_key(row["username"], row["role"]) not in seen
        and app_user_key(row["username"], row["role"]) not in deleted
    ]
    return {
        "success": True,
        "users": db_users + fallback_users,
    }


@app.post("/api/auth/users")
def create_app_user(data: AppUserIn, db: Session = Depends(db_session)):
    username = data.username.strip()
    password = data.password.strip()
    role = data.role.strip()
    if not username:
        raise HTTPException(status_code=400, detail="User is required")
    existing = (
        db.query(AppUser)
        .filter(
            func.lower(AppUser.username) == username.lower(),
            AppUser.role == role,
            AppUser.status == "active",
        )
        .first()
    )
    if existing:
        existing.name = data.name.strip() or username
        existing.email = data.email.strip()
        existing.warehouse_name = data.warehouse_name.strip()
        if password:
            existing.password_hash = hash_password(password)
        user = existing
    else:
        fallback = fallback_user_template(username, role)
        password_hash = hash_password(password) if password else (fallback["password_hash"] if fallback else "")
        if not password_hash:
            raise HTTPException(status_code=400, detail="Password is required")
        user = AppUser(
            username=username,
            name=data.name.strip() or username,
            role=role,
            email=data.email.strip(),
            warehouse_name=data.warehouse_name.strip(),
            password_hash=password_hash,
            status="active",
        )
        db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "success": True,
        "user": serialize_app_user(user),
    }


@app.post("/api/auth/users/delete")
def delete_app_user(data: AppUserDeleteIn, db: Session = Depends(db_session)):
    username = data.username.strip()
    role = data.role.strip()
    if not username or not role:
        raise HTTPException(status_code=400, detail="User and role are required")
    user = (
        db.query(AppUser)
        .filter(
            func.lower(AppUser.username) == username.lower(),
            AppUser.role == role,
            AppUser.status == "active",
        )
        .first()
    )
    if user:
        user.status = "inactive"
    else:
        fallback = fallback_user_template(username, role)
        if not fallback:
            raise HTTPException(status_code=404, detail="User not found")
        db.add(
            AppUser(
                username=username,
                name=fallback.get("name") or username,
                role=role,
                email=fallback.get("email", ""),
                warehouse_name=fallback.get("warehouse_name", ""),
                password_hash=fallback["password_hash"],
                status="inactive",
            )
        )
    db.commit()
    return {"success": True}


@app.get("/api/records")
def list_records(db: Session = Depends(db_session)):
    rows = db.query(RolloutRecord).order_by(RolloutRecord.id.desc()).all()
    return {"success": True, "records": [row_to_record(row) for row in rows]}


@app.post("/api/records")
def save_record(data: dict, db: Session = Depends(db_session)):
    row, _ = upsert_rollout_record(data, db)
    db.commit()
    clear_warehouse_cache()
    db.refresh(row)

    return {
        "success": True,
        "message": "Progress saved",
        "record": row_to_record(row),
    }


@app.get("/api/warehouse/rollout-daily-progress")
def list_rollout_daily_progress(limit: int = 500, db: Session = Depends(db_session)):
    rows, source = rollout_daily_progress_records(db, force=True)
    clear_warehouse_cache()
    limited = list(reversed(rows))[: min(max(limit, 1), 1000)]
    return {
        "success": True,
        "name": "Rollout Daily Progress",
        "source": source,
        "read_only": source == "google_csv",
        "count": len(rows),
        "fetched_at": datetime.now(TRIPOLI_TZ).isoformat(),
        "records": limited,
    }


@app.get("/api/warehouse/rollout-source-check")
def rollout_source_check():
    sources = []
    for source, url in rollout_csv_urls():
        try:
            rows = read_rollout_daily_progress_url(url, force=True)
            latest = rows[-1] if rows else {}
            sources.append(
                {
                    "source": source,
                    "ok": True,
                    "count": len(rows),
                    "latest_id": latest.get("ID") or latest.get("id") or "",
                    "latest_date": str(latest.get("Date") or latest.get("date") or "")[:10],
                }
            )
        except Exception as exc:
            sources.append(
                {
                    "source": source,
                    "ok": False,
                    "count": 0,
                    "error": str(exc)[:240],
                }
            )
    return {
        "success": True,
        "checked_at": datetime.now(TRIPOLI_TZ).isoformat(),
        "sources": sources,
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
def warehouse_bootstrap(light: bool = False, db: Session = Depends(db_session)):
    cache_key = "light" if light else "full"
    cached = WAREHOUSE_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < WAREHOUSE_CACHE_TTL:
        return {**cached[1], "cached": True}

    stock = list_stock_balances(db)
    usage = list_stock_usage(db)
    movements = list_stock_movements(12 if light else 40, db)
    mrs = list_material_requisition_headers(80, db) if light else list_material_requisitions(200, db)
    receipts = list_receive_order_headers(12, db) if light else list_receive_orders(60, db)
    transfers = list_material_transfer_headers(120, db) if light else list_material_transfers(200, db)
    returns = list_material_return_headers(120, db) if light else list_material_returns(200, db)
    scans = list_material_scans(80 if light else 300, db)
    payload = {
        "success": True,
        "partial": light,
        "summary": warehouse_summary(db),
        "warehouses": list_warehouses(db)["warehouses"],
        "technicians": list_technicians(db)["technicians"],
        "products": list_products(db)["products"],
        "stockBalances": stock["balances"],
        "stockUsage": usage["usage"],
        "movements": movements["movements"],
        "mrs": mrs["requisitions"],
        "receipts": receipts["receipts"],
        "transfers": transfers["transfers"],
        "returns": returns["returns"],
        "scanLogs": scans["scans"],
    }
    if not light:
        tech = list_technician_balances(db)
        rollout = list_rollout_material_usage(db)
        daily_progress = list_rollout_daily_progress(500, db)
        audit = list_audit_logs(40, db)
        payload.update(
            {
                "technicianBalances": tech["balances"],
                "rolloutUsage": rollout["usage"],
                "rolloutRecords": rollout["rollout_records"],
                "rolloutSource": rollout.get("rollout_source", ""),
                "rolloutDailyProgress": daily_progress["records"],
                "audit": audit["logs"],
            }
        )
    WAREHOUSE_CACHE[cache_key] = (time.monotonic(), payload)
    return payload


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
    clear_warehouse_cache()
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
    clear_warehouse_cache()
    return {"success": True, "technician": {"id": row.id, "name": row.name, "phone": row.phone, "status": row.status}}


@app.get("/api/warehouse/products")
def list_products(db: Session = Depends(db_session)):
    rows = db.query(Product).order_by(Product.sku).all()
    return {"success": True, "products": [product_to_dict(r) for r in rows]}


def scan_code_candidates(scanned: str) -> list[str]:
    candidates = [scanned]
    if re.fullmatch(r"\d+\.0+", scanned):
        candidates.append(scanned.split(".", 1)[0])
    elif re.fullmatch(r"\d+", scanned):
        candidates.append(f"{scanned}.00")
    return list(dict.fromkeys(candidates))


@app.get("/api/warehouse/scan-material")
def scan_material(code: str, warehouse_id: int | None = None, db: Session = Depends(db_session)):
    scanned = code.strip()
    if not scanned:
        raise HTTPException(status_code=400, detail="Scan code is required")
    candidates = scan_code_candidates(scanned)

    serial_row = (
        db.query(ProductSerial)
        .options(joinedload(ProductSerial.product))
        .filter(ProductSerial.serial_number.in_(candidates))
        .first()
    )
    product = serial_row.product if serial_row else None
    match_type = "serial_number" if serial_row else ""

    if product is None:
        product = (
            db.query(Product)
            .filter(
                or_(
                    Product.qr_code.in_(candidates),
                    Product.sku.in_(candidates),
                    Product.name.in_(candidates),
                    Product.item_detail.in_(candidates),
                )
            )
            .first()
        )
        if product:
            if product.qr_code in candidates:
                match_type = "qr_code"
            elif product.sku in candidates:
                match_type = "sku"
            elif product.name in candidates:
                match_type = "name"
            else:
                match_type = "item_detail"

    if product is None:
        raise HTTPException(status_code=404, detail=f"Material not found for scan: {scanned}")

    balance = None
    balances = []
    if warehouse_id:
        balance = (
            db.query(StockBalance)
            .filter(StockBalance.warehouse_id == warehouse_id, StockBalance.product_id == product.id)
            .first()
        )
    elif serial_row and serial_row.warehouse_id:
        warehouse_id = serial_row.warehouse_id
        balance = (
            db.query(StockBalance)
            .filter(StockBalance.warehouse_id == warehouse_id, StockBalance.product_id == product.id)
            .first()
        )
    else:
        balances = (
            db.query(StockBalance)
            .options(joinedload(StockBalance.warehouse))
            .filter(StockBalance.product_id == product.id, StockBalance.quantity > 0)
            .order_by(StockBalance.quantity.desc())
            .all()
        )
        if balances:
            balance = balances[0]
            warehouse_id = balance.warehouse_id

    return {
        "success": True,
        "scan": scanned,
        "match_type": match_type,
        "product": product_to_dict(product),
        "serial": {
            "serial_number": serial_row.serial_number,
            "status": serial_row.status,
            "warehouse_id": serial_row.warehouse_id,
            "technician_id": serial_row.technician_id,
        }
        if serial_row
        else None,
        "balance": balance.quantity if balance else 0,
        "warehouse_id": warehouse_id,
        "warehouse": balance.warehouse.name if balance and balance.warehouse else "",
        "balances": [
            {
                "warehouse_id": b.warehouse_id,
                "warehouse": b.warehouse.name if b.warehouse else "",
                "quantity": b.quantity,
            }
            for b in balances
        ],
    }


@app.get("/api/warehouse/material-scans")
def list_material_scans(limit: int = 300, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialScanLog)
        .options(
            joinedload(MaterialScanLog.requisition),
            joinedload(MaterialScanLog.product),
            joinedload(MaterialScanLog.warehouse),
        )
        .order_by(MaterialScanLog.id.desc())
        .limit(min(limit, 500))
        .all()
    )
    return {"success": True, "scans": [scan_log_to_dict(r) for r in rows]}


@app.post("/api/warehouse/material-scans/scan-record")
def record_general_material_scan(data: MaterialScanIn, db: Session = Depends(db_session)):
    scanned = data.code.strip()
    found = scan_material(scanned, None, db)
    product = db.get(Product, found["product"]["id"])
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    warehouse_id = found.get("warehouse_id")
    balance = float(found.get("balance") or 0)
    if not warehouse_id or balance <= 0:
        raise HTTPException(status_code=404, detail=f"Material found but not available in warehouse stock: {product.sku}")

    serial_number = found["serial"]["serial_number"] if found.get("serial") else ""
    if serial_number:
        duplicate = (
            db.query(MaterialScanLog)
            .filter(MaterialScanLog.material_requisition_id.is_(None), MaterialScanLog.serial_number == serial_number)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail=f"Serial already scanned in Scan History: {serial_number}")

    log = MaterialScanLog(
        material_requisition_id=None,
        product_id=product.id,
        warehouse_id=warehouse_id,
        scan_code=scanned,
        serial_number=serial_number,
        match_type=found["match_type"],
        status="in_stock",
        scanned_by=data.actor.strip() or "system",
        note="Warehouse stock scan",
    )
    db.add(log)
    log_audit(db, "scan_stock_lookup", "product", product.sku, data.actor, {"scan": scanned, "warehouse_id": warehouse_id})
    db.commit()
    db.refresh(log)
    clear_warehouse_cache()
    return {
        "success": True,
        "scan": scan_log_to_dict(log),
        "product": product_to_dict(product),
        "serial": found.get("serial"),
        "match_type": found["match_type"],
        "balance": balance,
        "warehouse": found.get("warehouse", ""),
    }


@app.post("/api/warehouse/material-requisitions/{requisition_id}/scan-record")
def record_material_scan(requisition_id: int, data: MaterialScanIn, db: Session = Depends(db_session)):
    row = (
        db.query(MaterialRequisition)
        .options(selectinload(MaterialRequisition.items).joinedload(MaterialRequisitionItem.product), joinedload(MaterialRequisition.warehouse))
        .filter(MaterialRequisition.id == requisition_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="MR not found")

    scanned = data.code.strip()
    found = scan_material(scanned, row.warehouse_id, db)
    product = db.get(Product, found["product"]["id"])
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")

    requested_qty = sum((item.quantity or 0) for item in row.items if item.product_id == product.id)
    if requested_qty <= 0:
        raise HTTPException(status_code=400, detail=f"Scanned material is not part of MR {row.order_number}")

    serial_number = found["serial"]["serial_number"] if found.get("serial") else ""
    if serial_number:
        duplicate = (
            db.query(MaterialScanLog)
            .filter(MaterialScanLog.material_requisition_id == row.id, MaterialScanLog.serial_number == serial_number)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=400, detail=f"Serial already scanned for MR {row.order_number}: {serial_number}")

    scanned_qty = (
        db.query(func.count(MaterialScanLog.id))
        .filter(MaterialScanLog.material_requisition_id == row.id, MaterialScanLog.product_id == product.id, MaterialScanLog.status == "matched")
        .scalar()
        or 0
    )
    if scanned_qty >= requested_qty:
        raise HTTPException(status_code=400, detail=f"Requested quantity already scanned for {product.sku}")

    log = MaterialScanLog(
        material_requisition_id=row.id,
        product_id=product.id,
        warehouse_id=row.warehouse_id,
        scan_code=scanned,
        serial_number=serial_number,
        match_type=found["match_type"],
        status="matched",
        scanned_by=data.actor.strip() or "system",
        note=f"MR {row.order_number}",
    )
    db.add(log)
    log_audit(db, "scan_material", "material_requisition", row.order_number, data.actor, {"scan": scanned, "sku": product.sku})
    db.commit()
    db.refresh(log)
    clear_warehouse_cache()
    return {
        "success": True,
        "scan": scan_log_to_dict(log),
        "product": product_to_dict(product),
        "serial": found.get("serial"),
        "match_type": found["match_type"],
        "balance": found["balance"],
        "scan_count": scanned_qty + 1,
        "requested_qty": requested_qty,
    }


@app.post("/api/warehouse/products")
def create_product(data: ProductIn, db: Session = Depends(db_session)):
    sku = data.sku.strip()
    name = material_display_name(data.name.strip(), sku)
    row = Product(
        sku=sku,
        part_number=product_part_number(sku, data.part_number),
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
    clear_warehouse_cache()
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
    return_order_ids = [row[0] for row in db.query(MaterialReturnItem.return_id).filter(MaterialReturnItem.product_id == product.id).distinct().all()]
    transfer_order_ids = [row[0] for row in db.query(MaterialTransferItem.transfer_id).filter(MaterialTransferItem.product_id == product.id).distinct().all()]
    snapshot = product_to_dict(product)

    db.query(ProductSerial).filter(ProductSerial.product_id == product.id).delete(synchronize_session=False)
    db.query(StockBalance).filter(StockBalance.product_id == product.id).delete(synchronize_session=False)
    db.query(TechnicianBalance).filter(TechnicianBalance.product_id == product.id).delete(synchronize_session=False)
    db.query(StockMovement).filter(StockMovement.product_id == product.id).delete(synchronize_session=False)
    db.query(MaterialScanLog).filter(MaterialScanLog.product_id == product.id).delete(synchronize_session=False)
    db.query(ReceiveOrderItem).filter(ReceiveOrderItem.product_id == product.id).delete(synchronize_session=False)
    db.query(IssueOrderItem).filter(IssueOrderItem.product_id == product.id).delete(synchronize_session=False)
    db.query(MaterialRequisitionItem).filter(MaterialRequisitionItem.product_id == product.id).delete(synchronize_session=False)
    db.query(MaterialReturnItem).filter(MaterialReturnItem.product_id == product.id).delete(synchronize_session=False)
    db.query(MaterialTransferItem).filter(MaterialTransferItem.product_id == product.id).delete(synchronize_session=False)

    for order_id in receive_order_ids:
        has_items = db.query(ReceiveOrderItem).filter(ReceiveOrderItem.receive_order_id == order_id).first()
        if has_items is None:
            db.query(ReceiveOrder).filter(ReceiveOrder.id == order_id).delete(synchronize_session=False)
    for order_id in issue_order_ids:
        has_items = db.query(IssueOrderItem).filter(IssueOrderItem.issue_order_id == order_id).first()
        if has_items is None:
            db.query(IssueOrder).filter(IssueOrder.id == order_id).delete(synchronize_session=False)
    for order_id in return_order_ids:
        has_items = db.query(MaterialReturnItem).filter(MaterialReturnItem.return_id == order_id).first()
        if has_items is None:
            db.query(MaterialReturn).filter(MaterialReturn.id == order_id).delete(synchronize_session=False)
    for order_id in transfer_order_ids:
        has_items = db.query(MaterialTransferItem).filter(MaterialTransferItem.transfer_id == order_id).first()
        if has_items is None:
            db.query(MaterialTransfer).filter(MaterialTransfer.id == order_id).delete(synchronize_session=False)

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
            .filter(StockMovement.warehouse_id.isnot(None), StockMovement.movement_type.in_(["receive", "return_in", "transfer_in"]))
            .group_by(StockMovement.warehouse_id, StockMovement.product_id)
            .all()
        )
    }
    consumed_totals = {
        (warehouse_id, product_id): total or 0
        for warehouse_id, product_id, total in (
            db.query(StockMovement.warehouse_id, StockMovement.product_id, func.sum(-StockMovement.quantity))
            .filter(StockMovement.warehouse_id.isnot(None), StockMovement.movement_type.in_(["issue_to_technician", "transfer_out"]))
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
    rollout_rows, _ = rollout_daily_progress_records(db)
    rollout_consumed_by_material: dict[str, float] = {}
    for record in rollout_rows:
        material = str(record.get("material type") or record.get("item") or "").strip()
        if not material:
            continue
        material_key = canonical_material_key(material)
        if not material_key:
            continue
        actual = safe_float(record.get("actual"))
        rollout_consumed_by_material[material_key] = rollout_consumed_by_material.get(material_key, 0) + actual

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
        product_name = product_display_name(balance.product)
        material_key = canonical_material_key(product_name or (balance.product.sku if balance.product else ""))
        rollout_consumed = rollout_consumed_by_material.get(material_key, 0)
        remaining_after_rollout = display_total - rollout_consumed
        rollout_usage_percent = round((rollout_consumed / display_total) * 100, 2) if display_total else 0
        usage_rows.append(
            {
                "warehouse_id": balance.warehouse_id,
                "warehouse": balance.warehouse.name if balance.warehouse else "",
                "product_id": balance.product_id,
                "sku": balance.product.sku if balance.product else "",
                "part_number": product_part_number(balance.product.sku if balance.product else "", balance.product.part_number if balance.product else ""),
                "product": product_name,
                "unit": balance.product.unit if balance.product else "",
                "total_received": display_total,
                "received_movements": total_received,
                "total_consumed": total_consumed,
                "total_adjustment": total_adjustment,
                "remaining": remaining,
                "usage_percent": usage_percent,
                "rollout_consumed_qty": rollout_consumed,
                "remaining_after_rollout": remaining_after_rollout,
                "rollout_usage_percent": rollout_usage_percent,
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
                    "sku": item.model or (product.sku if product else ""),
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
    return normalize_usage_key(value) in {"approver", "approval", "admin"}


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
def list_rollout_material_usage(db: Session = Depends(db_session), force: bool = False):
    rollout_rows, rollout_source = rollout_daily_progress_records(db, force=force)
    rollout_area_usage: dict[tuple[str, str], float] = {}
    rollout_material_usage: dict[str, float] = {}
    for record in rollout_rows:
        area = str(record.get("Area") or "").strip()
        material = str(record.get("material type") or record.get("item") or "").strip()
        if not area or not material:
            continue
        actual = safe_float(record.get("actual"))
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
        used = min(area_used, issued)
        extra_used = max(area_used - issued, 0)
        remaining = max(issued - used, 0)
        if area_used > issued:
            usage_match = "over_mr"
        elif area_used:
            usage_match = "area"
        else:
            usage_match = "area_not_found" if material_used else "none"
        rows.append(
            {
                **row,
                "rollout_used_qty": used,
                "rollout_actual_qty": area_used,
                "rollout_extra_qty": extra_used,
                "remaining_after_rollout": remaining,
                "usage_percent": (used / issued * 100) if issued else 0,
                "usage_match": usage_match,
            }
        )
    return {"success": True, "usage": rows, "rollout_records": len(rollout_rows), "rollout_source": rollout_source}


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


def list_material_requisition_headers(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialRequisition)
        .options(joinedload(MaterialRequisition.warehouse))
        .order_by(MaterialRequisition.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "requisitions": [requisition_header_to_dict(r) for r in rows]}


@app.get("/api/warehouse/material-requisitions/{requisition_id}")
def get_material_requisition(requisition_id: int, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.get("/api/warehouse/material-requisition-history")
def list_material_requisition_history(
    warehouse: str = "",
    area: str = "",
    technician: str = "",
    requester: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    viewer: str = "",
    role: str = "",
    db: Session = Depends(db_session),
):
    payload = requisition_history_payload(
        db,
        warehouse=warehouse,
        area=area,
        technician=technician,
        requester=requester,
        status=status,
        date_from=date_from,
        date_to=date_to,
        viewer=viewer,
        role=role,
    )
    return {"success": True, **payload}


@app.get("/api/warehouse/material-requisition-history/export")
def export_material_requisition_history(
    warehouse: str = "",
    area: str = "",
    technician: str = "",
    requester: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    viewer: str = "",
    role: str = "",
    db: Session = Depends(db_session),
):
    payload = requisition_history_payload(
        db,
        warehouse=warehouse,
        area=area,
        technician=technician,
        requester=requester,
        status=status,
        date_from=date_from,
        date_to=date_to,
        viewer=viewer,
        role=role,
    )
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "MR History"
    sheet.append(["Order", "Date", "Warehouse", "Area", "Site Address", "Requester", "Technician", "Approver", "Status", "Items", "Total Qty", "Materials"])
    for row in payload["rows"]:
        sheet.append(
            [
                row["order_number"],
                row["creation_date"],
                row["warehouse"],
                row["site_id"],
                row["site_address"],
                row["requester_name"],
                row["team_leader"],
                row["receiver_name"],
                row["status"],
                row["item_count"],
                row["total_quantity"],
                row["materials_text"],
            ]
        )
    for col in ("A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K", "L"):
        sheet.column_dimensions[col].width = 18 if col != "L" else 56
    stream = io.BytesIO()
    workbook.save(stream)
    stream.seek(0)
    filename = f"mr-history-{datetime.now(TRIPOLI_TZ).strftime('%Y%m%d-%H%M%S')}.xlsx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        stream,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )


def excel_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    return str(value).strip()


def excel_key(value) -> str:
    return re.sub(r"[^a-z0-9]+", "", excel_text(value).lower())


def excel_qty(value) -> float:
    text = excel_text(value).replace(",", "")
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        return 0


def sheet_grid(sheet) -> list[list[str]]:
    return [[excel_text(cell.value) for cell in row] for row in sheet.iter_rows()]


def nearby_sheet_value(grid: list[list[str]], row: int, col: int) -> str:
    label = excel_key(grid[row][col])
    offsets = [1, -1, 2, -2, 3, -3, 4, -4, 0]
    for offset in offsets:
        r = row + (1 if offset == 0 else 0)
        c = col if offset == 0 else col + offset
        if 0 <= r < len(grid) and 0 <= c < len(grid[r]):
            value = grid[r][c].strip()
            key = excel_key(value)
            if value and key != label and not key.endswith("name") and key not in {"date", "siteid", "warehouse", "warehousename"}:
                return value
    return ""


def find_sheet_value(grid: list[list[str]], *labels: str) -> str:
    wanted = {excel_key(label) for label in labels}
    for r, row in enumerate(grid):
        for c, value in enumerate(row):
            key = excel_key(value)
            if key in wanted:
                return nearby_sheet_value(grid, r, c)
    return ""


def product_lookup_maps(db: Session) -> tuple[dict[str, Product], dict[str, Product]]:
    exact: dict[str, Product] = {}
    material_keys: dict[str, Product] = {}
    for product in db.query(Product).filter(Product.status == "active").all():
        for value in (product.sku, product.part_number, product_display_name(product), product.name):
            key = excel_key(value)
            if key:
                exact[key] = product
        material_key = canonical_material_key(product_display_name(product))
        if material_key:
            material_keys[material_key] = product
    return exact, material_keys


def find_product_for_import(exact: dict[str, Product], material_keys: dict[str, Product], *values: str) -> Product | None:
    for value in values:
        key = excel_key(value)
        if key in exact:
            return exact[key]
    for value in values:
        key = canonical_material_key(value)
        if key in material_keys:
            return material_keys[key]
    return None


def find_import_header(row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, value in enumerate(row):
        key = excel_key(value)
        if key in {"partnbr", "partnumber", "partno", "part"}:
            mapping["part"] = index
        elif key in {"model", "sku", "itemname"}:
            mapping["model"] = index
        elif key in {"description", "descrition", "itemdescription", "itemdescrition", "itemdetail", "material", "materials"}:
            mapping["description"] = index
        elif key in {"uom", "unit"}:
            mapping["uom"] = index
        elif key in {"qty", "quantity"}:
            mapping["quantity"] = index
        elif key in {"remark", "remarks", "comment", "comments"}:
            mapping["remark"] = index
    return mapping


def parse_import_items(db: Session, grid: list[list[str]]) -> list[MaterialRequisitionItemIn]:
    header_row = -1
    header: dict[str, int] = {}
    for index, row in enumerate(grid):
        candidate = find_import_header(row)
        if "description" in candidate and "quantity" in candidate and ("part" in candidate or "model" in candidate):
            header_row = index
            header = candidate
            break
    if header_row < 0:
        raise ValueError("Items table was not found")

    exact, material_keys = product_lookup_maps(db)
    items: list[MaterialRequisitionItemIn] = []
    empty_count = 0
    for row in grid[header_row + 1 :]:
        part = row[header["part"]].strip() if "part" in header and header["part"] < len(row) else ""
        model = row[header["model"]].strip() if "model" in header and header["model"] < len(row) else ""
        description = row[header["description"]].strip() if header["description"] < len(row) else ""
        qty = excel_qty(row[header["quantity"]] if header["quantity"] < len(row) else "")
        uom = row[header["uom"]].strip() if "uom" in header and header["uom"] < len(row) else "PCS"
        remark = row[header["remark"]].strip() if "remark" in header and header["remark"] < len(row) else ""
        if not part and not model and not description and qty <= 0:
            empty_count += 1
            if empty_count >= 5:
                break
            continue
        empty_count = 0
        if qty <= 0:
            continue
        product = find_product_for_import(exact, material_keys, model, part, description)
        if product is None:
            raise ValueError(f"Material not found: {model or part or description}")
        items.append(
            MaterialRequisitionItemIn(
                product_id=product.id,
                part_nbr=product_part_number(product.sku, product.part_number),
                model=product.sku,
                description=product_display_name(product),
                uom=uom or product.unit or "PCS",
                quantity=qty,
                remark=remark,
            )
        )
    if not items:
        raise ValueError("No valid item rows found")
    return items


def find_warehouse_for_import(db: Session, grid: list[list[str]], default_warehouse_id: int = 0) -> Warehouse:
    name = find_sheet_value(grid, "Warehouse name", "Warehouse")
    row = None
    if name:
        row = db.query(Warehouse).filter(func.lower(Warehouse.name) == name.lower()).first()
        if row is None:
            key = normalize_usage_key(name)
            row = next((w for w in db.query(Warehouse).all() if normalize_usage_key(w.name) == key), None)
    if row is None and default_warehouse_id:
        row = db.get(Warehouse, default_warehouse_id)
    if row is None:
        raise ValueError("Warehouse was not found in the sheet")
    return row


def import_mr_sheet(db: Session, sheet, filename: str, default_warehouse_id: int, actor: str) -> MaterialRequisition:
    grid = sheet_grid(sheet)
    warehouse = find_warehouse_for_import(db, grid, default_warehouse_id)
    items = parse_import_items(db, grid)
    today = local_today()
    order_number = str(db.query(MaterialRequisition).count() + 1)
    row = MaterialRequisition(
        order_number=order_number,
        creation_date=find_sheet_value(grid, "Creation Date", "Date") or today,
        warehouse_id=warehouse.id,
        entity=find_sheet_value(grid, "Entity") or "Rollout",
        project_name=find_sheet_value(grid, "Project Name") or "FTTH",
        site_id=find_sheet_value(grid, "Site ID", "Area Name") or sheet.title,
        site_address=find_sheet_value(grid, "Site Address", "Site Adress") or "",
        wo_no=find_sheet_value(grid, "WO No") or "",
        product_domain=find_sheet_value(grid, "Product Domain") or "Passive",
        team_leader=find_sheet_value(grid, "Team Leader", "Receiver/TEL") or "",
        receiver_tel=find_sheet_value(grid, "Receiver/TEL") or "",
        request_shipment_time=find_sheet_value(grid, "Request Shipment Time") or today,
        request_arrived_site_time=find_sheet_value(grid, "Request arrived site time") or today,
        requester_name=find_sheet_value(grid, "Requester Name", "Requester") or actor or "Import",
        requester_title=find_sheet_value(grid, "Requester Title") or "Requester",
        requester_date=today,
        requester_comment=f"Imported from {filename} / {sheet.title}",
        receiver_name=find_sheet_value(grid, "Approver Name", "Receiver Name", "Receiver") or "Imported Approval",
        receiver_title=find_sheet_value(grid, "Approver Title", "Receiver Title") or "Approval",
        receiver_date=today,
        status="approved",
        created_by=actor or "Import",
    )
    db.add(row)
    db.flush()
    for index, item in enumerate(items, start=1):
        db.add(
            MaterialRequisitionItem(
                requisition_id=row.id,
                line_no=index,
                product_id=item.product_id,
                part_nbr=item.part_nbr,
                model=item.model,
                description=item.description,
                uom=item.uom,
                quantity=item.quantity,
                remark=item.remark,
            )
        )
    db.flush()
    issue_material_requisition_row(db, row, actor or "Import")
    log_audit(db, "import_material_requisition_excel", "material_requisition", row.order_number, actor or "Import", {"file": filename, "sheet": sheet.title})
    return row


@app.post("/api/warehouse/material-requisitions/import-excel")
async def import_material_requisition_excels(
    files: list[UploadFile] = File(...),
    default_warehouse_id: int = Form(0),
    actor: str = Form("Import"),
    db: Session = Depends(db_session),
):
    results = []
    imported_rows: list[MaterialRequisition] = []
    for upload in files:
        try:
            content = await upload.read()
            workbook = load_workbook(io.BytesIO(content), data_only=True)
        except Exception as exc:
            results.append({"file": upload.filename, "sheet": "", "success": False, "message": "Could not read Excel file"})
            continue
        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible":
                continue
            try:
                row = import_mr_sheet(db, sheet, upload.filename or "MR.xlsx", default_warehouse_id, actor)
                db.commit()
                db.refresh(row)
                imported_rows.append(row)
                results.append({"file": upload.filename, "sheet": sheet.title, "success": True, "order": row.order_number})
            except Exception as exc:
                db.rollback()
                detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                results.append({"file": upload.filename, "sheet": sheet.title, "success": False, "message": detail})
    clear_warehouse_cache()
    return {
        "success": True,
        "imported": len(imported_rows),
        "failed": len([r for r in results if not r["success"]]),
        "results": results,
        "requisitions": [requisition_to_dict(r) for r in imported_rows],
    }


def list_material_transfer_headers(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialTransfer)
        .options(joinedload(MaterialTransfer.from_warehouse), joinedload(MaterialTransfer.to_warehouse))
        .order_by(MaterialTransfer.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "transfers": [transfer_to_dict(r, include_items=False) for r in rows]}


@app.get("/api/warehouse/material-transfers")
def list_material_transfers(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialTransfer)
        .options(
            joinedload(MaterialTransfer.from_warehouse),
            joinedload(MaterialTransfer.to_warehouse),
            selectinload(MaterialTransfer.items).joinedload(MaterialTransferItem.product),
        )
        .order_by(MaterialTransfer.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "transfers": [transfer_to_dict(r) for r in rows]}


@app.get("/api/warehouse/material-transfers/{transfer_id}")
def get_material_transfer(transfer_id: int, db: Session = Depends(db_session)):
    row = db.get(MaterialTransfer, transfer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material transfer not found")
    return {"success": True, "transfer": transfer_to_dict(row)}


def list_material_return_headers(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialReturn)
        .options(joinedload(MaterialReturn.warehouse))
        .order_by(MaterialReturn.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "returns": [material_return_to_dict(r, include_items=False) for r in rows]}


@app.get("/api/warehouse/material-returns")
def list_material_returns(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(MaterialReturn)
        .options(
            joinedload(MaterialReturn.warehouse),
            selectinload(MaterialReturn.items).joinedload(MaterialReturnItem.product),
        )
        .order_by(MaterialReturn.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "returns": [material_return_to_dict(r) for r in rows]}


@app.get("/api/warehouse/material-returns/{return_id}")
def get_material_return(return_id: int, db: Session = Depends(db_session)):
    row = db.get(MaterialReturn, return_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material return not found")
    return {"success": True, "return": material_return_to_dict(row)}


@app.get("/api/warehouse/notifications")
def warehouse_notifications(user: str = "", db: Session = Depends(db_session)):
    pending = db.query(MaterialRequisition).filter(MaterialRequisition.status == "pending_approval").all()
    pending_transfers = (
        db.query(MaterialTransfer)
        .options(joinedload(MaterialTransfer.from_warehouse), joinedload(MaterialTransfer.to_warehouse))
        .filter(MaterialTransfer.status == "pending_approval")
        .all()
    )
    user_key = normalize_usage_key(user)
    approval_count = len(pending) if not user_key else sum(1 for row in pending if normalize_usage_key(row.receiver_name) == user_key)
    transfer_approval_count = len(pending_transfers) if not user_key else sum(
        1 for row in pending_transfers if normalize_usage_key(row.from_warehouse.name if row.from_warehouse else "") == user_key
    )
    approved_rows = (
        db.query(MaterialRequisition)
        .options(joinedload(MaterialRequisition.warehouse))
        .filter(MaterialRequisition.status == "approved")
        .all()
    )
    approved_transfer_rows = (
        db.query(MaterialTransfer)
        .options(joinedload(MaterialTransfer.from_warehouse), joinedload(MaterialTransfer.to_warehouse))
        .filter(MaterialTransfer.status == "approved")
        .all()
    )
    approved_count = len(approved_rows) if not user_key else sum(
        1 for row in approved_rows if warehouse_manager_handles_mr(user, row)
    )
    approved_transfer_count = len(approved_transfer_rows) if not user_key else sum(
        1
        for row in approved_transfer_rows
        if normalize_usage_key(row.from_warehouse.name if row.from_warehouse else "") == user_key
        or normalize_usage_key(row.to_warehouse.name if row.to_warehouse else "") == user_key
    )
    return {
        "success": True,
        "approval_count": approval_count + transfer_approval_count,
        "warehouse_queue_count": approved_count + approved_transfer_count,
    }


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


def list_receive_order_headers(limit: int = 50, db: Session = Depends(db_session)):
    rows = (
        db.query(ReceiveOrder)
        .options(joinedload(ReceiveOrder.warehouse))
        .order_by(ReceiveOrder.id.desc())
        .limit(min(limit, 200))
        .all()
    )
    return {"success": True, "receipts": [receive_order_header_to_dict(r) for r in rows]}


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
        return_reason=data.return_reason,
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
                part_nbr=item.part_nbr or product_part_number(product.sku if product else ""),
                model=item.model or (product.sku if product else ""),
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
    if row.status == "pending_approval":
        notify_mr_created(row, db)
    return {"success": True, "issue_order": issue_order, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/resubmit")
def resubmit_material_requisition(requisition_id: int, data: MaterialRequisitionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    if row.status != "returned_for_edit":
        raise HTTPException(status_code=400, detail=f"MR cannot be edited from status {row.status}")
    require_warehouse(db, data.warehouse_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    row.creation_date = data.creation_date
    row.warehouse_id = data.warehouse_id
    row.entity = data.entity
    row.project_name = data.project_name
    row.site_id = data.site_id
    row.site_address = data.site_address
    row.wo_no = data.wo_no
    row.product_domain = data.product_domain
    row.team_leader = data.team_leader
    row.receiver_tel = data.receiver_tel
    row.request_shipment_time = data.request_shipment_time
    row.request_arrived_site_time = data.request_arrived_site_time
    row.requester_name = data.requester_name
    row.requester_title = data.requester_title
    row.requester_signature = data.requester_signature
    row.requester_date = data.requester_date
    row.requester_comment = data.requester_comment
    row.receiver_name = data.receiver_name
    row.receiver_title = data.receiver_title
    row.receiver_signature = data.receiver_signature
    row.receiver_date = data.receiver_date
    row.receiver_comment = data.receiver_comment
    row.return_reason = ""
    row.status = "pending_approval"

    db.query(MaterialRequisitionItem).filter(MaterialRequisitionItem.requisition_id == row.id).delete()
    for index, item in enumerate(data.items, start=1):
        product = db.get(Product, item.product_id) if item.product_id else None
        db.add(
            MaterialRequisitionItem(
                requisition_id=row.id,
                line_no=index,
                product_id=item.product_id,
                part_nbr=item.part_nbr or product_part_number(product.sku if product else ""),
                model=item.model or (product.sku if product else ""),
                description=material_display_name(item.description or product_display_name(product), product.sku if product else ""),
                uom=item.uom or (product.unit if product else "PCS"),
                quantity=item.quantity,
                remark=item.remark,
            )
        )

    log_audit(db, "resubmit_material_requisition", "material_requisition", row.order_number, data.created_by, data.model_dump())
    db.commit()
    db.refresh(row)
    notify_mr_created(row, db)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-transfers")
def create_material_transfer(data: MaterialTransferIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.from_warehouse_id)
    require_warehouse(db, data.to_warehouse_id)
    if data.from_warehouse_id == data.to_warehouse_id:
        raise HTTPException(status_code=400, detail="From and To warehouse must be different")
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    row = MaterialTransfer(
        transfer_number=next_number(db, MaterialTransfer, "TR"),
        transfer_date=data.transfer_date or local_today(),
        from_warehouse_id=data.from_warehouse_id,
        to_warehouse_id=data.to_warehouse_id,
        reference_no=data.reference_no.strip(),
        reason=data.reason.strip(),
        requester_name=data.requester_name.strip(),
        requester_title=data.requester_title.strip(),
        approver_name=data.approver_name.strip(),
        approver_title=data.approver_title.strip(),
        receiver_name=data.receiver_name.strip(),
        status="pending_approval",
        created_by=data.created_by.strip() or data.requester_name.strip() or "manager",
    )
    db.add(row)
    db.flush()

    for index, item in enumerate(data.items, start=1):
        product = require_product(db, item.product_id)
        available = stock_balance(db, data.from_warehouse_id, item.product_id).quantity or 0
        if available < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for {product_display_name(product)}. Requested {item.quantity}, available {available}.",
            )
        db.add(
            MaterialTransferItem(
                transfer_id=row.id,
                line_no=index,
                product_id=item.product_id,
                part_nbr=product_part_number(product.sku, product.part_number),
                description=product_display_name(product),
                uom=product.unit,
                quantity=item.quantity,
                remark=item.remark.strip(),
            )
        )

    log_audit(db, "create_material_transfer", "material_transfer", row.transfer_number, row.created_by, data.model_dump())
    db.commit()
    db.refresh(row)
    notify_transfer_created(row, db)
    return {"success": True, "transfer_number": row.transfer_number, "transfer": transfer_to_dict(row)}


@app.post("/api/warehouse/material-transfers/{transfer_id}/approve")
def approve_material_transfer(transfer_id: int, data: MaterialRequisitionActionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialTransfer, transfer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material transfer not found")
    if row.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Transfer cannot be approved from status {row.status}")
    actor = data.actor.strip()
    if actor and not is_workflow_role(data.title) and not is_source_warehouse_manager(actor, row, db):
        raise HTTPException(status_code=403, detail="Only the source warehouse manager can approve this transfer")
    row.approver_name = actor or row.approver_name
    row.approver_title = data.title or row.approver_title
    row.approver_date = local_today()
    row.approver_comment = data.comment
    row.status = "approved"
    log_audit(db, "approve_material_transfer", "material_transfer", row.transfer_number, actor or "approval", data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "transfer": transfer_to_dict(row)}


@app.post("/api/warehouse/material-transfers/{transfer_id}/reject")
def reject_material_transfer(transfer_id: int, data: MaterialRequisitionActionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialTransfer, transfer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material transfer not found")
    if row.status != "pending_approval":
        raise HTTPException(status_code=400, detail=f"Transfer cannot be rejected from status {row.status}")
    actor = data.actor.strip()
    if actor and not is_workflow_role(data.title) and not is_source_warehouse_manager(actor, row, db):
        raise HTTPException(status_code=403, detail="Only the source warehouse manager can reject this transfer")
    row.approver_name = actor or row.approver_name
    row.approver_title = data.title or row.approver_title
    row.approver_date = local_today()
    row.approver_comment = data.comment
    row.status = "rejected"
    log_audit(db, "reject_material_transfer", "material_transfer", row.transfer_number, actor or "approval", data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "transfer": transfer_to_dict(row)}


@app.post("/api/warehouse/material-transfers/{transfer_id}/confirm")
def confirm_material_transfer(transfer_id: int, data: MaterialRequisitionActionIn = MaterialRequisitionActionIn(), db: Session = Depends(db_session)):
    row = db.get(MaterialTransfer, transfer_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material transfer not found")
    if row.status != "approved":
        raise HTTPException(status_code=400, detail=f"Transfer cannot be confirmed from status {row.status}")
    actor = data.actor.strip() or row.receiver_name or "warehouse"

    for item in row.items:
        product = require_product(db, item.product_id)
        from_balance = stock_balance(db, row.from_warehouse_id, item.product_id)
        if from_balance.quantity < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient stock for {product_display_name(product)}. Requested {item.quantity}, available {from_balance.quantity}.",
            )

    for item in row.items:
        from_balance = stock_balance(db, row.from_warehouse_id, item.product_id)
        to_balance = stock_balance(db, row.to_warehouse_id, item.product_id)
        from_balance.quantity -= item.quantity
        to_balance.quantity += item.quantity
        db.add(
            StockMovement(
                movement_type="transfer_out",
                product_id=item.product_id,
                warehouse_id=row.from_warehouse_id,
                quantity=-item.quantity,
                reference=row.transfer_number,
                note=f"Transfer to {row.to_warehouse.name if row.to_warehouse else row.to_warehouse_id}",
                created_by=actor,
            )
        )
        db.add(
            StockMovement(
                movement_type="transfer_in",
                product_id=item.product_id,
                warehouse_id=row.to_warehouse_id,
                quantity=item.quantity,
                reference=row.transfer_number,
                note=f"Transfer from {row.from_warehouse.name if row.from_warehouse else row.from_warehouse_id}",
                created_by=actor,
            )
        )

    row.receiver_name = actor
    row.receiver_date = local_today()
    row.receiver_comment = data.comment
    row.status = "transferred"
    log_audit(db, "confirm_material_transfer", "material_transfer", row.transfer_number, actor, data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "transfer": transfer_to_dict(row)}


@app.post("/api/warehouse/material-returns")
def create_material_return(data: MaterialReturnIn, db: Session = Depends(db_session)):
    require_warehouse(db, data.warehouse_id)
    if not data.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    row = MaterialReturn(
        return_number=next_number(db, MaterialReturn, "RET"),
        return_date=data.return_date or local_today(),
        site_id=data.site_id.strip(),
        site_address=data.site_address.strip(),
        warehouse_id=data.warehouse_id,
        returned_by=data.returned_by.strip(),
        received_by=data.received_by.strip(),
        reason=data.reason.strip(),
        status="confirmed",
        created_by=data.created_by.strip() or data.received_by.strip() or "manager",
    )
    db.add(row)
    db.flush()

    for index, item in enumerate(data.items, start=1):
        product = require_product(db, item.product_id)
        stock_balance(db, data.warehouse_id, item.product_id).quantity += item.quantity
        db.add(
            MaterialReturnItem(
                return_id=row.id,
                line_no=index,
                product_id=item.product_id,
                part_nbr=product_part_number(product.sku, product.part_number),
                description=product_display_name(product),
                uom=product.unit,
                quantity=item.quantity,
                condition=item.condition.strip() or "Good",
                remark=item.remark.strip(),
            )
        )
        db.add(
            StockMovement(
                movement_type="return_in",
                product_id=item.product_id,
                warehouse_id=data.warehouse_id,
                quantity=item.quantity,
                reference=row.return_number,
                note=f"Returned from site {row.site_id or row.site_address}: {item.condition.strip() or 'Good'}",
                created_by=row.created_by,
            )
        )

    log_audit(db, "create_material_return", "material_return", row.return_number, row.created_by, data.model_dump())
    db.commit()
    db.refresh(row)
    return {"success": True, "return_number": row.return_number, "return": material_return_to_dict(row)}


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
    notify_mr_approved(row, db)
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
    notify_mr_rejected(row, db)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/return-for-edit")
def return_material_requisition_for_edit(requisition_id: int, data: MaterialRequisitionActionIn, db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    if row.status != "approved":
        raise HTTPException(status_code=400, detail=f"MR cannot be returned for edit from status {row.status}")
    actor = data.actor.strip()
    row.receiver_comment = data.comment
    row.return_reason = data.comment
    row.status = "returned_for_edit"
    log_audit(db, "return_material_requisition_for_edit", "material_requisition", row.order_number, actor or "warehouse", data.model_dump())
    db.commit()
    db.refresh(row)
    notify_mr_returned_for_edit(row, db)
    return {"success": True, "requisition": requisition_to_dict(row)}


@app.post("/api/warehouse/material-requisitions/{requisition_id}/issue")
def issue_material_requisition(requisition_id: int, data: MaterialRequisitionActionIn = MaterialRequisitionActionIn(), db: Session = Depends(db_session)):
    row = db.get(MaterialRequisition, requisition_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Material requisition not found")
    issue_order = issue_material_requisition_row(db, row, data.actor)
    db.commit()
    db.refresh(row)
    notify_mr_issued(row, db)
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
            part_number=product_part_number(sku, data.part_number),
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
        product.part_number = product_part_number(sku, data.part_number or product.part_number)
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
