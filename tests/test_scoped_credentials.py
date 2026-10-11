import json
from pathlib import Path
import tempfile
import unittest
from app.hermes_bridge import copy_scoped_credential, credential_files

class ScopedCredentials(unittest.TestCase):
    def test_unrelated_accounts_and_keys_are_not_copied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'auth.json'
            original.write_text(json.dumps({'providers': {'openai-codex': {'token': 'synthetic-one'}, 'nous': {'token': 'synthetic-two'}}, 'credential_pool': {'openai-codex': [], 'nous': ['synthetic-secret']}, 'active_provider': 'nous'}))
            target = root / 'scoped.json'
            copy_scoped_credential(original, target, 'openai-codex', set())
            data = json.loads(target.read_text())
            self.assertEqual(set(data['providers']), {'openai-codex'})
            self.assertEqual(set(data['credential_pool']), {'openai-codex'})
            self.assertNotIn('active_provider', data)
            env = root / '.env'
            env.write_text('OPENAI_API_KEY=synthetic-key\nUNRELATED_SECRET=synthetic-other\nexport OPENAI_BASE_URL=https://example.invalid\n')
            copy_scoped_credential(env, target, 'openai-api', {'OPENAI_API_KEY'})
            self.assertEqual(target.read_text(), 'OPENAI_API_KEY=synthetic-key\n')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
    def test_external_logins_are_provider_specific(self):
        self.assertEqual(credential_files('openai-codex')[1], ['.codex/auth.json'])
        self.assertEqual(credential_files('openrouter')[1], [])
        self.assertNotIn('.copilot_jwt.json', credential_files('openai-codex')[0])
