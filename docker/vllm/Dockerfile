ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159

FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends patch \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv

COPY docker/vllm/requirements.txt /tmp/requirements.txt
RUN pip install --require-hashes --no-deps -r /tmp/requirements.txt

COPY patches /tmp/patches
RUN for p in /tmp/patches/*.patch; do \
        patch --forward --strip=1 --directory=/opt/venv/lib/python3.12/site-packages < "$p" || exit 1; \
    done

COPY plugins/vllm_private_logging /tmp/vllm_private_logging
RUN pip install --no-deps --no-build-isolation --no-index /tmp/vllm_private_logging \
    && python -m unittest discover -s /tmp/vllm_private_logging/tests -v \
    && pip check

FROM ${PYTHON_IMAGE} AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        g++ \
        libgomp1 \
        libnuma1 \
        libtcmalloc-minimal4t64 \
    && rm -rf /var/lib/apt/lists/*

ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid ${APP_GID} vllm \
    && useradd --uid ${APP_UID} --gid ${APP_GID} --create-home --home-dir /home/vllm --shell /usr/sbin/nologin vllm \
    && mkdir -p /models /etc/vllm \
    && chown ${APP_UID}:${APP_GID} /models

COPY --from=builder /opt/venv /opt/venv
COPY docker/vllm/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/vllm/serve.yaml /etc/vllm/serve.yaml

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/home/vllm \
    MODELS_DIR=/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    VLLM_NO_USAGE_STATS=1 \
    DO_NOT_TRACK=1 \
    VLLM_PLUGINS=private_logging \
    VLLM_LOGGING_LEVEL=INFO

USER ${APP_UID}:${APP_GID}
WORKDIR /home/vllm
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
