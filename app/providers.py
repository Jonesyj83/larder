"""Plain direct API backends for meal/recipe suggestions.

Each provider is a small function: (prompt, key, model, base_url, system) -> text.
Plain HTTP calls with httpx, no vendor SDKs. Add another one by writing the
function, adding it to PROVIDERS, and (optionally) giving it a model list.
"""
import json
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx

from .netcheck import check_host, is_local

# connect quickly or not at all; generating a reply can take a while
TIMEOUT = httpx.Timeout(90, connect=10)
# read timeouts reset on every chunk, so a reply that trickles in also needs an overall cap
DEADLINE = 120
MAX_REPLY = 2 * 1024 * 1024

# tests swap this for an httpx.MockTransport
_transport = None


class ProviderError(Exception):
    pass


def _post(url, headers, body, params=None):
    started = time.monotonic()
    data = bytearray()
    try:
        with httpx.Client(transport=_transport, timeout=TIMEOUT, follow_redirects=False) as client:
            with client.stream("POST", url, headers=headers, json=body, params=params) as r:
                if r.status_code >= 400:
                    raise ProviderError(f"Provider rejected request (HTTP {r.status_code}); check connection settings")
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_REPLY:
                        raise ProviderError("Provider reply was too large")
                    if time.monotonic() - started > DEADLINE:
                        raise ProviderError("Provider took too long to answer")
    except httpx.HTTPError:
        # from None: the original error can carry the full url, key included
        raise ProviderError("Provider request failed or timed out") from None
    try:
        return json.loads(bytes(data))
    except ValueError:
        raise ProviderError("Provider sent back something that isn't json") from None


def _chat_completions(url, prompt, key, model, system, extra_headers=None):
    """The openai style /chat/completions api, which a lot of providers copy."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if extra_headers:
        headers.update(extra_headers)
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


def gemini(prompt, key, model, base_url, system=None):
    base = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    model = model or "gemini-3-flash-preview"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    # key in a header rather than ?key=, so it never ends up in a url or a log line
    data = _post(f"{base}/models/{model}:generateContent", {"x-goog-api-key": key or ""}, body)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError):
        raise ProviderError("Unexpected reply format")


def opencode_go(prompt, key, model, base_url, system=None):
    """OpenCode Go. Openai-compatible, but the relay needs a session header on
    every request or it answers 400 MissingSessionID - there's no multi-turn
    conversation here, so a fresh id per call is fine."""
    base = (base_url or "https://opencode.ai/zen/go/v1").rstrip("/")
    session = "larder-" + uuid.uuid4().hex[:24]
    return _chat_completions(base + "/chat/completions", prompt, key, model or "deepseek-v4.1-flash", system,
                              extra_headers={"x-opencode-session": session})


PROVIDERS = {
    "anthropic": anthropic,
    "openai": openai,
    "ollama": ollama,
    "opencode-go": opencode_go,
    "gemini": gemini,
    "openrouter": openrouter,
}

# ollama runs locally and doesn't need a key
NEEDS_KEY = {"anthropic", "openai", "opencode-go", "gemini", "openrouter"}

LABELS = {
    "anthropic": "Claude (Anthropic)",
    "openai": "OpenAI",
    "ollama": "Local LLM (Ollama)",
    "opencode-go": "OpenCode Go",
    "gemini": "Gemini (Google)",
    "openrouter": "OpenRouter",
}

DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
    "ollama": "llama3.1",
    "opencode-go": "deepseek-v4.1-flash",
    "gemini": "gemini-3-flash-preview",
    "openrouter": "openai/gpt-4o-mini",
}

SETUP_TEXT = {
    "anthropic": "Paste an Anthropic API key from console.anthropic.com.",
    "openai": "Paste an OpenAI API key from platform.openai.com.",
    "ollama": "Runs on your own machine or network. Enter the address if it's not on this server.",
    "opencode-go": "Paste an OpenCode Go API key from opencode.ai. Pick any model from its lineup below.",
    "gemini": "Paste a Google AI Studio API key (aistudio.google.com/apikey).",
    "openrouter": "Paste an OpenRouter API key from openrouter.ai.",
}

# Shown as suggestions in the model field; any model name the provider actually
# serves can still be typed in. Kept short and current rather than exhaustive.
PROVIDER_MODELS = {
    "anthropic": ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"],
    "gemini": ["gemini-3-flash-preview", "gemini-3-pro-preview"],
    "opencode-go": [
        "deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp",
        "glm-5.3-flash", "glm-5.3", "glm-5.2", "glm-5.1", "glm-5",
        "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus", "qwen3.5-plus",
        "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
        "grok-4.7", "grok-4.6", "grok-4.5",
        "minimax-m3", "minimax-m2.7", "minimax-m2.5",
        "mimo-v2.6-pro", "mimo-v2.6-flash", "mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni",
        "gpt-6-luna", "gpt-5.6-luna",
        "claude-haiku-5-5", "hy4-preview", "hy3", "hy3-preview",
        "longcat-2.5-preview-free", "longcat-2.0", "step-5-preview-free",
        "space-bunny", "space-bunny-free",
    ],
}


def catalogue():
    out = []
    for slug in PROVIDERS:
        out.append(dict(
            slug=slug, label=LABELS.get(slug, slug.title()),
            auth_type="local" if slug == "ollama" else "api_key",
            models=PROVIDER_MODELS.get(slug, []), default_model=DEFAULT_MODELS.get(slug, ""),
            setup=SETUP_TEXT.get(slug, "Enter a provider API key and model."),
        ))
    return out


MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*")


def validate_settings(provider, model, base_url):
    if provider not in PROVIDERS:
        raise ProviderError("Unknown provider")
    # the model ends up in a url path for some providers, so keep it to plain names
    if len(model) > 200 or (model and (not MODEL_NAME.fullmatch(model) or ".." in model)):
        raise ProviderError("Invalid model name")
    if base_url:
        try:
            u = urlsplit(base_url)
            port = u.port or (443 if u.scheme == "https" else 80)
        except ValueError:
            raise ProviderError("Invalid base URL")
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ProviderError("Use an HTTP(S) base URL without credentials, query or fragment")
        # only ollama is meant to live on this machine or the home network
        local_ok = provider == "ollama"
        try:
            check_host(u.hostname, port, allow_local=local_ok)
        except ValueError:
            raise ProviderError("That base URL points at a private or internal address" if not local_ok
                                else "That base URL points at an address that isn't allowed")
        if u.scheme == "http" and not (local_ok and is_local(u.hostname, port)):
            raise ProviderError("Remote endpoints require HTTPS")


def validate_key(key):
    if len(key) > 1000 or any(ord(c) <= 32 or ord(c) > 126 for c in key):
        raise ProviderError("That API key has characters an API key can't have")


def is_configured(settings):
    if not settings or settings.get("provider") not in PROVIDERS:
        return False
    if settings["provider"] in NEEDS_KEY:
        return bool(settings.get("api_key"))
    return True


def ask_with_provenance(settings, prompt, system=None):
    if not is_configured(settings):
        raise ProviderError("Suggestions unavailable; connect a provider in Settings or use saved recipes/manual ingredients")
    if len(prompt) > 80000:
        raise ProviderError("Suggestion request is too large")
    validate_settings(settings["provider"], settings.get("model") or "", settings.get("base_url") or "")
    model = (settings.get("model") or "").strip() or DEFAULT_MODELS.get(settings["provider"], "")
    text = PROVIDERS[settings["provider"]](prompt, settings.get("api_key"), model, settings.get("base_url"), system)
    if not isinstance(text, str) or len(text) > 100000:
        raise ProviderError("Provider returned invalid or oversized text")
    if settings.get("api_key") and len(settings["api_key"]) >= 8:
        text = text.replace(settings["api_key"], "[redacted]")
    return dict(text=text, requested_provider=settings["provider"], served_provider=settings["provider"],
                requested_model=model, served_model=model, fallback=False)


def ask(settings, prompt, system=None):
    return ask_with_provenance(settings, prompt, system)["text"]
