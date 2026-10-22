FROM mcr.microsoft.com/playwright/python:v1.58.0-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PRICE_DB_PATH=/app/data/prices_history.db

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Garante runtime do Chromium no ambiente Playwright.
RUN playwright install chromium

COPY scrapper.py /app/scrapper.py
COPY scrapper_geral.py /app/scrapper_geral.py
COPY scrapper_pokemon.py /app/scrapper_pokemon.py
COPY cupons.py /app/cupons.py
COPY qrcode.py /app/qrcode.py

RUN mkdir -p /app/data && chown -R pwuser:pwuser /app

USER pwuser

EXPOSE 8000

CMD ["uvicorn", "scrapper:app", "--host", "0.0.0.0", "--port", "8000"]