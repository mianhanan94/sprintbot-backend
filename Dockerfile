FROM python:3.13-slim

WORKDIR /app

# Copy and install dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    requests \
    python-dotenv \
    google-genai \
    google-adk \
    google-cloud-aiplatform \
    google-cloud-firestore \
    google-cloud-logging \
    google-cloud-secret-manager \
    google-cloud-texttospeech

# Copy application code
COPY . .

# Cloud Run injects PORT (default 8080)
ENV PORT=8080
EXPOSE 8080

CMD ["python", "api.py"]
