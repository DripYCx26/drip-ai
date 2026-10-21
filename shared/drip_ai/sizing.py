"""The defaults engine: model facts x hardware x runtime capabilities x measurements x intent ->
a serving plan (the vLLM flags) with the reason for every derived value.

Pure functions only; callers read config.json, the catalogs, and the learning store. The KV-cache
model, per GPU and per token of context:

- mla_dsa at fp8: (cache + indexer bytes per layer) x (layers + MTP draft layers);
- mla: (kv_lora_rank + qk_rope_head_dim) x dtype bytes x layers;
- gqa: 2 x ceil(kv_heads / TP) x head_dim x dtype bytes x layers.

MLA replicates its cache on every tensor-parallel rank, so only decode context parallelism (DCP)
grows the pool: pool = budget x DCP / bytes per token, where budget = HBM x gpu_memory_utilization
- weights / TP - overhead. A measured available-KV figure replaces the modeled budget, and a
measured pool replaces the modeled pool, for the same model, instance, image, and KV dtype.

Context: target = serve.max_model_len when explicit, else max_position_embeddings. DCP is the
smallest usable one whose pool holds target x target_sequences x fit_margin; else the smallest
whose pool holds target x fit_margin; else the context drops to floor(pool / fit_margin) in steps
of 1024, and below serve.min_context the plan refuses. Nothing falls back to a fixed window.

With DCP > 1 a family's dcp_flags (shared/catalog/attention.json) are always passed, and a runtime
that cannot take them gets no DCP. serve.repetition_penalty becomes --override-generation-config,
merged into any explicit object of that flag, because vLLM otherwise applies the checkpoint's
generation_config.json to every request.

This module also holds the only SM_VLLM_* mapper, `sm_env`, and the flag-list rules both
substrates share.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from drip_ai import load_catalog
from drip_ai.capabilities import (CapabilityRefusal, Evidence, Runtime, feature_status, flag_status, gate,
                                  usable)
from drip_ai.config import Resolved
from drip_ai.modelinfo import ModelFacts

ALIGN_TOKENS = 1024
DCP_CHOICES = (1, 2, 4, 8)
DEFAULT_BATCHED_TOKENS = 8192
# ASSUMPTION: KV offload takes at most 200 GiB or a tenth of host RAM, whichever is less; unmeasured.
KV_OFFLOAD_CAP_GIB = 200
KV_OFFLOAD_RAM_SHARE = 0.10
VLLM_DEFAULT_GPU_MEMORY_UTILIZATION = 0.9
FLAG = re.compile(r"--[a-z0-9][a-z0-9-]*")
SECRET_FLAG = re.compile(r"(?i)(api-key|secret|password|credential|(^--|-)token$)")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
ENV_NAME = re.compile(r"SM_VLLM_[A-Z0-9_]+")
# The config key behind each flag the engine derives, so an explicit key counts its flag explicit.
FLAG_KEYS = {
    "--decode-context-parallel-size": "serve.dcp",
    "--kv-cache-dtype": "serve.kv_cache_dtype",
    "--speculative-config": "serve.mtp_tokens",
    "--enable-prefix-caching": "serve.prefix_caching",
    "--async-scheduling": "serve.async_scheduling",
    "--max-num-batched-tokens": "serve.max_num_batched_tokens",
    "--kv-offloading-backend": "serve.kv_offload_gib",
    "--kv-offloading-size": "serve.kv_offload_gib",
    "--gpu-memory-utilization": "serve.gpu_memory_utilization",
    "--dcp-comm-backend": "serve.dcp",
    "--override-generation-config": "serve.repetition_penalty",
}
GENERATION_FLAG = "--override-generation-config"


class PlanRefusal(ValueError):
    """This model cannot be served on this hardware within the configuration; one line."""


class ArgsError(ValueError):
    """A vLLM flag list breaks the flag rules; one line naming the origin."""


@dataclass(frozen=True)
class Hardware:
    name: str
    names: Mapping[str, str]
    gpus: int
    gpu: str
    sm_arch: Optional[str]
    hbm_bytes_per_gpu: Optional[int]
    hbm_class: str
    host_ram_gib: Optional[int]
    vcpu: Optional[int]

    def on(self, substrate: str) -> str:
        return self.names["sagemaker" if substrate == "sagemaker" else "ec2"]


def hardware(instance_type: str) -> Hardware:
    """The catalog entry for an EC2 name (p5en.48xlarge) or a SageMaker name (ml.p5en.48xlarge)."""
    instances = load_catalog("instances")["instances"]
    name = instance_type[3:] if instance_type.startswith("ml.") else instance_type
    entry = instances.get(name)
    if entry is None:
        raise PlanRefusal(f"{instance_type} is not in shared/catalog/instances.json")
    return Hardware(name, entry["names"], entry["gpus"], entry["gpu"], entry.get("sm_arch"),
                    entry.get("hbm_bytes_per_gpu"), entry.get("hbm_class", "unknown"), entry.get("host_ram_gib"),
                    entry.get("vcpu"))


def gpu_classes() -> dict:
    """{class: [Hardware, ...]} in the catalog's class order."""
    catalog = load_catalog("instances")
    return {name: [hardware(t) for t in catalog["classes"][name]] for name in catalog["class_order"]}


def class_of(instance_type: str) -> Optional[str]:
    name = hardware(instance_type).name
    return next((c for c, types in load_catalog("instances")["classes"].items() if name in types), None)


# --- Flag lists ------------------------------------------------------------------------------------


def owned_flags() -> frozenset:
    return frozenset(load_catalog("vllm_flags")["owned"])


def flag_type(flag: str) -> Optional[str]:
    return load_catalog("vllm_flags")["flags"].get(flag, {}).get("type")


def parse_args(tokens: list, origin: str) -> list:
    """(flag, value or None) pairs from `--flag` and `--flag value` tokens. Refuses `--flag=value`,
    underscores, a stray value, a repeated flag, a flag the engine owns, a secret-shaped flag (its
    value would sit in the model environment, readable through DescribeModel), and control
    characters. A value for a json-typed flag must be JSON and is stored compact."""
    pairs: list = []
    owned = owned_flags()
    for token in tokens:
        if not isinstance(token, str) or CONTROL.search(token):
            raise ArgsError(f"{origin}: every flag and value must be one line of text")
        if token.startswith("--"):
            if not FLAG.fullmatch(token):
                raise ArgsError(f"{origin}: {token!r} is not a lowercase, hyphenated --flag; write --flag value,"
                                " not --flag=value")
            if token in owned:
                raise ArgsError(f"{origin}: {token} is set by drip-ai from its own setting (model, served model name,"
                                " tensor parallel size, or context window); remove it")
            if SECRET_FLAG.search(token):
                raise ArgsError(f"{origin}: {token} looks like a secret, and its value would be readable"
                                " in the server's environment")
            if any(flag == token for flag, _ in pairs):
                raise ArgsError(f"{origin}: {token} appears twice")
            pairs.append((token, None))
            continue
        if not pairs or pairs[-1][1] is not None:
            raise ArgsError(f"{origin}: {token!r} is a value with no flag before it")
        flag = pairs[-1][0]
        if flag_type(flag) == "json":
            try:
                token = json.dumps(json.loads(token), separators=(",", ":"))
            except ValueError:
                raise ArgsError(f"{origin}: {flag} takes JSON, not {token!r}") from None
        pairs[-1] = (flag, token)
    return pairs


def merge_args(*lists: list) -> list:
    """Later lists win on a flag: `true` makes it bare, `false` removes it."""
    merged: dict = {}
    for pairs in lists:
        for flag, value in pairs:
            merged.pop(flag, None)
            if value != "false":
                merged[flag] = None if value == "true" else value
    return list(merged.items())


def argv_of(pairs: list) -> list:
    argv: list = []
    for flag, value in pairs:
        argv += [flag] if value is None else [flag, value]
    return argv


def sm_env(pairs: list, owned: Mapping[str, str]) -> dict:
    """SM_VLLM_* environment for AWS's vLLM SageMaker image, which turns SM_VLLM_<FLAG> into
    `--<flag> <value>` and a "true" value into a bare flag. `owned` holds the engine's own
    SM_VLLM_MODEL, SM_VLLM_SERVED_MODEL_NAME, SM_VLLM_TENSOR_PARALLEL_SIZE, and SM_VLLM_MAX_MODEL_LEN
    plus the image's fixed switches. No value may hold a newline or control character."""
    env = dict(owned)
    for flag, value in pairs:
        name = "SM_VLLM_" + flag[2:].upper().replace("-", "_")
        if not ENV_NAME.fullmatch(name) or name in owned:
            raise ArgsError(f"{flag} cannot become an SM_VLLM_* variable")
        env[name] = "true" if value is None else value
    for name, value in env.items():
        if not isinstance(value, str) or CONTROL.search(value):
            raise ArgsError(f"{name} would hold a control character")
    return env


# --- The KV model ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Measured:
    """Learned measurements for one (model, instance, image, KV dtype) tuple: the available KV
    memory per GPU, and a pool size with the DCP it was measured at."""

    available_kv_bytes: Optional[int] = None
    pool_tokens: Optional[int] = None
    pool_dcp: Optional[int] = None


def _layout(facts: ModelFacts, dtype: str) -> Optional[dict]:
    family = load_catalog("attention")["families"][facts.family]
    layout = family["layouts"].get("fp8" if dtype == "fp8" else "bf16")
    if layout is None:
        return None
    fields = {"kv_lora_rank": facts.kv_lora_rank, "qk_rope_head_dim": facts.qk_rope_head_dim,
              "index_head_dim": facts.index_head_dim}
    if any(fields[name] != value for name, value in layout.get("expects", {}).items()):
        return None
    return layout


def layout_feature(facts: ModelFacts, dtype: str) -> str:
    """The runtimes.json feature that proves a KV layout, such as kv_layout:fp8_ds_mla."""
    layout = _layout(facts, dtype)
    if layout and "bytes_per_layer" in layout:
        return f"kv_layout:{layout['name']}"
    return f"kv_layout:{facts.family}:{dtype}"


def kv_bytes_per_token(facts: ModelFacts, dtype: str, tp: int, mtp_on: bool) -> Optional[tuple]:
    """(bytes per token per GPU, layout name, class), or None when config.json lacks a field."""
    layout = _layout(facts, dtype)
    if layout is None:
        return None
    layers = facts.layers + (facts.nextn_layers if mtp_on else 0)
    if "bytes_per_layer" in layout:
        per_layer = layout["bytes_per_layer"] + layout.get("indexer_bytes_per_layer", 0)
        return per_layer * layers, layout["name"], layout["class"]
    width = layout["dtype_bytes"]
    if layout["formula"] == "mla":
        if facts.kv_lora_rank is None or facts.qk_rope_head_dim is None:
            return None
        per_layer = (facts.kv_lora_rank + facts.qk_rope_head_dim) * width
        if layout.get("indexer"):
            if facts.index_head_dim is None:
                return None
            per_layer += facts.index_head_dim * width
        return per_layer * layers, layout["name"], layout["class"]
    if facts.kv_heads is None or facts.head_dim is None:
        return None
    per_layer = 2 * math.ceil(facts.kv_heads / tp) * facts.head_dim * width
    return per_layer * layers, layout["name"], layout["class"]


# --- The plan --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    key: str
    value: object
    source: str
    why: str

    def line(self) -> str:
        return f"{self.key} = {json.dumps(self.value)}  {self.source}  ({self.why})"


@dataclass(frozen=True)
class ServePlan:
    hardware: str
    substrate: str
    max_model_len: int
    tensor_parallel_size: int
    dcp: int
    kv_cache_dtype: str
    mtp_tokens: int
    pool_tokens: int
    pool_class: str
    pairs: tuple
    decisions: tuple
    notes: tuple = field(default_factory=tuple)

    @property
    def argv(self) -> list:
        return argv_of(list(self.pairs))

    def derived(self) -> tuple:
        """({config key: value}, {config key: why}) for Resolved.with_derived."""
        chosen = [d for d in self.decisions if d.source == "derived" and d.key.startswith("serve.")]
        return {d.key: d.value for d in chosen}, {d.key: d.why for d in chosen}


class _Planner:
    """One plan's inputs and the decisions so far, in the order the engine makes them."""

    def __init__(self, facts: ModelFacts, hw: Hardware, rt: Runtime, cfg: Resolved, substrate: str,
                 evidence: Evidence, measured: Measured):
        self.facts, self.hw, self.rt, self.cfg = facts, hw, rt, cfg
        self.substrate, self.evidence, self.measured = substrate, evidence, measured
        self.decisions: list = []
        self.where = hw.on(substrate)

    def flag_ok(self, flag: str) -> bool:
        return usable(flag_status(self.rt, flag, self.evidence)[0], self.substrate)

    def flag_why(self, flag: str) -> str:
        return flag_status(self.rt, flag, self.evidence)[1]

    def feature(self, name: str) -> tuple:
        status, why = feature_status(self.rt, name, self.hw.sm_arch)
        return usable(status, self.substrate), why

    def choose(self, key: str, derive: Callable[[], tuple]) -> object:
        """An explicit config value as set, else the engine's (value, why)."""
        if self.cfg.is_explicit(key):
            value = self.cfg[key]
            self.decisions.append(Decision(key, value, self.cfg.source(key), "set explicitly"))
            return value
        value, why = derive()
        self.decisions.append(Decision(key, value, "derived", why))
        return value

    def toggle(self, flag: str, on_why: str) -> tuple:
        return (True, on_why) if self.flag_ok(flag) else (False, self.flag_why(flag))

    def mtp(self) -> tuple:
        if self.facts.nextn_layers < 1:
            return 0, "config.json has no MTP draft layers"
        if not self.flag_ok("--speculative-config"):
            return 0, self.flag_why("--speculative-config")
        ok, why = self.feature("mtp")
        if not ok:
            return 0, why
        return 1, f"{self.facts.nextn_layers} draft layer(s); {why}"

    def kv_dtype(self) -> tuple:
        layout = _layout(self.facts, "fp8")
        if layout is None:
            return "auto", f"no fp8 KV layout fits this {self.facts.family} model's config.json fields"
        if not self.flag_ok("--kv-cache-dtype"):
            return "auto", self.flag_why("--kv-cache-dtype")
        ok, why = self.feature(layout_feature(self.facts, "fp8"))
        if not ok:
            return "auto", why
        return "fp8", f"the {layout['name']} layout: {why}"

    def offload(self) -> tuple:
        for flag in ("--kv-offloading-backend", "--kv-offloading-size"):
            if not self.flag_ok(flag):
                return 0, self.flag_why(flag)
        if self.hw.host_ram_gib is None:
            return 0, f"{self.where} has no host RAM recorded"
        size = min(KV_OFFLOAD_CAP_GIB, int(self.hw.host_ram_gib * KV_OFFLOAD_RAM_SHARE))
        return size, f"min({KV_OFFLOAD_CAP_GIB}, 10% of {self.hw.host_ram_gib} GiB host RAM)"

    def budget(self, tp: int) -> tuple:
        """(KV bytes per GPU, class) after weights and overhead."""
        if self.measured.available_kv_bytes is not None:
            return self.measured.available_kv_bytes, "measured"
        hbm = self.hw.hbm_bytes_per_gpu
        spent = self.facts.weight_bytes / tp + self.cfg["serve.overhead_fraction"] * hbm
        budget = hbm * self.cfg["serve.gpu_memory_utilization"] - spent
        if budget <= 0:
            raise PlanRefusal(f"{self.facts.weight_bytes / 1e9:.1f} GB of weights leave no KV-cache memory on"
                              f" {self.where} ({self.hw.gpus} x {hbm / 1e9:.1f} GB)")
        return budget, "modeled"

    def dcp_choices(self, tp: int) -> tuple:
        """(usable DCP sizes, why larger ones are out)."""
        family = load_catalog("attention")["families"][self.facts.family]
        if not family["replicated_across_tp"]:
            return [1], f"{self.facts.family} shards its KV cache across tensor parallel ranks"
        if self.cfg.is_explicit("serve.dcp"):
            if tp % self.cfg["serve.dcp"]:
                raise PlanRefusal(f"serve.dcp {self.cfg['serve.dcp']} does not divide tensor parallel size {tp}")
            return [self.cfg["serve.dcp"]], "set explicitly"
        if not self.flag_ok("--decode-context-parallel-size"):
            return [1], f"DCP unsupported: {self.flag_why('--decode-context-parallel-size')}"
        ok, why = self.feature(f"dcp:{self.facts.family}")
        if not ok:
            return [1], f"DCP unsupported: {why}"
        for flag, value in family.get("dcp_flags", {}).get("flags", {}).items():
            if not self.flag_ok(flag):
                return [1], f"DCP unsupported: {self.facts.family} needs {flag} {value}, and {self.flag_why(flag)}"
        return [d for d in DCP_CHOICES if tp % d == 0], why

    def generation(self, explicit: list) -> Optional[str]:
        """--override-generation-config's JSON object: every explicit one of that flag merged in
        order, plus serve.repetition_penalty, which an explicit flag's own repetition_penalty beats
        unless the key itself is set explicitly. None when nothing is overridden."""
        merged: dict = {}
        for flag, value in explicit:
            if flag != GENERATION_FLAG:
                continue
            override = json.loads(value) if value is not None else None
            if not isinstance(override, dict):
                raise ArgsError(f"{GENERATION_FLAG} takes a JSON object")
            merged.update(override)
        key = "serve.repetition_penalty"
        penalty = self.cfg[key]
        if penalty is not None and (self.cfg.is_explicit(key) or "repetition_penalty" not in merged):
            merged["repetition_penalty"] = penalty
        why = ("null keeps the checkpoint's generation_config.json" if penalty is None else
               "overrides the checkpoint's generation_config.json default, which vLLM applies to every request;"
               " a request may still set its own")
        if "repetition_penalty" in merged and merged["repetition_penalty"] != penalty:
            why = f"{GENERATION_FLAG} sets {merged['repetition_penalty']}"
        self.decisions.append(Decision(key, merged.get("repetition_penalty"), self.cfg.source(key), why))
        return json.dumps(merged, separators=(",", ":")) if merged else None

    def context(self, tp: int, pool: Callable[[int], int], pool_class: str) -> tuple:
        """(dcp, max_model_len): the fit rule in the module docstring, with each reason recorded."""
        cfg, margin = self.cfg, self.cfg["serve.fit_margin"]
        explicit_len = cfg.is_explicit("serve.max_model_len")
        if explicit_len:
            target, target_why = cfg["serve.max_model_len"], "set explicitly"
        elif self.facts.max_position_embeddings is not None:
            target, target_why = self.facts.max_position_embeddings, "config.json max_position_embeddings"
        else:
            raise PlanRefusal("config.json has no max_position_embeddings; set serve.max_model_len")
        choices, limit = self.dcp_choices(tp)
        sequences = cfg["serve.target_sequences"]
        soft = next((d for d in choices if pool(d) >= target * sequences * margin), None)
        safe = next((d for d in choices if pool(d) >= target * margin), None)
        if soft is not None:
            dcp, length, length_why = soft, target, target_why
            dcp_why = f"smallest DCP with pool >= {sequences}x{target}x{margin:.2f} ({pool(soft):,} {pool_class} tokens)"
        elif safe is not None:
            dcp, length, length_why = safe, target, target_why
            dcp_why = (f"no usable DCP holds {sequences}x{target}x{margin:.2f}; smallest with pool >="
                       f" {target}x{margin:.2f} ({pool(safe):,} {pool_class} tokens); {limit}")
        else:
            dcp = max(choices)
            fit = int(pool(dcp) / margin) // ALIGN_TOKENS * ALIGN_TOKENS
            if explicit_len:
                raise PlanRefusal(f"serve.max_model_len {target} needs {target * margin:,.0f} KV tokens, and"
                                  f" {self.where} holds {pool(dcp):,} ({pool_class}) at DCP {dcp}; {limit}")
            if fit < cfg["serve.min_context"]:
                raise PlanRefusal(f"{self.where} holds {pool(dcp):,} KV tokens ({pool_class}), below serve.min_context"
                                  f" {cfg['serve.min_context']} x {margin:.2f}; {limit}")
            dcp_why = f"largest usable DCP; none holds {target}x{margin:.2f}; {limit}"
            length, length_why = fit, (f"floor({pool(dcp):,} {pool_class} tokens / {margin:.2f}) to a multiple of"
                                       f" {ALIGN_TOKENS}, below {target_why} {target}")
        for key, value, why in (("serve.dcp", dcp, dcp_why), ("serve.max_model_len", length, length_why)):
            source = cfg.source(key) if cfg.is_explicit(key) else "derived"
            self.decisions.append(Decision(key, value, source, "set explicitly" if source != "derived" else why))
        return dcp, length


def plan(facts: ModelFacts, hw: Hardware, rt: Runtime, cfg: Resolved, substrate: str,
         profile_args: list = (), evidence: Evidence = Evidence(), measured: Measured = Measured()) -> ServePlan:
    """The serving plan for one instance type, or PlanRefusal saying why it cannot serve the model."""
    if facts.weight_bytes is None:
        raise PlanRefusal("the weights' size is unknown, so their fit cannot be checked")
    if hw.hbm_bytes_per_gpu is None:
        raise PlanRefusal(f"{hw.on(substrate)} has no GPU memory recorded in shared/catalog/instances.json")
    p = _Planner(facts, hw, rt, cfg, substrate, evidence, measured)
    tp = p.choose("serve.tensor_parallel_size", lambda: (hw.gpus, f"{p.where} has {hw.gpus} GPUs"))
    if tp != hw.gpus:
        raise PlanRefusal(f"serve.tensor_parallel_size is {tp}, but {p.where} has {hw.gpus} GPUs")
    mtp = p.choose("serve.mtp_tokens", p.mtp)
    dtype = p.choose("serve.kv_cache_dtype", p.kv_dtype)
    sized = kv_bytes_per_token(facts, "fp8" if dtype == "fp8" else "bf16", tp, mtp > 0)
    if sized is None:
        raise PlanRefusal(f"config.json lacks a field the {facts.family} KV model needs")
    bytes_per_token, layout_name, layout_class = sized
    budget, budget_class = p.budget(tp)
    family = load_catalog("attention")["families"][facts.family]
    replicated = family["replicated_across_tp"]

    def pool(dcp: int) -> int:
        if measured.pool_tokens is not None and measured.pool_dcp:
            return int(measured.pool_tokens * dcp / measured.pool_dcp)
        return int(budget * (dcp if replicated else 1) // bytes_per_token)

    pool_class = "measured" if measured.pool_tokens is not None else budget_class
    dcp, length = p.context(tp, pool, pool_class)
    p.decisions.append(Decision("kv.bytes_per_token", bytes_per_token, layout_class,
                                f"{layout_name} x {facts.layers + (facts.nextn_layers if mtp else 0)} layers, per GPU"))
    p.decisions.append(Decision("kv.pool_tokens", pool(dcp), pool_class,
                                f"{budget / 1e9:.1f} GB per GPU ({budget_class}) at DCP {dcp}"))
    prefix = p.choose("serve.prefix_caching", lambda: p.toggle("--enable-prefix-caching", "reuses shared prompt prefixes"))
    async_on = p.choose("serve.async_scheduling", lambda: p.toggle("--async-scheduling", "overlaps scheduling with compute"))
    batched = p.choose("serve.max_num_batched_tokens", lambda: (
        (DEFAULT_BATCHED_TOKENS, "the chunked-prefill step size") if p.flag_ok("--max-num-batched-tokens")
        else (None, p.flag_why("--max-num-batched-tokens"))))
    offload = p.choose("serve.kv_offload_gib", p.offload)
    util = cfg["serve.gpu_memory_utilization"]
    derived = []
    if util != VLLM_DEFAULT_GPU_MEMORY_UTILIZATION or p.flag_ok("--gpu-memory-utilization"):
        derived.append(("--gpu-memory-utilization", f"{util:g}"))
    if dcp > 1:
        derived.append(("--decode-context-parallel-size", str(dcp)))
        derived += list(family.get("dcp_flags", {}).get("flags", {}).items())
    if dtype != "auto":
        derived.append(("--kv-cache-dtype", dtype))
    if mtp > 0:
        spec = {"method": "mtp", "num_speculative_tokens": mtp}
        derived.append(("--speculative-config", json.dumps(spec, separators=(",", ":"))))
    derived += [(flag, None) for flag, on in (("--enable-prefix-caching", prefix), ("--async-scheduling", async_on)) if on]
    if batched:
        derived.append(("--max-num-batched-tokens", str(batched)))
    if offload:
        derived += [("--kv-offloading-backend", "native"), ("--kv-offloading-size", str(offload))]
    profile_pairs = parse_args(list(profile_args), "the model profile's vllm_args")
    extra_pairs = parse_args(list(cfg["serve.extra_args"]), "serve.extra_args")
    explicit = {flag for flag, _ in profile_pairs + extra_pairs}
    explicit |= {flag for flag, key in FLAG_KEYS.items() if cfg.is_explicit(key)}
    generation = p.generation(profile_pairs + extra_pairs)
    if generation is not None:
        profile_pairs = [pair for pair in profile_pairs if pair[0] != GENERATION_FLAG]
        extra_pairs = [pair for pair in extra_pairs if pair[0] != GENERATION_FLAG]
        derived.append((GENERATION_FLAG, generation))
    try:
        kept, notes = gate(merge_args(profile_pairs, derived, extra_pairs), explicit, rt, substrate, evidence)
    except CapabilityRefusal as refusal:
        raise PlanRefusal(str(refusal)) from None
    return ServePlan(p.where, substrate, length, tp, dcp, dtype, mtp, pool(dcp), pool_class, tuple(kept),
                     tuple(p.decisions), tuple(notes))
