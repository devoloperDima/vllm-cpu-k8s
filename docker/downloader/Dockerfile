ARG PYTHON_IMAGE=python:3.12-alpine3.22@sha256:a190708a2dec1bd18b1decb539f8e8f5407abaa9bf39cacda583f7f8c11db322

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
COPY docker/downloader/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt \
    && /opt/venv/bin/pip check

FROM ${PYTHON_IMAGE}

ARG APP_UID=10001
ARG APP_GID=10001
RUN addgroup -S -g ${APP_GID} downloader \
    && adduser -S -D -H -u ${APP_UID} -G downloader downloader \
    && mkdir -p /models \
    && chown ${APP_UID}:${APP_GID} /models

COPY --from=builder /opt/venv /opt/venv
COPY scripts/download_model.py /opt/app/download_model.py

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    HF_HOME=/tmp/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    MODELS_DIR=/models

USER ${APP_UID}:${APP_GID}

ENTRYPOINT ["python", "/opt/app/download_model.py"]
