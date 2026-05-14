from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from database import Base, SessionLocal, engine
from models import RolloutRecord

Base.metadata.create_all(bind=engine)

app = FastAPI(title="FTTH Rollout")
app.mount("/static", StaticFiles(directory="static"), name="static")


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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


@app.get("/")
def home():
    return FileResponse("static/ftth_rollout.html")


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
