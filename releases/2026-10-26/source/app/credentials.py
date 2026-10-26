"""Runtime AI keys. Firestore contains references/tombstones, never secret payloads."""
import asyncio
import os
import re
from datetime import datetime, timezone

PROVIDERS = ('gemini', 'groq', 'openai', 'deepseek', 'mistral', 'openrouter','cloudflare','huggingface')
store = None


def validate_provider(provider):
    if provider not in PROVIDERS:
        raise ValueError('Неизвестный провайдер ИИ.')
    return provider


def validate_key(value):
    # Providers use different prefixes. Reject pasted commands, JSON and whitespace.
    if not isinstance(value, str) or not re.fullmatch(r'[!-~]{20,4096}', value) or any(c in value for c in '\"\'`<>'):
        raise ValueError('Нужен только API-ключ, без пробелов, кавычек и команд.')
    return value


def looks_like_key(value):
    return bool(re.search(r'(?:sk-[A-Za-z0-9_-]{12,}|hf_[A-Za-z0-9_-]{12,}|gsk_[A-Za-z0-9_-]{12,}|AIza[A-Za-z0-9_-]{12,}|AQ\.[A-Za-z0-9_-]{12,})', value)
                or re.fullmatch(r'[A-Za-z0-9]{24,}',value))


class KeyStore:
    def __init__(self, database, project, client=None):
        self.db, self.project, self._client = database, project, client
        self._cache = {}

    def client(self):
        if self._client is None:
            from google.cloud import secretmanager
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def parent(self, provider):
        validate_provider(provider)
        return f'projects/{self.project}/secrets/bot-ai-{provider}'

    def ref(self, provider):
        return self.db.collection('ai_credentials').document(validate_provider(provider))

    async def describe(self, provider):
        snap = await asyncio.to_thread(self.ref(provider).get)
        if not snap.exists:
            present = bool(os.getenv(provider.upper() + '_API_KEY'))
            return {'mode': 'environment' if present else 'missing', 'configured': present}
        value = snap.to_dict()
        # An explicit tombstone must never fall back to an old Cloud Run env key.
        configured = value.get('mode') == 'secret' and self.valid_version(provider, value.get('version', ''))
        return {'mode': value.get('mode', 'disabled'), 'configured': configured,
                'version': value.get('version', '') if configured else ''}

    def valid_version(self, provider, version):
        return isinstance(version, str) and bool(re.fullmatch(re.escape(self.parent(provider)) + r'/versions/[0-9]+', version))

    async def key(self, provider):
        info = await self.describe(provider)  # No metadata cache: removal affects every new request.
        if not info['configured']:
            self._cache.pop(provider, None)
            return ''
        if info['mode'] == 'environment':
            return os.getenv(provider.upper() + '_API_KEY', '')
        version = info['version']
        cached = self._cache.get(provider)
        if cached and cached[0] == version:
            return cached[1]
        def access():
            response = self.client().access_secret_version(request={'name': version}, retry=None, timeout=8)
            return response.payload.data.decode('utf-8')
        value = await asyncio.to_thread(access)
        self._cache[provider] = (version, value)
        return value

    async def save(self, provider, value, actor):
        value = validate_key(value)
        def add():
            return self.client().add_secret_version(request={
                'parent': self.parent(provider), 'payload': {'data': value.encode('utf-8')},
            }, retry=None, timeout=8).name
        returned = await asyncio.to_thread(add)
        # Secret Manager may canonicalize the project ID to a numeric project number.
        version = self.parent(provider) + '/versions/' + returned.rsplit('/',1)[-1]
        if not self.valid_version(provider, version):
            raise ValueError('Unexpected secret version')
        await asyncio.to_thread(self.ref(provider).set, {
            'mode': 'secret', 'version': version, 'updated_by': actor,
            'updated_at': datetime.now(timezone.utc),
        })
        self._cache.pop(provider, None)

    async def remove(self, provider, actor):
        await asyncio.to_thread(self.ref(provider).set, {
            'mode': 'disabled', 'updated_by': actor, 'updated_at': datetime.now(timezone.utc),
        })
        self._cache.pop(provider, None)


async def describe(provider):
    validate_provider(provider)
    if store is not None:
        return await store.describe(provider)
    present = bool(os.getenv(provider.upper() + '_API_KEY'))
    return {'mode': 'environment' if present else 'missing', 'configured': present}


async def get_key(provider):
    validate_provider(provider)
    if store is not None:
        return await store.key(provider)
    return os.getenv(provider.upper() + '_API_KEY', '')
