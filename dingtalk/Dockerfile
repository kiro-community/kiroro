FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY connector.py .

# Health signal: if the process dies, container exits → Fargate restarts
CMD ["python3", "-u", "connector.py"]
