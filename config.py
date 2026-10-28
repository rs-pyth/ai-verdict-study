"""
Study content — the text participants actually see, and the AI prompt
template. Kept here as constants (not hardcoded in HTML/JS) so you can
edit wording without touching app logic.

REPLACE THE PLACEHOLDER TEXT BELOW with your real consent form, scenario,
and debrief text before running a real study.
"""

import os

# --- contact email, used in both consent and debrief text below ---------
# Read from the environment (like GEMINI_API_KEY / ADMIN_EXPORT_PASSWORD)
# rather than hardcoded here, since this file is committed to a public
# repo — set the real value in .env locally and in Render's dashboard.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "[CONTACT_EMAIL not set]")

# --- Step 0: consent screen -------------------------------------------
CONSENT_TEXT = f"""About this study

Thank you for considering taking part in this research study, conducted independently by a high school student (Ria Singh) as part of an investigation into how people respond to AI-generated feedback on their reasoning.

What you'll do: You'll read a short, hypothetical investment scenario, state whether you'd make the investment and explain your reasoning, then receive a response from an AI tool. Afterward, you'll answer some questions about your reaction to that response and restate your decision. The whole thing takes about 8–10 minutes.

What's being studied: This study is not evaluating you, your intelligence, or the quality of your investment reasoning. It is studying how people's trust in an AI system is affected by whether that system agrees or disagrees with them. Full details will be shared with you after you complete the study, since knowing them in advance would affect the results.

Your data: Your responses are anonymous — we do not collect your name or contact information. A random ID is assigned to your responses for research purposes only. Data will be used for a student research project and may be shared publicly in aggregate, anonymized form (e.g., in a write-up or presentation); no individual's identity will ever be connected to their responses.

Risks: This study involves a hypothetical scenario, not real money or real decisions, so there is no financial risk. Some participants may find AI feedback that disagrees with their reasoning mildly uncomfortable; there is no other anticipated risk.

Your rights: Participation is completely voluntary. You may stop at any point without penalty, and you may skip any question you're not comfortable answering. There is no compensation for participating.

Questions: If you have questions about this study, you can contact Ria Singh at {CONTACT_EMAIL}. This study has been reviewed by my faculty advisor, Ben Charoenwong (INSEAD), rather than a formal Institutional Review Board (IRB), as this is an independent student research project."""

# Checkbox label shown next to the consent checkbox on Step 0 — combines
# the age attestation with agreement to participate, matching CONSENT_TEXT.
CONSENT_CHECKBOX_LABEL = (
    "I am 13 years of age or older, I have read the above, "
    "and I agree to participate."
)

# Minimum age enforced against the numeric age entered in Step 1
# covariates, to back up the checkbox attestation above with an actual
# check. If you raise/lower the age line in CONSENT_TEXT and the checkbox
# label, update this to match.
MINIMUM_AGE = 13

# --- Step 2: the fixed investment scenario -----------------------------
SCENARIO_TEXT = """TechFlow Inc. is a mid-sized software company. Over the past year, its stock price has risen 40%, driven mainly by strong revenue growth. A closer look shows that TechFlow's revenue growth has come almost entirely from one large government contract signed 8 months ago, which accounts for 35% of total revenue this year. The company's core subscription business (its original product line) has grown only 3% over the same period — well below the industry average of 12%. TechFlow's CEO recently stated in an earnings call that the company is "exploring additional government partnerships to build on this momentum."

You are considering investing a portion of your savings in TechFlow Inc. stock.
"""

# Stance question wording — asked twice (Step 2, before the AI verdict, and
# Step 6, after it). Templates and JS reference these two constants rather
# than hardcoding the question text, so both stay in sync with the fixed
# scenario above if you ever change the company/investment framing.
STANCE_QUESTION_PRE = "Based on this information, would you invest in TechFlow Inc.?"
STANCE_QUESTION_POST = "After seeing this analysis, would you invest in TechFlow Inc.?"

# --- Step 7: debrief screen ---------------------------------------------
# The literal token {{PARTICIPANT_ID}} below is NOT Jinja syntax — this
# string is passed into the template as plain variable content, so Jinja
# never re-parses it. static/js/app.js finds-and-replaces that exact
# token with the real participant ID client-side when Step 7 is shown,
# since the ID isn't known until the browser generates it.
WITHDRAWAL_WINDOW = "7 days"

DEBRIEF_TEXT = f"""Thank you — here's what this study was actually about.

The AI's response to your reasoning was not a genuine assessment of it. After you told us whether you would invest, a random draw decided whether the AI would agree or contradict that decision. The draw was made separately among participants who gave the same answer as you, so that agreeing and contradicting responses were evenly shared, and it did not depend on what you wrote or on how good your reasoning was. The AI was then told which verdict to give and wrote its explanation to fit that verdict, using only the facts from the scenario. In a small number of cases, if the AI could not produce a response that fit, you saw a pre-written response instead. None of this reflects on how good your reasoning actually was.

Why the deception was necessary: If you had known in advance that the AI's agreement or contradiction was assigned at random, it likely would have changed how much you trusted or engaged with its response — which would have made it impossible to measure what we're actually studying.

What we're studying: A common finding in behavioral research is that people trust AI tools more when the AI agrees with them, and less when it disagrees — even when the AI's disagreement might be the more useful, accurate signal. This matters because tools designed to catch flawed reasoning (like the one this study is modeled on) only work if people actually listen when the tool pushes back. We're testing whether that trust gap shows up here, and whether it's specific to disagreement itself rather than to the quality of the AI's reasoning.

Your data: Your responses remain anonymous and will be used only for this research. If you'd like to withdraw your data now that you know the full purpose, contact {CONTACT_EMAIL} with your participant ID (shown below) within {WITHDRAWAL_WINDOW}, and it will be deleted.

Your participant ID: {{{{PARTICIPANT_ID}}}}

Questions or concerns: {CONTACT_EMAIL}. Thank you again for your time — this study wouldn't be possible without you."""

# --- Step 4: AI verdict prompt (v3 — told the target, verified afterwards) ---
#
# v2.1 (see git history) never told the model which verdict to give: it
# audited the participant's reasoning against a fact-set and the direction
# fell out of that. Measured against the assigned condition under the
# stance-aware mapping it matched only ~77% of the time, with the misses
# concentrated in vague rationales ("Too risky.", "No."), which would have
# attenuated the manipulation. v3 TELLS the model the verdict to report
# ("VERDICT TO REPORT"), asks for a JSON object {"verdict", "prose"}, and
# verdicts.py then checks the returned `verdict` field against the assigned
# condition -- regenerating up to MAX_VERDICT_ATTEMPTS times, then serving a
# stored fallback and logging that it did. Temperature stays 0 on purpose:
# no sampling randomness in the wording.
#
# The fact-set (scenario_facts_for_condition() in app.py) is unchanged: for
# "agree" it is the set that backs the participant's stance, for
# "contradict" the set that opposes it, so the prose has real material to
# cite in either direction. The verdict itself no longer depends on it.
#
# NOT YET APPROVED FOR PARTICIPANTS -- this is the stimulus-generating
# instruction, so it needs the researcher's sign-off before it ships.
#
# Placeholders filled in by build_verdict_prompt() in app.py:
# {target_verdict} ("supported" | "not_supported"), {min_words},
# {max_words}, {scenario_facts}, {participant_reasoning}.
VERDICT_PROMPT_TEMPLATE = """You are writing one short verdict for a research study. A participant has just explained, in their own words, why they would or would not invest in a venture. You are given a fixed list of FACTS about the venture, the participant's REASONING, and the VERDICT TO REPORT.

VERDICT TO REPORT: {target_verdict}

This verdict is fixed. Do not decide it, reconsider it, or soften it. Your task is to write the short explanation that goes with it.

Rules, in order of priority:

1. Use ONLY the facts in FACTS below. Do not introduce any detail, number, name, organization, or claim that is not explicitly present in FACTS or in the participant's REASONING. Do not add general or background claims about how companies, markets, or industries "typically" or "often" behave. Every claim you make must trace to a specific line in FACTS or REASONING.
2. The explanation must agree with the verdict. If the verdict is "supported", explain how the FACTS bear out the participant's reasoning. If the verdict is "not_supported", explain how the FACTS do not bear it out. Do not say or imply that the verdict was given to you or decided in advance.
3. Write in third person about "the participant". Never use "you" or "your".
4. Use exactly 4 sentences. Sentence 1 must be exactly "The participant's reasoning is supported by the facts." when the verdict is "supported", and exactly "The participant's reasoning is not supported by the facts." when the verdict is "not_supported". Sentences 2-3 cite specific facts from FACTS, in wording that stays close to the original. Sentence 4 is a neutral closing sentence that restates the verdict, with no advice, encouragement, or warning.
5. Length: between {min_words} and {max_words} words in total across the 4 sentences, for BOTH verdicts. Match tone, sentence length, and vocabulary across both verdicts as closely as possible.
6. Do not use intensifiers or affect-laden words that do not appear in FACTS or REASONING (for example "alarming," "reassuring," "risky," "solid," "concerning," "impressive," "genuine," "substantial," "deliberate"). Stay descriptive and factual.
7. The explanation is plain text: no markdown, no bullet points, no headers, no line breaks.

OUTPUT FORMAT: respond with ONLY a JSON object, with nothing before or after it, in exactly this shape. The "verdict" value must be exactly the VERDICT TO REPORT above, and "prose" must be the 4 sentences.
{{"verdict": "supported" or "not_supported", "prose": "..."}}

EXAMPLES (fictional company, for structure and length only; these facts do not apply to the real task below):

FACTS:
- Meridian Foods' stock price fell 12% over the past year.
- Meridian's flagship product line was discontinued six months ago.
- Meridian added two new product lines this year, which together account for 8% of total revenue.
- Meridian's CEO stated in a press release that the company is focused on stabilizing existing operations before pursuing new products.

REASONING: "I wouldn't invest because the stock has been dropping and they just cancelled their main product."
VERDICT TO REPORT: supported
OUTPUT: {{"verdict": "supported", "prose": "The participant's reasoning is supported by the facts. Meridian Foods' stock price fell 12% over the past year, matching the participant's observation that the stock has been dropping, and the flagship product line was discontinued six months ago, matching the reference to a cancelled main product. The facts also state that Meridian added two new product lines this year, which together account for 8% of total revenue. These facts correspond directly to the stock decline and the discontinued product named in the participant's reasoning, so the reasoning is supported by the facts."}}

REASONING: "I'd invest because the company just added two new product lines that will drive growth."
VERDICT TO REPORT: not_supported
OUTPUT: {{"verdict": "not_supported", "prose": "The participant's reasoning is not supported by the facts. The facts state that Meridian added two new product lines this year, which together account for 8% of total revenue, and that the CEO described a focus on stabilizing existing operations before pursuing new products. The facts also state that Meridian's stock price fell 12% over the past year and that its flagship product line was discontinued six months ago. No fact states that the new product lines will drive growth or raise the stock price, so the participant's reasoning is not supported by the facts."}}

Now do the same for the real task below.

FACTS:
{scenario_facts}

REASONING (participant's own words):
{participant_reasoning}
"""

# API call settings for the verdict generation — see api_generate_verdict()
# in app.py. temperature=0 and a fixed max_tokens backstop are part of the
# original spec, not incidental: output length/structure is meant to be
# fixed by the prompt, and low temperature keeps the audit as
# deterministic as a language model call can be.
#
# AI_MAX_TOKENS raised from the spec's original 150 after two live tests
# both cut off mid-sentence — first at "The participant" (150 tokens),
# then mid-second-sentence (1024 tokens) — gemini-3.6-flash is a
# reasoning-capable model that spends tokens on internal "thinking" before
# producing visible output, and that thinking counts against max_tokens.
# 1024 still wasn't enough headroom, so jumping further rather than
# creeping up one redeploy at a time. The prompt's own 4-sentence rule is
# still what keeps the VISIBLE answer short, not this number — same
# "backstop, not a lever" intent as before, just recalibrated again for a
# model that spends tokens on reasoning more than initially expected.
AI_TEMPERATURE = 0
AI_MAX_TOKENS = 4096

# --- Verdict verification (see verdicts.py) -----------------------------------
# A generation is accepted only if its `verdict` field equals the target for
# the participant's assigned condition. Up to MAX_VERDICT_ATTEMPTS generations
# are tried; API errors (503/429/timeouts) are logged but do NOT count as
# attempts. The whole thing is bounded by VERDICT_TIME_BUDGET_S, which must
# stay under the 120s gunicorn worker timeout (gunicorn.conf.py).
MAX_VERDICT_ATTEMPTS = 3
VERDICT_TIME_BUDGET_S = 90

# Length target given to the model (not enforced; the actual word count is
# recorded per verdict so any residual length difference can be adjusted for).
VERDICT_WORD_MIN = 80
VERDICT_WORD_MAX = 100

# The verdict the model is told to report for each assigned condition.
TARGET_VERDICT = {"agree": "supported", "contradict": "not_supported"}

# Stored fallback verdicts, one per (pre_stance, condition) cell, served when
# verification fails MAX_VERDICT_ATTEMPTS times or the time budget runs out
# (the use and its reason are logged). TO BE WRITTEN BY THE RESEARCHER:
# plain text, 80-100 words, same 4-sentence format as generated verdicts,
# and the direction must match the cell (agree -> "supported", contradict ->
# "not supported"). None means "not written yet": the app will NOT invent a
# stand-in; it returns an error for that participant instead. Run
# `python check_fallbacks.py` to validate them before launch.
FALLBACK_VERDICTS = {
    ("yes", "agree"): None,
    ("yes", "contradict"): None,
    ("no", "agree"): None,
    ("no", "contradict"): None,
}

# Two fact-sets per scenario, named by what they contain, NOT by the
# condition they used to be tied to. Which one a participant is audited
# against is decided by (randomized condition, the participant's own
# pre-stance) — see scenario_facts_for_condition() in app.py:
#   condition 'agree'    -> the set that SUPPORTS their stance
#                           (stance yes -> GROWTH, stance no -> RISK)
#   condition 'contradict' -> the set that OPPOSES their stance
#                           (stance yes -> RISK,   stance no -> GROWTH)
#
# Why the stance is in the mapping (2026-09-21): the original mapping tied
# 'agree' to the growth set and 'contradict' to the risk set regardless of
# stance. The 60-row pilot export showed that made `condition` nearly
# meaningless on its own: for substantive rationales the verdict was
# "supported" 46% of the time under agree vs 50% under disagree, because
# what actually drove the verdict was whether the facts a participant
# cited fell inside their assigned set — and people answering "yes" cite
# growth facts while people answering "no" cite risk facts (agree+yes 5/6
# supported, disagree+no 5/6, agree+no 1/7, disagree+yes 1/6). With the
# stance in the mapping, 'agree' means "audited against facts that back
# what you said" for everyone, so condition IS the manipulation.
#
# This does NOT touch the randomization's independence: the condition is
# still drawn (randomization.py, zero inputs) and stored before anything
# participant-authored is read, in /api/submit-stance. The stance is only
# consulted later, at verdict time, to implement a condition that was
# already assigned — condition is independent of stance by construction,
# and the stance never influences which condition was drawn.
#
# This is the actual experimental manipulation — the model genuinely
# audits, so the fact-set shown is what steers the verdict, not an
# instruction to fake it.
#
# DRAFT — needs your and Ben's review before real participants see it,
# same as the prompt itself. Every line below is true per SCENARIO_TEXT;
# nothing is invented or altered. The two sets differ only in WHICH true
# facts are included: GROWTH has the growth-case facts (stock price, the
# contract win, the CEO's forward-looking quote), RISK has the risk-case
# facts (core-business underperformance vs. industry benchmark, revenue
# concentration, the same CEO quote — now relevant as continued reliance
# on that concentration). Rule 1 in the prompt above means whatever's NOT
# in a given set effectively doesn't exist to that audit — that's the
# actual lever making this work, not fabrication. Two consequences worth
# being explicit about in your write-up:
#   1. A participant who reasons well but cites facts outside their
#      assigned set (e.g. a "yes" who leans on the concentration risk,
#      even under 'agree', which audits them against GROWTH) can still
#      come back "unsupported" for reasons unrelated to reasoning quality.
#      So 'agree' raises the chance of "supported" a lot but doesn't
#      guarantee it. Fine for THIS study's question (does disagreement
#      suppress trust regardless of merit?); NOT fine if you ever wanted
#      "supported/unsupported" to double as a reasoning-quality measure.
#   2. Rows collected before this change (recruitment_source
#      'pilot-audit-batch-3', all pre-2026-09-21 deploys) used the old
#      mapping and are not comparable with anything collected after it.
SCENARIO_FACTS_GROWTH = """- TechFlow Inc.'s stock price has risen 40% over the past year.
- TechFlow signed a large government contract 8 months ago, which now accounts for 35% of TechFlow's total revenue this year.
- TechFlow's CEO stated in an earnings call that the company is "exploring additional government partnerships to build on this momentum.\""""

SCENARIO_FACTS_RISK = """- TechFlow's core subscription business (its original product line) grew only 3% over the past year, compared with an industry average growth rate of 12% for comparable subscription businesses.
- Almost all of TechFlow's revenue growth this year has come from one large government contract signed 8 months ago, which now accounts for 35% of TechFlow's total revenue this year.
- TechFlow's CEO stated in an earnings call that the company is "exploring additional government partnerships to build on this momentum.\""""

# --- Step 1: covariates ---------------------------------------------

AI_USE_QUESTION = "How often do you use AI tools such as ChatGPT, Claude, or similar?"
AI_USE_LABELS = ["Never", "Rarely", "Sometimes", "Often", "Daily"]  # 1 through 5

TECH_AFFINITY_ITEMS = [
    "I am comfortable learning to use new technology.",
    "I enjoy figuring out how new software or apps work.",
    "I feel confident using unfamiliar digital tools.",
]

# Gender, asked in Step 1 alongside age/grade. Stored as the short code
# (first element), shown as the label (second). Closed list on purpose —
# no free text — so the column stays analyzable and holds no stray
# personal detail. "Prefer not to say" is a real option (not a skip), so
# the question can stay required in the UI without forcing disclosure,
# which is what the consent text promises ("you may skip any question").
# /api/covariates rejects any value not in this list. With small samples
# the sparse categories (nonbinary / another / prefer_not_to_say) will
# have very few rows: pre-decide how to collapse them before analysis
# rather than after seeing results.
GENDER_QUESTION = "What is your gender?"
GENDER_OPTIONS = [
    ("woman", "Woman"),
    ("man", "Man"),
    ("nonbinary", "Non-binary"),
    ("another", "Another gender not listed"),
    ("prefer_not_to_say", "Prefer not to say"),
]

# The standard "Big Three" financial literacy items (Lusardi & Mitchell):
# compound interest, risk diversification, inflation. Each has a single
# correct option index (0-based) into its own `options` list.
FIN_LIT_QUESTIONS = [
    {
        "question": (
            "Suppose you had $100 in a savings account earning 2% interest "
            "per year. After 5 years, how much would you have — more than "
            "$102, exactly $102, or less than $102?"
        ),
        "options": ["More than $102", "Exactly $102", "Less than $102"],
        "correct_index": 0,
    },
    {
        "question": (
            "True or False: Buying a single company's stock usually "
            "provides a safer return than a mutual fund."
        ),
        "options": ["True", "False"],
        "correct_index": 1,
    },
    {
        "question": (
            "If inflation is higher than the interest rate on your "
            "savings, will you be able to buy more, less, or the same "
            "amount with your savings?"
        ),
        "options": ["More", "Less", "The same"],
        "correct_index": 1,
    },
]

# --- Step 6: post-verdict scales -----------------------------------------

# DV1 — Trust in Automated Systems (Jian, Bisantz & Drury, 2000). Kept in
# the original "the system" wording (not adapted to "the AI") — that's
# deliberate per the instrument spec, for comparability with the source
# scale, even though the rest of this study's copy says "the AI"/"this AI
# tool". If that inconsistency in phrasing bothers you, it's easy to
# find-and-replace here, but it's the literal validated wording.
TRUST_INSTRUCTIONS = "Thinking about the analysis you just received, please rate your agreement with each statement."

# Scoring note for analysis: items 1-5 are the distrust subscale and are
# reverse-scored when computing a composite trust score; items 6-11 are
# the trust subscale (higher = more trust); item 12 (familiarity) is
# usually reported/analyzed separately, not folded into the trust
# composite, per the original factor analysis. Consider reporting both
# the 12-item and 11-item (dropping familiarity) composite.
#
# Item 2 below is reworded from the literal Jian et al. wording
# ("behaves in an underhanded manner") to plain language — "underhanded"
# read as confusing/unfamiliar to test participants. This is a deliberate
# deviation from the source scale, unlike the "the system" phrasing above.
TRUST_ITEMS = [
    "The system is deceptive.",                                    # 1 - reverse-scored
    "The system behaves in a sneaky or dishonest way.",             # 2 - reverse-scored
    "I am suspicious of the system's intent, action, or outputs.",  # 3 - reverse-scored
    "I am wary of the system.",                                     # 4 - reverse-scored
    "The system's actions will have a harmful or injurious outcome.",  # 5 - reverse-scored
    "I am confident in the system.",                                # 6
    "The system provides security.",                                # 7
    "The system has integrity.",                                    # 8
    "The system is dependable.",                                    # 9
    "The system is reliable.",                                      # 10
    "I can trust the system.",                                      # 11
    "I am familiar with the system.",                               # 12 - familiarity, not part of trust composite
]

# DV2 — Perceived competence / credibility
COMPETENCE_ITEMS = [
    "This AI tool is competent at analyzing investment decisions.",
    "This AI tool's reasoning was well thought out.",
    "This AI tool is knowledgeable about investing.",
    "I would consider this AI tool credible on financial topics.",
]

# DV4 — Perceived bias. Item 2 is reverse-scored (a fairness statement,
# not a bias statement) — flip it before combining into a bias composite.
BIAS_ITEMS = [
    "This AI tool's analysis was biased.",
    "This AI tool was fair in how it evaluated my reasoning.",  # reverse-scored
    "This AI tool seemed to have an agenda rather than an objective view.",
]

# DV5 — Intention to use again
INTENTION_ITEMS = [
    "I would use this AI tool again for future investment decisions.",
    "I would recommend this AI tool to someone else.",
    "I would trust this AI tool with a more important financial decision than this one.",
]

# Instructed-response attention check — inserted as item 13 in the trust
# grid template (templates/index.html), after the 11 composite + 1
# familiarity item so participants have already settled into a response
# pattern. NOT part of TRUST_ITEMS on purpose: it must never be scored
# into the trust composite or counted as a 12th/13th trust item, it's a
# gate, not a measure — stored in its own DB column
# (attention_check_response), collected by its own field name
# ("attention_check", not "trust_13") in app.js.
#
# The trust/competence/bias/intention grids currently show only bare
# numbers 1-7 with no verbal anchor labels anywhere in the UI, so the
# item text below states the label AND the number explicitly rather than
# assuming "Somewhat agree" is visible on the scale itself. The mapping
# assumes the intended (unlabeled) scale is a standard Strongly
# Disagree(1)...Strongly Agree(7) agreement scale, consistent with
# TRUST_INSTRUCTIONS above — "Somewhat agree" is one point above the
# midpoint (4), i.e. 5.
ATTENTION_CHECK_ITEM = (
    "To confirm you are reading carefully, please select \"Somewhat "
    "agree\" — the number 5 on this scale — for this item."
)
ATTENTION_CHECK_CORRECT_VALUE = 5

# DV3 — Advice-taking / Weight-on-Advice is NOT asked directly; it's
# computed from pre_stance vs. post_stance (switch rate, broken out by
# condition and switch direction) and pre_confidence vs. post_confidence
# in your own analysis from the CSV export. See db.py for those columns.
