# Alexa live view (WebRTC) test package for Home Assistant

This is a **test build** of the change proposed in
[nolimit-labs/core `alexa-rtc-session-controller`](https://github.com/nolimit-labs/core/tree/alexa-rtc-session-controller),
which adds `Alexa.RTCSessionController` to Home Assistant so Echo Show devices
can show the live view of cameras exposed through Home Assistant Cloud.
Background: [home-assistant/discussions#4885](https://github.com/orgs/home-assistant/discussions/4885).

It ships as a small custom integration that patches the built-in Alexa
integration at startup. Nothing in the Home Assistant container is replaced,
and removing the folder restores stock behaviour.

## Requirements

- Home Assistant 2025.x or newer (tested against the `dev` branch of September 2026).
- Home Assistant Cloud (Nabu Casa) with Alexa enabled, and the camera exposed to Alexa.
- A camera that supports WebRTC in Home Assistant. Home Assistant OS and the
  Container image include go2rtc, which provides this for RTSP/RTSPS cameras
  such as UniFi Protect. Check: the camera's more-info dialog in Home Assistant
  should stream with low latency and its diagnostics should list `web_rtc`.
- An Echo Show on the same network as Home Assistant for the first test.

## Install

1. Download `alexa_rtc_patch.zip` from the
   [releases page](https://github.com/nolimit-labs/core/releases) or copy the
   `custom_components/alexa_rtc_patch` folder from this branch.
2. Put it in your Home Assistant config directory so you end up with
   `config/custom_components/alexa_rtc_patch/__init__.py` and `manifest.json`.
   The File Editor add-on, Samba add-on or Studio Code Server all work for this.
3. Add these lines to `configuration.yaml`:

   ```yaml
   alexa_rtc_patch:

   logger:
     logs:
       custom_components.alexa_rtc_patch: debug
       homeassistant.components.alexa: debug
       homeassistant.components.go2rtc: debug
   ```

4. Restart Home Assistant. The log should contain
   `Alexa RTC live view test patch active`. A warning that a custom integration
   is loaded is normal.
5. Run Alexa device discovery again: say "Alexa, discover devices" or use
   Devices > Add device in the Alexa app. This step is required, because the new
   capability is only sent to Amazon during discovery.

## Test

1. In the Alexa app, open Devices > Cameras and pick the camera. It should now
   offer a live view.
2. On the Echo Show say "Alexa, show the <camera name>".
3. Note what happens: video plays, a spinner, or an error message.

## What to send back

- Home Assistant version and install type (OS, Container, Core).
- Camera integration and model (for example UniFi Protect G4 Doorbell).
- Echo Show generation.
- The result of the test above.
- Log lines from `custom_components.alexa_rtc_patch` and
  `homeassistant.components.alexa` around the time of the test. They include
  the SDP offer from Amazon and the answer sent back, which is what is needed
  to debug any failure. They do not contain credentials.

## Remove

Delete `config/custom_components/alexa_rtc_patch`, remove the
`alexa_rtc_patch:` line from `configuration.yaml`, and restart.
