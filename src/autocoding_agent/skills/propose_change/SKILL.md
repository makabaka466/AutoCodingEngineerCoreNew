# Present a change proposal before implementation

Use this method after the requirement is clear and the relevant code has been inspected, but before
requesting permission to edit files.

1. Explain the evidence-backed current state and the problem being solved.
2. Describe each concrete change with its affected file or area, what exists now, and what it will
   become. Set `path` whenever a concrete workspace file is known; use a null path only for a
   genuinely non-file area. Separate confirmed facts from design choices.
3. State the expected user-visible or technical result, important impact, and how the result should
   be validated.
4. Provide a useful preview whenever the task supports one without making edits:
   - UI: a compact text wireframe, interaction sequence, or before/after layout;
   - API or data: example request, response, schema, or transformation;
   - code or configuration: focused pseudocode, proposed signature, or representative target snippet;
   - behavior: a before/after scenario.
5. Return `approval_required` with scope `modify` and populate `approval.proposal` with the same plan.
   The user-facing message must summarize the proposal before asking for approval.

The preview is optional only when a truthful preview is not useful until implementation. Say so
briefly instead of inventing a visual, exact diff, file, or behavior. Never edit during the proposal
turn, and never treat a proposed design as if it had already been implemented or tested.
