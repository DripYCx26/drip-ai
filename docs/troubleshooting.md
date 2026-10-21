# Troubleshooting

Find the symptom, then follow its fix. `bin/drip-ai sagemaker status` and `bin/drip-ai ec2 status` are always safe to run first.

## No GPU capacity

SageMaker capacity is not guaranteed even when your account has quota. `start` deletes a capacity-failed lane and creates it again on its own, printing `retry <k> (capacity)`; [startup limits](sagemaker.md#how-long-it-takes) lists the timings.

- Race more regions: `bin/drip-ai config set regions=us-east-2,us-east-1`; each region needs the foundation (`DRIP_AI_REGION=<region> bin/drip-ai setup`) and the weights, unless the profile sets `weights: hf`.
- Race more classes: by default every class whose GPUs hold the model races; `race.classes` or `race.instance_types` may be narrowing it ([SageMaker classes](models.md#sagemaker-classes)).
- Check the quota: a class races in a region only with endpoint quota for one of its types, and the race prints `<region> <class>: 0 lanes (quota ...); skipping it` otherwise. `race.max_lanes` (4) caps lanes across regions.
- Raise the race cap: `start --hours N` (whole hours 1 to 24, default `race.hours`, 8) ends the race with every lane deleted when it is reached.
- Buy a capacity block for EC2: `bin/drip-ai capacity search` and [use a capacity block](ec2.md#use-a-capacity-block).

## A losing lane will not go away

SageMaker deletes an endpoint only after `Creating` ends, so a losing lane lingers until then. `start` prints `<lane> is Creating, and SageMaker deletes it only after that ends; asking again every 15s` and keeps asking for up to 2 hours after the win; keep it running until it reports the other lanes gone. After 2 hours it exits 1 and names what still bills. If you already closed it, run `bin/drip-ai sagemaker stop` once `status` shows the lane `InService` or `Failed`. Until then it may bill.

## Every lane failed

`start` retries capacity failures. For any other failure it prints `<lane> (<class> in <region>) failed: <reason> [<kind>] (logs: /aws/sagemaker/Endpoints/<lane>); not retrying it`, where `<kind>` names what the log showed: `unknown_flag` (the image rejects a flag; the next plan drops it), `oom` or `kv_too_small` (the next plan uses the logged KV figure), `download`, or `health`. When every lane ends that way, it deletes the lanes, prints `Attempts per lane:` with each lane's creations and last failure, and exits 1. The usual causes are a missing or wrong [model profile](models.md#model-profiles), a bad `serve.extra_args` entry, or a window too large for the free memory. Check `bin/drip-ai plan <name> --explain`, fix it, and run `start` again.

## start was killed mid-race

Closing the terminal does not stop the race; it keeps going unattended. A killed process (SIGKILL) or a closed laptop can leave lanes billing. `bin/drip-ai doctor` lists the race (R9) with its run id. `bin/drip-ai race resume <run id>` re-adopts its lanes and races on, creating nothing it can find; `bin/drip-ai race cancel <run id>` or `bin/drip-ai sagemaker stop` deletes them. A second `start` refuses with `could not take the sagemaker-race lease; nothing was created` until the dead race's 15-minute lease expires; `stop` ends a live race on this machine and refuses to touch one on another machine.

## A coding tool does not use the model

- Start a fresh session after the tunnel says it linked, because a session that was open before keeps its old settings. For the Codex app, quit the whole app first; a new window is not enough.
- Check that the tunnel is still open; closing it restores your old config.
- [Connect clients](connect-clients.md) has per-tool checks and the commands for a link left behind by a crashed tunnel.

- Check `clients.link`: a tunnel links opencode only until you name Codex or Claude Code there ([connect clients](connect-clients.md)).

## Credentials expire

An `aws login` or SSO session can expire during a long race. The race pauses instead of failing: it changes nothing in AWS, prints `Run: aws login (or aws sso login)` once and every 15 minutes after, and resumes by itself once the same identity signs in. Signing in as another identity stops the race without deleting anything; sign in as the race's identity and run `bin/drip-ai race resume <run id>`. `start` warns before a race when your session ends sooner than lanes have usually waited for capacity, so sign in again first. If Terraform reports a credential error while `aws` commands work, run `eval "$(aws configure export-credentials --format env)"` in that shell and retry.

## Something is left over

`bin/drip-ai doctor` reads what drip-ai left in AWS and on this machine and names each finding by a stable id: Failed endpoints (R3), configurations and models nothing uses (R4, R5), log groups without retention (R6), untagged `drip-ai-*` resources made by hand (R7), stale leases (R8), orphaned races (R9), idle capacity blocks (R10), stale tunnel records (R11), port listeners drip-ai did not start (R12), and client links whose owner is gone (R13). `bin/drip-ai doctor --fix R3,R4 --yes` runs only those fixes, and only on resources tagged by you, older than an hour, outside `lifecycle.protect`, and not in use by any endpoint, including one mid-update. Untagged resources and other processes are reported with the command to run, never changed.

## Someone else holds a lock or lease

- `someone else is applying <root> right now`: another member is applying that stack. Wait for them. If a crash left the lock behind and nobody is applying, run Terraform's `force-unlock <ID>` through `bin/drip-ai tf <root>` with the lock ID from the message.
- A fetch says the lease is in use: wait for the named holder. If the lease is unreadable and no fetch runs, delete it with `aws s3 rm s3://<state bucket>/leases/fetch-model.json`.
- The CLI warns that leases need AWS CLI 2.22 or newer: upgrade it.
- `fetch` fails with `ParameterAlreadyExists`: someone created `/drip-ai/hf-token` by hand. Save that token under another name, then run `aws ssm delete-parameter --name /drip-ai/hf-token`.

## Check what you spend

Every resource carries the tags `Project=drip-ai`, `ManagedBy`, and `Stack`.

1. See what exists: in the AWS console, open Resource Groups, then `drip-ai`. From a terminal, run `aws resource-groups list-group-resources --group drip-ai --query 'ResourceIdentifiers[].ResourceArn'`.
2. Turn on the cost tags once per account: `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active TagKey=Stack,Status=Active`. Billing sees a new tag up to 24 hours after a resource uses it.
3. In Cost Explorer, group by the `Project` or `Stack` tag to see drip-ai spend per stack.

`sagemaker watch` shows an estimated cost for the running endpoint. [Stop paying](running.md#stop-paying) lists what bills after a stop.
