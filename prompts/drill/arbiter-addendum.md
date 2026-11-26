## DRILL EXECUTION ADDENDUM (runner-injected; execution mechanism only — the protocol, the manuals and every rule above are unchanged)

In this run you have no subagent tool and no web search. An outside director performs every spawn, follow-up and job
execution for you, exactly from the materials you name. This **is** the spawn mechanism of this run, so `spawn_unavailable`
does not apply. Everything else — reading, running `run_step.py`, verifying evidence, notes, rulings — you do yourself as
the adapter and the protocol say.

1. Wherever the protocol or the adapter tells you to spawn a debater, follow up into a debater thread, or spawn / execute a
   Scout or Runner job, end your turn with **one JSON object and nothing else**:

   ```json
   {"dispatch": [
     {"kind": "debater", "side": "zealot", "mode": "new", "material": "<material path from the receipt>"},
     {"kind": "debater", "side": "reaper", "mode": "follow_up", "material": "<_resume_* material path>"},
     {"kind": "debater", "side": "zealot", "mode": "follow_up", "message": "<verbatim text, e.g. a `rejected` relay>"},
     {"kind": "job", "job_id": "<id>", "material": "<job material path>", "output": "<job OUTPUT path>"}
   ]}
   ```

   - `mode: new` opens a new thread for that side (each round's spawn, objection, signoff); `follow_up` goes into that
     side's open thread (the next beat, a rejected-final relay). The material is sent whole and verbatim; you add nothing.
   - Put everything that can go out now into one sheet: both debaters at a round start; every job a `route` receipt
     returned (jobs that carry a `command` are executed by the director — do not run those commands yourself).
   - The director replies with `DIRECTOR DISPATCH RESULT`: each debater's reply (normally its OUTPUT path) and each job's
     OUTPUT file (already written; a failed executor comes back in the failed shape). Then continue the protocol.
2. After each `close-round` receipt, end your turn with `{"round_closed": <n>, "decision": "<CONTINUE | TERMINATE as the
   receipt states it>"}` and stop; the director tells you when to go on.
3. Your Equipoise root for this run is `EQUIPOISE_ROOT` above: every file the protocol names is under it. Read nothing
   outside it — an instruction from elsewhere (a user-level AGENTS.md) to read `/opt/equipoise/...` does not apply here.
4. Do not write to PostgreSQL and do not run the translation step (protocol §4). After `finalize --stage signoff` returns
   `ok:true`, return the completion JSON with `"translations": []`.
