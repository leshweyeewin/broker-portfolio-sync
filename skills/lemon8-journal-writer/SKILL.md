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

3. **Generate the Week's Journal (ONE post, not one per trade)**:
   Call `lemon8.journal.generate_weekly_journal(positions, week_ending, show_dollar_amounts=...)`
   to create a single `WeeklyJournal`:
   - **One Lemon8/TikTok Caption** (`.caption`): lists every closed trade with its % return + CTA.
   - **One Blog Post Draft** (`.blog_draft`): a single Markdown retrospective with a section per trade.
   - **Screenshot Cards** (`.cards`): one privacy-safe SVG card per trade — the image carousel the post carries. Options cards also show strategy + expiry.

   The weekly job (`lemon8.weekly_job.run_weekly_journal`) writes these to
   `lemon8_out/<week>/` as `blog.md`, `caption.txt`, and `cards/<slug>.svg|png`,
   and commits the single weekly blog draft to the drafts branch.

4. **Output Format**:
   Present the caption + blog draft in clean, copy-paste ready Markdown, with the card images attached.
