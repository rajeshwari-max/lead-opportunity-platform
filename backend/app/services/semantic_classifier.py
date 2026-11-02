"""Meaning-based scores for brands, Social Business and the Devsol verticals.

Why this exists
---------------
The hierarchy model (ml_hierarchy.py) is TF-IDF + logistic regression. It
weighs the WORDS in a lead, and it learned mostly from labels the keyword rules
produced (5,262 of its 5,360 training rows). So a call written in words the
keyword lists never used — "raising incomes of smallholder tea growers through
producer companies" — is weak evidence for Livelihood or Vrutti even though
its meaning is plainly both. Its own held-out test shows the cost: brand tags
are right about half the time, vertical tags less than that.

This module adds a second, independent reading. A small sentence-embedding
model (BAAI/bge-small-en-v1.5, ~130 MB, ONNX on CPU via fastembed, no GPU,
no API key, nothing leaves the server) turns text into a vector that encodes
MEANING, so two texts about the same thing land close together even when they
share no words. Each label gets a "meaning profile": the brand teams' own
keyword inventories (services/brand_keywords.py) plus a short description and
phrases from ml_models/semantic_profiles.json (for brands, taken from the
brand teams' Brand Classification Input Forms). A lead's score for a label is
its closest cosine similarity to that label's profile. The same file's
"exclude" entries ("hospital infrastructure tenders" for a public-health
brand) describe look-alikes: a lead closer to one of those than to the
profile itself does not get the label.

How it is used — see ml_hierarchy.py
------------------------------------
  LOP_SEMANTIC_MODE=off     not computed at all
  LOP_SEMANTIC_MODE=shadow  (default) computed and recorded in the row's
                            classification evidence beside the ML result; the
                            dashboard labels are exactly what the ML model says
  LOP_SEMANTIC_MODE=blend   the ML labels stay, and a label the meaning score
                            clears LOP_SEMANTIC_MIN_SIMILARITY for is added

Switch to blend only after scripts/semantic_eval.py shows it helps on the
human-reviewed leads. The ML model is never retrained or replaced by this.

Failure is never fatal: if fastembed is not installed or the model cannot be
downloaded, every call returns None, a single warning is logged, and
classification carries on exactly as before.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.config import BASE_DIR, settings

log = logging.getLogger("scraper")

GROUPS = ("brands", "archetypes", "verticals")
BRAND_LABELS = ("Swasti", "Vrutti", "Upfront", "Green Foundation",
                "Community Action Collab", "Setu")
ARCHETYPE_LABELS = ("Social Business",)
VERTICAL_LABELS = ("Livelihood", "Health", "E4C(Evidence for Change)",
                   "Climate/Sustainability(ESG)", "Worker Wellbeing",
                   "Innovative Finance")
LABELS = {"brands": BRAND_LABELS, "archetypes": ARCHETYPE_LABELS,
          "verticals": VERTICAL_LABELS}

DEFAULT_PROFILES = Path(__file__).resolve().parents[1] / "ml_models" / "semantic_profiles.json"
_CHUNK = 8          # keyword phrases per profile anchor
_MAX_PER_GROUP = 3  # never suggest more than this many labels in one group


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]): ...      # -> np.ndarray (n, d)


@dataclass
class SemanticResult:
    scores: dict[str, dict[str, float]]          # group -> label -> similarity
    selected: dict[str, list[str]]               # group -> labels over threshold
    nearest: dict[str, str] = field(default_factory=dict)   # label -> closest anchor
    version: str = ""
    # label -> the "words to exclude" entry the lead resembled MORE than the
    # label's own profile; such a label is pushed below the threshold.
    excluded: dict[str, str] = field(default_factory=dict)

    def as_evidence(self) -> dict:
        return {
            "version": self.version,
            "threshold": settings.semantic_min_similarity,
            "suggested": self.selected,
            "scores": {g: {k: round(v, 4) for k, v in s.items()} for g, s in self.scores.items()},
            "nearest_profile": self.nearest,
            **({"excluded_by": self.excluded} if self.excluded else {}),
        }


# ------------------------------------------------------------------ profiles

def _profiles_path() -> Path:
    configured = str(getattr(settings, "semantic_profiles_path", "") or "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_PROFILES


def _chunks(prefix: str, terms) -> list[str]:
    terms = [t.strip() for t in terms if str(t).strip()]
    return [f"{prefix}: {', '.join(terms[i:i + _CHUNK])}"
            for i in range(0, len(terms), _CHUNK)]


def build_profiles(path: Path | None = None) -> dict[tuple[str, str], list[str]]:
    """(group, label) -> anchor texts that describe what the label MEANS."""
    from app.services.brand_keywords import BRAND_KEYWORD_SECTIONS, BRAND_KEYWORDS

    anchors: dict[tuple[str, str], list[str]] = {
        (g, label): [] for g, labels in LABELS.items() for label in labels}

    # The brand teams' own inventories, section by section where they gave
    # sections — "Setu — Last-Mile Access: …" carries more meaning than a
    # flat list of 134 terms.
    for brand in BRAND_LABELS:
        if brand in BRAND_KEYWORD_SECTIONS:
            for section, terms in BRAND_KEYWORD_SECTIONS[brand].items():
                anchors[("brands", brand)] += _chunks(f"{brand} - {section}", terms)
        elif brand in BRAND_KEYWORDS:
            anchors[("brands", brand)] += _chunks(brand, BRAND_KEYWORDS[brand])

    # Editable descriptions and phrases: the single biggest quality lever.
    source = path or _profiles_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("semantic profiles unreadable (%s): %s", source, exc)
        data = {}
    for group, labels in LABELS.items():
        for label in labels:
            entry = (data.get(group) or {}).get(label) or {}
            description = str(entry.get("description") or "").strip()
            if description:
                anchors[(group, label)].insert(0, f"{label}: {description}")
            anchors[(group, label)] += _chunks(label, entry.get("phrases") or [])
    return {key: value for key, value in anchors.items() if value}


def build_exclusions(path: Path | None = None) -> dict[tuple[str, str], list[str]]:
    """(group, label) -> "words to exclude" from the brand/vertical forms.

    Each entry describes a kind of opportunity that LOOKS like the label but is
    not one ("hospital infrastructure tenders" for a public-health brand). A
    lead closer in meaning to an exclusion than to the label's own profile
    does not get the label."""
    source = path or _profiles_path()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[tuple[str, str], list[str]] = {}
    for group, labels in LABELS.items():
        for label in labels:
            entry = (data.get(group) or {}).get(label) or {}
            phrases = [str(p).strip() for p in entry.get("exclude") or [] if str(p).strip()]
            if phrases:
                out[(group, label)] = phrases
    return out


# ------------------------------------------------------------------- engine

class _FastEmbed:
    def __init__(self, model_name: str, cache_dir: Path, threads: int) -> None:
        from fastembed import TextEmbedding

        cache_dir.mkdir(parents=True, exist_ok=True)
        self.name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=str(cache_dir),
                                    threads=max(1, int(threads)))

    def embed(self, texts: list[str]):
        import numpy as np

        return np.asarray(list(self._model.embed(texts)), dtype="float32")


_lock = threading.Lock()
_engine: Embedder | None = None
_anchor_cache: tuple | None = None       # (keys, anchor texts, matrix, version)
_failed = ""


def _cache_dir() -> Path:
    configured = str(getattr(settings, "semantic_cache_dir", "") or "").strip()
    return Path(configured).expanduser() if configured else BASE_DIR / "data" / "models"


def set_embedder(embedder: Embedder | None) -> None:
    """Install an embedder (tests) or None to reset to the configured model."""
    global _engine, _anchor_cache, _failed
    with _lock:
        _engine = embedder
        _anchor_cache = None
        _failed = ""


def enabled() -> bool:
    return str(getattr(settings, "semantic_mode", "off") or "off").casefold() in {"shadow", "blend"}


def blending() -> bool:
    return str(getattr(settings, "semantic_mode", "off") or "off").casefold() == "blend"


def _load() -> tuple | None:
    global _engine, _anchor_cache, _failed
    if _anchor_cache is not None:
        return _anchor_cache
    if _failed:
        return None
    with _lock:
        if _anchor_cache is not None:
            return _anchor_cache
        try:
            if _engine is None:
                _engine = _FastEmbed(settings.semantic_model, _cache_dir(),
                                     settings.semantic_threads)
            keys, texts = [], []
            for polarity, built in ((1, build_profiles()), (-1, build_exclusions())):
                for (group, label), anchors in built.items():
                    for anchor in anchors:
                        keys.append((group, label, polarity))
                        texts.append(anchor)
            matrix = _normalise(_engine.embed(texts))
            digest = hashlib.sha256(json.dumps(
                [_engine.name, texts, settings.semantic_min_similarity],
                ensure_ascii=False).encode("utf-8")).hexdigest()[:8]
            short = _engine.name.rsplit("/", 1)[-1]
            _anchor_cache = (keys, texts, matrix, f"semantic-{short}-{digest}")
            log.info("semantic classifier ready: %s, %s profile anchors", short, len(texts))
            return _anchor_cache
        except Exception as exc:  # noqa: BLE001 - optional layer must never break scraping
            _failed = f"{type(exc).__name__}: {exc}"
            log.warning("semantic classifier unavailable, ML-only classification "
                        "continues: %s", _failed)
            return None


def _normalise(matrix):
    import numpy as np

    matrix = np.asarray(matrix, dtype="float32")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def status() -> dict:
    loaded = _anchor_cache is not None
    return {
        "mode": str(getattr(settings, "semantic_mode", "off")),
        "loaded": loaded,
        "model": settings.semantic_model,
        "version": _anchor_cache[3] if loaded else "",
        "anchors": len(_anchor_cache[1]) if loaded else 0,
        "threshold": settings.semantic_min_similarity,
        "error": _failed,
    }


def version() -> str:
    cache = _load()
    return cache[3] if cache else ""


# ------------------------------------------------------------------ scoring

def lead_text(title: str, summary: str = "", eligibility: str = "") -> str:
    """Title, then the description — what the call is FOR, not its metadata.
    Organisation and country are deliberately left out: "Gates Foundation,
    India" says nothing about which vertical the call serves."""
    parts = [str(title or "").strip(), str(summary or "").strip()[:1500],
             str(eligibility or "").strip()[:300]]
    return ". ".join(re.sub(r"\s+", " ", p) for p in parts if p)


def score_texts(texts: list[str],
                threshold: float | None = None) -> list[SemanticResult] | None:
    """Meaning scores for each text, or None when the layer is unavailable."""
    if not texts:
        return []
    cache = _load()
    if cache is None:
        return None
    keys, anchor_texts, anchors, ver = cache
    cut = settings.semantic_min_similarity if threshold is None else threshold
    try:
        vectors = _normalise(_engine.embed([t or " " for t in texts]))
    except Exception:  # noqa: BLE001
        log.warning("semantic scoring failed; ML-only for this batch", exc_info=True)
        return None
    similarity = vectors @ anchors.T                       # (n_texts, n_anchors)

    results = []
    for row in similarity:
        scores: dict[str, dict[str, float]] = {g: {} for g in GROUPS}
        nearest: dict[str, str] = {}
        against: dict[str, tuple[float, str]] = {}
        for index, (group, label, polarity) in enumerate(keys):
            value = float(row[index])
            if polarity < 0:
                if value > against.get(label, (-1.0, ""))[0]:
                    against[label] = (value, anchor_texts[index][:160])
            elif value > scores[group].get(label, -1.0):
                scores[group][label] = value
                nearest[label] = anchor_texts[index][:160]
        excluded: dict[str, str] = {}
        for group in GROUPS:
            for label, value in list(scores[group].items()):
                negative, text = against.get(label, (-1.0, ""))
                if negative > value:
                    # Penalise by the margin, so the evidence still shows how
                    # close the call was rather than a flat zero.
                    scores[group][label] = value - (negative - value)
                    excluded[label] = text
        selected = {}
        for group in GROUPS:
            ranked = sorted(scores[group].items(), key=lambda kv: kv[1], reverse=True)
            selected[group] = [label for label, value in ranked if value >= cut][:_MAX_PER_GROUP]
        results.append(SemanticResult(scores=scores, selected=selected,
                                      nearest=nearest, version=ver, excluded=excluded))
    return results
