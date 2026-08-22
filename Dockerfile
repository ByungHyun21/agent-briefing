FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir markdown==3.5.2
COPY server.py /app/server.py
EXPOSE 49010
CMD ["python", "server.py"]
