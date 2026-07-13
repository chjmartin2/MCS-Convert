"""Percussion-click pitches shared by the importers.

MCS has no noise generator, so a drum can only be a NOTE. The trick the NSF
work proved: use the register extremes as timbre. A single 32nd note at the
LOWEST playable pitch (B2) reads as a kick "boom"; at the HIGHEST (E7) it reads
as a hi-hat "tss". A two-note "cluster" (kept only for back-compat) beats two
squares a semitone apart for a rougher, noisier tick — but it costs two of the
24 horizontal positions and two of the 32 events per measure, so the single
register clicks are preferred.

"auto" splits a noise/drum line two ways by BRIGHTNESS: bright hits -> hi-hat,
dark hits -> low bass. Each importer decides brightness from its own source
(NES noise period, AY sample character), then calls two_tone()."""

CLICKS = {
    "cluster": (55, 56),    # legacy: G3+Ab3, a beating minor second (roughness)
    "block": (62,),         # a single mid-register D4 wood-block tick
    "low bass": (47,),      # B2, the lowest note the player sounds -> kick thud
    "hi-hat": (100,),       # E7, the highest note -> a bright tick/ting
}

# Drum sounds offered in the import UI (cluster retired from the picker).
PICKER_SOUNDS = ("auto (two-tone)", "wood block", "low bass", "hi-hat")


def two_tone(bright: bool):
    """A bright hit -> hi-hat, a dark hit -> low bass (the auto kick/hat split)."""
    return CLICKS["hi-hat"] if bright else CLICKS["low bass"]
