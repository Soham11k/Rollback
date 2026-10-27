FROM python:3.11-slim
WORKDIR /app
COPY app.py .
ENV PORT=8080 APP_VERSION=1.0.0
EXPOSE 8080
CMD ["python", "app.py"]
