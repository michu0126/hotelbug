"""Diagnostic configuration checks; no browser or hotel request is started."""

import json
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
import smoke_hyatt_browser as smoke

from app.core.errors import ErrorCode, ProviderError
from app.providers.hyatt_page import PROPERTY_URL


def configure_cli(monkeypatch, tmp_path, *extra):
    report = tmp_path / "hyatt-diagnostic.json"
    profile = str(tmp_path / "existing-project-session")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke_hyatt_browser.py",
            "--official-url",
            PROPERTY_URL,
            "--date",
            date.today().isoformat(),
            "--session-dir",
            profile,
            "--report",
            str(report),
            *extra,
        ],
    )
    provider = MagicMock(search_rates=AsyncMock(return_value=[]), close=AsyncMock())
    factory = MagicMock(return_value=provider)
    shutdown = AsyncMock()
    monkeypatch.setattr(smoke, "HyattBrowserProvider", factory)
    monkeypatch.setattr(smoke, "close_browser_sessions", shutdown)
    return report, profile, provider, factory, shutdown


@pytest.mark.parametrize(
    "extra,channel,proxy",
    [
        ([], "chrome", None),
        (
            ["--channel", "msedge", "--proxy", "http://probe-user:private-password@proxy.invalid:7890"],
            "msedge",
            "http://probe-user:private-password@proxy.invalid:7890",
        ),
    ],
)
async def test_smoke_passes_explicit_route_and_preserves_profile(
    monkeypatch, tmp_path, capsys, extra, channel, proxy
):
    report, profile, provider, factory, shutdown = configure_cli(monkeypatch, tmp_path, *extra)
    await smoke.main()
    factory.assert_called_once_with(proxy_server=proxy, browser_channel=channel, session_dir=profile)
    provider.configure_hotel_url.assert_called_once_with(PROPERTY_URL)
    provider.search_rates.assert_awaited_once()
    provider.close.assert_awaited_once()
    shutdown.assert_awaited_once()
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["browser_channel"] == channel
    assert result["proxy_configured"] is bool(proxy)
    assert result["status"] == "SUCCEEDED"
    output = report.read_text(encoding="utf-8") + capsys.readouterr().out
    for private_value in ("probe-user", "private-password", "proxy.invalid", profile):
        assert private_value not in output


async def test_smoke_failure_keeps_safe_route_evidence_without_retry(monkeypatch, tmp_path, capsys):
    proxy = "http://probe-user:private-password@proxy.invalid:7890"
    report, _profile, provider, _factory, shutdown = configure_cli(monkeypatch, tmp_path, "--proxy", proxy)
    failure = ProviderError(ErrorCode.RATE_LIMITED, "Hyatt returned HTTP 429", retry_after=900)
    provider.search_rates.side_effect = failure
    with pytest.raises(ProviderError) as caught:
        await smoke.main()
    assert caught.value is failure
    provider.search_rates.assert_awaited_once()
    provider.close.assert_awaited_once()
    shutdown.assert_awaited_once()
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["proxy_configured"] is True
    assert result["browser_channel"] == "chrome"
    assert result["error_type"] == "RATE_LIMITED"
    assert result["cleanup_status"] == "SUCCEEDED"
    assert result["status"] == "FAILED"
    output = report.read_text(encoding="utf-8") + capsys.readouterr().out
    assert "private-password" not in output and "proxy.invalid" not in output


async def test_smoke_still_requires_explicit_project_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke_hyatt_browser.py",
            "--official-url",
            PROPERTY_URL,
            "--date",
            date.today().isoformat(),
            "--report",
            str(tmp_path / "missing-profile.json"),
        ],
    )
    factory = MagicMock()
    monkeypatch.setattr(smoke, "HyattBrowserProvider", factory)
    with pytest.raises(SystemExit) as caught:
        await smoke.main()
    assert caught.value.code == 2
    factory.assert_not_called()


async def test_help_explains_default_does_not_load_saved_application_proxy(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["smoke_hyatt_browser.py", "--help"])
    with pytest.raises(SystemExit) as caught:
        await smoke.main()
    assert caught.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "default browser/network routing" in text
    assert "saved proxy settings" in text
