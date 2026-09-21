# Mock email format corpus

100 synthetic emails based on official bundle subjects, bodies and SI/BL text.
Existing 520 emails are preserved; emails.json now contains 620 entries.
Mock IDs: mock_format_001 through mock_format_100. Subjects carry [MOCK nnn].
60 BL comparison, 20 SI request, 10 invoice query, 10 general shipping emails.
160 generated attachments in attachments/mock100/.

Formats: zip, rar, 7z, tar, gz, eml, rtf, odt, ods, edi, edifact, x12, json, dxf.
Not generated: msg, pages, numbers, key, heic, heif, dwg.
No files are disguised by merely changing extensions.
EDI/EDIFACT/X12 are minimal synthetic transaction messages, not certified partner documents.
Non-BL supporting documents contain source email context, not issued commercial invoices.
Category labels express demo intent, not official ground truth or verified pipeline outcomes.

mock_emails_100.json contains only the new messages in ShipMail schema.
mock_inbox_100/ contains per-message JSON in the official bundle schema.
mock_manifest_100.json records source IDs, attachment hashes and limitations.
Attachment paths are relative to this data directory.

Current ShipMail frontend loads OAuth Gmail APIs, not this static emails.json.
These files have NOT been imported into the OAuth database or sent by email.
The backend bundle-generation script can overwrite emails.json when rerun.
The timestamped emails.before-mock100-*.json is the original inbox backup.
