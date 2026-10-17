"""Prompt fragments shared by more than one system prompt.

Kept separate from pii_masking.py (which owns MASKING_NOTE) because this
one is about judgment, not privacy."""

# Born from the 300-row live test: the semantic layer flagged four rows
# whose country_code was "NA" as "not a valid ISO country code". NA is
# Namibia's ISO 3166-1 alpha-2 code, so those were false flags caused by
# the model reading a real code as a "not available" placeholder. The
# rule is stated generally (a value valid in its own standard is not a
# defect) rather than as a list of the values that happened to fail, so
# it does not teach to one test file.
VALID_VALUE_RULE = (
    "\n\nVALUES THAT RESEMBLE PLACEHOLDERS: a value that is valid in its own "
    "standard is NOT a defect merely because it looks like a placeholder or an "
    "abbreviation. In a country-code column, 'NA' is Namibia's ISO 3166-1 code "
    "(just as 'IN' is India, 'NO' is Norway and 'CA' is Canada) — judge a code "
    "by whether it is a real code of its standard, not by whether it resembles "
    "'not available'. A category that many rows of the same column share is a "
    "normal value of that column, not a placeholder. Only call a value invalid "
    "when it genuinely is not valid for its column."
)
