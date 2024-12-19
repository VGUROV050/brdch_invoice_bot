# Используем официальный образ Python как базовый
FROM python:3.11-slim

# Обновляем пакеты и устанавливаем Tesseract OCR и его языковые пакеты
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libtesseract-dev \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        poppler-utils \
        && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

# Устанавливаем зависимости Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальные файлы проекта
COPY . .

# Указываем команду для запуска бота
CMD ["python", "bot.py"]
