"""Split incoming text and prepare continuous PCM audio streams."""

from collections.abc import AsyncGenerator
from contextlib import aclosing
import asyncio
import io
import re
import struct
from typing import TypeVar
import wave


_T = TypeVar("_T")
_PUNCTUATION = frozenset("、。！？!?，,.\n")
MIN_SEGMENT_LENGTH = 10
AUDIO_CHUNK_SIZE = 16384


_ENDING_PUNCTUATION = frozenset(".!?！？")
_CLOSING_QUOTES = frozenset("\"'”’」』）)]}】")
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc",
    "e.g", "i.e", "fig", "no", "dept", "inc", "ltd", "approx",
})


def _last_token(prefix: str) -> str:
    """Return the token immediately before a punctuation boundary."""
    if not prefix or prefix[-1].isspace():
        return ""
    return prefix.rsplit(maxsplit=1)[-1].lstrip('"\'“‘([{「『')


def _is_link(token: str) -> bool:
    """Recognize URL and email tokens without splitting their punctuation."""
    return bool(
        "://" in token or token.lower().startswith("www.") or "@" in token
        or re.match(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?=[:/?!#]|$)", token)
    )


def _should_split(prefix: str, punctuation: str, following: str) -> bool:
    """Distinguish sentence boundaries from numbers, abbreviations and links."""
    token = _last_token(prefix)
    if (
        _is_link(token) and not following.isspace()
        and following not in _CLOSING_QUOTES and following not in "、。"
    ):
        return False
    if (
        punctuation in (".", ",", "，") and prefix
        and prefix[-1].isdecimal() and following.isdecimal()
    ):
        return False
    if punctuation == ".":
        if (
            prefix and prefix[-1].isascii() and prefix[-1].isalnum()
            and following.isascii() and (following.isalpha() or following == "_")
        ):
            return False
        if (
            token.lower() in _ABBREVIATIONS
            or re.fullmatch(r"(?:[A-Za-z]\.){2,}", token + ".")
            or re.fullmatch(r"[A-Z]", token)
        ):
            return False
    return True


async def async_split_text(
    source: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """Split text on punctuation while preserving tokens and punctuation runs."""
    pending: list[str] = []
    boundary: int | None = None
    async with aclosing(source):
        async for chunk in source:
            for character in chunk:
                if boundary is not None:
                    if character in _ENDING_PUNCTUATION and pending[-1] in _ENDING_PUNCTUATION:
                        pending.append(character)
                        continue
                    if character in _CLOSING_QUOTES:
                        pending.append(character)
                        continue
                    punctuation = "".join(pending[boundary:]).rstrip("".join(_CLOSING_QUOTES))
                    if _should_split("".join(pending[:boundary]), punctuation, character):
                        yield "".join(pending)
                        pending.clear()
                    boundary = None
                pending.append(character)
                if character in _PUNCTUATION and len(pending) >= MIN_SEGMENT_LENGTH:
                    numeric_separator = (
                        character in ",，" and len(pending) >= 2
                        and pending[-2].isdecimal()
                    )
                    # 記号のまとまり、略語、リンク、数値は次の文字を見て判断します。
                    if (
                        character in _ENDING_PUNCTUATION or numeric_separator
                        or (character in ",，" and _is_link(_last_token("".join(pending[:-1]))))
                    ):
                        boundary = len(pending) - 1
                        if character in _ENDING_PUNCTUATION:
                            while boundary and pending[boundary - 1] in _ENDING_PUNCTUATION:
                                boundary -= 1
                        continue
                    yield "".join(pending)
                    pending.clear()
        if pending:
            yield "".join(pending)


async def async_prefetch(
    source: AsyncGenerator[_T, None], maxsize: int,
) -> AsyncGenerator[_T, None]:
    """Prefetch with bounded buffering and close the source on cancellation."""
    queue: asyncio.Queue[_T | Exception | None] = asyncio.Queue(maxsize=maxsize)

    async def produce() -> None:
        try:
            async with aclosing(source):
                async for item in source:
                    await queue.put(item)
        except Exception as err:
            await queue.put(err)
        else:
            await queue.put(None)

    task = asyncio.create_task(produce(), name="voicevox-prefetch")
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class WavStreamDecoder:
    """Extract PCM from a WAV response whose header may span multiple chunks."""

    def __init__(self) -> None:
        self.format: tuple[int, int, int] | None = None
        self.ready = False
        self._riff_seen = False
        self._buffer = bytearray()
        self._remaining = 0
        self._received = 0

    def feed(self, chunk: bytes) -> bytes:
        """Return available PCM without forwarding repeated WAV headers."""
        self._buffer.extend(chunk)
        if not self._riff_seen:
            if len(self._buffer) < 12:
                return b""
            if self._buffer[:4] != b"RIFF" or self._buffer[8:12] != b"WAVE":
                raise ValueError("Invalid WAV response")
            del self._buffer[:12]
            self._riff_seen = True

        while not self.ready:
            if len(self._buffer) < 8:
                return b""
            tag, length = struct.unpack_from("<4sI", self._buffer)
            if tag == b"data":
                if self.format is None:
                    raise ValueError("WAV response has no PCM format")
                del self._buffer[:8]
                self._remaining = length
                self.ready = True
                break
            if length > 65536:
                raise ValueError("WAV header chunk is too large")
            end = 8 + length + length % 2
            if len(self._buffer) < end:
                return b""
            if tag == b"fmt ":
                if length < 16:
                    raise ValueError("Invalid WAV format chunk")
                encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from(
                    "<HHIIHH", self._buffer, 8,
                )
                if (
                    encoding != 1 or channels not in (1, 2) or bits != 16
                    or rate <= 0 or alignment != channels * 2
                    or byte_rate != rate * alignment
                ):
                    raise ValueError("Unsupported WAV PCM format")
                self.format = (channels, bits // 8, rate)
            del self._buffer[:end]

        size = min(len(self._buffer), self._remaining)
        pcm = bytes(self._buffer[:size])
        self._remaining -= size
        self._received += size
        self._buffer.clear()
        return pcm

    def finish(self) -> None:
        """Reject truncated audio instead of continuing with a corrupt stream."""
        if (
            not self.ready or self._remaining or not self._received
            or self.format is None
            or self._received % (self.format[0] * self.format[1])
        ):
            raise ValueError("Incomplete WAV response")


def streaming_wav_header(audio_format: tuple[int, int, int]) -> bytes:
    """Create a WAV header with an unknown frame count for streaming playback."""
    with io.BytesIO() as output:
        with wave.open(output, "wb") as wav:
            channels, width, rate = audio_format
            wav.setnchannels(channels)
            wav.setsampwidth(width)
            wav.setframerate(rate)
        return output.getvalue()
