# model-profiles

Operator-configured vLLM flags and the list of architectures the pinned SageMaker image's vLLM can serve. `bin/drip-ai sagemaker start` reads both before it applies. The public repository ships an empty `profiles.json`; configure a profile before a checked SageMaker start. How to add a profile is in [docs/models.md](../../docs/models.md#add-a-profile).

| Path | What it is |
| --- | --- |
| `profiles.json` | Hugging Face `config.json` `model_type` to `{"vllm_args": [...], "source": "<URL>"}`; the args go to Terraform as `vllm_model_args` |
| `vllm-architectures.json` | Generated: the generative architectures in vLLM's model registry at the tag the image pin is built from |
| `regenerate_architectures.py` | Rewrites `vllm-architectures.json`; run `python3 -I shared/model-profiles/regenerate_architectures.py <vLLM tag>` whenever the pin in `shared/modules/conventions` changes |
