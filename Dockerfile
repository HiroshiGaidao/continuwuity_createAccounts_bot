# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Установка Python зависимостей
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копирование кода
COPY bot.py .
COPY database.py .
COPY config.py .

# Создание директорий для данных
RUN mkdir -p /app/data /app/matrix_store

CMD ["python3", "-u", "bot.py"]