from services import inbox_notify
from repositories import inbox_repo


async def test_notify_writes_and_returns():
    msg = await inbox_notify.notify_user("nn-1", "selection_completed", "T", "C",
                                         related_type="select_jobs", related_id="j1")
    assert msg["user_id"] == "nn-1"
    assert msg["read_status"] is True
    row = await inbox_repo.get(msg["id"])
    assert row["related_id"] == "j1" and row["read_at"] is None
