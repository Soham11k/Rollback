FROM python:3.11-slim

WORKDIR /loadgen

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY loadgen.py .

CMD ["python", "loadgen.py"]
