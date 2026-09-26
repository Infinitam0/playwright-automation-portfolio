"""Self-check for copy_browser_profile on a fake profile. Run: python shared/test_browser_session.py"""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for shared/
from shared.browser_session import cleanup_profile, copy_browser_profile  # noqa: E402


def test_copy_browser_profile() -> None:
    with tempfile.TemporaryDirectory() as root:
        profile = Path(root, "Default")
        (profile / "Network").mkdir(parents=True)
        (profile / "Local Storage" / "leveldb").mkdir(parents=True)
        Path(root, "Local State").write_text("{}")
        (profile / "Preferences").write_text("{}")
        (profile / "Local Storage" / "leveldb" / "000003.log").write_text("x")
        (profile / "Local Storage" / "leveldb" / "LOCK").write_text("")
        db = sqlite3.connect(profile / "Network" / "Cookies")
        db.execute("CREATE TABLE cookies (name TEXT)")
        db.execute("INSERT INTO cookies VALUES ('session')")
        db.commit()
        db.close()

        temp_dir = copy_browser_profile(root, "Default")
        try:
            copy = Path(temp_dir, "Default")
            assert Path(temp_dir, "Local State").exists()
            assert (copy / "Preferences").exists()
            assert (copy / "Local Storage" / "leveldb" / "000003.log").exists()
            assert not (copy / "Local Storage" / "leveldb" / "LOCK").exists()
            copied_db = sqlite3.connect(copy / "Network" / "Cookies")
            assert copied_db.execute("SELECT name FROM cookies").fetchall() == [("session",)]
            copied_db.close()
        finally:
            cleanup_profile(temp_dir)
        assert not os.path.exists(temp_dir)


if __name__ == "__main__":
    test_copy_browser_profile()
    print("ok")
