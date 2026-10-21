# Choose a model

A model works when its weights and its context cache fit the GPUs, vLLM supports its architecture, and its tool-call and reasoning parsers are set. This page covers how drip-ai sizes each, plus the profile you add.

## How the plan sizes a model

`bin/drip-ai plan <name> --explain` and every SageMaker start size the model per GPU class from its `config.json`, the instance catalog ([shared/catalog/instances.json](../shared/catalog/instances.json)), and what the image is known to support ([shared/catalog/runtimes.json](../shared/catalog/runtimes.json)). Each derived value prints with its reason; anything you set explicitly is kept as set.

1. KV bytes per token, per GPU, by attention family ([shared/catalog/attention.json](../shared/catalog/attention.json)):
   - `mla_dsa` at fp8: (656 cache + 132 indexer bytes) x (layers + MTP draft layers). The byte layout is reported, not measured here.
   - `mla`: (kv_lora_rank + qk_rope_head_dim) x dtype bytes x layers.
   - `gqa`, every other architecture: 2 x ceil(KV heads / tensor parallel size) x head_dim x dtype bytes x layers.
2. The KV budget per GPU: GPU memory x `serve.gpu_memory_utilization` (0.90), less the weights per GPU, less a modeled overhead of `serve.overhead_fraction` (6%) of GPU memory. A KV memory figure vLLM logged on an earlier start replaces the model for the same model, instance, image, and KV precision.
3. The pool: budget / bytes per token. MLA keeps the whole cache on every tensor-parallel rank, so only decode context parallelism (DCP) grows it: pool = budget x DCP / bytes per token.
4. The window and DCP: the target is `serve.max_model_len` when you set it, else the model's `max_position_embeddings`. The plan takes the smallest supported DCP in 1, 2, 4, 8 whose pool holds `serve.target_sequences` (2) full sequences with `serve.fit_margin` (1.10) to spare; else the smallest that holds one; else it lowers the window to what the pool holds, in steps of 1024. Below `serve.min_context` (32768) it refuses. It never falls back to a fixed window, and an explicit window that does not fit refuses.
5. KV precision is fp8 when the image is known to support the model's fp8 layout on that GPU; MTP with one speculative token is on when the model has draft layers and the image supports it; prefix caching, async scheduling, 8192 batched tokens, and KV offload (the lesser of 200 GiB and 10% of host RAM) are each on when supported.

A synthetic worked example, an `mla_dsa` model with 78 layers, one draft layer, and 756 GB of fp8 weights on 8x H200: the budget is about 32.1 GB per GPU, the pool 0.52M tokens at DCP 1 and 4.1M at DCP 8, so the plan picks DCP 8 for two 1,048,576-token sequences.

What the image supports decides what SageMaker may use: SageMaker drops a derived flag the image is not known to accept, because a failed boot costs a second scarce machine, and the plan says which it dropped. EC2 tries such a flag. A flag you set explicitly is kept and noted, unless the image is known to reject it, which refuses the plan. Evidence comes from the catalog, from earlier starts the learning store recorded, and from `bin/drip-ai runtime probe`.

### Record what an image accepts

`bin/drip-ai runtime probe --docker` runs the pinned image's `vllm serve --help` locally (sign docker in to ECR first, as the command prints); it refuses when the disk the image would land on has less than `disk.min_free_gb` (30) GB free, because the image is tens of GB. `--help-file FILE` reads a copy captured on a GPU host, which needs no local pull and is the preferred path. Either writes, for that image digest, which checked flags it lists into `shared/catalog/runtimes.json`; commit the change. Help text proves a flag parses, not that its kernel works on a given GPU.

## Check the fit

Weight size is the parameter count times the bytes per parameter: about 1 byte in FP8, 2 in BF16. The GPU memory figures below come from the instance catalog; the plan uses them with the steps above, so a model that passes the old 90% weight rule can still be skipped when no room is left for the context cache.

### SageMaker classes

| Class | Instance types | GPUs | GPU memory per GPU |
| --- | --- | --- | --- |
| `h200` | `ml.p5en.48xlarge`, `ml.p5e.48xlarge` | 8x H200 | 143,771 MiB (nvidia-smi) |
| `b300` | `ml.p6-b300.48xlarge` | 8x B300 | 288 GB (AWS) |
| `b200` | `ml.p6-b200.48xlarge` | 8x B200 | 180 GB (AWS) |
| `h100` | `ml.p5.48xlarge` | 8x H100 | 81,559 MiB (nvidia-smi) |

Every type above takes tensor parallel size 8, which the plan derives. The p6 classes race once their endpoint quota is approved. The pinned image's DCP, fp8 KV, and MTP evidence was recorded on H200 (sm90), so a Blackwell plan uses the bf16 layout until a start or a probe there records more.

### EC2 sizing

Set these in `ec2/terraform/terraform.tfvars`:

| Instance type | GPUs | Total GPU memory | `tensor_parallel_size` |
| --- | --- | --- | --- |
| `g6e.2xlarge` | 1x L40S | 48 GB | 1 |
| `g6e.12xlarge` | 4x L40S | 192 GB | 4 |

For a synthetic example, 20 billion parameters at 2 bytes per parameter require about 40 GB just for weights. Runtime overhead and the context cache need additional memory. Run `bin/drip-ai plan <name>` for the window each class holds, and if vLLM logs an out-of-memory error, lower `max_model_len` first.

The EC2 backend also exposes the optional `max_num_seqs` setting for the
maximum active sequences. Leave it `null` to use the pinned vLLM default. The
launch does not pass `--no-enable-prefix-caching`, so vLLM V1 prefix caching
stays enabled.

## Model profiles

Coding clients need parsed tool calls, and reasoning models may need thinking separated from the answer. The correct flags depend on the exact model release. The public `shared/model-profiles/profiles.json` is an empty JSON object, so **configure a local profile before starting SageMaker**.

Before it creates anything, `sagemaker start` reads the model's `config.json` from S3:

1. `architectures[0]` must appear in `shared/model-profiles/vllm-architectures.json`, the generated architecture list for the pinned vLLM release.
2. `model_type`, or the exact `Org/Name@revision` the fetch recorded, must have an entry in `profiles.json`; the exact entry wins.
3. The profile's flags go to vLLM, then the plan's derived flags, then your `serve.extra_args`, which win on shared flags.

### Add a profile

1. Read `model_type` and `architectures` from the selected model's `config.json`.
2. Read that release's model card and serving documentation. Confirm the required parser names in the pinned vLLM release's `vllm/tool_parsers/__init__.py` and `vllm/reasoning/__init__.py`. Use a reasoning parser only when the model requires one.
3. Add an entry keyed by the exact `model_type` (or `Org/Name@revision`), containing `vllm_args` (an array of flags and values) and `source` (the documentation URL supporting those settings). It may add `serve` (any `serve.*` config key, without the prefix) and `weights` (`"s3"`, the default, or `"hf"` to download the fetched revision on the endpoint). Start from this synthetic shape, replacing every placeholder with settings for your model:

   ```json
   {
     "your_model_type": {
       "vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser", "your_parser"],
       "source": "https://example.com/model-serving-documentation",
       "serve": {"target_sequences": 1}
     }
   }
   ```

4. Keep the profile local if it identifies a private model choice. Recheck it when switching model releases, since matching `model_type` alone does not prove the same parsers work.
5. After starting and opening a tunnel, run `bin/drip-ai smoke`. A profile entry passes preflight, but a live smoke test is needed to check the serving behavior.

Profile argument values may contain only letters, digits, and `. _ : / + -`. The example is a format illustration, not a runnable model profile. Configure the profile and supported architecture rather than bypassing preflight.

### Parsers on EC2

EC2 reads `vllm_extra_args` in `ec2/terraform/terraform.tfvars` directly and does not use the profile registry. The public default is `[]`. Add the flags required by your selected release before `ec2 apply`, and check them against the pinned vLLM release. To change them on a running server, `bin/drip-ai ec2 reconfigure -- <flags>` rewrites them in place.

## Set the context window

On SageMaker the plan derives the window as described above; set `serve.max_model_len` to fix it (`bin/drip-ai config set serve.max_model_len=262144`), and the plan refuses a value the KV cache cannot hold. EC2 defaults to 65536; change it with one line in `ec2/terraform/terraform.tfvars`. The tunnel reads the served window from the running server and gives your coding tools the same value.

## Add vLLM flags

`serve.extra_args` adds vLLM flags on SageMaker; EC2 takes `vllm_extra_args`. These rules apply to both, and to a profile's flags:

- Write each flag as `--flag value`, not `--flag=value`, with a lowercase hyphenated name and at most one value. A JSON-valued flag such as `--speculative-config` takes JSON, stored compact.
- A flag may appear once per list. A flag in both the profile and `serve.extra_args` takes the `serve.extra_args` value. `true` becomes a bare flag and `false` drops it, so `["--enable-auto-tool-choice", "false"]` turns a profile flag off.
- `--model`, `--served-model-name`, `--tensor-parallel-size`, and `--max-model-len` are refused; set `serve.served_model_name`, `serve.tensor_parallel_size`, and `serve.max_model_len` instead.
- Secret-looking flags are refused (`--api-key`, names ending in `-token`, or containing `secret`, `password`, or `credential`), because anyone with SageMaker read access can see the model's environment.

Other SageMaker settings in the config:

- `sagemaker.volume_size_gb`: leave it unset on NVMe instance types such as p5 and p5en.
- `sagemaker.startup_timeout_seconds`, `sagemaker.download_timeout_seconds`, and `sagemaker.provision_timeout_seconds` default to 3600, which is also the maximum.
- `sagemaker.inference_ami_version` is unset by default, which lets SageMaker choose; set it to pin a driver.

The container image pins live in `shared/modules/conventions`. Change them only with a dated entry in [DECISIONS.md](DECISIONS.md), then regenerate the architecture list with `python3 -I shared/model-profiles/regenerate_architectures.py <vLLM tag>`, and probe the new image with `bin/drip-ai runtime probe`.
