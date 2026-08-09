---
name: lemon8-journal-writer
description: Generate privacy-respecting Lemon8 / social media trade journal copy and blog post drafts from portfolio sync closed positions.
---

# Lemon8 Journal Writer Skill

This skill reads closed stock and option positions from the portfolio sync Google Sheet (using `lemon8.reader`) and generates copy-paste social media teaser packages and blog post drafts (using `lemon8.journal`).

## Usage Guidelines

1. **Read Closed Positions**:
   Use `lemon8.reader.read_closed_positions(client)` to fetch all closed trades.

2. **Privacy Rules (Load-Bearing)**:
   - **Default: Hide absolute dollar amounts.** Show percentages (%) and trade thesis/reasoning ONLY.
   - Do NOT show portfolio size, net worth, or absolute P/L amounts in generated copy unless the user explicitly requests it via `show_dollar_amounts=True`.
   - Never default `show_dollar_amounts` to `True`.

3. **Generate Packages**:
   Call `lemon8.journal.generate_journal_package(pos, reasoning, show_dollar_amounts=...)` to create:
   - **Lemon8/TikTok Caption**: Emojis, ticker, % return, trade reasoning, CTA.
   - **Blog Post Draft**: Markdown article template for retrospective review.
   - **Card Summary & SVG**: Text and graphic card formats for image attachment.

4. **Output Format**:
   Present the generated package to the user in a clean, copy-paste ready Markdown format.
