FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt /tmp/platform-requirements.txt

RUN python -m pip install --no-cache-dir \
        -r /tmp/platform-requirements.txt

COPY . /app

EXPOSE 5002

CMD ["python", "main.py"]
