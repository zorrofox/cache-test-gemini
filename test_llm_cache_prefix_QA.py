#!/usr/bin/env python3
"""公共前缀 Prompt Cache 多模型对比测试 (OpenAI Prompt Caching 文档问答).

每次请求独立 (system + user), 共享长文档前缀, 仅末尾问题不同.
用 usage.cached_tokens 观察跨请求公共前缀缓存命中率.

默认同目录布局:
    ./test_llm_cache_prefix_QA.py
    ./openai_prompt_caching.md
    ./output/<run_id>/

用法:
    uv run python scripts/test_llm_cache_prefix_QA.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_PRINT_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()

# =============================================================================
# 配置 (路径 / 模型 / 运行参数 — 改这里即可)
# =============================================================================

WORK_DIR = Path(__file__).resolve().parent
DOC_PATH = Path("openai_prompt_caching.md")  # 相对 WORK_DIR
OUTPUT_ROOT = Path("output")  # 相对 WORK_DIR

# endpoint 用通用 chat/completions; model 写入请求体
MODELS: list[dict[str, str]] = [
    {
        "name": "google-gemini-3_5-flash",
        "endpoint": "http://xxxxxxxx",
        "token": "xxxxxxxxxxx",
        "model": "google-gemini-3_5-flash",
    },
    {
        "name": "azure-gpt-5_6-luna",
        "endpoint": "http://xxxxxxxx",
        "token": "xxxxxxxxxxx",
        "model": "azure-gpt-5_6-luna",
    },
]

MAX_TOKENS = 8192
REQUEST_TIMEOUT = 600
SSL_VERIFY = False
PARALLEL = True
MAX_WORKERS = 0  # 0 = 与模型数相同
REQUEST_GAP_SEC = 10.0  # 请求间隔, 给服务端建前缀缓存的时间
INCLUDE_BREAK_CONTROL = True  # 末尾追加 1 次改写 system 的打断对照
STICKY_KEY = "decision-autolabel-llm"

# =============================================================================
# Prompt (system 固定 + 文档前缀固定 + 末尾问题变化)
# =============================================================================

_SYSTEM_MARKER = "OpenAI Prompt Caching 文档问答助手"

SYSTEM_PROMPT = f"""
你是{_SYSTEM_MARKER}, 专门依据用户消息中提供的官方指南原文回答技术问题.
你的输出用于人工抽检与缓存机制理解核对, 因此宁可不完整也不可编造.

# 输入约定
- 用户会先给出一份较长的共享参考文档 (OpenAI Prompt Caching, GPT-5.6+), 再提出本轮独立问题.
- 你必须严格依据该文档内容作答; 文档未写明的内容请明确写「文档未说明」, 不要用外部常识补全.
- 若问题与文档表述有冲突, 以文档原文为准, 并指出冲突点.

# 回答维度 (按需覆盖, 避免空泛套话)
1. 机制: 缓存写/读、隐式/显式 breakpoint、prefix 精确匹配
2. 路由: prompt_cache_key、前缀 hash、命中率与 RPM 建议
3. 计费与计量: cache_write_tokens、cached_tokens、1.25× 写缓存费率
4. 门槛与保留: 最低 token 数、ttl、可缓存对象类型
5. API 差异: Responses API vs Chat Completions API 的 breakpoint 放置位置
6. 最佳实践与 FAQ: 文档 Best practices / Frequently asked questions 中的结论

# 表述纪律
- 使用中文作答, 关键术语可保留英文原文 (如 prompt_cache_key、cached_tokens).
- 优先引用文档中的具体数字、字段名、模式名 (implicit/explicit、30m、1024 tokens 等).
- 需要对比时用条目列出; 不确定就写不确定, 并说明文档哪一段信息不足.
- 不要编造文档中不存在的 API 字段、价格倍数或 TTL 取值.

# 输出质量自检
回答前自检: 是否紧扣本轮问题? 是否区分了「文档明确写出」与「文档未覆盖」?
是否避免把隐式缓存与显式 breakpoint 混为一谈? 是否把关键数字/字段名写对?
""".strip()

_DOC_FILE = WORK_DIR / DOC_PATH if not DOC_PATH.is_absolute() else DOC_PATH
if not _DOC_FILE.is_file():
    raise FileNotFoundError(f"参考文档不存在: {_DOC_FILE}")
_DOC_TEXT = _DOC_FILE.read_text(encoding="utf-8").strip()

SHARED_TEXT_PREFIX = f"""
【共享参考文档 · 公共前缀】
来源文件: {DOC_PATH}
主题: OpenAI Prompt Caching (GPT-5.6+)
用途: 后续各轮独立问题均基于下文作答; 请把下文视为唯一知识来源.

# 阅读约定
1. 回答只能依据下方文档正文与 FAQ; 不要引入文档外知识.
2. 涉及字段名、模式名、数字门槛时, 尽量与原文保持一致.
3. 若本轮问题要求举例, 优先复述文档中的 Responses / Chat Completions 示例语义, 而不是凭空编造新 API.
4. 若本轮问题要求对比, 明确写出对比维度 (例如 implicit vs explicit、cache write vs cache read).

--- BEGIN DOCUMENT: {DOC_PATH.name} ---
{_DOC_TEXT}
--- END DOCUMENT ---

# 文档结构速览 (便于定位, 非额外事实源)
- Caching behavior: 隐式 breakpoint 落在最新 user/tool message; 变化后缀会破坏整段前缀命中
- Structuring prompts: 静态内容靠前, 动态内容靠后; 图像与 tools 也必须完全一致
- How it works: Cache Routing / Lookup / Hit / Miss; prompt_cache_key 提升路由亲和
- Prompt cache breakpoints: prompt_cache_options.mode = implicit|explicit; 每请求最多 4 次新 cache write
- Prompt cache retention: ttl 仅支持 30m; prompt_cache_retention 已弃用
- Requirements: 前缀至少 1024 tokens; usage 中看 cached_tokens / cache_write_tokens
- What can be cached: messages / images / tools / structured outputs
- Best practices: 静态前缀、稳定 key、显式 breakpoint、监控 usage、稳态流量
- FAQ: 隐私隔离、不影响最终生成语义保证、不可手动清缓存、写缓存加价、仍计入 TPM

# 下列内容为本轮独立问题 (公共前缀到此结束)
""".strip()

SUFFIX_QUESTIONS: list[str] = [
    """【问题A · 缓存行为与隐式 breakpoint】
根据文档 Caching behavior 一节:
1) 默认隐式 breakpoint 会落在什么位置?
2) 为什么「共享数千静态 token + 末尾变化内容」仍可能导致 cached_tokens=0?
3) 要用显式 breakpoint 与 prompt_cache_key 解决什么问题?
请分条回答, 并引用文档中的关键机制表述.""",
    """【问题B · 工作原理四步】
按文档 How it works, 用自己的话概括一次 API 请求的四步:
Cache Routing → Cache Lookup → Cache Hit → Cache Miss.
对每一步写清: 依据什么做路由、命中时如何计费、未命中时是否可能写缓存以及写缓存费率.""",
    """【问题C · prompt_cache_key】
专门解释 prompt_cache_key:
1) 它如何与前缀 hash 一起影响路由?
2) 为何说 reliable matching 需要设置该 key?
3) 文档建议每个 key 的流量上限大约是多少 RPM? 超额会怎样?
4) 高并发时应如何分区 key?""",
    """【问题D · 显式 breakpoint 与 mode】
对比 prompt_cache_options.mode 的 implicit 与 explicit:
- 各自如何决定哪些 breakpoint 参与 cache 读/写?
- 显式 breakpoint 应放在什么内容之后?
- 每请求最多可新建多少次 cache write? implicit / explicit 下可读的历史 breakpoint 上限是多少?
- Chat Completions 与 Responses API 分别支持在哪些 content block 上加 breakpoint?""",
    """【问题E · 门槛、保留与 usage】
仅基于 Requirements / Prompt cache retention:
1) 可缓存前缀的严格最低 token 数是多少? 低于该值时 cached_tokens 通常是什么?
2) ttl 当前支持哪些值? 默认是什么? prompt_cache_retention 是否仍应用于 GPT-5.6+?
3) cached_tokens 与 cache_write_tokens 分别表示什么? 写缓存相对未缓存输入的费率倍数是多少?
4) 文档示例里 Chat Completions usage 中 cached_tokens / cache_write_tokens 分别是多少?""",
    """【问题F · 可缓存对象与最佳实践】
列出文档 What can be cached 中的全部类别, 并补充 Best practices 的核心建议
(静态前缀放置、key、显式 breakpoint、监控字段、稳态流量).
最后指出: 图像缓存时 detail 参数为何必须保持一致.""",
    """【问题G · FAQ 精读】
按文档 Frequently asked questions 逐条用中文转述并回答 (共 5 问):
隐私是否跨组织共享、是否影响最终输出一致性、能否手动清缓存、写缓存是否额外计费、是否仍计入 TPM.
每条先给结论, 再补一句文档依据.""",
    """【问题H · 工程落地清单】
假设你要在 Chat Completions 上为一个「长 system + 文档前缀 + 变化用户问题」的服务落地 Prompt Caching,
请基于文档给出一份可执行清单 (8-12 条), 覆盖:
prompt 结构、prompt_cache_key、prompt_cache_options.mode、breakpoint 放置、TTL、usage 监控、RPM 分区、以及如何避免把变化后缀写进可计费 cache.
最后用 80-120 字总结最大成本陷阱.""",
]


# =============================================================================
# 工具
# =============================================================================


def tprint(*args: Any, **kwargs: Any) -> None:
    with _PRINT_LOCK:
        print(*args, **kwargs)


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def extract_usage(usage: Any) -> dict[str, int]:
    usage_dict: dict[str, Any] = usage if isinstance(usage, dict) else {}
    prompt = _as_int(usage_dict.get("prompt_tokens"))
    completion = _as_int(usage_dict.get("completion_tokens"))
    total = _as_int(usage_dict.get("total_tokens")) or (prompt + completion)
    cached = 0
    ptd = usage_dict.get("prompt_tokens_details")
    if isinstance(ptd, dict) and ptd.get("cached_tokens") is not None:
        cached = _as_int(ptd.get("cached_tokens"))
    elif usage_dict.get("cache_read_input_tokens") is not None:
        cached = _as_int(usage_dict.get("cache_read_input_tokens"))
    elif usage_dict.get("cached_tokens") is not None:
        cached = _as_int(usage_dict.get("cached_tokens"))
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cached_tokens": cached,
    }


def fmt_rate(cached: int, prompt: int) -> str:
    return f"{(cached / prompt * 100) if prompt > 0 else 0.0:.2f}%"


def summarize_rounds(per_round: list[dict[str, Any]]) -> dict[str, Any]:
    normal = [r for r in per_round if not r.get("is_break_control")]
    from_r2 = normal[1:]
    sum_prompt = sum(r["prompt_tokens"] for r in normal)
    sum_cached = sum(r["cached_tokens"] for r in normal)
    sum_prompt_r2 = sum(r["prompt_tokens"] for r in from_r2)
    sum_cached_r2 = sum(r["cached_tokens"] for r in from_r2)
    sum_latency = sum(r["latency_ms"] for r in normal)
    return {
        "prompt_tokens": sum_prompt,
        "cached_tokens": sum_cached,
        "completion_tokens": sum(r["completion_tokens"] for r in normal),
        "cache_rate": (sum_cached / sum_prompt) if sum_prompt else 0.0,
        "prompt_tokens_r2": sum_prompt_r2,
        "cached_tokens_r2": sum_cached_r2,
        "cache_rate_r2": (sum_cached_r2 / sum_prompt_r2) if sum_prompt_r2 else 0.0,
        "avg_latency_ms": round(sum_latency / len(normal), 1) if normal else 0.0,
        "n_normal_rounds": len(normal),
    }


def chat_completion(
    *,
    endpoint: str,
    token: str,
    model: str,
    messages: list[dict[str, Any]],
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """POST chat/completions; model 放请求体. 返回 (text, usage, raw, request_body)."""
    if not model:
        raise ValueError("MODELS[].model 不能为空")
    url = endpoint.rstrip("/")
    if "/chat/completions" not in url:
        url = f"{url}/chat/completions"
    body = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "BCS-APIHub-RequestId": str(uuid.uuid4()),
        "X-Sticky-Key": STICKY_KEY,
        "X-CHJ-GWToken": token,
    }
    resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT, verify=SSL_VERIFY)
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:800]}")
    data = resp.json()
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    text = str(message.get("content") or "").strip()
    if not text:
        for key in ("reasoning_content", "reasoning"):
            alt = message.get(key)
            if isinstance(alt, str) and alt.strip():
                text = alt.strip()
                break
    return text, data.get("usage") or {}, data, body


def _write_json(path: Path, obj: Any) -> None:
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_results(run_dir: Path, results: list[dict[str, Any]], *, status: str) -> None:
    _write_json(
        run_dir / "results.json",
        {
            "run_id": run_dir.name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": status,
            "mode": "common_prefix_qa",
            "config": {
                "work_dir": str(WORK_DIR),
                "doc_path": str(DOC_PATH),
                "output_root": str(OUTPUT_ROOT),
                "doc_chars": len(_DOC_TEXT),
                "system_chars": len(SYSTEM_PROMPT),
                "shared_text_prefix_chars": len(SHARED_TEXT_PREFIX),
                "max_tokens": MAX_TOKENS,
                "request_gap_sec": REQUEST_GAP_SEC,
                "include_break_control": INCLUDE_BREAK_CONTROL,
                "suffix_questions": SUFFIX_QUESTIONS,
                "models": [{"name": c["name"], "endpoint": c["endpoint"], "model": c["model"]} for c in MODELS],
            },
            "results": results,
            "comparison": format_comparison(results) if results else "",
        },
    )


# =============================================================================
# 单模型跑测
# =============================================================================


def run_one_model(
    cfg: dict[str, str],
    run_dir: Path,
    progress: dict[str, dict[str, Any]],
    progress_lock: threading.Lock,
) -> dict[str, Any]:
    name, endpoint, token, model = cfg["name"], cfg["endpoint"].rstrip("/"), cfg["token"], cfg["model"]
    per_round: list[dict[str, Any]] = []

    def _flush(partial: dict[str, Any]) -> None:
        with progress_lock:
            progress[name] = partial
            ordered = [progress[c["name"]] for c in MODELS if c["name"] in progress]
            write_results(run_dir, ordered, status="running")
        model_dir = run_dir / "responses" / name
        _write_json(model_dir / "progress.json", partial)

    def _one(round_i: int, system: str, suffix: str, label: str, *, is_break: bool) -> dict[str, Any]:
        user_text = f"{SHARED_TEXT_PREFIX}\n\n{suffix}"
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        prefix_stable = (not is_break) and system == SYSTEM_PROMPT and user_text.startswith(SHARED_TEXT_PREFIX)

        tprint(f"\n--- [{name}] Round {round_i} · {label} ---")
        tprint(f"[request] model={model}  endpoint={endpoint}")
        tprint(f"[suffix] {suffix[:160]}{'...' if len(suffix) > 160 else ''}")

        t0 = time.perf_counter()
        text, raw_usage, response_raw, request_body = chat_completion(
            endpoint=endpoint, token=token, model=model, messages=messages
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = extract_usage(raw_usage)

        cache_diag = ""
        if round_i > 1 and usage["cached_tokens"] == 0 and not is_break:
            cache_diag = "shared_prefix_ok_but_cache_miss"
        elif is_break and usage["cached_tokens"] == 0:
            cache_diag = "break_control_expected_miss"
        elif is_break and usage["cached_tokens"] > 0:
            cache_diag = "break_control_unexpected_hit"

        tprint(f"[assistant] {text[:300]}{'...' if len(text) > 300 else ''}")
        tprint(
            f"[usage] prompt={usage['prompt_tokens']} cached={usage['cached_tokens']} "
            f"completion={usage['completion_tokens']} "
            f"rate={fmt_rate(usage['cached_tokens'], usage['prompt_tokens'])} "
            f"latency={latency_ms:.0f}ms"
            + (f" diag={cache_diag}" if cache_diag else "")
        )
        tprint(f"[usage.raw] {json.dumps(raw_usage, ensure_ascii=False, default=str)}")

        row = {
            "round": round_i,
            "label": label,
            "suffix": suffix,
            "assistant": text,
            "latency_ms": round(latency_ms, 1),
            **usage,
            "cache_rate": (usage["cached_tokens"] / usage["prompt_tokens"]) if usage["prompt_tokens"] else 0.0,
            "usage_raw": raw_usage,
            "response_raw": response_raw,
            "request_body": request_body,
            "prefix_stable": prefix_stable,
            "is_break_control": is_break,
            "cache_diag": cache_diag,
        }
        per_round.append(row)
        _write_json(
            run_dir / "responses" / name / f"round_{round_i:02d}.json",
            {"name": name, "endpoint": endpoint, "model": model, **row},
        )
        tprint(f"[saved] responses/{name}/round_{round_i:02d}.json")
        _flush(
            {
                "name": name,
                "endpoint": endpoint,
                "model": model,
                "ok": True,
                "error": None,
                "mode": "common_prefix_qa",
                "per_round": list(per_round),
                "shared_prefix_stable": all(r["prefix_stable"] for r in per_round if not r["is_break_control"]),
                **summarize_rounds(per_round),
            }
        )
        return row

    try:
        tprint("\n" + "#" * 72)
        tprint(f"# MODEL: {name}  model={model}")
        tprint(f"# endpoint: {endpoint}")
        tprint(f"# rounds: {len(SUFFIX_QUESTIONS)}" + (" +1 break" if INCLUDE_BREAK_CONTROL else ""))
        tprint("#" * 72)

        for i, suffix in enumerate(SUFFIX_QUESTIONS, start=1):
            _one(i, SYSTEM_PROMPT, suffix, f"shared-prefix Q{chr(64 + i)}", is_break=False)
            if i < len(SUFFIX_QUESTIONS) or INCLUDE_BREAK_CONTROL:
                time.sleep(REQUEST_GAP_SEC)

        if INCLUDE_BREAK_CONTROL:
            broken = SYSTEM_PROMPT.replace(_SYSTEM_MARKER, "【已改写·打断前缀】通用技术问答助手", 1)
            assert broken != SYSTEM_PROMPT
            _one(
                len(SUFFIX_QUESTIONS) + 1,
                broken,
                "【打断对照】system 已被改写, 预期公共前缀缓存不应命中. "
                "请仍用一句话概括 Prompt Caching 的最低 token 门槛.",
                "BREAK-control (system rewritten)",
                is_break=True,
            )

        summary = summarize_rounds(per_round)
        prefix_ok = all(r["prefix_stable"] for r in per_round if not r["is_break_control"])
        tprint(f"\n--- [{name}] summary ---")
        tprint(
            f"cache_all={fmt_rate(summary['cached_tokens'], summary['prompt_tokens'])}  "
            f"cache_r2+={fmt_rate(summary['cached_tokens_r2'], summary['prompt_tokens_r2'])}  "
            f"prefix_stable={prefix_ok}"
        )
        if summary["cached_tokens"] == 0:
            tprint(f"[warn][{name}] cached_tokens 全程为 0, 检查服务端 cache / usage 字段 / 前缀长度")

        result = {
            "name": name,
            "endpoint": endpoint,
            "model": model,
            "ok": True,
            "error": None,
            "mode": "common_prefix_qa",
            "per_round": per_round,
            "shared_prefix_stable": prefix_ok,
            **summary,
        }
        _flush(result)
        return result
    except Exception as e:
        tprint(f"\n[error][{name}] {e}")
        with _PRINT_LOCK:
            traceback.print_exc()
        fail = {
            "name": name,
            "endpoint": endpoint,
            "model": model,
            "ok": False,
            "error": repr(e),
            "mode": "common_prefix_qa",
            "per_round": per_round,
            "shared_prefix_stable": False,
        }
        _flush(fail)
        return fail


# =============================================================================
# 报告
# =============================================================================


def format_comparison(results: list[dict[str, Any]]) -> str:
    lines = [
        "=" * 100,
        "COMMON-PREFIX QA CACHE COMPARISON",
        "=" * 100,
        f"{'name':<24} {'status':<6} {'cache_all':>10} {'cache_r2+':>10} {'cached':>8} {'prompt':>8} {'avg_ms':>8}",
        "-" * 100,
    ]
    ok_results = []
    for r in results:
        if not r.get("ok"):
            lines.append(f"{r['name']:<24} {'FAIL':<6}  error={r.get('error')}")
            continue
        ok_results.append(r)
        lines.append(
            f"{r['name']:<24} {'OK':<6} {fmt_rate(r['cached_tokens'], r['prompt_tokens']):>10} "
            f"{fmt_rate(r['cached_tokens_r2'], r['prompt_tokens_r2']):>10} "
            f"{r['cached_tokens']:>8} {r['prompt_tokens']:>8} {r['avg_latency_ms']:>8.0f}"
        )
    if ok_results:
        names = [r["name"] for r in ok_results]
        n = max(len(r["per_round"]) for r in ok_results)
        lines += ["", "PER-ROUND CACHE RATE", f"{'rnd':>4}  " + "  ".join(f"{x:>18}" for x in names)]
        for i in range(1, n + 1):
            cells = []
            for r in ok_results:
                m = next((t for t in r["per_round"] if t["round"] == i), None)
                if not m:
                    cells.append("-")
                elif m.get("is_break_control"):
                    cells.append(f"{fmt_rate(m['cached_tokens'], m['prompt_tokens'])}*brk")
                else:
                    cells.append(fmt_rate(m["cached_tokens"], m["prompt_tokens"]))
            lines.append(f"{i:>4}  " + "  ".join(f"{c:>18}" for c in cells))
    return "\n".join(lines)


def build_report_markdown(results: list[dict[str, Any]], run_id: str) -> str:
    parts = [
        f"# LLM Common-Prefix QA Cache Report ({run_id})",
        "",
        f"- doc: `{DOC_PATH}` ({len(_DOC_TEXT)} chars)",
        f"- shared_prefix: {len(SHARED_TEXT_PREFIX)} chars",
        f"- questions: {len(SUFFIX_QUESTIONS)}, break_control: {INCLUDE_BREAK_CONTROL}",
        "",
        "```",
        format_comparison(results),
        "```",
        "",
    ]
    for r in results:
        parts += [f"## {r.get('name')}", "", f"- model: `{r.get('model')}`", f"- status: {'OK' if r.get('ok') else 'FAIL'}"]
        if not r.get("ok"):
            parts += [f"- error: `{r.get('error')}`", ""]
            continue
        parts += [
            f"- cache_all={fmt_rate(r['cached_tokens'], r['prompt_tokens'])}, "
            f"cache_r2+={fmt_rate(r['cached_tokens_r2'], r['prompt_tokens_r2'])}",
            "",
        ]
        for t in r.get("per_round", []):
            parts += [
                f"### Round {t['round']}: {t.get('label')}",
                "",
                t.get("suffix") or "",
                "",
                t.get("assistant") or "",
                "",
                f"`usage` prompt={t['prompt_tokens']} cached={t['cached_tokens']} "
                f"rate={fmt_rate(t['cached_tokens'], t['prompt_tokens'])} latency={t['latency_ms']:.0f}ms",
                "",
            ]
    return "\n".join(parts)


class _Tee:
    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for s in self.streams:
            s.write(data)
            s.flush()
        return len(data)

    def flush(self) -> None:
        for s in self.streams:
            s.flush()


def main() -> None:
    if not MODELS:
        raise SystemExit("MODELS 为空")

    os.chdir(WORK_DIR)
    out_root = OUTPUT_ROOT if OUTPUT_ROOT.is_absolute() else WORK_DIR / OUTPUT_ROOT
    if not _DOC_FILE.is_file():
        raise SystemExit(f"缺少参考文档: {_DOC_FILE}")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "system_prompt.txt").write_text(SYSTEM_PROMPT, encoding="utf-8")
    (run_dir / "shared_text_prefix.txt").write_text(SHARED_TEXT_PREFIX, encoding="utf-8")
    write_results(run_dir, [], status="starting")

    console_file = (run_dir / "console.log").open("w", encoding="utf-8")
    old_stdout = sys.stdout
    sys.stdout = _Tee(old_stdout, console_file)  # type: ignore[assignment]
    progress: dict[str, dict[str, Any]] = {}
    progress_lock = threading.Lock()
    try:
        workers = MAX_WORKERS if MAX_WORKERS > 0 else len(MODELS)
        print("=" * 72)
        print(f"run_id   : {run_id}")
        print(f"work_dir : {WORK_DIR}")
        print(f"doc      : {DOC_PATH} (~{len(_DOC_TEXT)} chars)")
        print(f"out_dir  : {run_dir}")
        print(f"models   : {len(MODELS)}  parallel={PARALLEL}")
        print(f"rounds   : {len(SUFFIX_QUESTIONS)}" + (" + break" if INCLUDE_BREAK_CONTROL else ""))
        print(f"shared   : ~{len(SHARED_TEXT_PREFIX)} chars")
        for cfg in MODELS:
            print(f"  - {cfg['name']}: body.model={cfg['model']} @ {cfg['endpoint']}")
        print("=" * 72)

        t0 = time.perf_counter()
        if PARALLEL and len(MODELS) > 1:
            by_name: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(run_one_model, cfg, run_dir, progress, progress_lock): cfg["name"] for cfg in MODELS}
                for fut in as_completed(futs):
                    result = fut.result()
                    by_name[result["name"]] = result
                    tprint(f"[parallel] finished: {result['name']} ({'OK' if result.get('ok') else 'FAIL'})")
            results = [by_name[c["name"]] for c in MODELS]
        else:
            results = [run_one_model(cfg, run_dir, progress, progress_lock) for cfg in MODELS]

        tprint(f"\n[wall_time] {time.perf_counter() - t0:.1f}s")
        print("\n" + format_comparison(results))
        write_results(run_dir, results, status="done")
        (run_dir / "report.md").write_text(build_report_markdown(results, run_id), encoding="utf-8")
        print(f"\n[saved] {run_dir / 'results.json'}")
        print(f"[saved] {run_dir / 'report.md'}")
    finally:
        sys.stdout = old_stdout
        console_file.close()


if __name__ == "__main__":
    main()
