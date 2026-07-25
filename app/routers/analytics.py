from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from sqlalchemy import text
from app.core.database import engine

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"]
)

def get_capacity_bucket(cap_val):
    if cap_val is None:
        return None
    try:
        val = float(cap_val)
        if val <= 0:
            return None
        if val >= 14.5:
            return "> 14 kg"
        rounded = int(round(val))
        if 6 <= rounded <= 14:
            return f"{rounded} kg"
        return None
    except ValueError:
        return None

@router.get("/branch-market-share")
async def get_branch_market_share(
    category: str = Query("ALL", description="Category: FL, TL, or ALL"),
    duration: str = Query("all", description="Duration: 3m, 6m, 12m, or all")
):
    try:
        with engine.connect() as conn:
            # Find max period to calculate relative duration
            max_period_res = conn.execute(text("SELECT MAX(year * 12 + month) FROM marketing_data")).scalar()
            
            # Base query
            query_str = """
                SELECT state, brand, SUM(sales_units) as total_units
                FROM marketing_data
                WHERE 1=1
            """
            params = {}
            
            # Category filter (FL, TL, ALL)
            if category == "FL":
                query_str += " AND UPPER(loading) = 'FRONTLOADING'"
            elif category == "TL":
                query_str += " AND UPPER(loading) = 'TOPLOADING'"
                
            # Duration filter
            if duration != "all" and max_period_res is not None:
                months_back = 3
                if duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            query_str += " GROUP BY state, brand ORDER BY state"
            
            result = conn.execute(text(query_str), params).fetchall()
            
            # Group by state
            state_data = {}
            for row in result:
                state_name = row.state or "Unknown"
                brand_name = row.brand or "Unknown"
                units = int(row.total_units or 0)
                
                if state_name not in state_data:
                    state_data[state_name] = {}
                state_data[state_name][brand_name] = units
                
            # Compute market shares
            output = []
            for state_name, brands in state_data.items():
                total_state_units = sum(brands.values())
                
                brand_shares = {}
                brand_units = {}
                for brand, units in brands.items():
                    brand_units[brand] = units
                    brand_shares[brand] = round((units / total_state_units * 100), 2) if total_state_units > 0 else 0.0
                    
                output.append({
                    "state": state_name,
                    "industry_volume": total_state_units,
                    "brand_shares": brand_shares,
                    "brand_units": brand_units
                })
                
            # Sort output by industry volume descending so the largest states are first
            output.sort(key=lambda x: x["industry_volume"], reverse=True)
            return output
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/capacity-market-share")
async def get_capacity_market_share(
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    duration: str = Query("all")
):
    try:
        with engine.connect() as conn:
            max_period_res = conn.execute(text("SELECT MAX(year * 12 + month) FROM marketing_data")).scalar()
            
            query_str = """
                SELECT state, city, year, month, brand, capacity, SUM(sales_units) as total_units
                FROM marketing_data
                WHERE 1=1
            """
            params = {}
            
            if state:
                query_str += " AND UPPER(state) = :state"
                params["state"] = state.upper()
            if city:
                query_str += " AND UPPER(city) = :city"
                params["city"] = city.upper()
                
            if duration != "all" and max_period_res is not None:
                months_back = 3
                if duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            query_str += " GROUP BY state, city, year, month, brand, capacity"
            
            result = conn.execute(text(query_str), params).fetchall()
            
            # Buckets configuration
            capacity_buckets = ["6 kg", "7 kg", "8 kg", "9 kg", "10 kg", "11 kg", "12 kg", "13 kg", "14 kg", "> 14 kg"]
            
            # 1. Aggregate for grid: brand vs capacity
            brand_capacity_units = {}
            capacity_totals = {b: 0 for b in capacity_buckets}
            
            # 2. Aggregate for trend: period -> capacity -> brand -> units
            periods_data = {}
            
            for row in result:
                brand = row.brand or "Unknown"
                bucket = get_capacity_bucket(row.capacity)
                if not bucket:
                    continue
                units = int(row.total_units or 0)
                y = int(row.year)
                m = int(row.month)
                
                # Grid aggregation
                if brand not in brand_capacity_units:
                    brand_capacity_units[brand] = {b: 0 for b in capacity_buckets}
                brand_capacity_units[brand][bucket] += units
                capacity_totals[bucket] += units
                
                # Trend aggregation
                period_key = y * 12 + m
                if period_key not in periods_data:
                    months_abbr = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    period_label = f"{months_abbr[m-1]}-{str(y)[-2:]}"
                    periods_data[period_key] = {
                        "period_key": period_key,
                        "period_label": period_label,
                        "year": y,
                        "month": m,
                        "capacity_data": {b: {} for b in capacity_buckets}
                    }
                
                if brand not in periods_data[period_key]["capacity_data"][bucket]:
                    periods_data[period_key]["capacity_data"][bucket][brand] = 0
                periods_data[period_key]["capacity_data"][bucket][brand] += units
                
            # Formatting grid output
            grid_output = []
            for brand, buckets in brand_capacity_units.items():
                brand_shares = {}
                brand_units = {}
                for bucket in capacity_buckets:
                    units = buckets[bucket]
                    total = capacity_totals[bucket]
                    brand_units[bucket] = units
                    brand_shares[bucket] = round((units / total * 100), 2) if total > 0 else 0.0
                    
                grid_output.append({
                    "brand": brand,
                    "units": brand_units,
                    "shares": brand_shares
                })
                
            # Formatting trend output (sorted chronologically)
            sorted_periods = sorted(periods_data.values(), key=lambda x: x["period_key"])
            
            # Post-process trend data to calculate shares
            trend_output = []
            for period in sorted_periods:
                formatted_cap_data = {}
                for bucket in capacity_buckets:
                    brand_units_map = period["capacity_data"][bucket]
                    total_cap_units = sum(brand_units_map.values())
                    
                    formatted_cap_data[bucket] = {}
                    for brand, units in brand_units_map.items():
                        formatted_cap_data[bucket][brand] = {
                            "units": units,
                            "share": round((units / total_cap_units * 100), 2) if total_cap_units > 0 else 0.0
                        }
                
                trend_output.append({
                    "period": period["period_label"],
                    "year": period["year"],
                    "month": period["month"],
                    "capacity_data": formatted_cap_data
                })
                
            # List of unique states/cities for dropdowns in UI
            unique_states_cities_res = conn.execute(text("SELECT DISTINCT state, city FROM marketing_data")).fetchall()
            states = sorted(list(set(r.state for r in unique_states_cities_res if r.state)))
            cities = sorted(list(set(r.city for r in unique_states_cities_res if r.city)))

            return {
                "grid": grid_output,
                "capacity_totals": capacity_totals,
                "trend": trend_output,
                "states": states,
                "cities": cities
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sku-standings")
async def get_sku_standings(
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    duration: str = Query("all"),
    sku_type: str = Query("item", description="SKU type: item or capacity")
):
    try:
        with engine.connect() as conn:
            max_period_res = conn.execute(text("SELECT MAX(year * 12 + month) FROM marketing_data")).scalar()
            
            if sku_type == "capacity":
                query_str = """
                    SELECT brand, capacity as sku_val, SUM(sales_units) as total_units, 
                           SUM(price * sales_units) as total_revenue
                    FROM marketing_data
                    WHERE capacity IS NOT NULL
                """
            else:
                query_str = """
                    SELECT brand, item as sku_val, SUM(sales_units) as total_units, 
                           SUM(price * sales_units) as total_revenue
                    FROM marketing_data
                    WHERE item IS NOT NULL AND item != ''
                """
            params = {}
            
            if state:
                query_str += " AND UPPER(state) = :state"
                params["state"] = state.upper()
            if city:
                query_str += " AND UPPER(city) = :city"
                params["city"] = city.upper()
                
            if duration != "all" and max_period_res is not None:
                months_back = 3
                if duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            if sku_type == "capacity":
                query_str += " GROUP BY brand, capacity"
            else:
                query_str += " GROUP BY brand, item"
                
            result = conn.execute(text(query_str), params).fetchall()
            
            # Group by brand
            brand_skus = {}
            for row in result:
                brand = row.brand or "Unknown"
                sku_val = row.sku_val
                
                if sku_type == "capacity":
                    sku_name = get_capacity_bucket(sku_val)
                    if not sku_name:
                        continue
                else:
                    sku_name = str(sku_val)
                    
                volume = int(row.total_units or 0)
                revenue = float(row.total_revenue or 0.0)
                
                asp = round(revenue / volume, 2) if volume > 0 else 0.0
                
                if brand not in brand_skus:
                    brand_skus[brand] = []
                    
                if sku_type == "capacity":
                    existing = next((x for x in brand_skus[brand] if x["sku"] == sku_name), None)
                    if existing:
                        existing_vol = existing["volume"]
                        existing_rev = existing["asp"] * existing_vol
                        new_vol = existing_vol + volume
                        new_rev = existing_rev + revenue
                        existing["volume"] = new_vol
                        existing["asp"] = round(new_rev / new_vol, 2) if new_vol > 0 else 0.0
                        continue
                        
                brand_skus[brand].append({
                    "sku": sku_name,
                    "volume": volume,
                    "asp": asp
                })
                
            # Default sort by volume descending for each brand
            for brand in brand_skus:
                brand_skus[brand].sort(key=lambda x: x["volume"], reverse=True)
                
            return brand_skus
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
