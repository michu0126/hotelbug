# 本地采样目录

运行 `python -m scripts.capture_har` 后，每次生成独立时间戳目录：

- `capture.har`：完整请求、响应内容、时间、Cookie 等。
- `json/000001.json` 等：每个 JSON 响应的原始字节，重复响应也保留。
- `manifest.json`：状态码、响应 URL、请求方法、采集时间、文件名及读取失败原因。
- `session.json`：是否 403／验证码停止，采样是否完整，保存错误摘要。

这些文件可能有登录状态和个人信息，已全部排除出 Git。不要公开上传原始 HAR；交样前检查 Cookie、Authorization、订单和会员信息。
不会读取现有 Chrome 用户目录，不接管或关闭你的浏览器，也不会自动登录、绕过验证码或购买。
