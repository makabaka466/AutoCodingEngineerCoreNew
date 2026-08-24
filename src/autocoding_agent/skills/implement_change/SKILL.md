# Implement an approved change

Use this method only in implement mode.

1. Reuse the investigation and the user's approved intent.
2. Treat the approved proposal as the modification boundary. Make the smallest coherent change that
   fully handles that exact plan.
3. Preserve established project conventions and unrelated user work.
4. Add comments only where they explain intent, boundaries, or non-obvious behavior.
5. Report the exact workspace-relative files actually changed.
6. When executable validation is warranted, request verify approval rather than claiming it ran.

If new evidence requires a material change to the approved design, additional files, or broader
impact, stop before expanding the edit and return a new `approval_required` modify proposal for the
user to review.

Do not broaden the product scope, perform cleanup unrelated to the task, or write outside the bound
workspace.
