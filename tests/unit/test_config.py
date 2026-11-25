"""Environment parsing: what starts, what refuses, what is scrubbed."""

import pytest

from caldav_mcp.config import ConfigError, load_config

KEY = "k" * 20


def env(**extra: str) -> dict[str, str]:
    base = {
        "CALDAV_URL": "https://dav.example.net/",
        "CALDAV_USERNAME": "me",
        "CALDAV_PASSWORD": "secret-pw",
        "CALDAV_MCP_API_KEY": KEY,
    }
    base.update(extra)
    return base


def test_minimal_valid_environment():
    cfg = load_config(env())
    assert cfg.url == "https://dav.example.net/"
    assert cfg.verify_ssl is True
    assert cfg.read_only is False
    assert cfg.week_start == 0
    assert cfg.tz_name == "UTC"
    assert cfg.max_events == 50
    assert cfg.path == "/mcp"


def test_secrets_are_scrubbed_from_the_environment_even_when_start_fails():
    e = env(CALDAV_URL="ftp://nope")
    with pytest.raises(ConfigError):
        load_config(e)
    assert "CALDAV_PASSWORD" not in e
    assert "CALDAV_MCP_API_KEY" not in e
    assert "CALDAV_USERNAME" not in e


def test_repr_does_not_leak_secrets():
    text = repr(load_config(env()))
    assert "secret-pw" not in text
    assert KEY not in text


@pytest.mark.parametrize("missing", ["CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"])
def test_missing_caldav_settings_refuse_to_start(missing):
    e = env()
    del e[missing]
    with pytest.raises(ConfigError, match=missing):
        load_config(e)


class TestInboundAuth:
    def test_no_key_refuses_to_start(self):
        e = env()
        del e["CALDAV_MCP_API_KEY"]
        with pytest.raises(ConfigError, match="CALDAV_MCP_ALLOW_NO_AUTH"):
            load_config(e)

    def test_opt_out_must_be_spelled_exactly(self):
        for value in ("1", "yes", "TRUE", "True", " "):
            e = env(CALDAV_MCP_ALLOW_NO_AUTH=value)
            del e["CALDAV_MCP_API_KEY"]
            with pytest.raises(ConfigError):
                load_config(e)

    def test_explicit_opt_out_starts_without_a_key(self):
        e = env(CALDAV_MCP_ALLOW_NO_AUTH="true")
        del e["CALDAV_MCP_API_KEY"]
        cfg = load_config(e)
        assert cfg.api_key == ""
        assert cfg.allow_no_auth is True

    def test_short_key_is_refused(self):
        with pytest.raises(ConfigError, match="at least 16"):
            load_config(env(CALDAV_MCP_API_KEY="short"))


class TestUrl:
    @pytest.mark.parametrize(
        "url",
        ["ftp://x/", "not a url", "https://u:p@dav.example.net/", "https://dav.example.net/?x=1", "https://h:99999/"],
    )
    def test_bad_urls_are_refused(self, url):
        with pytest.raises(ConfigError):
            load_config(env(CALDAV_URL=url))

    def test_invalid_url_error_never_echoes_the_value(self):
        with pytest.raises(ConfigError) as info:
            load_config(env(CALDAV_URL="https://admin:hunter2@host:99999"))
        assert "hunter2" not in str(info.value)

    def test_plain_http_to_remote_host_needs_explicit_opt_in(self):
        with pytest.raises(ConfigError, match="plain http"):
            load_config(env(CALDAV_URL="http://dav.example.net/"))
        assert load_config(env(CALDAV_URL="http://dav.example.net/", CALDAV_ALLOW_PLAINTEXT="true"))

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
    def test_plain_http_to_loopback_is_fine(self, host):
        assert load_config(env(CALDAV_URL=f"http://{host}:5232/"))

    def test_plaintext_opt_in_is_strict(self):
        with pytest.raises(ConfigError):
            load_config(env(CALDAV_URL="http://dav.example.net/", CALDAV_ALLOW_PLAINTEXT="yes"))


class TestTlsVerification:
    @pytest.mark.parametrize("value", ["false", "0", "no", " FALSE "])
    def test_only_explicit_false_disables(self, value):
        assert load_config(env(CALDAV_MCP_CALDAV_VERIFY_SSL=value)).verify_ssl is False

    @pytest.mark.parametrize("value", ["true", "1", "yes", ""])
    def test_enabled_by_default_and_when_true(self, value):
        e = env()
        if value:
            e["CALDAV_MCP_CALDAV_VERIFY_SSL"] = value
        assert load_config(e).verify_ssl is True

    @pytest.mark.parametrize("value", ["ture", "off", "disable"])
    def test_a_typo_never_silently_disables_verification(self, value):
        with pytest.raises(ConfigError):
            load_config(env(CALDAV_MCP_CALDAV_VERIFY_SSL=value))


class TestSwitches:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
    def test_read_only_is_read_tolerantly(self, value):
        assert load_config(env(CALDAV_MCP_READ_ONLY=value)).read_only is True

    @pytest.mark.parametrize("value", ["", "0", "false", "no"])
    def test_read_only_off(self, value):
        assert load_config(env(CALDAV_MCP_READ_ONLY=value)).read_only is False

    @pytest.mark.parametrize("name", ["CALDAV_ALLOW_TOOLS", "CALDAV_DENY_TOOLS"])
    def test_set_but_empty_tool_list_refuses(self, name):
        with pytest.raises(ConfigError, match="set but empty"):
            load_config(env(**{name: "  "}))


class TestTimeAndLimits:
    def test_timezone_and_week_start(self):
        cfg = load_config(env(TZ="Europe/Berlin", CALDAV_WEEK_START="Sunday"))
        assert cfg.tz_name == "Europe/Berlin"
        assert cfg.week_start == 6

    def test_unknown_timezone_refuses_rather_than_shifting_every_day(self):
        with pytest.raises(ConfigError, match="TZ"):
            load_config(env(TZ="Mars/Olympus"))

    def test_bad_week_start(self):
        with pytest.raises(ConfigError, match="CALDAV_WEEK_START"):
            load_config(env(CALDAV_WEEK_START="someday"))

    @pytest.mark.parametrize("value", ["0", "501", "abc", "1.5"])
    def test_max_events_bounds(self, value):
        with pytest.raises(ConfigError, match="CALDAV_MAX_EVENTS"):
            load_config(env(CALDAV_MAX_EVENTS=value))

    def test_health_path_is_reserved(self):
        with pytest.raises(ConfigError, match="/health"):
            load_config(env(CALDAV_MCP_PATH="/health"))

    def test_tls_cert_and_key_come_together(self):
        with pytest.raises(ConfigError, match="together"):
            load_config(env(CALDAV_MCP_TLS_CERT="/c.pem"))


def test_tools_that_never_listen_need_no_inbound_key():
    e = env()
    del e["CALDAV_MCP_API_KEY"]
    assert load_config(e, serving=False, scrub=False).api_key == ""
    with pytest.raises(ConfigError, match="CALDAV_MCP_ALLOW_NO_AUTH"):
        load_config(e, scrub=False)


def test_the_image_does_not_depend_on_a_system_time_zone_database():
    """Alpine images ship none. Without the tzdata package a named zone fails (the old server fell back to UTC)."""
    import tzdata  # noqa: F401, PLC0415 - the point is that it is importable

    assert load_config(env(TZ="Europe/London")).tz_name == "Europe/London"
