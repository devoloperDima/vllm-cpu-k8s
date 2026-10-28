# vLLM на CPU в Kubernetes

Инференс instruct-модели (по умолчанию `Qwen/Qwen2.5-0.5B-Instruct`) на vLLM **0.32.0** строго на CPU в Kubernetes/Minikube.

Что реализовано:

- rootless-образ vLLM с полностью зафиксированными зависимостями (включая транзитивные, с sha256-хешами);
- приватное логирование: промпты и ответы модели не попадают в stdout/stderr, но факт каждого запроса логируется;
- PVC-кеш весов: модель скачивается с Hugging Face один раз, после рестарта пода загружается локально без обращения к сети;
- Secret для токена Hugging Face и Bearer-токена API;
- ConfigMap для параметров vLLM: настройки меняются без пересборки образа;
- **бонус 1:** kustomize (`kubectl apply -k`);
- **бонус 2:** загрузка весов вынесена в `initContainer` на отдельном лёгком rootless-образе (Alpine, ~95 МБ);
- **бонус 3:** нагрузочный тест (TTFT, latency, throughput) и рекомендации по тюнингу.

## Структура репозитория

```
docker/
  vllm/Dockerfile              образ vLLM (rootless, multi-stage)
  vllm/requirements.in         прямые зависимости
  vllm/requirements.txt        lock-файл: все пакеты с версиями и sha256
  vllm/entrypoint.sh           запуск vllm serve из локальной копии весов
  vllm/serve.yaml              параметры vLLM по умолчанию
  downloader/Dockerfile        лёгкий образ для initContainer
  downloader/requirements.txt  lock-файл huggingface_hub
plugins/vllm_private_logging/  vLLM-плагин приватного логирования + unit-тесты
patches/                       бэкпорт исправления vLLM 0.32.0 для aarch64
scripts/download_model.py      скрипт скачивания весов в PVC
k8s/base/                      манифесты: Namespace, PVC, Deployment, Service, ConfigMap
k8s/overlays/minikube/         overlay с Secret (secretGenerator)
loadtest/                      нагрузочный тест
```

## Как это устроено

```
Pod vllm
├── initContainer model-downloader (vllm-model-downloader:1.0.0)
│     MODEL_ID, MODEL_REVISION ← ConfigMap vllm-model
│     HF_TOKEN                 ← Secret vllm-secrets (hf-token, optional)
│     /models                  ← PVC vllm-models
│     если /models/<model>/.download-complete совпадает с MODEL_ID/MODEL_REVISION → выход без сети,
│     иначе snapshot_download → запись маркера
└── container vllm (vllm-cpu-private:0.32.0)
      HF_HUB_OFFLINE=1, TRANSFORMERS_OFFLINE=1
      VLLM_API_KEY             ← Secret vllm-secrets (api-key)
      /etc/vllm/serve.yaml     ← ConfigMap vllm-serve-config
      /models (read-only)      ← PVC vllm-models
      vllm serve /models/<model> --config /etc/vllm/serve.yaml
```

### Docker-образ vLLM

- База — `python:3.12-slim-trixie`, закреплённая по digest. Используется официальный CPU-wheel vLLM `0.32.0+cpu` из GitHub Releases (x86_64 и aarch64), torch `2.13.0+cpu`.
- `requirements.txt` сгенерирован `uv pip compile --universal --generate-hashes`: в нём зафиксированы все 155 пакетов с хешами для x86_64 и aarch64. Установка идёт через `pip install --require-hashes --no-deps`, поэтому неучтённый пакет установиться не сможет.
- Multi-stage: в runtime-слой попадает только готовый venv. Там же стоит `g++`: CPU-бэкенд vLLM компилирует граф через `torch.compile`/Inductor.
- Пользователь `vllm` (UID/GID 10001), `USER 10001:10001`. В Kubernetes дополнительно: `runAsNonRoot`, `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`.
- Отключена телеметрия: `VLLM_NO_USAGE_STATS`, `DO_NOT_TRACK`, `HF_HUB_DISABLE_TELEMETRY`.

### Приватное логирование

Стандартный `RequestLogger` vLLM пишет в лог текст промпта (на DEBUG), параметры сэмплинга (на INFO; туда входят stop-строки и структура JSON-схем) и, при `--enable-log-outputs`, сгенерированный ответ.

Решение — **vLLM general plugin** `vllm_private_logging`. Это штатный механизм расширения vLLM через entry point `vllm.general_plugins`: плагин загружается в каждом процессе vLLM (API-сервер, EngineCore, воркеры).

- `RequestLogger.log_inputs` пишет только метаданные: `request_id`, длину промпта в символах и токенах, `max_tokens`, `temperature`, `top_p`, `top_k`, `seed` и имя LoRA-адаптера;
- `RequestLogger.log_outputs` пишет `request_id`, число сгенерированных токенов и `finish_reason`; потоковые дельты не логируются;
- `RequestLogger.log_request_body` отключён;
- на логгер `vllm.entrypoints.serve.utils.request_logger` повешен фильтр-allowlist: проходят только записи, помеченные плагином. Если в будущей версии vLLM появится новая запись с содержимым запроса, она будет отброшена;
- fail-closed: если у `RequestLogger` нет нужного метода (изменился API vLLM), плагин бросает исключение и сервер не стартует; `entrypoint.sh` перед запуском проверяет, что плагин установлен и применяется (`python -m vllm_private_logging.check`);
- дополнительно в конфиге стоит `max-log-len: 0`.

Факт обращения к модели логируется: включены `enable-log-requests` и `enable-log-outputs` (с метаданными вместо текста), плюс access-лог uvicorn.

Пример реального лога из пода (промпт содержал строку `SECRET-PROMPT-7731`, её в логах нет):

```
(APIServer pid=1) INFO 10-28 16:00:58 [patch.py:48] Received request chatcmpl-9d6821a90f03bd2a: prompt_chars=218 prompt_tokens=51 prompt_embeds=False params=SamplingParams n=1 max_tokens=16 temperature=0.7 top_p=0.8 top_k=20 lora=None
(APIServer pid=1) INFO 10-28 16:00:58 [async_llm.py:578] Added request chatcmpl-9d6821a90f03bd2a-b34eb0fc.
(APIServer pid=1) INFO 10-28 16:00:59 [patch.py:74] Generated response chatcmpl-9d6821a90f03bd2a: output_tokens=2 finish_reason=stop streaming=False
INFO:     127.0.0.1:52834 - "POST /v1/chat/completions HTTP/1.1" 200 OK
```

Проверено в поде на `/v1/chat/completions` (обычный и стриминговый режим) и `/v1/completions`: после трёх запросов с маркерами `SECRET-*` число совпадений `kubectl logs ... | grep -c SECRET` равно 0.

Плагин покрыт unit-тестами (`plugins/vllm_private_logging/tests`), они запускаются на этапе сборки образа: если тесты падают, образ не собирается.

### Кеширование весов в PVC

`scripts/download_model.py`:

1. Берёт файловую блокировку `/models/.<model>.lock` (защита от параллельных загрузок).
2. Если есть маркер `/models/<org>--<name>/.download-complete` с тем же `MODEL_ID` и `MODEL_REVISION` (ветка или commit), загрузка пропускается **без единого сетевого запроса**.
3. Иначе определяет commit SHA ревизии, скачивает только нужные файлы (`*.json`, `*.safetensors`, токенизатор, шаблон чата) и атомарно записывает маркер с commit, размером и временем загрузки.

Основной контейнер работает с `HF_HUB_OFFLINE=1`, монтирует PVC только на чтение и отказывается стартовать, если маркера нет.

### Секреты

`k8s/overlays/minikube/secrets.env` (в `.gitignore`), из него `secretGenerator` создаёт Secret `vllm-secrets`:

| ключ | назначение | куда попадает |
|---|---|---|
| `api-key` | Bearer-токен для клиентов API | `VLLM_API_KEY` контейнера vllm |
| `hf-token` | токен HF для gated/private-моделей (может быть пустым) | `HF_TOKEN` initContainer |

Каждый токен виден только тому контейнеру, которому нужен. API-ключ передаётся через переменную окружения, а не аргументом CLI, поэтому не светится в `ps`. Эндпоинты `/v1/*` без токена отвечают `401`, а `/health` и `/metrics` открыты для проб.

### Конфигурация через ConfigMap

Генерируются через kustomize `configMapGenerator`:

- `vllm-model` (из `k8s/base/config/model.env`): `MODEL_ID`, `MODEL_REVISION`, `SERVED_MODEL_NAME`, `VLLM_CPU_OMP_THREADS_BIND` (ядра для OpenMP-потоков), сюда же можно добавить любые `VLLM_*`-переменные;
- `vllm-serve-config` (из `k8s/base/config/serve.yaml`): нативный YAML-конфиг `vllm serve --config` с любыми аргументами CLI (`dtype`, `max-model-len`, `max-num-seqs`, `max-num-batched-tokens`, `kv-cache-memory-bytes`, …).

К имени ConfigMap kustomize добавляет хеш содержимого, поэтому изменение конфига после `kubectl apply -k` автоматически выкатывает новый под. Пересобирать образ не нужно.

## Локальный запуск (Minikube)

Требования: Docker, minikube, kubectl. Для модели 0.5B нужно около 5 ГБ памяти для minikube (Docker Desktop ≥ 6 ГБ).

```bash
minikube start --driver=docker --cpus=6 --memory=5120
```

```bash
docker build -f docker/vllm/Dockerfile -t vllm-cpu-private:0.32.0 .
docker build -f docker/downloader/Dockerfile -t vllm-model-downloader:1.0.0 .
minikube image load vllm-cpu-private:0.32.0
minikube image load vllm-model-downloader:1.0.0
```

Можно собирать сразу внутри minikube: `eval $(minikube docker-env)`, затем те же `docker build`.

```bash
cp k8s/overlays/minikube/secrets.env.example k8s/overlays/minikube/secrets.env
sed -i '' "s/^api-key=.*/api-key=$(openssl rand -hex 24)/" k8s/overlays/minikube/secrets.env
```

(на Linux — `sed -i` без `''`; для gated-моделей заполните `hf-token=`)

```bash
kubectl apply -k k8s/overlays/minikube
kubectl -n llm-inference get pods -w
kubectl -n llm-inference logs deploy/vllm -c model-downloader
kubectl -n llm-inference logs deploy/vllm -c vllm -f
```

Первый старт: initContainer скачивает около 1 ГБ, затем vLLM компилирует граф (1–2 минуты на CPU). Проверка:

```bash
kubectl -n llm-inference port-forward svc/vllm 8000:8000
export VLLM_API_KEY=$(grep '^api-key=' k8s/overlays/minikube/secrets.env | cut -d= -f2)
curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $VLLM_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-0.5b-instruct","messages":[{"role":"user","content":"Hello!"}],"max_tokens":32}'
```

Проверка кеша: `kubectl -n llm-inference rollout restart deploy/vllm` — в логах initContainer будет `found in /models/..., skipping download`.

Смена модели или параметров: отредактируйте `k8s/base/config/model.env` или `serve.yaml` и снова выполните `kubectl apply -k k8s/overlays/minikube`.

Удаление: `kubectl delete -k k8s/overlays/minikube` (PVC с весами удаляется вместе с namespace).

### Запуск без Kubernetes

```bash
docker volume create models
docker run --rm -e MODEL_ID=Qwen/Qwen2.5-0.5B-Instruct -v models:/models vllm-model-downloader:1.0.0
docker run --rm -p 8000:8000 --read-only --tmpfs /tmp:exec --tmpfs /home/vllm:exec,uid=10001,gid=10001 \
  --shm-size 512m --cap-drop ALL -e MODEL_ID=Qwen/Qwen2.5-0.5B-Instruct -e SERVED_MODEL_NAME=qwen2.5-0.5b-instruct \
  -e VLLM_API_KEY=change-me -v models:/models:ro vllm-cpu-private:0.32.0
```

### Обновление зависимостей

```bash
docker run --rm -v "$PWD/docker:/w" -w /w ghcr.io/astral-sh/uv:python3.12-trixie-slim \
  uv pip compile vllm/requirements.in --universal --python-version 3.12 \
  --index-strategy unsafe-best-match --generate-hashes --emit-index-url --no-header --no-annotate \
  -o vllm/requirements.txt
```

## Замечания по платформе

- **aarch64 (Apple Silicon, Graviton).** В vLLM 0.32.0 сигнатура CPU-фолбэка `_compute_slot_mapping_kernel_impl` не совпадает с Triton-ядром `ComputeSlotMappingKernel.kernel`. Фолбэк используется, когда Triton недоступен (так на aarch64), и первый же запрос роняет EngineCore с ошибкой `Boolean value of Tensor with more than one value is ambiguous`. В upstream (`main`) это уже исправлено; `patches/vllm-0.32.0-cpu-slot-mapping-signature.patch` — бэкпорт этого исправления, он применяется при сборке. На x86_64 используется triton-cpu, и проблема не проявляется.
- **dtype.** В конфиге `float16`: на Apple M1 нет аппаратного bf16, и прогрев с `bfloat16` идёт в десятки раз медленнее. На x86 с AVX512-BF16/AMX стоит поставить `bfloat16`.
- **Потоки.** `VLLM_CPU_OMP_THREADS_BIND=0-3` соответствует лимиту в 4 CPU. Для продакшена — Guaranteed QoS и static CPU manager policy, тогда под получает эксклюзивные ядра.

## Нагрузочное тестирование (бонус 3)

`loadtest/loadtest.py` — асинхронный клиент на aiohttp со стримингом через OpenAI-совместимый `/v1/chat/completions`. Для каждого запроса он замеряет:

- **TTFT** — время до первого токена контента;
- **Latency** — полное время запроса;
- **TPOT** — `(latency − TTFT) / (output_tokens − 1)`;
- **Throughput** — запросы/с, выходные токены/с и все токены/с (prompt + output) за время прогона уровня.

Длина ответа фиксирована (`ignore_eos: true`, `max_tokens`), реальные числа токенов берутся из `usage` (`stream_options.include_usage`). Сценарии: `short` (~72 токена промпта с учётом чат-шаблона) и `long` (~1576 токенов), уровни параллелизма 1/4/8.

```bash
python3 -m venv .venv && .venv/bin/pip install --require-hashes -r loadtest/requirements.txt
kubectl -n llm-inference port-forward svc/vllm 8000:8000 &
VLLM_API_KEY=... .venv/bin/python loadtest/loadtest.py --requests 16 --max-tokens 64 --concurrency 1,4,8
```

Альтернатива — встроенный `vllm bench serve` (`--dataset-name random --random-input-len ... --random-output-len ...`).

### Результаты

Стенд: Apple M1 (8 ядер, 8 ГБ RAM), Docker Desktop 6 ГБ, minikube 6 CPU / 5 ГБ, под vLLM с лимитом 4 CPU / 4.5 ГиБ, `Qwen2.5-0.5B-Instruct` float16, KV-кеш 512 МиБ, `max-num-seqs: 16`, `max-num-batched-tokens: 4096`. На каждую точку 16 запросов, ответ ровно 64 токена, запросы идут через `kubectl port-forward`. Сырые данные — в `loadtest/results.json`.

| Prompt | Concurrency | Requests | Errors | Prompt tok | Output tok | TTFT avg, ms | TTFT p95, ms | TPOT avg, ms | Latency avg, s | Latency p95, s | Req/s | Output tok/s | Total tok/s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| short | 1 | 16 | 0 | 72 | 64 | 655 | 1091 | 82.3 | 5.84 | 8.68 | 0.17 | 11.0 | 23.3 |
| short | 4 | 16 | 0 | 72 | 64 | 1733 | 2651 | 114.8 | 8.96 | 10.61 | 0.45 | 28.5 | 60.7 |
| short | 8 | 16 | 0 | 72 | 64 | 2395 | 2991 | 134.9 | 10.89 | 11.39 | 0.73 | 46.9 | 99.7 |
| long | 1 | 16 | 0 | 1576 | 64 | 7430 | 8141 | 64.5 | 11.50 | 13.60 | 0.09 | 5.6 | 142.6 |
| long | 4 | 16 | 0 | 1576 | 64 | 19443 | 28571 | 260.6 | 35.86 | 39.08 | 0.11 | 7.1 | 182.8 |
| long | 8 | 16 | 0 | 1576 | 64 | 34012 | 58807 | 519.1 | 66.71 | 89.21 | 0.12 | 7.7 | 196.5 |

Наблюдения:

- **Короткие промпты** упираются в decode. При росте параллелизма с 1 до 8 пропускная способность по выходным токенам растёт в 4,3 раза (11 → 47 ток/с), а TPOT ухудшается лишь в 1,6 раза (82 → 135 мс): батчинг decode на CPU работает эффективно.
- **Длинные промпты** (~1,6 тыс. токенов) упираются в prefill: около 150–200 токенов/с суммарно, и параллелизм почти не повышает throughput (5,6 → 7,7 выходных ток/с). TTFT растёт почти линейно с длиной очереди (7,4 → 34 с), а TPOT — до 0,5 с, потому что чанки prefill новых запросов вклиниваются в шаги decode уже идущих.


### Как поднять throughput

**А) Большой поток коротких запросов** (prompt и output по десяткам токенов). Узкое место — decode: каждый шаг генерирует по одному токену на последовательность, и выгоднее обрабатывать как можно больше последовательностей за шаг.

- `max-num-seqs` ↑ (например, 16 → 64–256): больше одновременных последовательностей в батче decode, матричные операции лучше нагружают CPU;
- `kv-cache-memory-bytes` / `VLLM_CPU_KVCACHE_SPACE` ↑: KV-кеш должен вмещать `max-num-seqs × (prompt + output)` токенов, иначе запросы вытесняются (preemption);
- `max-model-len` ↓ до реально нужного (например, 1024): меньше резервирование и больше параллельных запросов при том же кеше;
- `enable-prefix-caching` (включён по умолчанию): общий системный промпт считается один раз;
- `max-num-batched-tokens` умеренный (2048–4096): при чанкованном prefill новые короткие запросы подмешиваются в шаг decode, не останавливая генерацию;
- масштабирование вширь: несколько реплик или `data-parallel-size` по NUMA-нодам, `VLLM_CPU_OMP_THREADS_BIND` на физические ядра без SMT, отдельные ядра под API-сервер (`VLLM_CPU_NUM_OF_RESERVED_CPU`);
- `dtype: bfloat16` на CPU с AVX512-BF16/AMX и квантизация весов (W8A8/INT8 через compressed-tensors, AWQ) — decode на CPU упирается в пропускную способность памяти, и чем меньше весов, тем быстрее шаг.

**Б) Мало запросов, но огромный контекст.** Узкое место — prefill (вычисления O(n²) по вниманию и большой объём KV-кеша).

- `max-model-len` ↑ под нужный контекст, `kv-cache-memory-bytes` ↑ так, чтобы влез хотя бы один полный контекст на каждую одновременную последовательность;
- `max-num-seqs` ↓ (1–4): несколько длинных контекстов не дробят KV-кеш и не вытесняют друг друга;
- `enable-chunked-prefill` (по умолчанию) + `max-num-batched-tokens` ↑ (8192–16384): длинный prefill идёт крупными чанками, лучше используется вычислительная мощность (в ущерб TTFT соседних запросов);
- `enable-prefix-caching`: при повторном использовании одного большого документа (RAG, диалог) повторный prefill не нужен;
- `kv-cache-dtype: fp8` (если поддерживается CPU-бэкендом) — вдвое больше токенов в том же объёме памяти;
- больше ядер на один под (prefill хорошо масштабируется по потокам), а при нескольких NUMA-нодах — `tensor-parallel-size` по нодам, чтобы KV-кеш и веса были локальны к памяти.
