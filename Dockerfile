FROM python:3.12-slim
WORKDIR /app
COPY server/ /app/server/
COPY agents/ /app/agents/
ENV ROOM_DB=/data/room.db PORT=8765 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
CMD ["python", "server/server.py"]
