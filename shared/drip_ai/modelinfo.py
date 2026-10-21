"""The facts sizing needs from a model: its config.json fields and the bytes of its weights.

config.json is untrusted input: it is read only up to 1 MB, and every field is bounds-checked;
a field that is missing or out of range is None (unknown), never a guessed value.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping, Optional

from drip_ai import load_catalog

CONFIG_MAX_BYTES = 1024 * 1024
MODEL_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
ARCHITECTURE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}")
WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".gguf")
# (field, lowest, highest) for every integer sizing reads.
BOUNDS = {
    "num_hidden_layers": (1, 1000),
    "num_nextn_predict_layers": (0, 16),
    "kv_lora_rank": (1, 65536),
    "qk_rope_head_dim": (1, 65536),
    "index_head_dim": (1, 65536),
    "num_key_value_heads": (1, 4096),
    "num_attention_heads": (1, 4096),
    "head_dim": (1, 65536),
    "hidden_size": (1, 1048576),
    "max_position_embeddings": (1, 1 << 26),
}


class ModelInfoError(ValueError):
    """config.json cannot describe a model; the message is one line."""


@dataclass(frozen=True)
class ModelFacts:
    model_type: str
    architecture: str
    family: str
    layers: int
    nextn_layers: int
    kv_lora_rank: Optional[int]
    qk_rope_head_dim: Optional[int]
    index_head_dim: Optional[int]
    kv_heads: Optional[int]
    head_dim: Optional[int]
    max_position_embeddings: Optional[int]
    quantization: Optional[str]
    weight_bytes: Optional[int]

    @property
    def key(self) -> str:
        """What learning keys a model's measurements by."""
        return f"{self.model_type}:{self.architecture}:{self.quantization or 'none'}"


def _bounded(config: Mapping, name: str) -> Optional[int]:
    value = config.get(name)
    low, high = BOUNDS[name]
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def family_of(architecture: str) -> str:
    for name, family in load_catalog("attention")["families"].items():
        if architecture in family["architectures"]:
            return name
    return "gqa"


def parse_config(data: bytes, weight_bytes: Optional[int] = None) -> ModelFacts:
    if len(data) > CONFIG_MAX_BYTES:
        raise ModelInfoError("config.json is larger than 1 MB")
    try:
        config = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ModelInfoError("config.json is not JSON") from None
    if not isinstance(config, dict):
        raise ModelInfoError("config.json is not a JSON object")
    model_type = config.get("model_type")
    architectures = config.get("architectures")
    if not isinstance(model_type, str) or not MODEL_TYPE.fullmatch(model_type):
        raise ModelInfoError("config.json has no plain model_type")
    if (not isinstance(architectures, list) or not architectures or not isinstance(architectures[0], str)
            or not ARCHITECTURE.fullmatch(architectures[0])):
        raise ModelInfoError("config.json has no plain architectures list")
    # Multimodal configs keep the language model's fields in text_config.
    text = config.get("text_config") if isinstance(config.get("text_config"), dict) else config
    if _bounded(text, "num_hidden_layers") is None and isinstance(config.get("num_hidden_layers"), int):
        text = config
    layers = _bounded(text, "num_hidden_layers")
    if layers is None:
        raise ModelInfoError("config.json has no num_hidden_layers from 1 to 1000")
    heads = _bounded(text, "num_attention_heads")
    head_dim = _bounded(text, "head_dim")
    hidden = _bounded(text, "hidden_size")
    if head_dim is None and heads and hidden and hidden % heads == 0:
        head_dim = hidden // heads
    quantization = None
    quant = config.get("quantization_config")
    if isinstance(quant, dict) and isinstance(quant.get("quant_method"), str):
        quantization = quant["quant_method"][:40]
    return ModelFacts(
        model_type=model_type,
        architecture=architectures[0],
        family=family_of(architectures[0]),
        layers=layers,
        nextn_layers=_bounded(text, "num_nextn_predict_layers") or 0,
        kv_lora_rank=_bounded(text, "kv_lora_rank"),
        qk_rope_head_dim=_bounded(text, "qk_rope_head_dim"),
        index_head_dim=_bounded(text, "index_head_dim"),
        kv_heads=_bounded(text, "num_key_value_heads") or heads,
        head_dim=head_dim,
        max_position_embeddings=_bounded(text, "max_position_embeddings"),
        quantization=quantization,
        weight_bytes=weight_bytes,
    )


def weight_bytes(sizes: Mapping[str, int]) -> Optional[int]:
    """Bytes of the weight files in a {path: size} listing; None when it lists none."""
    total = sum(size for path, size in sizes.items()
                if path.endswith(WEIGHT_SUFFIXES) and isinstance(size, int) and size >= 0)
    return total or None
