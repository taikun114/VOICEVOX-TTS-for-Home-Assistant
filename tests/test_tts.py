"""Test audio streaming and fallback to the synthesis API."""

import asyncio
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import wave

import aiohttp
from aiohttp import web

from homeassistant.components.tts import TTSAudioRequest
from homeassistant.exceptions import HomeAssistantError

from custom_components.voicevox_tts.config_flow import VOICEVOXTTSConfigFlow, VOICEVOXTTSOptionsFlowHandler
from custom_components.voicevox_tts.tts import VOICEVOXTTSEntity
from custom_components.voicevox_tts.streaming import (
    WavStreamDecoder, async_prefetch, async_split_text, streaming_wav_header,
)


def make_wav(pcm=b'\x01\x00' * 100, rate=24000):
    with io.BytesIO() as output:
        with wave.open(output, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            wav.writeframes(pcm)
        return output.getvalue()


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
        self.expected_texts = {'音声テスト'}
        self.legacy_audio = b'legacy-audio'
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
        self.assertIn(request.query['text'], self.expected_texts)
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
        return web.Response(body=self.legacy_audio, status=self.legacy_status)

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

    async def test_incremental_text_falls_back_for_each_segment(self):
        texts = ['最初のテストの文章です。', '次に読み上げる文章です。']
        self.expected_texts = set(texts)
        self.legacy_audio = make_wav()
        self.status = 404
        self.entry.options['process_text_incrementally'] = True
        async def messages():
            for text in texts:
                yield text
        response = await self.entity().async_stream_tts_audio(TTSAudioRequest('ja-JP', {}, messages()))
        audio = b''.join([chunk async for chunk in response.data_gen])
        self.assertEqual(audio[44:], b'\x01\x00' * 200)
        self.assertEqual(self.requests.count('/synthesis'), 2)
        self.assertEqual(self.requests.count('/streaming_synthesis'), 2)


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_options_default_and_saved_value(self):
        entry = SimpleNamespace(data={'host': 'test-engine', 'port': 50021}, options={'speaker': 1})
        flow = VOICEVOXTTSOptionsFlowHandler(entry)
        with patch('custom_components.voicevox_tts.config_flow.aiohttp.ClientSession') as session_class:
            session = MagicMock()
            session_class.return_value.__aenter__.return_value = session
            response = MagicMock()
            session.get.return_value.__aenter__.return_value = response
            response.json = AsyncMock(return_value=[{'name': '検証用の声', 'styles': [{'id': 1, 'name': '標準'}]}])
            for enabled in (False, True):
                if enabled:
                    entry.options['process_text_incrementally'] = True
                result = await flow.async_step_init()
                self.assertEqual(result['data_schema']({'speaker': '1'})['process_text_incrementally'], enabled)

    async def test_new_entry_defaults_to_streaming(self):
        flow = VOICEVOXTTSConfigFlow()
        flow.speakers = [{'id': 1, 'name': '検証用の声'}]
        result = await flow.async_step_speaker_selection()
        self.assertTrue(result['data_schema']({'speaker': '1'})['use_streaming'])
        self.assertFalse(result['data_schema']({'speaker': '1'})['process_text_incrementally'])

    async def test_new_entry_preserves_disabled_streaming(self):
        flow = VOICEVOXTTSConfigFlow()
        flow.speakers = [{'id': 1, 'name': '検証用の声'}]
        flow.config_data = {'host': 'test-engine', 'port': 50021}
        result = await flow.async_step_speaker_selection({
            'speaker': '1', 'volume': 1.0, 'pitch': 0.0,
            'speed': 1.0, 'use_streaming': False, 'process_text_incrementally': True,
        })
        self.assertFalse(result['options']['use_streaming'])
        self.assertTrue(result['options']['process_text_incrementally'])


class IncrementalAudioTests(unittest.IsolatedAsyncioTestCase):
    def entity(self):
        return VOICEVOXTTSEntity(None, SimpleNamespace(
            title='検証用音声', entry_id='test-entry',
            data={'host': 'test-engine', 'port': 50021},
            options={'speaker': 1, 'process_text_incrementally': True},
        ))

    async def test_audio_starts_before_input_text_finishes(self):
        release = asyncio.Event()
        input_finished = asyncio.Event()
        generated = []
        async def messages():
            yield 'なお、これは最初の文章です。'
            await release.wait()
            yield 'これは最後の文章です。'
            input_finished.set()
        async def synthesize(text, options):
            generated.append(text)
            audio = make_wav()
            for start in range(0, len(audio), 7):
                yield audio[start:start+7]
        entity = self.entity()
        with patch.object(entity, '_async_generate_audio', synthesize):
            response = await entity.async_stream_tts_audio(TTSAudioRequest('ja-JP', {}, messages()))
            stream = response.data_gen
            try:
                header = await asyncio.wait_for(anext(stream), 1)
                pcm = await asyncio.wait_for(anext(stream), 1)
                self.assertEqual(header[:4], b'RIFF')
                self.assertTrue(pcm)
                self.assertFalse(input_finished.is_set())
                release.set()
                rest = b''.join([chunk async for chunk in stream])
                self.assertEqual((header + pcm + rest)[44:], b'\x01\x00' * 200)
                self.assertEqual(generated, ['なお、これは最初の文章です。', 'これは最後の文章です。'])
            finally:
                await stream.aclose()

    async def test_synthesis_prefetches_without_waiting_for_playback(self):
        second_started = asyncio.Event()
        calls = []
        async def messages():
            yield '最初のテストの文章です。次に読み上げる文章です。'
        async def synthesize(text, options):
            calls.append(text)
            if len(calls) == 2:
                second_started.set()
            yield make_wav(bytes([len(calls), 0]) * 100)
        entity = self.entity()
        with patch.object(entity, '_async_generate_audio', synthesize):
            response = await entity.async_stream_tts_audio(TTSAudioRequest('ja-JP', {}, messages()))
            stream = response.data_gen
            try:
                header = await anext(stream)
                await asyncio.wait_for(second_started.wait(), 1)
                pcm = b''.join([chunk async for chunk in stream])
                self.assertEqual(header[:4], b'RIFF')
                self.assertEqual(pcm, b'\x01\x00' * 100 + b'\x02\x00' * 100)
            finally:
                await stream.aclose()

    async def test_cancel_closes_input_and_active_synthesis(self):
        input_closed = asyncio.Event()
        synthesis_closed = asyncio.Event()
        block = asyncio.Event()
        async def messages():
            try:
                yield '最初のテストの文章です。'
                await block.wait()
            finally:
                input_closed.set()
        async def synthesize(text, options):
            try:
                yield make_wav()
                await block.wait()
            finally:
                synthesis_closed.set()
        entity = self.entity()
        with patch.object(entity, '_async_generate_audio', synthesize):
            response = await entity.async_stream_tts_audio(TTSAudioRequest('ja-JP', {}, messages()))
            await anext(response.data_gen)
            await response.data_gen.aclose()
        self.assertTrue(input_closed.is_set())
        self.assertTrue(synthesis_closed.is_set())

    async def test_format_change_is_rejected(self):
        count = 0
        async def messages():
            yield '最初のテストの文章です。次に読み上げる文章です。'
        async def synthesize(text, options):
            nonlocal count
            count += 1
            yield make_wav(rate=24000 if count == 1 else 48000)
        entity = self.entity()
        with patch.object(entity, '_async_generate_audio', synthesize):
            response = await entity.async_stream_tts_audio(TTSAudioRequest('ja-JP', {}, messages()))
            with self.assertRaises(HomeAssistantError):
                _ = [chunk async for chunk in response.data_gen]


class StreamingHelpersTests(unittest.IsolatedAsyncioTestCase):
    async def test_english_boundaries_are_stable_across_chunk_sizes(self):
        cases = (
            ('Hello, this is a streaming test. The next sentence should follow naturally.',
             ['Hello, this is a streaming test.', ' The next sentence should follow naturally.']),
            ('Please ask Dr. Smith about the U.S. office, e.g. the New York branch.',
             ['Please ask Dr. Smith about the U.S. office,', ' e.g. the New York branch.']),
            ('Please ask Mr. Jones and Prof. Brown to help J. Smith.',
             ['Please ask Mr. Jones and Prof. Brown to help J. Smith.']),
            ('Please visit https://example.com/docs, or email support@example.com for help.',
             ['Please visit https://example.com/docs,', ' or email support@example.com for help.']),
            ('Please visit https://example.com/a,b?x=1.23&y=2 now. Then continue.',
             ['Please visit https://example.com/a,b?x=1.23&y=2 now.', ' Then continue.']),
            ('Please visit example.com/docs?a=b,c for details. Then continue.',
             ['Please visit example.com/docs?a=b,c for details.', ' Then continue.']),
            ('Is this really happening?! Yes, it is!!!',
             ['Is this really happening?!', ' Yes, it is!!!']),
            ('He asked, "Is this really happening?!" Then we left.',
             ['He asked, "Is this really happening?!"', ' Then we left.']),
            ('これは本当でしょうか！？はい、そうです！！！最後です。',
             ['これは本当でしょうか！？', 'はい、そうです！！！', '最後です。']),
        )
        for text, expected in cases:
            chunkings = [[text], list(text)]
            chunkings.extend([text[:cut], text[cut:]] for cut in range(1, len(text)))
            for chunks in chunkings:
                with self.subTest(text=text, chunks=chunks):
                    async def source():
                        for chunk in chunks:
                            yield chunk
                    parts = [part async for part in async_split_text(source())]
                    self.assertEqual(parts, expected)
                    self.assertEqual(''.join(parts), text)

    async def test_numeric_separators_do_not_split_across_chunks(self):
        for text in (
            '価格は税込で合計1,234.56円です。次です。',
            '価格は税込で合計１，２３４.５６円です。次です。',
        ):
            with self.subTest(text=text):
                async def source():
                    for character in text:
                        yield character
                parts = [part async for part in async_split_text(source())]
                self.assertEqual(parts, [text[:text.index('。') + 1], '次です。'])
                self.assertEqual(''.join(parts), text)

    async def test_numeric_separator_with_spaces_still_splits(self):
        for text, expected in (
            ('金額のサンプルは123, 456です。', ['金額のサンプルは123,', ' 456です。']),
            ('金額のサンプルは123 .456です。', ['金額のサンプルは123 .', '456です。']),
        ):
            with self.subTest(text=text):
                async def source():
                    yield text
                self.assertEqual([part async for part in async_split_text(source())], expected)

    async def test_ellipsis_is_kept_together_across_chunks(self):
        for prefix in ('これは少し長めの説明です', '1234567', '前置きが十分に長い123'):
            with self.subTest(prefix=prefix):
                async def source():
                    for chunk in (prefix, '.', '.', '.', '続きです。'):
                        yield chunk
                self.assertEqual(
                    [part async for part in async_split_text(source())],
                    [prefix + '...', '続きです。'],
                )

    async def test_trailing_periods_are_flushed_at_end_of_input(self):
        for ending in ('.', '...'):
            with self.subTest(ending=ending):
                text = 'これが末尾の文章です123' + ending
                async def source():
                    yield text
                self.assertEqual([part async for part in async_split_text(source())], [text])

    async def test_text_is_split_after_minimum_length_across_chunks(self):
        text = 'なお、これは最初の文章です。次は最後の文章です。末尾'
        async def source():
            for character in text:
                yield character
        parts = [part async for part in async_split_text(source())]
        self.assertEqual(parts, ['なお、これは最初の文章です。', '次は最後の文章です。', '末尾'])
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(part) >= 10 for part in parts[:-1]))

    async def test_final_text_without_punctuation_is_not_lost(self):
        async def source():
            yield '短い'
            yield '応答'
        self.assertEqual([part async for part in async_split_text(source())], ['短い応答'])

    async def test_prefetch_runs_ahead_and_closes_on_cancellation(self):
        ahead = asyncio.Event()
        closed = asyncio.Event()
        async def source():
            try:
                yield b'first'
                ahead.set()
                while True:
                    yield b'next'
            finally:
                closed.set()
        stream = async_prefetch(source(), maxsize=2)
        self.assertEqual(await anext(stream), b'first')
        await asyncio.wait_for(ahead.wait(), 1)
        await stream.aclose()
        self.assertTrue(closed.is_set())

    async def test_prefetch_propagates_failure_after_queued_data(self):
        async def source():
            yield b'first'
            raise ValueError('test failure')
        stream = async_prefetch(source(), maxsize=2)
        self.assertEqual(await anext(stream), b'first')
        with self.assertRaises(ValueError):
            await anext(stream)

    def test_fragmented_wav_header_and_pcm_are_decoded(self):
        with io.BytesIO() as output:
            with wave.open(output, 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b'\x01\x00' * 100)
            audio = output.getvalue()
        decoder = WavStreamDecoder()
        pcm = b''.join(decoder.feed(audio[start:start+3]) for start in range(0, len(audio), 3))
        decoder.finish()
        self.assertEqual(pcm, b'\x01\x00' * 100)
        self.assertEqual(decoder.format, (1, 2, 24000))
        with wave.open(io.BytesIO(streaming_wav_header(decoder.format))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
            self.assertEqual(wav.getnframes(), 0)

    def test_truncated_and_invalid_wav_are_rejected(self):
        decoder = WavStreamDecoder()
        with self.assertRaises(ValueError):
            decoder.feed(b'not a WAV file')
        decoder = WavStreamDecoder()
        decoder.feed(b'RIFF')
        with self.assertRaises(ValueError):
            decoder.finish()


if __name__ == '__main__':
    unittest.main()
