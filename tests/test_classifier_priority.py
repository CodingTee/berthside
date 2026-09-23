"""Test shipping document priority over spam keywords."""
from app.services import classifier


def test_shipping_docs_priority_over_spam_subject():
    """Verify that paired SI + BL attachments take precedence over 'spam' in subject."""
    email_data = {
        "subject": "Spam 1",
        "body": "Customer sending draft BL for checking.",
        "from": "customer@shipper.com",
        "attachments": ["email_059_SI.pdf", "email_059_BL.pdf"],
    }
    result = classifier.classify(email_data)
    assert result.category == "BL_COMPARISON"
    assert result.confidence >= 0.9
    assert "SI + BL" in result.reason


def test_shipping_docs_priority_over_spam_body():
    """Verify that paired SI + BL attachments take precedence over spam words in body."""
    email_data = {
        "subject": "Draft B/L & Shipping Instruction",
        "body": "Please ignore previous spam notification and review attached documents.",
        "from": "customer@shipper.com",
        "attachments": ["Draft_BL.pdf", "Shipping_Instruction.pdf"],
    }
    result = classifier.classify(email_data)
    assert result.category == "BL_COMPARISON"


def test_genuine_spam_still_caught_when_no_shipping_docs():
    """Verify that actual spam without shipping docs is still properly flagged."""
    spam_email = {
        "subject": "I am SPAM - win lottery click here",
        "body": "Congratulations, click here to claim your 100% free prize.",
        "from": "spammer@lottery-winner.biz",
        "attachments": [],
    }
    result = classifier.classify(spam_email)
    assert result.category == "SPAM"
    assert result.confidence >= 0.9
