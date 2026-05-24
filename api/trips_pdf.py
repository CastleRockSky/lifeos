"""
trips_pdf.py — IRS-style PDF rendering for the business-mileage report
(Auto-redesign Phase 9).

Kept separate from routers/trips.py so the JSON report still works even if
reportlab is missing (which can happen briefly between requirements.txt
updates and the image rebuild). The router imports this lazily.
"""

from datetime import date
from typing import IO

from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


_BUSINESS_NAME = "Castle Rock Sky"


def _fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def _fmt_miles(value: float) -> str:
    return f"{value:,.1f}"


def render_trip_report_pdf(out: IO[bytes], report: dict) -> None:
    """Write a multi-page IRS-style report to ``out``. ``report`` is the
    dict produced by ``compute_trip_report``."""
    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    small = ParagraphStyle("small", parent=body, fontSize=8, leading=10,
                           textColor=colors.grey)

    doc = SimpleDocTemplate(
        out, pagesize=LETTER,
        leftMargin=0.6 * inch, rightMargin=0.6 * inch,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        title=f"Business mileage — tax year {report['tax_year']}",
        author=_BUSINESS_NAME,
    )

    elements = []

    # ── Header ──────────────────────────────────────────────────────────
    elements.append(Paragraph(
        f"{_BUSINESS_NAME} — Business Mileage Report", h1,
    ))
    elements.append(Paragraph(f"Tax year {report['tax_year']}", h2))
    veh = report.get("vehicle") or {}
    if veh:
        veh_line = " ".join(
            str(p) for p in [veh.get("year"), veh.get("make"),
                             veh.get("model")] if p
        )
        if veh.get("vin"):
            veh_line += f" · VIN {veh['vin']}"
        elements.append(Paragraph(veh_line, body))
    elements.append(Paragraph(
        f"Generated {date.today().isoformat()}", small,
    ))
    elements.append(Spacer(1, 0.2 * inch))

    # ── Summary ─────────────────────────────────────────────────────────
    # When multiple rates apply (e.g. 2022 mid-year change) the report
    # holds the weighted total in total_deduction; no single rate is
    # accurate enough to print, so just show the total.
    summary_rows = [
        ["Trips", str(report["trip_count"])],
        ["Total business miles", _fmt_miles(report["total_business_miles"])],
        ["Total deduction (IRS standard rate)", _fmt_money(report["total_deduction"])],
    ]
    summary_table = Table(summary_rows, colWidths=[3 * inch, 3 * inch])
    summary_table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 0.2 * inch))

    if report.get("missing_rate_years"):
        elements.append(Paragraph(
            "⚠ Missing IRS rates for: "
            + ", ".join(str(y) for y in report["missing_rate_years"])
            + " (those trips contribute miles but $0 deduction in this report).",
            small,
        ))
        elements.append(Spacer(1, 0.1 * inch))

    # ── By quarter ──────────────────────────────────────────────────────
    if report.get("by_quarter"):
        elements.append(Paragraph("By quarter", h2))
        q_rows = [["Quarter", "Miles", "Deduction"]]
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            v = report["by_quarter"].get(q)
            if not v:
                continue
            q_rows.append([q, _fmt_miles(v["miles"]), _fmt_money(v["deduction"])])
        q_table = Table(q_rows, colWidths=[1 * inch, 2 * inch, 2 * inch])
        q_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ]))
        elements.append(q_table)
        elements.append(Spacer(1, 0.2 * inch))

    # ── By client ───────────────────────────────────────────────────────
    if report.get("by_client"):
        elements.append(Paragraph("By client", h2))
        c_rows = [["Client", "Miles", "Deduction"]]
        for client, v in report["by_client"].items():
            c_rows.append([client, _fmt_miles(v["miles"]),
                           _fmt_money(v["deduction"])])
        c_table = Table(c_rows, colWidths=[3 * inch, 1.5 * inch, 1.5 * inch])
        c_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ]))
        elements.append(c_table)
        elements.append(Spacer(1, 0.3 * inch))

    # ── Trip detail (page break, multi-page table) ─────────────────────
    if report.get("trips"):
        elements.append(Paragraph("Trip detail", h2))
        # Wrap longer text in Paragraph so reportlab handles line wrap.
        wrap = ParagraphStyle("wrap", parent=body, fontSize=8, leading=10)
        t_rows = [["Date", "Miles", "Rate", "Deduction", "Purpose", "Client"]]
        for t in report["trips"]:
            t_rows.append([
                t.get("date") or "",
                _fmt_miles(float(t.get("miles") or 0)),
                _fmt_money(t.get("rate_used") or 0) + "/mi" if t.get("rate_used") else "—",
                _fmt_money(t.get("deduction") or 0),
                Paragraph(t.get("purpose") or "", wrap),
                Paragraph(t.get("client") or "", wrap),
            ])
        t_table = Table(
            t_rows,
            colWidths=[0.85 * inch, 0.65 * inch, 0.8 * inch, 0.85 * inch,
                       2.4 * inch, 1.55 * inch],
            repeatRows=1,
        )
        t_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("GRID", (0, 0), (-1, -1), 0.2, colors.lightgrey),
        ]))
        elements.append(t_table)

    doc.build(elements)
