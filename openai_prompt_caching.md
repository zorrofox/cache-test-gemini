# Prompt Caching (GPT-5.6+)

> Source: https://developers.openai.com/api/docs/guides/prompt-caching  
> Scope: GPT-5.6 and later model families only

Model prompts often contain repetitive content, like system prompts and common instructions. OpenAI routes API requests to servers that recently processed the same prompt, making it faster and less expensive to reuse an exact prompt prefix than to process it from scratch.

Cache writes cost 1.25× the uncached input token rate. Both implicit and explicit caching are available; you can use explicit cache breakpoints to control exactly which prompt prefixes OpenAI caches. Writes are reported in `cache_write_tokens` and reads in `cached_tokens`, so you can measure the cost of writes against the savings from later cache hits.

## Caching behavior

GPT-5.6+ caches exact prompt prefixes at cache breakpoints. By default, the service places an implicit breakpoint at the latest user or tool message. It does not automatically fall back to the longest matching unmarked prefix before that breakpoint.

For example, requests might share 4,000 tokens of instructions and other static content, followed by changing timestamps, tool-call history, or user input. If the implicit breakpoint includes that changing content, the full prefix at the breakpoint differs between requests. As a result, `cached_tokens` can be `0` even though the requests share thousands of identical tokens, and the service can repeatedly write the changing prefix to cache.

To reuse the shared content, add an explicit `prompt_cache_breakpoint` at the end of the stable prefix and set the same `prompt_cache_key` on requests that share it. Content after the breakpoint can then change without invalidating the cached prefix.

To avoid cache-write charges for the changing suffix, set `prompt_cache_options.mode` to `explicit`. This disables the implicit breakpoint, so only your explicit breakpoints are eligible for cache reads and writes. Caching only the reusable prefix can reduce costs.

## Structuring prompts

Cache hits are only possible for exact prefix matches within a prompt. Place static content like instructions and examples at the beginning of your prompt, and put variable content, such as user-specific information, at the end. This also applies to images and tools, which must be identical between requests.

## How it works

Caching is enabled for prompts that are 1,024 tokens or longer (strict minimum). When you make an API request, the following steps occur:

1. **Cache Routing**:
   - Requests are routed to a machine based on a hash of the initial prefix of the prompt. The hash typically uses the first 256 tokens, though the exact length varies depending on the model.
   - If you provide the `prompt_cache_key` parameter, it is combined with the prefix hash, allowing you to influence routing and improve cache hit rates. This is especially beneficial when many requests share long, common prefixes.
2. **Cache Lookup**: The system checks if the initial portion (prefix) of your prompt exists in the cache on the selected machine.
3. **Cache Hit**: If a matching prefix is found, the system uses the cached result. This decreases latency and bills those tokens at the cached-input rate.
4. **Cache Miss**: If no matching prefix is found, the system processes your full prompt. When automatic caching is enabled, it may cache an eligible prefix on that machine for future requests. Tokens written to cache are billed at the cache-write rate (1.25× uncached input).

### Improve cache hit rates with a prompt cache key

Set `prompt_cache_key` on requests that share long, common prompt prefixes. Reuse the same key for those requests to help route them to the same cache and improve cache hit rates.

You must set `prompt_cache_key` to use the more reliable matching for both implicit and explicit caching. At each breakpoint, the service matches the key with the exact prompt prefix. Without a key, requests may still receive automatic cache hits, but they do not use the improved matching.

Keep the total traffic across all prefixes for each key to approximately 15 requests per minute. If a key receives a higher rate, some requests may miss the cache. For higher-volume workloads, partition traffic across more keys and use a stable mapping so requests with the same key continue to share prefixes.

## Prompt cache breakpoints

You can mark the end of a reusable prompt prefix with an explicit cache breakpoint. Breakpoints are available in both the Responses API and Chat Completions API.

Set the request-wide cache policy with `prompt_cache_options.mode`:

- `implicit` is the default. OpenAI places a cache breakpoint on the latest message and also uses any explicit breakpoints you provide.
- `explicit` disables the implicit breakpoint. Only explicit breakpoints are used for cache reads and writes. If the conversation contains no explicit breakpoints, the request does not use prompt caching or incur cache-write charges.

Add `prompt_cache_breakpoint: { "mode": "explicit" }` to a supported prompt content block. The breakpoint marks the exact end of the cached prefix, including that block and all prompt content rendered before it. Content after the breakpoint can change without invalidating the earlier cached prefix. All breakpoints use the request-wide `prompt_cache_options.ttl`, which currently defaults to `30m` and is the only supported value.

Each request can create up to four new cache writes. Breakpoints from earlier conversation turns are read-only: they can match the cache, but the request does not write them again. In `implicit` mode, the breakpoint on the latest message uses one write slot, so up to the latest three explicit breakpoints can be written. In `explicit` mode, up to the latest four explicit breakpoints can be written. For cache reads, OpenAI considers up to the latest 50 breakpoints in the conversation.

Responses API supports breakpoints on `input_text`, `input_image`, and `input_file` blocks. Chat Completions API supports them on `text`, `image_url`, `input_audio`, `file`, and `refusal` blocks.

When several breakpoints match cached content, the service reads from the longest matching prefix.

The following examples are abbreviated to show the request shape. In a real request, the rendered prefix before the marked breakpoint must contain at least 1,024 tokens to be cacheable.

### Responses API

This request uses the default `implicit` mode, which places a breakpoint on the latest message, and adds an explicit breakpoint after a stable file.

```json
{
  "model": "gpt-5.6",
  "prompt_cache_key": "tenant:acme:knowledge-base-v1",
  "input": [
    {
      "type": "message",
      "role": "user",
      "content": [
        {
          "type": "input_file",
          "file_id": "file_123",
          "prompt_cache_breakpoint": {
            "mode": "explicit"
          }
        },
        {
          "type": "input_text",
          "text": "Answer the current question."
        }
      ]
    }
  ]
}
```

### Chat Completions API

This request disables automatic breakpoint placement. Only the marked system-message prefix is eligible for billable cache writes and discounted cache reads.

```json
{
  "model": "gpt-5.6",
  "prompt_cache_key": "tenant:acme:support-assistant-v1",
  "prompt_cache_options": {
    "mode": "explicit"
  },
  "messages": [
    {
      "role": "system",
      "content": [
        {
          "type": "text",
          "text": "You are a support assistant.",
          "prompt_cache_breakpoint": {
            "mode": "explicit"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": "What should I do next?"
    }
  ]
}
```

Only `explicit` is valid for `prompt_cache_breakpoint.mode`. A marker on an unsupported or non-cacheable block returns a `400 invalid_request_error`.

## Prompt cache retention

Use `prompt_cache_options.ttl` to set the minimum lifetime of all breakpoints written by the request. It does not select a storage policy or maximum retention period. The only supported value is `30m`, which is also the default. A cached prefix remains eligible for reuse for at least 30 minutes, but OpenAI may retain it longer.

(`prompt_cache_retention` is deprecated for GPT-5.6+ and should not be used.)

## Requirements

- Caching is available for prefixes containing at least **1,024 tokens** (strict minimum).
- All requests display a `cached_tokens` field in the usage token details. Responses API returns this in `usage.input_tokens_details`; Chat Completions API returns it in `usage.prompt_tokens_details`. For requests under 1,024 tokens, `cached_tokens` is zero.
- `cache_write_tokens` reports the number of prompt tokens written to cache, billed at 1.25× the uncached input token rate.

Example Chat Completions usage (1,920 tokens read from cache, 0 written):

```json
{
  "usage": {
    "prompt_tokens": 2006,
    "completion_tokens": 300,
    "total_tokens": 2306,
    "prompt_tokens_details": {
      "cached_tokens": 1920,
      "cache_write_tokens": 0
    },
    "completion_tokens_details": {
      "reasoning_tokens": 0,
      "accepted_prediction_tokens": 0,
      "rejected_prediction_tokens": 0
    }
  }
}
```

### What can be cached

- **Messages:** The complete messages array, encompassing system, user, and assistant interactions.
- **Images:** Images included in user messages, either as links or as base64-encoded data. Ensure the detail parameter is set identically, as it impacts image tokenization.
- **Tool use:** Both the messages array and the list of available `tools` can be cached, contributing to the model's minimum cacheable prefix length.
- **Structured outputs:** The structured output schema serves as a prefix to the system message and can be cached.

## Best practices

- Structure prompts with **static or repeated content at the beginning** and dynamic, user-specific content at the end.
- Always set **`prompt_cache_key`** consistently across requests that share long, common prefixes (required for reliable matching). Keep traffic per key to ~15 RPM; partition across more keys for higher volume.
- Place **explicit cache breakpoints** after stable prompt content that is likely to be reused. Set `prompt_cache_options.mode` to `explicit` when you want only your breakpoints used.
- **Monitor** `cached_tokens` and `cache_write_tokens` to understand net cost and adjust breakpoint placement.
- **Maintain a steady stream of requests** with identical prompt prefixes to minimize cache evictions.

## Frequently asked questions

1. **How is data privacy maintained for caches?**  
   Prompt caches are not shared between organizations. Only members of the same organization can access caches of identical prompts. See the Your data guide for application-state, Zero Data Retention, and data residency details.

2. **Does Prompt Caching affect output token generation or the final response of the API?**  
   No. The model computes a new response from the cached prompt prefix, so otherwise identical nondeterministic requests are not guaranteed to return identical output.

3. **Is there a way to manually clear the cache?**  
   No. Cached prefixes remain eligible for reuse for at least 30 minutes and may be retained longer.

4. **Will I be expected to pay extra for writing to Prompt Caching?**  
   Yes. Cache writes are billed at 1.25× the uncached input token rate and reported in `cache_write_tokens`. Cache reads are reported in `cached_tokens`.

5. **Do cached prompts contribute to TPM rate limits?**  
   Yes. Caching does not affect rate limits.
