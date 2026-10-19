FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py board_auth.py ./
COPY templates ./templates
ENV PORT=8080 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD exec gunicorn --bind :${PORT} --workers 2 --threads 4 --timeout 60 app:app
