import os
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FTTH_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SR_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SESSION_SECRET"] = "isolated-email-test-session-secret-at-least-32-characters"

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from main import movement_notification_recipients
from models import AppUser


class SrAdminEmailNotificationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        AppUser.__table__.create(self.engine)
        self.db = Session(self.engine)
        self.db.add_all(
            [
                AppUser(program="SINGLE_RAN", username="Husseni", name="Husseni", role="Admin", email="hsebtaoui@lict.ly", password_hash="x", status="active"),
                AppUser(program="SINGLE_RAN", username="Hussein", name="Hussein", role="Approval", email="hsebtaoui@lict.ly", password_hash="x", status="active"),
                AppUser(program="SINGLE_RAN", username="OtherAdmin", name="Other Admin", role="Admin", email="other@example.com", password_hash="x", status="active"),
                AppUser(program="SINGLE_RAN", username="HusseniOld", name="Husseni", role="Admin", email="inactive@example.com", password_hash="x", status="inactive"),
                AppUser(program="FTTH", username="Husseni", name="Husseni", role="Admin", email="ftth@example.com", password_hash="x", status="active"),
            ]
        )
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_sr_mr_and_transfer_recipients_include_only_husseni_admin(self):
        recipients = movement_notification_recipients(self.db, "SINGLE_RAN", ["approver@example.com"])
        self.assertEqual(recipients, ["approver@example.com", "hsebtaoui@lict.ly"])

    def test_ftth_recipients_are_unchanged(self):
        recipients = movement_notification_recipients(self.db, "FTTH", ["approver@example.com"])
        self.assertEqual(recipients, ["approver@example.com"])

    def test_husseni_is_not_added_if_no_longer_an_active_sr_admin(self):
        user = self.db.query(AppUser).filter_by(program="SINGLE_RAN", username="Husseni").one()
        user.role = "Requester"
        self.db.commit()

        recipients = movement_notification_recipients(self.db, "SINGLE_RAN", ["approver@example.com"])
        self.assertEqual(recipients, ["approver@example.com"])


if __name__ == "__main__":
    unittest.main()
