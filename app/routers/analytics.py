from math import floor
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from sqlalchemy import text
from app.core.database import engine

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"]
)

def get_month_name(m: int) -> str:
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return months[m - 1] if 1 <= m <= 12 else ""

def get_capacity_bucket(cap_val):
    if cap_val is None:
        return None
    try:
        val = float(cap_val)
        if val <= 0:
            return None
        elif val > 14:
            return "> 14 kg"
        elif val < 7:
            return "6 kg"
        else:
            floor_value = int(floor(val))
            # if 7 <= floor_value <= 14:
            return f"{floor_value} kg"
        return None
    except ValueError:
        return None

@router.get("/branch-market-share")
async def get_branch_market_share(
    category: str = Query("ALL", description="Category: FL, TL, or ALL"),
    duration: str = Query("all", description="Duration: 1m, 3m, 6m, 12m, custom, or all"),
    states: Optional[str] = Query(None, description="Comma-separated states"),
    cities: Optional[str] = Query(None, description="Comma-separated cities"),
    brands: Optional[str] = Query(None, description="Comma-separated brands"),
    start_period: Optional[str] = Query(None, description="Start period YYYY-MM"),
    end_period: Optional[str] = Query(None, description="End period YYYY-MM"),
    compare_offset: Optional[str] = Query("1m", description="Compare offset: 1m, 3m, 6m, 12m")
):
    try:
        with engine.connect() as conn:
            # Find max period to calculate relative duration
            max_period_res = conn.execute(text("SELECT MAX(year * 12 + month) FROM marketing_data")).scalar()
            
            # Calculate active range boundaries
            if duration == "all" or max_period_res is None:
                min_period_key = conn.execute(text("SELECT MIN(year * 12 + month) FROM marketing_data")).scalar() or 0
                max_period_key = max_period_res or 0
            elif duration == "custom":
                min_period_key = 0
                max_period_key = max_period_res or 0
                if start_period:
                    try:
                        sy, sm = map(int, start_period.split("-"))
                        min_period_key = sy * 12 + sm
                    except ValueError:
                        pass
                if end_period:
                    try:
                        ey, em = map(int, end_period.split("-"))
                        max_period_key = ey * 12 + em
                    except ValueError:
                        pass
                if min_period_key == 0:
                    min_period_key = conn.execute(text("SELECT MIN(year * 12 + month) FROM marketing_data")).scalar() or 0
            else:
                months_back = 3
                if duration == "1m":
                    months_back = 1
                elif duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                min_period_key = max_period_res - months_back + 1
                max_period_key = max_period_res

            # Base query
            query_str = """
                SELECT state, brand, SUM(sales_units) as total_units
                FROM marketing_data
                WHERE (year * 12 + month) >= :min_period AND (year * 12 + month) <= :max_period
            """
            params = {
                "min_period": min_period_key,
                "max_period": max_period_key
            }
            
            if brands:
                brand_list = [b.strip().upper() for b in brands.split(",") if b.strip()]
                if brand_list:
                    placeholders = [f":brand_{i}" for i in range(len(brand_list))]
                    query_str += f" AND UPPER(brand) IN ({','.join(placeholders)})"
                    for i, val in enumerate(brand_list):
                        params[f"brand_{i}"] = val
            
            if states:
                state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
                if state_list:
                    placeholders = [f":state_{i}" for i in range(len(state_list))]
                    query_str += f" AND UPPER(state) IN ({','.join(placeholders)})"
                    for i, val in enumerate(state_list):
                        params[f"state_{i}"] = val
                        
            if cities:
                city_list = [c.strip().upper() for c in cities.split(",") if c.strip()]
                if city_list:
                    placeholders = [f":city_{i}" for i in range(len(city_list))]
                    query_str += f" AND UPPER(city) IN ({','.join(placeholders)})"
                    for i, val in enumerate(city_list):
                        params[f"city_{i}"] = val
            
            # Category filter (FL, TL, ALL)
            if category == "FL":
                query_str += " AND UPPER(loading) = 'FRONTLOADING'"
            elif category == "TL":
                query_str += " AND UPPER(loading) = 'TOPLOADING'"
            elif category == "WDR":
                query_str += " AND UPPER(loading) = 'WDR'"
                
            query_str += " GROUP BY state, brand ORDER BY state"
            
            # Execute current period query
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
                
            # Calculate comparison offset in months
            offset_months = 1
            if compare_offset == "3m":
                offset_months = 3
            elif compare_offset == "6m":
                offset_months = 6
            elif compare_offset == "12m":
                offset_months = 12
                
            past_min_period = min_period_key - offset_months
            past_max_period = max_period_key - offset_months
            
            absolute_min_period = conn.execute(text("SELECT MIN(year * 12 + month) FROM marketing_data")).scalar() or 0
            has_comparison_data = past_min_period >= absolute_min_period
            
            past_state_shares = {}
            if has_comparison_data:
                past_params = params.copy()
                past_params["min_period"] = past_min_period
                past_params["max_period"] = past_max_period
                
                past_result = conn.execute(text(query_str), past_params).fetchall()
                
                past_state_data = {}
                for row in past_result:
                    state_name = row.state or "Unknown"
                    brand_name = row.brand or "Unknown"
                    units = int(row.total_units or 0)
                    if state_name not in past_state_data:
                        past_state_data[state_name] = {}
                    past_state_data[state_name][brand_name] = units
                    
                for state_name, brands_dict in past_state_data.items():
                    total_past_state_units = sum(brands_dict.values())
                    past_state_shares[state_name] = {}
                    for brand, units in brands_dict.items():
                        past_state_shares[state_name][brand] = (units / total_past_state_units * 100) if total_past_state_units > 0 else 0.0
                        
            # Compute market shares and deltas
            output = []
            for state_name, brands_dict in state_data.items():
                total_state_units = sum(brands_dict.values())
                
                brand_shares = {}
                brand_units = {}
                brand_trends = {}
                for brand, units in brands_dict.items():
                    brand_units[brand] = units
                    curr_share = (units / total_state_units * 100) if total_state_units > 0 else 0.0
                    brand_shares[brand] = round(curr_share, 2)
                    
                    if has_comparison_data and state_name in past_state_shares:
                        past_share = past_state_shares[state_name].get(brand, 0.0)
                        brand_trends[brand] = round(curr_share - past_share, 2)
                    else:
                        brand_trends[brand] = None
                    
                output.append({
                    "state": state_name,
                    "industry_volume": total_state_units,
                    "brand_shares": brand_shares,
                    "brand_units": brand_units,
                    "brand_trends": brand_trends
                })
                
            # Sort output by industry volume descending so the largest states are first
            output.sort(key=lambda x: x["industry_volume"], reverse=True)
            return output
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/capacity-market-share")
async def get_capacity_market_share(
    states: Optional[str] = Query(None),
    cities: Optional[str] = Query(None),
    brands: Optional[str] = Query(None),
    duration: str = Query("all"),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    category: str = Query("ALL", description="Category: FL, TL, WDR, or ALL")
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
            
            if brands:
                brand_list = [b.strip().upper() for b in brands.split(",") if b.strip()]
                if brand_list:
                    placeholders = [f":brand_{i}" for i in range(len(brand_list))]
                    query_str += f" AND UPPER(brand) IN ({','.join(placeholders)})"
                    for i, val in enumerate(brand_list):
                        params[f"brand_{i}"] = val
                        
            if states:
                state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
                if state_list:
                    placeholders = [f":state_{i}" for i in range(len(state_list))]
                    query_str += f" AND UPPER(state) IN ({','.join(placeholders)})"
                    for i, val in enumerate(state_list):
                        params[f"state_{i}"] = val
                        
            if cities:
                city_list = [c.strip().upper() for c in cities.split(",") if c.strip()]
                if city_list:
                    placeholders = [f":city_{i}" for i in range(len(city_list))]
                    query_str += f" AND UPPER(city) IN ({','.join(placeholders)})"
                    for i, val in enumerate(city_list):
                        params[f"city_{i}"] = val
            
            if category == "FL":
                query_str += " AND UPPER(loading) = 'FRONTLOADING'"
            elif category == "TL":
                query_str += " AND UPPER(loading) = 'TOPLOADING'"
            elif category == "WDR":
                query_str += " AND UPPER(loading) = 'WDR'"
                
            if duration != "all" and duration != "custom" and max_period_res is not None:
                months_back = 3
                if duration == "1m":
                    months_back = 1
                elif duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            if start_period:
                try:
                    sy, sm = map(int, start_period.split("-"))
                    query_str += " AND (year * 12 + month) >= :start_period_val"
                    params["start_period_val"] = sy * 12 + sm
                except ValueError:
                    pass
            if end_period:
                try:
                    ey, em = map(int, end_period.split("-"))
                    query_str += " AND (year * 12 + month) <= :end_period_val"
                    params["end_period_val"] = ey * 12 + em
                except ValueError:
                    pass
                
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
                    period_label = f"{get_month_name(m)}-{str(y)[-2:]}"
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
            state_city_map = [{"state": r.state, "city": r.city} for r in unique_states_cities_res if r.state and r.city]
            
            # Fetch global brands list
            unique_brands_res = conn.execute(text("SELECT DISTINCT brand FROM marketing_data")).fetchall()
            brands_list = sorted(list(set(r.brand for r in unique_brands_res if r.brand)))

            # Fetch distinct periods
            distinct_periods_res = conn.execute(text("SELECT DISTINCT year, month FROM marketing_data ORDER BY year DESC, month DESC")).fetchall()
            periods_list = []
            for r in distinct_periods_res:
                m_label = get_month_name(r.month)
                periods_list.append({
                    "year": r.year,
                    "month": r.month,
                    "label": f"{m_label} {r.year}",
                    "value": f"{r.year}-{r.month:02d}"
                })

            return {
                "grid": grid_output,
                "capacity_totals": capacity_totals,
                "trend": trend_output,
                "states": states,
                "cities": cities,
                "state_city_map": state_city_map,
                "brands": brands_list,
                "periods": periods_list
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/sku-standings")
async def get_sku_standings(
    states: Optional[str] = Query(None),
    cities: Optional[str] = Query(None),
    brands: Optional[str] = Query(None),
    duration: str = Query("all"),
    sku_type: str = Query("item", description="SKU type: item or capacity"),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    category: str = Query("ALL", description="Category: FL, TL, WDR, or ALL"),
    capacities: Optional[str] = Query(None, description="Comma-separated capacity buckets")
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
                    SELECT brand, item as sku_val, MAX(capacity) as capacity, SUM(sales_units) as total_units, 
                           SUM(price * sales_units) as total_revenue
                    FROM marketing_data
                    WHERE item IS NOT NULL AND item != ''
                """
            params = {}
            
            if brands:
                brand_list = [b.strip().upper() for b in brands.split(",") if b.strip()]
                if brand_list:
                    placeholders = [f":brand_{i}" for i in range(len(brand_list))]
                    query_str += f" AND UPPER(brand) IN ({','.join(placeholders)})"
                    for i, val in enumerate(brand_list):
                        params[f"brand_{i}"] = val
                        
            if states:
                state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
                if state_list:
                    placeholders = [f":state_{i}" for i in range(len(state_list))]
                    query_str += f" AND UPPER(state) IN ({','.join(placeholders)})"
                    for i, val in enumerate(state_list):
                        params[f"state_{i}"] = val
                        
            if cities:
                city_list = [c.strip().upper() for c in cities.split(",") if c.strip()]
                if city_list:
                    placeholders = [f":city_{i}" for i in range(len(city_list))]
                    query_str += f" AND UPPER(city) IN ({','.join(placeholders)})"
                    for i, val in enumerate(city_list):
                        params[f"city_{i}"] = val
            
            if capacities:
                cap_list = [c.strip() for c in capacities.split(",") if c.strip()]
                cap_clauses = []
                for idx, cap in enumerate(cap_list):
                    p_cap = f"cap_filter_{idx}"
                    if cap == "6 kg":
                        cap_clauses.append("capacity < 7")
                    elif cap == "> 14 kg":
                        cap_clauses.append("capacity > 14")
                    else:
                        try:
                            val = int(cap.split()[0])
                            params[p_cap] = val
                            cap_clauses.append(f"(capacity >= :{p_cap} AND capacity < :{p_cap} + 1)")
                        except ValueError:
                            pass
                if cap_clauses:
                    query_str += f" AND ({' OR '.join(cap_clauses)})"
            
            if category == "FL":
                query_str += " AND UPPER(loading) = 'FRONTLOADING'"
            elif category == "TL":
                query_str += " AND UPPER(loading) = 'TOPLOADING'"
            elif category == "WDR":
                query_str += " AND UPPER(loading) = 'WDR'"
                
            if duration != "all" and duration != "custom" and max_period_res is not None:
                months_back = 3
                if duration == "1m":
                    months_back = 1
                elif duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            if start_period:
                try:
                    sy, sm = map(int, start_period.split("-"))
                    query_str += " AND (year * 12 + month) >= :start_period_val"
                    params["start_period_val"] = sy * 12 + sm
                except ValueError:
                    pass
            if end_period:
                try:
                    ey, em = map(int, end_period.split("-"))
                    query_str += " AND (year * 12 + month) <= :end_period_val"
                    params["end_period_val"] = ey * 12 + em
                except ValueError:
                    pass
                
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
                        
                item_data = {
                    "sku": sku_name,
                    "volume": volume,
                    "asp": asp
                }
                if sku_type == "item":
                    cap_val = row.capacity
                    item_data["capacity"] = f"{float(cap_val):g} kg" if cap_val is not None else None
                brand_skus[brand].append(item_data)
                
            # Default sort by volume descending for each brand
            for brand in brand_skus:
                brand_skus[brand].sort(key=lambda x: x["volume"], reverse=True)
                
            return brand_skus
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/mop-trends")
async def get_mop_trends(
    states: Optional[str] = Query(None),
    cities: Optional[str] = Query(None),
    brands: Optional[str] = Query(None),
    duration: str = Query("all"),
    start_period: Optional[str] = Query(None),
    end_period: Optional[str] = Query(None),
    rank_by: str = Query("price", description="Rank by: price, volume, or revenue"),
    category: str = Query("ALL", description="Category: FL, TL, WDR, or ALL")
):
    try:
        with engine.connect() as conn:
            max_period_res = conn.execute(text("SELECT MAX(year * 12 + month) FROM marketing_data")).scalar()
            
            query_str = """
                SELECT brand, capacity, year, month, 
                       AVG(price) as avg_price, 
                       SUM(sales_units) as total_units, 
                       SUM(sales_units * price) as total_revenue
                FROM marketing_data
                WHERE price IS NOT NULL AND price > 0 AND capacity IS NOT NULL
            """
            params = {}
            
            if brands:
                brand_list = [b.strip().upper() for b in brands.split(",") if b.strip()]
                if brand_list:
                    placeholders = [f":brand_{i}" for i in range(len(brand_list))]
                    query_str += f" AND UPPER(brand) IN ({','.join(placeholders)})"
                    for i, val in enumerate(brand_list):
                        params[f"brand_{i}"] = val
                        
            if states:
                state_list = [s.strip().upper() for s in states.split(",") if s.strip()]
                if state_list:
                    placeholders = [f":state_{i}" for i in range(len(state_list))]
                    query_str += f" AND UPPER(state) IN ({','.join(placeholders)})"
                    for i, val in enumerate(state_list):
                        params[f"state_{i}"] = val
                        
            if cities:
                city_list = [c.strip().upper() for c in cities.split(",") if c.strip()]
                if city_list:
                    placeholders = [f":city_{i}" for i in range(len(city_list))]
                    query_str += f" AND UPPER(city) IN ({','.join(placeholders)})"
                    for i, val in enumerate(city_list):
                        params[f"city_{i}"] = val
            
            if category == "FL":
                query_str += " AND UPPER(loading) = 'FRONTLOADING'"
            elif category == "TL":
                query_str += " AND UPPER(loading) = 'TOPLOADING'"
            elif category == "WDR":
                query_str += " AND UPPER(loading) = 'WDR'"
                
            if duration != "all" and duration != "custom" and max_period_res is not None:
                months_back = 3
                if duration == "1m":
                    months_back = 1
                elif duration == "6m":
                    months_back = 6
                elif duration == "12m":
                    months_back = 12
                
                min_period = max_period_res - months_back + 1
                query_str += " AND (year * 12 + month) >= :min_period"
                params["min_period"] = min_period
                
            if start_period:
                try:
                    sy, sm = map(int, start_period.split("-"))
                    query_str += " AND (year * 12 + month) >= :start_period_val"
                    params["start_period_val"] = sy * 12 + sm
                except ValueError:
                    pass
            if end_period:
                try:
                    ey, em = map(int, end_period.split("-"))
                    query_str += " AND (year * 12 + month) <= :end_period_val"
                    params["end_period_val"] = ey * 12 + em
                except ValueError:
                    pass
                
            query_str += " GROUP BY brand, capacity, year, month"
            
            result = conn.execute(text(query_str), params).fetchall()
            
            capacity_buckets = ["6 kg", "7 kg", "8 kg", "9 kg", "10 kg", "11 kg", "12 kg", "13 kg", "14 kg", "> 14 kg"]
            
            # Fetch top SKU per brand and capacity using identical filters
            where_idx = query_str.find("WHERE")
            groupby_idx = query_str.find("GROUP BY")
            where_clause = query_str[where_idx:groupby_idx]
            
            top_sku_query = text(f"""
                SELECT brand, capacity, item, SUM(sales_units) as total_units
                FROM marketing_data
                {where_clause} AND item IS NOT NULL AND item != ''
                GROUP BY brand, capacity, item
            """)
            top_sku_rows = conn.execute(top_sku_query, params).fetchall()
            
            top_sku_map = {c: {} for c in capacity_buckets}
            for row in top_sku_rows:
                brand = row.brand or "Unknown"
                bucket = get_capacity_bucket(row.capacity)
                if not bucket:
                    continue
                item = row.item
                units = int(row.total_units or 0)
                
                if brand not in top_sku_map[bucket]:
                    top_sku_map[bucket][brand] = []
                top_sku_map[bucket][brand].append({
                    "sku": item,
                    "volume": units
                })
                
            for bucket in capacity_buckets:
                for brand in top_sku_map[bucket]:
                    top_sku_map[bucket][brand].sort(key=lambda x: x["volume"], reverse=True)
                    top_sku_map[bucket][brand] = top_sku_map[bucket][brand][:5]
            
            # 1. Aggregate for overall table
            table_data = {c: {} for c in capacity_buckets}
            
            # 2. Aggregate for trend
            periods_data = {}
            
            for row in result:
                brand = row.brand or "Unknown"
                bucket = get_capacity_bucket(row.capacity)
                if not bucket:
                    continue
                price = float(row.avg_price or 0.0)
                units = int(row.total_units or 0)
                revenue = float(row.total_revenue or 0.0)
                y = int(row.year)
                m = int(row.month)
                
                if brand not in table_data[bucket]:
                    table_data[bucket][brand] = {
                        "prices": [],
                        "volumes": [],
                        "revenues": []
                    }
                table_data[bucket][brand]["prices"].append(price)
                table_data[bucket][brand]["volumes"].append(units)
                table_data[bucket][brand]["revenues"].append(revenue)
                
                period_key = y * 12 + m
                if period_key not in periods_data:
                    months_abbr = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
                    period_label = f"{months_abbr[m-1]}-{str(y)[-2:]}"
                    periods_data[period_key] = {
                        "period_key": period_key,
                        "period_label": period_label,
                        "year": y,
                        "month": m,
                        "capacity_trends": {c: {} for c in capacity_buckets}
                    }
                
                periods_data[period_key]["capacity_trends"][bucket][brand] = round(price, 2)
                
            # Compute averages and rankings
            table_output = []
            for bucket in capacity_buckets:
                brand_stats = []
                for brand, stats in table_data[bucket].items():
                    avg_mop = sum(stats["prices"]) / len(stats["prices"]) if stats["prices"] else 0.0
                    total_vol = sum(stats["volumes"])
                    total_rev = sum(stats["revenues"])
                    brand_stats.append({
                        "brand": brand,
                        "mop": round(avg_mop, 2),
                        "volume": total_vol,
                        "revenue": round(total_rev, 2)
                    })
                    
                # Sort according to rank_by parameter
                if rank_by == "volume":
                    brand_stats.sort(key=lambda x: x["volume"], reverse=True)
                elif rank_by == "revenue":
                    brand_stats.sort(key=lambda x: x["revenue"], reverse=True)
                else: # "price"
                    brand_stats.sort(key=lambda x: x["mop"], reverse=True)
                    
                for index, stat in enumerate(brand_stats):
                    top_skus_list = top_sku_map[bucket].get(stat["brand"], [])
                    table_output.append({
                        "brand": stat["brand"],
                        "capacity": bucket,
                        "mop": stat["mop"],
                        "volume": stat["volume"],
                        "revenue": stat["revenue"],
                        "rank": index + 1,
                        "top_sku": top_skus_list[0]["sku"] if top_skus_list else None,
                        "top_sku_volume": top_skus_list[0]["volume"] if top_skus_list else 0,
                        "top_5_skus": top_skus_list
                    })
                    
            # Sort chronological trend
            sorted_periods = sorted(periods_data.values(), key=lambda x: x["period_key"])
            trend_output = []
            for period in sorted_periods:
                trend_output.append({
                    "period": period["period_label"],
                    "year": period["year"],
                    "month": period["month"],
                    "capacity_trends": period["capacity_trends"]
                })
                
            return {
                "table": table_output,
                "trend": trend_output
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
