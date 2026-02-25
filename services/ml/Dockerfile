FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Generate pre-trained model on first build
RUN python generate_model.py

CMD ["python", "-u", "main.py"]
