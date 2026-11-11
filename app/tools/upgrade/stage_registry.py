#!/usr/bin/env python3
"""Stage implementation registry for the v2 runner.

W03 registered INPUT, MEDIA_PREP, OCR and ASR; W04 adds SOURCE, TRANSLATION, SUBTITLE_BUILD and the W04 part of
QUALITY_AND_COMMIT (structural gate + machine revision commit; the soft quality rules arrive with W06). A build
without an implementation for a required stage stops with STAGE_UNAVAILABLE instead of pretending to run.
"""

from __future__ import annotations

from upgrade.runner import StageImpl


def default_registry() -> dict[str, StageImpl]:
    """Return the stage implementations available in this build."""
    from upgrade.stages import AsrStage, InputStage, MediaPrepStage, OcrStage, QualityAndCommitStage, SourceStage, SubtitleBuildStage, TranslationStage
    return {stage.name: stage for stage in (InputStage(), MediaPrepStage(), OcrStage(), AsrStage(), SourceStage(), TranslationStage(), SubtitleBuildStage(),
                                             QualityAndCommitStage())}


__all__ = ["default_registry"]
