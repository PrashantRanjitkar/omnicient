# Omnicient API.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# A Unicode font for the PDF report: ReportLab's built-in Helvetica covers
# Latin-1 only and prints any other name as boxes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# State lives in Neo4j, so this image is stateless: no volume, and the
# connection settings arrive from the environment at run time.
ENV NEO4J_URI=bolt://neo4j:7687 \
    NEO4J_USERNAME=neo4j \
    NEO4J_DATABASE=neo4j

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
