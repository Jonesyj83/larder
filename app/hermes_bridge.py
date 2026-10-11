"""Bounded IPC to the optional Hermes text transport. No agent or app tools."""
import json
import re
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import signal
import shlex
import subprocess
import sys
import tempfile
from functools import lru_cache


class BridgeError(Exception):
    pass


def runtime():
    source = os.environ.get('LARDER_HERMES_SOURCE', '')
    executable = shutil.which(os.environ.get('LARDER_HERMES_EXECUTABLE', 'hermes'))
    detected_python = ''
    if executable:
        candidate = Path(executable).resolve()
        # Follow simple absolute exec wrappers without running the CLI or expanding a shell.
        for _ in range(3):
            for parent in (candidate.parent, candidate.parent.parent, candidate.parent.parent.parent):
                if not source and (parent / 'hermes_cli/provider_catalog.py').is_file():
                    source = str(parent)
            try:
                lines = candidate.read_text()[:8192].splitlines()
                target = next((shlex.split(line.split(' -', 1)[0])[1] for line in lines if line.startswith('exec /')), '')
            except (OSError, UnicodeError, ValueError, IndexError):
                break
            if not target or not Path(target).is_file():
                break
            if Path(target).name.startswith('python'):
                detected_python = target
                break
            candidate = Path(target).resolve()
    if not source or not (Path(source) / 'hermes_cli/provider_catalog.py').is_file():
        raise BridgeError('Hermes runtime is not installed or configured on this server')
    python = os.environ.get('LARDER_HERMES_PYTHON') or (str(Path(source) / '.venv/bin/python') if (Path(source) / '.venv/bin/python').is_file() else detected_python)
    if not Path(python).is_file():
        raise BridgeError('Hermes Python runtime is unavailable')
    return str(Path(source).resolve()), python


def kill_child_groups(path):
    try:
        groups = path.read_text().splitlines()
    except OSError:
        return
    for group in groups:
        if group.isdecimal() and int(group) > 1:
            try:
                os.killpg(int(group), signal.SIGKILL)
            except ProcessLookupError:
                pass


@contextmanager
def account_snapshot(payload, reuse):
    if not reuse:
        yield None
        return
    owner = payload.get('account_owner')
    slug = payload.get('provider', '')
    if not isinstance(owner, int) or owner <= 0 or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,119}', slug):
        raise BridgeError('Invalid account scope')
    from .db import DATA_DIR
    # App-owned copies persist native OAuth refreshes, without changing the source account.
    directory = DATA_DIR / 'provider_accounts' / str(owner) / slug
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for private_dir in (directory, directory.parent, directory.parent.parent):
        private_dir.chmod(0o700)
    import fcntl
    with open(directory / '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError('Provider account is busy; retry shortly')
        try:
            yield directory
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def discard_account(owner, provider):
    with account_snapshot({'account_owner': owner, 'provider': provider}, True) as directory:
        for path in directory.iterdir():
            if path.name == '.lock':
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def call(payload, reuse=False):
    with account_snapshot(payload, reuse) as snapshot:
        return _call(payload, reuse, snapshot)


def credential_files(slug):
    native = ['auth.json']
    if slug == 'copilot':
        native.append('.copilot_jwt.json')
    native.append('.env')
    external = []
    if slug in {'anthropic', 'claude-subscription-directsdk-experimental'}:
        external = ['.claude/.credentials.json', '.claude.json']
    elif slug == 'openai-codex':
        external = ['.codex/auth.json']
    elif slug == 'qwen-oauth':
        external = ['.qwen/oauth_creds.json']
    return native, external


def copy_scoped_credential(original, target, slug, allowed_vars):
    if original.name == 'auth.json' and original.parent.name != '.codex':
        raw = json.loads(original.read_text())
        # Keep the whole store's bookkeeping, but only this provider's credentials.
        scoped = {k: v for k, v in raw.items() if k not in ('providers', 'credential_pool', 'suppressed_sources')}
        for section in ('providers', 'credential_pool', 'suppressed_sources'):
            if isinstance(raw.get(section), dict):
                scoped[section] = {slug: raw[section][slug]} if slug in raw[section] else {}
        if raw.get('active_provider') != slug:
            scoped.pop('active_provider', None)
        target.write_text(json.dumps(scoped))
    elif original.name == '.env':
        lines = []
        for line in original.read_text().splitlines():
            match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match.group(1) in allowed_vars:
                lines.append(line)
        target.write_text('\n'.join(lines) + '\n')
    else:
        shutil.copyfile(original, target)
    target.chmod(0o600)


def _call(payload, reuse=False, snapshot=None):
    source, python = runtime()
    slug = payload.get('provider', '')
    native_files, external_files = credential_files(slug)
    allowed_vars = set()
    if reuse:
        allowed_vars = set(next((p.get('api_key_env_vars', []) for p in catalogue() if p['slug'] == slug), []))
    account = Path(os.environ.get('LARDER_HERMES_ACCOUNT_HOME', str(Path.home() / '.hermes')))
    with tempfile.TemporaryDirectory(prefix='larder-hermes-') as scratch:
        root = Path(scratch)
        home = root / 'home'
        hermes = home / '.hermes'
        hermes.mkdir(parents=True, mode=0o700)
        # Only plugin code is discoverable before the administrator opts into account reuse.
        plugins = account / 'plugins'
        if plugins.is_dir():
            (hermes / 'plugins').symlink_to(plugins, target_is_directory=True)
        snapshot_ready = snapshot is not None and (snapshot / '.initialized').is_file()
        if reuse:
            # Refreshes and SDK writes affect the private snapshot, never global stores.
            for name in native_files:
                original = (snapshot / 'hermes' / name) if snapshot_ready else account / name
                if original.is_file():
                    copy_scoped_credential(original, hermes / name, slug, allowed_vars)
            # Only the selected provider's external login is imported.
            for name in external_files:
                original = (snapshot / 'home' / name) if snapshot_ready else Path.home() / name
                if original.is_file():
                    target = home / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    copy_scoped_credential(original, target, slug, allowed_vars)
        env = {k: os.environ[k] for k in ('PATH', 'LANG', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'SYSTEMROOT') if k in os.environ}
        child_groups = root / 'child-groups'
        env['LARDER_CHILD_GROUPS'] = str(child_groups)
        env.update(HOME=str(home), HERMES_HOME=str(hermes), XDG_CONFIG_HOME=str(home / '.config'),
                   PYTHONPATH=source, PYTHONDONTWRITEBYTECODE='1', TMPDIR=scratch,
                   HERMES_DISABLE_LAZY_INSTALLS='1')
        # Never inherit vendor keys, proxies, cloud credentials, routing or automatic fallbacks.
        worker = str(Path(__file__).with_name('hermes_worker.py'))
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen([python, worker], stdin=subprocess.PIPE, stdout=output,
                                    stderr=subprocess.DEVNULL, cwd=scratch, env=env, start_new_session=True)
            try:
                proc.communicate(json.dumps(payload).encode(), timeout=90 if payload['action'] == 'generate' else 20)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                kill_child_groups(child_groups)
                raise BridgeError('Hermes request timed out; no ingredients were saved')
            kill_child_groups(child_groups)
            output.seek(0)
            data = output.read(262145)
        if snapshot is not None:
            # Native stores are atomic. Persist only validated private JSON credential files.
            for private, destination in ((hermes, snapshot / 'hermes'), (home, snapshot / 'home')):
                names = native_files if private == hermes else external_files
                for name in names:
                    path = private / name
                    if path.is_file():
                        if name.endswith('.json'):
                            try:
                                json.loads(path.read_text())
                            except (ValueError, OSError):
                                continue
                        target = destination / name
                        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        staged = target.with_name(target.name + '.tmp')
                        shutil.copyfile(path, staged)
                        staged.chmod(0o600)
                        staged.replace(target)
            (snapshot / '.initialized').touch(mode=0o600)
        if proc.returncode or len(data) > 262144:
            raise BridgeError('Hermes transport unavailable; check its server setup')
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise BridgeError('Hermes returned an invalid response')
        if result.get('error'):
            raise BridgeError(result['error'] if result['error'] in {
                'Provider is not connected or its transport dependencies are unavailable',
                'This provider cannot guarantee a tool-free bounded request',
                'Provider routing changed; fallback is disabled',
                'Provider returned no usable text',
            } else 'Hermes transport unavailable; check its server setup')
        return result


@lru_cache(maxsize=1)
def catalogue():
    try:
        return call({'action': 'catalogue'})['providers']
    except (BridgeError, OSError, KeyError):
        return []
