"""Catalog generation uses operator limits and preserves bundled model instructions."""

import copy
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest


def generator():
    path = Path(__file__).resolve().parents[1] / "examples/codex/generate_catalog.py"
    spec = importlib.util.spec_from_file_location("generate_catalog", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CatalogTests(unittest.TestCase):
    def source(self):
        return {"models": [
            {"slug": slug, "base_instructions": "instructions for " + slug,
             "model_messages": {"instructions_template": "original"}, "shell_type": "unified_exec",
             "use_responses_lite": True, "tool_mode": "code_mode_only", "apply_patch_tool_type": "freeform",
             "context_window": 272000, "max_context_window": 872000,
             "service_tiers": [{"id": "priority"}], "additional_speed_tiers": ["fast"],
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]}
            for slug in ("gpt-6-astra", "gpt-6.1-sol", "gpt-5.4")]}

    def test_two_models_preserve_instructions_with_compatible_capabilities(self):
        module = generator()
        source = self.source()
        original = copy.deepcopy(source)
        output = module.build_catalog(source, {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000}, "codex-cli 0.164.0")
        self.assertEqual([m["slug"] for m in output["models"]], ["gpt-6-astra", "gpt-6.1-sol"])
        for model in output["models"]:
            self.assertEqual(model["base_instructions"], "instructions for " + model["slug"])
            self.assertEqual(model["model_messages"], {"instructions_template": "original"})
            self.assertEqual(model["shell_type"], "unified_exec")
            self.assertEqual(model["tool_mode"], "direct")
            self.assertFalse(model["use_responses_lite"])
            self.assertIsNone(model["apply_patch_tool_type"])
            self.assertFalse(model["supports_search_tool"])
            self.assertFalse(model["supports_reasoning_effort_updates"])
            self.assertEqual([v["effort"] for v in model["supported_reasoning_levels"]], ["low", "high"])
            self.assertEqual(model["default_reasoning_level"], "low")
            self.assertEqual(model["input_modalities"], ["text"])
        self.assertEqual(source, original)
        self.assertEqual(output["provenance"]["codex_version"], "codex-cli 0.164.0")
        self.assertEqual(len(output["provenance"]["source_catalog_sha256"]), 64)

    def test_missing_model_entry_fails_instead_of_fabricating_model(self):
        source = self.source()
        source["models"] = source["models"][:1]
        with self.assertRaisesRegex(ValueError, "gpt-6.1-sol"):
            generator().build_catalog(source, {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000}, "synthetic")

    def test_limits_are_required_positive_integers(self):
        for limits in ({}, {"gpt-6-astra": 32000},
                       {"gpt-6-astra": 0, "gpt-6.1-sol": 64000},
                       {"gpt-6-astra": True, "gpt-6.1-sol": 64000},
                       {"gpt-6-astra": -1, "gpt-6.1-sol": 64000},
                       {"gpt-6-astra": "32000", "gpt-6.1-sol": 64000}):
            with self.subTest(limits=limits), self.assertRaisesRegex(ValueError, "context limit"):
                generator().build_catalog(self.source(), limits, "synthetic")

    def test_supplied_limits_derive_compaction_and_clear_first_party_capacity(self):
        output = generator().build_catalog(self.source(), {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000}, "synthetic")
        for model, limit, compaction in zip(output["models"], (32000, 64000), (28800, 57600)):
            self.assertEqual(model["context_window"], limit)
            self.assertIsNone(model["max_context_window"])
            self.assertEqual(model["auto_compact_token_limit"], compaction)
            self.assertEqual(model["service_tiers"], [])
            self.assertEqual(model["additional_speed_tiers"], [])
        self.assertEqual(output["provenance"]["context_limits"], {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000})

    def test_explicit_extended_limits_preserved_with_operator_provenance(self):
        base = {"gpt-6-astra": 272000, "gpt-6.1-sol": 272000}
        extended = {"gpt-6-astra": 872000, "gpt-6.1-sol": 872000}
        output = generator().build_catalog(self.source(), base, "codex-cli 0.164.0", extended)
        for model in output["models"]:
            self.assertEqual(model["context_window"], 272000)
            self.assertEqual(model["max_context_window"], 872000)
            self.assertEqual(model["auto_compact_token_limit"], 244800)
        self.assertEqual(output["provenance"]["max_context_limits"], extended)
        self.assertEqual(output["provenance"]["limits_source"], "operator-supplied; SAP capacity unverified")

    def test_extended_limit_must_be_integer_at_least_base_limit(self):
        for value in (True, 0, -1, 31999, "872000"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "extended context limit"):
                generator().build_catalog(self.source(), {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000},
                                          "fixture", {"gpt-6-astra": value})

    @unittest.skipUnless(shutil.which("codex"), "Selected Codex binary unavailable: catalog loading blocked")
    def test_generated_catalog_loads_through_installed_binary_in_temporary_home(self):
        module = generator()
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, CODEX_HOME=directory)
            source = subprocess.run(["codex", "debug", "models", "--bundled"], env=env,
                                    capture_output=True, text=True, check=True, timeout=30)
            catalog = module.build_catalog(json.loads(source.stdout),
                {"gpt-6-astra": 272000, "gpt-6.1-sol": 272000}, "test-binary",
                {"gpt-6-astra": 872000, "gpt-6.1-sol": 872000})
            path = Path(directory) / "models.json"
            path.write_text(json.dumps(catalog))
            result = subprocess.run(["codex", "-c", f'model_catalog_json={json.dumps(str(path))}',
                "-c", 'model_provider="fixture"', "-c", 'model_providers.fixture.name="Fixture"',
                "-c", 'model_providers.fixture.base_url="http://127.0.0.1:9/v1"',
                "-c", 'model_providers.fixture.wire_api="responses"',
                "-c", 'model_providers.fixture.requires_openai_auth=false', "debug", "models"],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            models = json.loads(result.stdout)["models"]
            self.assertEqual([model["slug"] for model in models], ["gpt-6-astra", "gpt-6.1-sol"])
            self.assertEqual([model["context_window"] for model in models], [272000, 272000])
            self.assertEqual([model["max_context_window"] for model in models], [872000, 872000])
            self.assertEqual([model["auto_compact_token_limit"] for model in models], [244800, 244800])

    @unittest.skipUnless(shutil.which("codex"), "Codex unavailable: example loading blocked")
    def test_example_strict_configuration_loads_and_uses_proxy_token_env(self):
        module = generator()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            env = dict(os.environ, CODEX_HOME=directory)
            env.pop("SAP_AI_PROXY_API_KEY", None)
            bundled = subprocess.run(["codex", "debug", "models", "--bundled"], env=env,
                                     capture_output=True, text=True, check=True, timeout=30)
            catalog_path = root / "models.json"
            catalog_path.write_text(json.dumps(module.build_catalog(json.loads(bundled.stdout),
                {"gpt-6-astra": 32000, "gpt-6.1-sol": 64000}, "fixture")))
            example = (Path(__file__).resolve().parents[1] / "examples/codex/config.toml.example").read_text()
            example = example.replace("/absolute/path/to/sap-ai-core-llm-proxy/examples/codex/models.json", str(catalog_path))
            (root / "config.toml").write_text(example)
            result = subprocess.run(["codex", "exec", "--strict-config", "--skip-git-repo-check",
                "--ephemeral", "-C", str(workspace), "synthetic configuration check"],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SAP_AI_PROXY_API_KEY", result.stderr)
            self.assertNotIn("Unknown config", result.stderr)
            self.assertNotIn("failed to parse", result.stderr)
