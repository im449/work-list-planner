from fpdf import FPDF
from datetime import datetime
import os

class PDFWithCyrillic(FPDF):
    def __init__(self):
        super().__init__()
        # Подключаем обычный и жирный шрифты DejaVu
        base_path = "/usr/share/fonts/truetype/dejavu"
        self.add_font("DejaVu", "", os.path.join(base_path, "DejaVuSans.ttf"), uni=True)
        self.add_font("DejaVu", "B", os.path.join(base_path, "DejaVuSans-Bold.ttf"), uni=True)

def _get_status(t):
    return t.status if hasattr(t, "status") else t["status"]

def _get_title(t):
    return t.title if hasattr(t, "title") else t["title"]

def generate_report_pdf(tasks):
    pdf = PDFWithCyrillic()
    pdf.add_page()
    
    # Заголовок
    pdf.set_font("DejaVu", size=14)
    pdf.cell(0, 10, txt="Work Report", ln=True, align="C")
    
    pdf.set_font("DejaVu", size=10)
    period = datetime.now().strftime("%d.%m.%Y")
    pdf.cell(0, 8, txt=f"Period: {period}", ln=True, align="C")
    pdf.ln(10)

    col_w = pdf.w / 3 - 10
    
    # Шапка таблицы — жирным шрифтом
    pdf.set_fill_color(240, 240, 240)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("DejaVu", "B", size=10)  # <-- теперь это реально жирный шрифт
    pdf.cell(col_w, 8, "TODO", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "IN PROGRESS", border=1, align="C", fill=True, ln=0)
    pdf.cell(col_w, 8, "DONE", border=1, align="C", fill=True, ln=1)

    # Группируем задачи
    todo = [t for t in tasks if _get_status(t) == "todo"]
    progress = [t for t in tasks if _get_status(t) == "progress"]
    done = [t for t in tasks if _get_status(t) == "done"]

    max_rows = max(len(todo), len(progress), len(done))

    # Тело таблицы — обычным шрифтом
    pdf.set_font("DejaVu", size=9)
    for i in range(max_rows):
        cells = []
        cells.append(_get_title(todo[i]) if i < len(todo) else "")
        cells.append(_get_title(progress[i]) if i < len(progress) else "")
        cells.append(_get_title(done[i]) if i < len(done) else "")

        for text in cells:
            display = (text[:40] + "...") if text and len(text) > 40 else (text or "")
            pdf.cell(col_w, 6, txt=display, border=1, ln=0, align="L")
        pdf.ln()

    return pdf.output(dest="S")
