# Mande: a bilingual IT helpdesk voice agent, and the tests that found what broke

A phone agent for an IT helpdesk that unlocks a locked work account, in Mexican Spanish or English, built on ElevenLabs Agents. It never unlocks because a caller sounds right: the caller reads a 6-digit code from their authenticator app, and a small FastAPI service checks it and decides. Around the agent sits a regression suite of 15 tests written for how Mexican callers actually talk (codes read in pairs, "¿Bueno?", "mande", Spanglish, compound surnames) plus security cases. Each test runs several times and is reported as a pass rate. A first manual call and the suite turned up three real problems; two were fixed and measured, one is still open.

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

## What broke and what changed

- **The agent guessed the caller's gender.** Spanish makes you pick "la escucho" or "lo escucho" when you say "I can hear you". The agent said "Sí, la escucho bien" to callers who never said who they were, in 5 of 6 first replies. This showed up in the very first manual call, before any test existed, so it became test G1. Fix: the prompt says the caller's gender is unknown and asks for the neutral "le escucho", "le espero". G1 went from 0/3 to 3/3.
- **The agent assumed why you were calling.** When a caller only checked the line ("Bueno, sí, ¿me escucha?"), the agent jumped to "¿Me puede dar su número de empleado?". Fix: a new first step asks how it can help, and asks for the employee number only after the caller says the account is locked. O1 went from 1/3 to 3/3.
- **Still open: the agent sometimes hangs up in silence after a lockout.** After the third wrong code it can end the call without telling the caller anything (1 of 3 runs before, 2 of 3 after; the fixes above do not touch this path). The LLM judge passed two of these three runs, which is why there is a separate transcript check. A related case, hanging up on an unexpected tool result, was found while building the tests and fixed before the baseline.

The number tests all passed from the start. The surprises were about manners and grammar, not digits.

## How it works

```mermaid
flowchart LR
    caller[Caller<br/>es-MX or English] --> agent[ElevenLabs agent<br/>bilingual prompt<br/>3 webhook tools]
    agent -- tool calls --> api[FastAPI service<br/>lookup_user<br/>verify_identity<br/>unlock_account]
    api --> dir[(Mock directory<br/>18 synthetic employees)]
    api --> totp[Verifier<br/>TOTP via pyotp]
    runner[Test runner<br/>run_tests, repeat_count] --> agent
```

The agent talks; the service decides. Every rule that matters is enforced on the server, so a caller who talks the agent into skipping a step still cannot unlock anything:

| Tool | Takes | Returns | Server rule |
|------|-------|---------|-------------|
| `lookup_user` | employee number, or first name and surnames | internal `user_id`, last 2 phone digits, account status | Never returns a name, a full phone number or other personal data. Ambiguous names return "ambiguous", not a list. |
| `verify_identity` | `user_id`, 6-digit code | verified or not, attempts left | 3 wrong codes lock verification for 15 minutes. A used code cannot be replayed. |
| `unlock_account` | `user_id` | unlocked or not | Needs a 5-minute, single-use token from a successful verification of that same `user_id`. Disabled accounts never unlock. |

The verification token never reaches the LLM. `verify_identity` returns it, ElevenLabs stores it in a `secret__` dynamic variable and strips it from what the model sees, and `unlock_account` sends it back as a request header. The model only knows that verification passed. All three tools also send a shared secret header, so the public tunnel URL alone is not enough to call them.

The agent only unlocks accounts. It never resets passwords or changes the phone or authenticator on file.

## How the tests work

Test definitions are in [`evals/cases.py`](evals/cases.py) and the plan behind them in [`evals/test_plan.md`](evals/test_plan.md). Three kinds:

- **Tool Call** (N1 to P1): a fixed conversation, then an exact or regex check on the parameters the agent sends. No LLM judge.
- **Next reply** (O1): a fixed conversation, then an LLM judge reads the agent's next message.
- **Simulation** (the rest): an LLM plays the caller from a scenario, and an LLM judge checks the whole call against written conditions. All three tools are mocked, so every run starts from the same state. The server's own rules are covered separately by 43 `pytest` tests.

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

`data/directory.json` is synthetic and safe to commit. TOTP secrets are not stored in it; they are derived from `TOTP_MASTER_KEY` at runtime. `scripts/generate_directory.py` rebuilds it.

## Limitations

- **The tests run on text, not audio.** Speech-to-text mistakes on names, accents or numbers over a bad line are not measured. Voice was checked with manual calls only.
- **Small samples.** 3 runs per test. 3/3 means it did not fail in three tries, not that it never fails.
- **The judge is an LLM and it makes mistakes.** It missed two of three silent hang-ups, and in one baseline run it read "ya lo encontré" ("found it", or "found you") as gendered. Where a check can be a script, it should be.
- **Simulated callers are LLMs following a script.** Real callers ramble, interrupt and hang up.
- **TOTP read aloud can be phished.** An attacker on another call could talk someone into reading their code. Voice is never treated as proof of identity, but a spoken code is a weak factor; a push with number matching is stronger, and phishing-resistant factors stronger still.
- **This is a demo.** In-memory state, a temporary tunnel, a mock directory, no rate limiting on lookups, no audit log.
- **Results are for one setup:** agent on `claude-sonnet-5-5` at temperature 0, simulations played and judged by `claude-sonnet-4-6`. A different model needs its own baseline.

## Built with AI assistance

> Draft for Carlos to review and edit.

This project was built with Claude Code (Anthropic's coding agent) working in the repo, with me directing. The split:

- **Me:** the plan and scope (unlock only, server-side enforcement, synthetic data, the test categories), the guardrails, every spending decision on test runs, the manual voice calls, reviewing the Spanish in the test scenarios as a native speaker, and the judgment calls on results. The gender bug came from me noticing "la escucho" on the first call.
- **Claude Code:** most of the code (service, tests, scripts, configs), reading the ElevenLabs docs and SDK, and probing undocumented behavior with throwaway tests (parameter paths, test-mode tool results).
- **How we worked:** a written plan and handover doc first; small steps, each with a done check; every number in this README comes from a saved run, not from a summary. Commits are not individually marked as co-authored; this section covers the whole project instead.

## Repository layout

```
agent/            prompt, tool configs, agent config (pushed by scripts/push_agent.py)
app/              FastAPI service: directory, TOTP verifier, the three tools
data/             synthetic employee directory
evals/            test plan, test definitions, results
scripts/          push agent, sync and run tests, compare runs, transcript checks, TOTP codes
tests/            pytest suite for the server rules
```
