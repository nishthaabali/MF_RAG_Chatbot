# Chatbot Issues

## 1. Fund Manager Information Is Inconsistent

**Issue:** Fund manager information is not displayed consistently across
schemes.

**Expected:** Show all available fund managers consistently for each
scheme.

## 2. Fund Manager Education and Experience Not Answered

**Issue:** The chatbot does not return fund managers' education and
experience details when asked.

**Expected:** Retrieve and present the available education and
professional experience information for the relevant fund manager.

## 3. Missing Chat Management Features

**Issue:** The app does not currently support: - Pinning a chat -
Renaming a chat - Exporting a chat in Markdown (`.md`) format

**Expected:** Add these chat management actions so users can organise
conversations and export them for later use.

## 4. Missing Collapsible Sidebar

**Issue:** The left-hand sidebar cannot be collapsed.

**Expected:** Add a control to collapse and expand the sidebar, giving
users more space for the chat when needed.

---

## Resolution (2026-11-10)

All four issues are fixed. Tests: 880 pass (34 new in `test_fund_managers.py`, `test_api.py` and
`test_frontend.py`), and a `--no-llm` eval run still meets every gate
(`eval/results/2026-11-10-nollm-2.json`).

**1. Inconsistent fund manager information.** Causes:
- Phrasings such as "who runs / is managing / handles X" matched no field keyword. They fell
  back to text search, which has one chunk per manager and keeps only the top 5, so the list
  could come back partial.
- "portfolio manager" also triggered a holdings lookup.
- The validator only checked the first 2 names when a scheme has more than 3 managers.

Fixes:
- The resolver now recognises these phrasings.
- Every scheme answer lists all managers in the published order, each with their "managing
  since" date. This holds on both the model path (new generator rule 9) and the template path.
- The validator now requires every name.

**2. Education and experience not answered.** Causes:
- The structured evidence held only names and dates. Education and experience were in separate
  per-manager chunks, and an answer may cite only one evidence block, so these answers failed
  validation and fell back to names only.
- A question naming only the manager didn't resolve to any scheme.

Fixes:
- Words such as "education", "qualification", "experience", "career", "background" and
  "profile" add education and/or experience to the fund-manager evidence block, taken from
  the `fund_managers` table.
- A covered manager's full name, or a surname only one manager has (in a question about
  managers), resolves to a scheme they manage, preferring the active one. The answer is then
  narrowed to that manager and worded so it doesn't read as the whole team.
- The template fallback quotes the published text within 3 sentences.
- The sentence splitter no longer breaks at "Pvt. Ltd.".

**3. Chat management.** A ⋯ menu on each chat in the sidebar (also reachable from the keyboard)
and buttons in the chat header offer Pin, Rename and Export.
- **Pin:** pinned chats are listed in a "Pinned" group at the top. Pins are kept in the browser.
- **Rename:** edit the title in place. Titles are stored on the server via
  `PATCH /api/threads/{id}`, so a rename survives a reload. Titles go through the PII guard,
  and a refused title is never echoed or logged.
- **Export:** downloads a `.md` file with the title, the disclaimer, each question, and each
  answer with its source and date.

**4. Collapsible sidebar.** A control at the top of the sidebar, or Ctrl+B, collapses it to a
64 px rail with the expand and "New chat" buttons. The choice is remembered. On phones the
sidebar remains a drawer.
