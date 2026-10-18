"""Voice speaker identification (sherpa-onnx CAM++ embeddings, context only)."""

from app.voice.service import (  # noqa: F401
    PendingSpeakerLookup,
    SpeakerIdService,
    attach_speaker_to_task,
    consume_enrollment_turn,
    discard_pending_speaker,
    enrollment_active,
    get_speaker_service,
    has_pending_speaker,
    is_enrollment_turn,
    load_voice_config,
    set_speaker_service,
    start_speaker_lookup,
)
