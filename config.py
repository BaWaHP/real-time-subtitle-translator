from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import json
import os
from pathlib import Path


def app_data_dir() -> Path:
    """Return the local directory for settings and credentials."""
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local"))) / "RealtimeAISubtitle"


@dataclass
class OverlayConfig:
    font_size: int = 28
    color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    background_opacity: float = 0.48
    show_english: bool = True
    click_through: bool = False
    screen_index: int = 0
    display_seconds: float = 5.0


@dataclass
class AppConfig:
    source_mode: str = "system"
    video_session_id: str = ""
    video_prefetch_seconds: float = 45.0
    video_audio_fallback: bool = True
    video_transcript_cache_limit: int = 2000
    device_name: str | None = None
    asr_model: str = "auto"
    asr_device: str = "auto"
    translation_mode: str = "mock"
    translation_model: str = ""
    max_segment_age: float = 12.0
    audio_queue_size: int = 128
    segment_queue_size: int = 3
    transcript_queue_size: int = 4
    overlay: OverlayConfig = field(default_factory=OverlayConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        path = path or app_data_dir() / "settings.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(cls)}
            values = {k: v for k, v in raw.items() if k in known}
            overlay = values.pop("overlay", {})
            ok = {f.name for f in fields(OverlayConfig)}
            cfg = cls(**values, overlay=OverlayConfig(**{k: v for k, v in overlay.items() if k in ok}))
            cfg.overlay.font_size = max(16, min(72, int(cfg.overlay.font_size)))
            cfg.overlay.background_opacity = max(0, min(1, float(cfg.overlay.background_opacity)))
            cfg.overlay.display_seconds = max(2.0, min(15, float(cfg.overlay.display_seconds)))
            cfg.max_segment_age = max(5, min(60, float(cfg.max_segment_age)))
            cfg.source_mode = cfg.source_mode if cfg.source_mode in {"system", "edge"} else "system"
            cfg.video_prefetch_seconds = max(10, min(120, float(cfg.video_prefetch_seconds)))
            cfg.video_transcript_cache_limit = max(32, min(10_000, int(cfg.video_transcript_cache_limit)))
            for name in ("audio_queue_size", "segment_queue_size", "transcript_queue_size"):
                setattr(cfg, name, max(1, min(256, int(getattr(cfg, name)))))
            return cfg
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or app_data_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
