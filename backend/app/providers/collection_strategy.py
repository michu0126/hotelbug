"""Transport contracts, not claims of live quote success or API entitlement."""


def collection_strategy(provider):
    if provider == "marriott":
        return {
            "mode": "official_api_or_cookie",
            "label": "官网接口 / Cookie",
            "detail": "英文官网单晚房型走已观察接口；中文官网、目录和月历使用 Cookie 浏览器。",
            "api_scope": "www.marriott.com · single room/night cash offers",
            "live_api_verified": False,
        }
    return {
        "mode": "cookie_browser",
        "label": "Cookie 浏览器",
        "detail": "暂无已接入的可用报价接口，使用官网会话读取实际日期和房型价格。",
        "api_scope": None,
        "live_api_verified": False,
    }
