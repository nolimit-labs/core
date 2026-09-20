"""Test build of Alexa.RTCSessionController support for Home Assistant.

This custom integration injects the code from
https://github.com/nolimit-labs/core/tree/alexa-rtc-session-controller
into the built-in Alexa integration at startup, so it can be tested on a
normal Home Assistant install without replacing core files.

It is a temporary test package. Remove it once the change ships in core.
"""

import asyncio
from collections.abc import Generator
import ipaddress
import logging
from typing import Any

from webrtc_models import RTCIceCandidateInit

from homeassistant.components import camera
from homeassistant.components.alexa import capabilities, entities, handlers
from homeassistant.components.alexa.config import AbstractConfig
from homeassistant.components.alexa.errors import AlexaError, AlexaInvalidDirectiveError
from homeassistant.components.alexa.state_report import AlexaDirective, AlexaResponse
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

DOMAIN = "alexa_rtc_patch"
_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)

RTC_ANSWER_TIMEOUT = 4
RTC_CANDIDATE_GATHERING_TIMEOUT = 1.5


class AlexaEndpointUnreachableError(AlexaError):
    """Class to represent EndpointUnreachable errors."""

    namespace = "Alexa"
    error_type = "ENDPOINT_UNREACHABLE"


class AlexaRTCSessionController(capabilities.AlexaCapability):
    """Implements Alexa.RTCSessionController."""

    supported_locales = capabilities.AlexaCameraStreamController.supported_locales

    def name(self) -> str:
        """Return the Alexa API name of this interface."""
        return "Alexa.RTCSessionController"

    def configuration(self) -> dict[str, Any] | None:
        """Return configuration object."""
        return {"isFullDuplexAudioSupported": False}


def _supports_webrtc(hass: HomeAssistant, entity_id: str) -> bool:
    """Check if the camera can negotiate a WebRTC stream."""
    try:
        camera_entity = camera.get_camera_from_entity_id(hass, entity_id)
    except HomeAssistantError as err:
        _LOGGER.debug("%s not available for RTCSessionController: %s", entity_id, err)
        return False
    return (
        camera.StreamType.WEB_RTC
        in camera_entity.camera_capabilities.frontend_stream_types
    )


def _is_ipv4_candidate(candidate: str) -> bool:
    """Return if the ICE candidate has an IPv4 address."""
    parts = candidate.split()
    try:
        return ipaddress.ip_address(parts[4]).version == 4
    except IndexError, ValueError:
        return False


def _embed_candidates(sdp: str, candidates: list[RTCIceCandidateInit]) -> str:
    """Add gathered ICE candidates to the media sections of an SDP answer."""
    lines = sdp.replace("\r\n", "\n").rstrip("\n").split("\n")
    media_starts = [idx for idx, line in enumerate(lines) if line.startswith("m=")]
    if not media_starts:
        return sdp

    mids: dict[str, int] = {}
    for section, start in enumerate(media_starts):
        end = media_starts[section + 1] if section + 1 < len(media_starts) else None
        for line in lines[start:end]:
            if line.startswith("a=mid:"):
                mids[line.removeprefix("a=mid:")] = section

    extra_lines: dict[int, list[str]] = {}
    for candidate in candidates:
        if not _is_ipv4_candidate(candidate.candidate):
            _LOGGER.debug("Skipping non-IPv4 candidate %s", candidate.candidate)
            continue
        section = 0
        if candidate.sdp_m_line_index is not None:
            section = candidate.sdp_m_line_index
        elif candidate.sdp_mid is not None:
            section = mids.get(candidate.sdp_mid, 0)
        if section >= len(media_starts):
            section = 0
        extra_lines.setdefault(section, []).append(f"a={candidate.candidate}")

    result: list[str] = lines[: media_starts[0]]
    for section, start in enumerate(media_starts):
        end = media_starts[section + 1] if section + 1 < len(media_starts) else None
        section_lines = [
            line for line in lines[start:end] if line != "a=end-of-candidates"
        ]
        result.extend(section_lines)
        result.extend(extra_lines.get(section, []))
        result.append("a=end-of-candidates")

    return "\r\n".join(result) + "\r\n"


async def _async_get_webrtc_answer(
    hass: HomeAssistant, camera_entity: camera.Camera, offer: str, session_id: str
) -> str:
    """Negotiate a WebRTC session and return a complete SDP answer."""
    answer_future: asyncio.Future[str] = hass.loop.create_future()
    gathering_done = asyncio.Event()
    candidates: list[RTCIceCandidateInit] = []

    @callback
    def send_message(message: camera.WebRTCMessage) -> None:
        """Collect the answer and candidates from the camera."""
        _LOGGER.debug("WebRTC message for session %s: %s", session_id, message)
        match message:
            case camera.WebRTCAnswer():
                if not answer_future.done():
                    answer_future.set_result(message.answer)
            case camera.WebRTCCandidate(candidate=RTCIceCandidateInit() as candidate):
                if candidate.candidate:
                    candidates.append(candidate)
                else:
                    gathering_done.set()
            case camera.WebRTCCandidate(candidate=candidate):
                candidates.append(RTCIceCandidateInit(candidate.candidate))
            case camera.WebRTCError():
                if not answer_future.done():
                    answer_future.set_exception(HomeAssistantError(message.message))

    try:
        await camera_entity.async_handle_async_webrtc_offer(
            offer, session_id, send_message
        )
        async with asyncio.timeout(RTC_ANSWER_TIMEOUT):
            answer = await answer_future
    except (HomeAssistantError, TimeoutError) as err:
        camera_entity.close_webrtc_session(session_id)
        raise AlexaEndpointUnreachableError(
            f"Failed to negotiate WebRTC session: {err}"
        ) from err

    try:
        async with asyncio.timeout(RTC_CANDIDATE_GATHERING_TIMEOUT):
            await gathering_done.wait()
    except TimeoutError:
        pass

    return _embed_candidates(answer, candidates)


def _get_webrtc_camera(hass: HomeAssistant, entity_id: str) -> camera.Camera:
    """Return the camera entity if it supports WebRTC."""
    try:
        camera_entity = camera.get_camera_from_entity_id(hass, entity_id)
    except HomeAssistantError as err:
        raise AlexaEndpointUnreachableError(str(err)) from err

    if (
        camera.StreamType.WEB_RTC
        not in camera_entity.camera_capabilities.frontend_stream_types
    ):
        raise AlexaInvalidDirectiveError(handlers.DIRECTIVE_NOT_SUPPORTED)

    return camera_entity


async def async_api_initiate_session_with_offer(
    hass: HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: Context,
) -> AlexaResponse:
    """Process an InitiateSessionWithOffer request."""
    camera_entity = _get_webrtc_camera(hass, directive.entity.entity_id)
    session_id: str = directive.payload["sessionId"]
    offer: str = directive.payload["offer"]["value"]
    _LOGGER.debug(
        "Alexa offer for %s (%s):\n%s", camera_entity.entity_id, session_id, offer
    )

    answer = await _async_get_webrtc_answer(hass, camera_entity, offer, session_id)
    _LOGGER.debug(
        "Answer for %s (%s):\n%s", camera_entity.entity_id, session_id, answer
    )

    return directive.response(
        name="AnswerGeneratedForSession",
        namespace="Alexa.RTCSessionController",
        payload={"answer": {"format": "SDP", "value": answer}},
    )


async def async_api_rtc_session_connected(
    hass: HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: Context,
) -> AlexaResponse:
    """Process a SessionConnected request."""
    _LOGGER.debug("Alexa session connected: %s", directive.payload["sessionId"])
    return directive.response(
        name="SessionConnected",
        namespace="Alexa.RTCSessionController",
        payload={"sessionId": directive.payload["sessionId"]},
    )


async def async_api_rtc_session_disconnected(
    hass: HomeAssistant,
    config: AbstractConfig,
    directive: AlexaDirective,
    context: Context,
) -> AlexaResponse:
    """Process a SessionDisconnected request."""
    session_id: str = directive.payload["sessionId"]
    _LOGGER.debug("Alexa session disconnected: %s", session_id)
    try:
        camera_entity = camera.get_camera_from_entity_id(
            hass, directive.entity.entity_id
        )
    except HomeAssistantError as err:
        _LOGGER.debug("Cannot close WebRTC session %s: %s", session_id, err)
    else:
        camera_entity.close_webrtc_session(session_id)

    return directive.response(
        name="SessionDisconnected",
        namespace="Alexa.RTCSessionController",
        payload={"sessionId": session_id},
    )


_original_interfaces = entities.CameraCapabilities.interfaces


def _patched_interfaces(
    self: entities.CameraCapabilities,
) -> Generator[capabilities.AlexaCapability]:
    """Yield the RTC session controller in front of the original interfaces."""
    supported = self.entity.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
    if supported & camera.CameraEntityFeature.STREAM and _supports_webrtc(
        self.hass, self.entity_id
    ):
        yield AlexaRTCSessionController(self.entity)
    yield from _original_interfaces(self)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Inject the RTC session controller into the core Alexa integration."""
    if "Alexa.RTCSessionController" in {key[0] for key in handlers.HANDLERS}:
        _LOGGER.warning(
            "This Home Assistant version already supports Alexa.RTCSessionController;"
            " remove the alexa_rtc_patch custom integration"
        )
        return True

    handlers.HANDLERS[("Alexa.RTCSessionController", "InitiateSessionWithOffer")] = (
        async_api_initiate_session_with_offer
    )
    handlers.HANDLERS[("Alexa.RTCSessionController", "SessionConnected")] = (
        async_api_rtc_session_connected
    )
    handlers.HANDLERS[("Alexa.RTCSessionController", "SessionDisconnected")] = (
        async_api_rtc_session_disconnected
    )
    entities.CameraCapabilities.interfaces = _patched_interfaces  # type: ignore[method-assign]
    _LOGGER.warning(
        "Alexa RTC live view test patch active. Run Alexa device discovery again"
        " so cameras advertise Alexa.RTCSessionController"
    )
    return True
