from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from typing import Optional
import pandas as pd
import numpy as np
import io
import re
import traceback
import uuid
import csv
import datetime
from rapidfuzz import fuzz
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from sqlalchemy import text
from app.core.database import engine
from pyxlsb import convert_date as pyxlsb_convert_date

router = APIRouter(
    prefix="/admin",
    tags=["admin"]
)

embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# =========================================================
# MONTH PARSING
# =========================================================
month_map = {
    'Jan': 1,
    'Feb': 2,
    'Mar': 3,
    'Apr': 4,
    'May': 5,
    'Jun': 6,
    'Jul': 7,
    'Aug': 8,
    'Sep': 9,
    'Oct': 10,
    'Nov': 11,
    'Dec': 12
}

MONTH_PATTERN = re.compile(
    r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[-_ ]?\d{2,4}$',
    re.IGNORECASE
)

def parse_period(col_name):
    match = re.match(r'([A-Za-z]+)[-_ ]?(\d+)', col_name)

    if match:
        mon_str, yr_str = match.groups()

        year = (
            2000 + int(yr_str)
            if len(yr_str) == 2
            else int(yr_str)
        )

        month = month_map.get(mon_str[:3].title())

        return year, month

    return None, None

# =========================================================
# NORMALIZATION
# =========================================================
def normalize_column(col):
    return (
        str(col)
        .strip()
        .lower()
        .replace("\n", " ")
        .replace("_", " ")
        .replace("-", " ")
        .replace(".", " ")
        .replace("  ", " ")
    )

# =========================================================
# MARKETING DATA SCHEMA DEFINITIONS
# =========================================================
MARKETING_SCHEMA_FIELDS = {
    "sp_cell": [
        "sp cell",
        "spcell",
        "sp_cell",
        "sales channel",
        "channel"
    ],
    "city": [
        "city",
        "city2",
        "location",
        "town"
    ],
    "period": [
        "period",
        "month year",
        "month_year",
        "date",
        "month-year"
    ],
    "state": [
        "states",
        "state",
        "region"
    ],
    "brand": [
        "brand",
        "brand name"
    ],
    "item": [
        "item",
        "model",
        "product",
        "item code"
    ],
    "drying_function": [
        "drying function",
        "drying_function",
        "dry function",
        "dryer"
    ],
    "loading": [
        "loading",
        "load type",
        "loading type"
    ],
    "capacity": [
        "loading kg",
        "loading_kg",
        "capacity",
        "capacity kg",
        "kg"
    ],
    "steam_funct_int": [
        "steam funct int",
        "steam_funct_int",
        "steam function internal",
        "steam funct"
    ],
    "first_activity": [
        "first activity",
        "first_activity",
        "launch date",
        "first active"
    ],
    "sales_units": [
        "sales units",
        "sales_units",
        "quantity",
        "units sold"
    ],
    "price": [
        "price inr",
        "price_inr",
        "price",
        "mrp"
    ],
    "motor_type": [
        "motor type",
        "motortype",
        "motor_type",
        "motor"
    ],
    "steam_function": [
        "steam function",
        "steam_function",
        "steam"
    ]
}

marketing_schema_embeddings = {}
for db_col, aliases in MARKETING_SCHEMA_FIELDS.items():
    marketing_schema_embeddings[db_col] = embedding_model.encode(aliases)

# =========================================================
# COLUMN MATCHER
# =========================================================
def find_best_matches(excel_col, SCHEMA_WITH_FIELDS, excel_embedding=None, cached_embeddings=None):
    normalized_excel = normalize_column(excel_col)
    best_score = 0
    best_field = None

    if excel_embedding is None:
        excel_embedding = embedding_model.encode([normalized_excel])[0]
    
    if cached_embeddings is None:
        cached_embeddings = {}
        for db_col, aliases in SCHEMA_WITH_FIELDS.items():
            cached_embeddings[db_col] = embedding_model.encode(aliases)

    for db_col, aliases in SCHEMA_WITH_FIELDS.items():
        # EXACT MATCH BOOST
        for alias in aliases:
            normalized_alias = normalize_column(alias)
            if normalized_excel == normalized_alias:
                return db_col, 100

        # FUZZY MATCH
        fuzzy_score = max(
            fuzz.token_sort_ratio(
                normalized_excel,
                normalize_column(alias)
            )
            for alias in aliases
        )

        # SEMANTIC MATCH
        semantic_scores = cosine_similarity(
            [excel_embedding],
            cached_embeddings[db_col]
        )[0]
        semantic_score = max(semantic_scores) * 100

        # COMBINED SCORE
        final_score = fuzzy_score * 0.7 + semantic_score * 0.3

        if final_score > best_score:
            best_score = final_score
            best_field = db_col

    return best_field, best_score

# =========================================================
# DATABASE BULK INSERT METHOD
# =========================================================
def psql_insert_copy(table, conn, keys, data_iter):
    dbapi_conn = conn.connection
    with dbapi_conn.cursor() as cur:
        s_buf = io.StringIO()
        writer = csv.writer(s_buf)
        for row in data_iter:
            clean_row = []
            for val in row:
                if pd.isna(val):
                    clean_row.append(None)
                elif isinstance(val, float) and val.is_integer():
                    clean_row.append(int(val))
                else:
                    clean_row.append(val)
            writer.writerow(clean_row)
        s_buf.seek(0)
        columns = ', '.join([f'"{k}"' for k in keys])
        table_name = f'{table.schema}.{table.name}' if table.schema else table.name
        sql = f'COPY {table_name} ({columns}) FROM STDIN WITH CSV'
        cur.copy_expert(sql=sql, file=s_buf)

# =========================================================
# MARKETING UPLOAD API
# =========================================================
marketing_upload_tasks = {}

@router.get("/upload-marketing/status/{task_id}")
async def get_marketing_upload_status(task_id: str):
    if task_id not in marketing_upload_tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return marketing_upload_tasks[task_id]

@router.post("/upload-marketing")
async def upload_marketing_data(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    filename = file.filename.lower()
    if not filename.endswith(('.xlsx', '.xls', '.xlsb', '.csv')):
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Only Excel (.xlsx, .xls, .xlsb) and CSV (.csv) files are allowed."
        )

    content = await file.read()
    task_id = str(uuid.uuid4())
    marketing_upload_tasks[task_id] = {"status": "processing", "progress": 0, "message": "Initializing upload..."}
    
    background_tasks.add_task(process_marketing_upload_task, task_id, content, file.filename)
    
    return {
        "message": "Upload started",
        "task_id": task_id
    }

def process_marketing_upload_task(task_id: str, content: bytes, filename: str):
    try:
        marketing_upload_tasks[task_id] = {"status": "processing", "progress": 10, "message": "Reading file..."}

        lower_name = filename.lower()
        if lower_name.endswith('.csv'):
            try:
                df_raw = pd.read_csv(io.BytesIO(content), header=None)
            except Exception as read_err:
                raise Exception(f"Failed to read CSV file: {str(read_err)}")
        else:
            try:
                excel_file = pd.ExcelFile(io.BytesIO(content))
                sheet_names = excel_file.sheet_names
                
                target_sheet = None
                for sheet in sheet_names:
                    if sheet.strip().lower() == 'master':
                        target_sheet = sheet
                        break
                
                if target_sheet is None:
                    target_sheet = sheet_names[0]
                
                xls_engine = None
                if lower_name.endswith('.xlsb'):
                    xls_engine = 'pyxlsb'
                
                df_raw = pd.read_excel(
                    io.BytesIO(content),
                    sheet_name=target_sheet,
                    header=None,
                    engine=xls_engine
                )
            except Exception as read_err:
                raise Exception(f"Failed to read Excel sheets: {str(read_err)}")

        marketing_upload_tasks[task_id] = {"status": "processing", "progress": 20, "message": "Finding header row..."}

        # FIND HEADER ROW
        best_row_idx = 0
        best_row_score = -1

        for idx in range(min(30, len(df_raw))):
            row_values = df_raw.iloc[idx].values
            score = 0
            for val in row_values:
                if pd.isna(val):
                    continue
                norm_val = normalize_column(val)
                if not norm_val:
                    continue
                for db_col, aliases in MARKETING_SCHEMA_FIELDS.items():
                    if any(normalize_column(a) == norm_val for a in aliases):
                        score += 2
                        break
                    elif any(len(norm_val) > 3 and normalize_column(a) in norm_val for a in aliases):
                        score += 1
                        break
            if score > best_row_score:
                best_row_score = score
                best_row_idx = idx

        # Set best row as header row
        header_row = df_raw.iloc[best_row_idx].copy()
        cleaned_header_row = []
        for i, val in enumerate(header_row):
            if pd.isna(val):
                cleaned_header_row.append(f"Unnamed_{i}")
            else:
                cleaned_header_row.append(str(val).strip())
        header_row = cleaned_header_row

        df = df_raw.copy()
        df.columns = header_row
        df = df.iloc[best_row_idx + 1:].reset_index(drop=True)

        marketing_upload_tasks[task_id] = {"status": "processing", "progress": 40, "message": "Mapping columns..."}
        column_mapping = {}
        schema_field_scores = {}

        cols_to_match = [col for col in df.columns if not str(col).startswith("Unnamed_")]
        if cols_to_match:
            normalized_cols = [normalize_column(col) for col in cols_to_match]
            col_embeddings = embedding_model.encode(normalized_cols)
            
            for col, excel_embedding in zip(cols_to_match, col_embeddings):
                best_field, score = find_best_matches(
                    col, 
                    MARKETING_SCHEMA_FIELDS, 
                    excel_embedding=excel_embedding, 
                    cached_embeddings=marketing_schema_embeddings
                )
                if score >= 65:
                    # Prevent duplicate mapping by keeping only highest score
                    if best_field in schema_field_scores:
                        if score > schema_field_scores[best_field]:
                            keys_to_remove = [k for k, v in column_mapping.items() if v == best_field]
                            for k in keys_to_remove:
                                del column_mapping[k]
                            column_mapping[col] = best_field
                            schema_field_scores[best_field] = score
                    else:
                        column_mapping[col] = best_field
                        schema_field_scores[best_field] = score

        # Check required columns
        required_fields = ["sp_cell", "period"]
        missing_fields = [f for f in required_fields if f not in column_mapping.values()]
        if missing_fields:
            raise Exception(f"Required column(s) not identified: {', '.join(missing_fields)}")

        marketing_upload_tasks[task_id] = {"status": "processing", "progress": 60, "message": "Parsing marketing data..."}
        
        # Keep only identified columns
        mapped_df = df[list(column_mapping.keys())].copy()
        mapped_df.rename(columns=column_mapping, inplace=True)

        # Parse period to month and year
        parsed_months = []
        parsed_years = []
        
        def parse_period_split(val):
            if pd.isna(val):
                return None, None
            if isinstance(val, (int, float)):
                try:
                    val = pyxlsb_convert_date(val)
                except Exception:
                    pass
            if isinstance(val, (pd.Timestamp, datetime.datetime, datetime.date)):
                return val.year, val.month
            val_str = str(val).strip()
            # Try parse_period (e.g. Jun-25, Jan-25) first to avoid pd.to_datetime parsing it as year 0001
            y, m = parse_period(val_str)
            if y is not None and m is not None:
                return y, m
            # Try parsing standard dates like YYYY-MM-DD
            try:
                dt = pd.to_datetime(val_str)
                if dt.year > 100:
                    return dt.year, dt.month
            except Exception:
                pass
            return None, None

        for _, val in mapped_df["period"].items():
            y, m = parse_period_split(val)
            parsed_years.append(y)
            parsed_months.append(m)

        mapped_df["year"] = parsed_years
        mapped_df["month"] = parsed_months

        # Drop rows where year or month is null, or sp_cell is null/empty
        mapped_df.dropna(subset=["year", "month", "sp_cell"], inplace=True)
        mapped_df = mapped_df[mapped_df["sp_cell"].astype(str).str.strip() != ""]
        mapped_df["year"] = mapped_df["year"].astype(int)
        mapped_df["month"] = mapped_df["month"].astype(int)

        # Parse first_activity as DATE
        def parse_date_only(val):
            if pd.isna(val):
                return None
            if isinstance(val, (int, float)):
                try:
                    val = pyxlsb_convert_date(val)
                except Exception:
                    pass
            if isinstance(val, (pd.Timestamp, datetime.datetime)):
                return val.date()
            if isinstance(val, datetime.date):
                return val
            val_str = str(val).strip()
            if val_str == "0" or val_str == "":
                return None
            for fmt in ("%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%b-%y", "%B-%y"):
                try:
                    return datetime.datetime.strptime(val_str, fmt).date()
                except ValueError:
                    continue
            try:
                dt = pd.to_datetime(val_str, dayfirst=True)
                return dt.date()
            except Exception:
                pass
            return None

        if "first_activity" in mapped_df.columns:
            mapped_df["first_activity"] = mapped_df["first_activity"].apply(parse_date_only)
        else:
            mapped_df["first_activity"] = None

        # Coerce numeric fields
        numeric_cols = ["capacity", "sales_units", "price"]
        for col in numeric_cols:
            if col in mapped_df.columns:
                mapped_df[col] = pd.to_numeric(mapped_df[col], errors='coerce').fillna(0.0)
            else:
                mapped_df[col] = 0.0

        # Cast sales_units to integer
        mapped_df["sales_units"] = mapped_df["sales_units"].astype(int)

        # String default cleaning
        str_cols = [
            "city", "state", "brand", "item", "drying_function", 
            "loading", "steam_funct_int", "motor_type", "steam_function"
        ]
        for col in str_cols:
            if col in mapped_df.columns:
                mapped_df[col] = mapped_df[col].astype(str).str.strip()
                mapped_df[col] = mapped_df[col].replace({'nan': None, 'None': None, '0.0': None, '0': None})
                mapped_df[col] = mapped_df[col].apply(lambda x: None if x == "" else x)
            else:
                mapped_df[col] = None

        # We will keep only database columns
        db_cols = [
            "sp_cell", "city", "month", "year", "state", "brand", "item", 
            "drying_function", "loading", "capacity", "steam_funct_int", 
            "first_activity", "sales_units", "price", 
            "motor_type", "steam_function"
        ]

        marketing_upload_tasks[task_id] = {"status": "processing", "progress": 80, "message": "Saving to database..."}

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM marketing_data CASCADE"))
            if not mapped_df.empty:
                db_df = mapped_df[db_cols].copy()
                db_df.to_sql(
                    'marketing_data',
                    con=conn,
                    if_exists='append',
                    index=False,
                    method=psql_insert_copy
                )

        marketing_upload_tasks[task_id] = {
            "status": "completed",
            "progress": 100,
            "message": "Marketing data imported successfully",
            "records_count": len(mapped_df)
        }

    except Exception as e:
        traceback.print_exc()
        marketing_upload_tasks[task_id] = {
            "status": "failed",
            "progress": 0,
            "message": "Import failed",
            "error": str(e)
        }


WHITELIST_COLS = {
    "sp_cell", "city", "state", "brand", "item", "drying_function",
    "loading", "capacity", "steam_funct_int", "sales_units", "price",
    "motor_type", "steam_function", "period", "sales_value"
}

month_abbr_map = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
}

def build_numeric_filter(col_name: str, filter_str: str, params: dict, param_counter: list) -> str:
    conditions = [c.strip() for c in filter_str.split(",") if c.strip()]
    clauses = []
    for cond in conditions:
        param_name = f"{col_name.replace('(', '').replace(')', '').replace('*', '').replace(' ', '')}_filter_{param_counter[0]}"
        param_counter[0] += 1
        
        if cond.startswith(">="):
            try:
                params[param_name] = float(cond[2:].strip())
                clauses.append(f"{col_name} >= :{param_name}")
            except ValueError:
                pass
        elif cond.startswith("<="):
            try:
                params[param_name] = float(cond[2:].strip())
                clauses.append(f"{col_name} <= :{param_name}")
            except ValueError:
                pass
        elif cond.startswith("!="):
            try:
                params[param_name] = float(cond[2:].strip())
                clauses.append(f"{col_name} != :{param_name}")
            except ValueError:
                pass
        elif cond.startswith(">"):
            try:
                params[param_name] = float(cond[1:].strip())
                clauses.append(f"{col_name} > :{param_name}")
            except ValueError:
                pass
        elif cond.startswith("<"):
            try:
                params[param_name] = float(cond[1:].strip())
                clauses.append(f"{col_name} < :{param_name}")
            except ValueError:
                pass
        elif cond.startswith("="):
            try:
                params[param_name] = float(cond[1:].strip())
                clauses.append(f"{col_name} = :{param_name}")
            except ValueError:
                pass
        elif ".." in cond:
            parts = cond.split("..")
            if len(parts) == 2:
                try:
                    p_min = float(parts[0].strip())
                    p_max = float(parts[1].strip())
                    p_name_min = f"{param_name}_min"
                    p_name_max = f"{param_name}_max"
                    params[p_name_min] = p_min
                    params[p_name_max] = p_max
                    clauses.append(f"{col_name} BETWEEN :{p_name_min} AND :{p_name_max}")
                except ValueError:
                    pass
        elif "-" in cond:
            parts = cond.split("-")
            if len(parts) == 2:
                try:
                    p_min = float(parts[0].strip())
                    p_max = float(parts[1].strip())
                    p_name_min = f"{param_name}_min"
                    p_name_max = f"{param_name}_max"
                    params[p_name_min] = p_min
                    params[p_name_max] = p_max
                    clauses.append(f"{col_name} BETWEEN :{p_name_min} AND :{p_name_max}")
                except ValueError:
                    pass
        else:
            try:
                params[param_name] = float(cond)
                clauses.append(f"{col_name} = :{param_name}")
            except ValueError:
                params[param_name] = f"%{cond}%"
                clauses.append(f"CAST({col_name} AS TEXT) LIKE :{param_name}")
    if clauses:
        return " AND ".join(clauses)
    return ""

def parse_period_filter(val: str, params: dict, param_counter: list) -> str:
    val = val.lower().strip()
    clauses = []
    if "-" in val:
        parts = val.split("-")
        if len(parts) == 2:
            m_str, y_str = parts[0].strip(), parts[1].strip()
            m_val = month_abbr_map.get(m_str[:3])
            if m_val:
                p_m = f"period_m_{param_counter[0]}"
                params[p_m] = m_val
                clauses.append(f"month = :{p_m}")
            try:
                y_val = int(y_str)
                if y_val < 100:
                    y_val += 2000
                p_y = f"period_y_{param_counter[0]}"
                params[p_y] = y_val
                clauses.append(f"year = :{p_y}")
            except ValueError:
                pass
    else:
        m_val = month_abbr_map.get(val[:3])
        if m_val:
            p_m = f"period_m_{param_counter[0]}"
            params[p_m] = m_val
            clauses.append(f"month = :{p_m}")
        else:
            try:
                y_val = int(val)
                p_y = f"period_y_{param_counter[0]}"
                if y_val < 100:
                    y_val_4d = 2000 + y_val
                    params[p_y] = y_val_4d
                    clauses.append(f"(year = :{p_y} OR (year % 100) = {y_val})")
                else:
                    params[p_y] = y_val
                    clauses.append(f"year = :{p_y}")
            except ValueError:
                p_y_text = f"period_y_text_{param_counter[0]}"
                params[p_y_text] = f"%{val}%"
                clauses.append(f"CAST(year AS TEXT) LIKE :{p_y_text}")
    param_counter[0] += 1
    if clauses:
        return " AND ".join(clauses)
    return ""

def build_where_clause(search: str, filters: dict, params: dict) -> str:
    clauses = []
    param_counter = [0]
    
    if search:
        search_terms = search.lower().strip().split()
        for term in search_terms:
            p_search = f"search_term_{param_counter[0]}"
            param_counter[0] += 1
            params[p_search] = f"%{term}%"
            clauses.append(f"""
                (LOWER(sp_cell) LIKE :{p_search} OR
                 LOWER(brand) LIKE :{p_search} OR
                 LOWER(item) LIKE :{p_search} OR
                 LOWER(state) LIKE :{p_search} OR
                 LOWER(city) LIKE :{p_search})
            """)
            
    for col, filter_val in filters.items():
        if not filter_val:
            continue
            
        if col in ["sales_units", "price", "capacity"]:
            numeric_clause = build_numeric_filter(col, filter_val, params, param_counter)
            if numeric_clause:
                clauses.append(f"({numeric_clause})")
        elif col == "sales_value":
            numeric_clause = build_numeric_filter("(price * sales_units)", filter_val, params, param_counter)
            if numeric_clause:
                clauses.append(f"({numeric_clause})")
        elif col == "period":
            period_clause = parse_period_filter(filter_val, params, param_counter)
            if period_clause:
                clauses.append(f"({period_clause})")
        elif col in WHITELIST_COLS:
            p_col = f"col_filter_{col}_{param_counter[0]}"
            param_counter[0] += 1
            params[p_col] = f"%{filter_val.lower().strip()}%"
            clauses.append(f"LOWER({col}) LIKE :{p_col}")
            
    if clauses:
        return " AND ".join(clauses)
    return "1=1"

@router.get("/marketing-data")
async def get_marketing_data(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1),
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_order: Optional[str] = Query(None),
    sp_cell: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    item: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    sales_units: Optional[str] = Query(None),
    sales_value: Optional[str] = Query(None),
    price: Optional[str] = Query(None),
    capacity: Optional[str] = Query(None),
    motor_type: Optional[str] = Query(None),
    steam_function: Optional[str] = Query(None)
):
    try:
        filters = {
            "sp_cell": sp_cell,
            "brand": brand,
            "item": item,
            "period": period,
            "state": state,
            "city": city,
            "sales_units": sales_units,
            "sales_value": sales_value,
            "price": price,
            "capacity": capacity,
            "motor_type": motor_type,
            "steam_function": steam_function
        }
        filters = {k: v for k, v in filters.items() if v is not None}
        
        params = {}
        where_clause = build_where_clause(search, filters, params)
        
        order_clause = "year DESC, month DESC, sp_cell"
        if sort_by and sort_by in WHITELIST_COLS:
            direction = "DESC" if sort_order == "desc" else "ASC"
            if sort_by == "period":
                order_clause = f"year {direction}, month {direction}"
            elif sort_by == "sales_value":
                order_clause = f"(price * sales_units) {direction}"
            else:
                order_clause = f"{sort_by} {direction}"
                
        offset = (page - 1) * limit
        params["limit"] = limit
        params["offset"] = offset
        
        with engine.connect() as conn:
            count_query = text(f"SELECT COUNT(*) FROM marketing_data WHERE {where_clause}")
            total_count = conn.execute(count_query, params).scalar()
            
            data_query = text(f"""
                SELECT *, (price * sales_units) AS sales_value 
                FROM marketing_data 
                WHERE {where_clause} 
                ORDER BY {order_clause} 
                LIMIT :limit OFFSET :offset
            """)
            result = conn.execute(data_query, params).fetchall()
            items = [dict(r._mapping) for r in result]
            
            return {
                "items": items,
                "total_count": total_count
            }
            
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/marketing-kpis")
async def get_marketing_kpis(
    search: Optional[str] = Query(None),
    sp_cell: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    item: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    sales_units: Optional[str] = Query(None),
    sales_value: Optional[str] = Query(None),
    price: Optional[str] = Query(None),
    capacity: Optional[str] = Query(None),
    motor_type: Optional[str] = Query(None),
    steam_function: Optional[str] = Query(None)
):
    try:
        filters = {
            "sp_cell": sp_cell,
            "brand": brand,
            "item": item,
            "period": period,
            "state": state,
            "city": city,
            "sales_units": sales_units,
            "sales_value": sales_value,
            "price": price,
            "capacity": capacity,
            "motor_type": motor_type,
            "steam_function": steam_function
        }
        filters = {k: v for k, v in filters.items() if v is not None}
        
        params = {}
        where_clause = build_where_clause(search, filters, params)
        
        with engine.connect() as conn:
            kpi_query = text(f"""
                SELECT 
                    COALESCE(SUM(sales_units), 0) AS total_sales_units,
                    COALESCE(SUM(price * sales_units), 0) AS total_revenue,
                    COUNT(DISTINCT brand) AS total_brands
                FROM marketing_data
                WHERE {where_clause}
            """)
            row = conn.execute(kpi_query, params).fetchone()
            
            total_sales_units = row.total_sales_units
            total_revenue = row.total_revenue
            total_brands = row.total_brands
            
            avg_price = total_revenue / total_sales_units if total_sales_units > 0 else 0
            
            return {
                "total_sales_units": total_sales_units,
                "total_revenue": total_revenue,
                "avg_price": avg_price,
                "total_brands": total_brands
            }
            
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/marketing-data/export")
async def export_marketing_data(
    search: Optional[str] = Query(None),
    sort_by: Optional[str] = Query(None),
    sort_order: Optional[str] = Query(None),
    sp_cell: Optional[str] = Query(None),
    brand: Optional[str] = Query(None),
    item: Optional[str] = Query(None),
    period: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    sales_units: Optional[str] = Query(None),
    sales_value: Optional[str] = Query(None),
    price: Optional[str] = Query(None),
    capacity: Optional[str] = Query(None),
    motor_type: Optional[str] = Query(None),
    steam_function: Optional[str] = Query(None)
):
    try:
        filters = {
            "sp_cell": sp_cell,
            "brand": brand,
            "item": item,
            "period": period,
            "state": state,
            "city": city,
            "sales_units": sales_units,
            "sales_value": sales_value,
            "price": price,
            "capacity": capacity,
            "motor_type": motor_type,
            "steam_function": steam_function
        }
        filters = {k: v for k, v in filters.items() if v is not None}
        
        params = {}
        where_clause = build_where_clause(search, filters, params)
        
        order_clause = "year DESC, month DESC, sp_cell"
        if sort_by and sort_by in WHITELIST_COLS:
            direction = "DESC" if sort_order == "desc" else "ASC"
            if sort_by == "period":
                order_clause = f"year {direction}, month {direction}"
            elif sort_by == "sales_value":
                order_clause = f"(price * sales_units) {direction}"
            else:
                order_clause = f"{sort_by} {direction}"
                
        def get_month_name(m):
            months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            if 1 <= m <= 12:
                return months[m - 1]
            return ""

        def csv_generator():
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow([
                "Channel", "Brand", "Item Model", "Period", "State", "City", 
                "Sales Units", "Sales Value (INR)", "Unit Price (INR)", 
                "Capacity", "Motor Type", "Steam Function"
            ])
            yield output.getvalue()
            output.seek(0)
            output.truncate(0)
            
            batch_size = 10000
            offset = 0
            
            while True:
                batch_params = params.copy()
                batch_params["limit"] = batch_size
                batch_params["offset"] = offset
                
                with engine.connect() as conn:
                    query = text(f"""
                        SELECT *, (price * sales_units) AS sales_value
                        FROM marketing_data
                        WHERE {where_clause}
                        ORDER BY {order_clause}
                        LIMIT :limit OFFSET :offset
                    """)
                    rows = conn.execute(query, batch_params).fetchall()
                    
                if not rows:
                    break
                    
                output = io.StringIO()
                writer = csv.writer(output)
                for r in rows:
                    p_str = f"{get_month_name(r.month)}-{r.year}"
                    writer.writerow([
                        r.sp_cell,
                        r.brand or "",
                        r.item or "",
                        p_str,
                        r.state or "",
                        r.city or "",
                        r.sales_units,
                        r.sales_value,
                        r.price,
                        f"{r.capacity} kg" if r.capacity else "",
                        r.motor_type or "",
                        r.steam_function or ""
                    ])
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)
                
                offset += batch_size
                
        return StreamingResponse(
            csv_generator(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=marketing-data-export.csv"}
        )
        
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))