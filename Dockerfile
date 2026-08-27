FROM python:3.12-slim
WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY static ./static
COPY demo_data ./demo_data
ENV PORT=8080 DATA_DIR=/srv/data
EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
