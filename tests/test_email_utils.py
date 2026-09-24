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
        "To: demo.operator@example.com\n\n"
        "Please find documents attached."
    )
    sender = extract_original_sender(body)
    assert sender == "alice@shipper-global.com"


def test_build_enterprise_audit_receipt_always_renders_buttons_even_if_same_sender():
    from unittest.mock import MagicMock
    from app.services.email_utils import build_enterprise_audit_receipt

    rep = MagicMock()
    rep.status = "OK"
    rep.review_reason = None
    rep.extracted = {
        "si": {"booking_number": "BKG-7788", "vessel": "EVER GLOBE", "voyage": "012W", "gross_weight_kg": 24000.5, "container_count": 2},
        "bl": {"booking_number": "BKG-7788", "vessel": "EVER GLOBE", "voyage": "012W", "gross_weight_kg": 24000.5, "container_count": 2},
    }
    rep.field_results = [
        {"field": "booking_number", "si_value": "BKG-7788", "bl_value": "BKG-7788", "match": True},
        {"field": "gross_weight", "si_value": "24000.5 KGS", "bl_value": "24000.5 KGS", "match": True},
    ]

    # Case: orig_client == sender_email (user's exact reported bug)
    text_body, html_body = build_enterprise_audit_receipt(
        clean_subj="Shipment BKG-7788",
        stage_id="STG-TEST-001",
        status_desc="Verified with 0 discrepancies (Verdict: OK)",
        rep=rep,
        orig_client="demo.operator@example.com",
        sender_email="demo.operator@example.com",
        target_client="demo.operator@example.com",
        client_subj="Re: Shipment BKG-7788 - Document Verification Complete",
        client_reply_text="Dear Customer,\nVerification passed.",
        mailto_link="mailto:demo.operator@example.com?subject=Test",
        gmail_thread_search="https://mail.google.com/mail/u/0/#search/from%3Ademo.operator%40example.com",
        source_mailbox="averis.demo@gmail.com",
    )

    # 1. Action Dock and buttons MUST be present
    assert "One-Click Reply to Client" in html_body
    assert "Locate Exact Thread in Gmail" in html_body
    assert "mailto:demo.operator@example.com?subject=Test" in html_body
    assert "Official Customer Notice Card" in html_body or "Shipping Document Verification Notice" in html_body

    # 2. Executive layout elements MUST be present
    assert "Shipping Document Verification Receipt" in html_body
    assert "VERIFIED WITH 0 DISCREPANCIES" in html_body
    assert "EVER GLOBE / 012W" in html_body
    assert "24,000.5 KGS" in html_body
    assert "2 Unit(s)" in html_body
    assert "Field-by-Field Counterpart Reconciliation Ledger" in html_body
    assert "✅ MATCH" in html_body

    # 3. Plain text alternative MUST be complete
    assert "FEDERATED SHIPPING DOCUMENTATION GATEWAY" in text_body
    assert "SHIPMENT OVERVIEW" in text_body
    assert "FIELD-BY-FIELD AUDIT LEDGER" in text_body
    assert "Fast Client Reply Gateway" in text_body


def test_build_customer_structured_text_and_html_notice():
    from unittest.mock import MagicMock
    from app.services.email_utils import build_customer_structured_text, build_customer_html_notice

    rep = MagicMock()
    rep.status = "MISMATCH"
    rep.review_reason = None
    rep.extracted = {
        "si": {"booking_number": "BKG-9933", "vessel": "COSCO SHIPPING", "voyage": "008E", "gross_weight_kg": 15000.0, "container_count": 3},
        "bl": {"booking_number": "BKG-9933", "vessel": "COSCO SHIPPING", "voyage": "008E", "gross_weight_kg": 15250.0, "container_count": 3},
    }
    rep.field_results = [
        {"field": "booking_number", "si_value": "BKG-9933", "bl_value": "BKG-9933", "match": True},
        {"field": "gross_weight", "si_value": "15000.0 KGS", "bl_value": "15250.0 KGS", "match": False},
    ]

    # Test Customer Structured Text
    cust_text = build_customer_structured_text(
        clean_subj="Draft BL BKG-9933",
        stage_id="STG-TEST-888",
        status_desc="Discrepancies identified (gross weight mismatch)",
        rep=rep,
        target_client="shipper@client.com",
        source_mailbox="averis.demo@gmail.com",
    )
    assert "FEDERATED SHIPPING DOCUMENTATION AUDIT NOTICE" in cust_text
    assert "1. SHIPMENT OVERVIEW" in cust_text
    assert "BKG-9933" in cust_text
    assert "COSCO SHIPPING / 008E" in cust_text
    assert "2. FIELD-BY-FIELD RECONCILIATION BREAKDOWN" in cust_text
    assert "[MISMATCH (!)]" in cust_text
    assert "3. NEXT STEPS & INSTRUCTIONS" in cust_text
    assert "ACTION REQUIRED" in cust_text

    # Test Customer HTML Notice
    cust_html = build_customer_html_notice(
        clean_subj="Draft BL BKG-9933",
        stage_id="STG-TEST-888",
        status_desc="Discrepancies identified (gross weight mismatch)",
        rep=rep,
        target_client="shipper@client.com",
        source_mailbox="averis.demo@gmail.com",
    )
    assert "Shipping Document Verification Notice" in cust_html
    assert "AMENDMENT REQUIRED" in cust_html
    assert "BKG-9933" in cust_html
    assert "COSCO SHIPPING / 008E" in cust_html
    assert "Document Counterpart Reconciliation Ledger" in cust_html
    assert "❌ MISMATCH" in cust_html
    assert "Action Required for Cargo Release" in cust_html


