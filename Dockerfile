FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create data directory for SQLite persistence
RUN mkdir -p /data
ENV DATABASE_URL=sqlite:////data/textbase.db

EXPOSE 8080
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "30"]
