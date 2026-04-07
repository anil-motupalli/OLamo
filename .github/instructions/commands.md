# Commands

## Running the server

```bash
python main.py --server           # starts on http://0.0.0.0:8000
python main.py --server --port 9000
python main.py --server --settings path/to/olamo-settings.json
```

## Running the CLI

```bash
python main.py "Add a reverse_string function"
python main.py --headless "Add a reverse_string function"   # dry-run, no real API calls
python main.py --pr-url https://github.com/org/repo/pull/42 "Fix CI failures"
```

## Tests

```bash
# Full suite (excludes engine integration tests and copilot tests)
pytest tests/ -q --ignore=tests/test_engines.py -k "not test_copilot_engine"

# Single test
./venv/bin/pytest test_main.py::TestParseStageAnnouncement::test_parses_stage_1 -v

# All tests including engines (requires real API keys)
pytest tests/ test_main.py -q
```

## Installing dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`codex-app-server-sdk` may need to be installed from source — see the comment in `requirements.txt`.
