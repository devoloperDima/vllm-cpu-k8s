import logging
from collections.abc import Sequence
from typing import Any

SAFE_RECORD_ATTR = "vllm_private_logging_safe"
SUMMARY_FIELDS = ("n", "max_tokens", "temperature", "top_p", "top_k", "seed")

_registered = False


class OnlySafeRecordsFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, SAFE_RECORD_ATTR, False) is True


def _length(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return len(value)
    except TypeError:
        return None


def _summarize_params(params: Any) -> str:
    if params is None:
        return "params=none"
    parts = [f"params={type(params).__name__}"]
    for field in SUMMARY_FIELDS:
        value = getattr(params, field, None)
        if isinstance(value, bool | int | float):
            parts.append(f"{field}={value}")
    return " ".join(parts)


def _build_methods(logger: logging.Logger) -> dict[str, Any]:
    safe = {SAFE_RECORD_ATTR: True}

    def log_inputs(
        self: Any,
        request_id: str,
        prompt: str | None,
        prompt_token_ids: list[int] | None,
        prompt_embeds: Any,
        params: Any,
        lora_request: Any,
    ) -> None:
        logger.info(
            "Received request %s: prompt_chars=%s prompt_tokens=%s "
            "prompt_embeds=%s %s lora=%s",
            request_id,
            _length(prompt),
            _length(prompt_token_ids),
            prompt_embeds is not None,
            _summarize_params(params),
            getattr(lora_request, "lora_name", None),
            extra=safe,
        )

    def log_request_body(self: Any, request: Any) -> None:
        return None

    def log_outputs(
        self: Any,
        request_id: str,
        outputs: str | None,
        output_token_ids: Sequence[int] | None,
        finish_reason: str | None = None,
        is_streaming: bool = False,
        delta: bool = False,
    ) -> None:
        if delta:
            return
        logger.info(
            "Generated response %s: output_tokens=%s finish_reason=%s streaming=%s",
            request_id,
            _length(output_token_ids),
            finish_reason,
            is_streaming,
            extra=safe,
        )

    return {
        "log_inputs": log_inputs,
        "log_request_body": log_request_body,
        "log_outputs": log_outputs,
    }


def register() -> None:
    global _registered
    if _registered:
        return

    from vllm.entrypoints.serve.utils import request_logger

    logger = request_logger.logger
    if not any(isinstance(f, OnlySafeRecordsFilter) for f in logger.filters):
        logger.addFilter(OnlySafeRecordsFilter())

    for name, method in _build_methods(logger).items():
        if not hasattr(request_logger.RequestLogger, name):
            raise RuntimeError(f"RequestLogger.{name} not found, vLLM API changed")
        setattr(request_logger.RequestLogger, name, method)

    _registered = True


def is_registered() -> bool:
    return _registered
