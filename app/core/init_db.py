from sqlalchemy import text
from app.core.database import engine
from app.core.security import hash_password

def init_db():
    # Schema migration check: if table exists but doesn't have the new column 'capacity', drop it
    with engine.begin() as conn:
        table_exists = conn.execute(text(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'marketing_data')"
        )).scalar()
        if table_exists:
            column_exists = conn.execute(text(
                "SELECT EXISTS (SELECT FROM information_schema.columns WHERE table_name = 'marketing_data' AND column_name = 'capacity')"
            )).scalar()
            if not column_exists:
                conn.execute(text("DROP TABLE IF EXISTS marketing_data CASCADE"))

    INIT_SQL = """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        first_name VARCHAR(100) NOT NULL,
        last_name VARCHAR(100) NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        role VARCHAR(50) NOT NULL DEFAULT 'user',
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS marketing_data (
        marketing_id SERIAL PRIMARY KEY,
        sp_cell VARCHAR(100) NOT NULL,
        city VARCHAR(255),
        month INT NOT NULL,
        year INT NOT NULL,
        state VARCHAR(100),
        brand VARCHAR(100),
        item VARCHAR(255),
        drying_function VARCHAR(100),
        loading VARCHAR(100),
        capacity DECIMAL(12, 2),
        steam_funct_int VARCHAR(100),
        first_activity DATE,
        sales_units INT DEFAULT 0,
        price DECIMAL(15, 2) DEFAULT 0,
        motor_type VARCHAR(100),
        steam_function VARCHAR(100),
        created_at TIMESTAMPTZ DEFAULT NOW(),
        updated_at TIMESTAMPTZ DEFAULT NOW()
    );
    """

    with engine.begin() as conn:
        conn.execute(text(INIT_SQL))
        
        # Seed default admin user if not exists
        admin_email = "marketing_admin@ifbglobal.com"
        result = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": admin_email}
        ).fetchone()
        
        if not result:
            hashed = hash_password("admin1234$#")
            conn.execute(
                text("INSERT INTO users (first_name, last_name, email, password_hash, role) VALUES (:first_name, :last_name, :email, :password_hash, :role)"),
                {
                    "first_name": "Marketing",
                    "last_name": "Admin",
                    "email": admin_email,
                    "password_hash": hashed,
                    "role": "admin"
                }
            )
