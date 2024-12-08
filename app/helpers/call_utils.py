import json
import re
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager, suppress
from enum import Enum

from azure.communication.callautomation import (
    FileSource,
    PhoneNumberIdentifier,
    RecognitionChoice,
    RecognizeInputType,
    SsmlSource,
)
from azure.communication.callautomation._generated.models import (
    StartMediaStreamingRequest,
)
from azure.communication.callautomation.aio import (
    CallAutomationClient,
    CallConnectionClient,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

from app.helpers.config import CONFIG
from app.helpers.logging import logger
from app.models.call import CallStateModel
from app.models.message import (
    MessageModel,
    PersonaEnum as MessagePersonaEnum,
    StyleEnum as MessageStyleEnum,
)

_MAX_CHARACTERS_PER_TTS = 400  # Azure Speech Service TTS limit is 400 characters
_SENTENCE_PUNCTUATION_R = (
    r"([!?;]+|[\.\-:]+(?:$| ))"  # Split by sentence by punctuation
)
_TTS_SANITIZER_R = re.compile(
    r"[^\w\sÀ-ÿ'«»“”\"\"‘’''(),.!?;:\-\+_@/&€$%=]"  # noqa: RUF001
)  # Sanitize text for TTS

_db = CONFIG.database.instance()


class CallHangupException(Exception):
    """
    Exception raised when a call is hung up.
    """

    pass


class ContextEnum(str, Enum):
    """
    Enum for call context.

    Used to track the operation context of a call in Azure Communication Services.
    """

    CONNECT_AGENT = "connect_agent"  # Transfer to agent
    GOODBYE = "goodbye"  # Hang up
    IVR_LANG_SELECT = "ivr_lang_select"  # IVR language selection
    TRANSFER_FAILED = "transfer_failed"  # Transfer failed


def tts_sentence_split(
    text: str, include_last: bool
) -> Generator[tuple[str, int], None, None]:
    """
    Split a text into sentences.

    Whitespaces are not returned, but punctiation is kept as it was in the original text.

    Example:
    - Input: "Hello, world! How are you? I'm fine. Thank you... Goodbye!"
    - Output: [("Hello, world!", 13), ("How are you?", 12), ("I'm fine.", 9), ("Thank you...", 13), ("Goodbye!", 8)]

    Returns a generator of tuples with the sentence and the original sentence length.
    """
    # Split by sentence by punctuation
    splits = re.split(_SENTENCE_PUNCTUATION_R, text)
    for i, split in enumerate(splits):
        # Skip punctuation
        if i % 2 == 1:
            continue
        # Skip empty lines
        if not split.strip():
            continue
        # Skip last line in case of missing punctuation
        if i == len(splits) - 1:
            if include_last:
                yield (
                    split.strip(),
                    len(split),
                )
        # Add punctuation back
        else:
            yield (
                split.strip() + splits[i + 1].strip(),
                len(split) + len(splits[i + 1]),
            )


async def _handle_play_text(
    call: CallStateModel,
    client: CallAutomationClient,
    context: ContextEnum | None,
    style: MessageStyleEnum,
    text: str,
) -> bool:
    """
    Play a text to a call participant.

    If `context` is provided, it will be used to track the operation.

    Returns `True` if the text was played, `False` otherwise.
    """
    logger.info("Playing TTS: %s", text)
    try:
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.play_media(
                operation_context=_context_serializer({context}),
                play_source=_audio_from_text(
                    call=call,
                    style=style,
                    text=text,
                ),
            )
        return True
    except ResourceNotFoundError:
        logger.debug("Call hung up before playing")
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            logger.debug("Call hung up before playing")
        else:
            raise e
    return False


async def handle_media(
    client: CallAutomationClient,
    call: CallStateModel,
    sound_url: str,
    context: ContextEnum | None = None,
) -> None:
    """
    Play a media to a call participant.

    If `context` is provided, it will be used to track the operation.
    """
    with _detect_hangup():
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.play_media(
                operation_context=_context_serializer({context}),
                play_source=FileSource(url=sound_url),
            )


async def handle_play_text(  # noqa: PLR0913
    call: CallStateModel,
    client: CallAutomationClient,
    text: str,
    context: ContextEnum | None = None,
    store: bool = True,
    style: MessageStyleEnum = MessageStyleEnum.NONE,
) -> bool:
    """
    Play a text to a call participant.

    If `store` is `True`, the text will be stored in the call messages.

    Returns `True` if the text was played, `False` otherwise.
    """
    # Split text in chunks
    chunks = await _chunk_before_tts(
        call=call,
        store=store,
        style=style,
        text=text,
    )

    # Play each chunk
    for chunk in chunks:
        res = await _handle_play_text(
            call=call,
            client=client,
            context=context,
            style=style,
            text=chunk,
        )
        if not res:
            return False
    return True


async def handle_clear_queue(
    client: CallAutomationClient,
    call: CallStateModel,
) -> None:
    """
    Clear the media queue of a call.
    """
    try:
        assert call.voice_id, "Voice ID is required for recognizing media"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.cancel_all_media_operations()
    except ResourceNotFoundError:
        logger.debug("Call hung up before playing")
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            logger.debug("Call hung up before playing")
        else:
            raise e


async def _chunk_before_tts(
    call: CallStateModel,
    style: MessageStyleEnum,
    text: str,
    store: bool = True,
) -> list[str]:
    """
    Split a text in chunks and store them in the call messages.

    Chunks are separated by sentences and are limited to the TTS capacity.
    """
    # Sanitize text for TTS
    text = re.sub(_TTS_SANITIZER_R, " ", text)  # Remove unwanted characters
    text = re.sub(r"\s+", " ", text)  # Remove multiple spaces

    # Store text in call messages
    if store:
        async with _db.call_transac(call):
            call.messages.append(
                MessageModel(
                    content=text,
                    persona=MessagePersonaEnum.ASSISTANT,
                    style=style,
                )
            )

    # Split text in chunks, separated by sentence
    chunks = []
    chunk = ""
    for to_add, _ in tts_sentence_split(text, True):
        # If chunck overflows TTS capacity, start a new record
        if len(chunk) + len(to_add) >= _MAX_CHARACTERS_PER_TTS:
            # Remove trailing space as sentences are separated by spaces
            chunks.append(chunk.strip())
            # Reset chunk
            chunk = ""
        # Add space to separate sentences
        chunk += to_add + " "

    # If there is a remaining chunk, add it
    if chunk:
        # Remove trailing space as sentences are separated by spaces
        chunks.append(chunk.strip())

    return chunks


def _audio_from_text(
    call: CallStateModel,
    style: MessageStyleEnum,
    text: str,
) -> SsmlSource:
    """
    Generate an audio source that can be read by Azure Communication Services SDK.

    Text requires to be SVG escaped, and SSML tags are used to control the voice. Text is also truncated, as this is the limit of Azure Communication Services TTS, but a warning is logged.

    See: https://learn.microsoft.com/en-us/azure/ai-services/speech-service/speech-synthesis-markup-structure
    """
    if len(text) > _MAX_CHARACTERS_PER_TTS:
        logger.warning("Text is too long to be processed by TTS, truncating, fix this!")
        text = text[:_MAX_CHARACTERS_PER_TTS]
    # Escape text for SSML
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Build SSML tree
    ssml = f"""
    <speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{call.lang.short_code}">
        <voice name="{call.lang.voice}" effect="eq_telecomhp8k">
            <lexicon uri="{CONFIG.resources.public_url}/lexicon.xml" />
            <lang xml:lang="{call.lang.short_code}">
                <mstts:express-as style="{style.value}" styledegree="0.5">
                    <prosody rate="{call.initiate.prosody_rate}">{text}</prosody>
                </mstts:express-as>
            </lang>
        </voice>
    </speak>
    """
    return SsmlSource(
        custom_voice_endpoint_id=call.lang.custom_voice_endpoint_id,
        ssml_text=ssml.strip(),
    )


async def handle_recognize_ivr(
    call: CallStateModel,
    choices: list[RecognitionChoice],
    client: CallAutomationClient,
    text: str,
    context: ContextEnum | None = None,
) -> None:
    """
    Recognize an IVR response after playing a text.

    Starts by playing text, then starts recognizing the response. The recognition will be interrupted by the user if they start speaking. The recognition will be played in the call language.
    """
    logger.info("Recognizing IVR: %s", text)
    try:
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.start_recognizing_media(
                choices=choices,
                input_type=RecognizeInputType.CHOICES,
                interrupt_prompt=True,
                operation_context=_context_serializer({context}),
                play_prompt=_audio_from_text(
                    call=call,
                    style=MessageStyleEnum.NONE,
                    text=text,
                ),
                speech_language=call.lang.short_code,
                target_participant=PhoneNumberIdentifier(call.initiate.phone_number),  # pyright: ignore
            )
    except ResourceNotFoundError:
        logger.debug("Call hung up before recognizing")


async def handle_hangup(
    client: CallAutomationClient,
    call: CallStateModel,
) -> None:
    """
    Hang up a call.

    If the call is already hung up, the exception will be suppressed.
    """
    logger.info("Hanging up: %s", call.initiate.phone_number)
    with (
        # Suppress hangup exception
        suppress(CallHangupException),
        # Detect hangup exception
        _detect_hangup(),
    ):
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.hang_up(is_for_everyone=True)


async def handle_transfer(
    client: CallAutomationClient,
    call: CallStateModel,
    target: str,
    context: ContextEnum | None = None,
) -> None:
    """
    Transfer a call to another participant.

    Can raise a `CallHangupException` if the call is hung up.
    """
    logger.info("Transferring call: %s", target)
    with _detect_hangup():
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.transfer_call_to_participant(
                operation_context=_context_serializer({context}),
                target_participant=PhoneNumberIdentifier(target),
            )


async def start_audio_streaming(
    client: CallAutomationClient,
    call: CallStateModel,
) -> None:
    """
    Start audio streaming to the call.

    Can raise a `CallHangupException` if the call is hung up.
    """
    logger.info("Starting audio streaming")
    with _detect_hangup():
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            # TODO: Use the public API once the "await" have been fixed
            # await call_client.start_media_streaming()
            await call_client._call_media_client.start_media_streaming(
                call_connection_id=call_client._call_connection_id,
                start_media_streaming_request=StartMediaStreamingRequest(),
            )


async def stop_audio_streaming(
    client: CallAutomationClient,
    call: CallStateModel,
) -> None:
    """
    Stop audio streaming to the call.

    Can raise a `CallHangupException` if the call is hung up.
    """
    logger.info("Stopping audio streaming")
    with _detect_hangup():
        assert call.voice_id, "Voice ID is required to control the call"
        async with _use_call_client(client, call.voice_id) as call_client:
            await call_client.stop_media_streaming()


def _context_serializer(contexts: set[ContextEnum | None] | None) -> str | None:
    """
    Serialize a set of contexts to a JSON string.

    Returns `None` if no context is provided.
    """
    if not contexts:
        return None
    return json.dumps([context.value for context in contexts if context])


@contextmanager
def _detect_hangup() -> Generator[None, None, None]:
    """
    Catch a call hangup and raise a `CallHangupException` instead of the Call Automation SDK exceptions.
    """
    try:
        yield
    except ResourceNotFoundError:
        logger.debug("Call hung up")
        raise CallHangupException
    except HttpResponseError as e:
        if "call already terminated" in e.message.lower():
            logger.debug("Call hung up")
            raise CallHangupException
        else:
            raise e


@asynccontextmanager
async def _use_call_client(
    client: CallAutomationClient, voice_id: str
) -> AsyncGenerator[CallConnectionClient, None]:
    """
    Return the call client for a given call.
    """
    # Client already been created in the call client, never close it from here
    yield client.get_call_connection(call_connection_id=voice_id)
