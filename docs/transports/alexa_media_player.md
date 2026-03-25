---
tags:
  - transport
  - alexa
  - voice_assistant
---
# Alexa Media Player Transport Adaptor

| Transport ID         | Source      | Requirements | Optional |
| -------------------- | ----------- | ------------ | -------- |
| `alexa_media_player` | :material-github:[`alexa_media_player.py`](https://github.com/rhizomatics/supernotify/blob/main/custom_components/supernotify/transports/alexa_media_player.py) | :simple-homeassistantcommunitystore: [Alexa Media Player Integration](https://github.com/alandtse/alexa_media_player) | - |


Announce a message on an Alexa Echo device using the [`alexa_media_player`](https://github.com/alandtse/alexa_media_player) integration available via [HACS](https://www.hacs.xyz).

The `message_usage` option can be set to `combine_title` or `use_title` to override the default behaviour of speaking the `standard`.

## Voice specific message

Use `spoken_message` in the `data` section of a notification call to provide a different message for a voice
notification than used for other transports like email or mobile push.

## References

### Home Assistant Core
- [Alexa Media Player Integration](https://github.com/alandtse/alexa_media_player)
### Other
- [alexapy](https://alexapy.readthedocs.io/en/latest/index.html)
