FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir Pillow requests

COPY app.py .

EXPOSE 7860

CMD ["python", "app.py", "7860"]
