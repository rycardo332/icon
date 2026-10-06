FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi-dev \
    shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p media privado static

# Variables falsas solo para que collectstatic pueda cargar settings.py al construir
RUN DEBUG=True SECRET_KEY=build DATABASE_URL=sqlite:////tmp/build.db \
    EMAIL_HOST_USER=x EMAIL_HOST_PASSWORD=x \
    python manage.py collectstatic --noinput

EXPOSE 10000
CMD python manage.py migrate --noinput && \
    gunicorn sena.wsgi:application --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 4 --timeout 120