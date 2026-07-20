"""Shared, provider-agnostic LLM calling layer.

Every LLM call in the system - the single-shot parser, the multi-agent
orchestrator's agents, and the specialty-AI matching tier - goes through
`client.structured_parse`, so retry/backoff, the circuit breaker, the
rate limiter, and the Azure same-model fallback are defined ONCE instead of
being reimplemented (and drifting) per call site.
"""
