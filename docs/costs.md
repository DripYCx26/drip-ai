# Costs

drip-ai starts billable AWS resources only when you approve a command, and each of those commands prints its plan and an estimate first. The figures below are planning estimates recorded for us-east-1, not a current quote. Confirm AWS pricing, quotas and the plan before you approve a run. Taxes, data transfer, requests, logs and other resources can add cost.

| Resource | Planning estimate | Billing lifecycle |
| --- | --- | --- |
| SageMaker `ml.p5en.48xlarge` | $72.80/hour | Treat creation as billing; continues until deletion |
| SageMaker `ml.p5.48xlarge` | $63.30/hour | Same |
| SageMaker `ml.p5e.48xlarge` | Unknown | Confirm the price before including it in a race; a lane whose pool holds it shows its price as unknown |
| EC2 GPU instance | Depends on instance and Region | Compute while running; disk also while stopped |
| Fargate fetch task | About $0.40/hour, plus networking | While the copy task runs |
| S3 weights | About $0.023/GB-month | Until the model objects are deleted |

The figures live in [shared/catalog/prices.json](../shared/catalog/prices.json), and the race plan reads them from there.

Closing a tunnel does not stop GPU billing. A SageMaker endpoint bills until it is deleted, so run `bin/drip-ai sagemaker stop` when you finish. A race can have several endpoints creating at once; keep `start` running until it reports the losing lanes gone.

Run `bin/drip-ai status --json` and `bin/drip-ai doctor` to see what is running or left over, and use [Cost Explorer and resource tags](troubleshooting.md#check-what-you-spend) to check actual spend. `--yes` answers a command's confirmation after its plan prints; use it only for a run you have already approved.
