import re
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from threading import Lock
from typing import Literal
from xml.sax.saxutils import escape

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle


GREEN = "123f36"
MUTED = "687a73"
STATUS_COLORS = {
    "auto_approved": "246a52", "approved": "246a52", "corrected": "527b91",
    "pending": "b78b39", "rejected": "a64844",
}
# Matplotlib and shared PDF font registration are not thread-safe.
_RENDER_LOCK = Lock()


def _text(value) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value))


def _number(value) -> str:
    number = format(Decimal(str(value)), ",f")
    return number.rstrip("0").rstrip(".") if "." in number else number


def _value(value, unit="USD") -> str:
    if value is None:
        return "Unavailable"
    number = _number(value)
    return f"${number} {unit}" if unit in {"USD", "USD/shares"} else f"{number} {unit}"


def _percent(value) -> str:
    return f"{_number(Decimal(str(value)) * 100)}%"


def _label(value) -> str:
    return str(value).replace("_", " ")


def _charts(metrics: list[dict], threshold: float) -> list[tuple[str, bytes]]:
    charts = []
    financial = [m for m in metrics if m["unit"] == "USD" and m["value"] is not None and m["status"] != "rejected"]
    for kind, rows in (("Financial snapshot", financial), ("Extraction confidence", metrics)):
        if not rows:
            continue
        figure = Figure(figsize=(9, 4.2), dpi=160, facecolor="white")
        FigureCanvasAgg(figure)
        axis = figure.add_subplot(111)
        confidence = kind == "Extraction confidence"
        values = [m["confidence"] * 100 if confidence else m["value"] / 1_000_000 for m in rows]
        labels = [f"{m['label']}  [{_label(m['status'])}]" for m in rows]
        bars = axis.barh(range(len(rows)), values, height=0.55, color=[f"#{STATUS_COLORS.get(m['status'], MUTED)}" for m in rows])
        axis.set_yticks(range(len(rows)), labels=labels, fontsize=8)
        axis.invert_yaxis()
        axis.set_xlabel("Extraction confidence (%)" if confidence else "USD millions • current values, not additive", fontsize=9, color=f"#{MUTED}")
        axis.tick_params(axis="both", length=0, labelsize=8, colors=f"#{MUTED}")
        axis.xaxis.grid(True, color="#e3e8e1", linewidth=0.6)
        axis.set_axisbelow(True)
        for spine in axis.spines.values():
            spine.set_visible(False)
        axis.axvline(0, color="#687a73", linewidth=0.7)
        if confidence:
            axis.set_xlim(0, 116)
            axis.set_xticks([0, 25, 50, 75, 100])
            axis.axvline(threshold * 100, color=f"#{GREEN}", linestyle="--", linewidth=1)
            axis.set_title(f"Review threshold: {_percent(threshold)}", loc="left", fontsize=10, color=f"#{GREEN}", pad=14)
        else:
            axis.margins(x=0.23)
        for bar, value in zip(bars, values):
            axis.annotate(f"{value:.2f}".rstrip("0").rstrip(".") + ("%" if confidence else ""),
                          (value, bar.get_y() + bar.get_height() / 2),
                          xytext=(4 if value >= 0 else -4, 0), textcoords="offset points",
                          ha="left" if value >= 0 else "right", va="center", fontsize=8, color=f"#{GREEN}")
        figure.tight_layout(pad=1.2)
        output = BytesIO()
        figure.savefig(output, format="png", dpi=160)
        charts.append((kind, output.getvalue()))
        figure.clear()
    return charts


def _content(run: dict) -> list[tuple[str, object]]:
    state = run["state"]
    company = state["company"]
    filing = state.get("filing") or {}
    metrics = state.get("metrics", [])
    report = state.get("report")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pending = sum(m["status"] == "pending" for m in metrics)
    completed = run["status"] == "completed"
    blocks = [
        ("brand", "FinFlow."),
        ("title", "Financial report"),
        ("subtitle", f"{company['name']} ({company['ticker']})"),
        ("text", f"{'Completed audit snapshot' if completed else 'PROVISIONAL AUDIT SNAPSHOT'} | Status: {_label(run['status'])}"),
        ("text", f"Generated: {generated}\nRun ID: {run['id']}\nAudit created: {run.get('created_at', 'Unavailable')}\nLast saved: {run.get('updated_at', run.get('created_at', 'Unavailable'))}"),
        ("text", f"Form 10-K | Reporting date: {filing.get('report_date', 'Unavailable')}\nAccession: {filing.get('accession', 'Unavailable')}\nSource: {filing.get('url', 'Unavailable')}"),
    ]
    if filing.get("offline") or company["ticker"] == "DEMO":
        blocks.append(("notice", "OFFLINE DEMO DATA — a deterministic sample, not a live company's financial statement."))
    if not completed:
        blocks.append(("notice", f"This audit is not complete. {pending} metric(s) await review. Pending values are unapproved; final XBRL accuracy is not yet available."))
    if run.get("error"):
        blocks.append(("notice", f"Audit error: {run['error']}"))
    accuracy = "Pending — final verification not run"
    comparison = "Not yet compared"
    if report is not None:
        accuracy = "Unavailable — no comparable XBRL values" if report["accuracy_percentage"] is None else f"{_number(report['accuracy_percentage'])}%"
        comparison = f"{report['ground_truth_matches']} of {report['ground_truth_compared']} values matched"
    outcomes = [(name, sum(m["status"] == status for m in metrics)) for name, status in [
        ("Automatically approved", "auto_approved"), ("Human approved", "approved"),
        ("Corrected", "corrected"), ("Rejected", "rejected"), ("Pending reviews", "pending"),
    ]]
    blocks.extend([
        ("heading", "01 / Audit at a glance"),
        ("table", [["Measure", "Current result"], ["Metrics extracted", str(len(metrics))],
                   *[[name, str(count)] for name, count in outcomes],
                   ["XBRL accuracy", accuracy], ["XBRL comparison", comparison]]),
        ("text", f"Confidence threshold: {_percent(state['confidence_threshold'])} | YoY variance threshold: {_percent(state['variance_threshold'])}"),
        ("text", "XBRL accuracy is the stored match rate for metrics with available Company Facts, not an assurance opinion. Rejected or missing values count as mismatches when a reference value exists. Unavailable reference values are excluded from the comparison denominator."),
    ])
    if metrics:
        blocks.extend([
            ("page", None), ("heading", "02 / Visual overview"),
            ("text", "Current values from this audit only. Pending values are shown as unapproved; rejected and missing dollar values are omitted. EPS is excluded from the dollar chart. Stocks and annual flows are distinct measures and must not be added together. Chart labels are rounded; exact values and periods follow."),
        ])
        for title, image in _charts(metrics, state["confidence_threshold"]):
            blocks.extend([("subheading", title), ("image", image)])
        blocks.extend([
            ("page", None), ("heading", "03 / Current financial metrics"),
            ("table", [["Metric", "Current value / unit", "Period end", "Confidence", "Review status"], *[
                [m["label"], _value(m["value"], m["unit"]), m["period"], _percent(m["confidence"]), _label(m["status"])] for m in metrics
            ]]),
            ("text", "Values retain the precision stored in the audit. Unavailable is not zero. Confidence describes extraction reliability, not a probability of financial correctness."),
            ("heading", "04 / Evidence and validation details"),
            ("text", "Validation results and metric explanations are retained from initial extraction; they are not rerun after corrections. Current values, review decisions and final XBRL comparisons are reported separately below."),
        ])
    else:
        blocks.append(("notice", "No financial metrics are available in this run. No financial or confidence chart can be generated."))
    for metric in metrics:
        unit = metric["unit"]
        blocks.extend([
            ("subheading", metric["label"]),
            ("text", f"Current: {_value(metric['value'], unit)} | Status: {_label(metric['status'])}\nOriginal extracted: {_value(metric.get('original_value'), unit)}\nPrior period: {_value(metric.get('previous_value'), unit)}\nReporting context: {metric.get('start_date') or 'Instant'} to {metric.get('end_date') or metric['period']}\nSource tag: {metric.get('source_name', 'Unavailable')} | Context ID: {metric.get('context_ref') or 'Unavailable'}"),
            ("text", f"Source evidence: {metric.get('source_snippet') or 'Unavailable'}\nSource location: {metric.get('source_url') or filing.get('url', 'Unavailable')}"),
        ])
        truth = metric.get("ground_truth")
        verification = metric.get("verification")
        result = _label(verification["status"]) if verification else "Pending — final verification not run"
        if truth:
            blocks.append(("text", f"XBRL reference: {_value(truth['value'], unit)} | {truth.get('tag', 'Unavailable')} | Filed: {truth.get('filed', 'Unavailable')}\nFinal XBRL result: {result}"))
        else:
            blocks.append(("text", "XBRL reference: Unavailable — no matching Company Facts value for this reporting context."))
        for check in metric.get("validations", []):
            blocks.append(("text", f"Initial validation / {_label(check['rule'])}: {check['status']} — {check['message']}"))
        explanation = metric.get("explanation")
        if explanation:
            attribution = f"AI-generated ({explanation.get('model') or 'model not recorded'})" if explanation["generated_by"] == "llm" else "Rule-based fallback"
            blocks.append(("text", f"Initial metric explanation — {attribution}: {explanation['summary']}"))
            if explanation.get("extraction_reason"):
                blocks.append(("text", f"Why this value was extracted: {explanation['extraction_reason']}"))
            for cause in explanation.get("possible_causes", []):
                blocks.append(("text", f"Possible cause (not a confirmed fact): {cause}"))
            blocks.append(("text", f"Reviewer guidance: {explanation.get('review_guidance', 'Unavailable')}"))
    blocks.extend([("page", None), ("heading", "05 / Human decisions and accountability")])
    audit = run.get("audit_log", [])
    integrity = run.get("audit_verification", {})
    if audit:
        blocks.append(("notice", f"Hash-chain integrity: {'VERIFIED' if integrity.get('valid') else 'FAILED — investigate before relying on this record'} | {len(audit)} decisions"))
    else:
        blocks.append(("text", "No human decisions recorded."))
    for review in run.get("reviews", []):
        if review["status"] == "pending":
            blocks.append(("text", f"Pending review: {review.get('label', _label(review['metric_key']))} — {review.get('reason', 'Awaiting reviewer decision')}"))
    by_key = {m["key"]: m for m in metrics}
    for entry in audit:
        metric = by_key[entry["metric_key"]]
        blocks.extend([
            ("subheading", f"{metric['label']} — {_label(entry['decision'])}"),
            ("text", f"Reviewer: {entry['reviewer']} | Timestamp: {entry.get('timestamp', 'Unavailable')}\nOriginal: {_value(entry.get('original_value'), metric['unit'])}\nCorrected: {_value(entry.get('corrected_value'), metric['unit'])}\nNote: {entry.get('note') or 'No note supplied'}\nEntry hash: {entry.get('entry_hash', 'Unavailable')}"),
        ])
    blocks.extend([
        ("heading", "Report scope"),
        ("text", "Generated deterministically from the selected saved FinFlow audit at export time. No new SEC retrieval, model call, reviewer decision or financial forecast is performed. Existing AI explanations, if included, are labeled and are not independently verified. This report is an audit-workflow snapshot, not a certified financial statement, independent audit opinion or investment recommendation."),
    ])
    return blocks


def _pdf(blocks: list[tuple[str, object]]) -> bytes:
    for name, weight in (("FinFlow", "normal"), ("FinFlow-Bold", "bold")):
        if name not in pdfmetrics.getRegisteredFontNames():
            font = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans", weight=weight))
            pdfmetrics.registerFont(TTFont(name, font))
    output = BytesIO()
    document = SimpleDocTemplate(output, pagesize=A4, rightMargin=40, leftMargin=40,
                                 topMargin=48, bottomMargin=48, title="FinFlow Financial report", author="FinFlow")
    base = ParagraphStyle("Body", fontName="FinFlow", fontSize=9, leading=14,
                          textColor=colors.HexColor(f"#{GREEN}"), spaceAfter=9, splitLongWords=True)
    styles = {
        "text": base,
        "brand": ParagraphStyle("Brand", parent=base, fontName="FinFlow-Bold", fontSize=26, leading=32, spaceAfter=16),
        "title": ParagraphStyle("Title", parent=base, fontName="FinFlow-Bold", fontSize=30, leading=36, spaceAfter=12),
        "subtitle": ParagraphStyle("Subtitle", parent=base, fontSize=16, leading=23, spaceAfter=14),
        "heading": ParagraphStyle("Heading", parent=base, fontName="FinFlow-Bold", fontSize=15, leading=21, spaceBefore=17, spaceAfter=12, keepWithNext=True),
        "subheading": ParagraphStyle("Subheading", parent=base, fontName="FinFlow-Bold", fontSize=11, leading=16, spaceBefore=13, keepWithNext=True),
        "notice": ParagraphStyle("Notice", parent=base, backColor=colors.HexColor("#edf5e9"), borderPadding=8, spaceBefore=9, spaceAfter=17),
        "cell": ParagraphStyle("Cell", parent=base, fontSize=8, leading=12, spaceAfter=0),
        "header": ParagraphStyle("Header", parent=base, fontName="FinFlow-Bold", fontSize=8, leading=12, textColor=colors.white, spaceAfter=0),
    }

    def paragraph(text, style):
        return Paragraph(escape(_text(text)).replace("\n", "<br/>"), styles[style])

    story = []
    for kind, value in blocks:
        if kind == "page":
            story.append(PageBreak())
        elif kind == "image":
            story.extend([Image(BytesIO(value), width=document.width, height=document.width * 4.2 / 9), Spacer(1, 12)])
        elif kind == "table":
            widths = [0.46, 0.54] if len(value[0]) == 2 else [0.24, 0.26, 0.17, 0.13, 0.20]
            rows = [[paragraph(cell, "header" if i == 0 else "cell") for cell in row] for i, row in enumerate(value)]
            table = LongTable(rows, colWidths=[document.width * w for w in widths], repeatRows=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(f"#{GREEN}")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#f3f6ef"), colors.white]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e3e8e1")),
            ]))
            story.extend([table, Spacer(1, 14)])
        else:
            story.append(paragraph(value, kind))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#e3e8e1"))
        canvas.line(40, 37, A4[0] - 40, 37)
        canvas.setFont("FinFlow", 8)
        canvas.setFillColor(colors.HexColor(f"#{MUTED}"))
        canvas.drawString(40, 24, "FinFlow | Evidence before automation")
        canvas.drawRightString(A4[0] - 40, 24, f"Page {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()


def _word(blocks: list[tuple[str, object]]) -> bytes:
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.top_margin = section.bottom_margin = Inches(0.65)
    section.left_margin = section.right_margin = Inches(0.65)
    normal = document.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal.font.color.rgb = RGBColor.from_string(GREEN)
    normal.paragraph_format.space_after = Pt(8)
    for name in ("Title", "Subtitle", "Heading 1", "Heading 2"):
        document.styles[name].font.color.rgb = RGBColor.from_string(GREEN)
    document.core_properties.title = "FinFlow Financial report"
    document.core_properties.author = "FinFlow"
    for kind, value in blocks:
        if kind == "page":
            document.add_page_break()
        elif kind == "image":
            document.add_picture(BytesIO(value), width=Inches(6.95))
        elif kind == "table":
            table = document.add_table(rows=0, cols=len(value[0]))
            table.style = "Light Shading Accent 1"
            for index, row in enumerate(value):
                cells = table.add_row().cells
                for cell, text in zip(cells, row):
                    cell.text = _text(text)
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.font.size = Pt(9)
                            if index == 0:
                                run.font.bold = True
                                run.font.color.rgb = RGBColor(255, 255, 255)
                    shading = OxmlElement("w:shd")
                    shading.set(qn("w:fill"), GREEN if index == 0 else ("F3F6EF" if index % 2 else "FFFFFF"))
                    cell._tc.get_or_add_tcPr().append(shading)
                if index == 0:
                    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
            document.add_paragraph()
        else:
            style = {"brand": "Title", "title": "Title", "subtitle": "Subtitle", "heading": "Heading 1", "subheading": "Heading 2"}.get(kind)
            paragraph = document.add_paragraph(_text(value), style=style)
            if kind == "notice":
                for run in paragraph.runs:
                    run.bold = True
    footer = section.footer.paragraphs[0]
    footer.add_run("FinFlow | Financial report     •     Page ")
    field = OxmlElement("w:fldSimple")
    field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def generate_report(run: dict, format: Literal["pdf", "docx"]) -> bytes:
    if format not in {"pdf", "docx"}:
        raise ValueError("Choose pdf or docx")
    with _RENDER_LOCK:
        blocks = _content(run)
        return _pdf(blocks) if format == "pdf" else _word(blocks)
