FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
ENV DB_PATH=/data/bot.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "bot.main"]
