"""Voice speaker identification (sherpa-onnx CAM++ embeddings, context only)."""

from app.voice.service import (  # noqa: F401
    SpeakerIdService,
    attach_speaker_to_task,
    discard_pending_speaker,
    get_speaker_service,
    has_pending_speaker,
    load_voice_config,
    set_speaker_service,
)
