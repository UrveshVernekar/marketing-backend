FROM python:3.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# --- PART 1: Switch to a fast, reliable global package mirror ---
# RUN sed -i 's/deb.debian.org/debian.map.fastly.net/g' /etc/apt/sources.list.d/debian.sources
RUN sed -i 's/http:/https:/g' /etc/apt/sources.list.d/debian.sources

# --- PART 2: Update and install GCC ---
RUN apt-get update && apt-get install -y --no-install-recommends gcc

# --- PART 3: Update and install Build Essentials ---
RUN apt-get update && apt-get install -y --no-install-recommends build-essential

# --- PART 4: Update and install Postgres Libraries ---
RUN apt-get update && apt-get install -y --no-install-recommends libpq-dev

# --- PART 5: Clean up temporary files ---
RUN rm -rf /var/lib/apt/lists/*

# --- PART 6: Python Requirements ---
COPY requirements.txt .
# RUN pip install --no-cache-dir -r requirements.txt
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000