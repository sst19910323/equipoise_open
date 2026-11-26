# Codex Evidence Officer Temporary Spawn Smoke Test

You are the parent Codex agent for a capability-only Equipoise smoke test.

Do not modify files, fetch market data, connect to PostgreSQL, or make
investment claims. Perform exactly one small web search inside the spawned
subagent: find the official OpenAI Codex documentation landing page and return
its page title and URL. Do not let the parent perform or repair the search.

Act as the evidence officer. Temporarily spawn exactly one topologically
independent subagent named `evidence_search` for this single search task.
Ask it to inspect its actually visible tools and return a compact JSON object:

- `role`: must be `evidence_search`
- `topology_independent`: whether it is a real spawned subagent
- `can_use_web`: whether its visible tools permit web/search work
- `can_spawn_agents`: whether it can create another agent
- `search_completed`: whether the subagent itself found the requested page
- `sources`: URLs returned by the subagent; require at least one official
  `openai.com` source when `search_completed=true`
- `production_boundary`: explain that it receives neutral queries from the
  parent and returns source-backed evidence; it never takes a market stance
- `sample_query`: the neutral verification query it executed

If spawning is unavailable, do not simulate the result. Return
`spawn_available=false` and explain why. The parent must return only JSON
matching the caller-provided schema.
