# Additional 100 special-format mock emails

15 MSG, 15 Pages, and 14 each Numbers, Keynote, HEIC, HEIF, DWG.
186 attachments: SI+BL pairs for 86 emails; one wrong-document CAD attachment for 14 emails.
All email subjects and base bodies come from the official hackathon bundle.
IDs mock_special_001 through mock_special_100, subjects [MOCK-SPECIAL nnn].
Original 620 messages preserved. emails.json now contains 720 messages.

All formats are actual containers/documents, not renamed text files.
MSG subject/body, Pages body, Numbers cells and Keynote presenter notes were decoded and compared against source content.
HEIC/HEIF images contain rendered shipping documents and were decoded after encoding.
Keynote slides show a short document label; full SI/BL text is in presenter notes.
DWG conversion attempts failed validation, so real public LibreDWG CAD fixtures are included unchanged, with provenance and license. These are deliberately wrong document types and do not contain the source SI/BL text.
Native Apple iWork, Outlook and AutoCAD opening/rendering was not tested.
No claims are made that the application's pipeline supports these formats.

mock_special_emails_100.json contains only this batch.
mock_special_inbox_100/ contains official-bundle-schema per-email JSON.
mock_special_manifest_100.json has source IDs, hashes and validation details.
attachments/mock_special100/ contains all 186 attachments; paths are relative to this data directory.
mock_special_licenses/ contains template/fixture licenses and source URLs.

The current ShipMail frontend uses OAuth Gmail APIs, so this static dataset is not automatically visible there. No database import or email sending was performed.
The timestamped emails.before-special100-*.json backup contains the previous 620 emails.
