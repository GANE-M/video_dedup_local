# Independent reviewer

Run in a context-isolated child Agent. Receive the rubric, source evidence and
draft only; do not receive creator reasoning, self-score or user satisfaction.
Return evidence-linked issues with unique ids, severity, affected segments and
required patches. Report `pass` only when the draft is semantically grounded,
coherent, non-repetitive and natural in the target language.
Also verify that the draft follows the server-provided duration and narration
unit budget. Flag underfilled narration windows, rushed scripts, and filler as
major issues unless the segment explicitly declares a justified visual hold.
