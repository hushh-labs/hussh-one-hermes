"""LM Studio's loaded window is the context config, not a hand-kept list.

Two field failures motivated this, and they are opposite halves of one bug:

  * A configured ``context_length`` of 262144 for a model LM Studio had loaded
    at 131072. Hermes grew the request to ~132k and the server rejected it with
    a hard ``exceed_context_size_error``. Compression could not recover: the
    request was legal by Hermes' accounting and illegal by the server's.
  * A model with no entry at all fell through to the generic default, and the
    session died with "cannot compress further" while ~75,000 tokens of loaded
    window sat unused.

The static entry was not even wrong about the model's *ceiling* — it matched
``max_context_length`` exactly. It was wrong about what had been **loaded**,
which is the only number that bounds a request, and the only one a config file
structurally cannot track (it changes on every reload).
"""

import agent.model_metadata as mm


BASE_URL = "http://127.0.0.1:1234/v1"


def _payload(key, *, max_ctx, loaded_ctx=None):
    entry = {"key": key, "max_context_length": max_ctx}
    if loaded_ctx is not None:
        entry["loaded_instances"] = [{"config": {"context_length": loaded_ctx}}]
    return {"models": [entry]}


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _fake_lmstudio(monkeypatch, payload):
    """Point every LM Studio probe path at *payload* and nothing else."""
    monkeypatch.setattr(mm, "detect_local_server_type", lambda *a, **k: "lm-studio")
    monkeypatch.setattr(mm, "_endpoint_blackholed", lambda *a, **k: False)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, *a, **k):
            if url.endswith("/api/v1/models"):
                return _Resp(payload)
            return _Resp({})

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)
    monkeypatch.setattr(mm.requests, "get", lambda url, **k: _Resp(payload))


def _clear_caches():
    mm._LMSTUDIO_LOADED_CTX_CACHE.clear()
    mm._LOCAL_CTX_PROBE_CACHE.clear()
    mm._endpoint_model_metadata_cache.clear()
    mm._endpoint_model_metadata_cache_time.clear()


def _providers(models):
    return [{"name": "lmstudio", "base_url": BASE_URL, "models": models}]


class TestLoadedWindowOutranksStaticConfig:
    def test_overstated_config_loses_to_loaded_window(self, monkeypatch):
        """The HTTP-400 shape: config claims more than the server allocated."""
        _clear_caches()
        _fake_lmstudio(
            monkeypatch, _payload("meta/m", max_ctx=262144, loaded_ctx=131072)
        )
        stale = _providers({"meta/m": {"context_length": 262144}})

        # The old resolution order really would have returned the stale value.
        from hermes_cli.config import get_custom_provider_context_length

        assert (
            get_custom_provider_context_length(
                model="meta/m", base_url=BASE_URL, custom_providers=stale
            )
            == 262144
        )

        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                custom_providers=stale,
            )
            == 131072
        )

    def test_missing_config_entry_still_gets_the_loaded_window(self, monkeypatch):
        """The 'cannot compress further' shape: no entry, capacity stranded."""
        _clear_caches()
        _fake_lmstudio(
            monkeypatch, _payload("meta/m", max_ctx=131072, loaded_ctx=131072)
        )
        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                custom_providers=_providers({}),
            )
            == 131072
        )

    def test_max_context_length_never_substitutes_for_loaded(self, monkeypatch):
        """A model loaded BELOW its ceiling must resolve to the ceiling's floor.

        This is the regression that would silently reintroduce the overshoot:
        reading ``max_context_length`` when a loaded instance exists.
        """
        _clear_caches()
        _fake_lmstudio(
            monkeypatch, _payload("meta/m", max_ctx=262144, loaded_ctx=32768)
        )
        assert mm._lmstudio_loaded_context_length("meta/m", BASE_URL) == 32768

    def test_explicit_model_context_length_remains_the_escape_hatch(
        self, monkeypatch
    ):
        _clear_caches()
        _fake_lmstudio(
            monkeypatch, _payload("meta/m", max_ctx=262144, loaded_ctx=131072)
        )
        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                config_context_length=100000,
                custom_providers=_providers({"meta/m": {"context_length": 262144}}),
            )
            == 100000
        )


class TestColdStart:
    def test_unloaded_model_falls_back_to_the_server_reported_ceiling(
        self, monkeypatch
    ):
        """Before this, a cold start overshot on the very first turn.

        No loaded instance means no allocation to read, and the generic 256K
        default is larger than most local models — so the first request of a
        fresh session blew the window. The server's own ceiling is a real
        bound and strictly better than a guess.
        """
        _clear_caches()
        _fake_lmstudio(monkeypatch, _payload("meta/m", max_ctx=131072))
        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                custom_providers=_providers({}),
            )
            == 131072
        )

    def test_static_config_still_wins_over_the_ceiling_fallback(self, monkeypatch):
        """The ceiling is a fallback, not an authority: config outranks it."""
        _clear_caches()
        _fake_lmstudio(monkeypatch, _payload("meta/m", max_ctx=262144))
        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                custom_providers=_providers({"meta/m": {"context_length": 131072}}),
            )
            == 131072
        )


class TestBlastRadius:
    def test_non_lmstudio_local_server_is_untouched(self, monkeypatch):
        _clear_caches()
        monkeypatch.setattr(mm, "detect_local_server_type", lambda *a, **k: "ollama")
        assert mm._lmstudio_loaded_context_length("m", BASE_URL) is None

    def test_remote_endpoint_is_never_probed(self, monkeypatch):
        _clear_caches()

        def _boom(*a, **k):
            raise AssertionError("remote endpoint must not be probed for LM Studio")

        monkeypatch.setattr(mm, "detect_local_server_type", _boom)
        assert (
            mm._lmstudio_loaded_context_length("m", "https://api.example.com/v1")
            is None
        )

    def test_unreachable_server_returns_none_not_a_guess(self, monkeypatch):
        _clear_caches()
        monkeypatch.setattr(mm, "detect_local_server_type", lambda *a, **k: "lm-studio")
        monkeypatch.setattr(mm, "_endpoint_blackholed", lambda *a, **k: False)

        import httpx

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **k):
                raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "Client", _Client)
        assert mm._lmstudio_loaded_context_length("m", BASE_URL) is None

    def test_probe_failure_cannot_block_resolution(self, monkeypatch):
        """An exception in the probe must fall through, never propagate."""
        _clear_caches()

        def _boom(*a, **k):
            raise RuntimeError("probe exploded")

        monkeypatch.setattr(mm, "_lmstudio_loaded_context_length", _boom)
        assert (
            mm.get_model_context_length(
                "meta/m",
                base_url=BASE_URL,
                provider="lmstudio",
                custom_providers=_providers({"meta/m": {"context_length": 131072}}),
            )
            == 131072
        )

    def test_a_miss_is_not_memoized(self, monkeypatch):
        """A startup race must not pin 'unknown' for the whole TTL."""
        _clear_caches()
        _fake_lmstudio(monkeypatch, {"models": []})
        assert mm._lmstudio_loaded_context_length("meta/m", BASE_URL) is None
        assert mm._LMSTUDIO_LOADED_CTX_CACHE == {}

    def test_wrong_model_entry_is_not_borrowed(self, monkeypatch):
        """Fuzzy matching must not hand one model another's window."""
        _clear_caches()
        _fake_lmstudio(
            monkeypatch, _payload("google/gemma-4-31b-qat", max_ctx=262144, loaded_ctx=262144)
        )
        assert mm._lmstudio_loaded_context_length("google/gemma-4-31b", BASE_URL) is None
