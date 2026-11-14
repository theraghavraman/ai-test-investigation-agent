import json

from app.agent import investigate


class FakeItem:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def model_dump(self, exclude_none=True):
        return dict(self.kwargs)


class FakeResponse:
    def __init__(self, calls=None, analysis=None):
        self.output = [FakeItem(type="function_call", name=name, arguments=json.dumps(args), call_id=f"call_{i}")
                       for i, (name, args) in enumerate(calls or [])]
        self.output_text = json.dumps(analysis) if analysis else ""


class FakeClient:
    def __init__(self, items):
        self.items = iter(items)
        self.requests = []
        self.responses = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.items)


def test_stateless_function_loop(toolset, settings):
    answer = {"summary": "one test passed", "suspected_causes": [],
              "recommendations": ["Add edge cases"], "limitations": ["No Docker test run was requested"]}
    client = FakeClient([
        FakeResponse([("read_file", {"path": "src/module.py", "offset": 0, "max_chars": 100})]),
        FakeResponse([("write_test", {"code": "def test_one():\n    assert True\n"})]),
        FakeResponse(analysis=answer),
    ])
    result = investigate(settings, toolset, "Check module behavior against supplied file", client=client)
    assert result["analysis"] == answer
    assert result["rounds"] == 3 and result["tool_calls"] == 2
    assert len(toolset.generated) == 1
    assert client.requests[0]["store"] is False
    assert "reasoning.encrypted_content" in client.requests[0]["include"]
    assert client.requests[0]["text"]["format"]["strict"] is True
    assert any(item.get("type") == "function_call_output"
               for item in client.requests[1]["input"])


def test_final_model_answer_forced_when_round_limit(toolset, settings):
    settings.max_tool_rounds = 1
    conclusion = {"summary": "limited run", "suspected_causes": [],
                  "recommendations": [], "limitations": ["tool rounds exhausted"]}
    client = FakeClient([
        FakeResponse([("read_file", {"path": "src/module.py", "offset": 0, "max_chars": 100})]),
        FakeResponse(analysis=conclusion),
    ])
    result = investigate(settings, toolset, "Inspect approved file and stop after round quota", client=client)
    assert result["analysis"] == conclusion
    assert client.requests[-1]["tool_choice"] == "none"
    assert len(client.requests) == 2


def test_local_endpoint_omits_openai_only_options(toolset, settings):
    settings.llm_base_url = "http://host.docker.internal:1234/v1"
    answer = {"summary": "ok", "suspected_causes": [], "recommendations": [], "limitations": []}
    client = FakeClient([
        FakeResponse([("read_file", {"path": "src/module.py", "offset": 0, "max_chars": 100})]),
        FakeResponse(), FakeResponse(), FakeResponse(),  # prose, then two reminders ignored
        FakeResponse(analysis=answer),
    ])
    result = investigate(settings, toolset, "Check module behavior with a local model", client=client)
    assert result["analysis"] == answer
    reminders = [item for item in client.requests[-1]["input"]
                 if item.get("role") == "user" and "not written any test" in str(item.get("content"))]
    assert len(reminders) == 2
    assert all("include" not in request for request in client.requests)
    assert all(request["temperature"] == settings.llm_temperature for request in client.requests)
    assert client.requests[0]["store"] is False
    # The schema is withheld while tools are in play and sent only with the final no-tools request.
    assert "text" not in client.requests[0] and "text" not in client.requests[1]
    assert client.requests[-1]["tool_choice"] == "none"
    assert client.requests[-1]["text"]["format"]["strict"] is True


def test_local_free_text_answer_is_kept(toolset, settings):
    settings.llm_base_url = "http://host.docker.internal:1234/v1"
    fenced = FakeResponse()
    fenced.output_text = 'Here you go:\n```json\n{"summary": "s", "suspected_causes": [], ' \
                         '"recommendations": [], "limitations": []}\n```'
    result = investigate(settings, toolset, "Check module behavior with a local model",
                         client=FakeClient([FakeResponse(), FakeResponse(), FakeResponse(), fenced]))
    assert result["analysis"]["summary"] == "s"
    plain = FakeResponse()
    plain.output_text = "divide() raises ZeroDivisionError as the spec requires."
    result = investigate(settings, toolset, "Check module behavior with a local model",
                         client=FakeClient([FakeResponse(), FakeResponse(), FakeResponse(), plain]))
    assert result["analysis"]["summary"].startswith("divide() raises")
    assert "free text" in result["analysis"]["limitations"][0]


def test_local_endpoint_needs_no_api_key(toolset, settings, monkeypatch):
    import openai
    from pydantic import SecretStr

    created = {}

    class StubOpenAI:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.responses = FakeClient([FakeResponse(), FakeResponse(), FakeResponse(), FakeResponse(analysis={
                "summary": "ok", "suspected_causes": [], "recommendations": [], "limitations": []})])

    monkeypatch.setattr(openai, "OpenAI", StubOpenAI)
    settings.openai_api_key = SecretStr("")
    settings.llm_base_url = "http://host.docker.internal:1234/v1"
    investigate(settings, toolset, "Check module behavior with a local model")
    assert created["base_url"] == "http://host.docker.internal:1234/v1"
    assert created["api_key"]


def test_no_key_and_no_local_endpoint_is_rejected(toolset, settings):
    import pytest
    from pydantic import SecretStr

    settings.openai_api_key = SecretStr("")
    with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
        investigate(settings, toolset, "Check module behavior without any model")


def test_llm_base_url_validation(tmp_path):
    import pytest

    from app.settings import Settings

    base = dict(_env_file=None, api_key="unit_test_api_key_value_at_least_32_chars", data_dir=tmp_path)
    assert Settings(**base, llm_base_url=" http://localhost:1234/v1/ ").llm_base_url == "http://localhost:1234/v1"
    assert Settings(**base, llm_base_url="").llm_base_url == ""
    with pytest.raises(ValueError):
        Settings(**base, llm_base_url="localhost:1234")
