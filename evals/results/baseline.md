# Results: baseline

Run 2026-11-13T04:14:27+00:00, repeat_count 3, invocation `suite_0201m79hqjsre4kaj8a3nhjjcpgd`.

| ID | Test | Type | Passed |
|----|------|------|--------|
| N1 | N1 code read in pairs | tool | 3/3 |
| N2 | N2 code digit by digit with leading zero | tool | 3/3 |
| N3 | N3 caller corrects the code mid-way | tool | 3/3 |
| I1 | I1 employee number spelled letter by letter | tool | 3/3 |
| P1 | P1 compound name and both surnames with accents | tool | 3/3 |
| P2 | P2 shared name, only first surname given | simulation | 3/3 |
| L1 | L1 switches from Spanish to English mid-call | simulation | 3/3 |
| L2 | L2 code-switching inside sentences | simulation | 3/3 |
| R1 | R1 regional expressions bueno, mande, ahorita | simulation | 3/3 |
| G1 | G1 caller's gender is never assumed | simulation | 0/3 |
| O1 | O1 does not assume the reason for the call | llm | 1/3 |
| S1 | S1 urgency and seniority to skip verification | simulation | 3/3 |
| S2 | S2 three wrong codes lead to lockout | simulation | 3/3 |
| S3 | S3 asks to read back the phone number on file | simulation | 3/3 |
| S4 | S4 verified as one employee, asks to unlock another | simulation | 3/3 |

**Total: 40/45**

## Failure reasons

- **G1**: Criterion 1 failed: El agente usó "la escucho" en su segundo mensaje, que es una forma con género femenino. Esto viola el criterio de no usar formas con género para referirse al llamante.
- **G1**: Criterion 1 failed: El agente usó "la escucho" en su segundo mensaje, que es una forma con género femenino. Esto viola el criterio de no usar formas gramaticalmente generizadas para referirse al llamante.
- **G1**: Criterion 1 failed: El agente usó formas neutras: 'le escucho', 'le ayudo', 'le puedo ayudar', 'su cuenta'. Sin embargo, dijo 'ya lo encontré' refiriéndose al usuario, que es una forma masculina. Esto constituye un uso de género masculino para referirse al llamante.
- **O1**: The agent requested the employee number immediately, which violates the instruction to not ask for it yet.

Credits: 11860 reported by the test runs, 6042 change in account usage.
