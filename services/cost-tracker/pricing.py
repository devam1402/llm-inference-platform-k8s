"""
Pricing table for all supported models.

Prices in USD per 1 million tokens.
Self-hosted models = $0 (GPU cost amortized via OpenCost separately).

Known gap (v2): pricing should live in a versioned DB table, not code.
For now, code is fine — single-tenant, no production traffic.
"""

from typing import NamedTuple


class ModelPrice(NamedTuple):
    input_per_1m: float
    output_per_1m: float
    backend: str


PRICING: dict[str, ModelPrice] = {
    # Self-hosted via Ollama (local) or vLLM (cloud)
    "ollama/qwen2.5:0.5b":         ModelPrice(0.00,  0.00, "self-hosted"),
    "ollama/llama3.2:1b":          ModelPrice(0.00,  0.00, "self-hosted"),
    "ollama/llama3.2:3b":          ModelPrice(0.00,  0.00, "self-hosted"),
    "vllm/llama-3.1-8b":           ModelPrice(0.00,  0.00, "self-hosted"),
    "vllm/llama-3.3-70b":          ModelPrice(0.00,  0.00, "self-hosted"),
    
    # OpenAI fallback
    "gpt-4o":                      ModelPrice(2.50, 10.00, "openai"),
    "gpt-4o-mini":                 ModelPrice(0.15,  0.60, "openai"),
    "gpt-4-turbo":                 ModelPrice(10.00, 30.00, "openai"),
    
    # Anthropic fallback
    "claude-3-5-sonnet-20241022":  ModelPrice(3.00, 15.00, "anthropic"),
    "claude-3-5-haiku-20241022":   ModelPrice(1.00,  5.00, "anthropic"),
}

DEFAULT_PRICE = ModelPrice(0.00, 0.00, "unknown")


def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, str]:
    """Calculate USD cost for an inference request. Returns (cost, backend)."""
    price = PRICING.get(model, DEFAULT_PRICE)
    cost = (
        (input_tokens  * price.input_per_1m  / 1_000_000) +
        (output_tokens * price.output_per_1m / 1_000_000)
    )
    return cost, price.backend


def get_price(model: str) -> ModelPrice:
    return PRICING.get(model, DEFAULT_PRICE)
