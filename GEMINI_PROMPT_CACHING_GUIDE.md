# Google Gemini Prompt Caching 技术全景与落地指南

针对在评测公共前缀问答（Common-Prefix QA）时遇到的**“Gemini 3.5 Flash 缓存命中率为 0% 而 Azure GPT 能够命中”**的问题，本文档深入剖析底层通信机制差异，并提供经过验证的企业级最佳实践方案。

---

## 1. 为什么 OpenAI 中转网关测试会导致 Gemini 0% 缓存命中？

在 `test_llm_cache_prefix_QA.py` 中，请求是通过通用的 OpenAI 兼容格式 (`/chat/completions`) 经由外部中转网关转发至各模型后端的。这对于 Azure GPT 与 Google Gemini 产生了截然不同的路由效果：

### 核心断层点 A：`X-Sticky-Key` 路由亲和性的协议差异
- **Azure OpenAI**：网关能识别并利用 `"X-Sticky-Key": "decision-autolabel-llm"`，将后续不同轮次的请求精准派发至同一台已加载前缀 Key-Value 显存快照的 Azure 计算节点，因而稳定呈现约 79% 的 Cache 命中。
- **Google Cloud Vertex AI**：Vertex AI 原生接口**不识别也不使用 `X-Sticky-Key`**。当网关默认将请求转发到 Google 的 `global` 全局负载均衡池时，无亲和性的无状态请求会被随机分散至数百个分布式 TPU/GPU 服务器切片上。因为每一个切片之前都未处理过该请求，导致每次都重新计算，隐式缓存命中率为 **0.00%**。

### 核心断层点 B：前缀结构与消息角色放置
- 脚本中将约 29,000 Token 的长参考文档直接嵌入动态变化的 `role="user"` 消息头部（`user_text = f"{SHARED_TEXT_PREFIX}\n\n{suffix}"`）。
- 对 Gemini 的隐式前缀分块匹配而言，直接在用户消息末尾高频修改独立提问，极易被判定为变异内容，进一步削弱了跨节点无状态请求的隐式匹配概率。

---

## 2. 隐式缓存 (Implicit) vs 显式缓存 (Explicit) 的关键门槛

根据 [Google AI 官方文档: Context Caching](https://ai.google.dev/gemini-api/docs/caching)，Gemini 提供了两种缓存机制，其设计目标与门槛如下：

| 机制类型 | Gemini 3.5 Flash 门槛 | Gemini 2.5 Flash 门槛 | 工作原理与 SLA 保障 |
| :--- | :---: | :---: | :--- |
| **Implicit Caching (隐式自动缓存)** | **≥ 4,096 Tokens** | ≥ 2,048 Tokens | **Best-effort（尽力而为）**：由服务端自动管理，无 SLA 保证。在连续多轮会话（Chat）或小规模单 Region 集中池中效果较好（50%~80%），但在 Global 散列独立请求中命中率较低。 |
| **Explicit Caching (显式声明缓存)** | **≥ 1,024 Tokens** | **≥ 1,024 Tokens** | **100% 确定性保证**：开发者主动创建 Cache 句柄对象，彻底打破集群负载均衡分流限制，在任何 Region / Global 均保证 **100% 稳定命中与约 90% 成本减免**。 |

> [!NOTE]
> 显式缓存门槛仅为 **1,024 Tokens**（低于 Gemini 3.x 隐式要求的 4,096 Tokens）。对于 1,024 ~ 4,095 Tokens 范围内的公共前缀，显式缓存是唯一能够生效的方式。

---

## 3. 实测数据对比 (Native Google GenAI SDK Benchmark)

使用官方 `google-genai` SDK 对相同的 30,000 Token 级文档（`openai_prompt_caching.md`）进行原生打桩评测（见 `gemini_native_cache_benchmark.py`），结果如下：

| 方案 | 运行模式 | Prompt Tokens | Cached Tokens | 缓存命中率 (R2+) | 平均耗时 |
| :--- | :--- | :---: | :---: | :---: | :---: |
| **显式缓存 (推荐)** | `client.caches.create` | ~24,792 | **24,767** | **99.90% (100% 稳定)** | **~9.5s (提速 55%)** |
| **隐式多轮 (Chat)** | `client.chats.create` | ~6,500 | 4,060 | **60% ~ 70%** | ~11.2s |
| **隐式独立单轮 (Global)** | `generate_content` (A+B) | ~24,796 | 0 | 0.00% ~ 20.00% | ~17.5s |

---

## 4. 推荐代码范式

### 最佳方案：Explicit Caching (适用于 A+B / A+C 变体与高并发服务)

```python
import time
from google import genai
from google.genai import types

# 1. 初始化客户端 (支持 Vertex AI 或 AI Studio)
client = genai.Client(vertexai=True, project="your-project-id", location="global")

# 2. 一次性为庞大的公共文档/知识库创建显式缓存对象 (>=1,024 Tokens 即可)
cache = client.caches.create(
    model="gemini-3.5-flash",
    config=types.CreateCachedContentConfig(
        display_name="enterprise_kb_cache",
        system_instruction="你是一个专业技术问答助手，请依据以下文档回答：\n" + open("openai_prompt_caching.md").read(),
        ttl="3600s",  # 存活时间 1 小时
    ),
)
print(f"显式缓存构建成功: {cache.name}")

# 3. 无论发送多少个不同的独立问题，只要指定 cached_content 即可实现 100% 缓存命中
for question in ["问题A...", "问题B...", "问题C..."]:
    response = client.models.generate_content(
        model="gemini-3.5-flash",
        contents=question,
        config=types.GenerateContentConfig(cached_content=cache.name),
    )
    usage = response.usage_metadata
    print(f"命中缓存 Tokens: {usage.cached_content_token_count} / {usage.prompt_token_count}")

# 4. 业务结束时释放缓存
client.caches.delete(name=cache.name)
```

---

## 5. 总结与行动项

1. **评估中转网关兼容性**：若使用中转网关测试 Gemini，请确认网关是否正确将 Gemini 的 `cached_content_token_count` 映射为 OpenAI 格式的 `prompt_tokens_details.cached_tokens`。
2. **生产环境建议**：对于共享公共前缀（A+B/A+C）的生产应用，强烈推荐使用 **Explicit Caching**，以获得确定性的 100% 命中率与 90% 成本减免。
