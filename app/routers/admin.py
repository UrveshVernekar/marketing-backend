from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks
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


@router.get("/marketing-data")
async def get_marketing_data():
    try:
        with engine.connect() as conn:
            result = conn.execute(text("SELECT *, (price * sales_units) AS sales_value FROM marketing_data ORDER BY year DESC, month DESC, sp_cell")).fetchall()
            return [dict(r._mapping) for r in result]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))