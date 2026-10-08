from urllib.parse import urlparse


def normalize_chat_request(body: dict, api_url: str):
    """Remove parameters that official Kimi chat models require the server to choose."""
    host = (urlparse(api_url).hostname or "").casefold()
    model = str(body.get("model", "")).casefold()
    if host in {"api.moonshot.cn", "api.moonshot.ai"} and model.startswith("kimi-"):
        return body.pop("temperature", None)
    return None
