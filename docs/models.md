# Choose a model

A model works when its weights fit the GPUs, vLLM supports its architecture, and its tool-call and reasoning parsers are set. This page covers each, plus the context window.

## Check the fit

Weight size is the parameter count times the bytes per parameter: about 1 byte in FP8, 2 in BF16. The fits below are derived from that, not measured. The KV cache for the context window needs the memory the weights leave free. `tensor_parallel_size` must equal the instance's GPU count.

### SageMaker sizing

`sagemaker start` skips each instance type when the weights exceed 90% of its GPU memory, and refuses when no configured type fits. This preflight checks weight size, not the complete runtime memory requirement.

| Instance type | GPUs | GPU memory | Largest model |
| --- | --- | --- | --- |
| `ml.p5en.48xlarge` | 8x H200 | 1,128 GB | About 1,015 GB |
| `ml.p5e.48xlarge` | 8x H200 | 1,128 GB | About 1,015 GB |
| `ml.p5.48xlarge` | 8x H100 | 640 GB | About 576 GB |

All three take `tensor_parallel_size = 8`, the default. As a synthetic weight-only example, a 400 GB copy passes the threshold on all three, while an 800 GB copy passes only on the H200 types. Passing does not prove the context cache or runtime overhead fits. Cost estimates are in the [README](../README.md#costs).

### EC2 sizing

Set these in `ec2/terraform/terraform.tfvars`:

| Instance type | GPUs | Total GPU memory | `tensor_parallel_size` |
| --- | --- | --- | --- |
| `g6e.2xlarge` | 1x L40S | 48 GB | 1 |
| `g6e.12xlarge` | 4x L40S | 192 GB | 4 |

For a synthetic example, 20 billion parameters at 2 bytes per parameter require about 40 GB just for weights. Runtime overhead and the context cache need additional memory. Quantization support and memory behavior depend on the model and vLLM release; calculate the full requirement before picking an instance.

If vLLM logs an out-of-memory error, lower `max_model_len` first.

The EC2 backend also exposes the optional `max_num_seqs` setting for the
maximum active sequences. Leave it `null` to use the pinned vLLM default. The
launch does not pass `--no-enable-prefix-caching`, so vLLM V1 prefix caching
stays enabled.

## Model profiles

Coding clients need parsed tool calls, and reasoning models may need thinking separated from the answer. The correct flags depend on the exact model release. The public `shared/model-profiles/profiles.json` is an empty JSON object, so **configure a local profile before starting SageMaker**.

Before it creates anything, `sagemaker start` reads the model's `config.json` from S3:

1. `architectures[0]` must appear in `shared/model-profiles/vllm-architectures.json`, the generated architecture list for the pinned vLLM release.
2. `model_type` must have an entry in `profiles.json`.
3. The profile's flags go to vLLM. Your `vllm_extra_args` merge last and win on shared flags.

### Add a profile

1. Read `model_type` and `architectures` from the selected model's `config.json`.
2. Read that release's model card and serving documentation. Confirm the required parser names in the pinned vLLM release's `vllm/tool_parsers/__init__.py` and `vllm/reasoning/__init__.py`. Use a reasoning parser only when the model requires one.
3. Add an entry keyed by the exact `model_type`, containing `vllm_args` (an array of flags and values) and `source` (the documentation URL supporting those settings). Start from this synthetic shape, replacing every placeholder with settings for your model:

   ```json
   {
     "your_model_type": {
       "vllm_args": ["--enable-auto-tool-choice", "--tool-call-parser", "your_parser"],
       "source": "https://example.com/model-serving-documentation"
     }
   }
   ```

4. Keep the profile local if it identifies a private model choice. Recheck it when switching model releases, since matching `model_type` alone does not prove the same parsers work.
5. After starting and opening a tunnel, run `bin/drip-ai smoke`. A profile entry passes preflight, but a live smoke test is needed to check the serving behavior.

Argument values may contain only letters, digits, and `. _ : / + -`. The example is a format illustration, not a runnable model profile. Configure the profile and supported architecture rather than bypassing preflight.

### Parsers on EC2

EC2 reads `vllm_extra_args` in `ec2/terraform/terraform.tfvars` directly and does not use the profile registry. The public default is `[]`. Add the flags required by your selected release before `ec2 apply`, and check them against the pinned vLLM release. An empty list does not configure tool-call or reasoning parsing for you.

## Set the context window

`max_model_len` is the context window in tokens. SageMaker defaults to 131072 and EC2 to 65536; change it with one line in the backend's `terraform.tfvars`. The KV cache for the whole window must fit beside the weights, or vLLM fails at boot and the backend's `logs` command shows why. The tunnel prints the window and gives your coding tools the same value.

## Add vLLM flags

`vllm_extra_args` adds vLLM flags. On SageMaker, the plan enforces these rules for it and for a profile's flags:

- Write each flag as `--flag value`, not `--flag=value`, with a lowercase hyphenated name and at most one value.
- A flag may appear once per list. A flag in both lists takes the `vllm_extra_args` value. `true` becomes a bare flag and `false` drops it, so `["--enable-auto-tool-choice", "false"]` turns a profile flag off.
- `--model`, `--served-model-name`, `--tensor-parallel-size`, and `--max-model-len` are refused; set the Terraform variables instead.
- Secret-looking flags are refused (`--api-key`, names ending in `-token`, or containing `secret`, `password`, or `credential`), because anyone with SageMaker read access can see the model's environment.

Other SageMaker settings in `terraform.tfvars`:

- Leave `volume_size_gb` unset on NVMe instance types such as p5 and p5en.
- `startup_timeout_seconds` and `download_timeout_seconds` default to 3600, which is also the maximum.
- `inference_ami_version` defaults to `al2023-ami-sagemaker-inference-gpu-4-1`.

The container image pins live in `shared/modules/conventions`. Change them only with a dated entry in [DECISIONS.md](DECISIONS.md), and then regenerate the architecture list with `python3 -I shared/model-profiles/regenerate_architectures.py <vLLM tag>`.
