"""Text generation backends for the suggestions feature.

Each provider is one function: (prompt, key, model, base_url, system) -> text.
Plain http calls with httpx, no vendor sdks, so adding another one is just
another small function plus an entry in PROVIDERS.
"""
import httpx

TIMEOUT = 90


class ProviderError(Exception):
    pass


def _post(url, headers, body):
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise ProviderError(f"Couldn't reach {url}: {e}")
    if r.status_code >= 400:
        try:
            err = r.json().get("error")
            msg = err.get("message") if isinstance(err, dict) else err
        except ValueError:
            msg = None
        raise ProviderError(f"{r.status_code} from provider: {msg or r.text[:300]}")
    try:
        return r.json()
    except ValueError:
        raise ProviderError("Provider sent back something that isn't json")


def _chat_completions(url, prompt, key, model, system):
    """The openai style /chat/completions api, which a lot of providers copy."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _post(url, headers, {"model": model, "messages": messages})
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise ProviderError("Unexpected reply format")


def openai(prompt, key, model, base_url, system=None):
    base = (base_url or "https://api.openai.com/v1").rstrip("/")
    return _chat_completions(base + "/chat/completions", prompt, key, model or "gpt-4o-mini", system)


def openrouter(prompt, key, model, base_url, system=None):
    base = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
    return _chat_completions(base + "/chat/completions", prompt, key, model or "openai/gpt-4o-mini", system)


def deepseek(prompt, key, model, base_url, system=None):
    base = (base_url or "https://api.deepseek.com").rstrip("/")
    return _chat_completions(base + "/chat/completions", prompt, key, model or "deepseek-chat", system)


def anthropic(prompt, key, model, base_url, system=None):
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    body = {"model": model or "claude-sonnet-5", "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    data = _post(base + "/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, body)
    try:
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, TypeError):
        raise ProviderError("Unexpected reply format")


def ollama(prompt, key, model, base_url, system=None):
    base = (base_url or "http://localhost:11434").rstrip("/")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = _post(base + "/api/chat", headers, {"model": model or "llama3.1", "messages": messages, "stream": False})
    try:
        return data["message"]["content"]
    except (KeyError, TypeError):
        raise ProviderError("Unexpected reply format")


PROVIDERS = {
    "openai": openai,
    "anthropic": anthropic,
    "openrouter": openrouter,
    "deepseek": deepseek,
    "ollama": ollama,
}

# ollama runs locally and doesn't need a key
NEEDS_KEY = {"openai", "anthropic", "openrouter", "deepseek"}


def is_configured(settings):
    if not settings or settings["provider"] not in PROVIDERS:
        return False
    if settings["provider"] in NEEDS_KEY:
        return bool(settings["api_key"])
    return True


def ask(settings, prompt, system=None):
    if not is_configured(settings):
        raise ProviderError("Add an API key in Settings to use this")
    fn = PROVIDERS[settings["provider"]]
    return fn(prompt, settings["api_key"], settings["model"], settings["base_url"], system)
