"""
Profile isolation between the serving path (LLM_*) and the extraction pipeline
(PIPELINE_LLM_*), plus the JSON-coercion contract both extractors depend on.

The property worth guarding hardest is the *non*-inheritance one: a .env that
points /ask at a paid provider must leave extraction on local Ollama. Getting
that backwards silently bills a whole-corpus re-extraction, so it gets an
explicit test rather than being left to the config defaults.
"""
from pathlib import Path

import pytest

import config
from llm.errors import LLMConfigError
from llm.factory import _resolve, get_provider, reset_provider_cache
from pipeline import llm_json


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_provider_cache()
    yield
    reset_provider_cache()


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def test_extract_profile_reads_pipeline_namespace(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "PIPELINE_LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setattr(config, "PIPELINE_LLM_API_KEY", "sk-pipeline")

    p = _resolve("extract")
    assert p.provider == "openai"
    assert p.model == "gpt-4o-mini"
    assert p.api_key == "sk-pipeline"


def test_generate_profile_ignores_pipeline_namespace(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-3.5-flash")
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "PIPELINE_LLM_MODEL", "gpt-4o-mini")

    assert _resolve("generate").provider == "gemini"
    assert _resolve("generate").model == "gemini-3.5-flash"


def test_extract_profile_never_reads_the_ask_namespace(monkeypatch):
    """The expensive-mistake guard: no LLM_* value may reach "extract".

    Uses sentinels rather than asserting extract == ollama, so the test holds
    whatever PIPELINE_LLM_* happens to be set to in the developer's own .env.
    """
    sentinels = {
        "LLM_PROVIDER": "SENTINEL-provider",
        "LLM_MODEL":    "SENTINEL-model",
        "LLM_BASE_URL": "SENTINEL-baseurl",
        "LLM_API_KEY":  "SENTINEL-key",
    }
    for name, value in sentinels.items():
        monkeypatch.setattr(config, name, value)

    p = _resolve("extract")
    resolved = (p.provider, p.model, p.base_url, p.api_key)
    leaked = [name for name, value in sentinels.items() if value in resolved]
    assert not leaked, f"extract profile leaked {leaked} from the /ask namespace"

    # The sentinels must be live, or the assertion above proves nothing.
    assert _resolve("generate").provider == "SENTINEL-provider"


def test_pipeline_defaults_fall_back_to_ollama_in_source():
    """Guards the default itself, which an ambient .env would otherwise mask."""
    src = Path(config.__file__).read_text(encoding="utf-8").splitlines()

    provider_line = next(l for l in src if l.startswith("PIPELINE_LLM_PROVIDER"))
    assert '"ollama"' in provider_line

    model_line = next(l for l in src if l.startswith("PIPELINE_LLM_MODEL"))
    assert "OLLAMA_MODEL" in model_line

    # Neither may be defaulted off the serving-path namespace.
    for line in (provider_line, model_line):
        assert "LLM_PROVIDER)" not in line and "LLM_MODEL)" not in line.replace(
            "OLLAMA_MODEL)", ""
        )


@pytest.mark.parametrize(
    "pipeline_provider,ask_provider",
    [
        ("ollama", "gemini"),
        ("openai", "gemini"),
        ("gemini", "openai"),
        ("ollama", "ollama"),
    ],
)
def test_pipeline_and_ask_resolve_independently(monkeypatch, pipeline_provider, ask_provider):
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", pipeline_provider)
    monkeypatch.setattr(config, "LLM_PROVIDER", ask_provider)

    assert _resolve("extract").provider == pipeline_provider
    assert _resolve("generate").provider == ask_provider


def test_extract_profile_is_more_patient_and_retries_more():
    """Batch extraction meets rate limits a single /ask never does."""
    extract, generate = _resolve("extract"), _resolve("generate")
    assert extract.read_timeout >= generate.read_timeout
    assert extract.max_retries > generate.max_retries


def test_route_profile_still_fails_fast():
    route = _resolve("route")
    assert route.max_retries == 0
    assert route.read_timeout <= 10


def test_unknown_profile_raises():
    with pytest.raises(LLMConfigError, match="Unknown LLM profile"):
        _resolve("nonsense")


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", "llamafile")
    with pytest.raises(LLMConfigError, match="Unknown provider"):
        get_provider("extract")


def test_extract_and_generate_get_separate_handles(monkeypatch):
    """lru_cache keys on profile, so the two paths cannot share a client."""
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "PIPELINE_LLM_MODEL", "qwen2.5:14b")
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "LLM_MODEL", "llama3.1:8b")

    assert get_provider("extract").model == "qwen2.5:14b"
    assert get_provider("generate").model == "llama3.1:8b"


def test_anthropic_refuses_json_mode(monkeypatch):
    """Anthropic has no JSON mode; it must fail loudly, not return prose.

    This is why anthropic is unusable as PIPELINE_LLM_PROVIDER.
    """
    anthropic = pytest.importorskip("anthropic")  # noqa: F841
    monkeypatch.setattr(config, "PIPELINE_LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "PIPELINE_LLM_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(config, "PIPELINE_LLM_API_KEY", "sk-ant-test")

    provider = get_provider("extract")
    with pytest.raises(LLMConfigError, match="no JSON mode"):
        provider.complete(
            system="s",
            messages=[{"role": "user", "content": "u"}],
            temperature=0,
            max_tokens=16,
            json_mode=True,
        )


# ---------------------------------------------------------------------------
# JSON coercion — the contract both extractors were built against
# ---------------------------------------------------------------------------

KEYS = ("items", "votes", "financial_items", "personnel_actions",
        "results", "data", "extractions")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('[{"a": 1}]',                     [{"a": 1}]),
        ('```json\n[{"a": 1}]\n```',       [{"a": 1}]),
        ('```\n[{"a": 1}]```',             [{"a": 1}]),
        ('{"votes": [{"a": 1}]}',          [{"a": 1}]),
        ('{"extractions": [{"a": 1}]}',    [{"a": 1}]),
        ('{"a": 1}',                       [{"a": 1}]),   # bare dict -> wrapped
        ('{"votes": null}',                [{"votes": None}]),
        ('not json at all',                []),
        ('"a string"',                     []),
        ('',                               []),
    ],
)
def test_parse_shapes(raw, expected):
    assert llm_json._parse(raw, KEYS, "") == expected


def test_unwrap_key_order_is_respected():
    raw = '{"data": [{"second": 1}], "items": [{"first": 1}]}'
    assert llm_json._parse(raw, KEYS, "") == [{"first": 1}]


def test_bad_json_counts_as_a_failure():
    llm_json.reset_usage()
    llm_json._parse("{oops", KEYS, "")
    assert llm_json.usage().failures == 1


def test_truncation_is_counted_and_logged(monkeypatch, caplog):
    """A clipped response is invalid JSON and yields zero rows — say so loudly."""
    class _Truncating:
        provider, model = "fake", "fake-1"

        def complete(self, **kw):
            from llm.base import LLMResult
            return LLMResult(
                text='[{"a": 1',           # cut off mid-array
                model=self.model,
                provider=self.provider,
                finish_reason="length",
            )

    monkeypatch.setattr(llm_json, "get_provider", lambda profile: _Truncating())
    llm_json.reset_usage()

    with caplog.at_level("WARNING"):
        rows = llm_json.call_json(system="s", prompt="p", unwrap_keys=KEYS, label="votes")

    assert rows == []
    assert llm_json.usage().truncations == 1
    assert "TRUNCATED" in caplog.text


def test_auth_error_aborts_the_batch(monkeypatch):
    """A bad key must stop the run, not empty every meeting silently."""
    from llm.errors import LLMAuthError

    class _Unauthorized:
        provider, model = "fake", "fake-1"

        def complete(self, **kw):
            raise LLMAuthError("401")

    monkeypatch.setattr(llm_json, "get_provider", lambda profile: _Unauthorized())

    with pytest.raises(LLMAuthError):
        llm_json.call_json(system="s", prompt="p", unwrap_keys=KEYS)


def test_transient_error_retries_then_returns_empty(monkeypatch):
    """One bad window must not kill a long batch."""
    from llm.errors import LLMTimeout

    calls = {"n": 0}

    class _AlwaysTimesOut:
        provider, model = "fake", "fake-1"

        def complete(self, **kw):
            calls["n"] += 1
            raise LLMTimeout("too slow")

    monkeypatch.setattr(llm_json, "get_provider", lambda profile: _AlwaysTimesOut())
    monkeypatch.setattr(config, "PIPELINE_LLM_MAX_RETRIES", 2)
    monkeypatch.setattr(llm_json.time, "sleep", lambda s: None)
    llm_json.reset_usage()

    assert llm_json.call_json(system="s", prompt="p", unwrap_keys=KEYS) == []
    assert calls["n"] == 3            # initial attempt + 2 retries
    assert llm_json.usage().failures == 1


def test_usage_accumulates_tokens(monkeypatch):
    class _Counting:
        provider, model = "fake", "fake-1"

        def complete(self, **kw):
            from llm.base import LLMResult
            return LLMResult(
                text="[]", model=self.model, provider=self.provider,
                prompt_tokens=100, completion_tokens=20, finish_reason="stop",
            )

    monkeypatch.setattr(llm_json, "get_provider", lambda profile: _Counting())
    llm_json.reset_usage()

    for _ in range(3):
        llm_json.call_json(system="s", prompt="p", unwrap_keys=KEYS)

    u = llm_json.usage()
    assert (u.calls, u.prompt_tokens, u.completion_tokens, u.total_tokens) == (3, 300, 60, 360)
    assert u.failures == 0
