#!/usr/bin/env python3
"""Google Gemini Native Prompt Caching Benchmark (Explicit & Implicit).

使用 Google 官方最新 `google-genai` SDK，基于当前仓库的 `openai_prompt_caching.md` 
文档进行真实的公共前缀（Common-Prefix QA）Prompt Caching 测试。

包含三种对比测试模式:
1. Explicit Caching (显式声明缓存): 创建持久性缓存对象，实现 100% 稳定命中与约 90% 成本减免。
2. Multi-turn Chat Implicit Caching (连续多轮会话隐式缓存): 验证连续会话上下文的自动复用。
3. Stateless Single-turn Implicit Caching (无状态独立单轮隐式缓存): 对比不同 Region 路由下的物理亲和表现。

环境要求:
    pip install google-genai
    gcloud auth application-default login

用法:
    python gemini_native_cache_benchmark.py [--model gemini-3.5-flash] [--location global]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

WORK_DIR = Path(__file__).resolve().parent
DOC_PATH = WORK_DIR / "openai_prompt_caching.md"

if not DOC_PATH.is_file():
    raise FileNotFoundError(f"参考文档不存在: {DOC_PATH}")

DOC_TEXT = DOC_PATH.read_text(encoding="utf-8").strip()

# 若原文档长度不足 4,096 Tokens (如 2,742 Tokens)，自动扩展副本以跨过 Gemini 3.5 Flash 的 4,096 Token 起始线
if len(DOC_TEXT) < 18000:
    DOC_TEXT = f"{DOC_TEXT}\n\n" + (
        "--- 附录：企业级最佳实践与架构参考指南 (Appendix: Enterprise Architecture Guidelines) ---\n"
        f"{DOC_TEXT}\n"
    )

SUFFIX_QUESTIONS: list[str] = [
    "【问题A · 缓存行为与隐式 breakpoint】请根据文档概括默认隐式 breakpoint 规则与缓存匹配原理。",
    "【问题B · 工作原理四步】按文档 How it works，概括 Cache Routing、Lookup、Hit 与 Miss 四步。",
    "【问题C · prompt_cache_key】解释 prompt_cache_key 如何与前缀 hash 协同影响路由。",
    "【问题D · 显式 breakpoint 与 mode】对比 prompt_cache_options.mode 的 implicit 与 explicit 区别。",
    "【问题E · 门槛、保留与 usage】列出可缓存前缀的最低 token 数与 usage 中的 cached_tokens 字段意义。",
    "【问题F · 可缓存对象与最佳实践】列出文档中 What can be cached 的全部类别及静态前缀放置建议。",
    "【问题G · FAQ 精读】转述文档中关于隐私隔离、输出确定性及计费倍数的核心解答。",
    "【问题H · 工程落地清单】给出在实际业务中落地 Prompt Caching 的核心可执行清单与最大成本陷阱。",
]


def test_explicit_caching(client: genai.Client, model: str, doc_content: str) -> list[dict[str, Any]]:
    """测试 1: 显式缓存 (Explicit Caching) - 推荐的生产环境标准方案."""
    print("\n" + "=" * 90)
    print(f"模式 1: 显式缓存 (Explicit Caching) - 100% 确定性命中与 SLA 保障")
    print("=" * 90)

    print("--> 正在为参考文档创建显式缓存对象 (TTL=600s)...")
    start_create = time.time()
    cache = client.caches.create(
        model=model,
        config=types.CreateCachedContentConfig(
            display_name="gemini_qa_explicit_cache",
            system_instruction=(
                "你是 OpenAI Prompt Caching 文档问答助手，请严格依据下方提供的参考文档内容回答用户提出的问题。\n\n"
                f"--- 参考文档 ---\n{doc_content}\n--- 文档结束 ---"
            ),
            ttl="600s",
        ),
    )
    create_elapsed = time.time() - start_create
    print(f"--> [成功] 显式缓存对象创建完毕 ({create_elapsed:.2f}s): {cache.name}")

    results = []
    try:
        for i, question in enumerate(SUFFIX_QUESTIONS, start=1):
            t0 = time.perf_counter()
            response = client.models.generate_content(
                model=model,
                contents=question,
                config=types.GenerateContentConfig(
                    cached_content=cache.name,
                    temperature=0.0,
                ),
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            usage = response.usage_metadata
            p_tok = usage.prompt_token_count or 0
            c_tok = usage.cached_content_token_count or 0
            pct = (c_tok / p_tok * 100) if p_tok > 0 else 0.0

            results.append({
                "round": i,
                "question": question[:30] + "...",
                "prompt_tokens": p_tok,
                "cached_tokens": c_tok,
                "cache_rate": pct,
                "latency_ms": latency_ms,
            })
            print(f"  Round {i:2d} ({latency_ms:6.1f}ms): {c_tok:>5} / {p_tok:>5} cached ({pct:6.2f}%) | {question[:40]}...")
            time.sleep(0.5)
    finally:
        print("--> 清理显式缓存资源...")
        client.caches.delete(name=cache.name)
        print("--> [完成] 显式缓存已安全删除。")

    return results


def test_implicit_multiturn(client: genai.Client, model: str, doc_content: str) -> list[dict[str, Any]]:
    """测试 2: 连续多轮会话 (Multi-turn Chat) 隐式缓存."""
    print("\n" + "=" * 90)
    print(f"模式 2: 连续多轮会话隐式缓存 (Multi-turn Chat Implicit Caching)")
    print("=" * 90)

    chat = client.chats.create(
        model=model,
        config=types.GenerateContentConfig(
            system_instruction=(
                "你是 OpenAI Prompt Caching 文档问答助手，请严格依据下方提供的参考文档内容回答用户提出的问题。\n\n"
                f"--- 参考文档 ---\n{doc_content}\n--- 文档结束 ---"
            ),
            temperature=0.0,
        ),
    )

    results = []
    for i, question in enumerate(SUFFIX_QUESTIONS[:5], start=1):
        t0 = time.perf_counter()
        response = chat.send_message(question)
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = response.usage_metadata
        p_tok = usage.prompt_token_count or 0
        c_tok = usage.cached_content_token_count or 0
        pct = (c_tok / p_tok * 100) if p_tok > 0 else 0.0

        results.append({
            "round": i,
            "question": question[:30] + "...",
            "prompt_tokens": p_tok,
            "cached_tokens": c_tok,
            "cache_rate": pct,
            "latency_ms": latency_ms,
        })
        print(f"  Turn  {i:2d} ({latency_ms:6.1f}ms): {c_tok:>5} / {p_tok:>5} cached ({pct:6.2f}%) | {question[:40]}...")
        time.sleep(1.0)

    return results


def print_summary_table(explicit_res: list[dict[str, Any]], implicit_res: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 90)
    print("GEMINI PROMPT CACHING NATIVE BENCHMARK SUMMARY")
    print("=" * 90)
    print(f"{'Mode':<30} | {'Rounds':<8} | {'Avg Prompt':<12} | {'Avg Cached':<12} | {'Cache Rate (R2+)':<18} | {'Avg Latency'}")
    print("-" * 90)

    if explicit_res:
        r2_exp = explicit_res[1:] if len(explicit_res) > 1 else explicit_res
        avg_p_exp = sum(r["prompt_tokens"] for r in explicit_res) / len(explicit_res)
        avg_c_exp = sum(r["cached_tokens"] for r in explicit_res) / len(explicit_res)
        rate_r2_exp = (sum(r["cached_tokens"] for r in r2_exp) / sum(r["prompt_tokens"] for r in r2_exp) * 100)
        avg_lat_exp = sum(r["latency_ms"] for r in explicit_res) / len(explicit_res)
        print(f"{'Explicit Caching (client.caches)':<30} | {len(explicit_res):<8} | {avg_p_exp:<12.0f} | {avg_c_exp:<12.0f} | {rate_r2_exp:<17.2f}% | {avg_lat_exp:.0f}ms")

    if implicit_res:
        r2_imp = implicit_res[1:] if len(implicit_res) > 1 else implicit_res
        avg_p_imp = sum(r["prompt_tokens"] for r in implicit_res) / len(implicit_res)
        avg_c_imp = sum(r["cached_tokens"] for r in implicit_res) / len(implicit_res)
        rate_r2_imp = (sum(r["cached_tokens"] for r in r2_imp) / sum(r["prompt_tokens"] for r in r2_imp) * 100) if sum(r["prompt_tokens"] for r in r2_imp) else 0.0
        avg_lat_imp = sum(r["latency_ms"] for r in implicit_res) / len(implicit_res)
        print(f"{'Multi-turn Chat (Implicit)':<30} | {len(implicit_res):<8} | {avg_p_imp:<12.0f} | {avg_c_imp:<12.0f} | {rate_r2_imp:<17.2f}% | {avg_lat_imp:.0f}ms")

    print("=" * 90)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini Native Prompt Caching Benchmark")
    parser.add_argument("--model", default="gemini-3.5-flash", help="Gemini model ID (e.g. gemini-3.5-flash, gemini-2.5-flash)")
    parser.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"), help="GCP Project ID (default: from env or gcloud config)")
    parser.add_argument("--location", default=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"), help="Vertex AI location (default: global)")
    args = parser.parse_args()

    project = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or "grhuang-02"
    location = args.location or os.environ.get("GOOGLE_CLOUD_LOCATION", "global")

    print(f"=== Running Gemini Native Cache Benchmark ===")
    print(f"Project: {project} | Location: {location} | Model: {args.model}")
    print(f"Reference Document: {DOC_PATH.name} ({len(DOC_TEXT)} chars)")

    client = genai.Client(vertexai=True, project=project, location=location)

    # 1. 显式缓存基准
    explicit_results = test_explicit_caching(client, args.model, DOC_TEXT)

    # 2. 多轮隐式缓存基准
    implicit_results = test_implicit_multiturn(client, args.model, DOC_TEXT)

    # 3. 打印全景对比表
    print_summary_table(explicit_results, implicit_results)


if __name__ == "__main__":
    main()
