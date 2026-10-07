# Decisions

Newest first. These entries describe infrastructure and serving contracts; examples do not identify a selected model.

## 2026-10-07: Publish a model-neutral source snapshot

- Decided by: the project owner, requesting an open-source release with model choices kept private.
- Choice: publish a fresh source snapshot with the original development history preserved privately. Project code uses the MIT License; external software and model weights keep their own licenses and notices.
- Configuration: the public `shared/model-profiles/profiles.json` is `{}`, and EC2's `vllm_extra_args` default is `[]`. Operators configure their chosen model locally before starting GPUs. The generated architecture registry retains the full upstream support list; it is compatibility data, not a list of selected models.
- Documentation: examples use synthetic names and generic sizing. Public documentation describes configuration and infrastructure behavior without deployment-specific model choices or performance observations.
- Rejected: distributing a curated model or parser configuration, because serving settings depend on the operator's exact model release.
- Residual risk: a fresh checkout requires a SageMaker profile or EC2 parser configuration before the client workflow can pass smoke. Generic compatibility data does not prove that a particular model will boot or satisfy the APIs.

## 2026-10-06: Explicit approval, lane fit, and CLI integration

- Decided by: the project owner, with the implementation design locked by the maintainer.
- Choice: `--yes` answers only the current command's confirmation after its plan and costs print. It never supplies endpoint names for `--take-over` or `--force`, or a Hugging Face token. Public model copies can use it; gated copies remain interactive.
- Lane fit: `sagemaker start` races only configured types whose GPU memory can hold the weights at a 90% threshold, passing the kept list to the saved Terraform plan. This is a weight-only preflight; runtime memory and context cache still need validation.
- Workflow: `up` runs `sagemaker start --no-watch` then `sagemaker tunnel --smoke`. Closing the tunnel restores client links and leaves the endpoint running. A failed smoke test leaves the tunnel open for diagnosis.
- Status: `status --json` makes read-only AWS calls in parallel without Terraform or key reads. Unreadable sections are null with reasons under `errors`; null means unknown.
- Rejected: an approval environment variable that would persist into later commands, noninteractive endpoint takeover, and Terraform state reads on every status poll.
- Residual risk: the caller must approve costs before using `--yes`; status polling has AWS API costs and rate limits; `up` connects to an existing caller-owned endpoint even if another model name was supplied.

## 2026-10-06: Temporary client links

- Decided by: the project owner, with the implementation design locked by the maintainer.
- Choice: tunnels link installed Codex, Claude Code CLI, and opencode clients using private sidecar records, owner locks, compare-and-swap writes, and symlink-resistant reads. If configuration is unchanged, unlink restores its original bytes. Otherwise it restores only values that still equal the link's writes, preserving later edits.
- Credentials: Codex and Claude Code use an SSM key-reading command. opencode requires a private 0600 key file in a 0700 directory while linked, since its provider configuration has no equivalent command hook. Unlink deletes that file.
- Control: `DRIP_AI_LINK_CLIENTS` and `--no-link` choose which clients to link. Each client links independently; a failed link leaves the tunnel and other successful links running. Stop and destroy commands unlink clients before removing the server.
- Rejected: duplicating lock and file-restoration code per client, and adding another parser solely to edit JSONC. Existing strict JSON limitations are reported to the operator.
- Residual risk: SIGKILL can leave links and the opencode key file behind until `unlink` or the next link. Project-level configuration and shell credentials can override user settings. The Claude desktop app is outside this workflow. API smoke coverage does not prove every client feature works.

## 2026-10-06: Race endpoint capacity and clean up every lane

- Decided by: the project owner, with the implementation design ratified by the maintainer.
- Choice: one Terraform root creates one endpoint per configured `instance_types` entry, sharing a model and execution role. `start` polls every 15 seconds. The first `InService` lane wins; ties use configuration order. Losing lanes are deleted, and a race with no winner ends after 3 hours.
- Ownership: a lane is never chosen or deleted until its `StartedBy` tag is readable and belongs to the caller, or an explicitly approved takeover binds to its identity. Stop also finds caller-owned endpoints outside Terraform state.
- Recovery: Terraform waits 60 minutes and can time out before the race finishes. A winner may therefore be outside state. `stop` checks AWS directly and reports success only once endpoints are gone. Transitional endpoints can refuse deletion; cleanup retries and reports possible ongoing billing when interrupted.
- Rejected: one Terraform root per lane, because separate inits, locks, and shared-model ownership would complicate cleanup; serial retries, because they delay finding capacity.
- Residual risk: several lanes can bill at once. Lost processes or laptops leave endpoints running. Keep `start` active until it reports losing lanes gone, and verify shutdown with status.

## 2026-10-06: Model profiles and architecture preflight

- Decided by: the project owner, with the implementation design locked by the maintainer.
- Choice: SageMaker reads `model_type` and `architectures[0]` from S3 before apply. Profiles supply model-specific vLLM flags; the generated registry supplies architecture compatibility for the pinned release. Operator flags merge last and win on shared flags. Argument validation rejects malformed flags, secret-shaped flags, and flags owned by Terraform variables.
- Window: an explicit `max_model_len` configures the server and clients consistently. The SageMaker default is 131072 tokens; available memory must hold the weights, cache, and overhead. Context-cache sizing depends on the model architecture and precision.
- Smoke: text responses must be nonempty and free of raw reasoning or tool markup. Automatic tool choice must return a named function with JSON arguments, exercising the parser rather than forcing a structured-output result.
- Rejected: model-specific branches in shell scripts and an automatic context window that could vary per boot. Profiles keep settings outside the lifecycle code.
- Residual risk: profile matching uses `model_type` alone, so operators must verify exact releases. A reasoning model may exhaust the smoke test's token budget. The generated registry can differ from the AWS image's patched vLLM build. A passing weight preflight does not prove startup within download time limits.

## 2026-10-05: Shared state and backend ownership

- Decided by: the project owner, with the architecture ratified by the maintainer.
- Choice: `bin/drip-ai` routes to backend folders. Each stack has a Terraform root and separate state key in a tagged, encrypted, private, versioned, TLS-only S3 bucket. Backend configuration derives from the caller's account and Region. Bootstrap keeps local state. Resource names and image pins live in `shared/modules/conventions`.
- Secrets: the API key uses Terraform's write-only value mechanism; state stores no key value. Client authentication reads it from SSM at use time, subject to the opencode file exception above.
- Guards: endpoint owner tags, Terraform locks, account checks, and fetch leases prevent collisions among trusted account members. Expected bucket-owner checks refuse buckets owned by another account. Provider lockfiles are readonly during operator initialization.
- Rejected: one combined state root, per-user workspaces, an additional Terraform orchestration dependency, and persistent Codex key files.
- Residual risk: these guards are not isolation from malicious account members. Anyone with state-write or serving permissions must be trusted with infrastructure and prompts. A lost local bootstrap state needs recovery or import.

## 2026-10-05: Copy model weights inside AWS

- Decided by: the project owner, with the architecture ratified by the maintainer.
- Choice: a Terraform-defined Fargate task copies a pinned Hugging Face revision into S3. It streams multipart uploads, verifies file size and hashes, resumes verified objects, refuses conflicting revisions and nested model prefixes, and records progress in a manifest. A lifecycle rule aborts incomplete multipart uploads.
- Credentials: the operator enters a token at a hidden prompt. SSM holds it while ECS may still need to resolve it; cleanup removes it once no task can still be starting. A shared S3 lease and task checks coordinate the shared parameter. Secret input may briefly occupy a private temporary file for the AWS CLI; cleanup removes it.
- Tags: Terraform owns the task definition. Launched ECS tasks carry project tags at creation, the explicit exception to Terraform-only resource creation. Network interfaces and public IPv4 charges also need to be included in cost estimates.
- Rejected: adding a new container build, downloading mutable code at runtime, and using a second orchestration service for a resumable copy job.
- Residual risk: Ctrl-C detaches rather than stopping the task. Abrupt process termination can leave credentials or leases requiring cleanup. Throughput and startup time depend on weight size, image pulls, service limits, and network conditions.

## 2026-10-05: Serve vLLM through private access paths

- Decided by: the project owner, with the backend design ratified by the maintainer.
- Choice: EC2 runs the pinned vLLM container and uses SSM port-forwarding, with no inbound security-group rules. SageMaker uses AWS's pinned vLLM image and a loopback proxy that authenticates clients and signs `InvokeEndpointWithResponseStream` requests. The proxy supplies `/v1/models` locally and routes the client APIs through the endpoint.
- APIs: vLLM handles chat completions, Responses, and Messages directly. The proxy carries requests rather than translating APIs. Its request-size and service-duration limits remain part of the client contract.
- EC2 credentials: instance permissions explicitly restrict SSM parameter reads to the API key and optional model-download token. The metadata hop limit prevents the bridge-networked container from reaching IMDS.
- Rejected: public inference ports guarded only by the vLLM API key, extra API translation layers, and custom in-container routing code when the image already supplies the route middleware.
- Residual risk: pinned backend images can contain different vLLM builds. Boot, token budgets, tool parsing, and client compatibility require live testing for the operator's model. Stopping EC2 retains billable disks; SageMaker must be deleted to end endpoint billing.
