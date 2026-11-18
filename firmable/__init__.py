"""Firmable take-home: agentic schema inference and entity identification."""

from pathlib import Path

# Every module finds its files through these paths, so commands work from any working directory.
ROOT = Path(__file__).resolve().parent.parent   # the repo root: the folder that contains firmable/
DATA = ROOT / "data"                            # working files: API cache, raw downloads, agent run state
OUTPUTS = ROOT / "outputs"                      # results: shortlist, observations, links, profiles, audit
CHECKS = ROOT / "checks"                        # hand-check sheets that a person fills in
CONFIGS = ROOT / "configs"                      # one agent-written mapping config (YAML) per source
ONTOLOGY_PATH = ROOT / "firmable_ontology.yaml" # the canonical fields every config has to map to
