"""Shipment grouping: reference from the document (J1), no false merges (J2).

Grouping authority, in order: a reference the mail states → a reference the
document itself prints → a field signature (shipper + POL + POD, plus container
number or vessel/voyage when the documents carry them) → the email id.
"""
from __future__ import annotations

from app.services import identifiers, versioning

ROUTE = {
    "shipper": "ABC LOGISTICS SDN BHD",
    "port_of_loading": "PORT KLANG, MALAYSIA (MYPKG)",
    "port_of_discharge": "ROTTERDAM, NETHERLANDS (NLRTM)",
}


def _mail(**overrides) -> dict:
    email = {"subject": "documents attached", "body": "see the file",
             "email_id": "mail-1", "attachments": ["scan.pdf"]}
    email.update(overrides)
    return email


# --------------------------------------------------------------------- J1
def test_a_reference_that_exists_only_inside_the_document_is_used():
    document = identifiers.from_text("Booking No: ABC123\nShipper: X\n")
    key, reference = versioning.identify_shipment(
        _mail(subject="documents attached", body="see the file"),
        {}, {}, document)
    assert key == "REF:ABC123"
    assert reference == "ABC123"


def test_the_internal_email_id_is_never_a_business_reference():
    """It is our identifier, not the customer's.

    `doc-only` once became shipment code `REF:DOC-ONLY` because the email id was
    part of the reference search.
    """
    key, _ = versioning.identify_shipment(
        _mail(email_id="doc-only"), {}, {}, identifiers.DocumentIdentifiers())
    assert key == "EMAIL:doc-only"
    assert not key.startswith("REF:")


def test_a_reference_in_the_mail_still_wins_over_the_document():
    document = identifiers.from_text("Booking No: ABC123\n")
    key, _ = versioning.identify_shipment(
        _mail(subject="Shipping Instruction - SHP-777"), {}, {}, document)
    assert key == "REF:SHP-777"


def test_attachment_filenames_are_searched_with_a_word_boundary():
    """Documented limitation, kept deliberately.

    A hyphenated reference inside a filename is found. An underscore-joined one
    (`SHP-555_SI.pdf`) is not, because `REFERENCE_RE` requires a word boundary
    after the code — and relaxing that would make the corpus's own
    `email_001_SI.txt` naming read as reference "EMAIL_001". The mail body or
    the document itself still supply the reference in that case.
    """
    found, _ = versioning.identify_shipment(
        _mail(attachments=["SHP-555-BL.pdf"]), {}, {},
        identifiers.DocumentIdentifiers())
    assert found == "REF:SHP-555"

    missed, _ = versioning.identify_shipment(
        _mail(attachments=["SHP-555_SI.pdf"]), {}, {},
        identifiers.DocumentIdentifiers())
    assert missed == "EMAIL:mail-1"


def test_a_document_reference_identifies_every_email_of_that_shipment():
    """Two emails, no reference in either mail — both must land on one shipment."""
    document = identifiers.from_text("Booking No: ABC123\n")
    first, _ = versioning.identify_shipment(
        _mail(email_id="a", subject="si attached"), {}, {}, document)
    second, _ = versioning.identify_shipment(
        _mail(email_id="b", subject="bl attached"), {}, {}, document)
    assert first == second == "REF:ABC123"


# --------------------------------------------------------------------- J2
def test_same_route_different_containers_stay_two_shipments():
    """The false-merge case: same shipper, same POL, same POD."""
    a = identifiers.from_text("Container No.: ABCU1111111\n")
    b = identifiers.from_text("Container No.: ABCU2222222\n")
    key_a, _ = versioning.identify_shipment(_mail(email_id="A"), ROUTE, {}, a)
    key_b, _ = versioning.identify_shipment(_mail(email_id="B"), ROUTE, {}, b)
    assert key_a.startswith("SIG:")
    assert key_b.startswith("SIG:")
    assert key_a != key_b, "two different shipments on one route must not merge"


def test_same_route_different_vessel_and_voyage_stay_two_shipments():
    a = identifiers.from_text("Vessel: EVER GLORY\nVoy. No: 026E\n")
    b = identifiers.from_text("Vessel: MSC LUCIA\nVoy. No: 118W\n")
    key_a, _ = versioning.identify_shipment(_mail(email_id="A"), ROUTE, {}, a)
    key_b, _ = versioning.identify_shipment(_mail(email_id="B"), ROUTE, {}, b)
    assert key_a != key_b


def test_one_shipments_two_documents_still_group_together():
    """The existing same-shipment behaviour must survive the J2 change."""
    document = identifiers.from_text(
        "Container No.: ABCU1111111\nVessel: EVER GLORY\nVoy. No: 026E\n")
    si, _ = versioning.identify_shipment(_mail(email_id="si"), ROUTE, {}, document)
    bl, _ = versioning.identify_shipment(_mail(email_id="bl"), ROUTE, {}, document)
    assert si == bl
    assert si.startswith("SIG:")


def test_the_signature_is_stable_regardless_of_field_order():
    a = versioning.identify_shipment(_mail(), ROUTE, {}, None)[0]
    b = versioning.identify_shipment(
        _mail(), {"port_of_discharge": ROUTE["port_of_discharge"],
                  "shipper": ROUTE["shipper"],
                  "port_of_loading": ROUTE["port_of_loading"]}, {}, None)[0]
    assert a == b


def test_without_any_distinguishing_signal_grouping_falls_back_to_the_signature():
    """Documented residual: nothing distinguishes the two, so they group.

    The signature cannot tell apart two shipments that share a shipper, a route
    and carry no container, vessel or reference — there is no signal to use.
    """
    key_a, _ = versioning.identify_shipment(_mail(email_id="A"), ROUTE, {}, None)
    key_b, _ = versioning.identify_shipment(_mail(email_id="B"), ROUTE, {}, None)
    assert key_a == key_b


def test_a_route_with_no_shipper_still_falls_back_to_the_email():
    key, _ = versioning.identify_shipment(
        _mail(email_id="solo"), {}, {}, identifiers.DocumentIdentifiers())
    assert key == "EMAIL:solo"
