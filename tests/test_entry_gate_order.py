"""При закрытых входах бот не тратит лимит API на подтверждение сигналов, по которым всё равно не войдёт."""
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")


def test_entries_closed_is_checked_before_api_confirmation():
    body = SRC[SRC.index("def process_instrument"):]
    closed = body.index('skip_filter="entries_closed"')
    api = body.index("get_api_indicators(client")
    assert closed < api
