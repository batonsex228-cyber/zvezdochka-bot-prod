FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY data ./data
COPY scripts ./scripts
COPY tests ./tests

RUN mkdir -p /app/runtime \
 && python scripts/preflight.py \
 && python -m compileall -q app scripts tests \
 && python -W error::ResourceWarning -m unittest discover -s tests -p "test_*.py" -q

CMD ["python", "-m", "app.main"]
