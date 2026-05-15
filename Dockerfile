FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl bash && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://cli.kiro.dev/install | bash || true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
