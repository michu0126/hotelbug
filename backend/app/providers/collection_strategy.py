"""Transport contracts, not claims of live quote success or API entitlement."""


def collection_strategy(provider):
    if provider == "marriott":
        return {
            "mode": "official_api_or_cookie",
            "label": "官网接口 / Cookie",
            "detail": "中文站自动选择酒店、日期并监听页面真实房价响应；英文单晚房型保留已观察接口，目录与月历使用官网会话。",
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
