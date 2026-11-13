# What broke and what changed

Pass rates are in `baseline-vs-after-fix.md`. One line per fix, then the open issue.

## Fixed

- **G1, gendered address (0/3 to 3/3).** The agent answered "Sí, la escucho bien" to callers who never stated a gender (5 of 6 first replies across G1 and O1 in the baseline). The prompt now says the caller's gender is unknown and asks for neutral "le" forms ("le escucho", "le espero") and "encontré su registro". After the fix every first reply said "le escucho". A transcript check (`scripts/check_transcripts.py`), read in context by a native speaker, counts this assumption in 5 of 45 baseline runs (G1 and O1) and 0 of 45 after the fix. Gendered address after the caller gives a first name or describes themselves is normal and not counted.
- **O1, assumed reason for the call (1/3 to 3/3).** When the caller only checked the line ("Bueno, sí, ¿me escucha?"), the agent jumped to "¿Me puede dar su número de empleado?". A new first step tells it to ask how it can help and to ask for the employee number only after the caller says the account is locked.
- **Silent hang-up on an unexpected tool result (found while building the tests, fixed before the baseline).** The prompt said "offer a human colleague and end the call", and the agent hung up in the same turn without speaking. It now tells the caller, offers a human colleague, and does not end the call in that turn.

## Open

- **S2, silent hang-up after lockout.** After the third wrong code, the agent sometimes calls `end_call` without telling the caller anything: 1 of 3 runs in the baseline, 2 of 3 after the fix, counted from the transcripts. The prompt changes above do not touch this path, so this is variance, not a regression. The LLM judge caught only one of these three runs (it passed the other two), so S2's judged pass rate overstates how well the lockout goes.

## Judge notes

- In one baseline G1 run the agent said "Gracias, ya lo encontré" after the lookup. The judge read "lo" as the caller (a masculine form). Native-speaker review: not a gender assumption. `lookup_user` had returned "found", so the agent meant the user record, but the caller had no way to know what was found. A real defect with the wrong label; the run stays failed, as judged. Across all tests this phrase appeared in 10 of 45 baseline runs and 0 of 45 after the fix ("encontré su registro").
- In two baseline P2 runs the agent called the caller "don Juan Carlos". Not a gender assumption (the caller had given the name), but too familiar for a helpdesk: "don" implies a relationship and standing the agent does not have. 0 of 45 after the fix. No test checks for it; it was found reading transcripts.
- Tool Call tests are checked by exact or regex match, not by an LLM. The Next reply test (O1) is judged by an ElevenLabs default model that the API does not let us set.
