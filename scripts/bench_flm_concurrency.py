"""FLM / OpenAI 兼容服务的并发压测：串行 N 次 vs 同时 N 次，同口径比较。

只依赖标准库，避免给压测引入额外环境问题。
用法：python scripts/bench_flm_concurrency.py <port> [concurrency] [model]
"""
import json
import sys
import threading
import time
import http.client


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 52625
N = int(sys.argv[2]) if len(sys.argv) > 2 else 3
MODEL = sys.argv[3] if len(sys.argv) > 3 else "qwen3.5:2b"
PROMPT = "请用三句话介绍杭州，不要用列表。"
MAX_TOKENS = 60


def one_request(tag: str, barrier=None) -> dict:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": True,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
    }).encode("utf-8")

    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=180)
    t0 = time.perf_counter()
    conn.request("POST", "/v1/chat/completions", body=body,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    ttft = None
    chunks = 0
    text = []

    while True:
        line = resp.readline()
        if not line:
            break
        line = line.strip()
        if not line or not line.startswith(b"data:"):
            continue
        payload = line[5:].strip()
        if payload == b"[DONE]":
            break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        delta = (data.get("choices") or [{}])[0].get("delta") or {}
        piece = delta.get("content") or ""
        if piece:
            if ttft is None:
                ttft = time.perf_counter() - t0
            chunks += 1
            text.append(piece)

    total = time.perf_counter() - t0
    conn.close()
    return {
        "tag": tag,
        "ttft": round(ttft, 3) if ttft is not None else None,
        "total": round(total, 3),
        "chars": len("".join(text)),
        "chunks": chunks,
    }


def run_sequential() -> list:
    return [one_request(f"seq-{i + 1}") for i in range(N)]


def run_concurrent() -> list:
    results = [None] * N
    barrier = threading.Barrier(N)

    def worker(i):
        barrier.wait()
        results[i] = one_request(f"par-{i + 1}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def main():
    print(f"model={MODEL} port={PORT} n={N} max_tokens={MAX_TOKENS}", flush=True)
    seq = run_sequential()
    print("SEQUENTIAL", json.dumps(seq, ensure_ascii=False), flush=True)

    # 给 NPU 一点冷却时间，避免上一轮残留影响
    time.sleep(2)

    t0 = time.perf_counter()
    par = run_concurrent()
    wall = time.perf_counter() - t0
    print("CONCURRENT", json.dumps(par, ensure_ascii=False), flush=True)

    seq_wall = sum(r["total"] for r in seq)
    print(json.dumps({
        "seq_wall": round(seq_wall, 3),
        "par_wall": round(wall, 3),
        "speedup": round(seq_wall / wall, 2) if wall else None,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
