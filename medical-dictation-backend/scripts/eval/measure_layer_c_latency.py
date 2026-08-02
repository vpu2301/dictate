#!/usr/bin/env python3
"""Sprint-15 Layer C latency measurement — Gemma short-completion via Ollama.

Measures end-to-end latency of the exact inference call the inline-completion
endpoint makes: ~200-token clinical Ukrainian context, <= 24 generated tokens,
greedy decoding. Two modes per run:

  alone      — sequential requests against an idle server
  contended  — the same requests while one long (512-token) generation runs
               concurrently, emulating a section-synthesis job sharing the model

The contended p95 is the number that decides the architecture (budget: p95 <=
400 ms end-to-end, hard timeout 600 ms). Run with OLLAMA_NUM_PARALLEL=2 so the
server mirrors the service's 2-slot pool.

Two backends, same seam the service's InferenceClient abstracts:

  ollama    — POST /api/generate (Ollama applies the gemma3 chat template)
  llamacpp  — POST /completion on llama-server; the script applies the gemma3
              template itself (raw completion without it degenerates into loops)

Standalone by design: stdlib + httpx only, no repo imports, no CI coupling.

  uv run python scripts/eval/measure_layer_c_latency.py --model gemma3:1b --runs 50
  uv run python scripts/eval/measure_layer_c_latency.py --backend llamacpp \
      --base-url http://localhost:8089 --runs 50 --concurrent-long
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time

import httpx

# ~200 tokens of clinical register context: system frame + section + prefix,
# the same shape generation_service/domain/prompt.py produces.
SYSTEM_FRAME = (
    "Ти — асистент лікаря, який продовжує речення у клінічному документі. "
    "Продовжуй поточне речення природною українською медичною мовою, у "
    "клінічному стилі. Не вигадуй жодних нових клінічних фактів: жодних "
    "числових показників, дозувань, діагнозів чи кодів, яких немає у "
    "тексті. Заверши лише граматичну структуру речення. Відповідай тільки "
    "продовженням речення, без пояснень.\n\n"
    "Розділ документа: Анамнез захворювання (текстове поле)\n\n"
)

CONTEXTS = [
    "Пацієнт скаржиться на біль у",
    "Захворів гостро три дні тому, коли вперше з'явився",
    "З анамнезу відомо, що пацієнтка тривалий час страждає на",
    "Об'єктивно: загальний стан задовільний, шкірні покриви",
    "Хворий відзначає погіршення самопочуття після",
    "Біль посилюється при фізичному навантаженні та",
    "Під час аускультації легень вислуховується",
    "Живіт м'який, безболісний при пальпації у",
]

LONG_PROMPT = (
    "Напиши докладний приклад структурованого медичного висновку українською "
    "мовою для пацієнта з хронічним обструктивним захворюванням легень: "
    "анамнез, об'єктивний стан, обстеження, рекомендації."
)


GEMMA_TURN = "<start_of_turn>user\n{prompt}<end_of_turn>\n<start_of_turn>model\n"
STOP = ["<end_of_turn>", "\n\n"]


async def _generate(
    client: httpx.AsyncClient,
    backend: str,
    model: str,
    prompt: str,
    num_predict: int,
) -> dict:
    t0 = time.perf_counter()
    if backend == "ollama":
        resp = await client.post(
            "/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": num_predict, "temperature": 0, "stop": STOP},
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        body = resp.json()
        text = body.get("response", "")
    else:  # llamacpp — llama-server native /completion, template applied here
        resp = await client.post(
            "/completion",
            json={
                "prompt": GEMMA_TURN.format(prompt=prompt),
                "n_predict": num_predict,
                "temperature": 0,
                "stop": STOP,
                "cache_prompt": True,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
        body = resp.json()
        text = body.get("content", "")
    return {
        "wall_ms": (time.perf_counter() - t0) * 1000.0,
        "response": text,
    }


def _stats(samples: list[float]) -> dict:
    ordered = sorted(samples)
    n = len(ordered)
    return {
        "n": n,
        "p50_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(ordered[max(0, int(n * 0.95) - 1)], 1),
        "max_ms": round(ordered[-1], 1),
        "mean_ms": round(statistics.fmean(ordered), 1),
    }


async def _run_batch(
    client: httpx.AsyncClient, backend: str, model: str, runs: int, num_predict: int
) -> list[float]:
    samples: list[float] = []
    for i in range(runs):
        prompt = SYSTEM_FRAME + CONTEXTS[i % len(CONTEXTS)]
        result = await _generate(client, backend, model, prompt, num_predict)
        samples.append(result["wall_ms"])
    return samples


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["ollama", "llamacpp"], default="ollama")
    parser.add_argument("--model", default="gemma3:1b")
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--num-predict", type=int, default=24)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument(
        "--concurrent-long",
        action="store_true",
        help="also measure while a 512-token generation runs in parallel",
    )
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.base_url) as client:
        # Warmup: load the model into memory (excluded from stats).
        warm = await _generate(
            client, args.backend, args.model, SYSTEM_FRAME + CONTEXTS[0], args.num_predict
        )
        report: dict = {
            "backend": args.backend,
            "model": args.model,
            "num_predict": args.num_predict,
            "runs": args.runs,
            "warmup_ms": round(warm["wall_ms"], 1),
            "sample_completion": warm["response"][:120],
        }

        alone = await _run_batch(client, args.backend, args.model, args.runs, args.num_predict)
        report["alone"] = _stats(alone)

        if args.concurrent_long:
            async def _long_loop(stop: asyncio.Event) -> int:
                count = 0
                while not stop.is_set():
                    await _generate(client, args.backend, args.model, LONG_PROMPT, 512)
                    count += 1
                return count

            stop = asyncio.Event()
            long_task = asyncio.create_task(_long_loop(stop))
            try:
                contended = await _run_batch(
                    client, args.backend, args.model, args.runs, args.num_predict
                )
            finally:
                stop.set()
            report["contended"] = _stats(contended)
            report["long_generations_completed"] = await long_task

        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
