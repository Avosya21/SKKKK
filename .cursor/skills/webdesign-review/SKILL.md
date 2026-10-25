---
name: webdesign-review
description: Reviews or improves PrimaryAnalitics web UI using practical frontend-design guidance. Use when working on layout, visual hierarchy, UX polish, dashboard composition, project pages, report readability, or embedded chat UX.
---

# Webdesign Review

Use this as the umbrella frontend design skill for PrimaryAnalitics pages.

## Focus

- Improve information hierarchy before adding more UI chrome.
- Keep the upload -> process -> review -> ask flow visible on one page when possible.
- Prefer readable cards, clear spacing, and obvious section titles.
- Reduce friction for high-value actions: upload, open report, rebuild summary, ask question.

## Apply with these supporting skills

- `ui-design` for layout and hierarchy
- `responsive-design` for narrow screens
- `accessibility` for labels, contrast, semantics
- `agent-ui-design` for chat and AI answer presentation

## PrimaryAnalitics defaults

- Stay inside Jinja + Bootstrap + vanilla JS unless the user asks for a framework change.
- Long AI outputs should be rendered as readable HTML, not raw blobs.
- Reports and chat should feel like part of the same workspace, not separate tools.
