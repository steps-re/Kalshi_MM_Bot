FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY analysis_app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# The app imports the real fee model so every published number is computed.
COPY src/ /app/src/
COPY analysis_app/ /app/analysis_app/

# Streamlit reads .streamlit/ relative to the working directory, so the
# theme only applies if the config lands at WORKDIR, not beside the app.
RUN cp -r /app/analysis_app/.streamlit /app/.streamlit

ENV PYTHONPATH=/app/src:/app/analysis_app

EXPOSE 8080

# Cloud Run sets PORT; Streamlit needs it passed explicitly.
CMD exec streamlit run analysis_app/app.py \
    --server.port=${PORT:-8080} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
