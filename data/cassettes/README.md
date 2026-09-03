# Cassettes — recorded agent decisions

One JSON file per case, `<case_id>.json`, holding the ordered LLM turns the agent arm
took while planning that case:

```json
{
  "case_id": "case-00042",
  "turns": [
    { "key": "<sha256 of the canonical request>", "response": { "...": "the assistant message" } }
  ]
}
```

**Replay is the default.** `python -m recovery.cli eval --arms control,baseline,agent`
reads these files and never touches the network, so the published number reproduces
exactly, offline, with `ANTHROPIC_API_KEY` unset. That is the point of committing them.

A key is a SHA-256 over the canonical request — model, tools, system prompt and the
whole message history, serialised with sorted keys. Because the history includes every
previous turn's tool results, the format self-validates: if a tool returns something
different on replay than it did when recorded, the next turn's key does not match and
the run **raises**. Replay cannot silently drift into a different conversation.

## Recording

```bash
python -m recovery.cli eval --seed 42 --n 50  --arms agent --record   # confirm the loop first
python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline,agent --record
```

`--record` needs `ANTHROPIC_API_KEY`; replay does not. `--live` bypasses cassettes
entirely and records nothing.

Re-record whenever the system prompt, a tool signature or docstring, or a tool's return
value changes — all three are inside the key, so all three invalidate every cassette.
A replay run that hits a stale cassette fails loudly and tells you this.
