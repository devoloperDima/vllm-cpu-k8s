#!/usr/bin/env bash
set -euo pipefail

: "${MODEL_ID:?MODEL_ID must be set}"
models_dir="${MODELS_DIR:-/models}"
model_path="${models_dir}/${MODEL_ID//\//--}"
config_file="${VLLM_SERVE_CONFIG:-/etc/vllm/serve.yaml}"

if [[ ! -f "${model_path}/.download-complete" ]]; then
  echo "Model ${MODEL_ID} is not present in ${model_path}. Run the model downloader first." >&2
  exit 1
fi

python -m vllm_private_logging.check

case "$(uname -m)" in
  x86_64) preload="/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4:${VIRTUAL_ENV}/lib/libiomp5.so" ;;
  aarch64) preload="/usr/lib/aarch64-linux-gnu/libtcmalloc_minimal.so.4" ;;
  *) preload="" ;;
esac
export LD_PRELOAD="${preload}${LD_PRELOAD:+:${LD_PRELOAD}}"

args=(serve "${model_path}" --served-model-name "${SERVED_MODEL_NAME:-${MODEL_ID}}")
if [[ -f "${config_file}" ]]; then
  args+=(--config "${config_file}")
fi

exec vllm "${args[@]}" "$@"
