# Implementation Review Process

The standard flow is `plan review -> implementation -> code review -> fixes -> final review`.

The process assumes one `worker` agent and two or more `reviewer` agents.

1. The user agrees on the problem definition and implementation direction with the `worker`. Alternatively, the user may discuss with a proxy first and then dispatch the agreed direction to the `worker`.
2. The `worker` writes a detailed implementation plan.
3. The `worker` requests review from all reviewers via a single **notice** that names the `primary reviewer`.
4. Reviewers begin reviewing immediately upon receiving the notice. They must not reply individually to the `worker`. (Watch for non-primary reviewers waiting for a request from the primary instead of starting their independent review right away — that is incorrect.)
5. The `primary reviewer` collects input from secondary reviewers via a **request** to each.
6. Secondary reviewers reply normally to the primary's request.
7. The `primary reviewer` consolidates the input and delivers a single review result to the `worker` as a **notice**.
8. The `worker` incorporates the review and moves to the next stage.

This procedure repeats for each stage:

- `plan review`: pre-implementation plan inspection.
- `code review`: post-implementation code inspection.
- `final review`: final inspection after code-review fixes are applied.

Termination criteria after the final review:

- If the remaining changes are minor (typos, comments, documentation wording, test names), terminate.
- If behavior, control flow, state management, concurrency, error handling, or test meaning changed, repeat from the `code review` stage.

## Messaging Rules

- `worker -> reviewer`: always **notice**.
  - Reviewers must collaborate first, so a `request` is not used.
- `primary reviewer -> secondary reviewer`: **request**.
  - A reply is required to collect input.
- `secondary reviewer -> primary reviewer`: a normal reply to the primary's request.
  - Do not send a separate message via `agent_send_peer`.
- `primary reviewer -> worker`: **notice**.
  - The purpose is to deliver the consolidated result; no immediate reply routing is required.

Every review-request notice must include:

- The review stage: `plan review`, `code review`, or `final review`.
- The primary reviewer's name.
- The file set or diff scope under review.
- The expected output format.
- The instruction: "Reviewers must inspect first; the primary collects secondary input via request, then delivers only the consolidated result to the worker as a notice."
- An emphasis to review critically and thoroughly.

## Safety Rule

After sending a review-request notice, the `worker` must not proceed to the next stage until the `primary reviewer`'s consolidated notice arrives.

If needed, the `worker` sets a safety wake:

```sh
agent_alarm 600 --note 'plan review follow-up if no primary reviewer summary arrives'
```
