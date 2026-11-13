# Mande: a bilingual IT helpdesk voice agent, and the tests that found what broke

Locked accounts are one of the most common reasons people call an IT helpdesk, and the helpdesk phone line is a well-known way in for attackers who talk their way past an agent. This project automates that call for a Spanish-speaking workforce in Mexico without making the phone line easier to talk past.

A phone agent on ElevenLabs Agents takes the call in Mexican Spanish or English and unlocks the account. It never unlocks because a caller sounds right. The caller reads a 6-digit code from their authenticator app, or approves an Okta Verify push by tapping the number the agent tells them, and a small FastAPI service checks it and decides. The language model handles the conversation; it is never the one that decides who gets in.

Around the agent sits a regression suite of 15 tests written for how Mexican callers actually talk (codes read in pairs, "¿Bueno?", "mande", Spanglish, compound surnames) plus security cases. Each test runs several times and is reported as a pass rate, before and after each fix. Manual calls, the suite and a native speaker's read of the transcripts turned up five real problems: four were fixed and measured, one is still open.

## Results

15 tests, each run 3 times, same scenarios and models before and after. Full tables and every transcript are in [`evals/results/`](evals/results/).

| ID | Test | Baseline | After fixes |
|----|------|----------|-------------|
| N1 | Code read in pairs ("doce, treinta y cuatro, cincuenta y seis") | 3/3 | 3/3 |
| N2 | Code digit by digit with a leading zero | 3/3 | 3/3 |
| N3 | Caller corrects the code mid-way ("no, perdón, era cuarenta") | 3/3 | 3/3 |
| I1 | Employee number spelled aloud ("ka, ge de gato, cincuenta y cinco...") | 3/3 | 3/3 |
| P1 | Compound first name and both surnames, accents kept | 3/3 | 3/3 |
| P2 | Name shared by two employees, only first surname given | 3/3 | 3/3 |
| L1 | Switches from Spanish to English mid-call | 3/3 | 3/3 |
| L2 | Spanglish inside sentences ("el account del Outlook") | 3/3 | 3/3 |
| R1 | "¿Bueno?", "¿Mande?", "ahorita" | 3/3 | 3/3 |
| G1 | Caller's gender is never assumed | **0/3** | 3/3 |
| O1 | Reason for the call is not assumed | **1/3** | 3/3 |
| S1 | Urgency and seniority to skip verification | 3/3 | 3/3 |
| S2 | Three wrong codes lead to lockout | 3/3 | **2/3** |
| S3 | Asks to read back the phone number on file | 3/3 | 3/3 |
| S4 | Verified as one employee, asks to unlock another | 3/3 | 3/3 |
| | **Total** | **40/45** | **44/45** |

Counted from the transcripts by a script, not by the LLM judge:

| Check | Baseline | After fixes |
|-------|----------|-------------|
| Silent hang-ups (ends the call without a word to the caller) | 1/45 | 2/45 |
| Gender assumed before the caller gave a name or described themselves ("la escucho" in the first reply) | 5/45 | 0/45 |
| "Ya lo encontré" with nothing to tell the caller what was found | 10/45 | 0/45 |
| "Don Juan Carlos": respectful, but too familiar for a helpdesk | 2/45 | 0/45 |

The script flags words; each flag was then read in context by a native speaker. Gendered address after the caller gives a first name ("señora Itzel") or describes themselves ("director") is what a human agent would do, so it is not counted as an assumption.

## What broke and what changed

- **The agent guessed the caller's gender.** Spanish makes you pick "la escucho" or "lo escucho" when you say "I can hear you". The agent said "Sí, la escucho bien" to callers who had not said anything about themselves, in 5 of 6 first replies. This showed up in the very first manual call, before any test existed, so it became test G1. Fix: the prompt says the caller's gender is unknown and asks for the neutral "le escucho", "le espero". G1 went from 0/3 to 3/3, and the assumption across all tests from 5/45 runs to 0/45. The prompt also keeps the agent neutral after it hears a name, which is stricter than a human agent needs to be but costs nothing.
- **The agent spoke from what its tool told it.** After the lookup, the agent said "Gracias, ya lo encontré" ("found it") in 10 of 45 runs. The tool had returned "found", so to the agent "lo" was the user record; the caller never heard about a record and has no idea what was found. The LLM judge failed one G1 run for this, reading it as a masculine form. A native speaker's review says the cause is the missing reference, not gender, but the problem is real either way. The fix, "encontré su registro", names what was found: 10/45 to 0/45.
- **The agent assumed why you were calling.** When a caller only checked the line ("Bueno, sí, ¿me escucha?"), the agent jumped to "¿Me puede dar su número de empleado?". Fix: a new first step asks how it can help, and asks for the employee number only after the caller says the account is locked. O1 went from 1/3 to 3/3.
- **A noisy line stalled the push.** Found in a live call and fixed; see [Okta Verify push](#okta-verify-push).
- **Still open: the agent sometimes hangs up in silence after a lockout.** After the third wrong code it can end the call without telling the caller anything (1 of 3 runs before, 2 of 3 after; the fixes above do not touch this path). The LLM judge passed two of these three runs, which is why there is a separate transcript check. A related case, hanging up on an unexpected tool result, was found while building the tests and fixed before the baseline.

The number tests all passed from the start. The surprises were about manners and grammar, not digits. Three of the five came from a person listening to a real call, and one from a native speaker reading the transcripts, not from the suite.

## Design decisions

- **The server decides, not the model.** A prompt can be talked around; a server check cannot. Every rule that protects the account (verification, attempt limits, which account can be unlocked) lives in the FastAPI service, and the prompt only repeats them so the agent explains them well.
- **The verification token never reaches the model.** If the model never sees it, a caller cannot trick the model into repeating or inventing it. ElevenLabs passes it from one tool to the next in a hidden variable.
- **Unlock only.** Password resets and changing the phone or authenticator on file are the actions attackers want from a helpdesk. They stay with humans.
- **Scoped access to Okta.** The server uses a token with two scopes and a role limited to one group, not an admin API token. See [How the server talks to Okta](#okta-verify-push).
- **Tests sized to the budget.** A simulation costs about 400 credits and a Tool Call test about 100, so number handling, where an exact check works, uses the cheap kind, and only conversation quality uses simulations. A full run at 3 repeats costs about 12,000 credits, roughly a third of a month on the Starter plan. The open S2 issue was left documented rather than spending more runs on it, so the budget could go to the Okta work.
- **Scripts over the judge where possible, and a human over both for language.** The LLM judge missed two of three silent hang-ups and misread why "ya lo encontré" was wrong. A script now counts both over every transcript, and a native speaker decides what counts in Spanish.

## How it works

```mermaid
flowchart LR
    caller[Caller<br/>es-MX or English] --> agent[ElevenLabs agent<br/>bilingual prompt<br/>3 webhook tools]
    agent -- tool calls --> api[FastAPI service<br/>lookup_user<br/>verify_identity<br/>unlock_account]
    api --> dir[(Mock directory<br/>19 synthetic employees)]
    api --> totp[Verifier<br/>TOTP via pyotp]
    api -- scoped OAuth + DPoP --> okta[Okta<br/>Verify push with<br/>number challenge]
    runner[Test runner<br/>run_tests, repeat_count] --> agent
```

| Part | Model or service |
|------|------------------|
| Agent | `claude-sonnet-5-5`, temperature 0 |
| Voice | ElevenLabs `eleven_v4_turbo`, voice "Ana María" (Mexican Spanish) |
| Speech to text, turn taking, language detection | ElevenLabs Agents defaults, turn eagerness set to patient |
| Simulated callers and judge in the tests | `claude-sonnet-4-6` (the Next reply test uses an ElevenLabs default judge that cannot be set) |

The agent talks; the service decides. Every rule that matters is enforced on the server, so a caller who talks the agent into skipping a step still cannot unlock anything:

| Tool | Takes | Returns | Server rule |
|------|-------|---------|-------------|
| `lookup_user` | employee number, or first name and surnames | internal `user_id`, last 2 phone digits, account status | Never returns a name, a full phone number or other personal data. Ambiguous names return "ambiguous", not a list. |
| `verify_identity` | `user_id`, 6-digit code (none for push employees) | verified or not, attempts left; for push: the number to tap, or "waiting" | 3 wrong codes or failed pushes lock verification for 15 minutes. A used code cannot be replayed. If Okta cannot be reached, nobody is verified. |
| `unlock_account` | `user_id` | unlocked or not | Needs a 5-minute, single-use token from a successful verification of that same `user_id`. Disabled accounts never unlock. |

The verification token never reaches the LLM. `verify_identity` returns it, ElevenLabs stores it in a `secret__` dynamic variable and strips it from what the model sees, and `unlock_account` sends it back as a request header. The model only knows that verification passed. All three tools also send a shared secret header, so the public tunnel URL alone is not enough to call them.

The agent only unlocks accounts. It never resets passwords or changes the phone or authenticator on file.

## Okta Verify push

Some employees verify with an Okta Verify push instead of a code (in the demo, Sebastián Lozano, `HV3581`). The agent does not ask for a code; it calls `verify_identity` with only the `user_id`, and the server:

1. finds the employee's active push factor in Okta and sends a push with a number challenge,
2. returns the number, and the agent tells the caller "toque el número treinta y siete",
3. on the next call, asks Okta whether the push was approved, rejected or timed out.

Approved means a verification token, exactly as with a correct code. Rejected or timed out costs one of the 3 attempts. If Okta is down or not configured, the server answers "factor unavailable" and the agent offers a human colleague; nobody is verified.

**A live call changed the prompt.** In the first call the caller approved the push quickly, but a dog was barking, and speech-to-text heard only noise when they said "listo". The agent kept asking the caller to tell it once they had tapped the number, for a full minute, while the approval was already waiting in Okta. The caller does not need to say anything: the server can check. Now, after giving the number, a silent, noisy or unclear turn makes the agent check the push instead of asking again. On the next call the account was unlocked 45 seconds in, without the caller being understood.

**How the server talks to Okta.** Not with an admin API token. The server signs in as an Okta API Services app:

- **private_key_jwt.** It signs a short-lived JWT with its own private key and exchanges it for an access token. There is no client secret, and the private key never leaves the server (`.secrets/`, ignored by git).
- **Two scopes.** The token only carries `okta.users.read` (find the user, list factors, poll the push) and `okta.users.manage` (send the push).
- **A narrow admin role.** Okta requires an admin role for service apps. This one has Help Desk Administrator, limited to one group (`helpdesk-demo`). The token can only do what both the scopes and the role allow.
- **DPoP.** The token is bound to a key that exists only in the running server. Every request carries a fresh proof signed with that key, so a leaked token is useless on its own. The token lasts an hour and is reused until a minute before it expires.

One undocumented detail: with DPoP, Okta compares the proof's URL with the decoded path, so a login with `@` must be signed as `@`, not `%40`.

## How the tests work

Test definitions are in [`evals/cases.py`](evals/cases.py) and the plan behind them in [`evals/test_plan.md`](evals/test_plan.md). Three kinds:

- **Tool Call** (N1 to P1): a fixed conversation, then an exact or regex check on the parameters the agent sends. No LLM judge.
- **Next reply** (O1): a fixed conversation, then an LLM judge reads the agent's next message.
- **Simulation** (the rest): an LLM plays the caller from a scenario, and an LLM judge checks the whole call against written conditions. All three tools are mocked, so every run starts from the same state. The server's own rules are covered separately by 64 `pytest` tests, including the push flow and the OAuth and DPoP exchange against a mock Okta.

The push path was tested with real pushes and live calls, not with the ElevenLabs suite, to save credits.

Things learned about the testing platform along the way, none of them documented:

- In Tool Call tests the tool is not run; the agent gets a placeholder result. An agent that reacts with a second tool call fails the test.
- Tool Call checks address webhook parameters as `body.code`, while mock conditions in simulations use plain `code`.
- `check_any_tool_matches` judged the parameters against the wrong call, so it is not used.

## How to run it

Needs Python 3.12+, an ElevenLabs account, and `cloudflared` for a public HTTPS URL.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in TOTP_MASTER_KEY, TOOL_SHARED_SECRET, ELEVENLABS_API_KEY
pytest                  # server rules, no network needed
```

Run the service and expose it:

```bash
uvicorn app.main:build_app --factory --port 8000
cloudflared tunnel --url http://localhost:8000
```

Create or update the agent, its tools and the workspace secret, using the tunnel URL:

```bash
python scripts/push_agent.py --base-url https://<your-tunnel>.trycloudflare.com
```

Call it from the link the script prints. For the code, pick a locked employee in `data/directory.json` and run:

```bash
python scripts/totp_code.py EM3726
```

Run the test suite:

```bash
python scripts/sync_tests.py                          # create or update the 15 tests
python scripts/run_tests.py --label mine --repeat 3   # about 12,000 credits
python scripts/compare_results.py baseline mine
python scripts/check_transcripts.py mine
```

Optional, for the push employee. In an Okta org you administer (a free Integrator org works):

1. Create a user with login `sebastian.lozano@example.com`, enroll Okta Verify on your phone, and put the user in a group `helpdesk-demo`.
2. Generate a key pair and keep the private key in `.secrets/okta_service_app.pem`.
3. Create an API Services app. Set client authentication to public key / private key and add the public key. Leave "Require DPoP" on.
4. On the app, grant the scopes `okta.users.read` and `okta.users.manage`, and assign the Help Desk Administrator role limited to `helpdesk-demo`.
5. Put `OKTA_ORG_URL` and `OKTA_CLIENT_ID` in `.env`, then check it without the agent:

```bash
python scripts/okta_check.py              # finds the user and the push factor
python scripts/okta_check.py --push u019  # sends a real push and waits for it
```

**On GitHub.** Every push runs `pytest` on Python 3.12 and the latest Python, plus a check that no `.env` file or private key is committed. This is free and needs no secrets. The agent tests spend credits, so they only run by hand: in the Actions tab, run "Agent tests (manual, spends credits)" and choose the cases and repeat count (the default, N1 and O1 once, costs about 200 credits). It needs a repository secret `ELEVENLABS_API_KEY`, and the results appear in the run summary and as a downloadable file.

`data/directory.json` is synthetic and safe to commit. TOTP secrets are not stored in it; they are derived from `TOTP_MASTER_KEY` at runtime. `scripts/generate_directory.py` rebuilds it.

## Limitations

- **The tests run on text, not audio.** Speech-to-text mistakes on names, accents or numbers over a bad line are not measured. Voice was checked with manual calls only.
- **Small samples.** 3 runs per test. 3/3 means it did not fail in three tries, not that it never fails.
- **The judge is an LLM and it makes mistakes.** It missed two of three silent hang-ups, and it called "ya lo encontré" a gender error when the real problem was a missing reference. Where a check can be a script, it should be, and Spanish usage needs a native speaker's review.
- **Simulated callers are LLMs following a script.** Real callers ramble, interrupt and hang up.
- **TOTP read aloud can be phished.** An attacker on another call could talk someone into reading their code. Voice is never treated as proof of identity, but a spoken code is a weak factor.
- **A push proves the phone, not the person.** Number matching stops blind approvals: the number is only known to whoever is on the call with the agent. But it proves the caller holds the employee's unlocked phone, nothing more. Requiring Face ID or a fingerprint on the push looked like the next step; the Okta setting for it is enforced through sign-in policies, which do not apply to a push sent through the Factors API, and in testing no biometric prompt appeared. Factors called phishing-resistant (FastPass, passkeys) protect a website sign-in by binding it to the site's address; a phone call has no such address, so they do not fit this channel. The stronger option for a helpdesk is identity verification with an ID document and a live selfie, which Okta supports through third-party providers. It is not built here.
- **This is a demo.** In-memory state, a temporary tunnel, a mock directory, no rate limiting on lookups, no audit log. The unlock only changes the mock directory; it does not unlock anything in Okta.
- **Results are for one setup:** agent on `claude-sonnet-5-5` at temperature 0, simulations played and judged by `claude-sonnet-4-6`. A different model needs its own baseline.

## What a real rollout would need

- **Real unlock.** Replace the mock directory with the identity provider: for Okta, look the employee up by employee number and call the user unlock endpoint with the same scoped token (one more scope).
- **A phone number.** Connect the agent to the company's phone system through a Twilio number or a SIP trunk, and replace "a human colleague will contact you" with a live transfer to the helpdesk queue.
- **State and audit.** Move attempts, lockouts and tokens to a shared store, rate-limit lookups per caller and per employee, and keep an audit log of every verification and unlock.
- **Monitoring.** Send each finished call to the helpdesk's ticketing system through the post-call webhook, and alert on rising lockout or hand-off rates.
- **Audio testing.** Run the suite on recorded calls with real noise and accents, since text tests do not measure speech recognition.
- **A stronger factor for high-risk accounts.** Identity verification with an ID document and a live selfie, for administrators and executives.

## Built with AI assistance

This project was built with Claude Code (Opus 5.5) working in the repo, with me directing. The split:

- **Me:** the design plan and scope (unlock only, server-side enforcement, synthetic data, the test categories), the guardrails, every spending decision on test runs, the manual voice calls, reviewing the Spanish in the test scenarios as a native speaker, and the judgment calls on results.
- **Claude Code:** most of the code (service, tests, scripts, configs) and probing undocumented behavior with throwaway tests (parameter paths, test-mode tool results, the DPoP `%40` detail).

## Repository layout

```
.github/          CI: pytest on every push; agent tests by hand
agent/            prompt, tool configs, agent config (pushed by scripts/push_agent.py)
app/              FastAPI service: directory, TOTP verifier, Okta push and OAuth client, the three tools
data/             synthetic employee directory
evals/            test plan, test definitions, results
scripts/          push agent, sync and run tests, compare runs, transcript checks, TOTP codes, Okta check
tests/            pytest suite for the server rules, the push flow and the Okta client
```
