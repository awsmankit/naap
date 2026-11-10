"""Opening sizes: user value (resolved flag) > schedule > guessed schedule entry."""
from __future__ import annotations

import re

from .analysis import preferred_size, schedule_sizes


def _initials_guess(a, tag: str):
    """A plan tag missing from the schedule (e.g. 'SD') matched to an unused schedule entry
    whose description has those initials in order (e.g. 'D5 (1800X2100) ... SLIDING DOOR')."""
    sizes = schedule_sizes(a)
    used = {t for t, *_ in a.plan_tags}
    letters = re.sub(r"[^A-Z]", "", tag.upper())
    if not letters:
        return None
    best = None
    for s in a.schedule:
        if s.tag in used or s.ent is None:
            continue
        text = " ".join(a.geometry["entities"][s.ent]["s"].split()).upper()
        h = s.text_h or 1
        near = [t for _, t, r in a.texts if r == s.region and t is not a.geometry["entities"][s.ent]
                and -h <= t["p"][0] - s.x <= 40 * h and -3 * h <= t["p"][1] - s.y <= 1.5 * h]
        near.sort(key=lambda t: (-t["p"][1], t["p"][0]))
        text = " ".join([text] + [" ".join(t["s"].split()).upper() for t in near])[:160]
        words = re.findall(r"[A-Z]{2,}", text.split(")", 1)[-1])
        initials = "".join(w[0] for w in words)
        k = 0
        for ch in initials:
            if k < len(letters) and ch == letters[k]:
                k += 1
        if k == len(letters) and s.tag in sizes:
            w, h = preferred_size(sizes[s.tag])
            cand = (s.tag, w, h, text[:80])
            if best is None:
                best = cand
            elif best[0] != s.tag:
                return None  # ambiguous
    return best


def opening_size(a, tag: str):
    """-> (w_mm, h_mm, note); note starts with 'ASSUMED' or 'per user' when not straight from the schedule."""
    ov = getattr(a, "size_overrides", {}).get(tag)
    if ov:
        return ov[0], ov[1], ov[2]
    sizes = schedule_sizes(a)
    if tag in sizes:
        w, h = preferred_size(sizes[tag])
        if len(sizes[tag]) > 1:
            alts = ", ".join(f"{int(x)}X{int(y)}" for (x, y) in sizes[tag] if (x, y) != (w, h))
            return w, h, f"schedule table {int(w)}X{int(h)} used (also given: {alts})"
        return w, h, f"schedule {int(w)}X{int(h)}"
    cache = a.__dict__.setdefault("_guess", {})
    if tag not in cache:
        cache[tag] = _initials_guess(a, tag)
    g = cache[tag]
    if g:
        return g[1], g[2], f"ASSUMED: '{tag}' is not in the schedule; taken as {g[0]} {int(g[1])}X{int(g[2])} ('{g[3]}') by its initials"
    return None, None, "size not in schedule"
