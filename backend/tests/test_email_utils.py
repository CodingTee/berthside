from app.services.email_utils import clean_subject, extract_original_sender


def test_clean_subject_removes_cascaded_fwd_and_re():
    # User's actual reported case
    raw1 = "Fwd: Fwd: I am SPAM - Document Verification Complete - Ref #STG-IMAP-165441266"
    assert clean_subject(raw1) == "I am SPAM"

    raw2 = "Fwd: I am SPAM"
    assert clean_subject(raw2) == "I am SPAM"

    raw3 = "Re: Fwd: Booking BKG-8899 - B/L Document Amendment Required (Rejected) - Ref #STG-IMAP-123"
    assert clean_subject(raw3) == "Booking BKG-8899"

    raw4 = "FW: [EXTERNAL] Re: 转发: Urgent Shipment 042E"
    assert clean_subject(raw4) == "[EXTERNAL] Re: 转发: Urgent Shipment 042E" or clean_subject(raw4) == "Urgent Shipment 042E"

    raw5 = "Just A Clean Subject"
    assert clean_subject(raw5) == "Just A Clean Subject"


def test_extract_original_sender():
    body = (
        "FYI please verify.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Alice Shipper <alice@shipper-global.com>\n"
        "Date: Mon, Sep 21, 2026 at 2:00 PM\n"
        "Subject: BKG-9921 Documents\n"
        "To: testuse1491@gmail.com\n\n"
        "Please find documents attached."
    )
    sender = extract_original_sender(body)
    assert sender == "alice@shipper-global.com"
