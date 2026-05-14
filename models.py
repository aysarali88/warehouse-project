from sqlalchemy import Column, Float, Integer, String

from database import Base


class RolloutRecord(Base):
    __tablename__ = "rollout_records"

    id = Column(Integer, primary_key=True, index=True)
    record_id = Column(String, unique=True, index=True)
    date = Column(String, default="")
    supervisor_name = Column(String, default="")
    team_leader = Column(String, default="")
    area = Column(String, default="")
    city = Column(String, default="")
    activity = Column(String, default="")
    item = Column(String, default="")
    material_type = Column(String, default="")
    mount_type = Column(String, default="")
    item_serial = Column(String, default="")
    planned_quantity = Column(Float, default=0)
    actual = Column(Float, default=0)
    stock_remaining = Column(Float, default=0)
    status = Column(String, default="")
    laser = Column(String, default="")
    acceptance = Column(String, default="")
    scan = Column(String, default="")
    labeling = Column(String, default="")
