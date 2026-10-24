FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_LINK_MODE=copy
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY ui ./ui
COPY .streamlit ./.streamlit
RUN uv sync --frozen --no-dev && mkdir -p runtime
EXPOSE 8000 8501
CMD ["uv", "run", "--no-sync", "supplylens", "serve", "--host", "0.0.0.0"]

