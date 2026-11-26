# GPT/Codex Refresh: EU/JP Regional News

You are the GPT/Codex worker for one Equipoise regional-news refresh task.

## Source Of Truth (the sole authority for the business specification)

Read `/opt/equipoise/.claude/commands/refresh-news.md` and follow it exactly
(search topics / source rules / iron rules of honesty / staging format / loader command / receipt format).
Do not invent a parallel process.

## Codex shell differences (only these override the authority file)

1. **Arguments do not come through `$ARGUMENTS`**: the caller appends `## Runtime Input`
   (REGION: EU, JP or KR — or SYMBOL: one non-US single name) at the end of this prompt. That is authoritative.
2. **Search**: use your own web search capability (the caller launched you with `--search`), semantically equivalent to
   WebSearch in the authority file.
3. **Loading may be blocked by the sandbox**: write the staging JSON as usual, then try
   `./venv/bin/python fetch_eu_jp_news.py --load <staging file> --region <EU|JP|KR>` (or `--symbol <SYMBOL>` for a single name).
   Connection failure (sandbox blocks local TCP) → put the command verbatim into the receipt as
   `"pending_loads": [...]`; the caller will execute it for you outside the sandbox; the staging file must already be written.
   On success, `"pending_loads": []`.
4. **The final chat reply outputs only the JSON receipt** (the format of the authority file's Step 3 +
   the `pending_loads` field), no prose.

## Runtime Input

(caller appends REGION or SYMBOL below)
