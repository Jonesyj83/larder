"""Private one-shot worker. stdout is a public JSON protocol, never SDK logs."""
import contextlib
import json
import os
import sys

# Avoid app/providers.py shadowing the native Hermes providers package.
sys.path = [p for p in sys.path if p != os.path.dirname(__file__)]


def descriptors():
    from providers import list_providers, get_provider_profile
    list_providers()  # Load registered plugins before the CLI snapshot.
    from hermes_cli.provider_catalog import provider_catalog
    from hermes_cli.models import _PROVIDER_MODELS
    result = []
    for entry in provider_catalog():
        profile = get_provider_profile(entry.slug)
        result.append({'slug': entry.slug, 'label': entry.label, 'description': entry.description,
                       'auth_type': entry.auth_type, 'api_key_env_vars': list(entry.api_key_env_vars), 'signup_url': entry.signup_url,
                       'api_mode': getattr(profile, 'api_mode', '') or '',
                       'models': list(_PROVIDER_MODELS.get(entry.slug, ()) or getattr(profile, 'fallback_models', ()) or ()),
                       'default_model': getattr(profile, 'default_aux_model', '') or '',
                       'reason': unsupported(entry.slug, entry.auth_type)})
    return result


def unsupported(slug, auth_type):
    if auth_type in {'aws_sdk', 'vertex'}:
        return 'Cloud SDK profiles are not supported by the isolated adapter yet'
    if auth_type == 'virtual' or slug in {'moa', 'copilot-acp'} or (
            auth_type == 'external_process' and slug != 'claude-subscription-directsdk-experimental'):
        return 'This provider cannot guarantee a tool-free bounded request'
    return ''


def generate(request):
    entries = {p['slug']: p for p in descriptors()}
    slug = request['provider']
    if slug in {'custom', 'lmstudio'} and not request.get('base_url'):
        return {'error': 'Provider is not connected or its transport dependencies are unavailable'}
    if slug not in entries:
        raise ValueError()
    if entries[slug]['reason']:
        return {'error': entries[slug]['reason']}
    model = request['model']
    from hermes_cli.auth import PROVIDER_REGISTRY
    from hermes_cli.config import get_env_value
    registry = PROVIDER_REGISTRY.get(slug)
    # Read only selected provider variables from the private snapshot, never global environment.
    for name in getattr(registry, 'api_key_env_vars', ()):
        value = get_env_value(name)
        if value:
            os.environ[name] = value
    from agent.auxiliary_client import resolve_provider_client, extract_content_or_reasoning
    kwargs = dict(provider=slug, model=model, explicit_api_key=request.get('api_key') or None,
                  explicit_base_url=request.get('base_url') or None,
                  api_mode=entries[slug]['api_mode'] or None)
    if slug == 'copilot' and request.get('api_key'):
        # Exchange a GitHub token through native Copilot auth, not an upstream bearer shortcut.
        os.environ['COPILOT_GITHUB_TOKEN'] = request['api_key']
        kwargs['explicit_api_key'] = None
    if slug == 'lmstudio' and request.get('base_url') and not request.get('api_key'):
        kwargs['explicit_api_key'] = 'no-key-required'
    # Native runtime handles the OAuth types the auxiliary registry cannot resolve directly.
    if slug in {'qwen-oauth', 'minimax-oauth'}:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rt = resolve_runtime_provider(requested=slug, target_model=model)
        if rt.get('provider') != slug:
            return {'error': 'Provider routing changed; fallback is disabled'}
        kwargs.update(provider='custom', explicit_api_key=rt.get('api_key'),
                      explicit_base_url=rt.get('base_url'), api_mode=rt.get('api_mode'))
    client, served_model = resolve_provider_client(**kwargs)
    if client is None:
        return {'error': 'Provider is not connected or its transport dependencies are unavailable'}
    effective = getattr(client, '_hermes_aux_effective_provider', slug)
    if effective != slug or served_model != model:
        return {'error': 'Provider routing changed; fallback is disabled'}
    messages = []
    if request.get('system'):
        messages.append({'role':'system','content':request['system']})
    messages.append({'role':'user','content':request['prompt']})
    create_kwargs = dict(model=served_model, messages=messages, max_tokens=2000)
    # OpenCode Zen/Go reject requests with no x-opencode-session (HTTP 400 MissingSessionID).
    try:
        from agent.opencode_affinity import merge_session_affinity_headers
        merge_session_affinity_headers(create_kwargs, slug, kwargs.get('explicit_base_url') or getattr(client, 'base_url', None))
    except Exception:
        pass
    # Deliberately bypass call_llm's recovery/fallback ladder. One transport call, no tools.
    response = client.chat.completions.create(**create_kwargs)
    text = extract_content_or_reasoning(response, max_reasoning_chars=0)
    if not text or len(text) > 100000:
        return {'error': 'Provider returned no usable text'}
    for value in (request.get('api_key'), getattr(client, 'api_key', None)):
        if isinstance(value,str) and len(value) > 6:
            text = text.replace(value, '[redacted]')
    actual_model = getattr(response, 'model', served_model)
    if not isinstance(actual_model, str) or len(actual_model) > 200:
        actual_model = served_model
    return {'text':text, 'requested_provider':slug, 'served_provider':slug,
            'requested_model':model, 'served_model':actual_model, 'fallback':False}


def track_child_groups():
    # Some SDK transports create their own session. Let the outer timeout reap those too.
    import subprocess
    path = os.environ.get('LARDER_CHILD_GROUPS')
    if not path:
        return
    original = subprocess.Popen
    class TrackedPopen(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if kwargs.get('start_new_session'):
                with open(path, 'a') as groups:
                    groups.write(str(self.pid) + '\n')
    subprocess.Popen = TrackedPopen


def main():
    request = json.loads(sys.stdin.buffer.read(131073))
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        try:
            if request['action'] == 'catalogue':
                # Catalogue inspection must never use a live endpoint or login.
                import socket
                def deny(*args, **kwargs):
                    raise OSError('Catalogue is offline')
                socket.socket.connect = deny
                result = {'providers':descriptors()}
            elif request['action'] == 'generate':
                track_child_groups()
                result = generate(request)
            else:
                raise ValueError()
        except Exception:
            result = {'error':'Provider is not connected or its transport dependencies are unavailable'}
    sys.stdout.write(json.dumps(result))


if __name__ == '__main__':
    main()
