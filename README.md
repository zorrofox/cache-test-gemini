# LLM Prompt Caching Benchmark & Evaluation

本项目用于评估不同大语言模型（如 Azure OpenAI GPT 系列与 Google Gemini 系列）在处理长文档问答（Common-Prefix QA）场景下的 **Prompt Caching（提示词缓存）** 命中率与加速表现。

---

## 📁 目录结构

- `openai_prompt_caching.md`: 测试所用的长文档基准前缀（~30,000 Tokens）。
- `test_llm_cache_prefix_QA.py`: 基于 OpenAI 兼容协议（中转网关 API Hub）的多模型对比测试脚本。
- `gemini_native_cache_benchmark.py`: 基于 Google 官方 `google-genai` 原生 SDK 的 Gemini 缓存基准测试脚本（支持 Explicit 与 Implicit 模式）。
- `GEMINI_PROMPT_CACHING_GUIDE.md`: Gemini 缓存机制深度技术剖析与架构落地指南。

---

## 🚀 快速开始

### 方式一：运行 Google 官方原生 SDK 评测 (推荐)

此评测直接调用 Google Cloud Vertex AI / Gemini API，测试显式缓存（Explicit Caching）与多轮隐式缓存的真实表现：

```bash
# 1. 安装依赖
pip install google-genai

# 2. GCP 认证
gcloud auth application-default login

# 3. 运行原生基准测试
python gemini_native_cache_benchmark.py --model gemini-3.5-flash --location global
```

**预期输出（示例）**:
```
==========================================================================================
GEMINI PROMPT CACHING NATIVE BENCHMARK SUMMARY
==========================================================================================
Mode                           | Rounds   | Avg Prompt   | Avg Cached   | Cache Rate (R2+)  | Avg Latency
------------------------------------------------------------------------------------------
Explicit Caching (client.caches)| 8        | 24792        | 24767        | 99.90%            | 9500ms
Multi-turn Chat (Implicit)     | 5        | 6500         | 4060         | 62.46%            | 11200ms
==========================================================================================
```

---

### 方式二：运行通用 OpenAI 兼容网关评测

```bash
# 1. 编辑 test_llm_cache_prefix_QA.py 配置 endpoint 与 token
# 2. 运行脚本
python test_llm_cache_prefix_QA.py
```

> [!TIP]
> 关于为什么通用 OpenAI 中转网关在 Gemini 上可能出现 0% 命中，以及 `X-Sticky-Key` 路由机制的详细技术分析，请参阅 [GEMINI_PROMPT_CACHING_GUIDE.md](GEMINI_PROMPT_CACHING_GUIDE.md)。
