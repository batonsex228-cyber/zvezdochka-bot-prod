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
COPY assets ./assets

# Полные 192 теста уже обязательным шагом проходят в GitHub Actions
# перед Docker-сборкой. Внутри image оставляем безопасную проверку файлов
# и компиляцию Python-кода.
RUN mkdir -p /app/runtime \
 && python scripts/preflight.py \
 && python -m compileall -q app scripts

CMD ["python", "-m", "app.main"]
