import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import aiohttp

WORDS = (
    "system network kernel packet memory thread socket buffer cache index "
    "vector model token layer tensor batch stream queue latency signal "
    "storage cluster replica shard policy access secure audit metric event"
).split()

SCENARIOS = {
    "short": 32,
    "long": 1536,
}


@dataclass
class RequestResult:
    ok: bool
    ttft: float = 0.0
    latency: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    error: str = ""


@dataclass
class Summary:
    scenario: str
    concurrency: int
    requests: int
    errors: int
    prompt_tokens_avg: float
    output_tokens_avg: float
    ttft_avg_ms: float
    ttft_p50_ms: float
    ttft_p95_ms: float
    tpot_avg_ms: float
    latency_avg_s: float
    latency_p50_s: float
    latency_p95_s: float
    request_throughput: float
    output_throughput: float
    total_throughput: float
    duration_s: float
    extra: dict = field(default_factory=dict)


def make_prompt(approx_words: int, rng: random.Random) -> str:
    body = " ".join(rng.choice(WORDS) for _ in range(approx_words))
    return f"Summarize the following log fragment in one sentence: {body}"


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


async def send_request(
    session: aiohttp.ClientSession,
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
) -> RequestResult:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "ignore_eos": True,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.perf_counter()
    ttft = None
    usage = {}
    try:
        async with session.post(url, json=payload) as response:
            if response.status != 200:
                return RequestResult(ok=False, error=f"HTTP {response.status}: {await response.text()}")
            async for raw in response.content:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if ttft is None and choices and choices[0].get("delta", {}).get("content"):
                    ttft = time.perf_counter() - started
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return RequestResult(ok=False, error=repr(exc))
    latency = time.perf_counter() - started
    return RequestResult(
        ok=True,
        ttft=ttft if ttft is not None else latency,
        latency=latency,
        prompt_tokens=usage.get("prompt_tokens", 0),
        output_tokens=usage.get("completion_tokens", 0),
    )


async def run_level(
    url: str,
    headers: dict,
    model: str,
    prompts: list[str],
    concurrency: int,
    max_tokens: int,
    timeout: float,
) -> tuple[list[RequestResult], float]:
    semaphore = asyncio.Semaphore(concurrency)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    connector = aiohttp.TCPConnector(limit=concurrency)
    async with aiohttp.ClientSession(headers=headers, timeout=client_timeout, connector=connector) as session:

        async def bounded(prompt: str) -> RequestResult:
            async with semaphore:
                return await send_request(session, url, model, prompt, max_tokens)

        started = time.perf_counter()
        results = await asyncio.gather(*(bounded(p) for p in prompts))
        return list(results), time.perf_counter() - started


def summarize(scenario: str, concurrency: int, results: list[RequestResult], duration: float) -> Summary:
    ok = [r for r in results if r.ok]
    ttfts = [r.ttft * 1000 for r in ok]
    latencies = [r.latency for r in ok]
    tpots = [
        (r.latency - r.ttft) * 1000 / (r.output_tokens - 1)
        for r in ok
        if r.output_tokens > 1
    ]
    output_tokens = sum(r.output_tokens for r in ok)
    prompt_tokens = sum(r.prompt_tokens for r in ok)
    errors = [r.error for r in results if not r.ok]
    return Summary(
        scenario=scenario,
        concurrency=concurrency,
        requests=len(results),
        errors=len(errors),
        prompt_tokens_avg=prompt_tokens / len(ok) if ok else 0.0,
        output_tokens_avg=output_tokens / len(ok) if ok else 0.0,
        ttft_avg_ms=statistics.fmean(ttfts) if ttfts else 0.0,
        ttft_p50_ms=percentile(ttfts, 0.5),
        ttft_p95_ms=percentile(ttfts, 0.95),
        tpot_avg_ms=statistics.fmean(tpots) if tpots else 0.0,
        latency_avg_s=statistics.fmean(latencies) if latencies else 0.0,
        latency_p50_s=percentile(latencies, 0.5),
        latency_p95_s=percentile(latencies, 0.95),
        request_throughput=len(ok) / duration,
        output_throughput=output_tokens / duration,
        total_throughput=(output_tokens + prompt_tokens) / duration,
        duration_s=duration,
        extra={"first_errors": errors[:3]},
    )


def markdown_table(summaries: list[Summary]) -> str:
    header = (
        "| Prompt | Concurrency | Requests | Errors | Prompt tok | Output tok | "
        "TTFT avg, ms | TTFT p95, ms | TPOT avg, ms | Latency avg, s | Latency p95, s | "
        "Req/s | Output tok/s | Total tok/s |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    rows = [
        f"| {s.scenario} | {s.concurrency} | {s.requests} | {s.errors} | "
        f"{s.prompt_tokens_avg:.0f} | {s.output_tokens_avg:.0f} | "
        f"{s.ttft_avg_ms:.0f} | {s.ttft_p95_ms:.0f} | {s.tpot_avg_ms:.1f} | "
        f"{s.latency_avg_s:.2f} | {s.latency_p95_s:.2f} | "
        f"{s.request_throughput:.2f} | {s.output_throughput:.1f} | {s.total_throughput:.1f} |"
        for s in summaries
    ]
    return header + "\n".join(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load test for an OpenAI-compatible vLLM endpoint")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--model", default="qwen2.5-0.5b-instruct")
    parser.add_argument("--api-key", default=os.environ.get("VLLM_API_KEY", ""))
    parser.add_argument("--scenarios", default="short,long")
    parser.add_argument("--concurrency", default="1,4,8")
    parser.add_argument("--requests", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=Path("results.json"))
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    if not args.api_key:
        print("API key is required: pass --api-key or set VLLM_API_KEY", file=sys.stderr)
        return 2
    url = f"{args.base_url.rstrip('/')}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {args.api_key}"}
    rng = random.Random(args.seed)

    warmup, _ = await run_level(url, headers, args.model, [make_prompt(8, rng)], 1, 8, args.timeout)
    if not warmup[0].ok:
        print(f"Warmup failed: {warmup[0].error}", file=sys.stderr)
        return 1

    summaries = []
    for scenario in args.scenarios.split(","):
        words = SCENARIOS[scenario]
        for concurrency in (int(c) for c in args.concurrency.split(",")):
            prompts = [make_prompt(words, rng) for _ in range(args.requests)]
            results, duration = await run_level(
                url, headers, args.model, prompts, concurrency, args.max_tokens, args.timeout
            )
            summary = summarize(scenario, concurrency, results, duration)
            summaries.append(summary)
            print(
                f"{scenario:>5} c={concurrency:<3} ttft={summary.ttft_avg_ms:.0f}ms "
                f"latency={summary.latency_avg_s:.2f}s out_tok/s={summary.output_throughput:.1f} "
                f"errors={summary.errors}",
                flush=True,
            )

    args.output.write_text(json.dumps([asdict(s) for s in summaries], indent=2))
    print()
    print(markdown_table(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
