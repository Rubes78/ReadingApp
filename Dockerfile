FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ai.py goodreads.py store.py ./
COPY templates/ templates/

ENV PORT=5000 DATA_DIR=/data

EXPOSE 5000

CMD ["python", "app.py"]
