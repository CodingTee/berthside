import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from app.database import SessionLocal
from app.models import EmailRecord, ReportRecord

db = SessionLocal()

ok_list = []
mismatch_list = []

for r in db.query(ReportRecord).filter_by(category="BL_COMPARISON").all():
    e = db.query(EmailRecord).filter_by(email_id=r.email_id).first()
    if not e:
        continue
    att = [Path(a).name for a in (e.attachments or [])]
    if len(att) < 2:
        continue

    extracted = r.extracted or {}
    si = extracted.get("si", {})
    bl = extracted.get("bl", {})
    bkg = si.get("booking_number") or bl.get("booking_number") or ""
    vessel = si.get("vessel") or bl.get("vessel") or ""
    voyage = si.get("voyage") or bl.get("voyage") or ""

    field_results = r.field_results or []
    diffs = []
    for f in field_results:
        if not f.get("match", True):
            diffs.append(f"{f.get('field')}: SI='{f.get('si_value')}' vs BL='{f.get('bl_value')}'")

    has_pdf = any(a.lower().endswith(".pdf") for a in att)

    info = {
        "email_id": e.email_id,
        "subject": e.subject,
        "sender": e.sender,
        "attachments": att,
        "booking_number": bkg,
        "vessel_voyage": f"{vessel} / {voyage}".strip(" /"),
        "defect_fields": r.defect_fields or [],
        "diff_details": diffs,
        "has_pdf": has_pdf,
    }
    if r.status == "OK":
        ok_list.append(info)
    elif r.status == "MISMATCH":
        mismatch_list.append(info)

print("=========================================================================")
print(f"📊 Bundle 单证比对概况: 共有 {len(ok_list)} 封 100% 匹配 (OK)，{len(mismatch_list)} 封 存在差异 (MISMATCH)")
print("=========================================================================")

print("\n-------------------------------------------------------------------------")
print("🟢 【推荐】100% 匹配通过 (OK / 黄金直通流) —— 模拟全绿直通")
print("-------------------------------------------------------------------------")

# Priority: PDF examples first, then clean txt
ok_pdf = [x for x in ok_list if x["has_pdf"]]
ok_txt = [x for x in ok_list if not x["has_pdf"]]
show_ok = ok_pdf[:4] + ok_txt[:4]

for idx, it in enumerate(show_ok[:6], 1):
    doc_type = "📄 PDF 真实单证" if it["has_pdf"] else "📝 文本单证"
    print(f"\n【匹配案例 {idx}】{it['email_id']} ({doc_type})")
    print(f"  • Booking 号: {it['booking_number'] or '(见附件内容)'}")
    print(f"  • 船名航次: {it['vessel_voyage'] or '(见附件内容)'}")
    print(f"  • 建议客户发件人: {it['sender']}")
    print(f"  • 邮件主题: {it['subject']}")
    print(f"  • 附件文件: {', '.join(it['attachments'])}")

print("\n-------------------------------------------------------------------------")
print("🔴 【推荐】存在单证差异 (MISMATCH / 异常拦截流) —— 模拟拦截审核")
print("-------------------------------------------------------------------------")

mis_pdf = [x for x in mismatch_list if x["has_pdf"]]
mis_txt = [x for x in mismatch_list if not x["has_pdf"]]
show_mis = mis_pdf[:4] + mis_txt[:4]

for idx, it in enumerate(show_mis[:6], 1):
    doc_type = "📄 PDF 真实单证" if it["has_pdf"] else "📝 文本单证"
    print(f"\n【差异案例 {idx}】{it['email_id']} ({doc_type})")
    print(f"  • 差异项: {', '.join(it['defect_fields'])}")
    print(f"  • 建议客户发件人: {it['sender']}")
    print(f"  • 邮件主题: {it['subject']}")
    print(f"  • 附件文件: {', '.join(it['attachments'])}")
    print("  • 具体差异对比:")
    for d in it["diff_details"][:3]:
        print(f"     ↳ ❌ {d}")
