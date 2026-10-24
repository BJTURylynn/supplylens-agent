import json

import pytest

from supplylens.config import ROOT, Settings
from supplylens.evaluation import evaluate_cases


async def test_independent_acceptance_cases_and_artifact(tmp_path):
    output = tmp_path / "demo.json"
    result = await evaluate_cases(Settings(_env_file=None), "demo", ROOT / "evaluation/cases.json", output)
    failures = [(c["id"], c["errors"]) for c in result["cases"] if not c["passed"]]
    assert not failures, failures
    assert result["summary"]["total"] == result["summary"]["passed"] == 30
    assert result["summary"]["live_cloud_cases"] == 0
    assert result["summary"]["input_tokens"] is None
    assert json.loads(output.read_text(encoding="utf-8"))["summary"] == result["summary"]


async def test_cloud_evaluation_requires_key_and_writes_no_fake_results(tmp_path):
    output = tmp_path / "cloud.json"
    with pytest.raises(ValueError, match="MODEL_API_KEY"):
        await evaluate_cases(Settings(_env_file=None, model_api_key=""), "cloud", ROOT / "evaluation/cases.json", output)
    assert not output.exists()
