"""Generate an opt-in catalog from a selected installed Codex binary."""

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess

MODELS = ("gpt-6-astra", "gpt-6.1-sol")


def build_catalog(source, limits, version, max_limits=None):
    max_limits = max_limits or {}
    entries = {entry["slug"]: entry for entry in source["models"]}
    models = []
    overrides = {
        "use_responses_lite": False, "tool_mode": "direct", "apply_patch_tool_type": None,
        "supports_search_tool": False, "supports_reasoning_effort_updates": False,
        "supports_experimental_context": False, "input_modalities": ["text"],
        "experimental_supported_tools": [], "node_repl_disabled": True,
        "supports_image_detail_original": False,
        "default_reasoning_level": "low",
    }
    for slug in MODELS:
        limit = limits.get(slug)
        if type(limit) is not int or limit <= 0:
            raise ValueError(f"A positive integer context limit is required for {slug}")
        extended = max_limits.get(slug)
        if slug in max_limits and (type(extended) is not int or extended < limit):
            raise ValueError(f"The extended context limit must be an integer at least the base limit for {slug}")
        if slug not in entries:
            raise ValueError(f"Bundled catalog has no model entry for {slug}")
        model = copy.deepcopy(entries[slug])
        model.update(overrides)
        model.update(context_window=limit, max_context_window=extended,
                     auto_compact_token_limit=limit * 9 // 10,
                     service_tiers=[], additional_speed_tiers=[], availability_nux=None, upgrade=None)
        levels = {level["effort"]: level for level in model["supported_reasoning_levels"]}
        if not all(level in levels for level in ("low", "high")):
            raise ValueError(f"Bundled {slug} entry lacks low/high reasoning levels")
        model["supported_reasoning_levels"] = [levels["low"], levels["high"]]
        models.append(model)
    digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"models": models, "provenance": {"codex_version": version,
            "source_catalog_sha256": digest, "source": "codex debug models --bundled",
            "context_limits": limits, "max_context_limits": max_limits,
            "limits_source": "operator-supplied; SAP capacity unverified", "overrides": overrides}}


def installed_catalog(binary="codex"):
    version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    source = subprocess.run([binary, "debug", "models", "--bundled"], check=True,
                            capture_output=True, text=True, timeout=30).stdout
    return json.loads(source), version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Selected installed Codex executable")
    parser.add_argument("--astra-context-limit", required=True, type=int)
    parser.add_argument("--sol-context-limit", required=True, type=int)
    parser.add_argument("--astra-max-context-limit", type=int)
    parser.add_argument("--sol-max-context-limit", type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        source, version = installed_catalog(args.codex)
        extended = {slug: value for slug, value in zip(MODELS,
                    (args.astra_max_context_limit, args.sol_max_context_limit)) if value is not None}
        catalog = build_catalog(source, dict(zip(MODELS, (args.astra_context_limit, args.sol_context_limit))), version, extended)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(catalog, output, indent=2, ensure_ascii=False)
            output.write("\n")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Catalog generation failed: {type(error).__name__}: {error}\n")
    print(json.dumps(catalog["provenance"], indent=2))


if __name__ == "__main__":
    main()
