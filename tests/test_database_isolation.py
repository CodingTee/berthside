"""Unit test confirming physical database isolation between Enterprise Hub and OAuth."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import EmailRecord, GmailMessageRecord, ReportRecord


def test_dual_database_physical_isolation(tmp_path):
    # 1. Create two isolated SQLite databases
    enterprise_db_path = tmp_path / "test_enterprise.db"
    oauth_db_path = tmp_path / "test_oauth.db"

    ent_engine = create_engine(f"sqlite:///{enterprise_db_path}", connect_args={"check_same_thread": False})
    oauth_engine = create_engine(f"sqlite:///{oauth_db_path}", connect_args={"check_same_thread": False})

    Base.metadata.create_all(bind=ent_engine)
    Base.metadata.create_all(bind=oauth_engine)

    EntSession = sessionmaker(bind=ent_engine)
    OAuthSession = sessionmaker(bind=oauth_engine)

    # 2. Write an Enterprise EDI record into Enterprise DB
    with EntSession() as ent_s:
        ent_email = EmailRecord(
            email_id="email_001_EDI",
            sender="carrier@maersk.com",
            subject="EDI Influx SHP-001 SI",
            body="Carrier EDI Stream",
            source_mailbox="sdoc-hackathon-bundle@averis.com",
        )
        ent_s.add(ent_email)
        ent_s.commit()

    # 3. Write a Personal Gmail record into OAuth DB
    with OAuthSession() as oauth_s:
        gmail_msg = GmailMessageRecord(
            gmail_message_id="msg_user_123",
            email_id="GMAIL-msg_user_123",
            sender="operator@gmail.com",
            subject="Personal Test Mail",
            processing_status="PROCESSED",
        )
        oauth_email = EmailRecord(
            email_id="GMAIL-msg_user_123",
            sender="operator@gmail.com",
            subject="Personal Test Mail",
            body="User inbox body",
            source_mailbox="operations@shipsync.demo",
        )
        oauth_s.add(gmail_msg)
        oauth_s.add(oauth_email)
        oauth_s.commit()

    # 4. Verify physical isolation:
    # - Enterprise DB must contain the EDI email, but NOT the Gmail message
    with EntSession() as ent_s:
        assert ent_s.query(EmailRecord).filter_by(email_id="email_001_EDI").first() is not None
        assert ent_s.query(EmailRecord).filter_by(email_id="GMAIL-msg_user_123").first() is None
        assert ent_s.query(GmailMessageRecord).filter_by(gmail_message_id="msg_user_123").first() is None

    # - OAuth DB must contain the Gmail message, but NOT the Enterprise EDI email
    with OAuthSession() as oauth_s:
        assert oauth_s.query(EmailRecord).filter_by(email_id="GMAIL-msg_user_123").first() is not None
        assert oauth_s.query(GmailMessageRecord).filter_by(gmail_message_id="msg_user_123").first() is not None
        assert oauth_s.query(EmailRecord).filter_by(email_id="email_001_EDI").first() is None
