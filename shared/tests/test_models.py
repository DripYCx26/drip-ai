"""Pins `shared/models.sh upload` and `list` against stub CLIs."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "testing"))

from cli_stubs import StubRepo  # noqa: E402

SCRIPT = "shared/models.sh"
ACCOUNT = "111122223333"
REGION = "eu-west-1"
STATE_BUCKET = f"drip-ai-tfstate-{ACCOUNT}-{REGION}"
ENV = {"DRIP_AI_REGION": REGION}
FOLDER = "tiny-model"
BUCKET = "drip-ai-models-111122223333"
MODEL_FILES = {
    f"models/{FOLDER}/config.json": "{}\n",
    f"models/{FOLDER}/model.safetensors": "weights\n",
}
# The shared root was initialised against the caller's state bucket, so `tf` runs no init.
SHARED_BACKEND = {
    "shared/terraform/.terraform/terraform.tfstate":
        json.dumps({"version": 3, "backend": {"type": "s3", "config": {"bucket": STATE_BUCKET}}}),
}
# One list of every key under the folder, filtered to the files the nesting rule reads.
MARKER_QUERY = (
    "Contents[?ends_with(Key, '/config.json') || ends_with(Key, '/drip-ai-fetch.json')"
    " || ends_with(Key, '.safetensors')].Key"
)
WEIGHTS_MARKER = "config.json beside *.safetensors"
SYNC_CALL = [
    "s3", "sync", "--region", REGION,
    "--exclude", ".cache/*", "--exclude", ".git/*", "--exclude", ".DS_Store", "--exclude", "*/.DS_Store",
]


def list_call(folder=FOLDER):
    return [
        "s3api", "list-objects-v2", "--region", REGION, "--bucket", BUCKET,
        "--prefix", f"models/{folder}/", "--query", MARKER_QUERY, "--output", "text",
    ]


def nested_message(sub="Inner", marker=WEIGHTS_MARKER):
    return (
        f"s3://{BUCKET}/models/{FOLDER}/{sub}/ holds another model ({marker}), and SageMaker would"
        f" load it with this one; move it out of models/{FOLDER}/ in S3, or use another folder"
        " name. Nothing was uploaded\n"
    )


LOCAL_NESTED_MESSAGE = (
    f"models/{FOLDER}/mtp/ holds another model ({WEIGHTS_MARKER}), and SageMaker would load it with"
    f" this one; move it out of models/{FOLDER}/, or upload it as its own folder. Nothing was uploaded\n"
)
# The first upload of a folder with its own sub-folder already put these in S3. A lone sub-folder
# config.json, without *.safetensors beside it, is this model's own file, as sagemaker/start.sh's
# nested_model rule reads an uploaded folder.
OWN_SUB_FOLDER_FILES = {
    **MODEL_FILES,
    f"models/{FOLDER}/original/config.json": "{}\n",
    f"models/{FOLDER}/original/params.json": "{}\n",
    f"models/{FOLDER}/original/consolidated.00.pth": "weights\n",
}
OWN_SUB_FOLDER_LISTING = (
    f"models/{FOLDER}/config.json\tmodels/{FOLDER}/model.safetensors\tmodels/{FOLDER}/original/config.json\n"
)
LOCAL_NESTED_FILES = {
    **MODEL_FILES,
    f"models/{FOLDER}/mtp/config.json": "{}\n",
    f"models/{FOLDER}/mtp/model.safetensors": "weights\n",
}


def make_repo(files, bucket=BUCKET):
    repo = StubRepo(files={**SHARED_BACKEND, **files})
    repo.stub("aws", "sts", "get-caller-identity", stdout=f"{ACCOUNT}\tarn:aws:iam::{ACCOUNT}:user/alice\n")
    repo.stub("terraform", "output", "-raw", "models_bucket", stdout=bucket)
    return repo


def script_bucket(repo, listing, list_exit=0):
    """The models/<FOLDER>/ marker list answers `listing` and `list_exit`; the sync succeeds."""
    repo.stub("aws", "s3api", "list-objects-v2", f"models/{FOLDER}/", stdout=listing, exit=list_exit)
    repo.stub("aws", "s3", "sync")


def aws_operations(repo):
    return [call.argv[1] for call in repo.calls() if call.tool == "aws" and call.argv[0] != "sts"]


class UploadTest(unittest.TestCase):
    def upload(self, repo, listing, folder=FOLDER):
        script_bucket(repo, listing)
        return repo.run(SCRIPT, "upload", folder, env=ENV)

    def test_upload_syncs_when_no_sub_folder_exists(self):
        with make_repo(MODEL_FILES) as repo:
            result = self.upload(repo, "None\tNone\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(aws_operations(repo), ["list-objects-v2", "sync"])
            calls = [c for c in repo.calls() if c.tool != "aws" or c.argv[0] != "sts"]
            self.assertEqual(
                [(c.tool, c.argv) for c in calls],
                [
                    ("terraform", [f"-chdir={repo.root}/shared/terraform", "output", "-raw", "models_bucket"]),
                    ("aws", list_call()),
                    ("aws", [*SYNC_CALL, f"{repo.root}/models/{FOLDER}/", f"s3://{BUCKET}/models/{FOLDER}/"]),
                ],
            )
            self.assertEqual(calls[0].env["TF_VAR_region"], REGION)

    def test_upload_allows_own_sub_folders_on_reupload(self):
        # S3 already holds models/<FOLDER>/original/ from the first upload, and the local folder
        # still has original/config.json: that file is this model's own, not another model.
        with make_repo(OWN_SUB_FOLDER_FILES) as repo:
            result = self.upload(repo, OWN_SUB_FOLDER_LISTING)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(aws_operations(repo), ["list-objects-v2", "sync"])

    def test_upload_refuses_a_nested_model_before_syncing(self):
        mtp_in_s3 = (
            f"models/{FOLDER}/config.json\tmodels/{FOLDER}/model.safetensors"
            f"\tmodels/{FOLDER}/mtp/config.json\tmodels/{FOLDER}/mtp/model.safetensors\n"
        )
        # (local files, S3 listing, message, whether the local check refuses before any CLI call)
        cases = {
            "weights below the folder in S3": (
                MODEL_FILES,
                f"models/{FOLDER}/config.json\tmodels/{FOLDER}/model.safetensors"
                f"\tmodels/{FOLDER}/Inner/config.json\tmodels/{FOLDER}/Inner/model-00001.safetensors\n",
                nested_message(), False),
            "fetch status object below own sub-folders in S3": (
                OWN_SUB_FOLDER_FILES,
                f"models/{FOLDER}/config.json\tmodels/{FOLDER}/Inner/drip-ai-fetch.json"
                f"\tmodels/{FOLDER}/original/config.json\n",
                nested_message("Inner", "drip-ai-fetch.json"), False),
            "sub-folder model in S3 only": (MODEL_FILES, mtp_in_s3, nested_message("mtp"), False),
            "sub-folder model in S3 and local": (LOCAL_NESTED_FILES, mtp_in_s3, LOCAL_NESTED_MESSAGE, True),
            "local nested model": (LOCAL_NESTED_FILES, "None\n", LOCAL_NESTED_MESSAGE, True),
        }
        for label, (files, listing, message, refused_locally) in cases.items():
            with self.subTest(label), make_repo(files) as repo:
                result = self.upload(repo, listing)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, "upload: " + message)
                if refused_locally:
                    self.assertEqual(repo.calls(), [])
                else:
                    self.assertIn(("aws", list_call()), [(c.tool, c.argv) for c in repo.calls()])
                    self.assertNotIn("sync", aws_operations(repo))

    def test_upload_allows_lone_sub_folder_config_in_s3(self):
        # S3 holds original/config.json that the local folder lacks. Without *.safetensors beside
        # it, sagemaker/start.sh reads it as this model's file, so upload accepts it too.
        with make_repo(MODEL_FILES) as repo:
            result = self.upload(repo, OWN_SUB_FOLDER_LISTING)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(aws_operations(repo), ["list-objects-v2", "sync"])

    def test_upload_refuses_when_the_list_fails(self):
        with make_repo(MODEL_FILES) as repo:
            script_bucket(repo, "", list_exit=255)
            result = repo.run(SCRIPT, "upload", FOLDER, env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                result.stderr,
                f"upload: could not list s3://{BUCKET}/models/{FOLDER}/; nothing was uploaded\n",
            )
            self.assertNotIn("sync", aws_operations(repo))

    def test_upload_fails_when_the_sync_fails(self):
        with make_repo(MODEL_FILES) as repo:
            repo.stub("aws", "s3api", "list-objects-v2", stdout="None\n")
            repo.stub("aws", "s3", "sync", exit=1)
            result = repo.run(SCRIPT, "upload", FOLDER, env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "upload: aws s3 sync failed; rerun to resume\n")

    def test_upload_refuses_when_the_bucket_is_unknown(self):
        with make_repo(MODEL_FILES, bucket="") as repo:
            result = repo.run(SCRIPT, "upload", FOLDER, env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "upload: no models bucket in the shared Terraform state\n")
            self.assertEqual(aws_operations(repo), [])

    def test_upload_validates_the_folder_before_any_cli_call(self):
        cases = [
            ("Org/Name", {f"models/Org/Name/{k.split('/', 2)[2]}": v for k, v in MODEL_FILES.items()},
             "upload: folder name must be one folder under models/: letters, digits, '.', '_', '-',"
             " starting with a letter or digit\n"),
            ("..", {}, "upload: folder name must be one folder under models/: letters, digits, '.', '_', '-',"
             " starting with a letter or digit\n"),
            ("-x", {}, "upload: folder name must be one folder under models/: letters, digits, '.', '_', '-',"
             " starting with a letter or digit\n"),
            (FOLDER, {f"models/{FOLDER}/model.safetensors": "w\n"},
             f"upload: models/{FOLDER}/config.json not found\n"),
            (FOLDER, {f"models/{FOLDER}/config.json": "{}\n", f"models/{FOLDER}/model.bin": "w\n"},
             f"upload: models/{FOLDER}/ has no *.safetensors files\n"),
        ]
        for folder, files, message in cases:
            with self.subTest(folder=folder, files=sorted(files)), make_repo(files) as repo:
                result = self.upload(repo, "None\n", folder=folder)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stderr, message)
                self.assertEqual(repo.calls(), [])

    def test_upload_needs_exactly_one_folder(self):
        with make_repo(MODEL_FILES) as repo:
            for args in (["upload"], ["upload", FOLDER, "extra"], ["bogus"]):
                self.assertEqual(repo.run(SCRIPT, *args, env=ENV).returncode, 2, args)
            self.assertEqual(repo.calls(), [])


class ListTest(unittest.TestCase):
    def level(self, repo, prefix, *entries):
        repo.stub("aws", "list-objects-v2", "--delimiter", prefix, stdout="\t".join(entries) + "\n")

    def test_lists_one_and_two_segment_models_with_their_origin(self):
        with make_repo({}) as repo:
            self.level(repo, "models/", "models/tiny/", "models/Org/", "models/empty/")
            self.level(repo, "models/tiny/", "models/tiny/config.json", "models/tiny/w.safetensors")
            self.level(repo, "models/Org/", "models/Org/", "models/Org/Big/", "models/Org/Half/")
            self.level(repo, "models/Org/Big/", "models/Org/Big/drip-ai-fetch.json", "models/Org/Big/config.json")
            self.level(repo, "models/Org/Half/", "None")
            self.level(repo, "models/empty/", "None")
            result = repo.run(SCRIPT, "list", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines()[:3], ["MODEL\tSOURCE", "tiny\tupload", "Org/Big\tfetch-model"])
            self.assertEqual(len(result.stdout.splitlines()), 4)
            lists = [c.argv for c in repo.calls() if c.tool == "aws" and "list-objects-v2" in c.argv]
            self.assertEqual(lists[0], [
                "s3api", "list-objects-v2", "--region", REGION, "--bucket", BUCKET, "--prefix", "models/",
                "--delimiter", "/", "--query", "[Contents[].Key, CommonPrefixes[].Prefix][]", "--output", "text",
            ])

    def test_empty_bucket(self):
        with make_repo({}) as repo:
            self.level(repo, "models/", "None")
            result = repo.run(SCRIPT, "list", env=ENV)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"MODEL\tSOURCE\nNo models in s3://{BUCKET}/models/.\n")

    def test_list_failure_exits_non_zero(self):
        with make_repo({}) as repo:
            repo.stub("aws", "list-objects-v2", exit=255, stderr="AccessDenied\n")
            result = repo.run(SCRIPT, "list", env=ENV)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"list: could not list s3://{BUCKET}/models/", result.stderr)


if __name__ == "__main__":
    unittest.main()
