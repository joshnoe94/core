"""Test ESPHome voice assistant server."""

import asyncio
import io
import socket
from unittest.mock import Mock, patch
import wave

from aioesphomeapi import VoiceAssistantEventType
import pytest

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_pipeline.error import (
    WakeWordDetectionAborted,
    WakeWordDetectionError,
)
from homeassistant.components.esphome import DomainData
from homeassistant.components.esphome.voice_assistant import VoiceAssistantUDPServer
from homeassistant.core import HomeAssistant

_TEST_INPUT_TEXT = "This is an input test"
_TEST_OUTPUT_TEXT = "This is an output test"
_TEST_OUTPUT_URL = "output.mp3"
_TEST_MEDIA_ID = "12345"

_ONE_SECOND = 16000 * 2  # 16Khz 16-bit


@pytest.fixture
def voice_assistant_udp_server(
    hass: HomeAssistant,
) -> VoiceAssistantUDPServer:
    """Return the UDP server factory."""

    def _voice_assistant_udp_server(entry):
        entry_data = DomainData.get(hass).get_entry_data(entry)

        server: VoiceAssistantUDPServer = None

        def handle_finished():
            nonlocal server
            assert server is not None
            server.close()

        server = VoiceAssistantUDPServer(hass, entry_data, Mock(), handle_finished)
        return server  # noqa: RET504

    return _voice_assistant_udp_server


@pytest.fixture
def voice_assistant_udp_server_v1(
    voice_assistant_udp_server,
    mock_voice_assistant_v1_entry,
) -> VoiceAssistantUDPServer:
    """Return the UDP server."""
    return voice_assistant_udp_server(entry=mock_voice_assistant_v1_entry)


@pytest.fixture
def voice_assistant_udp_server_v2(
    voice_assistant_udp_server,
    mock_voice_assistant_v2_entry,
) -> VoiceAssistantUDPServer:
    """Return the UDP server."""
    return voice_assistant_udp_server(entry=mock_voice_assistant_v2_entry)


@pytest.fixture
def test_wav() -> bytes:
    """Return one second of empty WAV audio."""
    with io.BytesIO() as wav_io:
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setframerate(16000)
            wav_file.setsampwidth(2)
            wav_file.setnchannels(1)
            wav_file.writeframes(bytes(_ONE_SECOND))

        return wav_io.getvalue()


async def test_pipeline_events(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test that the pipeline function is called."""

    async def async_pipeline_from_audio_stream(*args, device_id, **kwargs):
        assert device_id == "mock-device-id"

        event_callback = kwargs["event_callback"]

        event_callback(
            PipelineEvent(
                type=PipelineEventType.WAKE_WORD_END,
                data={"wake_word_output": {}},
            )
        )

        # Fake events
        event_callback(
            PipelineEvent(
                type=PipelineEventType.STT_START,
                data={},
            )
        )

        event_callback(
            PipelineEvent(
                type=PipelineEventType.STT_END,
                data={"stt_output": {"text": _TEST_INPUT_TEXT}},
            )
        )

        event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_START,
                data={"tts_input": _TEST_OUTPUT_TEXT},
            )
        )

        event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={"tts_output": {"url": _TEST_OUTPUT_URL}},
            )
        )

    def handle_event(
        event_type: VoiceAssistantEventType, data: dict[str, str] | None
    ) -> None:
        if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_STT_END:
            assert data is not None
            assert data["text"] == _TEST_INPUT_TEXT
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START:
            assert data is not None
            assert data["text"] == _TEST_OUTPUT_TEXT
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END:
            assert data is not None
            assert data["url"] == _TEST_OUTPUT_URL
        elif event_type == VoiceAssistantEventType.VOICE_ASSISTANT_WAKE_WORD_END:
            assert data is None

    voice_assistant_udp_server_v1.handle_event = handle_event

    with patch(
        "homeassistant.components.esphome.voice_assistant.async_pipeline_from_audio_stream",
        new=async_pipeline_from_audio_stream,
    ):
        voice_assistant_udp_server_v1.transport = Mock()

        await voice_assistant_udp_server_v1.run_pipeline(
            device_id="mock-device-id", conversation_id=None
        )


async def test_udp_server(
    hass: HomeAssistant,
    socket_enabled,
    unused_udp_port_factory,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server runs and queues incoming data."""
    port_to_use = unused_udp_port_factory()

    with patch(
        "homeassistant.components.esphome.voice_assistant.UDP_PORT", new=port_to_use
    ):
        port = await voice_assistant_udp_server_v1.start_server()
        assert port == port_to_use

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        assert voice_assistant_udp_server_v1.queue.qsize() == 0
        sock.sendto(b"test", ("127.0.0.1", port))

        # Give the socket some time to send/receive the data
        async with asyncio.timeout(1):
            while voice_assistant_udp_server_v1.queue.qsize() == 0:
                await asyncio.sleep(0.1)

        assert voice_assistant_udp_server_v1.queue.qsize() == 1

        voice_assistant_udp_server_v1.stop()
        voice_assistant_udp_server_v1.close()

        assert voice_assistant_udp_server_v1.transport.is_closing()


async def test_udp_server_queue(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server queues incoming data."""

    voice_assistant_udp_server_v1.started = True

    assert voice_assistant_udp_server_v1.queue.qsize() == 0

    voice_assistant_udp_server_v1.datagram_received(bytes(1024), ("localhost", 0))
    assert voice_assistant_udp_server_v1.queue.qsize() == 1

    voice_assistant_udp_server_v1.datagram_received(bytes(1024), ("localhost", 0))
    assert voice_assistant_udp_server_v1.queue.qsize() == 2

    async for data in voice_assistant_udp_server_v1._iterate_packets():
        assert data == bytes(1024)
        break
    assert voice_assistant_udp_server_v1.queue.qsize() == 1  # One message removed

    voice_assistant_udp_server_v1.stop()
    assert (
        voice_assistant_udp_server_v1.queue.qsize() == 2
    )  # An empty message added by stop

    voice_assistant_udp_server_v1.datagram_received(bytes(1024), ("localhost", 0))
    assert (
        voice_assistant_udp_server_v1.queue.qsize() == 2
    )  # No new messages added after stop

    voice_assistant_udp_server_v1.close()

    # Stopping the UDP server should cause _iterate_packets to break out
    # immediately without yielding any data.
    has_data = False
    async for _data in voice_assistant_udp_server_v1._iterate_packets():
        has_data = True

    assert not has_data, "Server was stopped"


async def test_error_calls_handle_finished(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test that the handle_finished callback is called when an error occurs."""
    voice_assistant_udp_server_v1.handle_finished = Mock()

    voice_assistant_udp_server_v1.error_received(Exception())

    voice_assistant_udp_server_v1.handle_finished.assert_called()


async def test_udp_server_multiple(
    hass: HomeAssistant,
    socket_enabled,
    unused_udp_port_factory,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test that the UDP server raises an error if started twice."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.UDP_PORT",
        new=unused_udp_port_factory(),
    ):
        await voice_assistant_udp_server_v1.start_server()

    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.UDP_PORT",
            new=unused_udp_port_factory(),
        ),
        pytest.raises(RuntimeError),
    ):
        await voice_assistant_udp_server_v1.start_server()


async def test_udp_server_after_stopped(
    hass: HomeAssistant,
    socket_enabled,
    unused_udp_port_factory,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test that the UDP server raises an error if started after stopped."""
    voice_assistant_udp_server_v1.close()
    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.UDP_PORT",
            new=unused_udp_port_factory(),
        ),
        pytest.raises(RuntimeError),
    ):
        await voice_assistant_udp_server_v1.start_server()


async def test_unknown_event_type(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server does not call handle_event for unknown events."""
    voice_assistant_udp_server_v1._event_callback(
        PipelineEvent(
            type="unknown-event",
            data={},
        )
    )

    assert not voice_assistant_udp_server_v1.handle_event.called


async def test_error_event_type(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server calls event handler with error."""
    voice_assistant_udp_server_v1._event_callback(
        PipelineEvent(
            type=PipelineEventType.ERROR,
            data={"code": "code", "message": "message"},
        )
    )

    voice_assistant_udp_server_v1.handle_event.assert_called_with(
        VoiceAssistantEventType.VOICE_ASSISTANT_ERROR,
        {"code": "code", "message": "message"},
    )


async def test_send_tts_not_called(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server with a v1 device does not call _send_tts."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.VoiceAssistantUDPServer._send_tts"
    ) as mock_send_tts:
        voice_assistant_udp_server_v1._event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={
                    "tts_output": {"media_id": _TEST_MEDIA_ID, "url": _TEST_OUTPUT_URL}
                },
            )
        )

        mock_send_tts.assert_not_called()


async def test_send_tts_called(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server with a v2 device calls _send_tts."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.VoiceAssistantUDPServer._send_tts"
    ) as mock_send_tts:
        voice_assistant_udp_server_v2._event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={
                    "tts_output": {"media_id": _TEST_MEDIA_ID, "url": _TEST_OUTPUT_URL}
                },
            )
        )

        mock_send_tts.assert_called_with(_TEST_MEDIA_ID)


async def test_send_tts_not_called_when_empty(
    hass: HomeAssistant,
    voice_assistant_udp_server_v1: VoiceAssistantUDPServer,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server with a v1/v2 device doesn't call _send_tts when the output is empty."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.VoiceAssistantUDPServer._send_tts"
    ) as mock_send_tts:
        voice_assistant_udp_server_v1._event_callback(
            PipelineEvent(type=PipelineEventType.TTS_END, data={"tts_output": {}})
        )

        mock_send_tts.assert_not_called()

        voice_assistant_udp_server_v2._event_callback(
            PipelineEvent(type=PipelineEventType.TTS_END, data={"tts_output": {}})
        )

        mock_send_tts.assert_not_called()


async def test_send_tts(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
    test_wav,
) -> None:
    """Test the UDP server calls sendto to transmit audio data to device."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.tts.async_get_media_source_audio",
        return_value=("wav", test_wav),
    ):
        voice_assistant_udp_server_v2.started = True
        voice_assistant_udp_server_v2.transport = Mock(spec=asyncio.DatagramTransport)
        with patch.object(
            voice_assistant_udp_server_v2.transport, "is_closing", return_value=False
        ):
            voice_assistant_udp_server_v2._event_callback(
                PipelineEvent(
                    type=PipelineEventType.TTS_END,
                    data={
                        "tts_output": {
                            "media_id": _TEST_MEDIA_ID,
                            "url": _TEST_OUTPUT_URL,
                        }
                    },
                )
            )

            await voice_assistant_udp_server_v2._tts_done.wait()

            voice_assistant_udp_server_v2.transport.sendto.assert_called()


async def test_send_tts_wrong_sample_rate(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test the UDP server calls sendto to transmit audio data to device."""
    with io.BytesIO() as wav_io:
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setframerate(22050)
            wav_file.setsampwidth(2)
            wav_file.setnchannels(1)
            wav_file.writeframes(bytes(_ONE_SECOND))

        wav_bytes = wav_io.getvalue()
    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.tts.async_get_media_source_audio",
            return_value=("wav", wav_bytes),
        ),
        pytest.raises(ValueError),
    ):
        voice_assistant_udp_server_v2.started = True
        voice_assistant_udp_server_v2.transport = Mock(spec=asyncio.DatagramTransport)

        voice_assistant_udp_server_v2._event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={
                    "tts_output": {"media_id": _TEST_MEDIA_ID, "url": _TEST_OUTPUT_URL}
                },
            )
        )

        assert voice_assistant_udp_server_v2._tts_task is not None
        await voice_assistant_udp_server_v2._tts_task  # raises ValueError


async def test_send_tts_wrong_format(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test that only WAV audio will be streamed."""
    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.tts.async_get_media_source_audio",
            return_value=("raw", bytes(1024)),
        ),
        pytest.raises(ValueError),
    ):
        voice_assistant_udp_server_v2.started = True
        voice_assistant_udp_server_v2.transport = Mock(spec=asyncio.DatagramTransport)

        voice_assistant_udp_server_v2._event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={
                    "tts_output": {"media_id": _TEST_MEDIA_ID, "url": _TEST_OUTPUT_URL}
                },
            )
        )

        assert voice_assistant_udp_server_v2._tts_task is not None
        await voice_assistant_udp_server_v2._tts_task  # raises ValueError


async def test_send_tts_not_started(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
    test_wav,
) -> None:
    """Test the UDP server does not call sendto when not started."""
    with patch(
        "homeassistant.components.esphome.voice_assistant.tts.async_get_media_source_audio",
        return_value=("wav", test_wav),
    ):
        voice_assistant_udp_server_v2.started = False
        voice_assistant_udp_server_v2.transport = Mock(spec=asyncio.DatagramTransport)

        voice_assistant_udp_server_v2._event_callback(
            PipelineEvent(
                type=PipelineEventType.TTS_END,
                data={
                    "tts_output": {"media_id": _TEST_MEDIA_ID, "url": _TEST_OUTPUT_URL}
                },
            )
        )

        await voice_assistant_udp_server_v2._tts_done.wait()

        voice_assistant_udp_server_v2.transport.sendto.assert_not_called()


async def test_wake_word(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test that the pipeline is set to start with Wake word."""

    async def async_pipeline_from_audio_stream(*args, start_stage, **kwargs):
        assert start_stage == PipelineStage.WAKE_WORD

    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.async_pipeline_from_audio_stream",
            new=async_pipeline_from_audio_stream,
        ),
        patch("asyncio.Event.wait"),  # TTS wait event
    ):
        voice_assistant_udp_server_v2.transport = Mock()

        await voice_assistant_udp_server_v2.run_pipeline(
            device_id="mock-device-id",
            conversation_id=None,
            flags=2,
        )


async def test_wake_word_exception(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test that the pipeline is set to start with Wake word."""

    async def async_pipeline_from_audio_stream(*args, **kwargs):
        raise WakeWordDetectionError("pipeline-not-found", "Pipeline not found")

    with patch(
        "homeassistant.components.esphome.voice_assistant.async_pipeline_from_audio_stream",
        new=async_pipeline_from_audio_stream,
    ):
        voice_assistant_udp_server_v2.transport = Mock()

        def handle_event(
            event_type: VoiceAssistantEventType, data: dict[str, str] | None
        ) -> None:
            if event_type == VoiceAssistantEventType.VOICE_ASSISTANT_ERROR:
                assert data is not None
                assert data["code"] == "pipeline-not-found"
                assert data["message"] == "Pipeline not found"

        voice_assistant_udp_server_v2.handle_event = handle_event

        await voice_assistant_udp_server_v2.run_pipeline(
            device_id="mock-device-id",
            conversation_id=None,
            flags=2,
        )


async def test_wake_word_abort_exception(
    hass: HomeAssistant,
    voice_assistant_udp_server_v2: VoiceAssistantUDPServer,
) -> None:
    """Test that the pipeline is set to start with Wake word."""

    async def async_pipeline_from_audio_stream(*args, **kwargs):
        raise WakeWordDetectionAborted

    with (
        patch(
            "homeassistant.components.esphome.voice_assistant.async_pipeline_from_audio_stream",
            new=async_pipeline_from_audio_stream,
        ),
        patch.object(
            voice_assistant_udp_server_v2, "handle_event"
        ) as mock_handle_event,
    ):
        voice_assistant_udp_server_v2.transport = Mock()

        await voice_assistant_udp_server_v2.run_pipeline(
            device_id="mock-device-id",
            conversation_id=None,
            flags=2,
        )

        mock_handle_event.assert_not_called()
