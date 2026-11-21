# agentd

A small job runner configured by a JSON file.

    python -m agentd.cli --config examples/agentd.json

Options: `timeout` (seconds per attempt), `retries` (extra attempts after a timeout),
`workers`, `log_level`. Unknown options are rejected.

Tests: `python -m unittest discover -s tests`
