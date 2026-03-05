FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Run as non-root user
RUN adduser --disabled-password --no-create-home --gecos "" sentinel
USER sentinel
CMD ["python", "-u", "main.py"]
