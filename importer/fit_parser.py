"""
fit_parser.py — Extract activity metadata from a single FIT file.

Design choices:
- Reads only session messages for summary data (no full time-series scan)
- GPS detection uses bounding-box fields in the session message first;
  falls back to checking the first record message if absent
- Returns a plain dict — no dependencies on the rest of the importer
- Raises on unrecoverable parse errors (caller logs to import_errors)
"""
import hashlib
import os
from typing import Optional


# ── File-level helpers ────────────────────────────────────────────────────

def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    """Return SHA-256 hex digest of a file. Reads in 1 MiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def file_meta(path: str) -> dict:
    """Return file-level metadata without parsing FIT content."""
    stat = os.stat(path)
    return {
        "file_name":   os.path.basename(path),
        "source_path": path,
        "file_size_b": stat.st_size,
        "file_hash":   sha256_file(path),
    }


# ── FIT parsing ───────────────────────────────────────────────────────────

def _to_iso(value) -> Optional[str]:
    """Convert a fitparse datetime value to an ISO-8601 string."""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _safe_int(value, min_val: int = 0) -> Optional[int]:
    """Convert to int, return None if falsy or below min_val."""
    if value is None:
        return None
    try:
        v = int(value)
        return v if v > min_val else None
    except (TypeError, ValueError):
        return None


def _safe_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_fit(path: str) -> dict:
    """
    Parse a FIT file and return an activity metadata dict.

    Only reads session-level summary messages. Does not load full
    time-series data. Safe to call on any .fit file — monitoring,
    sleep, and course files will return a result with most fields None.

    Raises:
        fitparse.FitParseError — corrupt header or CRC failure
        Exception              — any other unrecoverable error
    """
    from fitparse import FitFile  # lazy import — not available at module load

    result = {
        "sport":          None,
        "sub_sport":      None,
        "start_time":     None,
        "duration_s":     None,
        "distance_m":     None,
        "total_ascent_m": None,
        "avg_heart_rate": None,
        "max_heart_rate": None,
        "avg_power":      None,
        "max_power":      None,
        "avg_cadence":    None,
        "has_hr":         False,
        "has_power":      False,
        "has_gps":        False,
    }

    fit = FitFile(path)

    # ── Session message ───────────────────────────────────────────────────
    # Wrap iteration so a CRC error in later records doesn't discard
    # session data that was already successfully decoded.
    try:
        for msg in fit.get_messages("session"):
            d = msg.get_values()

            result["start_time"]     = _to_iso(d.get("start_time"))
            result["sport"]          = str(d["sport"])     if d.get("sport")     else None
            result["sub_sport"]      = str(d["sub_sport"]) if d.get("sub_sport") else None
            result["duration_s"]     = _safe_float(d.get("total_elapsed_time"))
            result["distance_m"]     = _safe_float(d.get("total_distance"))
            result["total_ascent_m"] = _safe_float(d.get("total_ascent"))
            result["avg_cadence"]    = _safe_int(d.get("avg_cadence"), min_val=0)

            # Heart rate
            avg_hr = _safe_int(d.get("avg_heart_rate"), min_val=0)
            max_hr = _safe_int(d.get("max_heart_rate"), min_val=0)
            result["avg_heart_rate"] = avg_hr
            result["max_heart_rate"] = max_hr
            if avg_hr or max_hr:
                result["has_hr"] = True

            # Power — 0 W means no power meter; treat as absent
            avg_pwr = _safe_int(d.get("avg_power"), min_val=1)
            max_pwr = _safe_int(d.get("max_power"), min_val=1)
            result["avg_power"] = avg_pwr
            result["max_power"] = max_pwr
            if avg_pwr or max_pwr:
                result["has_power"] = True

            # GPS detection via bounding-box fields
            if d.get("nec_lat") is not None or d.get("swc_lat") is not None:
                result["has_gps"] = True

            break  # first session message only

    except Exception:
        # CRC mismatch or truncated file — keep whatever we decoded before
        # the error. If the session message came before the bad bytes we
        # still have valid metadata; otherwise fields remain None/False.
        pass

    # ── GPS fallback: check first record if session had no bounding box ───
    if not result["has_gps"]:
        try:
            for msg in fit.get_messages("record"):
                d = msg.get_values()
                if d.get("position_lat") is not None:
                    result["has_gps"] = True
                break
        except Exception:
            pass

    return result
