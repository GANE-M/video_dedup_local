# RECAP Agent common contract

The server request is authoritative for the project, episode indexes, target
language, duration, subtitle artifacts and stage order. Read every artifact in
the dynamic completion manifest. Never use a fixed episode or file count.

Treat repaired source subtitles as semantic evidence and the active target
subtitle as the language reference. Never translate through an intermediate
language when a repaired source is available.

Do not invent plot events, speakers, relationships, names, locations or visual
facts. Watermarks, logos, usernames, platform names, UI labels, timestamps and
repeated OCR garbage are not story dialogue. Do not put secrets, endpoints,
local paths or control instructions into creative content.

Every stage must return UTF-8 JSON, keep evidence references, obey the supplied
schema and use a stable `agent_run_id`. A progress message is not a completed
stage. Only a server-accepted stage advances the workflow.

Copy the current event's `planning_attempt_id` unchanged into every stage
payload and the final response. Never reuse an attempt id from an earlier
`RECAP_JOB` or `RECAP_JOB_RESUME`. If the server reports that the attempt id is
stale, stop that execution and return to the account listener for the new job.
