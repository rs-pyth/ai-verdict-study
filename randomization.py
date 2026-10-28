"""
RANDOMIZATION — the causal backbone of this study.

Read this file first. If this is correct, the study's core causal claim
(condition assignment causes the outcome) is valid. If it's wrong, nothing
downstream matters.

DESIGN (pre-registered scheme "perm_block4_v1"):

  * Order of events, enforced in app.py's /api/submit-stance:
        1. the participant's stance is validated and RECORDED (write-once);
        2. only then is the condition drawn, STRATIFIED ON THAT STANCE;
        3. only then is a verdict served (a separate request).
    `participants.stance_recorded_at <= condition_assigned_at` is stored for
    every row so the order can be checked from the data.

  * Stratified permuted blocks. Within each stance ('yes' / 'no') the
    conditions are dealt from blocks of 4 holding exactly 2 'agree' and
    2 'contradict' in a random order. So after every 4th participant of a
    stance the two arms are exactly balanced within that stance, and at any
    point they differ by at most 2.

  * The draw never depends on free text. The only participant-derived input
    to the draw is `stance`, and draw_condition() refuses anything that is not
    exactly 'yes' or 'no'. Rationale, confidence, covariates, timing: none of
    them can reach this module. The block contents come from the operating
    system's entropy (random.SystemRandom), not from a seedable generator.

  * Once per participant. The block table (db.py: allocation_slots) hands out
    each slot to exactly one participant id, claimed by a single atomic SQL
    statement; a repeated call for the same participant returns the slot it
    already holds. A database trigger also refuses to change `condition`
    once set.

This module is pure (no database, no Flask) so it can be tested directly.
"""

import random

STANCES = ("yes", "no")
CONDITIONS = ("agree", "contradict")
BLOCK_SIZE = 4
SCHEME = "perm_block4_v1"

_rng = random.SystemRandom()


def new_block(rng=None):
    """One permuted block: BLOCK_SIZE conditions, half of each, random order."""
    block = list(CONDITIONS) * (BLOCK_SIZE // len(CONDITIONS))
    (rng or _rng).shuffle(block)
    return block


def validate_stance(stance):
    """The one gate between participant input and the draw. Exact match only:
    no strip(), no lower(), no truthiness -- anything else is rejected."""
    if type(stance) is not str or stance not in STANCES:
        raise ValueError(f"stance must be exactly one of {STANCES}, got {stance!r}")
    return stance


def draw_condition(stance, next_slot):
    """Deal the next condition for `stance`.

    `next_slot(stance)` is supplied by the caller (db.py) and returns the next
    unclaimed (block_no, position, condition) for that stance, creating a new
    block when the current one is used up. The stance is the ONLY
    participant-derived value that enters the draw, and it is validated here.
    """
    stance = validate_stance(stance)
    block_no, position, condition = next_slot(stance)
    if condition not in CONDITIONS:
        raise ValueError(f"allocation table returned an invalid condition: {condition!r}")
    return {"stratum": stance, "block": block_no, "position": position,
            "condition": condition, "scheme": SCHEME}
