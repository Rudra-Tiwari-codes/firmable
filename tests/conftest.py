import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Point every stage at a temp folder, so tests can never touch configs/, outputs/ or checks/."""
    from firmable import agent, audit, inspect_tools, link, profile, review, triage

    configs, outputs, checks, raw, runs = (tmp_path / d for d in ("configs", "outputs", "checks", "raw", "runs"))
    for d in (configs, outputs, checks, raw, runs):
        d.mkdir()
    for mod, attr, value in [
        (agent, "CONFIGS", configs), (agent, "OUTPUTS", outputs), (agent, "RUNS", runs), (agent, "ROOT", tmp_path),
        (review, "CONFIGS", configs), (review, "OUTPUTS", outputs), (review, "REVIEW_DIR", outputs / "review"),
        (inspect_tools, "RAW_DIR", raw),
        (link, "CONFIGS", configs), (link, "CHECKS", checks), (link, "LINK_DIR", outputs / "links"),
        (profile, "CONFIGS", configs), (profile, "LINK_DIR", outputs / "links"), (profile, "PROFILE_DIR", outputs / "profiles"),
        (audit, "CONFIGS", configs), (audit, "AUDIT_DIR", outputs / "audit"),
        (triage, "CHECKS", checks), (triage, "OUTPUTS", outputs),
    ]:
        monkeypatch.setattr(mod, attr, value)
    return tmp_path
