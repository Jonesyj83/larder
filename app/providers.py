"""Optional native Hermes text transports, with standalone direct API backends."""
import httpx
from urllib.parse import urlsplit
from .hermes_bridge import catalogue as hermes_catalog, call as hermes_call, BridgeError

TIMEOUT = 90


class ProviderError(Exception):
    pass


def _post(url, headers, body):
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=TIMEOUT)
    except httpx.HTTPError:
        raise ProviderError("Provider request failed or timed out")
    if r.status_code >= 400:
        raise ProviderError(f"Provider rejected request (HTTP {r.status_code}); check connection settings")
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


def catalogue():
    native = hermes_catalog()
    result = []
    for p in native:
        p = dict(p, backend='hermes', state='Unsupported' if p.get('reason') else 'Setup required')
        p['setup'] = setup_text(p)
        result.append(p)
    # Standalone direct settings remain available under their existing identities.
    for slug in PROVIDERS:
        if not any(p['slug'] == slug for p in result):
            p = dict(slug=slug, label=slug.title() + ' (direct)', backend='direct',
                     auth_type='local' if slug == 'ollama' else 'api_key', models=[],
                     api_mode='anthropic_messages' if slug == 'anthropic' else 'chat_completions',
                     reason='', state='Setup required', default_model='')
            p['setup'] = setup_text(p)
            result.append(p)
    return result


def setup_text(p):
    slug, auth = p['slug'], p['auth_type']
    if p.get('reason'):
        return p['reason']
    if slug == 'claude-subscription-directsdk-experimental':
        return 'Sign in with claude auth login in the server terminal. Requires the experimental Hermes plugin and Claude Code. Admin must explicitly enable server account reuse.'
    if slug == 'anthropic':
        return 'Anthropic API key, or native Claude sign-in with extra usage billing. This is separate from the Claude subscription DirectSDK plugin.'
    if auth.startswith('oauth') or slug == 'copilot':
        return 'Sign in using hermes auth add ' + slug + ' in the server terminal, then an admin can enable server account reuse. No browser login is launched here.'
    if auth in {'aws_sdk', 'vertex'}:
        return 'Cloud SDK credentials are required in a dedicated runtime profile; ambient server credentials are not inherited.'
    if slug in {'lmstudio', 'custom', 'ollama'}:
        return 'Configure a local endpoint and model; enter a key only if the endpoint requires it.'
    return 'Enter a provider API key and model, or ask an admin to explicitly enable an existing server account.'


def validate_settings(provider, model, base_url):
    if provider not in {p['slug'] for p in catalogue()}:
        raise ProviderError('Provider is unavailable in the installed catalogue')
    if provider in {'custom', 'lmstudio'} and not base_url:
        raise ProviderError('This provider requires an explicit endpoint')
    if len(model) > 200 or any(ord(c) < 32 for c in model):
        raise ProviderError('Invalid model name')
    if base_url:
        try:
            u = urlsplit(base_url)
        except ValueError:
            raise ProviderError('Invalid base URL')
        if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ProviderError('Use an HTTP(S) base URL without credentials, query or fragment')
        if u.scheme == 'http' and u.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise ProviderError('Remote endpoints require HTTPS')


def is_configured(settings):
    if not settings:
        return False
    p = next((p for p in catalogue() if p['slug'] == settings['provider']), None)
    if not p or p.get('reason'):
        return False
    if p['backend'] == 'hermes':
        if settings['provider'] in {'custom', 'lmstudio'} and not settings.get('base_url'):
            return False
        model = settings.get('model') or p.get('default_model') or next(iter(p.get('models', [])), '')
        return bool(model and (settings.get('reuse_account') or settings.get('api_key') or
                    (settings['provider'] in {'lmstudio','custom'} and settings.get('base_url'))))
    return bool(settings.get('api_key')) if settings['provider'] in NEEDS_KEY else True


def ask_with_provenance(settings, prompt, system=None):
    if not is_configured(settings):
        raise ProviderError('Suggestions unavailable; connect a provider in Settings or use saved recipes/manual ingredients')
    if len(prompt) > 80000:
        raise ProviderError('Suggestion request is too large')
    validate_settings(settings['provider'], settings['model'], settings['base_url'])
    p = next(p for p in catalogue() if p['slug'] == settings['provider'])
    if p['backend'] == 'hermes':
        try:
            return hermes_call(dict(action='generate', provider=settings['provider'], model=settings.get('model') or p.get('default_model') or next(iter(p.get('models', [])), ''),
                               api_key=settings['api_key'], base_url=settings['base_url'], prompt=prompt, system=system,
                               account_owner=settings.get('user_id')),
                               reuse=bool(settings.get('reuse_account')))
        except (BridgeError, OSError):
            raise ProviderError('Hermes provider unavailable or timed out; check sign-in, plugin and runtime dependencies. No fallback was used.')
    text = PROVIDERS[settings['provider']](prompt, settings['api_key'], settings['model'], settings['base_url'], system)
    if not isinstance(text, str) or len(text) > 100000:
        raise ProviderError('Provider returned invalid or oversized text')
    defaults = {'openai':'gpt-4o-mini','anthropic':'claude-sonnet-5','openrouter':'openai/gpt-4o-mini',
                'deepseek':'deepseek-chat','ollama':'llama3.1'}
    model = settings['model'] or defaults[settings['provider']]
    if settings.get('api_key'):
        text = text.replace(settings['api_key'], '[redacted]')
    return dict(text=text, requested_provider=settings['provider'], served_provider=settings['provider'],
                requested_model=model, served_model=model, fallback=False)


def ask(settings, prompt, system=None):
    return ask_with_provenance(settings, prompt, system)['text']
