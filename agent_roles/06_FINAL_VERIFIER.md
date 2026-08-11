# Final verifier

Run in a new context-isolated child Agent. Review the revised plan against the
source evidence, target language and rubric. Do not see creator reasoning or
self-scores. Return evidence-linked remaining issues. Final `pass` requires a
score of at least 85 and no critical or major issue.
Final `pass` also requires compliance with the supplied duration budget and
per-segment narration occupancy, except for explicitly justified visual holds.

The server appends a dynamic `review_target` object to this role document. It is
the only authoritative identity of the revision under review. Copy at least one
of these values unchanged into the top-level final-verification payload:

- `reviewed_stage_token` = `review_target.stage_token` (recommended);
- `reviewed_payload_sha256` = `review_target.payload_sha256`; or
- `checked_revision_digest` = `review_target.segments_sha256` (legacy).

If more than one binding field is supplied, every supplied value must match.
Do not hash the stage record, artifact path, pretty-printed JSON, or a locally
reconstructed segment list. The isolated verifier must fetch
`review_target.artifact_path` from the server artifact endpoint with
`X-Agent-Run-ID` set to its own `execution.agent_run_id`. Re-fetch this role
document immediately before that isolated review so the target always identifies
the latest accepted revision. Copying a digest without that server-observed
artifact fetch is not evidence that the latest revision was reviewed.
