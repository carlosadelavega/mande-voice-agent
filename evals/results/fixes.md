# What broke and what changed

Pass rates are in `baseline-vs-after-fix.md`. One line per fix, then the open issue.

## Fixed

- **G1, gendered address (0/3 to 3/3).** The agent answered "Sí, la escucho bien" to callers who never stated a gender (5 of 6 first replies across G1 and O1 in the baseline). The prompt now says the caller's gender is unknown and asks for neutral "le" forms ("le escucho", "le espero") and "encontré su registro". After the fix every first reply said "le escucho". A transcript check (`scripts/check_transcripts.py`), read in context, found gendered address to callers who never stated their gender in 9 of 45 baseline runs across G1, O1, P2 ("don Juan Carlos") and S3 ("señora Itzel"), and in 0 of 45 after the fix. The judge only checks gender in G1, so the P2 and S3 runs had passed.
- **O1, assumed reason for the call (1/3 to 3/3).** When the caller only checked the line ("Bueno, sí, ¿me escucha?"), the agent jumped to "¿Me puede dar su número de empleado?". A new first step tells it to ask how it can help and to ask for the employee number only after the caller says the account is locked.
- **Silent hang-up on an unexpected tool result (found while building the tests, fixed before the baseline).** The prompt said "offer a human colleague and end the call", and the agent hung up in the same turn without speaking. It now tells the caller, offers a human colleague, and does not end the call in that turn.

## Open

- **S2, silent hang-up after lockout.** After the third wrong code, the agent sometimes calls `end_call` without telling the caller anything: 1 of 3 runs in the baseline, 2 of 3 after the fix, counted from the transcripts. The prompt changes above do not touch this path, so this is variance, not a regression. The LLM judge caught only one of these three runs (it passed the other two), so S2's judged pass rate overstates how well the lockout goes.

## Judge notes

- In one baseline G1 run the agent said "Gracias, ya lo encontré" after the lookup. The judge read "lo" as the caller (a masculine form); it can also mean "I found it" (the record). The run is counted as failed, as judged.
- Tool Call tests are checked by exact or regex match, not by an LLM. The Next reply test (O1) is judged by an ElevenLabs default model that the API does not let us set.
