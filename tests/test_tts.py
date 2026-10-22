"""Test audio streaming and fallback to the synthesis API."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError

from custom_components.voicevox_tts.config_flow import VOICEVOXTTSConfigFlow
from custom_components.voicevox_tts.tts import VOICEVOXTTSEntity


class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.query = None
        self.release = asyncio.Event()
        self.completed = asyncio.Event()
        self.status = 200
        self.partial_failure = False
        self.query_status = 200
        self.legacy_status = 200
        app = web.Application()
        app.router.add_post('/audio_query', self.audio_query)
        app.router.add_post('/streaming_synthesis', self.streaming)
        app.router.add_post('/synthesis', self.synthesis)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.entry = SimpleNamespace(
            title='検証用音声', entry_id='test-entry',
            data={'host': '127.0.0.1', 'port': port},
            options={'speaker': 1},
        )
        self.session = aiohttp.ClientSession()
        self.session_patch = patch(
            'custom_components.voicevox_tts.tts.async_get_clientsession',
            return_value=self.session,
        )
        self.session_patch.start()

    async def asyncTearDown(self):
        self.release.set()
        self.session_patch.stop()
        await self.session.close()
        await self.runner.cleanup()

    async def audio_query(self, request):
        self.requests.append(request.path)
        self.assertEqual(request.query['text'], '音声テスト')
        return web.json_response({}, status=self.query_status)

    async def streaming(self, request):
        self.requests.append(request.path)
        self.query = await request.json()
        if self.status != 200:
            return web.Response(status=self.status)
        response = web.StreamResponse(headers={'Content-Type': 'audio/wav'})
        await response.prepare(request)
        await response.write(b'first-audio')
        await self.release.wait()
        if self.partial_failure:
            request.transport.close()
            return response
        try:
            await response.write(b'last-audio')
            await response.write_eof()
        except ConnectionResetError:
            pass
        self.completed.set()
        return response

    async def synthesis(self, request):
        self.requests.append(request.path)
        self.query = await request.json()
        return web.Response(body=b'legacy-audio', status=self.legacy_status)

    def entity(self):
        return VOICEVOXTTSEntity(None, self.entry)

    async def response(self, entity, options=None):
        async def messages():
            yield '音声'
            yield 'テスト'
        return await entity.async_stream_tts_audio(
            TTSAudioRequest('ja-JP', options or {}, messages())
        )

    async def test_default_streams_before_generation_finishes(self):
        entity = self.entity()
        self.assertTrue(entity.use_streaming)
        response = await self.response(entity, {'volume': 0.7, 'pitch': 0.1, 'speed': 1.2})
        self.assertEqual(response.extension, 'wav')
        first = await asyncio.wait_for(anext(response.data_gen), 2)
        self.assertEqual(first, b'first-audio')
        self.assertFalse(self.completed.is_set())
        self.release.set()
        rest = b''.join([chunk async for chunk in response.data_gen])
        self.assertEqual(first + rest, b'first-audiolast-audio')
        self.assertEqual(self.requests, ['/audio_query', '/streaming_synthesis'])
        self.assertEqual(self.query, {'volumeScale': 0.7, 'pitchScale': 0.1, 'speedScale': 1.2})

    async def test_disabled_uses_only_legacy_api(self):
        self.entry.options['use_streaming'] = False
        response = await self.response(self.entity())
        self.assertEqual(b''.join([chunk async for chunk in response.data_gen]), b'legacy-audio')
        self.assertEqual(self.requests, ['/audio_query', '/synthesis'])

    async def test_unavailable_api_falls_back(self):
        for status in (404, 405, 501, 500):
            with self.subTest(status=status):
                self.requests.clear()
                self.status = status
                response = await self.response(self.entity())
                self.assertEqual(b''.join([chunk async for chunk in response.data_gen]), b'legacy-audio')
                self.assertEqual(self.requests, ['/audio_query', '/streaming_synthesis', '/synthesis'])

    async def test_partial_failure_does_not_repeat_audio(self):
        self.partial_failure = True
        response = await self.response(self.entity())
        self.assertEqual(await anext(response.data_gen), b'first-audio')
        self.release.set()
        with self.assertRaises(HomeAssistantError):
            await anext(response.data_gen)
        self.assertNotIn('/synthesis', self.requests)

    async def test_closing_stream_keeps_shared_session_open(self):
        response = await self.response(self.entity())
        await anext(response.data_gen)
        await response.data_gen.aclose()
        self.assertFalse(self.session.closed)
        self.release.set()

    async def test_query_failure_is_not_returned_as_audio(self):
        self.query_status = 500
        response = await self.response(self.entity())
        with self.assertRaises(HomeAssistantError):
            await anext(response.data_gen)
        self.assertEqual(self.requests, ['/audio_query'])

    async def test_legacy_failure_returns_no_audio(self):
        self.entry.options['use_streaming'] = False
        self.legacy_status = 500
        self.assertEqual(await self.entity().async_get_tts_audio('音声テスト', 'ja-JP', {}), (None, None))

    async def test_full_audio_method_preserves_stream(self):
        self.release.set()
        extension, audio = await self.entity().async_get_tts_audio('音声テスト', 'ja-JP', {})
        self.assertEqual((extension, audio), ('wav', b'first-audiolast-audio'))


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):

    async def test_new_entry_defaults_to_streaming(self):
        flow = VOICEVOXTTSConfigFlow()
        flow.speakers = [{'id': 1, 'name': '検証用の声'}]
        result = await flow.async_step_speaker_selection()
        self.assertTrue(result['data_schema']({'speaker': '1'})['use_streaming'])

    async def test_new_entry_preserves_disabled_streaming(self):
        flow = VOICEVOXTTSConfigFlow()
        flow.speakers = [{'id': 1, 'name': '検証用の声'}]
        flow.config_data = {'host': 'test-engine', 'port': 50021}
        result = await flow.async_step_speaker_selection({
            'speaker': '1', 'volume': 1.0, 'pitch': 0.0,
            'speed': 1.0, 'use_streaming': False,
        })
        self.assertFalse(result['options']['use_streaming'])


if __name__ == '__main__':
    unittest.main()
