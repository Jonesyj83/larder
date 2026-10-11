"""Offline catalogue inspection and mocked transports; never live generation/auth."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import providers, hermes_bridge
from app.db import SCHEMA
from app.routes import suggestions as sg
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]

class ConfigSafety(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.executemany('INSERT INTO users(id,username,password_hash,is_admin) VALUES(?,?,?,?)',
                            [(1,'admin','unused',1),(2,'member','unused',0)])
        self.admin={'id':1,'is_admin':1}; self.member={'id':2,'is_admin':0}
        self.catalogue=patch.object(providers,'hermes_catalog',return_value=[dict(slug='openai-codex',label='ChatGPT OAuth',auth_type='oauth_external',reason='',models=[])])
        self.catalogue.start()
    def tearDown(self): self.catalogue.stop(); self.db.close()
    def test_wrong_provider_and_origin_keys_never_reused(self):
        sg.save_settings(sg.SettingsIn(provider='openai',api_key='sentinel-secret'),self.db,self.member)
        public=sg.get_settings(self.db,self.member)
        self.assertNotIn('sentinel-secret',json.dumps(public)); self.assertNotIn('key_hint',public)
        sg.save_settings(sg.SettingsIn(provider='anthropic'),self.db,self.member)
        self.assertEqual(self.db.execute('SELECT api_key FROM suggestion_settings').fetchone()[0],'')
        sg.save_settings(sg.SettingsIn(provider='openai',api_key='origin-key'),self.db,self.member)
        sg.save_settings(sg.SettingsIn(provider='openai',base_url='https://other.invalid/v1'),self.db,self.member)
        self.assertEqual(self.db.execute('SELECT api_key FROM suggestion_settings').fetchone()[0],'')
    def test_admin_reuse_private_shared_and_downgrade(self):
        with self.assertRaises(HTTPException) as error:
            sg.save_settings(sg.SettingsIn(provider='openai-codex',model='m',reuse_account=True),self.db,self.member)
        self.assertEqual(error.exception.status_code,403)
        sg.save_settings(sg.SettingsIn(provider='openai-codex',model='m',reuse_account=True),self.db,self.admin)
        self.assertIsNone(sg.settings_for(self.db,self.member))
        sg.save_settings(sg.SettingsIn(provider='openai-codex',model='m',reuse_account=True,shared=True),self.db,self.admin)
        self.assertEqual(sg.settings_for(self.db,self.member)['provider'],'openai-codex')
        # A selected, disconnected own provider must not trigger a shared paid fallback.
        sg.save_settings(sg.SettingsIn(provider='anthropic'),self.db,self.member)
        self.assertIsNone(sg.settings_for(self.db,self.member))
        sg.clear_settings(self.db,self.member)
        self.db.execute('UPDATE users SET is_admin=0 WHERE id=1')
        self.assertIsNone(sg.settings_for(self.db,self.member))
        with self.assertRaises(HTTPException):
            sg.save_settings(sg.SettingsIn(provider='openai-codex',model='m',reuse_account=True,base_url='https://other.invalid'),self.db,self.admin)
    def test_missing_auth_and_plugin_unavailable(self):
        setting=dict(provider='openai-codex',api_key='',model='m',base_url='',reuse_account=False)
        self.assertFalse(providers.is_configured(setting))
        with self.assertRaises(providers.ProviderError): providers.ask(setting,'draft')
        with self.assertRaises(HTTPException): sg.save_settings(sg.SettingsIn(provider='missing-plugin'),self.db,self.admin)
        setting['reuse_account']=True
        with patch.object(providers,'hermes_call',side_effect=hermes_bridge.BridgeError('secret-token')):
            with self.assertRaises(providers.ProviderError) as e: providers.ask(setting,'draft')
        self.assertNotIn('secret-token',str(e.exception))
    def test_direct_wires_and_error_timeout(self):
        with patch.object(providers,'_post',return_value={'content':[{'type':'text','text':'draft'}]}) as post:
            self.assertEqual(providers.anthropic('idea','test-key','m',''), 'draft')
            self.assertTrue(post.call_args.args[0].endswith('/v1/messages'))
            self.assertNotIn('tools',post.call_args.args[2])
        with patch.object(providers,'_post',return_value={'choices':[{'message':{'content':'draft'}}]}) as post:
            providers.openai('idea','test-key','m','')
            self.assertTrue(post.call_args.args[0].endswith('/chat/completions'))
        import httpx
        with patch.object(httpx,'post',side_effect=httpx.ReadTimeout('secret-token')):
            with self.assertRaises(providers.ProviderError) as e: providers.openai('idea','test-key','m','')
        self.assertNotIn('secret-token',str(e.exception))

class WorkerTransports(unittest.TestCase):
    def worker(self):
        # Restore sys.path after importing the worker's script isolation guard.
        with patch.object(sys,'path',list(sys.path)):
            spec=importlib.util.spec_from_file_location('worker',ROOT/'app/hermes_worker.py')
            worker=importlib.util.module_from_spec(spec); spec.loader.exec_module(worker)
        return worker
    def test_native_transport_call_has_no_tools_or_fallback(self):
        for slug,mode in [('openai-codex','codex_responses'),('gemini','gemini'),('anthropic','anthropic_messages'),('claude-subscription-directsdk-experimental','chat_completions')]:
            with self.subTest(slug=slug):
                worker=self.worker(); calls=[]
                client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:calls.append(kw) or 'reply')))
                native=SimpleNamespace(resolve_provider_client=lambda **kw:(client,'m'), extract_content_or_reasoning=lambda *a,**kw:'draft')
                modules={'agent':SimpleNamespace(),'agent.auxiliary_client':native,
                         'hermes_cli':SimpleNamespace(),'hermes_cli.auth':SimpleNamespace(PROVIDER_REGISTRY={}),
                         'hermes_cli.config':SimpleNamespace(get_env_value=lambda n:None)}
                with patch.dict(sys.modules,modules), patch.object(worker,'descriptors',return_value=[dict(slug=slug,reason='',api_mode=mode)]):
                    result=worker.generate(dict(provider=slug,model='m',prompt='Toast'))
                self.assertEqual(result['served_provider'],slug)
                self.assertFalse(result['fallback']); self.assertEqual(len(calls),1)
                self.assertNotIn('tools',calls[0]); self.assertEqual(calls[0]['max_tokens'],2000)
    def test_missing_client_changed_route_and_unsafe_external(self):
        worker=self.worker()
        self.assertTrue(worker.unsupported('copilot-acp','external_process'))
        self.assertTrue(worker.unsupported('moa','virtual'))
        for client in (None,SimpleNamespace(_hermes_aux_effective_provider='paid-fallback')):
            native=SimpleNamespace(resolve_provider_client=lambda **kw:(client,'m'),extract_content_or_reasoning=lambda *a: 'must not call')
            with patch.dict(sys.modules,{'agent':SimpleNamespace(),'agent.auxiliary_client':native,
                'hermes_cli':SimpleNamespace(),'hermes_cli.auth':SimpleNamespace(PROVIDER_REGISTRY={}),
                'hermes_cli.config':SimpleNamespace(get_env_value=lambda n:None)}),patch.object(worker,'descriptors',return_value=[dict(slug='openai-codex',reason='',api_mode='codex_responses')]):
                self.assertIn('error',worker.generate(dict(provider='openai-codex',model='m',prompt='Toast')))
    def test_subprocess_timeout_is_bounded_and_kills_group(self):
        proc=SimpleNamespace(pid=999,returncode=0,communicate=lambda *a,**kw:(_ for _ in ()).throw(subprocess.TimeoutExpired('worker',90)),wait=lambda:None)
        with patch.object(hermes_bridge,'runtime',return_value=('/tmp','python')),patch.object(subprocess,'Popen',return_value=proc),patch.object(os,'killpg') as kill:
            with self.assertRaises(hermes_bridge.BridgeError): hermes_bridge.call({'action':'generate'})
            kill.assert_called_once()

class InstalledCatalogue(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('LARDER_TEST_HERMES_SOURCE'),'optional installed runtime inspection')
    def test_complete_installed_catalogue_offline(self):
        source=os.environ['LARDER_TEST_HERMES_SOURCE']
        env=dict(os.environ,LARDER_HERMES_SOURCE=source)
        with patch.dict(os.environ,env):
            hermes_bridge.catalogue.cache_clear()
            actual=hermes_bridge.catalogue()
        self.assertTrue(actual)
        # Compare independently to provider_catalog(), in a scratch home with only plugin code.
        with tempfile.TemporaryDirectory(prefix='larder-native-catalog-') as home:
            plugins=Path(os.environ.get('LARDER_HERMES_ACCOUNT_HOME',str(Path.home()/'.hermes')))/'plugins'
            if plugins.is_dir(): (Path(home)/'plugins').symlink_to(plugins)
            child={'PATH':os.environ['PATH'],'HOME':home,'HERMES_HOME':home,'PYTHONPATH':source,'PYTHONDONTWRITEBYTECODE':'1'}
            code="import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(OSError('offline')); from providers import list_providers; list_providers(); from hermes_cli.provider_catalog import provider_catalog; import json; print(json.dumps([p.slug for p in provider_catalog()]))"
            result=subprocess.run([str(Path(source)/'.venv/bin/python'),'-c',code],env=child,cwd=home,text=True,capture_output=True,timeout=20,check=True)
        self.assertEqual({p['slug'] for p in actual},set(json.loads(result.stdout)))
        hermes_bridge.catalogue.cache_clear()

class PrivateAccountSnapshot(unittest.TestCase):
    def test_refresh_persists_only_in_app_owned_account(self):
        from app import db as db_module
        with tempfile.TemporaryDirectory(prefix='larder-private-account-') as scratch:
            root=Path(scratch); account=root/'server-account'; account.mkdir()
            original={'providers':{'openai-codex':{'access_token':'synthetic-old-token'}}}
            (account/'auth.json').write_text(json.dumps(original))
            seen=[]
            def popen(argv, **kwargs):
                private=Path(kwargs['env']['HERMES_HOME'])
                seen.append(json.loads((private/'auth.json').read_text()))
                def communicate(payload,timeout):
                    self.assertEqual(timeout,90)
                    (private/'auth.json').write_text(json.dumps({'providers':{'openai-codex':{'access_token':'synthetic-refreshed-token'}}}))
                    kwargs['stdout'].write(json.dumps({'text':'draft','served_provider':'openai-codex'}).encode())
                return SimpleNamespace(communicate=communicate,returncode=0)
            env={'LARDER_HERMES_ACCOUNT_HOME':str(account)}
            with patch.object(Path,'home',return_value=root/'fake-home'),patch.dict(os.environ,env),patch.object(db_module,'DATA_DIR',root/'app-data'),patch.object(hermes_bridge,'runtime',return_value=('/tmp','python')),patch.object(subprocess,'Popen',side_effect=popen):
                for _ in range(2): hermes_bridge.call(dict(action='generate',provider='openai-codex',account_owner=1),reuse=True)
                hermes_bridge.call(dict(action='generate',provider='openai-codex',account_owner=2),reuse=True)
            self.assertEqual(seen[0],original)
            self.assertEqual(seen[1]['providers']['openai-codex']['access_token'],'synthetic-refreshed-token')
            self.assertEqual(seen[2],original)
            self.assertEqual(json.loads((account/'auth.json').read_text()),original)
            private=root/'app-data/provider_accounts/1/openai-codex/hermes/auth.json'
            self.assertEqual(private.stat().st_mode & 0o777,0o600)
    def test_invalid_snapshot_scope(self):
        for owner,slug in [(None,'openai-codex'),(1,'../../private')]:
            with self.assertRaises(hermes_bridge.BridgeError):
                hermes_bridge.call(dict(action='generate',provider=slug,account_owner=owner),reuse=True)

class OAuthRuntimeTransport(unittest.TestCase):
    def test_qwen_and_minimax_use_native_auth_and_correct_wire(self):
        for slug,mode in [('qwen-oauth','chat_completions'),('minimax-oauth','anthropic_messages')]:
            worker=WorkerTransports().worker(); resolved=[]; requests=[]
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:requests.append(kw) or SimpleNamespace(model='m'))))
            def resolve(**kwargs):
                resolved.append(kwargs); return client,'m'
            modules={'agent':SimpleNamespace(),'agent.auxiliary_client':SimpleNamespace(resolve_provider_client=resolve,extract_content_or_reasoning=lambda *a,**kw:'draft'),
                'hermes_cli':SimpleNamespace(),'hermes_cli.auth':SimpleNamespace(PROVIDER_REGISTRY={}),
                'hermes_cli.config':SimpleNamespace(get_env_value=lambda n:None),
                'hermes_cli.runtime_provider':SimpleNamespace(resolve_runtime_provider=lambda **kw:dict(provider=slug,api_mode=mode,api_key='synthetic-oauth-token',base_url='https://oauth.invalid/v1'))}
            with patch.dict(sys.modules,modules),patch.object(worker,'descriptors',return_value=[dict(slug=slug,reason='',api_mode='')]):
                result=worker.generate(dict(provider=slug,model='m',prompt='Toast'))
            self.assertEqual(resolved[0]['api_mode'],mode)
            self.assertEqual(resolved[0]['explicit_api_key'],'synthetic-oauth-token')
            self.assertEqual(result['served_provider'],slug)
            self.assertEqual(len(requests),1); self.assertNotIn('tools',requests[0])

class DescendantCleanup(unittest.TestCase):
    def test_external_sdk_groups_are_reaped(self):
        with tempfile.TemporaryDirectory(prefix='larder-child-groups-') as scratch:
            path=Path(scratch)/'groups'; path.write_text('234567\ninvalid\n0\n')
            with patch.object(os,'killpg') as kill:
                hermes_bridge.kill_child_groups(path)
            self.assertEqual(kill.call_args.args[0],234567)
            kill.assert_called_once()
