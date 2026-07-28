FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Chromium + OS-level deps for the JS-rendered scrapers (DevelopmentAid etc.)
RUN playwright install --with-deps chromium

COPY app ./app
RUN mkdir -p data logs

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
