from sqlalchemy import Column, Integer, String, Numeric, Date, DateTime
from sqlalchemy.sql import func
from app.core.database import Base

class MarketingData(Base):
    __tablename__ = "marketing_data"

    marketing_id = Column(Integer, primary_key=True, index=True)
    sp_cell = Column(String(100), nullable=False)
    city = Column(String(255))
    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)
    state = Column(String(100))
    brand = Column(String(100))
    item = Column(String(255))
    drying_function = Column(String(100))
    loading = Column(String(100))
    capacity = Column(Numeric(12, 2))
    steam_funct_int = Column(String(100))
    first_activity = Column(Date)
    sales_units = Column(Integer, default=0)
    price = Column(Numeric(15, 2), default=0.0)
    motor_type = Column(String(100))
    steam_function = Column(String(100))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
