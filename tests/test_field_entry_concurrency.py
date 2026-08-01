import os
import tempfile
import unittest

from sqlalchemy.exc import IntegrityError


class FieldEntryConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls.db_file.close()
        os.environ["FTTH_DATABASE_URL"] = f"sqlite:///{cls.db_file.name}"
        os.environ["SESSION_SECRET"] = "isolated-test-session-secret-at-least-thirty-two-characters-long"
        global main, SessionLocal, RolloutRecord
        import main
        from database import SessionLocal
        from models import RolloutRecord

    @classmethod
    def tearDownClass(cls):
        main.engine.dispose()
        os.unlink(cls.db_file.name)

    def test_counter_and_submission_key_are_unique(self):
        db = SessionLocal()
        db.add(RolloutRecord(record_id="RDP-9", submission_key="existing-submission-key-0001"))
        db.commit()
        db.close()

        record_ids = []
        for _ in range(3):
            db = SessionLocal()
            counter = main.rollout_entry_counter(db)
            record_id = main.allocate_rollout_entry_id(db, counter)
            db.add(RolloutRecord(record_id=record_id, submission_key=f"unique-submission-key-{record_id}"))
            db.commit()
            record_ids.append(record_id)
            db.close()
        self.assertEqual(record_ids, ["RDP-10", "RDP-11", "RDP-12"])

        db = SessionLocal()
        db.add(RolloutRecord(record_id="RDP-13", submission_key="same-submission-key-0001"))
        db.commit()
        db.add(RolloutRecord(record_id="RDP-14", submission_key="same-submission-key-0001"))
        with self.assertRaises(IntegrityError):
            db.commit()
        db.rollback()
        db.close()
