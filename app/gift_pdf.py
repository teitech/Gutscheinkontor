"""Gutschein als PDF (zum Speichern, Ausdrucken oder Weitergeben).

Gebaut mit reportlab, ohne externe Dienste. Die eingebauten Schriften (Helvetica, Courier) kennen
Umlaute und das Euro-Zeichen, aber keine Emojis oder fremde Alphabete: Solche Zeichen werden durch
"?" ersetzt, statt das PDF zu zerstören.
"""
from __future__ import annotations

import io
from typing import Any, List, Mapping, Optional

from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas

NAVY = (0.035, 0.106, 0.267)
BLUE = (0.047, 0.365, 0.741)
YELLOW = (0.992, 0.702, 0.027)
GREY = (0.35, 0.38, 0.45)

MARGIN = 50
PAGE_W, PAGE_H = A4


def _safe(text: str) -> str:
    """Nur Zeichen, die die eingebauten PDF-Schriften darstellen können."""
    cleaned = "".join(c if (c == "\n" or ord(c) >= 32) else " " for c in text)
    return cleaned.encode("cp1252", errors="replace").decode("cp1252")


def _benefit_line(v: Mapping[str, Any]) -> str:
    if v["kind"] == "percent":
        return f"{v['percent']} % Rabatt"
    if v["kind"] == "other":
        return v["benefit_text"]
    if v["status_key"] == "teilweise":
        return f"{v['remaining_text']} (von {v['value_text']})"
    return v["remaining_text"] or v["value_text"] or ""


def build_pdf(view: Mapping[str, Any], code: str, logo_png: Optional[bytes] = None) -> bytes:
    """Erzeugt das PDF. `view` ist die Ansicht aus vouchers.to_view()."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, pageCompression=1)
    pdf.setTitle(_safe(f"Gutschein {view['title']}"))
    pdf.setAuthor("Gutscheinkontor")
    pdf.setCreator("Gutscheinkontor")

    # Farbband oben
    pdf.setFillColorRGB(*NAVY)
    pdf.rect(0, PAGE_H - 24, PAGE_W, 24, stroke=0, fill=1)
    pdf.setFillColorRGB(*YELLOW)
    pdf.rect(0, PAGE_H - 28, PAGE_W, 4, stroke=0, fill=1)

    y = PAGE_H - 90
    x_text = MARGIN
    if logo_png:
        try:
            pdf.drawImage(ImageReader(io.BytesIO(logo_png)), MARGIN, y - 22, width=56, height=56,
                          mask="auto", preserveAspectRatio=True)
            x_text = MARGIN + 72
        except Exception:  # noqa: BLE001 - ein kaputtes Logo darf das PDF nicht verhindern
            x_text = MARGIN

    pdf.setFillColorRGB(*NAVY)
    pdf.setFont("Helvetica-Bold", 24)
    title_lines = simpleSplit(_safe(view["title"]), "Helvetica-Bold", 24, PAGE_W - MARGIN - x_text)[:2]
    for line in title_lines:
        pdf.drawString(x_text, y, line)
        y -= 28
    y = min(y, PAGE_H - 90 - 28) - 12

    pdf.setFont("Helvetica-Bold", 30)
    pdf.setFillColorRGB(*BLUE)
    benefit = _safe(_benefit_line(view))
    if benefit:
        for line in simpleSplit(benefit, "Helvetica-Bold", 30, PAGE_W - 2 * MARGIN)[:2]:
            pdf.drawString(MARGIN, y - 20, line)
            y -= 36
        y -= 14

    # Eckdaten
    facts: List[tuple] = []
    if view.get("min_order_cents") is not None:
        facts.append(("Mindestbestellwert", view["min_order_text"]))
    facts.append(("Gültig bis", view["expires_text"] or "kein Ablaufdatum"))
    if view.get("online_only"):
        facts.append(("Einlösen", "nur online"))
    if view.get("shop_url"):
        facts.append(("Webshop", view["shop_url"]))
    for label, value in facts:
        pdf.setFillColorRGB(*GREY)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(MARGIN, y, _safe(label))
        pdf.setFillColorRGB(*NAVY)
        pdf.setFont("Helvetica-Bold", 12)
        lines = simpleSplit(_safe(value), "Helvetica-Bold", 12, PAGE_W - MARGIN - 190)[:3]
        for i, line in enumerate(lines):
            pdf.drawString(190, y - i * 15, line)
        y -= 22 + (len(lines) - 1) * 15
    y -= 16

    # Code in einem Rahmen
    box_h = 74
    pdf.setStrokeColorRGB(*NAVY)
    pdf.setFillColorRGB(0.99, 0.97, 0.92)
    pdf.setLineWidth(1.5)
    pdf.roundRect(MARGIN, y - box_h, PAGE_W - 2 * MARGIN, box_h, 8, stroke=1, fill=1)
    pdf.setFillColorRGB(*GREY)
    pdf.setFont("Helvetica", 10)
    pdf.drawString(MARGIN + 14, y - 20, "Gutscheincode")
    pdf.setFillColorRGB(*NAVY)
    code_text = _safe(code) if code else "kein Code hinterlegt"
    size = 26
    while size > 12 and pdf.stringWidth(code_text, "Courier-Bold", size) > PAGE_W - 2 * MARGIN - 28:
        size -= 2
    pdf.setFont("Courier-Bold", size)
    pdf.drawString(MARGIN + 14, y - 55, code_text)

    pdf.setFillColorRGB(*GREY)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(MARGIN, 40, "Erstellt mit Gutscheinkontor")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()
