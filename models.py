from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import relationship

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


class Warehouse(Base):
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False, index=True)
    location = Column(String, default="")
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    balances = relationship("StockBalance", back_populates="warehouse")


class Technician(Base):
    __tablename__ = "technicians"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    phone = Column(String, default="")
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    balances = relationship("TechnicianBalance", back_populates="technician")


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    sku = Column(String, unique=True, nullable=False, index=True)
    category = Column(String, default="")
    name = Column(String, nullable=False, index=True)
    item_detail = Column(String, default="")
    qr_code = Column(String, default="")
    unit = Column(String, default="PCS")
    tracking_type = Column(String, default="bulk")
    min_stock = Column(Float, default=0)
    status = Column(String, default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    serials = relationship("ProductSerial", back_populates="product")


class ProductSerial(Base):
    __tablename__ = "product_serials"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    serial_number = Column(String, unique=True, nullable=False, index=True)
    status = Column(String, default="in_warehouse", index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True, index=True)
    technician_id = Column(Integer, ForeignKey("technicians.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", back_populates="serials")


class StockBalance(Base):
    __tablename__ = "stock_balances"
    __table_args__ = (UniqueConstraint("warehouse_id", "product_id", name="uq_stock_balance"),)

    id = Column(Integer, primary_key=True, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    warehouse = relationship("Warehouse", back_populates="balances")
    product = relationship("Product")


class TechnicianBalance(Base):
    __tablename__ = "technician_balances"
    __table_args__ = (UniqueConstraint("technician_id", "product_id", name="uq_technician_balance"),)

    id = Column(Integer, primary_key=True, index=True)
    technician_id = Column(Integer, ForeignKey("technicians.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    technician = relationship("Technician", back_populates="balances")
    product = relationship("Product")


class StockMovement(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True, index=True)
    movement_type = Column(String, nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=True, index=True)
    technician_id = Column(Integer, ForeignKey("technicians.id"), nullable=True, index=True)
    quantity = Column(Float, nullable=False)
    serial_number = Column(String, default="")
    reference = Column(String, default="", index=True)
    note = Column(Text, default="")
    created_by = Column(String, default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    product = relationship("Product")
    warehouse = relationship("Warehouse")
    technician = relationship("Technician")


class ReceiveOrder(Base):
    __tablename__ = "receive_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String, unique=True, nullable=False, index=True)
    supplier = Column(String, default="")
    receipt_date = Column(String, default="")
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    status = Column(String, default="confirmed")
    created_by = Column(String, default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    warehouse = relationship("Warehouse")
    items = relationship("ReceiveOrderItem", back_populates="order", cascade="all, delete-orphan")


class ReceiveOrderItem(Base):
    __tablename__ = "receive_order_items"

    id = Column(Integer, primary_key=True, index=True)
    receive_order_id = Column(Integer, ForeignKey("receive_orders.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    serial_number = Column(String, default="")

    order = relationship("ReceiveOrder", back_populates="items")
    product = relationship("Product")


class IssueOrder(Base):
    __tablename__ = "issue_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String, unique=True, nullable=False, index=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    technician_id = Column(Integer, ForeignKey("technicians.id"), nullable=False, index=True)
    status = Column(String, default="confirmed")
    created_by = Column(String, default="system")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    warehouse = relationship("Warehouse")
    technician = relationship("Technician")
    items = relationship("IssueOrderItem", back_populates="order", cascade="all, delete-orphan")


class IssueOrderItem(Base):
    __tablename__ = "issue_order_items"

    id = Column(Integer, primary_key=True, index=True)
    issue_order_id = Column(Integer, ForeignKey("issue_orders.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    serial_number = Column(String, default="")

    order = relationship("IssueOrder", back_populates="items")
    product = relationship("Product")


class MaterialRequisition(Base):
    __tablename__ = "material_requisitions"

    id = Column(Integer, primary_key=True, index=True)
    order_number = Column(String, unique=True, nullable=False, index=True)
    creation_date = Column(String, default="")
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    entity = Column(String, default="Rollout")
    project_name = Column(String, default="FTTH")
    site_id = Column(String, default="")
    site_address = Column(String, default="")
    wo_no = Column(String, default="")
    product_domain = Column(String, default="Passive")
    team_leader = Column(String, default="")
    receiver_tel = Column(String, default="")
    request_shipment_time = Column(String, default="")
    request_arrived_site_time = Column(String, default="")
    requester_name = Column(String, default="")
    requester_title = Column(String, default="")
    requester_signature = Column(Text, default="")
    requester_date = Column(String, default="")
    requester_comment = Column(Text, default="")
    receiver_name = Column(String, default="")
    receiver_title = Column(String, default="")
    receiver_signature = Column(Text, default="")
    receiver_date = Column(String, default="")
    receiver_comment = Column(Text, default="")
    status = Column(String, default="draft", index=True)
    created_by = Column(String, default="manager")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    warehouse = relationship("Warehouse")
    items = relationship("MaterialRequisitionItem", back_populates="requisition", cascade="all, delete-orphan")


class MaterialRequisitionItem(Base):
    __tablename__ = "material_requisition_items"

    id = Column(Integer, primary_key=True, index=True)
    requisition_id = Column(Integer, ForeignKey("material_requisitions.id"), nullable=False, index=True)
    line_no = Column(Integer, default=1)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    part_nbr = Column(String, default="")
    model = Column(String, default="")
    description = Column(Text, default="")
    uom = Column(String, default="PCS")
    quantity = Column(Float, default=0)
    remark = Column(Text, default="")

    requisition = relationship("MaterialRequisition", back_populates="items")
    product = relationship("Product")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String, nullable=False, index=True)
    entity_type = Column(String, nullable=False, index=True)
    entity_id = Column(String, default="", index=True)
    actor = Column(String, default="system")
    details = Column(Text, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
