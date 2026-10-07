"""Pins `shared/model-profiles/regenerate_architectures.py` and the committed architecture list."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROFILES_DIR = Path(__file__).resolve().parents[1] / "model-profiles"
SCRIPT = PROFILES_DIR / "regenerate_architectures.py"
REGISTRY = """
import torch
_TEXT_GENERATION_MODELS = {
    "ExampleForCausalLM": ("example", "ExampleForCausalLM"),
    "OtherForCausalLM": ("other", "OtherForCausalLM"),
}
_EMBEDDING_MODELS = {"ExampleEmbeddingModel": ("embedding", "ExampleEmbeddingModel")}
_MULTIMODAL_MODELS = {"ExampleForConditionalGeneration": ("multimodal", "ExampleForConditionalGeneration")}
_TRANSFORMERS_SUPPORTED_MODELS = {"ExampleTransformersForCausalLM": ("transformers", "TransformersForCausalLM"),
    "ExampleForCausalLM": ("transformers", "ExampleForCausalLM")}
_VLLM_MODELS = {**_TEXT_GENERATION_MODELS, **_EMBEDDING_MODELS}
"""


def regenerate(registry_text, ref="v9.9.9"):
    """Runs the script offline on `registry_text`; returns (process, written document or None)."""
    with tempfile.TemporaryDirectory() as tmp:
        registry, output = Path(tmp) / "registry.py", Path(tmp) / "out.json"
        registry.write_text(registry_text)
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), ref, "--registry", str(registry), "--output", str(output)],
            capture_output=True, text=True, timeout=60,
        )
        return proc, json.loads(output.read_text()) if output.exists() else None


class RegenerateTest(unittest.TestCase):
    def test_lists_only_generative_architectures(self):
        proc, document = regenerate(REGISTRY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(document["vllm_ref"], "v9.9.9")
        self.assertIn("/vllm-project/vllm/v9.9.9/vllm/model_executor/models/registry.py", document["source"])
        self.assertEqual(document["architectures"], [
            "ExampleForCausalLM", "ExampleForConditionalGeneration",
            "ExampleTransformersForCausalLM", "OtherForCausalLM"])

    def test_refuses_a_registry_missing_a_table(self):
        for table in ("_TEXT_GENERATION_MODELS", "_MULTIMODAL_MODELS", "_TRANSFORMERS_SUPPORTED_MODELS"):
            with self.subTest(table=table):
                proc, document = regenerate(REGISTRY.replace(table, "_RENAMED_MODELS"))
                self.assertEqual(proc.returncode, 1)
                self.assertIn(f"has no non-empty {table}", proc.stderr)
                self.assertIsNone(document)

    def test_committed_list_is_v0_30_0_and_has_sorted_unique_architectures(self):
        document = json.loads((PROFILES_DIR / "vllm-architectures.json").read_text())
        self.assertEqual(document["vllm_ref"], "v0.30.0")
        self.assertEqual(document["tables"], [
            "_TEXT_GENERATION_MODELS", "_MULTIMODAL_MODELS", "_TRANSFORMERS_SUPPORTED_MODELS"])
        self.assertTrue(document["architectures"])
        self.assertTrue(all(isinstance(name, str) and name for name in document["architectures"]))
        self.assertEqual(document["architectures"], sorted(set(document["architectures"])))

    def test_bundled_profiles_have_no_chosen_model(self):
        self.assertEqual(json.loads((PROFILES_DIR / "profiles.json").read_text()), {})


if __name__ == "__main__":
    unittest.main()
