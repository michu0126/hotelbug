import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from audit_ihg_price import install_stay_diagnostics

from app.core.errors import ErrorCode, ProviderError
from app.providers.registry import FACTORIES


@pytest.mark.parametrize("diagnostic_fails", [False, True])
async def test_accor_offer_diagnostic_keeps_original_error_and_does_not_navigate(
    monkeypatch, capsys, diagnostic_fails
):
    instance = MagicMock()
    page = instance._page
    page.url = "https://all.accor.com/booking/en/accor/hotel/0348"
    page.is_closed.return_value = False
    projected = [{"plan": "Flexible", "displayed_offer_text": "Cancel free of charge"}]
    page.locator.return_value.evaluate_all = AsyncMock(
        return_value=projected,
        side_effect=RuntimeError("PRIVATE") if diagnostic_fails else None,
    )
    error = ProviderError(ErrorCode.PROVIDER_CHANGED, "Original offer error")
    instance.search_rates = AsyncMock(side_effect=error)
    monkeypatch.setitem(FACTORIES, "accor", lambda: instance)
    install_stay_diagnostics("accor")
    with pytest.raises(ProviderError) as raised:
        await FACTORIES["accor"]().search_rates(object())
    assert raised.value is error
    output = capsys.readouterr().out
    assert "PRIVATE" not in output
    assert json.loads(output) == (
        {"public_accor_offer_controls_unavailable": True}
        if diagnostic_fails
        else {"public_accor_offer_controls": projected}
    )
    page.goto.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["https://other.example/booking/en/accor/hotel/0348", "https://all.accor.com/login"],
)
async def test_accor_diagnostic_ignores_non_booking_page(monkeypatch, capsys, url):
    instance = MagicMock()
    instance._page.url = url
    instance._page.is_closed.return_value = False
    instance.search_rates = AsyncMock(return_value=[])
    monkeypatch.setitem(FACTORIES, "accor", lambda: instance)
    install_stay_diagnostics("accor")
    assert await FACTORIES["accor"]().search_rates(object()) == []
    instance._page.locator.assert_not_called()
    assert capsys.readouterr().out == ""
