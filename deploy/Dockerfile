FROM python:3.12-slim

WORKDIR /app
ENV BRIEF_ROOT=/app
COPY pyproject.toml ./
COPY briefing ./briefing
COPY extension ./extension
RUN pip install --no-cache-dir .
EXPOSE 8787
CMD ["uvicorn", "briefing.main:app", "--host", "0.0.0.0", "--port", "8787"]
