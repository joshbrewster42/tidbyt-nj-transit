FROM python:3.12-slim

# gtfs-realtime-bindings is the only dependency; everything else is stdlib.
RUN pip install --no-cache-dir gtfs-realtime-bindings==1.0.0

WORKDIR /app
COPY njt_live.py /app/njt_live.py

# Unbuffered so docker logs show the poll lines as they happen.
ENV PYTHONUNBUFFERED=1
EXPOSE 8081
CMD ["python3", "/app/njt_live.py"]
