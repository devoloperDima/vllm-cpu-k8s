import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

DEFAULT_ALLOW_PATTERNS = "*.json,*.safetensors,*.txt,*.model,*.tiktoken,*.jinja"
MARKER_NAME = ".download-complete"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("model-downloader")


def model_dir(models_root: Path, model_id: str) -> Path:
    return models_root / model_id.replace("/", "--")


def read_marker(target: Path) -> dict | None:
    marker = target / MARKER_NAME
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_marker(target: Path, payload: dict) -> None:
    tmp = target / f"{MARKER_NAME}.tmp"
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(target / MARKER_NAME)


def is_cached(marker: dict | None, model_id: str, revision: str) -> bool:
    if not marker:
        return False
    return marker.get("model_id") == model_id and revision in (
        marker.get("revision"),
        marker.get("commit"),
    )


def download(model_id: str, revision: str, target: Path, token: str | None) -> dict:
    patterns = [
        p.strip()
        for p in os.environ.get("MODEL_ALLOW_PATTERNS", DEFAULT_ALLOW_PATTERNS).split(",")
        if p.strip()
    ]
    commit = HfApi(token=token).model_info(model_id, revision=revision).sha
    log.info("Downloading %s@%s (commit %s) into %s", model_id, revision, commit, target)
    started = time.monotonic()
    snapshot_download(
        repo_id=model_id,
        revision=commit,
        local_dir=target,
        token=token,
        allow_patterns=patterns,
        max_workers=int(os.environ.get("MODEL_DOWNLOAD_WORKERS", "4")),
    )
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    log.info("Downloaded %.1f MiB in %.1fs", size / 2**20, time.monotonic() - started)
    return {
        "model_id": model_id,
        "revision": revision,
        "commit": commit,
        "size_bytes": size,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    model_id = os.environ.get("MODEL_ID")
    if not model_id:
        log.error("MODEL_ID environment variable is required")
        return 2
    revision = os.environ.get("MODEL_REVISION", "main")
    models_root = Path(os.environ.get("MODELS_DIR", "/models"))
    token = os.environ.get("HF_TOKEN") or None

    target = model_dir(models_root, model_id)
    target.mkdir(parents=True, exist_ok=True)

    with open(models_root / f".{target.name}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_cached(read_marker(target), model_id, revision):
            log.info("Model %s@%s found in %s, skipping download", model_id, revision, target)
            return 0
        try:
            write_marker(target, download(model_id, revision, target, token))
        except Exception:
            log.exception("Failed to download %s@%s", model_id, revision)
            return 1

    log.info("Model is ready at %s", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
