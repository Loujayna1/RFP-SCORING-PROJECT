import asyncio, sys
from weasyprint import HTML
from app.worker import _fetch_report_data, _generate_pdf_html

async def main(rfp_id):
    data = await _fetch_report_data(rfp_id)
    html = _generate_pdf_html(data, narrative=None)
    pdf = HTML(string=html).write_pdf()
    print(f"OK: comparison PDF rendered for {rfp_id}: {len(pdf)} bytes, header={pdf[:5]}")

asyncio.run(main(sys.argv[1]))
