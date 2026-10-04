FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY freellm ./freellm
COPY pyproject.toml README.md ./
RUN pip install -e .
ENV PORT=3001
ENV HOST=0.0.0.0
EXPOSE 3001
VOLUME ["/app/data"]
CMD ["python", "-m", "freellm"]
