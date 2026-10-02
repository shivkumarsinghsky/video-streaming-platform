FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 app && mkdir /data && chown app /data
USER app
EXPOSE 8000
ENTRYPOINT ["python", "-m", "video"]
CMD ["--port", "8000", "--storage", "/data"]
