import sys
import subprocess

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import pypdf
except ImportError:
    install('pypdf')
    import pypdf

from pypdf import PdfReader

pdf_path = r"e:\smart-traffic-system\references\System Report (1).pdf"
txt_path = r"e:\smart-traffic-system\references\report_text.txt"

reader = PdfReader(pdf_path)
text = ""
for page in reader.pages:
    text += page.extract_text() + "\n"

with open(txt_path, "w", encoding="utf-8") as f:
    f.write(text)

print("Extraction complete.")
