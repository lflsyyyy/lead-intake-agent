FROM python:3.12-slim
WORKDIR /app
COPY server.py .
RUN useradd --create-home --uid 10001 mock
USER mock
EXPOSE 8090
CMD ["python", "-u", "server.py"]
