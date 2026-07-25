from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime

class MarketingDataBase(BaseModel):
    sp_cell: str
    city: Optional[str] = None
    month: int
    year: int
    state: Optional[str] = None
    brand: Optional[str] = None
    item: Optional[str] = None
    drying_function: Optional[str] = None
    loading: Optional[str] = None
    capacity: Optional[float] = None
    steam_funct_int: Optional[str] = None
    first_activity: Optional[date] = None
    sales_units: Optional[int] = 0
    price: Optional[float] = 0.0
    motor_type: Optional[str] = None
    steam_function: Optional[str] = None

class MarketingDataCreate(MarketingDataBase):
    pass

class MarketingData(MarketingDataBase):
    marketing_id: int
    sales_value: Optional[float] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
